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

import tempfile
from pathlib import Path

import pytest
import pyvisa
from PyQt6.QtCore import QCoreApplication
from pyvisa import constants, errors

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

    ``replies`` are consumed one per ``read_raw`; an exhausted script answers with a VISA timeout,
    and a reply that is a :class:`pyvisa.errors.VisaIOError` raises it."""

    def __init__(self, replies: list) -> None:
        self.calls: list[tuple] = []
        self.replies = list(replies)
        self.timeout: int | None = None

    def read_raw(self, size: int | None = None) -> bytes:
        self.calls.append(("read_raw", size, self.timeout))
        reply = self.replies.pop(0) if self.replies else None
        if isinstance(reply, errors.VisaIOError):
            raise reply
        if reply is None:
            raise errors.VisaIOError(constants.StatusCode.error_timeout)
        return reply

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

    def open_resource(self, resource: str, timeout: int | None = None,
                     read_termination=None, write_termination=None) -> _FakeResource:
        self.opened.append((resource, timeout, read_termination, write_termination))
        self.resource.timeout = timeout
        return self.resource


class _FailingManager:
    """A manager whose open always fails, the way an unreachable instrument does."""

    def open_resource(self, resource: str, timeout: int | None = None,
                     read_termination=None, write_termination=None) -> None:
        raise errors.VisaIOError(constants.StatusCode.error_connection_lost)


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


def test_a_resource_that_cannot_be_opened_fails_through_onerror(app: QCoreApplication) -> None:
    """An instrument ATE cannot reach is a Device failure like a refused TCP connect, not a raise."""
    store, ru = _run_fake(app, """return ScheduleParams("x") :setSchedule({
    StepParams("io"):setStepNo("1.")
      :setAction(function()
        local msg = ATE.open(DeviceType.VISA, {resource="TCPIP0::dead-instrument::SOCKET",
          timeout=1, readTerm="\\n", writeTerm="\\n"},
          function(res) return tostring(res.ok) .. " " .. res.err end)
        ATE.report(Result.PASS, msg)
      end),
  })""", _FailingManager())

    message = _step_message(store, ru)
    assert message.startswith("false VI_ERROR_CONN_LOST"), message


# --- the scripted fake: what ATE actually calls -------------------------------------------------


def test_the_fake_registered_in_the_registry_open_write_read_and_close() -> None:
    """The registry hands the VISA driver its params; the handle behaves like every other Device."""
    manager = _FakeManager(_FakeResource([b"OK\n"]))
    dev = _open_fake(manager)

    assert manager.opened == [("TCPIP0::1::INSTR", 5000, None, None)], "pyvisa gets the timeout in ms, terminators off"
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
    assert kinds.count("read_raw") == 1, f"one read for one ATE.read: {kinds!r}"
    assert kinds.index("read_raw") > kinds.index("write_raw"), "nothing reads before the schedule asks"
    assert "clear" not in kinds, "flush must never send an instrument clear"


def test_per_call_timeouts_reach_the_resource_in_milliseconds() -> None:
    """ATE's seconds become pyvisa's milliseconds, per call, on reads and writes alike."""
    resource = _FakeResource([b"OK\n"])
    dev = _open_fake(_FakeManager(resource), timeout=5.0)
    dev.write({"data": "x", "timeout": 0.5})
    dev.read({"timeout": 2.5})

    writes = [c for c in resource.calls if c[0] == "write_raw"]
    reads = [c for c in resource.calls if c[0] == "read_raw"]
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
