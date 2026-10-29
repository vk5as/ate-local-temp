"""The Lua host: a fresh lupa runtime per schedule load, with just enough to run.

Ticket 02's slice of the engine. Real Lua execution is deliberately stubbed here —
the host keeps the exact interface later tickets need (version gate, enums as Lua
tables, the ``ScheduleParams``/``StepParams`` builders, and a minimal ``ATE`` table),
but it runs canned step actions instead of talking to hardware.

Real Lua execution is **deferred** by design: this module never imports widgets or
any device driver, so it stays testable under an offscreen Qt event loop. Later
tickets replace the stubbed action closures with real Device I/O behind the same
``ScheduleParams``/``StepParams`` interface.

Bridging facts (validated empirically against lupa 2.8):

* ``register_eval=False, register_builtins=False, unpack_returned_tuples=True`` — no
  Python leaks into Lua; only ATE-provided globals are visible.
* The standard library still ships ``dofile``/``loadfile``/``load``, so they are shadowed to
  nil and ``python`` is set to nil in :meth:`_LuaHost._lockdown`; a schedule cannot load files
  or reach Python, and calling a removed name raises "attempt to call a nil value".
* Loaded chunks run with the name ``@<path>`` in text mode (see :meth:`_load_schedule`), so an
  error's traceback carries the schedule's own file:line.
* Enums must be defined by executing an *assignment chunk* (``Result = { ... }``); the
  table then persists as a real Lua global that later chunks can read. Assigning the
  value lua.execute returns back onto the global env does **not** work — it comes back
  as ``None`` and clobbers the global with nil.
* Builder classes (``ScheduleParams``/``StepParams``) and ATE functions are registered
  on the runtime's global environment by assigning them there; Lua calls them normally.
* Each schedule instance (packet 15b) runs with its own ``_ENV`` table that reads through to
  that shared global environment, loaded with ``load(code, name, "t", env)`` (Lua 5.5). Lua may
  not set attributes on Python objects (lupa ``attribute_filter``), and the enums are read-only.
* Steps read back from a loaded schedule: enumerate the step table's keys for the
  count, then index with **1-based** values to get the real builder objects (iterating
  the proxy yields indices, not steps). Each step's action is a closure that runs in
  Lua when called.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import math
import re
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


def _sha256_hex(data: bytes) -> str:
    """SHA-256 of ``data`` as lowercase hex (packet 06b)."""
    return hashlib.sha256(data).hexdigest()


# Packet 06d: the reserved `ate.` namespace resolves to modules bundled with ATE, never to the Test
# Definition. Only `ate.<word>` is a name; anything else is rejected outright so it cannot walk out of
# the bundled directory. importlib.resources finds ate/lua/ in a wheel as well as a source checkout.
_ATE_BUNDLED_RE = re.compile(r"ate\.[A-Za-z0-9_]+\Z")


def _bundled_module_bytes(name: str) -> Optional[bytes]:
    """The shipped bytes of bundled module ``name`` (``ate.devices`` -> ``ate/lua/devices.lua``)."""
    try:
        return (importlib.resources.files("ate") / "lua" / f"{name.split('.', 1)[1]}.lua").read_bytes()
    except (OSError, ModuleNotFoundError):
        return None

from ..devices import DeviceRegistry, TcpDriver
from ..devices.serial import SerialDriver  # packet 18a: the SERIAL driver, registered like TCP
from ..devices.udp import UdpDriver  # packet 18b: UDP datagrams, registered like the other streams
from ..devices.visa import VisaDriver  # packet 19: VISA instruments, read on demand (no reader)
from ..devices.registry import DEFAULT_RECEIVE_BUFFER_BYTES, validate_params  # packet 16b: ATE.read/ATE.write validate per-call overrides
from ..devices.tracing import DEFAULT_TRACE_MAX_BYTES  # packet 17c: the default deviceTrace.maxBytes
from ..errors import ATEError
from .operator_input import OperatorInput
from .step_inputs import InvalidStepInput, StepInput, validate_inputs


class NotAllowedError(ATEError):
    """ATE was called where it isn't allowed (criterion b).

    Raised by :meth:`_AteTable.report` and :meth:`_AteTable.input` when the host's current
    execution context makes the call illegal — e.g. ``ATE.report``/``ATE.input`` at load time, or
    inside a Pre-test/Post-test Action instead of a step. The message carries the schedule's own
    ``<file>:<line>`` and names the function, so it reads as
    ``schedule.tslua:42: ATE.report: only allowed in a step action``."""


class HostError(ATEError):
    """Raised when the host refuses or fails to load a schedule."""


class VersionMismatch(HostError):
    """The Lua interpreter is not the version ATE targets (Lua 5.5)."""


class _AbortFlag:
    """One abort flag shared by a run's Lua runtime and its :class:`RunEngine`.

    The Lua count hook (:meth:`_LuaHost._install_abort_hook`) calls ``aborted()`` every 1000
    instructions to interrupt a busy loop; the engine sets it from the operator/main thread via
    :meth:`request_abort`. Both touch it on different threads, so reads and writes go through one
    lock. The flag is also backed by a :class:`threading.Event` (:meth:`wait`), so a worker blocked
    in an ATE call (ATE.sleep, ATE.input — packet 13b) can wait on it and raise "aborted" into
    Lua the moment an abort is requested. ATE's own step runner keeps reading this flag directly
    rather than the wrapped Lua ``pcall``/``xpcall`` (packet 13c)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._aborted = False
        # Waitable handle behind the same lock: request() sets it, clear() clears it, and
        # wait() lets a blocked ATE call sleep on it instead of the flag being read-only.
        self._event = threading.Event()

    @property
    def aborted(self) -> bool:
        with self._lock:
            return self._aborted

    def request(self) -> None:
        with self._lock:
            self._aborted = True
            self._event.set()

    def clear(self) -> None:
        """Clear the abort flag so a busy loop stops tripping it (packet 14c).

        Called by the run engine right before its Post-test Actions, which must always run even
        after an earlier whole-run abort. It resets ``_aborted`` and the waitable event; callers
        that need to keep remembering the run was aborted read :attr:`aborted` themselves and
        preserve it."""
        with self._lock:
            self._aborted = False
            self._event.clear()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until an abort is requested, or until ``timeout`` seconds elapse.

        Returns True when the flag was set (so a blocked ATE.sleep / ATE.input wakes and raises
        "aborted" into Lua), False when the wait elapsed with no abort."""
        return self._event.wait(timeout)


# The one and only Lua version ATE runs Test Schedules on.
TARGET_LUA_VERSION = (5, 5)

# ATE.run field names (criterion c). Each maps to a RunEngine attribute when populating the table
# from run context; until then every field reads back as nil. The table is read-only — writing any
# of these raises, so a schedule can only READ the current run's identifying metadata and never
# mutate it. "name" is the test schedule's declared name (distinct conceptually from
# Test_Schedule_Name, the identifier used to select/run it); both are sourced from the loaded
# ScheduleParams in ticket 02 where they coincide.
_RUN_FIELDS: tuple[str, ...] = (
    "UUT_serial",
    "Operator",
    "Station",
    "Project",
    "Test_Schedule_Name",
    "Media_Release_Media_Number",
    "name",
    "version",
)

# Raised when a schedule tries to write one of ATE.run's fields. Surfaced by the run engine as an
# ERROR Attempt (criterion 02); naming the table keeps it consistent with "<file>:<line>: ATE.<fn>:".
ATE_RUN_READONLY = "ATE.run: read-only"

# Packet 16c: the first value a Device function returns to its Lua wrapper when the operation failed
# (see ``_LuaHost._device_wrapper``). A string so Lua can compare it; schedules never see it.
_DEVICE_FAILURE = "\0ate-device-failure\0"
#: The longest ATE.sleep waits between device-warning drains (packet 17b), in seconds.
_DRAIN_SLICE = 0.05


def _build_readonly_table_source(mapping: Mapping[str, Any]) -> str:
    """Return Lua source for a read-only metadata table seeded with ``mapping`` (field -> value).

    Values are stored in a private local ``vals``, captured by the metatable's closures as upvalues,
    so nothing leaks into the schedule's global scope. The public table is empty; its ``__index``
    serves reads from ``vals`` while its ``__newindex`` raises on any assignment. Because no key is
    stored directly in the public table, every field appears *missing* to Lua — which makes
    ``__newindex`` fire even when writing an already-seeded field (Lua only consults ``__newindex``
    for keys absent from the underlying table; a plain seeded table would let existing-field writes
    slip through untouched). The raiser is a separate local function because this lupa build's parser
    rejects an inline ``error(...)`` as the last expression before ``})``, and needs an explicit
    ``end``. The empty table is returned, so no global leaks into the schedule."""
    pairs = ", ".join(f"{field}={_lua_value(value)}" for field, value in mapping.items()) or "nil"
    return (
        "local function _raise() error(%r) end" % ATE_RUN_READONLY
        + "\n"
        + f"local vals = {{ {pairs} }}"
        + "\n"
        + "return setmetatable({}, {"
        + "__index=function(t,k) return vals[k] end,"
        + "__newindex=function() return _raise() end"
        + "})"
    )


def _lua_value(value: Any) -> str:
    """Render a Python value as a safe single-quoted Lua literal (``None``/``""`` become ``nil``).

    Used only to seed ATE.run's fields via :meth:`_AteTable.publish_run`; field names are the fixed
    :data:`_RUN_FIELDS` constants, so nothing here reaches Lua source and there is no injection."""
    if value is None or value == "":
        return "nil"
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


# Enum tables: Lua label -> string value. Defined as Lua assignment chunks so each
# becomes a real global readable by the schedule chunk that uses it.
_ENUMS: dict[str, Mapping[str, str]] = {
    "Result": {
        "PASS": "PASS",
        "FAIL": "FAIL",
        "ERROR": "ERROR",
        "SKIPPED": "SKIPPED",
        "NOTRUN": "NOTRUN",
        "FORCED_NOTRUN": "FORCED_NOTRUN",
        "NOREPORT": "NOREPORT",
    },
    "Log": {
        "DEBUG": "DEBUG",
        "INFO": "INFO",
        "WARNING": "WARNING",
        "ERROR": "ERROR",
    },
    "DeviceType": {
        "SERIAL": "SERIAL",
        "TCP": "TCP",
        "UDP": "UDP",
        "VISA": "VISA",
    },
    "Parity": {
        "NONE": "NONE",
        "EVEN": "EVEN",
        "ODD": "ODD",
        "MARK": "MARK",
        "SPACE": "SPACE",
    },
    "FlowControl": {
        "NONE": "NONE",
        "RTSCTS": "RTSCTS",
        "XONXOFF": "XONXOFF",
        "DSRDTR": "DSRDTR",
    },
}


# Packet 15b: every schedule instance shares the enum tables, so they are read-only proxies. The
# values live in a private table behind ``__index``; the public table stays empty so every write
# reaches ``__newindex``, ``__pairs`` still iterates the values, and ``__metatable`` hides the
# metatable. ``rawset`` is wrapped so it can't plant a key in a protected table either.
_READONLY_PRELUDE = r"""
local setmetatable, error, next, tostring, raw_rawset = setmetatable, error, next, tostring, rawset
local protected = setmetatable({}, {__mode = "k"})
rawset = function(t, k, v)
  if protected[t] then error("rawset: table is read-only", 2) end
  return raw_rawset(t, k, v)
