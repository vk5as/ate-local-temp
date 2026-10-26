"""Packet 19 — the VISA Device Type.

VISA is the one Device Type with no background reader: an instrument only speaks when it is
queried, so ``ATE.read`` reads on demand, ``ATE.flush`` discards nothing (``count = 0``, never an
instrument clear), and a failed read or write fails only that call — there is no reader to see a
break between calls, so the Device is not marked failed and no device WARNING is logged.

Seam 1 runs a schedule through the real run engine against a real instrument socket: the
LoopbackPeer behind a ``TCPIP0::127.0.0.1::<port>::SOCKET`` resource, served by whatever backend
pyvisa finds (pyvisa-py here, NI-VISA wherever it is installed). The scripted fake stands in for a
resource so the calls ATE makes can be counted exactly: no read without an ``ATE.read``, no clear on
flush, the per-call timeout arriving in milliseconds, and one bad call not poisoning the next.
"""

from __future__ import annotations

import contextlib
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest
import pyvisa
from PyQt6.QtCore import QCoreApplication
from pyvisa import constants, errors
from pyvisa_py.tcpip import TCPIPSocketSession

from ate.devices import visa
from ate.devices.registry import DeviceRegistry
from ate.devices.visa import VisaDriver, get_resource_manager
from ate.engine.run_engine import RunEngine
from ate.storage.store import RunStore

from ._fixtures import LoopbackPeer, _drive
from .test_device_builders import _builder_fields
from .test_tcp_read_write import _run_with_peer, _step_message


# --- the scripted fake: a resource that records every call --------------------------------------


class _FakeResource:
    """A stand-in pyvisa resource: every call is recorded and the reads are scripted.

    ATE reads with the low-level ``visalib.read(session, count)``, so the fake is its own visalib.
    ``replies`` are consumed one per read, each answered with END; an exhausted script answers with
    a VISA timeout, and a reply that is a :class:`pyvisa.errors.VisaIOError` raises it."""

    resource_class = "INSTR"
    session = 1

    def __init__(self, replies: list) -> None:
        self.calls: list[tuple] = []
        self.replies = list(replies)
        self.timeout: int | None = None
        self.visalib = self

    def read(self, session: int, count: int) -> tuple[bytes, constants.StatusCode]:
        self.calls.append(("read", count, self.timeout))
        reply = self.replies.pop(0) if self.replies else None
        if isinstance(reply, errors.VisaIOError):
            raise reply
        if reply is None:
            raise errors.VisaIOError(constants.StatusCode.error_timeout)
        return reply, constants.StatusCode.success

    def ignore_warning(self, *codes):
        return contextlib.nullcontext()

    def write_raw(self, data: bytes) -> int:
        self.calls.append(("write_raw", data, self.timeout))
        return len(data)

    def set_visa_attribute(self, attribute, state) -> int:
        self.calls.append(("set_visa_attribute", int(attribute), state))
        return 0

    def clear(self) -> None:
        self.calls.append(("clear",))

    def close(self) -> None:
        self.calls.append(("close",))


class _FakeManager:
    """A ResourceManager stand-in recording what ``open_resource`` was asked for."""

    def __init__(self, resource: _FakeResource) -> None:
        self.resource = resource
        self.opened: list[tuple] = []

    def open_resource(self, resource: str, open_timeout: int | None = None) -> _FakeResource:
        self.opened.append((resource, open_timeout))
        return self.resource


def _fake_driver_class(manager: _FakeManager) -> type[VisaDriver]:
    """A driver class for the registry whose manager factory is the scripted fake."""

    class _FakeVisaDriver(VisaDriver):
        def __init__(self) -> None:
            super().__init__(manager_factory=lambda: manager)

    return _FakeVisaDriver


def _open_fake(manager: _FakeManager, timeout: float = 5.0) -> object:
    """Open the fake VISA Device through a registry, the way the host does."""
    registry = DeviceRegistry()
    registry.register("VISA", _fake_driver_class(manager))
    return registry.open_device(
        "VISA",
        {"resource": "TCPIP0::1::INSTR", "timeout": timeout, "readTerm": "\n", "writeTerm": "\n"},
    )


