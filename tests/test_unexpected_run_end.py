"""A run the worker cannot finish: the Operator can see why, and the run row closes at once.

An exception leaving the run engine used to reach stderr alone, with the row left open until the
next launch's recovery closed it.

Packet 31c adds the other half of that bargain: the run may end with its Post-test Actions skipped,
but every Device the run left open is closed before FINISHED is announced, and the next run starts
with a clean Lua environment.
"""

import sys
import time

import pytest
from PyQt6.QtWidgets import QApplication

from ate.devices.serial import SerialDriver
from ate.engine.run_engine import RunEngine
from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.storage.store import RunStore

from ._fixtures import LoopbackPeer, _append_file, wait_for_disconnect, write_ate_env


def _pump(predicate, what: str, timeout: float = 20.0) -> None:
    """Deliver the worker's signals until ``predicate`` holds; never landing is a failure."""
    deadline = time.time() + timeout
    while not predicate():
        assert time.time() < deadline, what
        QApplication.processEvents()
        time.sleep(0.005)


def _broken_run(tmp_path) -> tuple[HeadlessRunSession, list[str]]:
    """Start a run on a session with an application log, and return what its report signal says."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    assert session.on_log is not None, "this test is about the application log"
    written: list[str] = []
    session.report_written.connect(lambda html, _pdf: written.append(html))
    session.select_schedule("Passing schedule")
    session.set_uut_type("U")
    session.set_uut_serial("S")
    session.set_operator("O")
    session.set_project("P")
    session.start()
    return session, written


def _finished(session: HeadlessRunSession) -> None:
    _pump(lambda: session.state == SessionState.FINISHED, "the screen was left waiting on a worker that has gone")


def _app_log_text(session: HeadlessRunSession) -> str:
    return session._app_log.path.read_text(encoding="utf-8")


def test_a_worker_that_raises_leaves_a_closed_run_a_logged_traceback_and_a_report(tmp_path, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("the wiring is broken")

    monkeypatch.setattr(RunEngine, "_insert_node", broken)
    session, written = _broken_run(tmp_path)
    _finished(session)

    row = session.store.get_test_run(session.run_uuid)
    assert row["ended_at"] is not None, "the run row stayed open"
    assert row["result"] == "FAIL" and row["end_reason"] == "INTERRUPTED", row

    errors = [line for line in session.store.log_lines(session.run_uuid) if line["level"] == "ERROR"]
    assert len(errors) == 1 and "Traceback" in errors[0]["text"], errors
    assert "the wiring is broken" in errors[0]["text"], errors
    assert errors[0]["node_seq"] is None, "the error belongs to the run, not a step"

    logged = _app_log_text(session)
    assert " ERROR " in logged and "Traceback" in logged and "the wiring is broken" in logged, logged

    _pump(lambda: written, "no Test Report was written")
    with open(written[0], encoding="utf-8") as report:
        assert "INTERRUPTED" in report.read()


def test_a_worker_that_raises_before_the_run_row_exists_is_still_logged(tmp_path, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("no row for you")

    monkeypatch.setattr(RunStore, "insert_test_run", broken)
    session, _written = _broken_run(tmp_path)
    _finished(session)

    assert session.run_uuid is None
    assert "no row for you" in _app_log_text(session)


def test_a_store_that_cannot_log_still_closes_the_run_and_finishes(tmp_path, monkeypatch):
    """The exception may be the store's: one failed step of the ending does not stop the next."""
    def broken(*_args, **_kwargs):
        raise RuntimeError("the wiring is broken")

    monkeypatch.setattr(RunEngine, "_insert_node", broken)
    monkeypatch.setattr(RunStore, "insert_log_lines", broken)
    session, _written = _broken_run(tmp_path)
    _finished(session)

    row = session.store.get_test_run(session.run_uuid)
    assert row["ended_at"] is not None and row["end_reason"] == "INTERRUPTED", row
    assert "the wiring is broken" in _app_log_text(session)


def test_a_run_failing_before_it_announces_its_key_still_gets_its_report(tmp_path, monkeypatch):
    """The row exists but the session was never told its key: the report needs it."""
    def broken(*_args, **_kwargs):
        raise RuntimeError("the wiring is broken")

    monkeypatch.setattr(RunStore, "insert_schedule_instance", broken)
    session, written = _broken_run(tmp_path)
    _finished(session)
    _pump(lambda: written, "the session never learned which run ended")
    assert session.store.get_test_run(session.run_uuid)["end_reason"] == "INTERRUPTED"


