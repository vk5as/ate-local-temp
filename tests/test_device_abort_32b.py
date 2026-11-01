"""Packet 32b: an abort ends a Device call in flight, within story 83's limit, for every Device Type.

Each test runs a real schedule through the real run engine with a 600 s timeout on the call, so nothing
but the abort can end it in time. The peers are real loopback sockets; what stands in for a call only
the OS or a C library can end (a connect, `viRead`) is a Python wait the abort cannot reach either."""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from pathlib import Path

import pytest
from pyvisa import constants

from ate.devices import visa
from ate.devices.abort import _Call, run_abortable
from ate.devices.tcp import TcpDriver, _TcpDevice
from ate.devices.visa import VisaDriver
from ate.engine.run_engine import RunEngine, RunStatus
from ate.storage.store import RunStore

from ._fixtures import LoopbackPeer, describe_stuck_runs, scratch_directory
from .test_serial_devices import _StuckPeer

#: Story 83: an abort must stop within this, and should within 0.5 s.
MUST_STOP_SECONDS = 10.0

_OPEN = {
    "TCP": 'ATE.open(DeviceType.TCP, {{host="127.0.0.1", port={port}, timeout=600, readTerm="\\n", writeTerm=""}})',
    "SERIAL": (
        'ATE.open(DeviceType.SERIAL, {{port="socket://127.0.0.1:{port}", baud=9600, dataBits=8, stopBits=1, '
        'parity=Parity.NONE, flowControl=FlowControl.NONE, timeout=600, readTerm="\\n", writeTerm=""}})'
    ),
    "UDP": 'ATE.open(DeviceType.UDP, {{host="127.0.0.1", port={port}, localPort=0, timeout=600}})',
    "VISA": (
        'ATE.open(DeviceType.VISA, {{resource="TCPIP0::127.0.0.1::{port}::SOCKET", timeout=600, readTerm="\\n", '
        'writeTerm=""}})'
    ),
}
_BIG_WRITE = 'ATE.write(dev, {data = string.rep("x", 200000000)})'


@pytest.fixture(autouse=True)
def _no_cached_visa_manager(monkeypatch):
    """Keep the process-wide manager these real opens build out of later tests: pyvisa shares it, and
    the CLI's self-check, run in-process by a later test, closes it."""
    monkeypatch.setattr(visa, "_manager", None)


def _schedule(body: str, post: str = "") -> str:
    post_action = f":setPostTestAction(function() {post} end)" if post else ""
    return (
        f'local dev\nreturn ScheduleParams("s", "d"){post_action}:setSchedule({{\n'
        f'StepParams("device"):setStepNo("1."):setAction(function() {body} end),\n'
        'StepParams("later"):setStepNo("2."):setAction(function() ATE.report(Result.PASS, "ok") end),\n})\n'
    )


class _Run:
    """One schedule on a real engine, announcing when it reaches ``ATE.ready()``."""

    def __init__(self, td: str, source: str) -> None:
        self.store = RunStore(Path(td) / "ate.db")
        path = Path(td) / "schedule.tslua"
        path.write_text(source, encoding="utf-8", newline="\n")
        self.ready, self.finished = threading.Event(), threading.Event()
        self.seen: dict = {}
        self.engine = RunEngine(self.store, str(path), ate_fns={"ready": self._ready})
        self.engine.run_uuid_reported.connect(lambda run_uuid: self.seen.update(run_uuid=run_uuid))
        self.engine.state_changed.connect(lambda status: self.finished.set() if status == RunStatus.FINISHED else None)

    def _ready(self) -> bool:
        self.ready.set()
        return True

    def pump(self, app, event: threading.Event, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not event.is_set() and time.monotonic() < deadline:
            app.processEvents()
            event.wait(0.005)
        app.processEvents()
        return event.is_set()

    def abort_in_flight(self, app) -> float:
        """Start the run, let the call begin, abort it and return how long the run took to end."""
        self.engine.start()
        assert self.pump(app, self.ready, 10.0), describe_stuck_runs()
        time.sleep(0.3)  # into the call: ATE.ready() is the line before it
        assert not self.finished.is_set(), "the call ended by itself, so nothing here tests an abort"
        started = time.monotonic()
        self.engine.request_abort()
        assert self.pump(app, self.finished, MUST_STOP_SECONDS + 5.0), describe_stuck_runs()
        return time.monotonic() - started

    def assert_ended_aborted(self) -> None:
        run_uuid = self.seen["run_uuid"]
        assert self.store.get_test_run(run_uuid)["end_reason"] == "ABORTED"
        nodes = self.store.nodes(run_uuid)
        assert [n["result"] for n in nodes] == ["ERROR", "NOTRUN"]
        assert [a["msg"] for a in self.store.attempts(run_uuid)] == ["Aborted by Operator"]


@pytest.mark.parametrize("device_type", ["TCP", "SERIAL", "UDP", "VISA"])
def test_an_abort_ends_a_long_read(qapp, device_type):
    peer = _UdpPeer() if device_type == "UDP" else LoopbackPeer()
    if device_type != "UDP":
        peer.start()
    try:
        opened = _OPEN[device_type].format(port=peer.port)
        with scratch_directory() as td:
            run = _Run(td, _schedule(f"dev = {opened} ATE.ready() ATE.read(dev)"))
            try:
                took = run.abort_in_flight(qapp)
                print(f"\n{device_type} read: abort took {took:.3f} s")
                assert took < MUST_STOP_SECONDS
                run.assert_ended_aborted()
            finally:
                run.engine.stop_and_wait()
    finally:
        peer.stop()


class _UdpPeer:
    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]

    def stop(self) -> None:
        self._sock.close()