end
return function(name, vals)
  local t = setmetatable({}, {
    __index = vals,
    __newindex = function(_, k) error(name .. "." .. tostring(k) .. " is read-only", 2) end,
    __pairs = function() return next, vals, nil end,
    __metatable = false,
  })
  protected[t] = true
  return t
end
"""


def _declare_enums(runtime) -> None:
    """Define every enum table as a read-only Lua global on ``runtime`` (packet 15b)."""
    readonly = runtime.execute(_READONLY_PRELUDE, name="@enum-readonly", mode="t")
    for name, labels in _ENUMS.items():
        pairs = ", ".join(f"{key}={value!r}" for key, value in labels.items())
        runtime.execute(
            f"local readonly = ...\n{name} = readonly({name!r}, {{ {pairs} }})",
            readonly,
            name=f"@enum-{name}",
            mode="t",
        )


def _lua_attribute_filter(obj: Any, name: str, is_setting: bool) -> str:
    """lupa's attribute filter: Lua reads Python attributes but never sets one (packet 15b).

    ``ATE``, the builders and the objects they hand out are shared by every schedule instance of
    a run, so ``ATE.log = f`` in one include would rewrite it for all of them. Python code still
    sets attributes freely; only assignments made from Lua come through here."""
    if is_setting:
        owner = "ATE" if isinstance(obj, _AteTable) else getattr(obj, "__name__", type(obj).__name__).lstrip("_")
        raise AttributeError(f"{owner}.{name} is read-only")
    return name


class _AteTable:
    """The ``ATE`` Lua table.

    Only :meth:`report` is implemented for ticket 02; every other ATE function a
    later ticket needs is registered here the same way and becomes callable from Lua
    without touching this class's body. Unknown method names fall through to
    :attr:`_extra`, so adding functions is a one-line registration, not an edit of
    the dispatch path.
    """

    def __init__(self) -> None:
        self._extra: dict[str, Callable[..., Any]] = {}
        # Criterion c's read-only ATE.run table. Built by :meth:`_LuaHost.__init__`; ``None`` until
        # then so a stray attribute probe surfaces the usual AttributeError rather than a table.
        self._run: Any = None
        # Packet 16a: register ATE.open/ATE.close as finalized device wrappers (see _isolate_device).
        # They are context-aware — load-time calls propagate instead of isolating, and step/pre/post
        # errors isolate at source="ate" — so __getattr__ returns them directly rather than re-wrapping
        # with the always-isolating _isolate. Registered here; they read self._host lazily when called.
        self._extra["open"] = self._isolate_device(self._do_open, "open")
        self._extra["close"] = self._isolate_device(self._do_close, "close")
        # Packet 16b: register ATE.read/ATE.write as finalized device wrappers (see _isolate_device).
        # They validate the per-call override fields and delegate to the device; I/O failures are caught
        # by the device and returned as result tables, so only param-validation errors raise here.
        self._extra["read"] = self._isolate_device(self._do_read, "read")
        self._extra["write"] = self._isolate_device(self._do_write, "write")
        # Packet 17a: register ATE.flush as a finalized device wrapper (same as read/write): it
        # empties the Device's receive buffer and returns {ok = true, count = n}; a closed or
        # failed Device goes through onError.
        self._extra["flush"] = self._isolate_device(self._do_flush, "flush")
        # Packet 16c: each open Device's onError from ATE.open, the fallback for its read/write/close.
        self._open_handlers: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()
        # Packet 16c: the Lua-wrapped device functions __getattr__ hands out, built on first use.
        self._lua_device_fns: dict[str, Any] = {}

    def register(self, name: str, fn: Callable[..., Any]) -> "_AteTable":
        """Register a Lua-callable under ``name`` (used by later tickets)."""
        self._extra[name] = fn
        return self

    def _isolate(self, fn: Callable[..., Any], name: str) -> Callable[..., Any]:
        """Wrap an ATE function so a Python exception never reaches Lua (criterion 03).

        If ``fn`` raises while running inside a step action, we log its full traceback at ERROR —
        with the settled "<file>:<line>: ATE.<name>:" prefix (see :meth:`_location`) — and return
        halves ``(false, "<file>:<line>: ATE.<name>: <msg>")`` instead of letting the raw Python
        error surface into Lua. The schedule therefore sees a graceful failure it can handle
        (through ``onError`` or by inspecting the returned value), never an uncaught exception —
        and both the log line and the message carry the caller's file:line."""
        import traceback

        def _wrapped(*args: Any) -> tuple[bool, Any]:
            self._host.drain_device_warnings()
            try:
                return fn(*args)
            except Exception as exc:  # noqa: BLE001 - isolated; never reaches Lua
                recorded = "".join(traceback.format_exception(exc))  # the full Python traceback
                loc = self._host._location()
                msg = str(exc).splitlines()[0] if str(exc) else "ATE error"
                # Log at ERROR with the settled format "<file>:<line>: ATE.<fn>:" prefixed to the
                # full traceback, so the app log carries both the schedule's file:line and the
                # Python traceback; return graceful halves carrying just that prefix + message.
                self._host.record_log("ERROR", f"{loc} ATE.{name}: {recorded}")
                return (False, f"{loc} ATE.{name}: {msg}")

        return _wrapped

    def report(
        self, pass_fail: Any, msg: Any = None, actual: Any = None, expected: Any = None
    ) -> bool:
        """Record a step's result. Returns ``true`` so the schedule keeps running."""
        # ATE.report belongs to steps only; it is not legal in a Pre-test/Post-test Action or while
        # the file is loading (only the builders, require, log and the enums are). Refuse there
        # first with an error that carries the caller's file:line and names the function — exactly
        # like ATE.input.
        self._host.drain_device_warnings()
        if self._host._context != "step":
            raise NotAllowedError(
                f"{self._host._location()} ATE.report: only allowed in a step action"
            )
        # The host appends every call in order; the run engine attributes each batch
        # to the step whose action closure just ran (last call wins per step).
        self._host.record_report(pass_fail, msg, actual, expected)
        return True

    def input(self, n: Any) -> Any:
        """Return the current step's n-th Step Input. Raises outside a step."""
        # ATE.input reads the Operator's answer for the running step; it is not legal in a Pre-test
        # or Post-test Action (or while loading). Refuse there first with an error that carries the
        # caller's file:line and names the function.
        self._host.drain_device_warnings()
        if self._host._context != "step":
            raise NotAllowedError(
                f"{self._host._location()} ATE.input: only allowed in a step action"
            )
        return self._host.inputs(n)

    def sleep(self, seconds: Any) -> bool:
        """Sleep for ``seconds`` (int or float, fractions allowed), interruptible by an abort.

        Waits on the shared abort flag, so an abort requested during the wait wakes it and raises
        "aborted" into Lua immediately — the same path the count hook uses — and the running step
        records ERROR "Aborted by Operator". A non-number, non-finite or negative argument raises
        a clear error naming ATE.sleep, so a bad call errors the step (story 48). The wait is sliced
        so a device failure during a long sleep is still logged at once (packet 17b)."""
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise ValueError(f"ATE.sleep: seconds must be a number >= 0, got {seconds!r}")
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError(f"ATE.sleep: seconds must be a number >= 0, got {seconds!r}")
        if seconds > threading.TIMEOUT_MAX:  # past this the wait itself overflows
            raise ValueError(f"ATE.sleep: seconds must be at most {threading.TIMEOUT_MAX:.0f}, got {seconds!r}")
        flag = self._host._abort_flag
        # An abort requested before the wait starts is no reason to keep sleeping: raise now.
        if flag.aborted:
            raise RuntimeError("aborted")
        deadline = time.monotonic() + seconds
        while True:
            self._host.drain_device_warnings()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            if flag.wait(min(remaining, _DRAIN_SLICE)):
                raise RuntimeError("aborted")

    def log(self, level: Any, *args: Any) -> bool:
        """Record a schedule's own log line at ``level``. Returns ``true``."""
        # The host appends every call in order with source "schedule"; the run engine
        # attributes each batch to the step whose action closure just ran (see below).
        self._host.drain_device_warnings()
        self._host.record_log(level, *args)
        return True

    def addEquipment(self, name: Any = None, serial: Any = None, connection: Any = None,
                     cal_due: Any = None, notes: Any = None, *extra: Any) -> bool:
        """Declare a piece of Equipment this Test Run used (packet 20). Returns ``true``.

        Every argument is a string: `name` is required, the others may be nil and show blank. Any
        other type is an error that fails the step (or Action) like any other Lua error, and so is a
        call at load time — the top level of the file runs before a Test Run exists, so Equipment,
        like ATE.report and ATE.input, belongs to a Pre-test Action, a step or a Post-test Action.
        `cal_due` is free text, printed as given; ATE never parses it. A repeat of the same `name`
        and `serial` adds no row and logs one DEBUG line instead; the first call's order is kept."""
        self._host.drain_device_warnings()
        if self._host._context not in ("step", "pretest", "posttest"):
            raise NotAllowedError(
                f"{self._host._location()} ATE.addEquipment: only allowed in a step or a Pre-test/Post-test Action"
            )
        if extra:
            raise ValueError(f"ATE.addEquipment: takes at most 5 arguments, got {5 + len(extra)}")
        for label, value in (("name", name), ("serial", serial), ("connection", connection),
                             ("cal_due", cal_due), ("notes", notes)):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"ATE.addEquipment: {label} must be a string or nil, got {value!r}")
        if not name:  # nil or ""
            raise ValueError("ATE.addEquipment: name is required")
        key = (name, serial or None)  # no serial and an empty serial are the same Equipment
        if key in self._host._equipment_seen:
            # The same Equipment declared twice is the same Equipment: keep the first row, log the
            # duplicate at DEBUG so the Operator can see the schedule repeated it.
            self._host.record_log("DEBUG", f"ATE.addEquipment: {name}" + (f" (serial {serial})" if serial else "") + " already declared - ignored", source="ate")
            self._host.drain_device_warnings()
            return True
        self._host._equipment_seen.add(key)
        self._host.equipment.append(
            {"name": name, "serial": serial, "connection": connection, "cal_due": cal_due, "notes": notes,
             "at": time.monotonic()}
        )
        # Write the row now, owned by the running step Attempt or Action, so the record shows the
        # declaration at the moment the schedule made it.
        self._host.drain_device_warnings()
        return True

    def _device_type_name(self, value: Any) -> str:
        """Map a DeviceType enum member to its type name.

        A schedule passes the specific member ``DeviceType.TCP``; with the flat enum tables in
        :data:`_ENUMS` that resolves to the string label ``"TCP"``. The same call may also be given
        a one-field enum-member table (e.g. ``{TCP="TCP"}``) whose single key equals its value — that
        key is the type name. Both shapes resolve to the same name; passing the whole enum table or
        anything else is rejected."""
        if isinstance(value, str):
            return value
        table_cls = self._host._table_cls
        if not isinstance(value, table_cls):
            raise ValueError(
                f"deviceType must be a DeviceType enum member, got {type(value).__name__}"
            )
        names = [key for key, val in value.items() if key == val]
        if len(names) != 1:
            raise ValueError(f"unknown DeviceType")
        return names[0]

    def _isolate_device(self, fn: Callable[..., Any], name: str) -> Callable[..., Any]:
        """Wrap an ATE.open/read/write/close call (packets 16a–16c).

        Unlike :meth:`_isolate` this wrapper is *context-aware*: while loading it re-raises so a
        load-time call propagates as a HostError (Devices are only legal after the schedule loads),
        but in a step, Pre-test or Post-test Action a param error isolates like any other ATE function —
        logging at source="ate" and returning graceful halves, never touching ``onError``. A Device
        failure is not an exception: ``fn`` returns it as :meth:`_failure`'s tuple, which passes
        through here untouched to the Lua wrapper that runs ``onError`` or raises (see
        :attr:`_LuaHost._device_wrapper`). The finalized wrapper is marked so :meth:`__getattr__` wraps
        it in that Lua function instead of the always-isolating ``_isolate``."""
        import traceback

        def _wrapped(*args: Any) -> tuple[bool, Any]:
            if self._host._context == "load":
                # Only legal after loading; refuse before connecting so a load-time call surfaces as a HostError (not an isolated ERROR).
                raise NotAllowedError(
                    f"{self._host._location()} ATE.{name}: only legal after the schedule has loaded"
                )
            # Packet 17b: drain before (a failure since the last call) and after (one this call met,
            # e.g. during a read that waited out its timeout), so each WARNING lands at once.
            self._host.drain_device_warnings()
            try:
                return fn(*args)
            except Exception as exc:  # noqa: BLE001 - isolated; never reaches Lua
                recorded = "".join(traceback.format_exception(exc))  # the full Python traceback
                loc = self._host._location()
                msg = str(exc).splitlines()[0] if str(exc) else "ATE error"
                # Log at ERROR with source="ate" so a device failure attaches to the run as a whole rather than
                # to any step; return graceful halves carrying just that prefix + message.
                self._host.record_log("ERROR", f"{loc} ATE.{name}: {recorded}", source="ate")
                return (False, f"{loc} ATE.{name}: {msg}")
            finally:
                self._host.drain_device_warnings()

        _wrapped._isolate_device = True  # marker: __getattr__ wraps this for Lua instead of re-wrapping
        return _wrapped

    @staticmethod
    def _split_args(params: Any, error_handler: Any) -> tuple[Any, Any]:
        """Let an optional ``params`` be skipped: ``ATE.read(dev, onError)`` passes the handler second."""
        from lupa import lua_type

        if error_handler is None and lua_type(params) == "function":
            return None, params
        return params, error_handler

    def _failure(self, name: str, what: str, result: dict[str, Any], params: Any, handler: Any) -> tuple:
        """The tuple a failed Device call hands its Lua wrapper (packet 16c).

        ``result`` is the failure table the call would return (with ``err``), ``params`` the table the
        schedule passed in, ``handler`` the ``onError`` to try, and the message the error raised when
        there is no handler or it returns nil: ``<file>:<line>: ATE.<fn>: <device>: <err>``."""
        message = f"{self._host._location()} ATE.{name}: {what}: {result['err']}"
        return _DEVICE_FAILURE, self._host._runtime.table_from(result), params, handler, message

    def _handler_for(self, device: Any, error_handler: Any) -> Any:
        """The call's own ``onError`` if given, else the one given to ``open`` (or nil).

        The fallback is looked up only for a real handle: that table is keyed by weak reference, and
        a string or nil passed where a Device belongs (packet 29d review) has no handler to inherit."""
        if error_handler is not None:
            return error_handler
        return self._open_handlers.get(device) if self._is_device_handle(device) else None

    def _device_driver(self, device: Any) -> type:
        """The driver class that opened ``device``, so ``read``/``write`` validate their per-call
        fields against that Device Type rather than always TCP (packet 18a).

        Only ever asked after :meth:`_is_device_handle` has accepted the argument, so this is a
        handle's own driver; the TCP fallback covers a handle that was opened before tracing wrapped
        it."""
        driver = getattr(device, "driver", None)
        return driver if isinstance(driver, type) else TcpDriver

    def _is_device_handle(self, handle: Any) -> bool:
        """Whether ``handle`` is a Device handle, or a value the schedule made up (packet 29d review).

        Every driver's device object — and the tracing wrapper around it — describes itself, and the
        sandbox gives a schedule no way to build one, so ``describe`` is what a handle is. A string
        left behind by a failed ``open`` whose ``onError`` returned one, a number, or nil is not a
        Device: using it is a Device failure through ``onError``, not a Python ``AttributeError``."""
        return callable(getattr(handle, "describe", None))

    def _handle_name(self, handle: Any) -> str:
        """How a value passed where a Device handle belongs is named in the failure message."""
        if handle is None:
            return "nil"
        if isinstance(handle, str):
            return "a string"
        if isinstance(handle, (int, float)):
            return "a number"
        if isinstance(handle, self._host._table_cls):
            return "a table"
        return type(handle).__name__

    def _not_a_device(self, name: str, handle: Any, params: Any, error_handler: Any, result: dict[str, Any]) -> tuple:
        """The failure for a Device call whose first argument is not a Device (packet 29d review).

        ``result`` is the operation's own failure shape with ``err = "not a Device"``, so ``onError``
        sees the fields it would for a closed Device; with no handler (or one returning nil) the call
        raises ``<file>:<line>: ATE.<fn>: <value>: not a Device``."""
        result["err"] = "not a Device"
        return self._failure(name, self._handle_name(handle), result, params, self._handler_for(handle, error_handler))

    def _do_open(self, device_type: Any, params: Any = None, error_handler: Any = None) -> Any:
        """Open a Device of ``device_type`` with ``params``; returns an opaque handle (userdata).

        The real logic behind :meth:`open`. The handle is only ever passed back to ``read``/``write``/``close``,
        and ``error_handler`` is kept as their fallback ``onError``. Param validation raises before
        connecting and never goes through ``error_handler``; a failed connect does."""
        registry = self._host._devices
        name = self._device_type_name(device_type)
        raw = self._raw_params(params, ("readTerm", "writeTerm"))
        try:
            handle = registry.open_device(name, raw)
        except OSError as exc:  # refused, unreachable, timed out: a Device failure, not a schedule bug
            # TCP names itself host:port; SERIAL names the pyserial port it tried to open.
            if name == "TCP":
                what = f"{name} {raw.get('host')}:{raw.get('port')}"
            else:
                what = f"{name} {raw.get('port') or raw.get('host') or raw.get('resource') or ''}"
            return self._failure("open", what, {"ok": False, "err": str(exc)}, params, error_handler)
        if error_handler is not None:
            self._open_handlers[handle] = error_handler
        return handle

    def _do_close(self, device: Any = None, params: Any = None, error_handler: Any = None) -> Any:
        """Close a Device and release it; the real logic behind :meth:`close`.

        Returns a genuine Lua table ``{ok = true}`` (built by the runtime) so ``"closed "..r.ok`` reads "closed
        true"; a plain dict/object would round-trip as userdata instead. Closing twice is a no-op: removing from
        tracking returns False on the second call, but we still report success. A close that fails goes
        through ``onError``."""
        params, error_handler = self._split_args(params, error_handler)
        if not self._is_device_handle(device):
            return self._not_a_device("close", device, params, error_handler, {"ok": False})
        registry = self._host._devices
        try:
            registry.close_device(device)
        except OSError as exc:
            result = {"ok": False, "err": str(exc)}
            return self._failure("close", device.describe(), result, params, self._handler_for(device, error_handler))
        return self._host._runtime.table_from({"ok": True})

    def _raw_params(self, params: Any, byte_fields: tuple[str, ...]) -> Any:
        """Copy a Lua params table into a dict, taking each of ``byte_fields`` as raw bytes.

        Fetched normally, a Lua string comes back UTF-8-decoded, so a terminator or payload holding
        0xFF would raise; these fields are fetched through the host's hex helper instead. Anything
        that isn't a Lua table is returned as is for :func:`validate_params` to reject."""
        if params is None:
            return {}
        if not isinstance(params, self._host._table_cls):
            return params
        out = {}
        for key in params.keys():
            hexed = self._host._hex_field(params, key) if key in byte_fields else None
            out[key] = bytes.fromhex(hexed) if hexed is not None else params[key]
        return out

    def _do_read(self, device=None, params=None, error_handler=None):
        """Read from a Device (packet 16b); the real logic behind ATE.read.

        Validates the per-call override fields against the driver's read_fields and delegates to the
        device; I/O timeouts/failures come back from the device as result tables, never raised here —
        only a param-validation error raises, and that is isolated at source="ate" by _isolate_device.
        A timeout with no bytes, a failed connection or a closed Device goes through ``onError``.
        ``data`` is handed to Lua as bytes, which lupa passes as a Lua string unchanged.
        Packet 17a: the buffer cap is remembered by the reader thread; the first read after an
        overflow carries ``overflow = true`` and logs one WARNING (source device) for it."""
        params, error_handler = self._split_args(params, error_handler)
        if not self._is_device_handle(device):
            return self._not_a_device(
                "read", device, params, error_handler, {"data": None, "count": 0, "partial": False}
            )
        raw = self._raw_params(params, ("readTerm",)) or {}  # a skipped params table is the empty one
        driver = self._device_driver(device)  # the Device Type that opened this handle
        validated = validate_params(driver, raw, driver.read_fields)
        if validated.get("size") is not None and validated["size"] < 1:  # a byte limit must be positive
            raise ValueError(f"field 'size' must be a positive int, got {validated['size']}")
        if self._host._devices.is_open(device):
            data, count, partial, err = device.read(validated)
            overflow, dropped = device.take_overflow()  # the reader recorded it when the cap was exceeded
        else:
            data, count, partial, err, overflow, dropped = None, 0, False, "device closed", False, 0
        if partial:  # spec: a partial read logs a WARNING — for a stream the deadline caught it
            # mid-bytes; for a Device Type that reads whole messages (UDP) `size` cut a datagram
            # short, and saying "timed out" there is a lie. The device says which it is.
            if getattr(device, "partial_read_is_truncation", False):
                self._host.record_log(
                    "WARNING",
                    f"{self._host._location()} ATE.read: datagram truncated to {count} byte(s)",
                    source="ate",
                )
            else:
                self._host.record_log(
                    "WARNING",
                    f"{self._host._location()} ATE.read: timed out after {count} byte(s)",
                    source="ate",
                )
        result = {
            "data": data,  # None -> nil on a timeout with no bytes or a failure
            "count": count,
            "partial": partial,
            "err": err,  # None -> nil unless the read failed
        }
        if overflow:
            # The worker notices the overflow here, not when the reader dropped the bytes: one WARNING
            # (source device) per read that finds any overflow since the previous read, with every
            # byte dropped in between summed into it; take_overflow reset both for the next episode.
            self._host.record_log(
                "WARNING", f"{device.describe()}: receive buffer overflow - dropped {dropped} oldest byte(s)",
                source="device",
            )
            result["overflow"] = True
        if err is not None:
            return self._failure("read", device.describe(), result, params, self._handler_for(device, error_handler))
        return self._host._runtime.table_from(result)

    def _do_write(self, device=None, params=None, error_handler=None):
        """Write to a Device (packet 16b); the real logic behind ATE.write.

        Validates the per-call fields against write_fields and delegates to the device; I/O
        timeouts/failures come back from the device as result tables, never raised here, and go through
        ``onError``, as does writing to a closed Device. ``count`` is the number of bytes actually sent,
        terminator included."""
        if not self._is_device_handle(device):
            return self._not_a_device("write", device, params, error_handler, {"ok": False, "count": 0})
        driver = self._device_driver(device)  # the Device Type that opened this handle
        validated = validate_params(driver, self._raw_params(params, ("data", "writeTerm")), driver.write_fields)
        if self._host._devices.is_open(device):
            ok, count, err = device.write(validated)
        else:
            ok, count, err = False, 0, "device closed"
        result = {
            "ok": ok,
            "count": count,
            "err": err,  # None -> nil unless the write failed
        }
        if err is not None:
            return self._failure("write", device.describe(), result, params, self._handler_for(device, error_handler))
        return self._host._runtime.table_from(result)

    def _do_flush(self, device=None, params=None, error_handler=None):
        """Empty a Device's receive buffer; the real logic behind ATE.flush.

        Returns a genuine Lua table ``{ok = true, count = n}`` (count = bytes discarded); a closed
        Device or one the reader marked failed returns the failure through ``onError``, like a
        read on a closed Device."""
        params, error_handler = self._split_args(params, error_handler)
        if not self._is_device_handle(device):
            return self._not_a_device("flush", device, params, error_handler, {"ok": False, "count": 0})
        validate_params(self._device_driver(device), self._raw_params(params, ()) or {}, {})  # flush takes no fields
        if self._host._devices.is_open(device):
            ok, count, err = device.flush()
        else:
            ok, count, err = False, 0, "device closed"
        result = {
            "ok": ok,
            "count": count,
            "err": err,  # None -> nil unless the flush failed
        }
        if err is not None:
            return self._failure("flush", device.describe(), result, params, self._handler_for(device, error_handler))
        return self._host._runtime.table_from(result)

    def __getattr__(self, name: str) -> Callable[..., Any]:  # noqa: F821
        if name == "run":
            # Criterion c: ATE.run is a read-only metadata table (a real Lua table), not a function.
            # Return it directly so field reads work and writes trip its __newindex guard; routing
            # it through _isolate would wrap it in a callable and break both. Names that are not the
            # run table or registered functions fall through to ``AttributeError`` — otherwise dunder
            # probes (e.g. ``__getitem__``, which lupa runs when assigning this table as a global)
            # would raise the wrong exception type and break construction/lookup.
            return self._run
        try:
            fn = self._extra[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
        # Packet 16a: ATE.open/read/write/close are finalized device wrappers (see _isolate_device); they
        # skip _isolate so their context-aware load-time propagation and source="ate" isolation take
        # effect. Packet 16c: each is wrapped once in the host's Lua function that runs onError or raises.
        if getattr(fn, "_isolate_device", False):
            if name not in self._lua_device_fns:
                self._lua_device_fns[name] = self._host._device_wrapper(_DEVICE_FAILURE, fn)
            return self._lua_device_fns[name]
        return self._isolate(fn, name)

    def publish_run(self, values: Mapping[str, Any]) -> Any:
        """Rebuild ATE.run from run context (criterion c) and return the new read-only table.

        The table is read-only, so a schedule can only READ these fields; writing any of them raises.
        Field names are the fixed :data:`_RUN_FIELDS` constants, so nothing here reaches Lua source and
        there is no injection to worry about."""
        seeded = {field: values.get(field) for field in _RUN_FIELDS if field in values}
        self._run = self._host._runtime.execute(
            _build_readonly_table_source(seeded),
            name=f"@{self._host._schedule_path.name}-run",
            mode="t",
        )


class _StepParams:
    """The ``StepParams`` builder. Setters chain and stash fields for the runner.

    The Step Name is optional (packet 15a): ``StepParams()`` and ``StepParams(nil)`` both load and
    leave ``name`` None, so the Run screen and the report show nothing for that step. Setters are
    only checked when the schedule finishes loading (see :meth:`_LuaHost._validate_steps`), so the
    order they are called in never matters."""

    def __init__(self, name: Any = None) -> None:
        self.name = name
        self.step_no = None
        self.enabled = True
        self._enable_set = False  # setEnable was called, whatever it was given (packet 15d)
        self.inputs = None
        self.action = None  # a closure that runs in Lua when called
        self.include_file = None
        self.keyvals = None
        self.prefix = None
        self._included = False  # setInclude was called, whatever it was given

    @property
    def is_header(self) -> bool:
        """True for an include step: a header row, not a Test Step."""
        return self._included

    @property
    def kind(self) -> str:
        """``"header"`` for an include step, ``"step"`` for an ordinary one — the node row's kind."""
        return "header" if self.is_header else "step"

    def setStepNo(self, value: str) -> "_StepParams":
        self.step_no = value
        return self

    def setEnable(self, value: bool) -> "_StepParams":
        # Packet 15d: on an include step this FORCES the default of every step beneath it, so the
        # runner needs to know whether the setter was called at all, not just what it set.
        self.enabled = value
        self._enable_set = True
        return self

    def setInputs(self, table: Any) -> "_StepParams":
        self.inputs = table
        return self

    def setAction(self, fn: Callable[..., Any]) -> "_StepParams":
        self.action = fn
        return self

    def setInclude(self, file: str, keyvals: Any = None) -> "_StepParams":
        self.include_file = file
        self._included = True
        self.keyvals = keyvals  # kept as given: the host checks it is a plain data table when it expands the include
        return self

    def setPrefix(self, value: str) -> "_StepParams":
        self.prefix = value
        return self


class _ScheduleParams:
    """The ``ScheduleParams`` builder. Its name/description identify the schedule."""

    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description
        self.doc_title = None
        self.doc_id = None
        self.doc_rev = None
        self.pre_test_action = None
        self.post_test_action = None
        self.stop_on_fail = True
        # A Lua array of step objects, read back 1-based.
        self.steps: list[_StepParams] = []

    def setDocTitle(self, value: str) -> "_ScheduleParams":
        self.doc_title = value
        return self

    def setDocId(self, value: str) -> "_ScheduleParams":
        self.doc_id = value
        return self

    def setDocRev(self, value: str) -> "_ScheduleParams":
        self.doc_rev = value
        return self

    def setPreTestAction(self, fn: Callable[..., Any]) -> "_ScheduleParams":
        self.pre_test_action = fn
        return self

    def setPostTestAction(self, fn: Callable[..., Any]) -> "_ScheduleParams":
        self.post_test_action = fn
        return self

    def setStopOnFail(self, value: bool) -> "_ScheduleParams":
        self.stop_on_fail = value
        return self

    def setSchedule(self, steps: Any) -> "_ScheduleParams":
        # ``steps`` arrives as a _LuaTable proxy (an array passed as a function
        # argument is not unpacked to a Python list). Reading it back by enumerating
        # its keys and indexing with 1-based values preserves the real builder objects;
        # ``list(steps)`` would instead return each step's index. Steps without an
        # action (include/header rows, expanded later) are skipped by the runner.
        if steps is None:
            self.steps = []
            return self
        keys = list(steps.keys())
        self.steps = [steps[i] for i in range(1, len(keys) + 1)]
        return self


# Packet 15b: the loader for one schedule instance. Its globals live in a fresh table that falls back
# to the shared global table (the builders, ``ATE``, the enums, ``require``, the standard library)
# for reads, so a global the chunk sets — at load time or later in an action, whose closures keep
# this ``_ENV`` — stays in the instance. ``_G`` names the instance's own table. Modules loaded
# through ``require`` still run in the shared table, once per run, so they stay shared.
_INSTANCE_LOADER = r"""
local load, setmetatable, error, G = load, setmetatable, error, _G
return function(code, chunkname)
  local env = setmetatable({}, {__index = G, __metatable = false})  -- hides the shared table
  env._G = env
  local chunk, err = load(code, chunkname, "t", env)
  if not chunk then error(err, 0) end
  return chunk, env
end
"""

# Packet 15b: keyvals nested deeper than this are refused, so a table that contains itself is a
# load error rather than a runaway recursion.
MAX_KEYVALS_DEPTH = 32

@dataclass
class _ScheduleInstance:
    """One schedule in the expanded tree: the top level, or one `setInclude` (packet 15b).

    `header` is the tree index of the include header row that introduced this instance, `parent`
    is the instance that included it (None for the top level), and `keyvals` is the plain Python
    table the include passed in (`{}` for a top-level schedule or an include with no keyvals)."""

    parent_instance: Optional[int]
    header: Optional[int]
    name: str
    file: str
    media_number: Optional[str]
    prefix: str
    keyvals: dict[Any, Any]
    # The release that owns `file` (None without a Test Definition): includes from this instance
    # resolve against it. `env` is the instance's own Lua global table. `schedule` is this instance's
    # loaded ScheduleParams — packet 15e needs it to reach each instance's Pre-/Post-test Action.
    owner: Any = field(default=None, repr=False, compare=False)
    env: Any = field(default=None, repr=False, compare=False)
    schedule: Any = field(default=None, repr=False, compare=False)


def _media_of(member: Any) -> Optional[str]:
    """The media number of a release from the catalog, or None without one."""
    return (member.media_number or None) if member is not None else None


@dataclass
class _TreeNode:
    """One row of the expanded step tree, in the order the run engine records it.

    `parent` is the tree index of the enclosing include header (None at the top level), `instance`
    the schedule instance the row belongs to, `depth` how deeply it is nested. Everything else is
    read straight off the step, so the runner's `step.step_no`/`step.is_header` reads work on a
    tree entry as they do on a bare `_StepParams`.

    Packet 15c: `step_no` is the fully prefixed Step Number (every enclosing include's `setPrefix`,
    outside in, then the step's own number; a header takes its ancestors' prefixes, not its own) and
    is what the run records and every view shows. `chain` names the row by its include chain,
    `file:number` per level, for load errors.

    Packet 15d: `default_enabled` is the row's enabled default after header forcing (see
    :meth:`_LuaHost._expand`): the nearest enclosing header's `setEnable` when one was called,
    otherwise the row's own."""

    step: _StepParams
    parent: Optional[int]
    instance: int
    depth: int
    step_no: Optional[str] = None
    chain: str = ""
    # Packet 15d: this row's enabled default AFTER header forcing — a Test Step takes the nearest
    # enclosing header's setEnable when one was called, else its own; a header row is not a step,
    # so its own default is its own setEnable (enabled when it never called one).
    default_enabled: bool = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self.step, name)