# --- Packet 31c: an unexpected end releases every Device and leaves no Lua state ------------
#
# A run that dies on a worker exception skips its Post-test Actions. That is accepted, provided
# the teardown in :meth:`RunEngine._end_unexpectedly` closes every Device the run left open before
# FINISHED is emitted, and the next run gets a fresh Lua runtime: a leaked socket or a leaked
# global is a leak the following run inherits.


def _always_raises(what: str):
    """A stand-in for a store method that always fails, naming the failure."""
    def broken(*_args, **_kwargs):
        raise RuntimeError(what)

    return broken


def _serial_params(port: str) -> str:
    """A Lua table of SERIAL open params for a Device on ``port``.

    A ``socket://`` peer port works on every platform; a pseudo-terminal path is Linux only."""
    return (
        f'{{port="{port}", baud=9600, dataBits=8, stopBits=1, parity=Parity.NONE, '
        'flowControl=FlowControl.NONE, timeout=1, readTerm="", writeTerm=""}'
    )


def _two_devices_and_lua_state(port_a: str, port_b: str | None = None) -> str:
    """A step that opens two SERIAL Devices, leaves them both open, and writes Lua state that no
    later run should be able to read. ``port_b`` defaults to the first port."""
    params_a = _serial_params(port_a)
    params_b = _serial_params(port_b or port_a)
    return f"""return ScheduleParams("Two open Devices"):setSchedule({{
  StepParams("open two"):setStepNo("1."):setAction(function()
    _G.device_one = ATE.open(DeviceType.SERIAL, {params_a})
    _G.device_two = ATE.open(DeviceType.SERIAL, {params_b})
    leaked_global = "written by the crashed run"
    string.leaked_field = "written by the crashed run"
    ATE.report(Result.PASS, "two Devices left open")
  end),
}})"""


def _clean_lua_check(port: str) -> str:
    """A step that reopens the same port and reports what survived the run before it."""
    params = _serial_params(port)
    return f"""return ScheduleParams("Clean Lua check"):setSchedule({{
  StepParams("reopen"):setStepNo("1."):setAction(function()
    local dev = ATE.open(DeviceType.SERIAL, {params})
    ATE.close(dev)
    ATE.report(Result.PASS, table.concat({{
      tostring(leaked_global), tostring(string.leaked_field), tostring(_G.device_one)
    }}, ","))
  end),
}})"""


def _device_from_the_pretest(port: str) -> str:
    """A Device opened by the Pre-test Action, then a step that runs before the run dies."""
    params = _serial_params(port)
    return f"""return ScheduleParams("Device from the Pre-test Action")
  :setPreTestAction(function() _G.pre_device = ATE.open(DeviceType.SERIAL, {params}) end)
  :setSchedule({{
    StepParams("the later step"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "the step after the Pre-test Action") end),
  }})"""


def _session_on(tmp_path, schedule_name: str, source: str, extra: dict[str, str] | None = None) -> HeadlessRunSession:
    """A session on the demo Test Definition with ``source`` added under ``schedule_name``.

    ``extra`` adds further schedules to the same definition, for tests that run twice."""
    env = write_ate_env(tmp_path)
    _append_file(env["defs_dir"], f"{schedule_name.lower().replace(' ', '_')}.tslua", source)
    for name, added in (extra or {}).items():
        _append_file(env["defs_dir"], f"{name.lower().replace(' ', '_')}.tslua", added)
    session = HeadlessRunSession(env["config"])
    session.select_schedule(schedule_name)
    session.set_uut_type("U")
    session.set_uut_serial("S")
    session.set_operator("O")
    session.set_project("P")
    return session


def _registry_at_finished(session: HeadlessRunSession, engine: RunEngine) -> tuple[list, list]:
    """The worker's Device registry at the instant FINISHED is announced: (still open, closed).

    The sample is taken inside the FINISHED signal itself, so it is exactly the moment the screen
    learns the run is over: Devices the worker never closed would still be in the first list."""
    sample: dict[str, list] = {}

    def note(state: SessionState) -> None:
        if state == SessionState.FINISHED and engine._host is not None:
            registry = engine._host._devices
            sample["open"] = registry.snapshot()
            sample["closed"] = registry.closed_handles()

    session.state_changed.connect(note)
    _finished(session)
    return sample.get("open", []), sample.get("closed", [])