@pytest.mark.parametrize("device_type", ["TCP", "SERIAL", "VISA"])
def test_an_abort_ends_a_long_write_to_a_peer_that_stopped_reading(qapp, device_type):
    """UDP is not here: a datagram goes out or is dropped, so its socket does not stall."""
    peer = _StuckPeer()
    try:
        opened = _OPEN[device_type].format(port=peer.port)
        with scratch_directory() as td:
            run = _Run(td, _schedule(f"dev = {opened} ATE.ready() {_BIG_WRITE}"))
            try:
                took = run.abort_in_flight(qapp)
                print(f"\n{device_type} write: abort took {took:.3f} s")
                assert took < MUST_STOP_SECONDS
                run.assert_ended_aborted()
            finally:
                run.engine.stop_and_wait()
    finally:
        peer.stop()


def test_an_abort_leaves_behind_a_connect_only_the_os_can_end(qapp, monkeypatch):
    """A connect or DNS lookup cannot be interrupted, so the worker leaves it; a handle it still produces
    is closed, not leaked."""
    release, closed = threading.Event(), threading.Event()

    class _LateHandle:
        def close(self) -> None:
            closed.set()

    def stuck_open(_driver, _params, _limit=None):
        release.wait(30.0)
        return _LateHandle()

    monkeypatch.setattr(TcpDriver, "open", stuck_open)
    with scratch_directory() as td:
        run = _Run(td, _schedule(f"ATE.ready() dev = {_OPEN['TCP'].format(port=1)}"))
        try:
            took = run.abort_in_flight(qapp)
            print(f"\nTCP open: abort took {took:.3f} s")
            assert took < MUST_STOP_SECONDS
            run.assert_ended_aborted()
            assert not closed.is_set()
            release.set()
            assert closed.wait(5.0), "the handle of a connect the abort left behind was never closed"
        finally:
            release.set()
            run.engine.stop_and_wait()


class _StuckResource:
    """A pyvisa resource whose `viRead` blocks as a C call does: nothing the abort can reach ends it."""

    resource_class = "INSTR"
    session = 1

    def __init__(self) -> None:
        self.release = threading.Event()
        self.reads = 0
        self.timeout = None
        self.visalib = self
        self.calls: list[str] = []
        self.events: list[str] = []  # everything in the order it happened, terminators included

    def read(self, _session, _count):
        self.reads += 1
        self.events.append("read")
        self.release.wait(30.0)
        self.calls.append("read ended")
        self.events.append("read ended")
        return b"late\n", constants.StatusCode.success

    @contextlib.contextmanager
    def ignore_warning(self, *_codes):
        yield

    def set_visa_attribute(self, *_args) -> None:
        self.events.append("attr")  # an attribute call needs the session, and cannot be made during a read

    def write_raw(self, payload: bytes) -> int:
        self.calls.append(f"write {payload!r}")
        self.events.append(f"write {payload!r}")
        return len(payload)

    def close(self) -> None:
        self.calls.append("close")
        self.events.append("close")


class _StuckManager:
    def __init__(self, resource: _StuckResource) -> None:
        self.resource = resource

    def open_resource(self, _name, **_kwargs):
        return self.resource


def _stuck_visa(monkeypatch) -> _StuckResource:
    resource = _StuckResource()
    monkeypatch.setattr("ate.devices.visa.get_resource_manager", lambda _factory=None: _StuckManager(resource))
    return resource


