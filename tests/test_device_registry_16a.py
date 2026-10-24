"""Packet 16a — Device registry, `open`/`close` and TCP connect.

Seam-1 coverage of a real LuaHost + QThread RunEngine driving schedules that open/close a loopback
TCP device through the registry; no mocks. The peer is a controlled listener on 127.0.0.1 whose
accepted connections the test inspects, so "open connects" and "run-end teardown disconnects" are
verified against real sockets."""

import tempfile
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication

from ate.devices import DeviceRegistry, Driver, TcpDriver, validate_params
from ate.engine.lua_host import HostError, LuaHost
from ate.engine.run_engine import RunEngine
from ate.storage.store import RunStore

from ._fixtures import (
    BAD_PARAMS,
    CLOSE_TCP,
    DOUBLE_CLOSE_TCP,
    LOAD_OPEN,
    OPEN_TCP,
    LoopbackPeer,
    _drive,
    wait_for_disconnect,
)


# --- Unit: the TCP driver validates params and connects -------------------------------------


class _FakeDevice:
    """A socket-free opaque handle that still exposes the teardown contract :meth:`close_device`
    relies on (a ``close`` method), so the registry's track/close/double-close semantics are exercised
    without touching a real socket."""

    def __init__(self) -> None:
        self.closed = False

    def describe(self) -> str:  # noqa: D401 - matches _TcpDevice.describe for symmetry
        return "fake device"

    def close(self) -> None:
        self.closed = True


class _FakeDriver(Driver):
    """A socket-free driver for exercising registry tracking semantics."""

    name = "FAKE"
    fields = {"host": ((str,), True)}

    def open(self, params):  # noqa: D401 - returns a distinct opaque handle per call
        return _FakeDevice()


def test_tcp_open_connects_to_loopback_peer() -> None:
    """A real connect — the peer records exactly one accepted connection."""
    peer = LoopbackPeer()
    peer.start()
    try:
        dev = TcpDriver().open(
            {
                "host": "127.0.0.1",
                "port": peer.port,
                "timeout": 5,
                "readTerm": "\n",
                "writeTerm": "\n",
            }
        )
    finally:
        dev.close()
        peer.stop()
    # The accept loop runs on a background thread; wait for the connection to be recorded before
    # asserting (see LoopbackPeer.wait_for_connection).
    assert peer.wait_for_connection(1)
    assert len(peer.connections) == 1


def test_param_errors_name_the_field() -> None:
    """Unknown, missing and mistyped fields each raise a ValueError naming the field."""
    driver = TcpDriver()

    # Unknown field (a typo such as ``buad``). Every valid field is present so only the typo trips.
    with pytest.raises(ValueError, match="unknown field 'buad'"):
        driver.open(
            {
                "host": "127.0.0.1",
                "port": 5025,
                "timeout": 5,
                "readTerm": "",
                "writeTerm": "",
                "buad": 5025,
            }
        )

    # Missing required field — omit host entirely.
    with pytest.raises(ValueError, match="missing required field 'host'"):
        driver.open({"port": 5025})

    # Wrong type on port (a string instead of a number).
    with pytest.raises(ValueError, match="field 'port' must be one of int, float"):
        driver.open({"host": "127.0.0.1", "port": "5025", "timeout": 5})

    # A boolean is not a number: port=true must be rejected even though bool is an int subclass.
    with pytest.raises(ValueError, match="field 'port' must be one of int, float"):
        driver.open({"host": "127.0.0.1", "port": True, "timeout": 5})

    # Wrong type on host (a number instead of a string).
    with pytest.raises(ValueError, match="field 'host' must be one of str"):
        driver.open({"host": 12345, "port": 5025, "timeout": 5})


def test_registry_tracks_open_and_double_close_is_a_noop() -> None:
    """The registry tracks a handle at open, removes it on close, and a second close is a no-op."""
    reg = DeviceRegistry()
    reg.register("FAKE", _FakeDriver)

    handle = reg.open_device("FAKE", {"host": "COM1"})
    assert reg.snapshot() == [handle]  # tracked this run

    first = reg.close_device(handle)  # real close -> True
    second = reg.close_device(handle)  # already closed -> False (no-op success)
    assert (first, second) == (True, False)
    assert reg.snapshot() == []  # nothing left open


