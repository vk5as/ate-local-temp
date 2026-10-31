"""Packet 17a — background reader, receive buffer and ATE.flush.

Seam-1 coverage of the 17a receive path against a loopback TCP peer (no mocks): bytes the peer
sends are buffered in the background from open to close, so a read that is made before the
schedule asks takes what is already there without waiting on the socket; the buffer is capped by
``devices.receiveBufferBytes`` dropping the oldest bytes (a WARNING, source device, and
``overflow = true`` on the next read), ``ATE.flush`` empties it and reports the count, and the
reader thread is gone after close (including the run's leftover-device cleanup). The driver logic
lives in :mod:`ate.devices.tcp`; ``_do_read``/:func:`_do_flush` are the ATE.read/ATE.flush wrappers
behind it, and the LoopbackPeer fixtures exercise the whole stack.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from PyQt6.QtCore import QCoreApplication

from ate.devices.tcp import TcpDriver
from ate.engine.run_engine import RunEngine
from ate.storage.store import RunStore

from ._fixtures import (
    OPEN_TCP,
    LoopbackPeer,
    _drive,
    wait_for_disconnect,
)
from .test_tcp_read_write import _run_with_peer, _step, _step_message


def _open(peer: LoopbackPeer, read_term: str = "", write_term: str = "", limit: int | None = None):
    """Open a TCP device on ``peer`` and wait until the peer has accepted it.

    ``limit`` overrides ``receiveBufferBytes`` (default: the 16 MiB config value) so a small cap
    can be exercised in the unit tests."""
    params = {
        "host": "127.0.0.1",
        "port": peer.port,
        "timeout": 5,
        "readTerm": read_term,
        "writeTerm": write_term,
    }
    dev = TcpDriver().open(params, limit) if limit is not None else TcpDriver().open(params)
    assert peer.wait_for_connection()
    return dev


def _wait_for(dev, predicate, timeout: float = 2.0) -> None:
    """Wait, under the device lock, until ``predicate(dev)`` holds; the reader notifies the
    condition on every append and failure, so this polls the buffer state with a deadline
    instead of sleeping for a guessed time."""
    with dev._cond:
        assert dev._cond.wait_for(lambda: predicate(dev), timeout), "reader never reached the expected state"


class _SockProxy:
    """Stands in for the device socket so a test can change one behaviour (``send``, ``close``,
    ``fileno``) while everything else goes to the real socket."""

    def __init__(self, real, **overrides) -> None:
        self._real = real
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._real, name)


# --- Unit: the reader thread and buffer-first reads ------------------------------------------


def test_bytes_sent_before_first_read_are_buffered() -> None:
    """Data the peer sends before the schedule reads is not lost, and both reads take it from the
    buffer without waiting on the socket (a 5 s timeout must not be waited out)."""
    peer = LoopbackPeer(payload=b"123\n456\n")
    peer.start()
    try:
        dev = _open(peer, read_term="\n")
        start = time.monotonic()
        first = dev.read({"timeout": 5})
        mid = time.monotonic()
        second = dev.read({"timeout": 5})
        dev.close()
    finally:
        peer.stop()

    assert first == (b"123", 3, False, None)
    assert second == (b"456", 3, False, None)
    # both reads were satisfied from the buffer: no socket wait
    assert mid - start < 0.2, f"first read waited {mid - start:.2f}s on the socket"
    assert time.monotonic() - mid < 0.2, f"second read waited {time.monotonic() - mid:.2f}s on the socket"


def test_read_waits_on_condition_only_for_missing_data() -> None:
    """With nothing arriving, a read waits its timeout out (not busy-spinning) and reports
    ``data = nil`` / ``err = "timeout"``; with data, it never waits at all (previous test)."""
    peer = LoopbackPeer()  # sends nothing
    peer.start()
    try:
        dev = _open(peer)
        start = time.monotonic()
        result = dev.read({"timeout": 0.2})
        dev.close()
    finally:
        peer.stop()

    assert result == (None, 0, False, "timeout")
    elapsed = time.monotonic() - start
    assert 0.15 < elapsed < 0.5, f"read neither waited out the 0.2 s timeout nor busy-spun: {elapsed:.2f}s"


def test_bytes_arriving_between_reads_are_buffered() -> None:
    """Bytes the peer sends after the first read are buffered by the reader thread, so the next
    read takes them without waiting on the socket."""
    peer = LoopbackPeer(payload=b"a\n")
    peer.start()
    try:
        dev = _open(peer, read_term="\n")
        first = dev.read({"timeout": 0.5})
        conn = peer.connections[-1]
        conn.sendall(b"b\n")  # sent while the schedule is not reading
        start = time.monotonic()
        second = dev.read({"timeout": 0.2})
        dev.close()
    finally:
        peer.stop()

    assert first == (b"a", 1, False, None)
    assert second == (b"b", 1, False, None)
    assert time.monotonic() - start < 0.2, "second read must take buffered bytes, not wait on the socket"


def test_peer_close_leaves_buffered_bytes_readable_then_failed() -> None:
    """A peer that sends bytes and then closes is seen by the reader thread: the read returns the
    buffered bytes (complete, no error) at once, and the next read returns the failure."""
    peer = LoopbackPeer(payload=b"tail", hangup="close")
    peer.start()
    try:
        dev = _open(peer, read_term="\n")
        start = time.monotonic()
        first = dev.read({"timeout": 2})
        elapsed = time.monotonic() - start
        second = dev.read({"timeout": 2})
        dev.close()
    finally:
        peer.stop()

    assert first == (b"tail", 4, False, None)
    assert elapsed < 1, "EOF must end the read at once, not wait out the timeout"
    assert second == (None, 0, False, "connection closed by peer")


def test_reset_failure_leaves_buffered_bytes_readable_then_failed() -> None:
    """A connection reset seen by the reader thread keeps the already-buffered bytes readable, then
    the next read fails with the reset error (today's failure behaviour)."""
    peer = LoopbackPeer(payload=b"abc", hangup="reset")
    peer.start()
    try:
        dev = _open(peer, read_term="\n")
        assert dev.write({"data": "z"})[0], "the trigger write must reach the peer"  # the device's first byte is when the peer sends its RST
        first = dev.read({"timeout": 2})
        second = dev.read({"timeout": 2})
        dev.close()
    finally:
        peer.stop()

    assert first == (b"abc", 3, False, None)
    assert (second[0], second[1], second[2]) == (None, 0, False)
    assert second[3] and "reset" in second[3].lower()


def test_reader_thread_runs_until_close_and_is_gone_after() -> None:
    """The reader thread lives while the device is open, is joined by close, and close itself is
    prompt; the peer sees the disconnect."""
    peer = LoopbackPeer(payload=b"hi\n")
    peer.start()
    try:
        dev = _open(peer)
        assert dev._reader_thread is not None and dev._reader_thread.is_alive()
        start = time.monotonic()
        dev.close()
        elapsed = time.monotonic() - start
        assert dev._reader_thread is None, "close must join the reader thread"
        assert elapsed < 1.0, f"close took {elapsed:.2f}s"
        assert wait_for_disconnect(peer)
    finally:
        peer.stop()


# --- Unit: the cap, overflow flag, and flush -----------------------------------------------


def test_overflow_drops_oldest_keeps_newest_and_flags_pending() -> None:
    """With a 64-byte cap and 100 bytes arriving, the buffer keeps the newest 64 and the next read
    takes them; take_overflow reports the episode (36 bytes dropped) once, then nothing."""
    peer = LoopbackPeer(payload=b"0123456789" * 10)  # 100 bytes
    peer.start()
    try:
        dev = _open(peer, limit=64)
        _wait_for(dev, lambda d: d._overflow_dropped == 36)  # the whole payload has arrived
        with dev._lock:
            assert len(dev._buffer) == 64, f"buffer holds {len(dev._buffer)} bytes, expected the newest 64"
        # the next read takes the newest 64 bytes (bytes 36..99 of the payload)
        result = dev.read({"size": 64, "timeout": 1})
        assert result == ((b"0123456789" * 10)[36:], 64, False, None), f"read returned {result!r}"
        assert dev.take_overflow() == (True, 36)
        assert dev.take_overflow() == (False, 0), "an episode is taken once"
        dev.close()
    finally:
        peer.stop()


def test_flush_neither_reports_nor_cancels_an_overflow() -> None:
    """A flush after an overflow discards the buffer but leaves the episode for the next read."""
    peer = LoopbackPeer(payload=b"0123456789" * 10)
    peer.start()
    try:
        dev = _open(peer, limit=64)
        _wait_for(dev, lambda d: d._overflow_dropped == 36)
        assert dev.flush() == (True, 64, None)
        assert dev.take_overflow() == (True, 36), "flush must not clear the pending overflow"
        dev.close()
    finally:
        peer.stop()


def test_overflow_flag_is_set_per_episode_and_cleared_by_the_next_read() -> None:
    """A second overflow (new bytes arriving after the first was taken) is a new episode with its
    own dropped count."""
    peer = LoopbackPeer(payload=b"0123456789" * 10)
    peer.start()
    try:
        dev = _open(peer, limit=64)
        _wait_for(dev, lambda d: d._overflow_dropped == 36)
        dev.read({"size": 64, "timeout": 1})
        assert dev.take_overflow() == (True, 36)
        conn = peer.connections[-1]
        conn.sendall(b"abcdefghij" * 10)  # another 100 bytes -> a new overflow episode
        _wait_for(dev, lambda d: d._overflow_dropped == 36)
        result = dev.read({"size": 64, "timeout": 1})
        assert result[0] == (b"abcdefghij" * 10)[36:]
        assert dev.take_overflow() == (True, 36), "a new overflow must re-set the flag"
        dev.close()
    finally:
        peer.stop()


def test_take_overflow_holds_the_lock() -> None:
    """take_overflow reads and resets the episode under the device lock, so it cannot interleave
    with the reader recording a drop: while the lock is held it must not return."""
    peer = LoopbackPeer()
    peer.start()
    try:
        dev = _open(peer, limit=64)
        dev._append(b"x" * 71)  # a real overflow: 7 bytes dropped
        out = []
        with dev._lock:
            t = threading.Thread(target=lambda: out.append(dev.take_overflow()))
            t.start()
            t.join(0.2)
            assert t.is_alive() and not out, "take_overflow returned without taking the lock"
        t.join(1)
        assert out == [(True, 7)]
        dev.close()
    finally:
        peer.stop()


def test_exact_fit_chunk_is_not_an_overflow() -> None:
    """A chunk exactly the size of the cap into an empty buffer drops nothing."""
    peer = LoopbackPeer()
    peer.start()
    try:
        dev = _open(peer, limit=64)
        dev._append(b"x" * 64)
        assert dev.take_overflow() == (False, 0)
        with dev._lock:
            assert bytes(dev._buffer) == b"x" * 64
        dev.close()
    finally:
        peer.stop()


def test_small_chunks_overflow_a_partly_full_buffer() -> None:
    """Chunks each smaller than the cap overflow a partly full buffer: the oldest bytes go, the
    dropped count is the excess."""
    peer = LoopbackPeer()
    peer.start()
    try:
        dev = _open(peer, limit=64)
        dev._append(b"a" * 40)
        assert dev.take_overflow() == (False, 0)
        dev._append(b"b" * 40)
        with dev._lock:
            assert bytes(dev._buffer) == b"a" * 24 + b"b" * 40
        assert dev.take_overflow() == (True, 16)
        dev.close()
    finally:
        peer.stop()


def test_terminator_arriving_at_the_deadline_is_not_returned_in_partial_data() -> None:
    """The timeout branch decides under the lock: bytes arriving the instant the wait gives up
    (simulated by appending as the read leaves its locked block) never leak into a partial read,
    so a terminator is never handed back as data."""
    peer = LoopbackPeer(payload=b"abc")
    peer.start()
    try:
        dev = _open(peer, read_term="\n")
        _wait_for(dev, lambda d: len(d._buffer) == 3)
        real = dev._cond

        class _LateArrival:
            def __enter__(self):
                return real.__enter__()

            def __exit__(self, *exc):
                real.__exit__(*exc)
                dev._append(b"\n")  # the terminator lands right as the read leaves the lock
                return False

            def __getattr__(self, name):
                return getattr(real, name)

        dev._cond = _LateArrival()
        result = dev.read({"timeout": 0.05})
        dev._cond = real
        dev.close()
    finally:
        peer.stop()

    assert result == (b"abc", 3, True, None), f"read returned {result!r}"


def test_write_failure_wakes_a_waiting_read() -> None:
    """A write that fails marks the device failed under the lock and wakes a read already waiting,
    which returns the error at once instead of waiting out its timeout."""
    peer = LoopbackPeer()
    peer.start()
    try:
        dev = _open(peer)

        def _boom(_data):
            raise OSError("boom")

        real = dev.sock
        out = []
        reader = threading.Thread(target=lambda: out.append(dev.read({"timeout": 5})))
        reader.start()
        _wait_for(dev, lambda d: len(d._cond._waiters) == 1)  # the read is waiting on the condition
        dev.sock = _SockProxy(real, send=_boom)
        assert dev.write({"data": "x"}) == (False, 0, "boom")
        reader.join(1)
        dev.sock = real
        assert not reader.is_alive(), "the waiting read was not woken by the write failure"
        assert out == [(None, 0, False, "boom")]
        assert dev.flush() == (False, 0, "boom")
        dev.close()
    finally:
        peer.stop()


def test_close_sets_teardown_before_closing_the_socket() -> None:
    """close stops the reader before it closes the socket, so the reader can never see the
    close as a failure; a clean close records nothing."""
    peer = LoopbackPeer()
    peer.start()
    try:
        dev = _open(peer)
        real = dev.sock
        seen = []

        def _close():
            seen.append((dev._teardown, dev._stop.is_set()))
            real.close()

        dev.sock = _SockProxy(real, close=_close)
        dev.close()
    finally:
        peer.stop()

    assert seen == [(True, True)], f"socket closed with (teardown, stop) = {seen}"
    assert dev._failed is None


def test_close_keeps_a_reader_that_did_not_stop() -> None:
    """If the reader join times out, close keeps the thread reference instead of losing track."""
    peer = LoopbackPeer()
    peer.start()
    try:
        dev = _open(peer)
        real_thread = dev._reader_thread
        dev._stop.set()
        real_thread.join(1)

        class _Stuck:
            def join(self, timeout=None):
                pass

            def is_alive(self):
                return True

        stuck = _Stuck()
        dev._reader_thread = stuck
        dev._stop.clear()
        dev.close()
    finally:
        peer.stop()

    assert dev._reader_thread is stuck


def test_reader_select_error_marks_the_device_failed() -> None:
    """A select error while the device is open is a real failure: the reader records it and wakes
    the waiting read, instead of quietly exiting and leaving reads to time out."""
    peer = LoopbackPeer()
    peer.start()
    try:
        dev = _open(peer)
        real = dev.sock

        def _bad_fileno():
            raise OSError("bad descriptor")

        dev.sock = _SockProxy(real, fileno=_bad_fileno)
        start = time.monotonic()
        result = dev.read({"timeout": 2})
        elapsed = time.monotonic() - start
        dev.sock = real
        dev.close()
    finally:
        peer.stop()

    assert result == (None, 0, False, "bad descriptor")
    assert elapsed < 1


def test_flush_empties_buffer_and_counts_discarded_bytes() -> None:
    """flush removes exactly the buffered bytes and returns (ok, count); the device then has
    nothing left to read."""
    peer = LoopbackPeer(payload=b"x" * 50)
    peer.start()
    try:
        dev = _open(peer)
        _wait_for(dev, lambda d: len(d._buffer) == 50)
        ok, count, err = dev.flush()
        assert (ok, count, err) == (True, 50, None)
        with dev._lock:
            assert len(dev._buffer) == 0
        dev.close()
    finally:
        peer.stop()


def test_flush_on_failed_device_fails_with_the_connection_error() -> None:
    """A device the reader marked failed flushes through the failure, not as an empty success."""
    peer = LoopbackPeer(hangup="reset")
    peer.start()
    try:
        dev = _open(peer)
        assert dev.write({"data": "z"})[0], "the trigger write must reach the peer"  # the peer resets once the device sends this byte
        _wait_for(dev, lambda d: d._failed is not None)  # the reader saw the reset
        ok, count, err = dev.flush()
        dev.close()
    finally:
        peer.stop()

    assert (ok, count) == (False, 0)
    assert err and "reset" in err.lower()


# --- Seam-1: through a driven RunEngine -----------------------------------------------------


def _run_with_peer_limited(app, source: str, port: int, limit: int | None = None):
    """Like _run_with_peer but with a per-device buffer cap set on the engine's host."""
    import tempfile

    captured = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(
            source.replace(LoopbackPeer.PORT_PLACEHOLDER, str(port)), encoding="utf-8",
            newline="\n",
        )
        kwargs = {"receive_buffer_bytes": limit} if limit is not None else {}
        engine = RunEngine(store, str(schedule_path), **kwargs)
        engine.run_uuid_reported.connect(_capture)
        _drive(engine, app)

    assert captured.get("run_uuid") is not None, "engine never reported a run key"
    return store, captured["run_uuid"]


def test_seam_data_sent_before_read_is_not_lost(app: QCoreApplication) -> None:
    """Bytes sent at connect (before the schedule reads) come back whole: the read takes them
    from the buffer, so a big timeout is never waited out."""
    peer = LoopbackPeer(payload=b"before-read")  # no terminator: the read takes all of it
    peer.start()
    try:
        source = _step("""
        local r = ATE.read(dev, {size = 100, timeout = 1})
        ATE.report(Result.PASS, r.data.." "..r.count)""")
        store, ru = _run_with_peer(app, source, peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "before-read 11"


def test_seam_overflow_reports_true_and_logs_one_device_warning(app: QCoreApplication) -> None:
    """With a 64-byte cap and a 100-byte reply, the read returns the newest 64 bytes with
    ``overflow = true`` exactly once, and the worker logs one WARNING (source device)."""
    peer = LoopbackPeer(payload=b"0123456789" * 10)
    peer.start()
    try:
        source = _step("""
        local r = ATE.read(dev, {size = 64, timeout = 1})
        ATE.report(Result.PASS, #r.data.." "..tostring(r.overflow))""")
        store, ru = _run_with_peer_limited(app, source, peer.port, limit=64)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "64 true"
    warnings = [ln for ln in store.log_lines(ru) if ln["level"] == "WARNING" and ln["source"] == "device"]
    assert len(warnings) == 1, f"exactly one device WARNING per overflow, got {len(warnings)}"
    assert "overflow" in warnings[0]["text"].lower()


def test_seam_overflow_is_not_reported_twice_on_the_next_read(app: QCoreApplication) -> None:
    """One overflow episode sets ``overflow = true`` on the next read only: the following read of
    the same buffer reports nothing new, and the log holds one WARNING, not two."""
    peer = LoopbackPeer(payload=b"0123456789" * 16)  # 160 bytes; the buffer keeps the newest 64
    peer.start()
    try:
        source = _step("""
        local r1 = ATE.read(dev, {size = 32, timeout = 1})
        local r2 = ATE.read(dev, {size = 32, timeout = 1})
        ATE.report(Result.PASS, tostring(r1.overflow).." "..tostring(r2.overflow))""")
        store, ru = _run_with_peer_limited(app, source, peer.port, limit=64)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "true nil"
    warnings = [ln for ln in store.log_lines(ru) if ln["level"] == "WARNING" and ln["source"] == "device"]
    assert len(warnings) == 1, f"one WARNING for one episode, got {len(warnings)}"


def test_seam_flush_reports_count_and_empties_buffer(app: QCoreApplication) -> None:
    """ATE.flush returns {ok = true, count = n} for the buffered bytes; the read after it has
    nothing left (timeout with no bytes, through onError)."""
    peer = LoopbackPeer(payload=b"noise")
    peer.start()
    try:
        source = _step("""
        local f, total = nil, 0
        for _ = 1, 200 do  -- poll the buffer (2 s deadline) until the 5 bytes have arrived
          f = ATE.flush(dev)
          total = total + f.count
          if total >= 5 then break end
          ATE.sleep(0.01)
        end
        local r = ATE.read(dev, {timeout = 0.2}, function(res) return res end)
        ATE.report(Result.PASS, tostring(f.ok).." "..tostring(total).." "..tostring(f.err)
          .."|"..tostring(r.data).." "..tostring(r.count).." "..tostring(r.err))""")
        store, ru = _run_with_peer_limited(app, source, peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "true 5 nil|nil 0 timeout"


def test_seam_flush_on_closed_device_fails_through_onerror(app: QCoreApplication) -> None:
    """A flush after ATE.close fails with err = "device closed" (through onError, like a read on a
    closed device)."""
    peer = LoopbackPeer(payload=b"x")
    peer.start()
    try:
        source = _step("""
        ATE.close(dev)
        local f = ATE.flush(dev, function(res) return res end)
        ATE.report(Result.PASS, tostring(f.ok).." "..tostring(f.count).." "..tostring(f.err))""")
        store, ru = _run_with_peer_limited(app, source, peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "false 0 device closed"


def test_seam_no_reader_thread_left_after_run_cleanup(app: QCoreApplication) -> None:
    """A run that leaves its device open gets it closed at run end by the engine, and the reader
    thread must be gone afterwards (no thread survives the run)."""
    peer = LoopbackPeer(payload=b"hi")
    peer.start()
    try:
        store, ru = _run_with_peer(app, OPEN_TCP, peer.port)
    finally:
        peer.stop()

    assert wait_for_disconnect(peer)
    leftovers = [t for t in threading.enumerate() if t.name == "ate-tcp-reader" and t.is_alive()]
    assert not leftovers, f"reader threads survived the run: {[t.name for t in leftovers]}"


def test_seam_existing_behaviour_unchanged(app: QCoreApplication) -> None:
    """A normal read with no overflow still reports ``overflow`` as nil (the key is absent), and
    the run's log has no device warnings."""
    peer = LoopbackPeer(payload=b"ok\n")
    peer.start()
    try:
        source = _step("""
        local r = ATE.read(dev, {timeout = 1})
        ATE.report(Result.PASS, tostring(r.overflow))""")
        store, ru = _run_with_peer_limited(app, source, peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "nil"
    warnings = [ln for ln in store.log_lines(ru) if ln["level"] == "WARNING" and ln["source"] == "device"]
    assert warnings == []