def test_an_abort_leaves_behind_a_visa_read_in_c_and_closes_only_after_it(qapp, monkeypatch):
    """No poll of the instrument (one `viRead` however long the wait), and the session stays open until
    the call holding it ends."""
    resource = _stuck_visa(monkeypatch)
    with scratch_directory() as td:
        opened = 'ATE.open(DeviceType.VISA, {resource="GPIB0::5::INSTR", timeout=600, readTerm="\\n", writeTerm=""})'
        run = _Run(td, _schedule(f"dev = {opened} ATE.ready() ATE.read(dev)"))
        try:
            took = run.abort_in_flight(qapp)
            print(f"\nVISA viRead: abort took {took:.3f} s")
            assert took < MUST_STOP_SECONDS
            run.assert_ended_aborted()
            assert resource.reads == 1
            assert resource.calls == []  # the run has ended, and nothing has touched the session under the read
            resource.release.set()
            deadline = time.monotonic() + 5.0
            while "close" not in resource.calls and time.monotonic() < deadline:
                time.sleep(0.01)
            assert resource.calls == ["read ended", "close"]
        finally:
            resource.release.set()
            run.engine.stop_and_wait()


def test_a_post_test_action_waits_for_the_call_an_abort_left_behind(qapp, monkeypatch):
    """Story 84: the power-down still reaches the instrument, after the read, never across it."""
    resource = _stuck_visa(monkeypatch)
    with scratch_directory() as td:
        opened = 'ATE.open(DeviceType.VISA, {resource="GPIB0::5::INSTR", timeout=600, readTerm="\\n", writeTerm=""})'
        source = _schedule(f"dev = {opened} ATE.ready() ATE.read(dev)", post='ATE.write(dev, {data = "OUTP OFF"})')
        run = _Run(td, source)
        try:
            run.engine.start()
            assert run.pump(qapp, run.ready, 10.0), describe_stuck_runs()
            time.sleep(0.3)
            run.engine.request_abort()
            time.sleep(0.5)
            assert not run.finished.is_set() and resource.calls == []  # the write has not gone across the read
            resource.release.set()
            assert run.pump(qapp, run.finished, MUST_STOP_SECONDS), describe_stuck_runs()
            assert resource.calls[:2] == ["read ended", "write b'OUTP OFF'"]
            run.assert_ended_aborted()
        finally:
            resource.release.set()
            run.engine.stop_and_wait()


def test_a_read_after_an_abandoned_read_waits_for_it(qapp, monkeypatch):
    """`read` sets the terminator on the session before it reads, so it cannot do that while an
    abandoned `viRead` still holds the session: `write` and `_visa_read` wait first, and so must `read`."""
    resource = _stuck_visa(monkeypatch)
    with scratch_directory() as td:
        opened = 'ATE.open(DeviceType.VISA, {resource="GPIB0::5::INSTR", timeout=600, readTerm="\\n", writeTerm=""})'
        source = _schedule(f"dev = {opened} ATE.ready() ATE.read(dev)", post='ATE.read(dev)')
        run = _Run(td, source)
        try:
            run.engine.start()
            assert run.pump(qapp, run.ready, 10.0), describe_stuck_runs()
            time.sleep(0.3)
            started = time.monotonic()
            run.engine.request_abort()
            time.sleep(0.5)  # the Post-test Action has begun its read
            assert not run.finished.is_set(), describe_stuck_runs()
            assert resource.events == ["attr", "attr", "attr", "read"], f"the second read touched the session during the first: {resource.events}"
            resource.release.set()
            assert run.pump(qapp, run.finished, MUST_STOP_SECONDS), describe_stuck_runs()
            assert resource.events == [
                "attr", "attr", "attr", "read", "read ended",
                "attr", "attr", "read", "read ended", "close",
            ], resource.events
            run.assert_ended_aborted()
        finally:
            resource.release.set()
            run.engine.stop_and_wait()


def test_a_second_abort_ends_a_post_test_action_waiting_for_a_call_left_behind(qapp, monkeypatch):
    resource = _stuck_visa(monkeypatch)
    with scratch_directory() as td:
        opened = 'ATE.open(DeviceType.VISA, {resource="GPIB0::5::INSTR", timeout=600, readTerm="\\n", writeTerm=""})'
        source = _schedule(f"dev = {opened} ATE.ready() ATE.read(dev)", post='ATE.write(dev, {data = "OUTP OFF"})')
        run = _Run(td, source)
        try:
            run.engine.start()
            assert run.pump(qapp, run.ready, 10.0), describe_stuck_runs()
            time.sleep(0.3)
            run.engine.request_abort()
            time.sleep(0.5)  # the Post-test Action has begun, and the flag was cleared for it
            run.engine.request_abort()
            assert run.pump(qapp, run.finished, MUST_STOP_SECONDS), describe_stuck_runs()
            actions = run.store.actions(run.seen["run_uuid"]) if hasattr(run.store, "actions") else []
            assert not actions or actions[-1]["outcome"] == "aborted"
            assert "write b'OUTP OFF'" not in resource.calls
        finally:
            resource.release.set()
            run.engine.stop_and_wait()