# --- Unit: ATE.open / ATE.close return values and tracking through the host -----------------


def _tcp_device_type(host: LuaHost):
    """A DeviceType.TCP enum member as a one-field Lua table (what a schedule passes)."""
    return host._runtime.table_from({"TCP": "TCP"})


def test_open_registers_and_tracks_handle() -> None:
    """ATE.open connects for real and the registry tracks the returned handle this run."""
    peer = LoopbackPeer()
    peer.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            schedule_path = Path(td) / "schedule.tslua"
            schedule_path.write_text("return ScheduleParams('x') :setSchedule({})", encoding="utf-8")
            host = LuaHost(str(schedule_path))
            host._context = None  # not load time, so open is allowed for a direct call
            wrapped = host.ate.__getattr__("open")
            handle = wrapped(_tcp_device_type(host), {
                "host": "127.0.0.1",
                "port": peer.port,
                "timeout": 5,
                "readTerm": "\n",
                "writeTerm": "\n",
            })
    finally:
        peer.stop()

    # wait for the accept thread to record the connection before asserting
    assert peer.wait_for_connection(1)
    assert len(peer.connections) == 1  # connected through real Lua execution
    assert host._devices.snapshot() == [handle]  # tracked for run-end teardown


def test_close_returns_ok_table_and_double_close_noop() -> None:
    """ATE.close returns {ok = true}; a second close is still {ok = true} and removes nothing."""
    peer = LoopbackPeer()
    peer.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            schedule_path = Path(td) / "schedule.tslua"
            schedule_path.write_text("return ScheduleParams('x') :setSchedule({})", encoding="utf-8")
            host = LuaHost(str(schedule_path))
            handle = TcpDriver().open(
                {
                    "host": "127.0.0.1",
                    "port": peer.port,
                    "timeout": 5,
                    "readTerm": "",
                    "writeTerm": "",
                }
            )
    finally:
        peer.stop()

    # A fresh host is in load context; open/close are only legal after loading, so settle it to a
    # non-load context before calling the wrapped method directly (as a step/pre/post action would).
    host._context = None
    wrapped = host.ate.__getattr__("close")
    ok1 = wrapped(handle)
    # close returns a genuine Lua table {ok = true}, not a plain dict/object: assert on it as one.
    assert type(ok1).__name__ == "_LuaTable"
    assert ok1.ok is True
    # Double close is a no-op that still reports success and leaves tracking empty.
    ok2 = wrapped(handle)
    assert type(ok2).__name__ == "_LuaTable"
    assert ok2.ok is True
    assert host._devices.snapshot() == []


# --- Unit: generic param validation --------------------------------------------------------


def test_validate_params_rejects_unknown_missing_and_mistyped() -> None:
    """The shared validator names the field for every failure mode."""
    driver = TcpDriver()

    with pytest.raises(ValueError, match="unknown field 'zzz'"):
        validate_params(
            driver, {"host": "h", "port": 1, "timeout": 1.0, "readTerm": "", "writeTerm": "", "zzz": 1}
        )

    with pytest.raises(ValueError, match="missing required field 'port'"):
        validate_params(driver, {"host": "h"})

    # A present-but-wrong type on timeout (a boolean is not a number) trips the mistype check.
    with pytest.raises(ValueError, match="field 'timeout' must be one of int, float"):
        validate_params(driver, {"host": "h", "port": 1, "timeout": True, "readTerm": "", "writeTerm": ""})


# --- Seam-1: open/close through a driven RunEngine ------------------------------------------


def _run_with_peer(app: QCoreApplication, source: str, port: int):
    """Drive one schedule with a live loopback peer; return (store, run_row)."""
    captured = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        # The 16a schedules target the live peer's ephemeral port via a placeholder; substitute it.
        schedule_path.write_text(
            source.replace(LoopbackPeer.PORT_PLACEHOLDER, str(port)), encoding="utf-8"
        )
        engine = RunEngine(store, str(schedule_path))
        engine.run_uuid_reported.connect(_capture)
        _drive(engine, app)

    assert captured.get("run_uuid") is not None, "engine never reported a run key"
    ru = captured["run_uuid"]
    return store, ru