class _LuaHost:
    """A fresh lupa runtime plus the globals and one loaded schedule."""

    def __init__(
        self,
        path: str | Path,
        operator_input: Optional[OperatorInput] = None,
        aborted_flag: Optional[_AbortFlag] = None,
        modules: Mapping[str, Path] | None = None,  # packet 06a: the require'd module map for this run
        includes: Any = None,  # packet 15b: the release's `catalog.IncludeMap`, resolving `setInclude` files
        receive_buffer_bytes: int = DEFAULT_RECEIVE_BUFFER_BYTES,  # packet 17a: the per-device receive cap
        device_trace_enabled: bool = False,  # packet 17c: deviceTrace.enabled — one DEBUG line per operation
        device_trace_max_bytes: int = DEFAULT_TRACE_MAX_BYTES,  # packet 17c: deviceTrace.maxBytes truncation
    ) -> None:
        from lupa import LuaRuntime  # imported here so importing this module needs no lupa

        # The OperatorInput controller a step's ATE.input calls read/write through; the run engine
        # injects its own (so the app can wire supply_input/click_poll), otherwise we make one.
        self._op = operator_input or OperatorInput()
        # Packet 06a: the whitelist of require'd modules for this host (one per Test Run). ``None``
        # means no replacement require is installed, so any require call reads back as a nil value.
        self._modules = dict(modules) if modules is not None else {}  # no map: nothing is requirable
        # Packet 15b: the Sub-schedule resolver for this host (`catalog.IncludeMap`), which knows the
        # release owning every listed `.tslua` file. Without one (a bare `LuaHost(path)` in a test, or
        # a run with no Test Definition) an include is resolved against the directory of the file
        # that includes it, and it gets no media number: that is a test seam only, not a second way
        # to run a release.
        self._includes = includes
        # Packet 06b: every file this host loads at run time — the schedule plus each module it
        # requires — in load order, once each, as ``(path, kind, sha256-of-raw-bytes)``. ``kind``
        # is "tslua" for the schedule and "lua" for a required module; the run engine records these
        # rows (and flags mismatches) when the run ends. Packet 06d: a bundled ``ate.`` module is
        # recorded with kind "ate-module" and the module NAME in place of a path.
        self._files_used: list[tuple[str, str, str]] = []
        # Packet 06c: the exact bytes each of those files loaded from, keyed by the same path string,
        # so a mismatched file can be stored with the run as it was read rather than re-read later.
        # Bundled `ate.` modules are deliberately not held here — they ship with ATE and are never
        # stored.
        self._file_bytes: dict[str, bytes] = {}
        # Cache of already-loaded module return values; keyed by module name. A nil return caches as
        # ``True`` (stock Lua), and every later require of the same name returns this without re-running.
        self._require_cache: dict[str, Any] = {}
        # Shared abort flag: the Lua count hook reads :attr:`aborted` every 1000 instructions, and
        # the engine sets it from request_abort(). RunEngine passes its own so both see one value;
        # a host built without one owns a private flag (used by isolated tests of the prelude).
        self._abort_flag = aborted_flag or _AbortFlag()
        # The current step's validated declarations, index-ordered StepInputs, set by the run
        # engine before each action so ATE.input(n) can look up and type-map that step's inputs.
        self._step_inputs: list[Any] = []
        # The (node_seq, attempt_no) context this host is in while a step runs; POLL reads key on it.
        # Set by the run engine before each action; None/idle outside a run.
        self._node_seq: Optional[int] = None
        self._attempt_no: Optional[int] = None

        # The options ATE asks lupa for — kept on the instance so criterion 01's runtime
        # settings can be asserted directly (lupa exposes no Python-level accessor).
        base = dict(register_eval=False, register_builtins=False, unpack_returned_tuples=True)
        self._runtime_config = base

        # The schedule path this host was built for; :meth:`_location` uses it to name the
        # error's file (lupa has no API to read a Python call site).
        self._schedule_path = Path(path)
        self._runtime = LuaRuntime(**base, attribute_filter=_lua_attribute_filter)  # packet 15b: Lua can't set Python attributes
        if self._runtime.lua_version != TARGET_LUA_VERSION:
            raise VersionMismatch(
                f"ATE requires Lua {TARGET_LUA_VERSION[0]}.{TARGET_LUA_VERSION[1]} "
                f"(found {self._runtime.lua_version})"
            )
        # The table/array proxy type for this runtime's Lua version, captured from an empty table so
        # it is stable across lupa releases (the module name changes with the Lua version). Used to
        # convert a schedule's input declarations out of Lua tables into plain Python.
        self._table_cls = type(self._runtime.table_from({}))
        # Packet 29a review: ATE stores a reported value as text "using tostring" (ate-table-reference
        # "Results"). Python's str() is not Lua's: it writes True/False and 0.3333333333333333 where
        # Lua writes true/false and 0.33333333333333331. Every value that crosses out of Lua into a
        # text column goes through this one Lua function, so the stored text is what the schedule
        # itself would have printed. :meth:`to_text` is the only way in.
        self._to_lua_text = self._runtime.eval("function(v) return tostring(v) end")
        # Packet 16b: lupa decodes every Lua string it hands Python as UTF-8, which fails on (or
        # mangles) raw device bytes such as 0xFF. This returns a table field's string as hex — always
        # ASCII — so ATE.write/ATE.read params can be rebuilt as the exact bytes (see _raw_params).
        self._hex_field = self._runtime.eval(
            "function(t, k) local v = t[k]; if type(v) == 'string' then "
            "return (v:gsub('.', function(c) return string.format('%02x', c:byte()) end)) end end"
        )
        # Packet 16c: wraps each Device function so a failure is finished in Lua. The Python side returns
        # ``_DEVICE_FAILURE, opResult, opArgs, onError, message``; the wrapper calls onError there and
        # returns its non-nil value, or raises ``message`` as a plain Lua error. Calling the handler and
        # raising in Lua keeps the handler's own errors (and an abort) ordinary Lua errors.
        self._device_wrapper = self._runtime.eval(
            """function(fail, fn)
              local function finish(first, ...)
                if first ~= fail then return first, ... end
                local result, args, handler, message = ...
                if handler ~= nil then
                  local value = handler(result, args)
                  if value ~= nil then return value end
                end
                error(message, 0)
              end
              return function(...) return finish(fn(...)) end
            end"""
        )

        # Enums as persistent Lua globals, then the builders and ATE table.
        _declare_enums(self._runtime)
        env = self._runtime.globals()
        # Packet 15b: each schedule instance (the top level and every include) runs in its own
        # global table, built by this loader. It captures the stock ``load`` before the lockdown
        # removes it from the schedule's reach.
        self._instance_loader = self._runtime.execute(_INSTANCE_LOADER, name="@instance-env", mode="t")
        # Reads a table's metatable past any ``__metatable`` field, so keyvals can be checked for one.
        self._raw_getmetatable = self._runtime.eval("debug.getmetatable")
        # Lock down the sandbox before anything else is registered so a schedule — even at load
        # time — cannot escape via file loading or reach Python (criterion 01).
        self._lockdown(env)
        env["ScheduleParams"] = _ScheduleParams
        env["StepParams"] = _StepParams
        ate = _AteTable()
        ate._host = self  # report()/log() record into the host's ordered lists
        env["ATE"] = ate
        self._ate = ate
        # Criterion c: expose the current run's identifying metadata as a read-only Lua table. Built
        # here (all fields nil) so it exists even before any run context is published; :meth:`_AteTable.publish_run`
        # rebuilds it from RunEngine context via rawset without tripping the read-only guard. The
        # schedule reads ATE.run through _AteTable.__getattr__("run").
        ate._run = self._runtime.execute(
            _build_readonly_table_source({field: None for field in _RUN_FIELDS}),
            name=f"@{Path(path).name}-run",
            mode="t",
        )
        # Redirect stray prints to ATE.log(INFO, ...) so schedule output is always logged.
        self._bind_print(ate)

        # Packet 13c: install the abort count hook before the schedule runs so a busy loop (or one
        # that pcall/xpcall's itself) can be interrupted. ``aborted`` is a Lua callable reading the
        # shared flag live, and the hook raises "aborted" every 1000 instructions once it is set;
        # pcall/xpcall are wrapped to re-raise it so an abort can't be caught (packet 13c).
        env["aborted"] = self._make_aborted_callable()
        self._install_abort_hook(env)
        # Packet 15b: with ``debug`` a schedule could reach the instance loader (and the stock
        # ``load`` it holds) through ``debug.getregistry``, strip the read-only locks with
        # ``debug.setmetatable``, read the shared globals past ``__metatable`` or remove the abort
        # hook. ATE keeps what it needs (the hook above, ``_raw_getmetatable``) from Python; only
        # ``debug.traceback`` stays, because error tracebacks are built through it.
        self._runtime.execute("debug = { traceback = debug.traceback }", name="@debug-lockdown", mode="t")

        # Every ATE.report call, in order. The run engine diffs this around each step's
        # action closure to attribute reports (last-call-wins) to that step.
        self.reports: list[tuple[Any, Any, Any, Any]] = []
        # Every schedule-sourced log line (ATE.log and the redirected print), in order,
        # as ``(level, source="schedule", text)``. The run engine diffs this around each
        # step's action closure to attribute lines to that Attempt.
        self.logs: list[tuple[str, str, str]] = []
        # The monotonic time each line in ``logs`` was emitted, index for index, so the run engine
        # can stamp it with its own time and order it against device warnings (packet 17b).
        self.log_times: list[float] = []
        # Packet 20: every ATE.addEquipment call this run made, in call order, as
        # ``{"name", "serial", "connection", "cal_due", "notes", "at"}`` with ``at`` the monotonic
        # time of the call; the run engine writes each row as it is drained, owned by the running
        # step Attempt or Pre-/Post-test Action. ``_equipment_seen`` is the (name, serial) pairs
        # already declared, so a repeat adds no row.
        self.equipment: list[dict[str, Any]] = []
        self._equipment_seen: set[tuple[str, Optional[str]]] = set()
        # The step index currently running (set by the run engine before each action), used to
        # attribute an isolated ATE error to the schedule line where it was called. None outside
        # a run, when only the filename is known.
        self._current_step: Optional[int] = None
        # The execution context this host is in right now — one of "load" (the top level while the
        # file is loading), "pretest", "posttest" or "step". None means idle, which for a freshly
        # built host is the same as load time: nothing has started running yet. :meth:`_run_action`
        # sets it around an action and restores None afterwards; context-checked ATE functions use
        # it to raise where a call isn't allowed (see criterion b).
        self._context = "load"

        # Packet 16a: one DeviceRegistry per host/run. It maps each Device Type to its driver and
        # tracks every device opened this run so leftovers can be closed at run end; the run engine
        # reads it after the Post-test Actions (see RunEngine.run). The TCP driver is registered
        # here; later packets add their drivers to the same registry. The receive buffer cap (17a)
        # is held by the registry, which hands it to each driver's open.
        self._devices = DeviceRegistry(
            receive_buffer_bytes=receive_buffer_bytes,
            trace_enabled=device_trace_enabled,  # packet 17c: each operation logs one DEBUG line
            trace_max_bytes=device_trace_max_bytes,
            emit=self.record_log,  # through the run's own log, so the engine owns it like ATE.log
        )
        # Packet 17b: the run engine's callable that records pending device warnings; None until a run.
        self.device_warning_sink: Optional[Callable[[], None]] = None
        self._devices.register("TCP", TcpDriver)
        self._devices.register("SERIAL", SerialDriver)  # packet 18a: pyserial ports, same API
        self._devices.register("UDP", UdpDriver)  # packet 18b: datagrams, one datagram per message
        self._devices.register("VISA", VisaDriver)  # packet 19: pyvisa resources, no background reader

        # Load and validate the schedule file. Runs in this runtime's global env,
        # where the enums, builders and ATE table are already registered globals.
        self._schedule_source = Path(path).read_text(encoding="utf-8")
        self._schedule, top_env = self._load_schedule(path)
        # Check every step's setter combination against the Test Schedule format (packet 15a) before
        # anything else: a header that carries an Action, or an ordinary step with no number or no
        # Action, is a load error and must fail the schedule the same way a bad input table does.
        self._validate_steps(self._schedule, self._schedule_path)
        # Validate every step's input declarations against the Test Schedule format; a bad one is a
        # load error, so surface it here as HostError carrying the schedule's own filename.
        self._validate_inputs(self._schedule, self._schedule_path)
        # Packet 15b: expand every `setInclude` into the tree the runner walks. The top-level
        # schedule is instance 0; each include is its own instance, numbered in tree order.
        top_file, top_owner = self._own_listing(self._schedule_path)
        self._instances = [_ScheduleInstance(parent_instance=None, header=None, name=self._schedule.name,
                                           file=top_file, media_number=_media_of(top_owner), prefix="", keyvals={},
                                           owner=top_owner, env=top_env, schedule=self._schedule)]
        self._tree: list[_TreeNode] = []
        self._chain_names: dict[Path, str] = {}
        try:
            self._expand(self._schedule, self._schedule_path, 0, None, 0, "", (Path(self._schedule_path).resolve(),), "")
        except RecursionError as exc:  # a legal but absurdly deep chain is a load error for this schedule only
            raise HostError(f"{Path(self._schedule_path).name}: include tree too deep to load") from exc
        self._check_tree_step_numbers()

    def _lockdown(self, env) -> None:
        """Remove the file-loading entry points and the python builtin from ``env``.

        Assigning each to None makes it Lua nil; calling a nil value raises "attempt to call a
        nil value", which xpcall/run_engine turns into an ERROR Attempt (criterion 02). The
        standard library still provides dofile/loadfile/load, so they must be shadowed explicitly.
        python is excluded by register_builtins=False but is set here too so it reads back as nil
        and cannot reach the host's Python."""
        for name in ("dofile", "loadfile", "load"):
            env[name] = None
        env["python"] = None
        # Packet 06a: when this host has a module map, install ATE's replacement ``require`` and close
        # off every other file on disk (empty package paths) so nothing outside the whitelist is reachable.
        self._install_require(env)  # always: stock require/package.path must never be reachable

    def _install_require(self, env: Any) -> None:
        """Install ATE's replacement ``require`` and lock ``package.path``/``package.cpath`` (packet 06a).

        ``require(name)`` resolves only against this host's module map; an unknown name raises a Lua
        error naming the module (via :meth:`_location`, so it reads as ``<file>:<line>: require: unknown
        module '<name>'``), and a known module is read from disk and run once here, cached per host. With
        package paths emptied nothing else on disk is reachable. Packet 06d: the reserved ``ate.`` prefix
        bypasses that map entirely and resolves to a module bundled in ``ate/lua/`."""
        modules = self._modules  # the whitelist this host was built for

        def _require(name: str) -> Any:
            cached = self._require_cache.get(name)
            if cached is not None:  # a nil return was already cached as True below, so this never hits for nil
                return cached
            if isinstance(name, str) and name.startswith("ate."):
                # Packet 06d: `ate.` is ATE's own namespace — never the Test Definition, and never a path.
                if not _ATE_BUNDLED_RE.fullmatch(name):
                    raise NotAllowedError(f"{self._location()} require: unknown module '{name}'")
                data = _bundled_module_bytes(name)
                if data is None:
                    raise NotAllowedError(f"{self._location()} require: unknown module '{name}'")
                # the module NAME is recorded, not the file it came from, and there is no expectation to match
                self._files_used.append((name, "ate-module", _sha256_hex(data)))
                source = data.decode("utf-8")
                try:
                    value = self._runtime.execute(source, name=f"@{name}", mode="t")
                except Exception as exc:  # noqa: BLE001 - surfaced to the caller as HostError
                    raise HostError(f"failed to load {name}: {exc}") from exc
                self._require_cache[name] = True if value is None else value  # a nil return caches as true (stock Lua)
                return self._require_cache[name]
            if name not in modules:
                raise NotAllowedError(f"{self._location()} require: unknown module '{name}'")
            module_path = Path(modules[name])
            data = module_path.read_bytes()  # checksum the raw bytes before decoding (packet 06b)
            self._files_used.append((str(module_path), "lua", _sha256_hex(data)))
            self._file_bytes[str(module_path)] = data  # packet 06c: kept for a mismatched file's content
            source = data.decode("utf-8")
            try:
                value = self._runtime.execute(source, name=f"@{modules[name]}", mode="t")
            except Exception as exc:  # noqa: BLE001 - surfaced to the caller as HostError
                raise HostError(f"failed to load {modules[name]}: {exc}") from exc
            self._require_cache[name] = True if value is None else value  # a nil return caches as true (stock Lua)
            return self._require_cache[name]

        env["require"] = _require
        # Empty package paths so nothing outside the whitelist is reachable; a real Lua table (built by
        # executing an assignment chunk, like :meth:`_declare_enums`) rather than a Python dict.
        self._runtime.execute(
            "package = {}; package.path = ''; package.cpath = ''",
            name=f"@{self._schedule_path.name}-pkg",
            mode="t",
        )

    def _location(self) -> str:
        """Return ``<file>:<line>:``, the schedule line where the current call was made.

        lupa has no API to read a Python call site, so this locates the offending line in the
        loaded source and reports its 1-based number:

        * Inside a Pre-test/Post-test Action (``_context`` set to that phase by :meth:`_run_action`)
          it points at the action's ``:setPreTestAction(`/`:setPostTestAction(` line.
        * While a step runs, or while loading / when idle, it locates the current step's
          ``:setAction(`` line via :attr:`_current_step`.

        Outside a run it falls back to just the filename."""
        name = Path(self._schedule_path).name if self._schedule_path else "schedule.tslua"
        ctx = self._context
        # A Pre-test/Post-test Action is running: point at that action's line.
        if ctx in ("pretest", "posttest"):
            lines = self._schedule_source.splitlines() if self._schedule_source else []
            marker = ":setPreTestAction(" if ctx == "pretest" else ":setPostTestAction("
            for i, line in enumerate(lines):
                if marker in line:
                    return f"{name}:{i + 1}:"
            return f"{name}:"
        # A step is running (criterion 03) or we are loading / idle: the current step's action.
        if self._current_step is None or self._schedule_source is None:
            return f"{name}:"
        lines = self._schedule_source.splitlines()
        target = self._current_step  # 0-based step index; actions are in order
        found = -1
        for i, line in enumerate(lines):
            if ":setAction(" in line:
                found += 1
                if found == target:
                    return f"{name}:{i + 1}:"
        return f"{name}:"

    def _run_action(self, fn: Callable[..., Any], phase: str, pass_fail: Optional[str] = None) -> Any:
        """Run a Lua action closure with ``phase`` active so context-checked ATE calls raise.

        While ``phase`` is set (one of "pretest", "posttest" or "step") the host knows where it is,
        so :meth:`_AteTable.report` and :meth:`_AteTable.input` can refuse a call made where it isn't
        allowed. The context is restored to idle (``None``) afterwards; a fresh host starts at "load"
        because the top level of the file runs before anything else exists.

        A Pre-test or Post-test Action is called with the run's current ``pass_fail`` as its first
        argument so it can branch on where the run stands (a step action keeps taking no arguments)."""
        self._context = phase
        try:
            if pass_fail is not None and phase in ("pretest", "posttest"):
                return fn(pass_fail)  # type: ignore[call-arg]
            return fn()
        finally:
            self._context = None

    def to_text(self, value: Any) -> Optional[str]:
        """Return Lua's own ``tostring(value)``, or ``None`` for `nil`.

        The one path a value takes from Lua into a text column (report Actual/Expected, log text,
        a recorded Step Input value): a boolean is `true`/`false`, a float prints the way Lua prints
        floats, and an integer keeps no decimal point. Anything else is left to Lua to decide, so
        nothing here can disagree with what the schedule saw."""
        if value is None:
            return None
        return str(self._to_lua_text(value))

    def record_report(self, pass_fail: Any, msg: Any, actual: Any, expected: Any) -> None:
        """Record one ATE.report call from a running schedule, its values already Lua-text."""
        self.reports.append(
            (pass_fail, self.to_text(msg), self.to_text(actual), self.to_text(expected))
        )

    def record_log(self, level: Any, *args: Any, source: str = "schedule") -> None:
        """Append one log line as ``(level, source, text)``.

        ``source`` defaults to ``"schedule"`` (ATE.log and the redirected print); device open/close
        errors are isolated by :meth:`_AteTable._isolate_device` and recorded with ``source="ate"``
        so they attach to the run as a whole rather than to any step. Every argument goes through
        ``tostring`` and is joined with spaces; the global ``print`` is wired to this so stray prints
        land at INFO (see :meth:`_bind_print`). The emission time goes to :attr:`log_times`."""
        self.logs.append((str(level), source, " ".join(str(self._to_lua_text(a)) for a in args)))
        self.log_times.append(time.monotonic())

    def take_device_warnings(self) -> list[tuple[float, str]]:
        """Drain and return every device's (monotonic timestamp, message) failure warning(s).

        Covers both still-open and already-closed devices (the registry keeps closed handles so a
        warning enqueued before its close is never lost), and only drivers whose handles support it
        (``getattr(..., "take_warnings", None)``), so a driver without a background reader adds
        nothing. Called on the worker thread through :meth:`drain_device_warnings` and by the run
        engine; the reader thread only enqueues, never drains. A closed handle whose reader has
        stopped can enqueue nothing more, so it is dropped from the registry once drained."""
        out: list[tuple[float, str]] = []
        for device in self._devices.snapshot():
            fn = getattr(device, "take_warnings", None)
            if fn is not None:
                out.extend(fn())
        for device in self._devices.closed_handles():
            quiescent = getattr(device, "quiescent", None)
            done = quiescent() if quiescent is not None else True  # checked before draining: no late warning slips past
            fn = getattr(device, "take_warnings", None)
            if fn is not None:
                out.extend(fn())
            if done:
                self._devices.forget_closed(device)
        return out

    def drain_device_warnings(self) -> None:
        """Hand any pending device failure warnings to the run engine's sink, if one is set.

        Called at every ATE call (and through a long ATE.sleep) so a background failure is logged
        at once; the engine's sink attributes and records them (packet 17b). No sink, no-op."""
        sink = self.device_warning_sink
        if sink is not None:
            sink()

    def _bind_print(self, ate: "_AteTable") -> None:
        """Redirect the global ``print`` to ``ATE.log(Log.INFO, ...)``.

        The standard library still provides ``print`` (only Python builtins are excluded),
        so overriding it here routes stray schedule output through the same log line — at
        INFO — without a separate code path."""
        env = self._runtime.globals()
        ate_log = ate.log

        def _print(*args: Any) -> str:
            ate_log("INFO", *args)
            return ""

        env["print"] = _print  # type: ignore[assignment]

    def _make_aborted_callable(self) -> Callable[[], bool]:
        """Return a callable registered as Lua ``aborted`` that reads the shared flag live.

        The count hook calls it every 1000 instructions; reading through :attr:`_abort_flag` (which
        locks) is safe whether the hook fires on the worker thread or request_abort sets it on another."""
        flag = self._abort_flag

        def aborted() -> bool:
            return flag.aborted

        return aborted

    _AbortPrelude = r"""
local function check_abort() if aborted() then error("aborted", 0) end end
debug.sethook(check_abort, "", 1000)

-- the hook's error is catchable, so a schedule's own pcall/xpcall would swallow an abort (packet
-- 13c): once the flag is set both re-raise "aborted" so it can't be caught
local raw_pcall, raw_xpcall = pcall, xpcall
local function reraise(...)
  if aborted() then error("aborted", 0) end
  return ...
end
pcall = function(...) return reraise(raw_pcall(...)) end
xpcall = function(...) return reraise(raw_xpcall(...)) end
"""

    def _install_abort_hook(self, env: Any) -> None:
        """Install the abort count hook and re-raising pcall/xpcall on ``env`` (packet 13c)."""
        self._runtime.execute(self._AbortPrelude, name=f"@{self._schedule_path.name}-abort", mode="t")

    def _own_listing(self, path: Path) -> tuple[str, Any]:
        """The Test Definition entry for ``path``: ``(rel path as listed, owning release)``.

        Falls back to the bare file name and no owner when no Test Definition stands behind this
        host (a bare `LuaHost(path)` in a test) or the file isn't listed."""
        found = self._includes.owner(path) if self._includes is not None else None
        return found if found is not None else (Path(path).name, None)

    def _resolve_include(self, name: str, from_path: Path, owner: Any) -> tuple[Path, Any]:
        """Find the file an include refers to and the release that owns it (packet 15b).

        Resolution is anchored at ``owner``, the release that owns the including file: its own
        listed files first, and a hit there wins; otherwise its dependencies depth first in
        declaration order, where a path more than one of them lists is ambiguous. One listed
        nowhere, or listed but absent on disk, is a load error too. The caller names the include,
        so these messages don't repeat it. With no Test Definition behind this host the name is
        resolved against the directory of the file that includes it and has no owner — a test
        seam, not a second resolution rule."""
        rel = Path(name).as_posix()
        if self._includes is None:
            path, member = Path(from_path).parent / rel, None
        else:
            hits = self._includes.candidates(owner, rel) if owner is not None else []
            if not hits:
                raise HostError("is not listed in the Test Definition or its dependencies")
            if len(hits) > 1:
                owners = ", ".join(member.media_number or member.name for member, _ in hits)
                raise HostError(f"is ambiguous: listed by more than one dependency ({owners})")
            member, path = hits[0]
        if not path.is_file():
            raise HostError(f"is missing ({path})")
        return path, member

    def _plain_keyvals(self, value: Any) -> dict[Any, Any]:
        """Return ``keyvals`` as plain Python data, or raise if it is not a plain data table.

        Only strings, numbers, booleans and nested tables of those are allowed (packet 15b); a
        function, userdata, builder object or a table with a metatable anywhere inside is a load
        error (the caller names the header that passed it), and so is a value that isn't a table at all. Keys
        keep their Lua type, so an array arrives in the included chunk as an array."""
        if value is None:
            return {}
        if not isinstance(value, (dict, self._table_cls)):
            raise HostError(f"keyvals must be a table, got {value!r}")
        return self._plain_data(value, 0)

    def _plain_data(self, value: Any, depth: int) -> Any:
        if isinstance(value, (str, bool, int, float)):
            return value
        if isinstance(value, (dict, self._table_cls)):
            if depth >= MAX_KEYVALS_DEPTH:
                raise HostError(f"keyvals are nested more than {MAX_KEYVALS_DEPTH} deep")
            if isinstance(value, self._table_cls) and self._raw_getmetatable(value) is not None:
                raise HostError(f"keyvals must be plain data, got a table with a metatable")
            plain = {}
            for key, item in value.items():
                if not isinstance(key, (str, bool, int, float)):
                    raise HostError(f"keyvals must be plain data, got a key {key!r}")
                plain[key] = self._plain_data(item, depth + 1)
            return plain
        raise HostError(f"keyvals must be plain data, got {type(value).__name__} ({value!r})")

    def _expand(self, schedule: _ScheduleParams, schedule_path: Path, instance_seq: int, parent_node: Optional[int],
                depth: int, prefix: str, chain: tuple[Path, ...], where_chain: str,
                forced: Optional[bool] = None) -> None:
        """Walk one schedule's headers and expand every include into :attr:`_tree`/:attr:`_instances`.

        Each header is followed immediately by the subtree of the schedule it includes, so the list
        is exactly the node order the run engine records. The included chunk is compiled and run
        again per include, each in its own global table (so instances never see each other's
        globals, while `require`d modules stay shared), with that include's keyvals as its `...`
        argument. The include is resolved from the release owning the including file. Anything
        that goes wrong loading one — unlisted, missing, ambiguous, cyclic, raising, bad keyvals, or
        a setter error inside it — is a load error for the top-level schedule, and the message names
        the chain that led to it.

        Packet 15c: `prefix` is every enclosing include's `setPrefix`, outside in, prepended as is to
        each Step Number at this level; a header's own prefix applies only beneath it. `chain` is
        the resolved files on this include chain, top level first: a file already on it is a cycle,
        while the same file in a sibling branch is not. Nesting is otherwise unlimited.
        `where_chain` is the `file:number → ` trail naming each row for duplicate errors.

        Packet 15d: `forced` is the `setEnable` value of the nearest enclosing header that called it
        (None when none did), so each Test Step's `default_enabled` is that value, else its own
        `setEnable`, else enabled. A header's own `setEnable` replaces it for its subtree, so the
        innermost header wins; a header row is not a Test Step, so its own default is just its own
        `setEnable` — and a header with no `setEnable` leaves its children's own defaults alone."""
        owner = self._instances[instance_seq].owner
        file_name = self._instances[instance_seq].file
        for seq, step in enumerate(schedule.steps):
            node = len(self._tree)
            label = str(step.step_no) if step.step_no else str(seq + 1)
            number = prefix + step.step_no if step.step_no else None
            # Packet 15d: the effective default. A Test Step takes the nearest enclosing header's
            # setEnable when one was called, otherwise its own; a header row is not a Test Step, so
            # its own default is just its own setEnable (enabled when it never called one).
            default = bool(step.enabled) if step.is_header else (forced if forced is not None else bool(step.enabled))
            self._tree.append(_TreeNode(step=step, parent=parent_node, instance=instance_seq, depth=depth,
                                        step_no=number, chain=f"{where_chain}{file_name}:{label}",
                                        default_enabled=default))
            if not step.is_header:
                continue
            where = f"{Path(schedule_path).name}:{label}:"
            try:
                path, member = self._resolve_include(step.include_file, schedule_path, owner)
                resolved = Path(path).resolve()
                if resolved in chain:
                    names = [self._chain_name(p) for p in chain] + [self._chain_name(resolved)]
                    raise HostError(f"is an include cycle: {' → '.join(names)}")
                keyvals = self._plain_keyvals(step.keyvals)
                child, env = self._load_schedule(path, keyvals=keyvals)
                self._validate_steps(child, path)
                self._validate_inputs(child, path)
                child_seq = len(self._instances)
                self._instances.append(
                    _ScheduleInstance(parent_instance=instance_seq, header=node, name=child.name,
                                      file=Path(step.include_file).as_posix(), media_number=_media_of(member),
                                      prefix=step.prefix or "", keyvals=keyvals, owner=member, env=env,
                                      schedule=child)
                )
                self._chain_names.setdefault(resolved, Path(step.include_file).as_posix())
                self._expand(child, path, child_seq, node, depth + 1, prefix + (step.prefix or ""),
                             chain + (resolved,), f"{where_chain}{file_name}:{label} → ",
                             forced=step.enabled if step._enable_set else forced)
            except HostError as exc:
                # Each level prefixes the header that led to it, so a defect deep in a nested
                # chain reads as the include chain that reached it, naming each include once.
                raise HostError(f"{where} include '{step.include_file}' {exc}") from exc

    def _chain_name(self, resolved: Path) -> str:
        """How a file on an include chain is named in a cycle error: as listed, or as first included."""
        if resolved == Path(self._schedule_path).resolve():
            return self._instances[0].file
        return self._chain_names.get(resolved, resolved.name)

    def _check_tree_step_numbers(self) -> None:
        """Refuse a fully prefixed Step Number used twice anywhere in the expanded tree (packet 15c).

        Siblings are already checked per file by :meth:`_validate_steps`; this catches what the
        includes create (a missing or colliding prefix) and names both rows by their include
        chains. It is a load error for the top-level schedule."""
        seen: dict[str, _TreeNode] = {}
        for entry in self._tree:
            if entry.step_no is None:
                continue
            first = seen.setdefault(entry.step_no, entry)
            if first is not entry:
                raise HostError(
                    f"{Path(self._schedule_path).name}: duplicate Step Number '{entry.step_no}' "
                    f"in the expanded tree: {first.chain} and {entry.chain}"
                )

    def _load_schedule(self, path: str | Path, keyvals: Optional[dict[Any, Any]] = None) -> tuple[_ScheduleParams, Any]:
        """Execute ``path`` as a text chunk in a new instance global table; return its ScheduleParams and that table.

        The schedule's raw bytes are checksummed here (packet 06b) and recorded in :attr:`_files_used`
        before decoding for execution — the first file used by this host, so it always lands first.
        Packet 15b: an included Sub-schedule comes through here too, so its bytes are checksummed and
        kept for `run_file` rows the same way. A file this host already loaded is checksummed and
        recorded once (including the same file twice is two instances of one file) but its chunk is
        compiled and run again for each instance, in its own global table (see
        :data:`_INSTANCE_LOADER`), with that include's keyvals as the chunk's `...` argument — a
        top-level schedule, or an include with no keyvals, gets `{}`."""
        path = Path(path)
        data = path.read_bytes()
        if str(path) not in self._file_bytes:
            self._files_used.append((str(path), "tslua", _sha256_hex(data)))
        self._file_bytes[str(path)] = data  # packet 06c: kept for a mismatched file's content
        code = data.decode("utf-8")
        try:
            chunk, env = self._instance_loader(code, f"@{path.name}")
            # `table_from` builds a real Lua table (nested tables included), so `local kv = ...`
            # indexes it like any Lua table; passing the plain dict straight through would leave a
            # Python object in the chunk, where a missing key raises instead of reading nil.
            schedule = chunk(self._runtime.table_from(keyvals or {}, recursive=True))
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as HostError
            raise HostError(f"failed to load {path}: {exc}") from exc
        if not isinstance(schedule, _ScheduleParams):
            raise HostError(
                f"{path} did not return a ScheduleParams (got {type(schedule).__name__})"
            )
        if schedule.name is None or schedule.name == "":
            raise HostError(f"{path} has no ScheduleParams name")
        return schedule, env

    def _materialize(self, value: Any) -> Any:
        """Convert a Lua table/array proxy (or one nested inside another) into plain Python.

        A table whose keys are integers is an array and becomes a list; one keyed by strings stays a
        dict. Scalars pass through untouched."""
        if isinstance(value, self._table_cls):
            keys = list(value.keys())
            int_keys = [k for k in keys if isinstance(k, int)]
            str_keys = [k for k in keys if not isinstance(k, int)]
            if int_keys:
                return [self._materialize(value[k]) for k in sorted(int_keys)]
            return {str(k): self._materialize(value[k]) for k in str_keys}
        return value

    def _validate_steps(self, schedule: _ScheduleParams, path: Path) -> None:
        """Validate one loaded schedule against the Test Schedule format (packets 15a, 15b).

        An include step (``setInclude``) is a header: no Action, no Step Inputs, and its own Step
        Number is optional. An ordinary step needs a Step Number and an Action, and ``setPrefix``
        only means something on a header. Setters are stashed first and checked here, so the order
        they are called in is irrelevant. Each violation raises HostError naming the step by its
        Step Number, or its 1-based position when it has no number — the same shape as a bad
        ``setInputs`` table, so the schedule picker and the run start both report it as a load
        error. Step Numbers are checked for duplicates among siblings here; packet 15c checks the
        fully prefixed numbers across the expanded tree once the includes are expanded."""
        name = Path(path).name if path else "schedule.tslua"
        seen: set[str] = set()
        for seq, step in enumerate(schedule.steps):
            label = str(step.step_no) if step.step_no else str(seq + 1)
            if step.name is not None and not isinstance(step.name, str):
                raise HostError(f"{name}:{label}: Step Name must be a string, got {step.name!r}")
            if step.step_no is not None and not isinstance(step.step_no, str):
                raise HostError(f"{name}:{seq + 1}: Step Number must be a string, got {step.step_no!r}")
            if step.is_header:
                if not isinstance(step.include_file, str) or not step.include_file:
                    raise HostError(f"{name}:{label}: setInclude needs a file name, got {step.include_file!r}")
                if step.action is not None:
                    raise HostError(f"{name}:{label}: setInclude step cannot also have an Action")
                if step.inputs is not None:
                    raise HostError(f"{name}:{label}: setInclude step cannot also have Step Inputs")
                if step.prefix is not None and not isinstance(step.prefix, str):
                    raise HostError(f"{name}:{label}: setPrefix needs a string, got {step.prefix!r}")
            else:
                if step.prefix is not None:
                    raise HostError(f"{name}:{label}: setPrefix needs setInclude")
                if not step.step_no:  # an empty string is no Step Number either
                    raise HostError(f"{name}:{label}: ordinary step needs a Step Number")
                if step.action is None:
                    raise HostError(f"{name}:{label}: ordinary step needs an Action")
            if step.step_no:
                if step.step_no in seen:
                    raise HostError(f"{name}:{label}: duplicate Step Number '{step.step_no}'")
                seen.add(step.step_no)

    def _validate_inputs(self, schedule: _ScheduleParams, path: Path) -> None:
        """Validate one schedule's declared step inputs against the Test Schedule format.

        Each declaration is converted out of its Lua tables into plain Python and checked by
        :func:`step_inputs.validate_inputs`; a violation raises HostError carrying that schedule's
        own filename so a bad ``setInputs`` table fails the load."""
        for seq, step in enumerate(schedule.steps):
            if step.inputs is None:
                continue
            declarations = {int(k): self._materialize(v) for k, v in step.inputs.items()}
            try:
                validated = validate_inputs(declarations)
            except InvalidStepInput as exc:
                name = Path(path).name if path else "schedule.tslua"
                raise HostError(f"{name}:{seq + 1}: {exc}") from exc
            # Stash the validated declarations on the step so the run engine can hand them to the
            # OperatorInput controller and the host maps typed values for ATE.input(n).
            step._validated_inputs = validated

    def _map_input(self, decl: StepInput, raw: Any) -> Any:
        """Convert a raw supplied value into ``ATE.input``'s documented typed return.

        The Operator supplies each value in native form (the label it picked for an ENUM widget, the
        number/string it typed), and this maps that to what the schedule reads back: the single
        label for RADIOBUT/PUSHBUT, an array of labels for CHECKBOX, a Lua integer/float for
        INTEGER/FLOAT, or the entered text for STRING/PROMPT. Raises if ``decl`` is not one of the
        validated types (defensive — declarations are already checked at load)."""
        t = decl.type
        if t == "INTEGER":
            return int(raw)
        if t == "FLOAT":
            return float(raw)
        if t == "STRING":
            return str(raw)
        if t == "PROMPT":
            # The operator pressed/typed text; returned verbatim.
            return str(raw)
        if t == "ENUM":
            widget = decl.widget
            if widget == "CHECKBOX":
                labels = raw if isinstance(raw, (list, tuple)) else (raw,)
                # Packet 29b review: the spec says CHECKBOX returns "an array of labels". A Python
                # tuple would be spread by lupa (unpack_returned_tuples=True) into several return
                # values, so `local v = ATE.input(1)` would bind only the first label. It must cross
                # into Lua as one real table — an empty selection is an empty table.
                return self._runtime.table_from([str(label) for label in labels])
            # RADIOBUT / PUSHBUT: the single selected option's label.
            return str(raw)
        raise InvalidStepInput(f"input {decl.index}: unknown type {t!r}")

    def inputs(self, n: Any) -> Any:
        """Return the current step's n-th Step Input value (typed), blocking for non-POLL types.

        ``n`` is 1-based into this step's declarations; an undeclared index raises :class:`InvalidStepInput`
        so a bad call becomes an ERROR Attempt. POLL is the only non-blocking type: it returns nil until
        its button is clicked, then that text (sticky). Every other type blocks this worker thread via
        the OperatorInput controller until the value is supplied from the operator thread; an abort
        requested while the wait runs raises "aborted" (packet 13b)."""
        if not self._step_inputs or n < 1 or n > len(self._step_inputs):
            raise InvalidStepInput(f"undeclared input {n}")
        decl = self._step_inputs[n - 1]
        op = self._op
        # POLL reads sticky state keyed by the running step's context; nil until clicked.
        if decl.type == "POLL":
            return op.poll_value(self._node_seq, self._attempt_no, n - 1)
        raw = op.get_raw(n - 1, self._abort_flag, self._node_seq, self._attempt_no)  # blocks this worker thread until supplied (spec behavior); an abort raises "aborted"
        return self._map_input(decl, raw)

    @property
    def runtime_configuration(self) -> dict[str, bool]:
        """The lupa options this host was constructed with.

        register_eval=False / register_builtins=False keep Python out of Lua; unpack_returned_
        tuples=True lets a returned tuple be handed to a step's action as arguments. Kept on the
        instance so criterion 01's runtime settings can be asserted directly (lupa exposes no
        Python-level accessor for them)."""
        return self._runtime_config

    @property
    def runtime(self) -> Any:
        """The underlying lupa runtime (for later tickets that need it)."""
        return self._runtime

    @property
    def ate(self) -> _AteTable:
        """The registered ``ATE`` table."""
        return self._ate

    @property
    def schedule(self) -> _ScheduleParams:
        """The loaded top-level ScheduleParams object."""
        return self._schedule

    @property
    def tree(self) -> list[_TreeNode]:
        """The expanded step tree in node order: each header immediately followed by its subtree.

        Packet 15b — this is what the run engine walks and the Run screen previews. A tree entry
        reads like its step (`entry.step_no`, `entry.is_header`) and adds `parent`, `instance` and
        `depth`. Packet 15d adds `default_enabled`: the row's enabled default after every enclosing
        header's `setEnable` has forced it."""
        return self._tree

    @property
    def instances(self) -> list[_ScheduleInstance]:
        """The top-level schedule and every Sub-schedule include, in tree order (packet 15b).

        Index 0 is the top-level schedule; each `setInclude` is its own instance with the index of
        the header that introduced it, the schedule name/file, the owning media number (None for a
        host with no Test Definition), the `setPrefix` value as given, the keyvals as plain
        Python, and that instance's loaded ScheduleParams (packet 15e: its Pre-/Post-test Actions)."""
        return self._instances


# The public host type. ``_``-prefixed classes above are implementation details;
# later tickets import :class:`LuaHost` from this module and subclass or wrap it as
# needed, so the name is stable even though the internals may change.
LuaHost = _LuaHost