def test_an_aborted_runs_stuck_close_does_not_hold_the_worker(qapp, monkeypatch):
    """At run end the abort flag is clear, so the run's own closes carry their own bound."""
    release = threading.Event()
    real_close = _TcpDevice._close_transport

    def stuck_close(device) -> None:
        release.wait(30.0)
        real_close(device)

    monkeypatch.setattr(_TcpDevice, "_close_transport", stuck_close)
    peer = LoopbackPeer()
    peer.start()
    try:
        with scratch_directory() as td:
            run = _Run(td, _schedule(f"dev = {_OPEN['TCP'].format(port=peer.port)} ATE.ready() ATE.read(dev)"))
            try:
                took = run.abort_in_flight(qapp)
                print(f"\nTCP close: abort took {took:.3f} s")
                assert took < MUST_STOP_SECONDS
                run.assert_ended_aborted()
                lines = [line["text"] for line in run.store.log_lines(run.seen["run_uuid"])]
                assert any("still closing" in line for line in lines), lines
            finally:
                release.set()
                run.engine.stop_and_wait()
    finally:
        release.set()
        peer.stop()


def test_a_blocking_call_with_no_abort_is_untouched(qapp, monkeypatch):
    """One `viRead`, given the call's whole timeout: the helper thread changes nothing an instrument sees."""
    resource = _stuck_visa(monkeypatch)
    resource.release.set()
    with scratch_directory() as td:
        opened = 'ATE.open(DeviceType.VISA, {resource="GPIB0::5::INSTR", timeout=600, readTerm="\\n", writeTerm=""})'
        body = (
            f'dev = {opened} local r = ATE.read(dev) '
            'ATE.report(r.data == "late" and Result.PASS or Result.FAIL, "read") ATE.close(dev)'
        )
        run = _Run(td, _schedule(body))
        run.engine.start()
        try:
            assert run.pump(qapp, run.finished, MUST_STOP_SECONDS), describe_stuck_runs()
            assert run.store.get_test_run(run.seen["run_uuid"])["end_reason"] == "COMPLETED"
            assert [n["result"] for n in run.store.nodes(run.seen["run_uuid"])] == ["PASS", "PASS"]
            assert resource.reads == 1 and resource.timeout == 600000
        finally:
            run.engine.stop_and_wait()


def test_a_driver_used_directly_has_no_helper_thread():
    """Without the registry there is no flag, and the call is the plain blocking one."""
    calls = []

    class _Resource(_StuckResource):
        def read(self, _session, _count):
            calls.append(threading.current_thread())
            return b"x\n", constants.StatusCode.success

    device = VisaDriver(manager_factory=lambda: _StuckManager(_Resource())).open(
        {"resource": "GPIB0::5::INSTR", "timeout": 1, "readTerm": "\n", "writeTerm": ""}
    )
    assert device.read({})[0] == b"x"
    assert calls == [threading.current_thread()]
    device.close()


class _NeverAborts:
    """An abort flag that stays clear: nothing here tests the abort path, only the waiting."""

    aborted = False


class _NotAnException(BaseException):
    """What a helper thread can die on that `except Exception` misses."""


def _run_off_thread(fn) -> tuple[bool, list[BaseException]]:
    """Run ``fn`` on a daemon thread and return (it returned within 5 s, what it raised).

    The worker's wait is what is being tested, so it cannot be allowed to hang the suite: a wait
    that never ends fails with the reason instead of sitting until the run's 600 s timeout does."""
    caught: list[BaseException] = []
    ended = threading.Event()

    def worker() -> None:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - the test decides what it expected
            caught.append(exc)
        ended.set()

    threading.Thread(target=worker, daemon=True).start()
    return ended.wait(5.0), caught


def test_a_helper_thread_dying_on_a_base_exception_reaches_the_worker():
    """A BaseException in the helper must reach the worker, not leave it joining a dead thread.

    `run_abortable` caught Exception and watched only for an abort, so a helper killed by anything
    else recorded no outcome and the worker polled a finished thread until the call's own timeout."""
    def boom() -> None:
        raise _NotAnException("the helper died on something that is not an Exception")

    returned, caught = _run_off_thread(lambda: run_abortable(boom, _NeverAborts()))
    assert returned, "run_abortable never returned: the helper died on a BaseException with no outcome"
    assert len(caught) == 1 and isinstance(caught[0], _NotAnException), caught


def test_a_helper_thread_that_records_no_outcome_is_not_waited_on(monkeypatch):
    """The wait also leaves when the helper is dead, however it got that way."""
    monkeypatch.setattr(_Call, "_run", lambda self: None)  # a helper that ends without an outcome
    returned, caught = _run_off_thread(lambda: run_abortable(lambda: 1, _NeverAborts()))
    assert returned, "run_abortable kept joining a thread that had already ended"
    assert len(caught) == 1 and "without an outcome" in str(caught[0]), caught