def test_open_connects_and_leftover_closed_at_run_end(app: QCoreApplication) -> None:
    """A schedule that opens and leaves the device open gets it closed at run end with a WARNING."""
    peer = LoopbackPeer()
    peer.start()
    try:
        store, ru = _run_with_peer(app, OPEN_TCP, peer.port)
    finally:
        peer.stop()

    # The step opened (and reported PASS); the device was left open so teardown closed it.
    node = next(n for n in store.nodes(ru) if n["kind"] == "step")
    assert node["result"] == "PASS"

    # Peer saw the connection while the run was live and the disconnect at run end.
    assert len(peer.connections) >= 1
    assert wait_for_disconnect(peer), "peer never saw the leftover device close at run end"

    # One WARNING per leftover device, source ate, naming it (the peer's ephemeral port).
    warnings = [ln for ln in store.log_lines(ru) if ln["level"] == "WARNING" and ln["source"] == "ate"]
    assert len(warnings) == 1
    assert f"TCP 127.0.0.1:{peer.port}" in warnings[0]["text"]


def test_close_returns_ok_and_releases_during_run(app: QCoreApplication) -> None:
    """A step that opens then closes the device sees {ok = true} and releases it mid-run."""
    peer = LoopbackPeer()
    peer.start()
    try:
        store, ru = _run_with_peer(app, CLOSE_TCP, peer.port)
    finally:
        peer.stop()

    node = next(n for n in store.nodes(ru) if n["kind"] == "step")
    assert node["result"] == "PASS"
    # r.ok is true only if close returned {ok=true}; the report's message carries it.
    assert any(a["msg"] == "closed true" for a in store.attempts(ru))
    # Released during the run, so the peer disconnects before run-end teardown (nothing left open).
    assert wait_for_disconnect(peer)


def test_double_close_is_a_noop_through_lua(app: QCoreApplication) -> None:
    """Closing twice through Lua is a no-op that still reports PASS and closes once."""
    peer = LoopbackPeer()
    peer.start()
    try:
        store, ru = _run_with_peer(app, DOUBLE_CLOSE_TCP, peer.port)
    finally:
        peer.stop()

    node = next(n for n in store.nodes(ru) if n["kind"] == "step")
    assert node["result"] == "PASS"
    # The device was released once (during the run), so there is no leftover warning at run end.
    ate_warnings = [ln for ln in store.log_lines(ru) if ln["level"] == "WARNING" and ln["source"] == "ate"]
    assert ate_warnings == []
    assert wait_for_disconnect(peer)


def test_open_at_load_time_is_rejected() -> None:
    """ATE.open at the top level while loading is not a builder/require/log/enum, so it raises."""
    raised: Exception | None = None
    try:
        _make_host(LOAD_OPEN)  # loading runs the top level; ATE.open there must raise
    except HostError as exc:  # _load_schedule wraps execute failures in HostError
        raised = exc

    assert raised is not None, "a load-time ATE.open must raise"
    text = str(raised)
    assert "ATE.open:" in text
    assert "only legal after the schedule has loaded" in text


def test_bad_params_error_isolated_and_names_field(app: QCoreApplication) -> None:
    """An unknown-field open is isolated (never onError), names the field, and logs at ERROR."""
    peer = LoopbackPeer()
    peer.start()
    try:
        store, ru = _run_with_peer(app, BAD_PARAMS, peer.port)
    finally:
        peer.stop()

    # Isolated before it reached run_engine's except -> no ERROR Attempt on the step.
    node = next(n for n in store.nodes(ru) if n["kind"] == "step")
    assert node["result"] != "ERROR"

    # But the failure was logged at ERROR with file:line and the offending field name.
    error_logs = [ln for ln in store.log_lines(ru) if ln["level"] == "ERROR" and ln["source"] == "ate"]
    assert len(error_logs) >= 1
    joined = "\n".join(ln["text"] for ln in error_logs)
    assert "ATE.open:" in joined
    assert "buad" in joined


# --- Helpers -------------------------------------------------------------------------------


def _make_host(source: str) -> LuaHost:
    """Construct a host for one schedule on disk (no Qt event loop needed)."""
    with tempfile.TemporaryDirectory() as td:
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8")
        return LuaHost(str(schedule_path))
