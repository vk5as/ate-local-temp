"""Packet 32b: an abort ends a Device call in flight, within story 83's limit, for every Device Type.

Each test runs a real schedule through the real run engine with a 600 s timeout on the call, so nothing
but the abort can end it in time. The peers are real loopback sockets; what stands in for a call only
the OS or a C library can end (a connect, `viRead`) is a Python wait the abort cannot reach either.

Every wait on an abort goes through `wait_for_aborted_run`, which fails past story 83's 10 s and warns
past the 0.5 s aim: a 9 s abort shows up in pytest's summary instead of passing. A test that holds a
call behind the abort on purpose ends past 0.5 s, and its warning says so from the timeline.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from pathlib import Path

import pytest
from pyvisa import constants

from ate.devices import visa
from ate.devices.abort import DeviceAborted, _Call, run_abortable
from ate.devices.registry import DeviceRegistry
from ate.devices.tcp import TcpDriver, _TcpDevice
from ate.devices.visa import VisaDriver
from ate.engine.run_engine import RunEngine, RunStatus
from ate.storage.store import RunStore

from ._fixtures import (
    ABORT_MUST_STOP_S,
    LoopbackPeer,
    describe_stuck_runs,
    temp_directory,
    wait_for_aborted_run,
)
from .test_serial_devices import _StuckPeer

#: Story 83: an abort must stop the run within this. `wait_for_aborted_run` also warns past the
#: 0.5 s aim, so an abort that crawls does not pass as a passing test (packet 32b).
MUST_STOP_SECONDS = ABORT_MUST_STOP_S

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
#: The stuck-write test's payload, in pieces the step logs as they go out. A piece is 1 MB and
#: there are 200 of them, so the write cannot finish inside the 0.3 s before the abort once the
#: peer's window is full, and the log says how far it got when the abort landed.
_WRITE_PIECES = 200
_WRITE_PIECE_BYTES = 1000000
_STUCK_WRITE = (
    f'for i = 1, {_WRITE_PIECES} do '
    f'ATE.write(dev, {{data = string.rep("x", {_WRITE_PIECE_BYTES})}}) '
    f'ATE.log(Log.INFO, "piece " .. i .. " of {_WRITE_PIECES}") end'
)


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
        self.timeline: list[tuple[float, str]] = []
        self.engine = RunEngine(self.store, str(path), ate_fns={"ready": self._ready})
        self.engine.run_uuid_reported.connect(lambda run_uuid: self.seen.update(run_uuid=run_uuid))
        self.engine.state_changed.connect(self._note_state)

    def _note_state(self, status: RunStatus) -> None:
        """FINISHED sets the event and puts the moment on the timeline `wait_for_aborted_run` prints."""
        if status == RunStatus.FINISHED:
            self.finished.set()
            self.timeline.append((time.monotonic(), "run FINISHED"))

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
        """Start the run, let the call begin, abort it and return how long the run took to end.

        Story 83's limit is enforced by `wait_for_aborted_run`, which also warns past 0.5 s: a 9 s
        abort is not a passing test."""
        self.engine.start()
        assert self.pump(app, self.ready, 10.0), describe_stuck_runs()
        time.sleep(0.3)  # into the call: ATE.ready() is the line before it
        assert not self.finished.is_set(), "the call ended by itself, so nothing here tests an abort"
        started = time.monotonic()
        self.timeline.append((started, "abort requested"))
        self.engine.request_abort()
        return wait_for_aborted_run(app, self.finished, started, timeline=self.timeline)

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
        with temp_directory() as td:
            run = _Run(td, _schedule(f"dev = {opened} ATE.ready() ATE.read(dev)"))
            try:
                took = run.abort_in_flight(qapp)
                print(f"\n{device_type} read: abort took {took:.3f} s")
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
    """UDP is not here: a datagram goes out or is dropped, so its socket does not stall.

    The peer stops reading, the write stalls on the peer's window, and the abort has to arrive
    while the write is stalled. The payload goes out in 1 MB pieces and the step logs each one,
    so the run records how much was out when the abort landed, and a write that finished on its
    own fails the test rather than quietly reporting a different thing. On the Windows runner the
    whole 200 MB went out in a few tenths of a second: the peer's accepted socket had the
    platform's default receive buffer, because on Windows an accepted socket does not inherit the
    listener's ``SO_RCVBUF`` (Linux does, which is why this passed here — packet 35b, CI
    41407051552). The read versions of these tests, which do not depend on a buffer filling, run
    on every platform."""
    peer = _StuckPeer()
    try:
        opened = _OPEN[device_type].format(port=peer.port)
        with temp_directory() as td:
            run = _Run(td, _schedule(f"dev = {opened} ATE.ready() {_STUCK_WRITE}"))
            try:
                took = run.abort_in_flight(qapp)
                run_uuid = run.seen["run_uuid"]
                pieces = sum(1 for line in run.store.log_lines(run_uuid) if line["text"].startswith("piece "))
                print(
                    f"\n{device_type} write: abort took {took:.3f} s, "
                    f"{pieces * _WRITE_PIECE_BYTES} of {_WRITE_PIECES * _WRITE_PIECE_BYTES} bytes out "
                    f"when it landed (peer accepted: {peer.accepted.is_set()})"
                )
                run.assert_ended_aborted()
                assert pieces < _WRITE_PIECES, (
                    f"the write finished on its own ({pieces} pieces of {_WRITE_PIECES} logged) before "
                    f"the abort, so nothing here ended a write in flight: {run.store.nodes(run_uuid)}"
                )
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
    with temp_directory() as td:
        run = _Run(td, _schedule(f"ATE.ready() dev = {_OPEN['TCP'].format(port=1)}"))
        try:
            took = run.abort_in_flight(qapp)
            print(f"\nTCP open: abort took {took:.3f} s")
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
    with temp_directory() as td:
        opened = 'ATE.open(DeviceType.VISA, {resource="GPIB0::5::INSTR", timeout=600, readTerm="\\n", writeTerm=""})'
        run = _Run(td, _schedule(f"dev = {opened} ATE.ready() ATE.read(dev)"))
        try:
            took = run.abort_in_flight(qapp)
            print(f"\nVISA viRead: abort took {took:.3f} s")
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
    with temp_directory() as td:
        opened = 'ATE.open(DeviceType.VISA, {resource="GPIB0::5::INSTR", timeout=600, readTerm="\\n", writeTerm=""})'
        source = _schedule(f"dev = {opened} ATE.ready() ATE.read(dev)", post='ATE.write(dev, {data = "OUTP OFF"})')
        run = _Run(td, source)
        try:
            run.engine.start()
            assert run.pump(qapp, run.ready, 10.0), describe_stuck_runs()
            time.sleep(0.3)
            started = time.monotonic()
            run.timeline.append((started, "abort requested"))
            run.engine.request_abort()
            time.sleep(0.5)
            assert not run.finished.is_set() and resource.calls == []  # the write has not gone across the read
            resource.release.set()
            assert wait_for_aborted_run(qapp, run.finished, started, timeline=run.timeline), describe_stuck_runs()
            assert resource.calls[:2] == ["read ended", "write b'OUTP OFF'"]
            run.assert_ended_aborted()
        finally:
            resource.release.set()
            run.engine.stop_and_wait()


def test_a_read_after_an_abandoned_read_waits_for_it(qapp, monkeypatch):
    """`read` sets the terminator on the session before it reads, so it cannot do that while an
    abandoned `viRead` still holds the session: `write` and `_visa_read` wait first, and so must `read`."""
    resource = _stuck_visa(monkeypatch)
    with temp_directory() as td:
        opened = 'ATE.open(DeviceType.VISA, {resource="GPIB0::5::INSTR", timeout=600, readTerm="\\n", writeTerm=""})'
        source = _schedule(f"dev = {opened} ATE.ready() ATE.read(dev)", post='ATE.read(dev)')
        run = _Run(td, source)
        try:
            run.engine.start()
            assert run.pump(qapp, run.ready, 10.0), describe_stuck_runs()
            time.sleep(0.3)
            started = time.monotonic()
            run.timeline.append((started, "abort requested"))
            run.engine.request_abort()
            time.sleep(0.5)  # the Post-test Action has begun its read
            assert not run.finished.is_set(), describe_stuck_runs()
            assert resource.events == ["attr", "attr", "attr", "read"], f"the second read touched the session during the first: {resource.events}"
            resource.release.set()
            assert wait_for_aborted_run(qapp, run.finished, started, timeline=run.timeline), describe_stuck_runs()
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
    with temp_directory() as td:
        opened = 'ATE.open(DeviceType.VISA, {resource="GPIB0::5::INSTR", timeout=600, readTerm="\\n", writeTerm=""})'
        source = _schedule(f"dev = {opened} ATE.ready() ATE.read(dev)", post='ATE.write(dev, {data = "OUTP OFF"})')
        run = _Run(td, source)
        try:
            run.engine.start()
            assert run.pump(qapp, run.ready, 10.0), describe_stuck_runs()
            time.sleep(0.3)
            started = time.monotonic()
            run.timeline.append((started, "abort requested"))
            run.engine.request_abort()
            time.sleep(0.5)  # the Post-test Action has begun, and the flag was cleared for it
            again = time.monotonic()
            run.timeline.append((again, "second abort requested"))
            run.engine.request_abort()
            assert wait_for_aborted_run(qapp, run.finished, again, timeline=run.timeline), describe_stuck_runs()
            post = [action for action in run.store.actions(run.seen["run_uuid"]) if action["phase"] == "post"]
            assert post, "the Post-test Action never began, so there was nothing for the second abort to end"
            assert post[-1]["outcome"] == "aborted", post[-1]
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
        with temp_directory() as td:
            run = _Run(td, _schedule(f"dev = {_OPEN['TCP'].format(port=peer.port)} ATE.ready() ATE.read(dev)"))
            try:
                took = run.abort_in_flight(qapp)
                print(f"\nTCP close: abort took {took:.3f} s")
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
    with temp_directory() as td:
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


class _Flag:
    """A switchable stand-in for the run's abort flag."""

    def __init__(self) -> None:
        self.aborted = False


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


