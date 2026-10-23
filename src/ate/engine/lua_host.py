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
* Steps read back from a loaded schedule: enumerate the step table's keys for the
  count, then index with **1-based** values to get the real builder objects (iterating
  the proxy yields indices, not steps). Each step's action is a closure that runs in
  Lua when called.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ..errors import ATEError


class HostError(ATEError):
    """Raised when the host refuses or fails to load a schedule."""


class VersionMismatch(HostError):
    """The Lua interpreter is not the version ATE targets (Lua 5.5)."""


# The one and only Lua version ATE runs Test Schedules on.
TARGET_LUA_VERSION = (5, 5)

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


def _declare_enums(runtime) -> None:
    """Define every enum table as a Lua global on ``runtime``."""
    for name, labels in _ENUMS.items():
        pairs = ", ".join(f"{key}={value!r}" for key, value in labels.items())
        runtime.execute(
            f"{name} = {{ {pairs} }}",
            name=f"@enum-{name}",
            mode="t",
        )


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
        # The host appends every call in order; the run engine attributes each batch
        # to the step whose action closure just ran (last call wins per step).
        self._host.record_report(pass_fail, msg, actual, expected)
        return True

    def log(self, level: Any, *args: Any) -> bool:
        """Record a schedule's own log line at ``level``. Returns ``true``."""
        # The host appends every call in order with source "schedule"; the run engine
        # attributes each batch to the step whose action closure just ran (see below).
        self._host.record_log(level, *args)
        return True

    def __getattr__(self, name: str) -> Callable[..., Any]:  # noqa: F821
        # Only the device operations later tickets register here are wrapped so a Python
        # exception returns graceful halves instead of reaching Lua (criterion 03). Names that
        # are not registered fall through to ``AttributeError`` — otherwise dunder probes
        # (e.g. ``__getitem__``, which lupa runs when assigning this table as a global) would
        # raise the wrong exception type and break construction/lookup.
        try:
            fn = self._extra[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
        return self._isolate(fn, name)


class _StepParams:
    """The ``StepParams`` builder. Setters chain and stash fields for the runner."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.step_no = None
        self.enabled = True
        self.inputs = None
        self.action = None  # a closure that runs in Lua when called
        self.include_file = None
        self.keyvals = None
        self.prefix = None

    def setStepNo(self, value: str) -> "_StepParams":
        self.step_no = value
        return self

    def setEnable(self, value: bool) -> "_StepParams":
        self.enabled = value
        return self

    def setInputs(self, table: Any) -> "_StepParams":
        self.inputs = table
        return self

    def setAction(self, fn: Callable[..., Any]) -> "_StepParams":
        self.action = fn
        return self

    def setInclude(self, file: str, keyvals: Mapping[str, Any] | None = None) -> "_StepParams":
        self.include_file = file
        self.keyvals = dict(keyvals or {})
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


class _LuaHost:
    """A fresh lupa runtime plus the globals and one loaded schedule."""

    def __init__(self, path: str | Path) -> None:
        from lupa import LuaRuntime  # imported here so importing this module needs no lupa

        # The options ATE asks lupa for — kept on the instance so criterion 01's runtime
        # settings can be asserted directly (lupa exposes no Python-level accessor).
        base = dict(register_eval=False, register_builtins=False, unpack_returned_tuples=True)
        self._runtime_config = base

        # The schedule path this host was built for; :meth:`_location` uses it to name the
        # error's file (lupa has no API to read a Python call site).
        self._schedule_path = Path(path)
        self._runtime = LuaRuntime(**base)
        if self._runtime.lua_version != TARGET_LUA_VERSION:
            raise VersionMismatch(
                f"ATE requires Lua {TARGET_LUA_VERSION[0]}.{TARGET_LUA_VERSION[1]} "
                f"(found {self._runtime.lua_version})"
            )

        # Enums as persistent Lua globals, then the builders and ATE table.
        _declare_enums(self._runtime)
        env = self._runtime.globals()
        # Lock down the sandbox before anything else is registered so a schedule — even at load
        # time — cannot escape via file loading or reach Python (criterion 01).
        self._lockdown(env)
        env["ScheduleParams"] = _ScheduleParams
        env["StepParams"] = _StepParams
        ate = _AteTable()
        ate._host = self  # report()/log() record into the host's ordered lists
        env["ATE"] = ate
        self._ate = ate
        # Redirect stray prints to ATE.log(INFO, ...) so schedule output is always logged.
        self._bind_print(ate)

        # Every ATE.report call, in order. The run engine diffs this around each step's
        # action closure to attribute reports (last-call-wins) to that step.
        self.reports: list[tuple[Any, Any, Any, Any]] = []
        # Every schedule-sourced log line (ATE.log and the redirected print), in order,
        # as ``(level, source="schedule", text)``. The run engine diffs this around each
        # step's action closure to attribute lines to that Attempt.
        self.logs: list[tuple[str, str, str]] = []
        # The step index currently running (set by the run engine before each action), used to
        # attribute an isolated ATE error to the schedule line where it was called. None outside
        # a run, when only the filename is known.
        self._current_step: Optional[int] = None

        # Load and validate the schedule file. Runs in this runtime's global env,
        # where the enums, builders and ATE table are already registered globals.
        self._schedule_source = Path(path).read_text(encoding="utf-8")
        self._schedule = self._load_schedule(path)

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

    def _location(self) -> str:
        """Return ``<file>:<line>:``, the schedule line where the step currently running began.

        lupa has no API to read a Python call site, so this locates the current step's action in
        the loaded source (each action-bearing step contributes one ``:setAction(``), in order)
        and reports its 1-based line. Outside a run it falls back to just the filename."""
        name = Path(self._schedule_path).name if self._schedule_path else "schedule.tslua"
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

    def record_report(self, pass_fail: Any, msg: Any, actual: Any, expected: Any) -> None:
        """Record one ATE.report call from a running schedule."""
        self.reports.append((pass_fail, msg, actual, expected))

    def record_log(self, level: Any, *args: Any) -> None:
        """Append one schedule-sourced log line as ``(level, "schedule", text)``.

        Every argument goes through ``tostring`` and is joined with spaces; the global
        ``print`` is wired to this so stray prints land at INFO (see :meth:`_bind_print`)."""
        self.logs.append((str(level), "schedule", " ".join(str(a) for a in args)))

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

    def _load_schedule(self, path: str | Path) -> _ScheduleParams:
        """Execute ``path`` as a text chunk and return its ScheduleParams."""
        code = Path(path).read_text(encoding="utf-8")
        try:
            schedule = self._runtime.execute(
                code, name=f"@{Path(path).name}", mode="t"
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as HostError
            raise HostError(f"failed to load {path}: {exc}") from exc
        if not isinstance(schedule, _ScheduleParams):
            raise HostError(
                f"{path} did not return a ScheduleParams (got {type(schedule).__name__})"
            )
        if schedule.name is None or schedule.name == "":
            raise HostError(f"{path} has no ScheduleParams name")
        return schedule

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


# The public host type. ``_``-prefixed classes above are implementation details;
# later tickets import :class:`LuaHost` from this module and subclass or wrap it as
# needed, so the name is stable even though the internals may change.
LuaHost = _LuaHost