def test_two_devices_left_open_are_closed_before_finished(tmp_path, monkeypatch):
    """The step opens two SERIAL Devices and writes Lua state; the Attempt insert then raises."""
    peer = LoopbackPeer()
    peer.start()
    try:
        monkeypatch.setattr(RunStore, "insert_attempt", _always_raises("the wiring is broken"))
        session = _session_on(tmp_path, "Two open Devices", _two_devices_and_lua_state(
            f"socket://127.0.0.1:{peer.port}"))
        engine = session.start()

        still_open, closed = _registry_at_finished(session, engine)
        assert still_open == [], "Devices were still open when FINISHED was announced"
        assert len(closed) == 2 and all(dev.quiescent() for dev in closed), closed
        assert wait_for_disconnect(peer, 3.0, 2), "the two Device connections were never closed"

        row = session.store.get_test_run(session.run_uuid)
        assert row["result"] == "FAIL" and row["end_reason"] == "INTERRUPTED", row
    finally:
        peer.stop()


def test_a_crash_while_closing_the_run_row_still_closes_the_devices(tmp_path, monkeypatch):
    """`update_run_end` raising cannot undo the teardown that already ran.

    The row stays open until the next launch's recovery closes it; what matters here is that no
    Device and no reader thread is left behind."""
    peer = LoopbackPeer()
    peer.start()
    try:
        monkeypatch.setattr(RunStore, "insert_attempt", _always_raises("the wiring is broken"))
        monkeypatch.setattr(RunStore, "update_run_end", _always_raises("the row will not close"))
        session = _session_on(tmp_path, "Two open Devices", _two_devices_and_lua_state(
            f"socket://127.0.0.1:{peer.port}"))
        engine = session.start()

        still_open, closed = _registry_at_finished(session, engine)
        assert still_open == [] and len(closed) == 2, closed
        assert wait_for_disconnect(peer, 3.0, 2), "the two Device connections were never closed"
        assert session.store.get_test_run(session.run_uuid)["ended_at"] is None, (
            "the row closed even though update_run_end raised"
        )
    finally:
        peer.stop()


def test_a_store_that_cannot_log_still_closes_the_devices(tmp_path, monkeypatch):
    """Even the teardown's own WARNING insert failing leaves both Devices closed."""
    peer = LoopbackPeer()
    peer.start()
    try:
        monkeypatch.setattr(RunStore, "insert_attempt", _always_raises("the wiring is broken"))
        monkeypatch.setattr(RunStore, "insert_log_lines", _always_raises("no lines for you"))
        session = _session_on(tmp_path, "Two open Devices", _two_devices_and_lua_state(
            f"socket://127.0.0.1:{peer.port}"))
        engine = session.start()

        still_open, closed = _registry_at_finished(session, engine)
        assert still_open == [] and len(closed) == 2, closed
        assert wait_for_disconnect(peer, 3.0, 2), "the two Device connections were never closed"
    finally:
        peer.stop()


def test_a_device_opened_in_a_pre_test_action_is_closed_when_the_run_crashes(tmp_path, monkeypatch):
    """A Device the Pre-test Action left open meets the same teardown as one a step opened."""
    peer = LoopbackPeer()
    peer.start()
    try:
        # The Pre-test Action records through insert_action, so insert_attempt first fails on the
        # step that comes after it: the run dies with the Pre-test Device still open.
        monkeypatch.setattr(RunStore, "insert_attempt", _always_raises("the step's Attempt fails"))
        session = _session_on(tmp_path, "Device from the Pre-test Action", _device_from_the_pretest(
            f"socket://127.0.0.1:{peer.port}"))
        engine = session.start()

        still_open, closed = _registry_at_finished(session, engine)
        assert still_open == [], "the Pre-test Device was still open when FINISHED was announced"
        assert len(closed) == 1 and closed[0].quiescent(), closed
        assert wait_for_disconnect(peer, 3.0, 1), "the Pre-test Device's connection was never closed"
        row = session.store.get_test_run(session.run_uuid)
        assert row["result"] == "FAIL" and row["end_reason"] == "INTERRUPTED", row
    finally:
        peer.stop()


