"""Packet 17b — device failures: the reader's warning, attributed logging, and recovery.

Seam-1 coverage of a failed TCP Device (peer close or reset) against a loopback TCP peer (no
mocks). A background reader failure is recorded on the device as exactly one (time, message)
warning, drained by the host at a safe point, and logged as one WARNING (source device) naming
the device; the already-buffered bytes stay readable, after which every read and write fails
through its ``onError``; a close + a fresh open recovers; and a write honours the open-time and
per-call ``timeout`` reporting the bytes actually sent. The device side lives in
:mod:`ate.devices.tcp`; :func:`RunEngine._flush_device_warnings` attributes each failure to the
action it happened in (a step's ``node_seq``/``attempt_no`` or a Pre-/Post-test Action's
``action_seq``) or to the run when it happened between actions."""

from __future__ import annotations

import struct
import threading
import time
import socket
import tempfile
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QCoreApplication

from ate.devices.tcp import TcpDriver
from ate.engine.run_engine import RunEngine, RunStatus
from ate.storage.store import RunStore

from ._fixtures import (
    LoopbackPeer,
    _drive,
)
from .test_tcp_read_write import _run_with_peer, _step, _step_message


def _open(peer: LoopbackPeer, read_term: str = "", write_term: str = ""):
    """Open a TCP device on ``peer`` and wait until the peer has accepted it."""
    dev = TcpDriver().open(
        {"host": "127.0.0.1", "port": peer.port, "timeout": 5, "readTerm": read_term, "writeTerm": write_term}
    )
    assert peer.wait_for_connection()
    return dev


def _wait_for(dev, predicate, timeout: float = 2.0) -> None:
    """Wait, under the device lock, until ``predicate(dev)`` holds; the reader notifies the
    condition on every append and failure, so this polls with a deadline instead of sleeping for
    a guessed time."""
    with dev._cond:
        assert dev._cond.wait_for(lambda: predicate(dev), timeout), "device never reached the expected state"


class _ManualPeer:
    """A one-connection TCP peer the test drives by hand: it accepts in the background, records
    the wall time it reset the connection (``reset_at``), and can shrink its receive buffer so a
    small write fills the path (``rcvbuf``). ``reset_on_accept`` resets as soon as it accepts."""

    def __init__(self, rcvbuf: int | None = None, reset_on_accept: bool = False) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if rcvbuf is not None:  # inherited by the accepted socket; must be set before listen
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self.conn: socket.socket | None = None
        self.reset_at: float | None = None
        self._accepted = threading.Event()
        self._reset_on_accept = reset_on_accept
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        try:
            self.conn, _ = self._listener.accept()
        except OSError:
            return
        if self._reset_on_accept:
            self.reset()
        self._accepted.set()

    def wait_accepted(self, timeout: float = 2.0) -> socket.socket:
        assert self._accepted.wait(timeout), "the peer never accepted"
        return self.conn

    def reset(self) -> None:
        """Close the connection with an RST and remember when (wall time)."""
        self.conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        self.reset_at = time.time()
        self.conn.close()

    def stop(self) -> None:
        self._listener.close()
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass


def _at(line: dict) -> float:
    """A log line's ``at`` as a wall-clock timestamp."""
    return datetime.fromisoformat(line["at"]).timestamp()


def _device_warnings(store: RunStore, ru: str) -> list[dict]:
    return [ln for ln in store.log_lines(ru) if ln["level"] == "WARNING" and ln["source"] == "device"]


# --- Unit: the reader's warning, and _fail idempotency -------------------------------------