def _visa_step(body: str) -> str:
    """A one-step schedule that opens the peer as ``dev``, runs ``body``, and closes it."""
    return f"""return ScheduleParams("x") :setSchedule({{
    StepParams("io"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.VISA, {{resource="TCPIP0::127.0.0.1::__PORT__::SOCKET",
          timeout=5, readTerm="\\n", writeTerm="\\n"}})
{body}
        ATE.close(dev)
      end),
  }})"""


def _run_fake(app: QCoreApplication, source: str, manager: _FakeManager):
    """Drive a schedule through the real run engine with the scripted fake as VISA's manager.

    The engine builds its own LuaHost, so the fake is handed in the same way a real driver would
    find it: as the cached process ResourceManager, restored afterwards.
    """
    captured = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    original = visa._manager
    visa._manager = manager
    try:
        with tempfile.TemporaryDirectory() as td:
            store = RunStore(Path(td) / "ate.db")
            path = Path(td) / "schedule.tslua"
            path.write_text(source, encoding="utf-8")
            engine = RunEngine(store, str(path))
            engine.run_uuid_reported.connect(_capture)
            _drive(engine, app)
    finally:
        visa._manager = original
    return store, captured["run_uuid"]


# --- Seam 1: a real instrument socket over pyvisa -----------------------------------------------