def test_one_device_whose_close_raises_does_not_stop_the_next_device_closing(tmp_path, monkeypatch):
    """A close that fails on one Device cannot end the teardown loop for the next one."""
    stubborn = LoopbackPeer()
    following = LoopbackPeer()
    stubborn.start()
    following.start()
    opened: dict[str, object] = {}
    attempts = {"n": 0}
    real_open = SerialDriver.open

    def open_with_a_close_that_fails_once(self, params, *rest):
        device = real_open(self, params, *rest)
        if params["port"] == f"socket://127.0.0.1:{stubborn.port}":
            real_close = device.close

            def close_failing_once():
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise OSError("the port would not close")
                real_close()  # the test's own cleanup at the end succeeds

            monkeypatch.setattr(device, "close", close_failing_once)
            opened["device"] = device
        return device

    try:
        monkeypatch.setattr(SerialDriver, "open", open_with_a_close_that_fails_once)
        monkeypatch.setattr(RunStore, "insert_attempt", _always_raises("the wiring is broken"))
        session = _session_on(tmp_path, "Two open Devices", _two_devices_and_lua_state(
            f"socket://127.0.0.1:{stubborn.port}", f"socket://127.0.0.1:{following.port}"))
        engine = session.start()

        still_open, closed = _registry_at_finished(session, engine)
        assert attempts["n"] == 1, "the failing close was not the one the teardown hit"
        assert still_open == [], "the registry still held a Device when the run finished"
        assert wait_for_disconnect(following, 3.0, 1), "the next Device never closed"
        assert not stubborn.closed, "the failing close was meant to leave this peer connected"
    finally:
        try:
            if "device" in opened:
                opened["device"].close()
        finally:
            stubborn.stop()
            following.stop()


def test_the_next_run_on_the_same_session_reopens_the_port_with_clean_lua_state(tmp_path, monkeypatch):
    """Nothing the crashed run wrote into Lua is visible to the run that follows it."""
    peer = LoopbackPeer()
    peer.start()
    try:
        port = f"socket://127.0.0.1:{peer.port}"
        session = _session_on(tmp_path, "Two open Devices", _two_devices_and_lua_state(port),
                             extra={"Clean Lua check": _clean_lua_check(port)})
        monkeypatch.setattr(RunStore, "insert_attempt", _always_raises("the wiring is broken"))
        engine = session.start()
        still_open, closed = _registry_at_finished(session, engine)
        assert still_open == [] and len(closed) == 2, closed

        monkeypatch.undo()  # the second run is an ordinary one
        session.select_schedule("Clean Lua check")
        session.start()
        _finished(session)

        second = session.store.get_test_run(session.run_uuid)
        assert second["end_reason"] == "COMPLETED" and second["result"] == "PASS", second
        (attempt,) = session.store.attempts(session.run_uuid)
        assert attempt["msg"] == "nil,nil,nil", attempt
        assert wait_for_disconnect(peer, 3.0, 3), "the reopened port was not released either"
    finally:
        peer.stop()


@pytest.mark.skipif(sys.platform != "linux", reason="the pseudo-terminal check needs a Linux pty")
def test_a_crash_on_a_pseudo_terminal_leaves_no_descriptor_on_the_port(tmp_path, monkeypatch):
    """With the teardown disabled the run keeps two descriptors on the port (the probe saw this);
    with it in place none is left. An exclusive open proves nothing here: on Linux it succeeds even
    while another descriptor holds the port, because ATE does not take that lock itself."""
    import os
    import pty
    import tty

    master, slave = pty.openpty()
    tty.setraw(slave)
    port = os.ttyname(slave)
    os.close(slave)  # the master stays here; any descriptor on the port now belongs to a Device

    def fds_on_port() -> list[int]:
        out = []
        for fd in os.listdir("/proc/self/fd"):
            try:
                if os.readlink(f"/proc/self/fd/{fd}") == port:
                    out.append(int(fd))
            except OSError:
                continue
        return out

    try:
        monkeypatch.setattr(RunStore, "insert_attempt", _always_raises("the wiring is broken"))
        session = _session_on(tmp_path, "Two open Devices", _two_devices_and_lua_state(port))
        engine = session.start()
        still_open, closed = _registry_at_finished(session, engine)
        assert still_open == [] and len(closed) == 2, closed
        assert fds_on_port() == [], f"descriptors still point at {port}: {fds_on_port()}"
    finally:
        os.close(master)