def test_failure_records_one_warning_naming_device_and_error() -> None:
    """The reader's failure enqueues exactly one (time, message) warning naming the device, and
    take_warnings drains it once."""
    peer = LoopbackPeer(payload=b"abc", hangup="reset")  # send bytes, then RST
    peer.start()
    try:
        dev = _open(peer)
        _wait_for(dev, lambda d: d._failed is not None)  # the reader saw the reset
        warnings = dev.take_warnings()
        assert len(warnings) == 1, f"exactly one warning per failure, got {len(warnings)}"
        t, text = warnings[0]
        assert t > 0
        assert text.startswith(f"TCP 127.0.0.1:{peer.port}:"), f"warning does not name the device: {text!r}"
        assert "reset" in text.lower(), f"warning does not carry the error: {text!r}"
        assert dev.take_warnings() == [], "a warning is drained once"
        dev.close()
    finally:
        peer.stop()


def test_fail_keeps_the_first_error_and_drops_later_duplicates() -> None:
    """A second failure (the race of a write error and the reader on the same close) is dropped:
    the device records exactly one warning, the first."""
    peer = LoopbackPeer()
    peer.start()
    try:
        dev = _open(peer)
        dev._fail("first")
        dev._fail("second")  # a duplicate on the same failure
        warnings = dev.take_warnings()
        assert len(warnings) == 1, f"first failure wins, got {len(warnings)}"
        assert "first" in warnings[0][1], f"the kept error is not the first: {warnings[0][1]!r}"
        assert dev.take_warnings() == [], "a warning is drained once"
        dev.close()
    finally:
        peer.stop()


def test_later_read_write_failures_add_no_further_warning() -> None:
    """After the reader has recorded the failure, a read and a write that hit the same failure
    add nothing: one failure, one warning."""
    peer = LoopbackPeer(payload=b"abc", hangup="reset")
    peer.start()
    try:
        dev = _open(peer)
        first = dev.read({"timeout": 2})
        _wait_for(dev, lambda d: d._failed is not None)
        second = dev.read({"timeout": 2})
        write = dev.write({"data": "x", "timeout": 2})
        dev.close()
    finally:
        peer.stop()

    assert first == (b"abc", 3, False, None), f"the buffered bytes must be readable first: {first!r}"
    assert (second[0], second[1], second[2]) == (None, 0, False)
    assert second[3] and "reset" in second[3].lower(), f"the next read carries the failure: {second[3]!r}"
    assert (write[0], write[1]) == (False, 0), f"a write after the failure fails with no bytes: {write!r}"
    assert write[2] == second[3], "the write reports the same failure message as the read"
    assert len(dev.take_warnings()) == 1, f"a late read/write adds no further warning: {dev.take_warnings()!r}"


# --- Unit: RunEngine._flush_device_warnings ----------------------------------------------


def test_flush_attributes_a_between_action_failure_to_the_run() -> None:
    """A warning timestamped outside an action's window attaches to the run (all ownership None);
    one inside the window is owned by the action, and a single drain splits them."""
    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        ru = store.insert_test_run(
            {"run_uuid": "test", "station_name": "X", "operator": "op", "project": "p",
             "uut_type": "u", "uut_serial": "s", "schedule_name": "n", "utc_offset": "+00:00"}
        )["run_uuid"]

        engine = RunEngine(store, str(Path(td) / "schedule.tslua"))  # never run

        class _StubHost:
            def __init__(self, warnings):
                self._w = warnings

            def take_device_warnings(self):
                return self._w

        # One warning inside the window, one outside: the split must put each in its place.
        inside = (0.5, "TCP 127.0.0.1:1111: connection reset by peer")
        outside = (10.0, "TCP 127.0.0.1:2222: connection closed by peer")
        engine._flush_device_warnings(
            _StubHost([inside, outside]), ru, in_window=(0.4, 0.6), action_seq=0
        )
        lines = store.log_lines(ru)
        assert len(lines) == 2, f"the drain must record both warnings: {len(lines)}"
        in_line = next(ln for ln in lines if ln["text"] == inside[1])
        out_line = next(ln for ln in lines if ln["text"] == outside[1])
        assert in_line["level"] == "WARNING" and in_line["source"] == "device"
        assert in_line["action_seq"] == 0, f"an in-window warning is action-owned: {in_line!r}"
        assert in_line["node_seq"] is None and in_line["attempt_no"] is None
        assert out_line["action_seq"] is None and out_line["node_seq"] is None and out_line["attempt_no"] is None, \
            f"a between-action warning is run-level: {out_line!r}"

        # A second drain with everything outside the window must add only run-level lines.
        engine._flush_device_warnings(
            _StubHost([(99.0, "TCP 127.0.0.1:3333: reset")]), ru, in_window=(0.4, 0.6)
        )
        lines = store.log_lines(ru)
        run_level = [ln for ln in lines if ln["text"] == "TCP 127.0.0.1:3333: reset"]
        assert len(run_level) == 1 and run_level[0]["action_seq"] is None
        # A drain with nothing to report is a no-op.
        engine._flush_device_warnings(_StubHost([]), ru, in_window=(0.4, 0.6))
        assert len(store.log_lines(ru)) == 3, f"an empty drain must add no lines: {len(store.log_lines(ru))}"