def test_open_every_field_then_write_and_read_through_pyvisa(app: QCoreApplication) -> None:
    """Every open field is honoured, ``write`` appends ``writeTerm``, ``read`` strips ``readTerm``."""
    peer = LoopbackPeer(payload=b"SEAM1\n", after_first_byte=True)
    peer.start()
    try:
        store, ru = _run_with_peer(app, _visa_step("""
        local w = ATE.write(dev, {data = "*IDN?"})
        local r = ATE.read(dev)
        ATE.report(Result.PASS, "w "..w.count.." "..r.count.." "..r.data.." "
          ..tostring(r.partial).." "..tostring(r.err))"""), peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "w 6 5 SEAM1 false nil", "the reply comes back with the terminator stripped"
    assert peer.received[peer.connections[-1]] == b"*IDN?\n", "the peer got the command with its terminator"


def test_a_size_read_takes_that_many_bytes_and_the_rest_survives(app: QCoreApplication) -> None:
    """``size`` caps a read; what arrived past it is not lost, the next read finds it."""
    peer = LoopbackPeer(payload=b"SEAM1\n", after_first_byte=True)
    peer.start()
    try:
        store, ru = _run_with_peer(app, _visa_step("""
        ATE.write(dev, {data = "*IDN?"})
        local a = ATE.read(dev, {size = 4})
        local b = ATE.read(dev)
        ATE.report(Result.PASS, a.count.." "..a.data.." "..b.count.." "..b.data)"""), peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "4 SEAM 1 1", "size cuts the read and the remainder is read next"


def test_a_read_that_times_out_errors_and_leaves_the_device_usable(app: QCoreApplication) -> None:
    """No reply within the timeout is ``err = "timeout"`` through onError; the next call still works."""
    peer = LoopbackPeer(payload=b"LATER\n", after_first_byte=True)
    peer.start()
    try:
        store, ru = _run_with_peer(app, _visa_step("""
        local t = ATE.read(dev, {timeout = 0.4}, function(res) return res end)
        local w = ATE.write(dev, {data = "*IDN?"})
        local r = ATE.read(dev)
        ATE.report(Result.PASS, t.err.." "..t.count.." "..tostring(t.data).." "..w.count.." "..r.count)"""), peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "timeout 0 nil 6 5", "the timeout errors and the device stays usable"


def test_a_read_with_no_terminator_that_times_out_after_some_bytes_is_partial(app: QCoreApplication) -> None:
    """A reply with no terminator, read with ``readTerm = ""``, comes back partial at the deadline."""
    peer = LoopbackPeer(payload=b"PARTIAL", after_first_byte=True)
    peer.start()
    try:
        store, ru = _run_with_peer(app, _visa_step("""
        ATE.write(dev, {data = "*IDN?"})
        local r = ATE.read(dev, {timeout = 0.6, readTerm = ""})
        ATE.report(Result.PASS, r.count .. " " .. r.data .. " " .. tostring(r.partial))"""), peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "7 PARTIAL true", "the bytes that arrived are not lost to the timeout"
    warnings = [ln for ln in store.log_lines(ru) if ln["level"] == "WARNING" and "timed out after 7 byte(s)" in ln["text"]]
    assert warnings, "a partial read logs the timeout WARNING like every other Device Type"


def test_flush_is_a_no_op_returning_zero(app: QCoreApplication) -> None:
    """VISA has no receive buffer: flush returns ``{ok = true, count = 0}`` and clears nothing."""
    peer = LoopbackPeer(payload=b"SEAM1\n", after_first_byte=True)
    peer.start()
    try:
        store, ru = _run_with_peer(app, _visa_step("""
        ATE.write(dev, {data = "*IDN?"})
        local f = ATE.flush(dev)
        local r = ATE.read(dev)
        ATE.report(Result.PASS, tostring(f.ok).." "..f.count.." "..r.count)"""), peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "true 0 5", "flush discards nothing and the reply still arrives"


def test_the_visa_builder_open_write_and_read(app: QCoreApplication) -> None:
    """``devices.Visa(resource)`` with its setters is a valid open params table."""
    peer = LoopbackPeer(payload=b"BUILDER\n", after_first_byte=True)
    peer.start()
    try:
        store, ru = _run_with_peer(app, """return ScheduleParams("x") :setSchedule({
    StepParams("io"):setStepNo("1.")
      :setAction(function()
        local devices = require("ate.devices")
        local dev = ATE.open(DeviceType.VISA,
          devices.Visa("TCPIP0::127.0.0.1::__PORT__::SOCKET"):setTimeout(5):setReadTerm("\\n"):setWriteTerm(""))
        ATE.write(dev, {data = "*IDN?"})
        local r = ATE.read(dev)
        ATE.report(Result.PASS, r.data .. " " .. r.count)
        ATE.close(dev)
      end),
  })""", peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "BUILDER 7"
    assert peer.received[peer.connections[-1]] == b"*IDN?", "setWriteTerm(\"\") writes without a terminator"


class _HalfOpenResource(_FakeResource):
    """A resource that opens, then fails while ATE sets it up - what a real backend does only on
    an odd instrument, so the fake scripts it."""

    @property
    def timeout(self) -> int | None:
        return None

    @timeout.setter
    def timeout(self, value: int | None) -> None:
        if value is not None:
            raise RuntimeError("the backend rejected the timeout")


def test_a_resource_that_fails_during_setup_is_closed_and_fails_through_onerror(app: QCoreApplication) -> None:
    """A failure after the resource opened is still an open failure through onError, and the
    half-opened resource is closed rather than leaked."""
    resource = _HalfOpenResource([])
    store, ru = _run_fake(app, """return ScheduleParams("x") :setSchedule({
    StepParams("io"):setStepNo("1.")
      :setAction(function()
        local msg = ATE.open(DeviceType.VISA, {resource="GPIB0::5::INSTR",
          timeout=1, readTerm="\\n", writeTerm="\\n"},
          function(res) return tostring(res.ok) .. " " .. res.err end)
        ATE.report(Result.PASS, msg)
      end),
  })""", _FakeManager(resource))

    assert _step_message(store, ru) == "false GPIB0::5::INSTR: the backend rejected the timeout"
    assert ("close",) in resource.calls, "the half-opened resource must be closed"


# --- the scripted fake: what ATE actually calls -------------------------------------------------


def test_the_fake_registered_in_the_registry_open_write_read_and_close() -> None:
    """The registry hands the VISA driver its params; the handle behaves like every other Device."""
    manager = _FakeManager(_FakeResource([b"OK\n"]))
    dev = _open_fake(manager)

    assert manager.opened == [("TCPIP0::1::INSTR", 5000)], "pyvisa gets the timeout in ms"
    assert (manager.resource.timeout, manager.resource.read_termination, manager.resource.write_termination) == (
        5000, None, None), "the timeout is set in ms and pyvisa's terminators are off"
    assert dev.describe() == "VISA TCPIP0::1::INSTR"
    assert dev.write({"data": "*IDN?"}) == (True, 6, None)
    assert dev.read({}) == (b"OK", 2, False, None)
    assert dev.flush() == (True, 0, None)
    assert dev.take_overflow() == (False, 0), "VISA has no receive buffer to overflow"
    dev.close()
    dev.close()  # closing twice is safe


def test_no_background_reader_and_flush_never_clears(app: QCoreApplication) -> None:
    """Over a whole run the only reads are the ones the schedule asked for, and flush never clears."""
    resource = _FakeResource([b"REPLY\n"])
    store, ru = _run_fake(app, """return ScheduleParams("x") :setSchedule({
    StepParams("io"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.VISA, {resource="GPIB0::5::INSTR", timeout=5, readTerm="\\n", writeTerm="\\n"})
        ATE.write(dev, {data = "*IDN?"})
        ATE.flush(dev)
        ATE.sleep(0.3)  -- the idle window: with no background reader the instrument is untouched
        local r = ATE.read(dev)
        ATE.report(Result.PASS, r.count)
        ATE.close(dev)
      end),
  })""", _FakeManager(resource))

    assert _step_message(store, ru) == "5", "the terminator was stripped by ATE, not pyvisa"
    kinds = [call[0] for call in resource.calls]
    assert kinds.count("read") == 1, f"one read for one ATE.read: {kinds!r}"
    assert kinds.index("read") > kinds.index("write_raw"), "nothing reads before the schedule asks"
    assert "clear" not in kinds, "flush must never send an instrument clear"


def test_per_call_timeouts_reach_the_resource_in_milliseconds() -> None:
    """ATE's seconds become pyvisa's milliseconds, per call, on reads and writes alike."""
    resource = _FakeResource([b"OK\n"])
    dev = _open_fake(_FakeManager(resource), timeout=5.0)
    dev.write({"data": "x", "timeout": 0.5})
    dev.read({"timeout": 2.5})

    writes = [c for c in resource.calls if c[0] == "write_raw"]
    reads = [c for c in resource.calls if c[0] == "read"]
    assert writes == [("write_raw", b"x\n", 500)], writes
    assert reads[0][2] == 2500, f"a 2.5 s read timeout must reach pyvisa as 2500 ms: {reads!r}"


def test_a_visa_error_fails_only_that_call_and_warns_nothing(app: QCoreApplication) -> None:
    """A non-timeout VISA error is that call's failure: the Device stays open and logs no warning."""
    resource = _FakeResource([b"ONE\n", errors.VisaIOError(constants.StatusCode.error_io), b"TWO\n"])
    store, ru = _run_fake(app, """return ScheduleParams("x") :setSchedule({
    StepParams("io"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.VISA, {resource="GPIB0::5::INSTR", timeout=5, readTerm="\\n", writeTerm="\\n"})
        local a = ATE.read(dev)
        local b = ATE.read(dev, function(res) return res end)
        local c = ATE.read(dev)
        local w = ATE.write(dev, {data = "again"})
        ATE.report(Result.PASS, a.data .. " " .. b.err .. " " .. c.data .. " " .. w.count)
        ATE.close(dev)
      end),
  })""", _FakeManager(resource))

    message = _step_message(store, ru)
    assert message.startswith("ONE VI_ERROR_IO"), message
    warnings = [ln for ln in store.log_lines(ru) if ln["level"] == "WARNING" and ln["source"] == "device"]
    assert not warnings, f"a failed call fails no device: {warnings!r}"


def test_a_read_error_carries_the_visa_error_name_and_the_device_recovers() -> None:
    """The err is the VISA error's name, and the next read works normally."""
    resource = _FakeResource([errors.VisaIOError(constants.StatusCode.error_connection_lost), b"OK\n"])
    dev = _open_fake(_FakeManager(resource))
    ok, count, partial, err = dev.read({})
    assert (ok, count, partial) == (None, 0, False)
    assert err.startswith("VI_ERROR_CONN_LOST"), err
    assert dev.read({}) == (b"OK", 2, False, None)


# --- backend selection --------------------------------------------------------------------------


def test_the_py_backend_is_used_when_no_ivi_library_is_present(monkeypatch) -> None:
    """With no IVI/NI-VISA library to find, ATE falls back to the bundled pyvisa-py (`@py`)."""
    tried: list = []
    pyvisa_py = object()

    def fake_manager(path=None):
        tried.append(path)
        if path is None:
            raise OSError("Could not find a VISA library")
        return pyvisa_py

    monkeypatch.setattr(pyvisa, "ResourceManager", fake_manager)
    monkeypatch.setattr(visa, "_manager", None)

    assert get_resource_manager() is pyvisa_py
    assert tried == [None, "@py"], "the default backend is tried before pyvisa-py"
    assert get_resource_manager() is pyvisa_py, "the manager is built once and cached"


# --- schedule bugs raise, naming the field ---------------------------------------------------------


_FULL = {"resource": '"TCPIP0::1::INSTR"', "timeout": "5", "readTerm": '"\\n"', "writeTerm": '"\\n"'}

_BAD_OPEN_FIELDS = [
    ({**_FULL, "resourse": '"TCPIP0::1::INSTR"'}, "unknown field 'resourse'"),  # a typo, not a Device failure
    ({k: v for k, v in _FULL.items() if k != "resource"}, "missing required field 'resource'"),
    ({k: v for k, v in _FULL.items() if k != "timeout"}, "missing required field 'timeout'"),
    ({k: v for k, v in _FULL.items() if k != "readTerm"}, "missing required field 'readTerm'"),
    ({**_FULL, "timeout": "0"}, "field 'timeout' must be a positive finite number"),
    ({**_FULL, "timeout": "math.huge"}, "field 'timeout' must be a positive finite number"),
    ({**_FULL, "timeout": "0/0"}, "field 'timeout' must be a positive finite number"),
    ({**_FULL, "resource": "5"}, "field 'resource' must be one of str"),
    ({**_FULL, "readTerm": 10}, "field 'readTerm' must be one of str, bytes"),
]


@pytest.mark.parametrize("fields, expected", _BAD_OPEN_FIELDS)
def test_a_bad_open_field_raises_naming_it_and_never_reaches_onerror(app: QCoreApplication,
                                                                    fields: dict, expected: str) -> None:
    """Missing, unknown or mistyped open fields raise naming the field and never open a resource."""
    manager = _FakeManager(_FakeResource([]))
    lua = ", ".join(f"{key}={value}" for key, value in fields.items())
    store, ru = _run_fake(app, f"""return ScheduleParams("x") :setSchedule({{
    StepParams("io"):setStepNo("1.")
      :setAction(function()
        local calls = {{}}
        local mark = function() calls[#calls + 1] = "called"; return "handled" end
        local msg = select(2, ATE.open(DeviceType.VISA, {{{lua}}}, mark))
        ATE.report(Result.PASS, #calls.." "..msg)
      end),
  }})""", manager)

    count, *msg = _step_message(store, ru).split(" ", 1)
    assert count == "0", f"onError must never run for a schedule bug: {count} calls"
    assert expected in msg[0], f"the error must name the field: {msg[0]!r}"
    assert manager.opened == [], "a bad field trips before anything is opened"


# --- the builder ----------------------------------------------------------------


def test_the_visa_builder_carries_the_defaults_and_chains_setters() -> None:
    """``Visa(resource)`` seeds every open field; the setters chain on the same table."""
    assert _builder_fields('local t = devices.Visa("GPIB0::5::INSTR")') == {
        "resource": "GPIB0::5::INSTR", "timeout": 1.0, "readTerm": "\n", "writeTerm": "\n",
    }
    assert _builder_fields(
        'local t = devices.Visa("h"):setTimeout(2.5):setReadTerm(""):setWriteTerm("\\r\\n")'
    ) == {"resource": "h", "timeout": 2.5, "readTerm": "", "writeTerm": "\r\n"}
    assert isinstance(_builder_fields('local t = devices.Visa("h")')["timeout"], float)
    # The metatable is locked, like every other builder's.
    assert _builder_fields("local t = {} t[1] = tostring(getmetatable(devices.Visa('h')))") == {1: "false"}


# --- the real pyvisa-py path: reads, writes and opens against a live socket ------------------------


class _ScriptedPeer:
    """A one-connection TCP peer that runs ``script(conn, done)`` on its own thread, for what
    LoopbackPeer has no knob for: a reply dribbled out a byte at a time, or a peer that never reads.

    ``rcvbuf`` shrinks the accepted socket's receive buffer, so a peer that never reads fills up
    after little data. ``done`` is set by :meth:`stop` and is what a script waits on."""

    def __init__(self, script: Callable[[socket.socket, threading.Event], None], rcvbuf: int | None = None) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if rcvbuf:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self.conn: socket.socket | None = None
        self.done = threading.Event()
        self._script = script
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            self.conn, _addr = self._sock.accept()
            self._script(self.conn, self.done)
        except OSError:
            pass  # stopped, or the device went away

    def stop(self) -> None:
        self.done.set()
        for sock in (self.conn, self._sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._thread.join(2)


def _open_real(port: int, **fields: Any) -> Any:
    """Open a VISA Device on ``127.0.0.1:port`` through the real backend pyvisa finds (pyvisa-py here)."""
    params = {"resource": f"TCPIP0::127.0.0.1::{port}::SOCKET", "timeout": 2.0, "readTerm": "\n", "writeTerm": "\n"}
    return VisaDriver().open({**params, **fields})


def _count_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every read ATE makes of the instrument: each viRead into pyvisa-py's TCPIP SOCKET
    session, and each wait ATE does on the session's socket itself. The list grows by one per read."""
    calls: list[str] = []
    session_read = TCPIPSocketSession.read

    def counted_read(self, count):
        calls.append("viRead")
        return session_read(self, count)

    monkeypatch.setattr(TCPIPSocketSession, "read", counted_read)
    socket_wait = getattr(visa, "_wait_readable", None)
    if socket_wait is not None:
        def counted_wait(*args):
            calls.append("socket")
            return socket_wait(*args)

        monkeypatch.setattr(visa, "_wait_readable", counted_wait)
    return calls


@pytest.fixture(params=["socket", "viRead"])
def read_path(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Each real-backend read test runs both ways ATE can read pyvisa-py: on the TCPIP SOCKET
    session's own socket (the default) and through ``visalib.read`` (what any other backend gets)."""
    if request.param == "viRead":
        monkeypatch.setattr(visa, "_socket_session", lambda resource: None, raising=False)
    return request.param


def test_an_exact_size_read_with_no_terminator_returns_at_once(read_path: str) -> None:
    """``size`` bytes in hand ends the read: no terminator needed, nothing read past it, no timeout."""
    peer = LoopbackPeer(payload=b"ABCD", after_first_byte=True)
    peer.start()
    try:
        dev = _open_real(peer.port, timeout=2.0)
        dev.write({"data": "Q"})
        started = time.monotonic()
        result = dev.read({"size": 4})
        elapsed = time.monotonic() - started
        dev.close()
    finally:
        peer.stop()

    assert result == (b"ABCD", 4, False, None), "exactly size bytes is a complete read"
    assert elapsed < 1.0, f"the read must not wait out the 2 s timeout: {elapsed:.2f} s"


def test_a_terminator_mid_segment_ends_the_read_and_the_rest_stays_for_the_next(read_path: str) -> None:
    """``AB\\nCD`` arrives as one segment: the first read stops at the terminator (stripped), and ``CD``
    is not taken off the instrument with it; the next read gets it (partial: no terminator follows)."""
    peer = LoopbackPeer(payload=b"AB\nCD", after_first_byte=True)
    peer.start()
    try:
        dev = _open_real(peer.port, timeout=2.0)
        dev.write({"data": "Q"})
        first = dev.read({})
        second = dev.read({"timeout": 0.5})
        dev.close()
    finally:
        peer.stop()

    assert first == (b"AB", 2, False, None), "the read ends at the terminator, with nothing past it"
    assert second == (b"CD", 2, True, None), "the bytes after the terminator are still there"


def test_bytes_pyvisa_py_already_buffered_come_first_and_in_order() -> None:
    """pyvisa-py keeps its own ``_pending_buffer`` of bytes it pulled off the socket; ATE's socket path
    serves those before the socket, in order, and still stops at a terminator inside them."""
    peer = LoopbackPeer(payload=b"EF\n", after_first_byte=True)
    peer.start()
    try:
        dev = _open_real(peer.port, timeout=2.0)
        session = visa._socket_session(dev.resource)
        assert session is not None, "this test needs pyvisa-py's TCPIP SOCKET session"
        session._pending_buffer.extend(b"AB\nCD")  # as if pyvisa-py had over-read earlier
        dev.write({"data": "Q"})
        first = dev.read({})
        second = dev.read({})
        dev.close()
    finally:
        peer.stop()

    assert first == (b"AB", 2, False, None), "buffered bytes first, up to the terminator"
    assert second == (b"CDEF", 4, False, None), "then the rest of the buffer, then the socket"


def test_a_size_read_on_a_long_reply_reads_only_that_much(read_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4-byte read of a 2001-byte reply takes 4 bytes in a bounded number of reads, and the rest of
    the reply is still the instrument's to give - the next read gets it."""
    peer = LoopbackPeer(payload=b"X" * 2000 + b"\n", after_first_byte=True)
    peer.start()
    try:
        dev = _open_real(peer.port)
        dev.write({"data": "Q"})
        calls = _count_reads(monkeypatch)
        first = dev.read({"size": 4})
        reads_for_first = len(calls)
        rest = dev.read({})
        dev.close()
    finally:
        peer.stop()

    assert first == (b"XXXX", 4, False, None)
    assert 1 <= reads_for_first <= 2, f"a 4-byte read made {reads_for_first} reads: {calls[:5]}..."
    assert rest == (b"X" * 1996, 1996, False, None), "the rest of the reply is not lost"


def _dribble(conn: socket.socket, done: threading.Event) -> None:
    """Wait for the command, then send a byte every 0.1 s - never a terminator - until stopped, or
    for 3 s at most, so a read that ignores its deadline still ends (and fails on its time)."""
    conn.recv(64)
    for _ in range(30):
        if done.wait(0.1):
            return
        conn.send(b"d")


def test_a_dribbling_reply_comes_back_partial_at_the_deadline() -> None:
    """A reply still arriving when the timeout is up is returned as it stands, ``partial = true``,
    on time - not after the instrument stops talking.

    Socket path only: pyvisa-py's own viRead checks its timeout only when the line goes quiet, so a
    byte every 0.1 s holds it for as long as the instrument keeps talking. That is why ATE reads a
    pyvisa-py SOCKET session's socket itself; the viRead fallback relies on the backend honouring
    the timeout, as NI-VISA does."""
    peer = _ScriptedPeer(_dribble)
    try:
        dev = _open_real(peer.port)
        dev.write({"data": "Q"})
        started = time.monotonic()
        data, count, partial, err = dev.read({"timeout": 1.0})
        elapsed = time.monotonic() - started
        dev.close()
    finally:
        peer.stop()

    assert elapsed < 1.4, f"a 1 s read took {elapsed:.2f} s"
    assert (partial, err) == (True, None) and count >= 3 and data == b"d" * count, (data, count, partial, err)


def test_a_timed_out_read_with_nothing_pending_is_one_blocking_read(read_path: str,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec: VISA never polls. A read the instrument never answers is ONE blocking read for the whole
    timeout, not a string of short ones (each of which a SCPI instrument would log as -420)."""
    peer = LoopbackPeer()
    peer.start()
    try:
        dev = _open_real(peer.port)
        calls = _count_reads(monkeypatch)
        started = time.monotonic()
        result = dev.read({"timeout": 0.5})
        elapsed = time.monotonic() - started
        dev.close()
    finally:
        peer.stop()

    assert result == (None, 0, False, "timeout")
    assert len(calls) == 1, f"one read, not a poll: {calls!r}"
    assert 0.45 <= elapsed < 0.9, f"the one read lasts the timeout: {elapsed:.2f} s"


def test_a_write_the_peer_never_reads_times_out_on_time() -> None:
    """Spec: a write that can't send everything within ``timeout`` fails with ``err = "timeout"``,
    on time, with ``count`` the bytes that did go out, and only that call fails."""
    peer = _ScriptedPeer(lambda conn, done: done.wait(), rcvbuf=4096)
    dev = _open_real(peer.port)
    outcome: list = []
    writer = threading.Thread(target=lambda: outcome.append(dev.write({"data": b"W" * (16 << 20), "timeout": 0.5})))
    started = time.monotonic()
    writer.start()
    writer.join(3.0)
    elapsed = time.monotonic() - started
    stuck = writer.is_alive()
    peer.stop()  # unblocks a write that ignored its timeout
    writer.join(5.0)
    restored = [visa._socket_session(dev.resource).interface.gettimeout()]  # pyvisa-py leaves it blocking (None)
    dev.close()

    assert not stuck, "the write blocked past its timeout"
    assert restored == [None], f"the session socket's own timeout is put back afterwards: {restored}"
    ok, count, err = outcome[0]
    assert (ok, err) == (False, "timeout"), outcome
    assert 0 < count < (16 << 20), f"count is what was sent: {count}"
    assert elapsed < 1.0, f"a 0.5 s write took {elapsed:.2f} s"


def _closed_port() -> int:
    """A loopback port nothing listens on: bound once to pick it, then released."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize("resource, expected", [
    ("TCPIP0::no-such-host.invalid::5025::SOCKET", "could not connect"),  # pyvisa-py's bare Exception
    ("TCPIP0::127.0.0.1::5025::BOGUS", "TCPIP0::127.0.0.1::5025::BOGUS"),  # a ValueError from pyvisa
    ("USB0::0x1234::0x5678::SN1::INSTR", "USB0::0x1234::0x5678::SN1::INSTR"),  # no USB backend here
    ("TCPIP0::127.0.0.1::__CLOSED__::SOCKET", "refused"),  # nothing listening: fails at open, not at read
])
def test_a_resource_pyvisa_cannot_open_fails_through_onerror(app: QCoreApplication, resource: str,
                                                            expected: str) -> None:
    """Whatever pyvisa raises opening a resource, it is a Device failure: onError gets it and the
    step decides, with no traceback and no "opened" device."""
    resource = resource.replace("__CLOSED__", str(_closed_port()))
    store, ru = _run_with_peer(app, f"""return ScheduleParams("x") :setSchedule({{
    StepParams("io"):setStepNo("1.")
      :setAction(function()
        local calls = 0
        local msg = ATE.open(DeviceType.VISA, {{resource="{resource}", timeout=1, readTerm="\\n", writeTerm="\\n"}},
          function(res) calls = calls + 1; return tostring(res.ok) .. " " .. res.err end)
        ATE.report(Result.PASS, calls .. " " .. tostring(msg))
      end),
  }})""", 0)

    message = _step_message(store, ru)
    assert message.startswith("1 false "), message
    assert expected in message, message
    errors_logged = [ln for ln in store.log_lines(ru) if ln["level"] == "ERROR"]
    assert not errors_logged, f"an open failure is not an isolated error: {errors_logged!r}"


def test_flush_discards_nothing_and_a_read_after_it_gets_no_stale_bytes(app: QCoreApplication) -> None:
    """Flush is a no-op: the rest of a reply is still the instrument's to give, and once it is read
    a further read finds nothing - ATE kept no bytes of its own to hand back."""
    peer = LoopbackPeer(payload=b"ONE\n", after_first_byte=True)
    peer.start()
    try:
        store, ru = _run_with_peer(app, _visa_step("""
        ATE.write(dev, {data = "*IDN?"})
        local a = ATE.read(dev, {size = 2})
        local f = ATE.flush(dev)
        local b = ATE.read(dev)
        local c = ATE.read(dev, {timeout = 0.3}, function(res) return res end)
        ATE.report(Result.PASS, a.data.." "..f.count.." "..b.data.." "..c.err.." "..c.count)"""), peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "ON 0 E timeout 0"


@pytest.mark.parametrize("value", ["math.huge", "0/0", "0", "-1"])
def test_a_bad_per_call_timeout_raises_naming_the_field(app: QCoreApplication, value: str) -> None:
    """A per-call ``timeout`` must be finite and > 0, like the open-time one: anything else is a
    schedule bug that raises naming the field and never reaches onError, on read and write alike."""
    peer = LoopbackPeer()
    peer.start()
    try:
        store, ru = _run_with_peer(app, _visa_step(f"""
        local calls = 0
        local mark = function() calls = calls + 1 end
        local r = select(2, ATE.read(dev, {{timeout = {value}}}, mark))
        local w = select(2, ATE.write(dev, {{data = "x", timeout = {value}}}, mark))
        ATE.report(Result.PASS, calls.." | "..tostring(r).." | "..tostring(w))"""), peer.port)
    finally:
        peer.stop()

    calls, read_msg, write_msg = _step_message(store, ru).split(" | ")
    assert calls == "0", "onError must never run for a schedule bug"
    assert "field 'timeout'" in read_msg, read_msg
    assert "field 'timeout'" in write_msg, write_msg