def test_an_open_or_close_left_behind_by_an_abort_leaves_a_trace_line(monkeypatch):
    """17c traces a Device operation that failed, and `except OSError` misses DeviceAborted.

    A call the worker gave up on is still running, so the trace says `aborted` rather than calling it
    a failure: without it the debug window has no line at all for an open or close the abort left behind."""
    lines: list[str] = []
    flag = _Flag()
    registry = DeviceRegistry(
        trace_enabled=True,
        emit=lambda _level, text, source="device": lines.append(text),
        abort_flag=flag,
    )
    registry.register("TCP", TcpDriver)

    class _SlowHandle:
        def close(self) -> None:
            time.sleep(1.0)

    def open_that_stalls_when_aborted(_driver, _params, _limit=None):
        if flag.aborted:
            time.sleep(1.0)  # only the OS or its own timeout can end this, as a connect would
        return _SlowHandle()

    monkeypatch.setattr(TcpDriver, "open", open_that_stalls_when_aborted)

    flag.aborted = True
    with pytest.raises(DeviceAborted):
        registry.open_device("TCP", {"host": "127.0.0.1", "port": 1})
    opens = [text for text in lines if text.startswith("open TCP 127.0.0.1:1")]
    assert len(opens) == 1 and "aborted" in opens[0], lines

    flag.aborted = False
    handle = registry.open_device("TCP", {"host": "127.0.0.1", "port": 1})
    flag.aborted = True
    with pytest.raises(DeviceAborted):
        registry.close_device(handle)
    closes = [text for text in lines if text.startswith("close ")]
    assert len(closes) == 1 and "aborted" in closes[0], lines