# --- Seam: a background failure warns without a read and is action-owned ---------------


def test_seam_background_failure_warns_without_a_read_and_is_attributed_to_the_step(app: QCoreApplication) -> None:
    """A peer that resets right after connect is seen by the reader during the step's sleep:
    the step still passes, and exactly one device WARNING (source device) lands owned by the
    step (node_seq 0, attempt 1), even though no ATE.read ran."""
    peer = LoopbackPeer(hangup="reset")  # no payload: the step never reads
    peer.start()
    try:
        store, ru = _run_with_peer(app, _step("""
        ATE.sleep(0.15)
        ATE.report(Result.PASS, "done")"""), peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "done"
    device_warnings = [ln for ln in store.log_lines(ru) if ln["level"] == "WARNING" and ln["source"] == "device"]
    assert len(device_warnings) == 1, f"exactly one device WARNING per failure: {device_warnings!r}"
    text = device_warnings[0]["text"]
    assert f"TCP 127.0.0.1:{peer.port}:" in text, f"the WARNING must name the device: {text!r}"
    assert "reset" in text.lower()
    assert device_warnings[0]["node_seq"] == 0, f"the WARNING is owned by the step: {device_warnings[0]!r}"
    assert device_warnings[0]["attempt_no"] == 1


def test_seam_pretest_failure_warning_is_action_owned(app: QCoreApplication) -> None:
    """A device opened in a Pre-test Action and left open fails while the Action runs: its device
    WARNING is owned by the Action (action_seq 0, node_seq None), not the run or a step."""
    peer = LoopbackPeer(hangup="reset")  # resets on accept; nothing is read
    peer.start()
    try:
        source = f"""
return ScheduleParams("Pretest failure schedule", "a pretest opens a device that resets")
  :setPreTestAction(function()
    local dev = ATE.open(DeviceType.TCP, {{host="127.0.0.1", port={peer.port}, timeout=5, readTerm="", writeTerm=""}})
    ATE.sleep(0.15)
    dev = nil  -- left open; the engine closes it at run end
  end)
"""
        with tempfile.TemporaryDirectory() as td:
            store = RunStore(Path(td) / "ate.db")
            schedule_path = Path(td) / "schedule.tslua"
            schedule_path.write_text(source, encoding="utf-8")
            engine = RunEngine(store, str(schedule_path))
            captured = {}
            engine.run_uuid_reported.connect(lambda u: captured.setdefault("ru", u))
            _drive(engine, app)
            assert captured.get("ru"), "engine never reported a run key"
            ru = captured["ru"]
    finally:
        peer.stop()

    device_warnings = [ln for ln in store.log_lines(ru) if ln["level"] == "WARNING" and ln["source"] == "device"]
    assert len(device_warnings) == 1, f"exactly one device WARNING per failure: {device_warnings!r}"
    text = device_warnings[0]["text"]
    assert f"TCP 127.0.0.1:{peer.port}:" in text and "reset" in text.lower()
    assert device_warnings[0]["action_seq"] == 0, f"the WARNING is owned by the Pre-test Action: {device_warnings[0]!r}"
    assert device_warnings[0]["node_seq"] is None and device_warnings[0]["attempt_no"] is None


# --- Seam: buffered-then-fail, then close + open recovers -------------------------------------


def test_seam_failed_device_serves_buffer_then_fails_and_recovers_on_reopen(app: QCoreApplication) -> None:
    """A peer that sends bytes and then closes is seen by the reader: the first read takes the
    buffered bytes, the next read and the write fail through their onError, and a second open of
    the same peer recovers (the next read takes its buffered bytes). Each failure logs one device
    WARNING, attributed to the step it happened in."""
    peer = LoopbackPeer(payload=b"abc", hangup="close")
    peer.start()
    try:
        source = f"""
return ScheduleParams("Buffer then fail then reopen", "two steps")
  :setSchedule({{
    StepParams("first"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {{host="127.0.0.1", port={peer.port}, timeout=5, readTerm="", writeTerm=""}})
        ATE.sleep(0.15)
        local keep = function(res) return res end
        local r1 = ATE.read(dev, {{timeout = 2}}, keep)
        local r2 = ATE.read(dev, {{timeout = 2}}, keep)
        local w = ATE.write(dev, {{data = "x", timeout = 2}}, keep)
        ATE.close(dev)
        ATE.report(Result.PASS, tostring(r1.count).." "..tostring(r2.err).." "..tostring(w.err))
      end),
    StepParams("second"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {{host="127.0.0.1", port={peer.port}, timeout=5, readTerm="", writeTerm=""}})
        ATE.sleep(0.05)
        local r = ATE.read(dev, {{timeout = 2}})
        ATE.close(dev)
        ATE.report(Result.PASS, r.data.." "..tostring(r.count))
      end),
  }})
"""
        with tempfile.TemporaryDirectory() as td:
            store = RunStore(Path(td) / "ate.db")
            schedule_path = Path(td) / "schedule.tslua"
            schedule_path.write_text(source, encoding="utf-8")
            engine = RunEngine(store, str(schedule_path))
            captured = {}
            engine.run_uuid_reported.connect(lambda u: captured.setdefault("ru", u))
            _drive(engine, app)
            assert captured.get("ru"), "engine never reported a run key"
            ru = captured["ru"]
    finally:
        peer.stop()

    # Step 1: the read takes the buffered bytes; the next read and the write both fail.
    attempt1 = next(a for a in store.attempts(ru) if a["node_seq"] == 0 and a["attempt_no"] == 1)
    assert attempt1["msg"] == "3 connection closed by peer connection closed by peer", \
        f"step 1 must show the buffered count then the failure twice: {attempt1['msg']!r}"

    # Step 2: a fresh open of the same peer recovers; the read takes its buffered bytes.
    attempt2 = next(a for a in store.attempts(ru) if a["node_seq"] == 1 and a["attempt_no"] == 1)
    assert attempt2["msg"] == "abc 3", f"the reopen must recover and read its buffer: {attempt2['msg']!r}"

    device_warnings = [ln for ln in store.log_lines(ru) if ln["level"] == "WARNING" and ln["source"] == "device"]
    assert len(device_warnings) == 2, f"one device WARNING per failure (two failures): {device_warnings!r}"
    assert device_warnings[0]["node_seq"] == 0 and device_warnings[0]["attempt_no"] == 1, \
        f"step 1's failure is owned by step 1: {device_warnings[0]!r}"
    assert device_warnings[1]["node_seq"] == 1 and device_warnings[1]["attempt_no"] == 1, \
        f"step 2's failure is owned by step 2: {device_warnings[1]!r}"


_SMALL_BUF = 4096  # both ends' socket buffers shrink to this, so a 256 KiB write can't fit


def _open_small(peer: "_ManualPeer", timeout: float = 5):
    """Open a TCP device on ``peer`` with a tiny send buffer; return ``(device, peer socket)``."""
    dev = TcpDriver().open(
        {"host": "127.0.0.1", "port": peer.port, "timeout": timeout, "readTerm": "", "writeTerm": ""}
    )
    dev.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _SMALL_BUF)
    return dev, peer.wait_accepted()


