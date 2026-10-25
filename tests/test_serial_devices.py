"""Packet 18a — SERIAL Devices: pyserial ports behind the same ATE API as TCP.

Seam-1 coverage against a loopback TCP peer reached through pyserial's ``socket://`` URL handler
(no mocks): ``open`` takes every field (port, baud, dataBits, stopBits, parity, flowControl,
timeout, readTerm, writeTerm) and a bad one raises naming the field before anything is opened; the
background reader, the capped buffer, the overflow and the failure/warning logic are the shared
streaming device in :mod:`ate.devices.stream`, so a reply that arrives before the schedule reads it
is not lost, ``readTerm``/``size``/``timeout`` still override per call, and a peer that hangs up
fails the device once (one WARNING, buffered bytes readable, then read and write fail through
``onError``).

pyserial 3.5 behaviour the driver leans on: the socket:// handler's ``read`` returns whatever
arrived and stops at its timeout, so ``b""`` means "nothing yet", not end of stream - when the peer
really goes away the handler raises ``SerialException('socket disconnected')``. Its ``write`` raises
``SerialTimeoutException`` without saying how many bytes got out, so writes go out in pieces and
``count`` counts the pieces that made it out whole.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest
import serial
from PyQt6.QtCore import QCoreApplication

from ate.devices.serial import SerialDriver

from ._fixtures import LoopbackPeer, wait_for_disconnect
from .test_device_onerror import _dead_port
from .test_receive_buffer import _run_with_peer_limited
from .test_tcp_read_write import _run_with_peer, _step_message


def _params(peer: LoopbackPeer, read_term: str = "", write_term: str = "") -> dict:
    """The open params for a SERIAL device on ``peer``, as a Python dict (the enum fields are the
    same strings the Lua enums hold)."""
    return {
        "port": f"socket://127.0.0.1:{peer.port}",
        "baud": 9600,
        "dataBits": 8,
        "stopBits": 1,
        "parity": "NONE",
        "flowControl": "NONE",
        "timeout": 5,
        "readTerm": read_term,
        "writeTerm": write_term,
    }


def _open(peer: LoopbackPeer, read_term: str = "", write_term: str = "", limit: int | None = None):
    """Open a SERIAL device on ``peer`` (driver level) and wait until the peer accepted it."""
    params = _params(peer, read_term, write_term)
    dev = SerialDriver().open(params, limit) if limit is not None else SerialDriver().open(params)
    assert peer.wait_for_connection()
    return dev


def _wait_for(dev, predicate, timeout: float = 2.0) -> None:
    """Wait, under the device lock, until ``predicate(dev)`` holds (the reader notifies on every
    append and failure)."""
    with dev._cond:
        assert dev._cond.wait_for(lambda: predicate(dev), timeout), "device never reached the expected state"


def _lua_term(term: str) -> str:
    """``term`` as the body of a Lua string literal, so a real newline in a terminator stays one
    line of source: Lua reads ``\\n`` back as the newline the schedule meant."""
    return term.encode("unicode_escape").decode("ascii").replace('"', '\\"')


def _step(body: str, read_term: str = "", write_term: str = "") -> str:
    """A one-step schedule that opens a SERIAL device on the peer as ``dev`` and runs ``body``."""
    open_call = (
        'ATE.open(DeviceType.SERIAL, {port="socket://127.0.0.1:__PORT__", baud=9600, dataBits=8, '
        f'stopBits=1, parity=Parity.NONE, flowControl=FlowControl.NONE, timeout=5, '
        f'readTerm="{_lua_term(read_term)}", writeTerm="{_lua_term(write_term)}"}})'
    )
    return f"""return ScheduleParams("x") :setSchedule({{
    StepParams("io"):setStepNo("1.")
      :setAction(function()
        local dev = {open_call}
{body}
        ATE.close(dev)
      end),
  }})"""


class _StuckPeer:
    """A loopback listener that accepts but never reads, with a tiny receive buffer: a large write
    to it stalls, which is how the write-timeout path is exercised."""

    def __init__(self, rcvbuf: int = 2048) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self.conn: socket.socket | None = None
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        try:
            self.conn, _ = self._listener.accept()
        except OSError:
            return

    def stop(self) -> None:
        self._listener.close()
        if self.conn is not None:
            self.conn.close()


# --- driver level: open fields, enum mapping, buffering, failures ------------------------------


@pytest.mark.parametrize(
    "bad, expected",
    [
        ({"buad": 9600}, "unknown field 'buad'"),  # a typo, not a Device failure
        ({"baud": None}, "missing required field 'baud'"),
        ({"baud": 0}, "field 'baud' must be a positive int"),
        ({"baud": -9600}, "field 'baud' must be a positive int"),
        ({"dataBits": 4}, "field 'dataBits' must be one of 5, 6, 7, 8"),
        ({"dataBits": 9}, "field 'dataBits' must be one of 5, 6, 7, 8"),
        ({"stopBits": 3}, "field 'stopBits' must be one of 1, 1.5, 2"),
        ({"stopBits": 0.5}, "field 'stopBits' must be one of 1, 1.5, 2"),
        ({"parity": "X"}, "field 'parity' must be one of NONE, EVEN, ODD, MARK, SPACE"),
        ({"flowControl": "X"}, "field 'flowControl' must be one of NONE, RTSCTS, XONXOFF, DSRDTR"),
        ({"timeout": "5"}, "field 'timeout' must be one of int, float"),
    ],
)
def test_open_rejects_a_bad_field_naming_it_before_connecting(bad: dict, expected: str) -> None:
    """Every field is required, the ranges and the enums are checked, and the error names the field
    - all before anything is opened, so no peer is ever touched."""
    peer = LoopbackPeer()
    peer.start()
    try:
        params = _params(peer)
        for field, value in bad.items():
            if value is None:
                params.pop(field)
            else:
                params[field] = value
        with pytest.raises(ValueError, match=expected):
            SerialDriver().open(params)
        assert peer.connections == [], "validation must trip before the port is opened"
    finally:
        peer.stop()


def test_every_enum_value_maps_onto_pyserial() -> None:
    """The Parity/FlowControl strings map to pyserial's constants and flags, and dataBits/stopBits
    to bytesize/stopbits; the reader gets its short poll timeout, not the open-time one."""
    peer = LoopbackPeer()
    peer.start()
    try:
        for parity, flag in (("NONE", serial.PARITY_NONE), ("EVEN", serial.PARITY_EVEN),
                             ("ODD", serial.PARITY_ODD), ("MARK", serial.PARITY_MARK),
                             ("SPACE", serial.PARITY_SPACE)):
            params = _params(peer)
            params["parity"] = parity
            dev = SerialDriver().open(params)
            assert dev.ser.parity == flag, f"{parity} did not map to pyserial"
            dev.close()
        for flow, (rtscts, xonxoff, dsrdtr) in (("NONE", (False, False, False)),
                                               ("RTSCTS", (True, False, False)),
                                               ("XONXOFF", (False, True, False)),
                                               ("DSRDTR", (False, False, True))):
            params = _params(peer)
            params["flowControl"] = flow
            dev = SerialDriver().open(params)
            assert (dev.ser.rtscts, dev.ser.xonxoff, dev.ser.dsrdtr) == (rtscts, xonxoff, dsrdtr), f"{flow} did not map"
            dev.close()
        for bits in (5, 6, 7, 8):
            params = _params(peer)
            params["dataBits"] = bits
            dev = SerialDriver().open(params)
            assert dev.ser.bytesize == bits
            dev.close()
        for stop, flag in ((1, serial.STOPBITS_ONE), (1.5, serial.STOPBITS_ONE_POINT_FIVE),
                           (2, serial.STOPBITS_TWO)):
            params = _params(peer)
            params["stopBits"] = stop
            dev = SerialDriver().open(params)
            assert dev.ser.stopbits == flag, f"stopBits {stop} did not map"
            dev.close()
        dev = _open(peer)
        assert dev.ser.baudrate == 9600
        assert dev.ser.timeout == 0.05, "the reader polls the port at 50 ms so close() stays prompt"
        assert dev.describe() == f"SERIAL socket://127.0.0.1:{peer.port}"
        dev.close()
    finally:
        peer.stop()


def test_bytes_are_buffered_before_the_read_and_a_write_appends_its_terminator() -> None:
    """The reader drains the port from open onward, so a read right after open finds the peer's
    payload without waiting; a write gets ``writeTerm`` appended and reports the bytes sent."""
    peer = LoopbackPeer(payload=b"OK\n")
    peer.start()
    try:
        dev = _open(peer, read_term="\n", write_term="\n")
        _wait_for(dev, lambda d: len(d._buffer) >= 3)
        assert dev.read({"timeout": 1}) == (b"OK", 2, False, None)
        ok, count, err = dev.write({"data": "PING"})
        assert (ok, count, err) == (True, 5, None), f"writeTerm must be counted: {(ok, count, err)!r}"
        dev.close()  # the peer only records what it saw once the connection ends
        assert wait_for_disconnect(peer)
        assert b"PING\n" in b"".join(peer.received.values()), "the peer never saw the write"
    finally:
        peer.stop()


def test_read_honours_per_call_read_term_size_and_timeout() -> None:
    """``readTerm``, ``size`` and ``timeout`` override the open-time configuration per call: the
    terminator is stripped, ``size`` caps the take, and an empty buffer times out with err."""
    peer = LoopbackPeer(payload=b"0123456789")
    peer.start()
    try:
        dev = _open(peer)
        _wait_for(dev, lambda d: len(d._buffer) >= 10)
        assert dev.read({"size": 4, "timeout": 1}) == (b"0123", 4, False, None)
        assert dev.read({"readTerm": "6", "timeout": 1}) == (b"45", 2, False, None), "the terminator is stripped"
        started = time.monotonic()
        assert dev.read({"timeout": 0.2}) == (b"789", 3, True, None), "bytes without a terminator are partial"
        assert dev.read({"timeout": 0.2}) == (None, 0, False, "timeout")
        assert time.monotonic() - started < 1.0, "the per-call timeout, not the open-time 5 s, was used"
        dev.close()
    finally:
        peer.stop()


def test_flush_discards_the_buffer_and_reports_the_count() -> None:
    peer = LoopbackPeer(payload=b"noise")
    peer.start()
    try:
        dev = _open(peer)
        _wait_for(dev, lambda d: len(d._buffer) >= 5)
        assert dev.flush() == (True, 5, None)
        assert dev.flush() == (True, 0, None), "the buffer is empty after a flush"
        dev.close()
    finally:
        peer.stop()


def test_a_small_buffer_keeps_the_newest_bytes_and_flags_one_overflow() -> None:
    peer = LoopbackPeer(payload=b"0123456789" * 10)  # 100 bytes into a 64-byte cap
    peer.start()
    try:
        dev = _open(peer, limit=64)
        _wait_for(dev, lambda d: d._overflow_pending)
        data, count, partial, err = dev.read({"size": 64, "timeout": 1})
        assert (count, partial, err) == (64, False, None)
        assert data == (b"0123456789" * 10)[-64:], "the oldest 36 bytes were dropped"
        overflow, dropped = dev.take_overflow()
        assert overflow and dropped == 36, f"the drop is counted: {overflow}, {dropped}"
        assert dev.take_overflow() == (False, 0), "one episode, reported once"
        dev.close()
    finally:
        peer.stop()


def test_peer_hang_up_fails_the_device_once_and_later_calls_report_it() -> None:
    """pyserial's socket handler raises SerialException('socket disconnected') when the peer closes:
    the reader records exactly one warning, the buffered bytes are still readable, and every later
    read and write returns that error."""
    peer = LoopbackPeer(payload=b"buffered", hangup="close")
    peer.start()
    try:
        dev = _open(peer)
        _wait_for(dev, lambda d: d._failed is not None)
        assert dev.read({"timeout": 1}) == (b"buffered", 8, False, None), "buffered bytes survive the failure"
        failed = dev.read({"timeout": 1})
        assert failed[3] and "disconnect" in failed[3].lower(), f"the next read carries the failure: {failed!r}"
        assert dev.write({"data": "x", "timeout": 1})[:2] == (False, 0)
        warnings = dev.take_warnings()
        assert len(warnings) == 1, f"exactly one warning per failure, got {warnings!r}"
        assert warnings[0][1].startswith(f"SERIAL socket://127.0.0.1:{peer.port}:")
        dev.close()
    finally:
        peer.stop()


def test_an_unexpected_reader_error_still_fails_the_device() -> None:
    """An exception the transport didn't translate must not kill the reader silently: the device is
    marked failed with one warning, so reads fail instead of timing out forever."""
    peer = LoopbackPeer()
    peer.start()
    try:
        dev = _open(peer)

        def boom():
            raise RuntimeError("boom")

        dev._recv = boom  # the reader looks _recv up on each poll
        _wait_for(dev, lambda d: d._failed is not None)
        assert dev._failed == "RuntimeError: boom"
        assert dev.read({"timeout": 1})[3] == "RuntimeError: boom"
        assert len(dev.take_warnings()) == 1
        dev.close()
    finally:
        peer.stop()


def test_a_write_that_cannot_get_out_times_out_that_call_only() -> None:
    """pyserial's write raises SerialTimeoutException without reporting how far it got, so the
    payload is sent in pieces and ``count`` is the pieces that made it out whole; the device itself
    stays usable."""
    peer = _StuckPeer()
    dev = SerialDriver().open(_params(peer))
    try:
        payload = b"x" * 1_500_000  # far more than the loopback connection can absorb in 0.5 s
        started = time.monotonic()
        ok, count, err = dev.write({"data": payload, "timeout": 0.5})
        elapsed = time.monotonic() - started
        assert (ok, err) == (False, "timeout"), f"a stalled write reports a timeout: {(ok, count, err)!r}"
        assert 0 < count < len(payload), f"count is the bytes that made it out: {count}"
        assert elapsed < 1.5, f"the write waited the call's timeout, not forever: {elapsed}s"
        assert dev._failed is None, "a write timeout is this call's failure, not the device's"
    finally:
        dev.close()
        peer.stop()


# --- Seam 1: SERIAL Devices through a driven RunEngine -----------------------------------------


def test_open_with_every_field_then_write_and_read(app: QCoreApplication) -> None:
    """A schedule opens a SERIAL Device with every field, writes a terminated command, and reads the
    reply back: ``count`` is the bytes sent (terminator included), ``data`` is the reply with its
    terminator stripped."""
    peer = LoopbackPeer(payload=b"OK\n")
    peer.start()
    try:
        store, ru = _run_with_peer(app, _step("""
        local w = ATE.write(dev, {data = "PING"})
        local r = ATE.read(dev, {timeout = 1})
        ATE.report(Result.PASS, w.count.." "..tostring(w.ok).." "..r.data.." "..r.count.." "
          ..tostring(r.partial))""",
                                            read_term="\n", write_term="\n"), peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "5 true OK 2 false"
    deadline = time.time() + 2
    while time.time() < deadline and b"PING\n" not in b"".join(peer.received.values()):
        time.sleep(0.02)
    assert b"PING\n" in b"".join(peer.received.values()), "the peer never saw the write"


def test_data_that_arrives_before_the_read_is_buffered(app: QCoreApplication) -> None:
    # The peer sends and hangs up before the read: only the background buffer still has the bytes
    # (a read straight off pyserial's socket:// port would hit the EOF and drop them).
    peer = LoopbackPeer(payload=b"early", hangup="close")
    peer.start()
    try:
        store, ru = _run_with_peer(app, _step("""
        ATE.sleep(0.3)  -- let the peer hang up first
        local r = ATE.read(dev, {size = 100, timeout = 1})
        ATE.report(Result.PASS, r.data.." "..r.count)"""), peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "early 5"


def test_read_term_size_and_timeout_can_be_overridden_per_call(app: QCoreApplication) -> None:
    peer = LoopbackPeer(payload=b"0123456789")
    peer.start()
    try:
        store, ru = _run_with_peer(app, _step("""
        local a = ATE.read(dev, {size = 4, timeout = 1})
        local b = ATE.read(dev, {readTerm = "6", timeout = 1})
        local c = ATE.read(dev, {timeout = 0.3})
        local d = ATE.read(dev, {timeout = 0.3}, function() return "nothing" end)
        ATE.report(Result.PASS, a.data.." "..a.count.." "..b.data.." "..c.count.." "
          ..tostring(c.partial).." "..d)"""), peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "0123 4 45 3 true nothing"


def test_a_read_that_times_out_after_some_bytes_is_partial_and_warns(app: QCoreApplication) -> None:
    peer = LoopbackPeer(payload=b"abc")  # no terminator: the read stalls on the timeout
    peer.start()
    try:
        store, ru = _run_with_peer(app, _step("""
        local r = ATE.read(dev, {readTerm = "\\n", timeout = 0.3})
        ATE.report(Result.PASS, r.count.." "..tostring(r.partial).." "..tostring(r.err))"""), peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "3 true nil"
    assert any(ln["level"] == "WARNING" and "timed out" in ln["text"] for ln in store.log_lines(ru))


def test_flush_empties_the_buffer(app: QCoreApplication) -> None:
    peer = LoopbackPeer(payload=b"noise")
    peer.start()
    try:
        store, ru = _run_with_peer(app, _step("""
        ATE.sleep(0.2)  -- the background reader has the peer's payload buffered by now
        local f = ATE.flush(dev)
        local r = ATE.read(dev, {timeout = 0.3}, function() return "nothing" end)
        ATE.report(Result.PASS, f.count.." "..tostring(f.ok).." "..r)"""), peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "5 true nothing"


def test_a_small_receive_buffer_overflows_once_with_a_warning(app: QCoreApplication) -> None:
    peer = LoopbackPeer(payload=b"0123456789" * 10)  # 100 bytes into a 64-byte cap
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
    assert "SERIAL" in warnings[0]["text"] and "overflow" in warnings[0]["text"].lower()


def test_a_peer_that_hangs_up_fails_the_device_once_and_later_calls_error(app: QCoreApplication) -> None:
    """One WARNING (source device) at the hang-up, the buffered reply is still readable, and every
    read and write after it fails with that error through onError."""
    peer = LoopbackPeer(payload=b"buffered\n", hangup="close")
    peer.start()
    try:
        source = _step("""
        local keep = function(res) return res.err end
        local r1 = ATE.read(dev, {readTerm = "\\n", timeout = 1})
        local r2 = ATE.read(dev, {timeout = 1}, keep)
        local w = ATE.write(dev, {data = "x"}, keep)
        ATE.report(Result.PASS, r1.count.."|"..r2.."|"..w)""")
        store, ru = _run_with_peer(app, source, peer.port)
    finally:
        peer.stop()

    msg = _step_message(store, ru)
    count, r2, w = msg.split("|", 2)
    assert count == "8", f"the buffered reply is readable after the failure: {msg!r}"
    assert "disconnect" in r2.lower() and w == r2, f"read and write carry the same failure: {msg!r}"
    warnings = [ln for ln in store.log_lines(ru) if ln["level"] == "WARNING" and ln["source"] == "device"]
    assert len(warnings) == 1, f"one warning for one failure, got {len(warnings)}"
    assert f"SERIAL socket://127.0.0.1:{peer.port}" in warnings[0]["text"]
    assert wait_for_disconnect(peer)


def test_close_then_open_again_gives_a_new_device(app: QCoreApplication) -> None:
    """ATE never reconnects on its own: the schedule closes and reopens with the same params and
    gets a fresh, working device."""
    peer = LoopbackPeer()
    peer.start()
    try:
        source = _step("""
        ATE.close(dev)
        local dev2 = ATE.open(DeviceType.SERIAL, {port="socket://127.0.0.1:__PORT__", baud=115200,
          dataBits=7, stopBits=2, parity=Parity.EVEN, flowControl=FlowControl.RTSCTS, timeout=5,
          readTerm="", writeTerm=""})
        local w = ATE.write(dev2, {data = "again"})
        ATE.report(Result.PASS, w.count.." "..tostring(w.ok))""")
        store, ru = _run_with_peer(app, source, peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "5 true"
    assert len(peer.connections) == 2, "the reopen opened a second connection"
    assert b"again" in b"".join(peer.received.values())


def test_open_on_a_port_that_is_not_there_is_a_device_failure(app: QCoreApplication) -> None:
    """A port pyserial cannot open is handled like a refused TCP connect: open's onError gets
    ``{ok = false, err}`` and open returns whatever the handler gives back."""
    port = _dead_port()
    # open_call is deliberately left open: the handler argument below closes the call.
    open_call = (f'ATE.open(DeviceType.SERIAL, {{port="socket://127.0.0.1:{port}", baud=9600, dataBits=8, '
                 'stopBits=1, parity=Parity.NONE, flowControl=FlowControl.NONE, timeout=1, '
                 'readTerm="", writeTerm=""}')
    store, ru = _run_with_peer(app, f"""return ScheduleParams("x") :setSchedule({{
    StepParams("io"):setStepNo("1.")
      :setAction(function()
        local calls = {{}}
        local dev = {open_call}, function(res, args) calls[#calls + 1] = tostring(res.ok).."|"..args.port; return "no device" end)
        ATE.report(Result.PASS, dev.." "..table.concat(calls, ","))
      end),
  }})""", port)

    assert _step_message(store, ru) == f"no device false|socket://127.0.0.1:{port}"


def test_open_failure_without_a_handler_errors_the_step_naming_the_port(app: QCoreApplication) -> None:
    port = _dead_port()
    open_call = (f'ATE.open(DeviceType.SERIAL, {{port="socket://127.0.0.1:{port}", baud=9600, dataBits=8, '
                 'stopBits=1, parity=Parity.NONE, flowControl=FlowControl.NONE, timeout=1, '
                 'readTerm="", writeTerm=""})')
    store, ru = _run_with_peer(app, f"""return ScheduleParams("x") :setSchedule({{
    StepParams("io"):setStepNo("1.")
      :setAction(function()
        {open_call}
        ATE.report(Result.PASS, "carried on")
      end),
  }})""", port)

    node = next(n for n in store.nodes(ru) if n["kind"] == "step")
    assert node["result"] == "ERROR"
    (attempt,) = store.attempts(ru)
    assert f"ATE.open: SERIAL socket://127.0.0.1:{port}: " in attempt["msg"], attempt["msg"]
    assert "could not open port" in attempt["msg"].lower()


_BAD_OPEN_FIELDS = [
    ({"buad": 9600}, "unknown field 'buad'"),  # a typo, not a Device failure
    ({"baud": None}, "missing required field 'baud'"),
    ({"port": None}, "missing required field 'port'"),
    ({"baud": 0}, "field 'baud' must be a positive int"),
    ({"dataBits": 4}, "field 'dataBits' must be one of 5, 6, 7, 8"),
    ({"dataBits": 9}, "field 'dataBits' must be one of 5, 6, 7, 8"),
    ({"stopBits": 3}, "field 'stopBits' must be one of 1, 1.5, 2"),
    ({"parity": "X"}, "field 'parity' must be one of NONE, EVEN, ODD, MARK, SPACE"),
    ({"flowControl": "X"}, "field 'flowControl' must be one of NONE, RTSCTS, XONXOFF, DSRDTR"),
    ({"timeout": "5s"}, "field 'timeout' must be one of int, float"),
]


@pytest.mark.parametrize("change, expected", _BAD_OPEN_FIELDS)
def test_a_bad_open_field_raises_naming_it_and_never_reaches_onerror(app: QCoreApplication,
                                                                    change: dict, expected: str) -> None:
    """The schedule-bug path: the raise names the field, neither open's nor the call's onError runs,
    and the port is never opened."""
    params = {"port": f"socket://127.0.0.1:__PORT__", "baud": 9600, "dataBits": 8, "stopBits": 1,
              "parity": "NONE", "flowControl": "NONE", "timeout": 5, "readTerm": "", "writeTerm": ""}
    for field, value in change.items():
        if value is None:
            params.pop(field)
        else:
            params[field] = value
    lua = ", ".join(f"{field}={value!r}" for field, value in params.items())
    peer = LoopbackPeer()
    peer.start()
    try:
        store, ru = _run_with_peer(app, f"""return ScheduleParams("x") :setSchedule({{
    StepParams("io"):setStepNo("1.")
      :setAction(function()
        local calls = {{}}
        local mark = function() calls[#calls + 1] = "called"; return "handled" end
        local msg = select(2, ATE.open(DeviceType.SERIAL, {{{lua}}}, mark))
        ATE.report(Result.PASS, #calls.." "..msg)
      end),
  }})""", peer.port)
    finally:
        peer.stop()

    count, *msg = _step_message(store, ru).split(" ", 1)
    assert count == "0", f"onError must never run for a schedule bug: {count} calls"
    assert expected in msg[0], f"the error must name the field: {msg[0]!r}"
    assert peer.connections == [], "a bad field trips before anything is opened"