def _small_send_buffer(monkeypatch) -> None:
    """Make every TCP device the engine opens use a tiny send buffer (see :func:`_open_small`)."""
    real_open = TcpDriver.open

    def _open(self, params, *args):
        dev = real_open(self, params, *args)
        dev.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _SMALL_BUF)
        return dev

    monkeypatch.setattr(TcpDriver, "open", _open)


def test_write_honours_the_open_timeout_when_the_peer_stops_reading() -> None:
    """A write on a device whose peer stops reading must fail with the open-time timeout, not wait
    forever: the device honours the open timeout and reports the bytes it did send."""
    peer = _ManualPeer(rcvbuf=_SMALL_BUF)
    try:
        dev, _conn = _open_small(peer, timeout=0.3)
        payload = b"\x00" * (256 * 1024)
        start = time.monotonic()
        ok, sent, err = dev.write({"data": payload})  # no per-call timeout: uses the open-time 0.3 s
        elapsed = time.monotonic() - start
        dev.close()
    finally:
        peer.stop()

    assert ok is False
    assert err == "timeout", f"the write must report a timeout, not an error: {err!r}"
    assert 0 < sent < len(payload), f"the write must report the bytes actually sent: {sent}"
    assert 0.25 < elapsed < 1.0, f"the write must honour the 0.3 s timeout, it took {elapsed:.2f}s"


def test_write_per_call_timeout_overrides_the_open_timeout() -> None:
    """A per-call ``timeout = 0.3`` wins over the open-time 5 s: the stalled write returns well
    under 5 s with err = "timeout"."""
    peer = _ManualPeer(rcvbuf=_SMALL_BUF)
    try:
        dev, _conn = _open_small(peer, timeout=5)
        start = time.monotonic()
        ok, sent, err = dev.write({"data": b"\x00" * (256 * 1024), "timeout": 0.3})
        elapsed = time.monotonic() - start
        dev.close()
    finally:
        peer.stop()

    assert (ok, err) == (False, "timeout"), f"the stalled write must time out: {(ok, sent, err)!r}"
    assert 0.25 < elapsed < 1.0, f"the per-call 0.3 s timeout must win over the open 5 s, took {elapsed:.2f}s"


def test_write_timeout_does_not_fail_the_device() -> None:
    """A write timeout is that call's failure only: once the peer drains, a later write and read
    on the same device succeed, and no device warning is recorded (spec: only a connection error
    fails the Device)."""
    peer = _ManualPeer(rcvbuf=_SMALL_BUF)
    try:
        dev, conn = _open_small(peer)
        ok, sent, err = dev.write({"data": b"a" * (256 * 1024), "timeout": 0.3})
        assert (ok, err) == (False, "timeout") and 0 < sent, f"the first write must time out: {(ok, sent, err)!r}"

        # The peer drains exactly what was sent, then the device must work again.
        got = 0
        conn.settimeout(2)
        while got < sent:
            got += len(conn.recv(65536))
        later = dev.write({"data": b"ping", "timeout": 2})
        echoed = b""
        while len(echoed) < 4:
            echoed += conn.recv(4 - len(echoed))
        conn.sendall(b"pong")
        read = dev.read({"size": 4, "timeout": 2})
        warnings = dev.take_warnings()
        dev.close()
    finally:
        peer.stop()

    assert later == (True, 4, None), f"a write after the drained timeout must succeed: {later!r}"
    assert echoed == b"ping"
    assert read == (b"pong", 4, False, None), f"a read after the write timeout must succeed: {read!r}"
    assert warnings == [], f"a write timeout must not fail the device: {warnings!r}"


def test_seam_write_timeout_reports_count_and_err_through_onerror(app: QCoreApplication, monkeypatch) -> None:
    """A write that the peer stops reading fails with {ok = false, count = n, err = 'timeout'}
    through its onError, and the step reports those exact values (n between 0 and the payload).
    The timeout doesn't fail the Device, so no device WARNING is logged."""
    _small_send_buffer(monkeypatch)
    peer = _ManualPeer(rcvbuf=_SMALL_BUF)
    try:
        source = _step(f"""
        local big = string.rep("x", 256*1024)
        local w = ATE.write(dev, {{data = big, timeout = 0.3}}, function(res) return res end)
        ATE.report(Result.PASS, tostring(w.ok).." "..tostring(w.count).." "..tostring(w.err))""")
        store, ru = _run_with_peer(app, source, peer.port)
    finally:
        peer.stop()

    msg = _step_message(store, ru)
    parts = msg.split(" ")
    assert len(parts) == 3 and parts[0] == "false" and parts[2] == "timeout", f"unexpected message: {msg!r}"
    n = int(parts[1])
    assert 0 < n < 256 * 1024, f"count must be the bytes actually sent (0 < n < 256 KiB): {n}"
    assert _device_warnings(store, ru) == [], "a write timeout does not fail the device"


# --- Seam: a write that fails mid-send marks the device failed ---------------------------------


def test_seam_write_failure_wake_read_and_device_failure_is_logged(app: QCoreApplication) -> None:
    """A device whose connection was reset fails a write and a following read with the same error
    through their onError, and logs exactly one device WARNING. The step waits (polling a zero-timeout
    flush, which reports the failure once the reader has seen the RST) before writing, so the write
    always meets the failed device rather than racing the reset."""
    peer = LoopbackPeer(hangup="reset")  # resets on accept
    peer.start()
    try:
        source = _step("""
        local keep = function(res) return res end
        for _ = 1, 200 do
          if not ATE.flush(dev, keep).ok then break end
          ATE.sleep(0.01)
        end
        local w = ATE.write(dev, {data = "x"}, keep)
        local r = ATE.read(dev, {timeout = 2}, keep)
        ATE.report(Result.PASS, tostring(w.ok).." "..tostring(w.err).."|"..tostring(r.err))""")
        store, ru = _run_with_peer(app, source, peer.port)
    finally:
        peer.stop()

    msg = _step_message(store, ru)
    head, rerr = msg.split("|", 1)  # the write err itself contains spaces
    ok, werr = head.split(" ", 1)
    assert ok == "false"
    assert werr and ("reset" in werr.lower() or "pipe" in werr.lower()), f"the write must report the failure: {werr!r}"
    assert rerr == werr, f"the read must report the same failure the write raised: {rerr!r}"
    device_warnings = _device_warnings(store, ru)
    assert len(device_warnings) == 1, f"exactly one device WARNING per failure: {device_warnings!r}"
    assert device_warnings[0]["node_seq"] == 0 and device_warnings[0]["attempt_no"] == 1


def test_send_error_still_fails_the_device() -> None:
    """A real send error (the peer reset the connection) fails the device: one warning, and a
    later read reports the same error. Driven at the unit level by writing until the RST bites."""
    peer = _ManualPeer()
    try:
        dev, _conn = _open_small(peer)
        dev._stop.set()  # park the reader so the write, not the reader, meets the reset
        dev._reader_thread.join(2)
        peer.reset()
        time.sleep(0.05)  # let the RST arrive
        result = dev.write({"data": "x", "timeout": 2})
        if result[0]:  # the first send can land in the buffer before the RST is noticed
            result = dev.write({"data": "x", "timeout": 2})
        read = dev.read({"timeout": 0.1})
        warnings = dev.take_warnings()
        dev.close()
    finally:
        peer.stop()

    assert result[0] is False and result[2] and result[2] != "timeout", f"the send must error: {result!r}"
    assert read[3] == result[2], f"the read must report the write's failure: {read!r}"
    assert len(warnings) == 1, f"a send error fails the device once: {warnings!r}"
