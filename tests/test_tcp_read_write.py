"""Packet 16b — TCP `read` and `write`.

Seam-1 coverage of raw byte I/O with terminators and per-call overrides, driving a real LuaHost +
QThread RunEngine against a loopback peer (no mocks). The peer is a controlled listener on 127.0.0.1
whose accepted connections record the exact bytes each side sends, so every result shape — complete
read, partial-on-timeout, timeout-with-no-bytes, other failure, write success/failure, terminator
stripping, size limits, per-call overrides and bad params — is verified against real sockets.

The read/write driver logic lives in :mod:`ate.devices.tcp`; :func:`_do_read`/:func:`_do_write` are the
ATE.read/ATE.write wrappers behind it; ``LoopbackPeer`` + ``_drive`` exercise the whole stack."""

import re
import select
import socket
import time
from pathlib import Path

from PyQt6.QtCore import QCoreApplication

from ate.devices import TcpDriver
from ate.engine.run_engine import RunEngine
from ate.storage.store import RunStore

from ._fixtures import (
    LoopbackPeer,
    _drive,
    wait_for_disconnect,
)
from tests._fixtures import scratch_directory


def _open(peer: LoopbackPeer, read_term: str = "", write_term: str = ""):
    """Open a TCP device on ``peer`` and wait until the peer has accepted it."""
    dev = TcpDriver().open(
        {"host": "127.0.0.1", "port": peer.port, "timeout": 5, "readTerm": read_term, "writeTerm": write_term}
    )
    assert peer.wait_for_connection()
    return dev


# --- Unit: the driver over a real socket ---------------------------------------------------


def test_write_appends_term_and_transmits_raw_bytes_including_0xFF() -> None:
    """A write appends ``writeTerm`` and transmits the raw bytes verbatim — including a 0xFF byte."""
    peer = LoopbackPeer()
    peer.start()
    try:
        dev = _open(peer)
        conn = peer.connections[-1]
        ok, count, err = dev.write({"data": b"Hi\xff", "writeTerm": b"\n"})
        dev.close()
    finally:
        peer.stop()

    assert (ok, count, err) == (True, 4, None)  # 3 data bytes + "\n"; count includes the terminator
    assert wait_for_disconnect(peer)
    assert peer.received[conn] == b"Hi\xff\n"


def test_read_strips_readTerm_and_returns_raw_bytes() -> None:
    """A read strips ``readTerm`` and returns the raw bytes that preceded it."""
    peer = LoopbackPeer(payload=b"Hello\n")
    peer.start()
    try:
        dev = _open(peer, read_term="\n")
        result = dev.read({"size": 10})
        dev.close()
    finally:
        peer.stop()

    assert result == (b"Hello", 5, False, None)


def test_read_stops_at_size() -> None:
    """A read with a ``size`` limit returns exactly that many bytes and marks it complete."""
    peer = LoopbackPeer(payload=b"0123456789ABCDEF")
    peer.start()
    try:
        dev = _open(peer)
        result = dev.read({"size": 5})
        dev.close()
    finally:
        peer.stop()

    assert result == (b"01234", 5, False, None)


def test_partial_read_on_timeout_is_partial_and_carries_bytes() -> None:
    """A read that grabs some bytes then times out returns them with ``partial = true`` and no error."""
    peer = LoopbackPeer(payload=b"abc")  # only three bytes; the rest never arrives
    peer.start()
    try:
        dev = _open(peer)
        result = dev.read({"size": 10, "timeout": 0.2})
        dev.close()
    finally:
        peer.stop()

    assert result == (b"abc", 3, True, None)


def test_two_reads_split_buffered_terminators() -> None:
    """Bytes past a terminator are kept, and a terminator already buffered completes the next read
    without waiting on the socket."""
    peer = LoopbackPeer(payload=b"a\nb\n")
    peer.start()
    try:
        dev = _open(peer, read_term="\n")
        first = dev.read({"timeout": 0.5})
        start = time.monotonic()
        second = dev.read({"timeout": 0.5})
        elapsed = time.monotonic() - start
        dev.close()
    finally:
        peer.stop()

    assert first == (b"a", 1, False, None)
    assert second == (b"b", 1, False, None)
    assert elapsed < 0.2, f"second read waited {elapsed:.2f}s on the socket despite a buffered terminator"


def test_partial_read_bytes_not_returned_again() -> None:
    """The bytes a partial read returns are consumed: the next read times out with no bytes."""
    peer = LoopbackPeer(payload=b"abc")
    peer.start()
    try:
        dev = _open(peer, read_term="\n")
        first = dev.read({"timeout": 0.2})
        second = dev.read({"timeout": 0.1})
        dev.close()
    finally:
        peer.stop()

    assert first == (b"abc", 3, True, None)  # no terminator arrives -> partial
    assert second == (None, 0, False, "timeout")


def test_size_limit_with_terminator_beyond_keeps_rest() -> None:
    """``size`` caps the read even when the terminator lies past it; the rest stays for the next read."""
    peer = LoopbackPeer(payload=b"0123456789abc\n")
    peer.start()
    try:
        dev = _open(peer, read_term="\n")
        first = dev.read({"size": 4})
        second = dev.read({"timeout": 0.5})
        dev.close()
    finally:
        peer.stop()

    assert first == (b"0123", 4, False, None)
    assert second == (b"456789abc", 9, False, None)


def test_terminator_within_size_ends_the_read() -> None:
    """A terminator inside the ``size`` limit ends the read there; the bytes after it are kept."""
    peer = LoopbackPeer(payload=b"ab\ncdefgh")
    peer.start()
    try:
        dev = _open(peer, read_term="\n")
        first = dev.read({"size": 4})
        second = dev.read({"size": 4})
        dev.close()
    finally:
        peer.stop()

    assert first == (b"ab", 2, False, None)
    assert second == (b"cdef", 4, False, None)


def test_bytes_then_peer_close_is_complete_then_next_read_fails() -> None:
    """Bytes followed by the peer closing are a complete read (no timeout, no partial); the next read
    returns the failure with ``data = nil``."""
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


def test_read_connection_reset_is_other_failure() -> None:
    """A connection reset is a read 'other failure': ``data = nil``, ``count = 0``, ``err = <message>``."""
    peer = LoopbackPeer(hangup="reset")  # the peer resets as soon as the device sends a byte
    peer.start()
    try:
        dev = _open(peer)
        assert dev.write({"data": b"z"})[0], "the trigger write must reach the peer"  # the byte that triggers the reset
        data, count, partial, err = dev.read({"timeout": 2})
        dev.close()
    finally:
        peer.stop()

    assert (data, count, partial) == (None, 0, False)
    assert err and "reset" in err.lower()


def test_write_after_connection_reset_fails_with_message() -> None:
    """A write on a reset connection returns ``ok = false``, the bytes sent (0) and the error."""
    peer = LoopbackPeer(hangup="reset")
    peer.start()
    try:
        dev = _open(peer)
        assert dev.write({"data": b"z"})[0], "the trigger write must reach the peer"  # the byte that makes the peer send its RST
        readable, _, _ = select.select([dev.sock], [], [], 2)  # the RST has arrived
        assert readable
        ok, count, err = dev.write({"data": b"x"})
        dev.close()
    finally:
        peer.stop()

    assert (ok, count) == (False, 0)
    assert err and ("reset" in err.lower() or "pipe" in err.lower())


def test_write_that_cannot_drain_times_out_with_count_sent() -> None:
    """A write the peer never drains fails with ``err = "timeout"`` and ``count`` = the bytes that went out."""
    listener = socket.create_server(("127.0.0.1", 0))
    try:
        dev = TcpDriver().open(
            {"host": "127.0.0.1", "port": listener.getsockname()[1], "timeout": 5, "readTerm": "", "writeTerm": ""}
        )
        conn, _ = listener.accept()  # accepted but never read, so the send buffers fill
        payload = b"\x00" * (64 * 1024 * 1024)
        start = time.monotonic()
        ok, count, err = dev.write({"data": payload, "timeout": 0.2})
        elapsed = time.monotonic() - start
        dev.close()
        conn.close()
    finally:
        listener.close()

    assert (ok, err) == (False, "timeout")
    assert 0 < count < len(payload)
    assert elapsed < 1.5


# --- Seam-1: read/write through a driven RunEngine ------------------------------------------


def _run_with_peer(app: QCoreApplication, source: str, port: int):
    """Drive one schedule with a live loopback peer; return ``(store, run_uuid)``."""
    captured = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with scratch_directory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source.replace(LoopbackPeer.PORT_PLACEHOLDER, str(port)), encoding="utf-8", newline="\n")
        engine = RunEngine(store, str(schedule_path))
        engine.run_uuid_reported.connect(_capture)
        _drive(engine, app)

    assert captured.get("run_uuid") is not None, "engine never reported a run key"
    return store, captured["run_uuid"]


def _step_message(store: RunStore, ru: str) -> str:
    """The single step's result, asserted PASS, and the message its ATE.report recorded."""
    node = next(n for n in store.nodes(ru) if n["kind"] == "step")
    assert node["result"] == "PASS"
    (attempt,) = store.attempts(ru)
    return attempt["msg"]


def _step(body: str, read_term: str = "", write_term: str = "") -> str:
    """A one-step schedule that opens a TCP device on the peer as ``dev`` and runs ``body``."""
    return f"""return ScheduleParams("x") :setSchedule({{
    StepParams("io"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {{host="127.0.0.1", port=__PORT__, timeout=5, readTerm="{read_term}", writeTerm="{write_term}"}})
{body}
        ATE.close(dev)
      end),
  }})"""


def test_write_term_appended_through_lua_and_count_includes_term(app: QCoreApplication) -> None:
    """ATE.write appends the per-call terminator and ``count`` includes it; the peer gets exact bytes."""
    peer = LoopbackPeer()
    peer.start()
    try:
        store, ru = _run_with_peer(app, _step("""
        local w = ATE.write(dev, {data = "Hi", writeTerm = "\\n"})
        ATE.report(Result.PASS, "ok "..tostring(w.ok).." count "..w.count.." err "..tostring(w.err))"""), peer.port)
    finally:
        peer.stop()

    assert wait_for_disconnect(peer)
    assert peer.received[peer.connections[-1]] == b"Hi\n"
    assert _step_message(store, ru) == "ok true count 3 err nil"


def test_read_strips_readTerm_through_lua(app: QCoreApplication) -> None:
    """ATE.read strips the terminator and reports a complete read."""
    peer = LoopbackPeer(payload=b"Hello=")
    peer.start()
    try:
        store, ru = _run_with_peer(app, _step("""
        local r = ATE.read(dev, {size=10})
        ATE.report(Result.PASS, r.data.." "..r.count.." "..tostring(r.partial).." "..tostring(r.err))""",
            read_term="="), peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "Hello 5 false nil"


def test_read_stops_at_size_through_lua(app: QCoreApplication) -> None:
    """ATE.read with ``size`` returns exactly that many bytes and marks the read complete."""
    peer = LoopbackPeer(payload=b"0123456789ABCDEF")
    peer.start()
    try:
        store, ru = _run_with_peer(app, _step("""
        local r = ATE.read(dev, {size=5})
        ATE.report(Result.PASS, r.data.." "..r.count.." "..tostring(r.partial))"""), peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "01234 5 false"


def test_per_call_override_applies_to_that_call_only(app: QCoreApplication) -> None:
    """A per-call readTerm and writeTerm override applies to that call only; the next call uses the
    open-time terminators again."""
    peer = LoopbackPeer(payload=b"a;b#")
    peer.start()
    try:
        store, ru = _run_with_peer(app, _step("""
        local r1 = ATE.read(dev, {readTerm = ";", timeout = 0.5})
        local r2 = ATE.read(dev, {timeout = 0.5})
        local w1 = ATE.write(dev, {data = "hello", writeTerm = "X"})
        local w2 = ATE.write(dev, {data = "bye"})
        ATE.report(Result.PASS, r1.data.." "..r2.data.." "..w1.count.." "..w2.count)""",
            read_term="#", write_term="#"), peer.port)
    finally:
        peer.stop()

    assert wait_for_disconnect(peer)
    assert peer.received[peer.connections[-1]] == b"helloXbye#"
    assert _step_message(store, ru) == "a b 6 4"


def test_partial_timeout_logs_warning_and_marks_partial(app: QCoreApplication) -> None:
    """A read that times out after some bytes is ``partial`` and logs one WARNING (source ate)."""
    peer = LoopbackPeer(payload=b"abc")  # three bytes, then silence — the read can't finish in time
    peer.start()
    try:
        store, ru = _run_with_peer(app, _step("""
        local r = ATE.read(dev, {size=10, timeout=0.2})
        ATE.report(Result.PASS, r.data.." "..r.count.." "..tostring(r.partial).." "..tostring(r.err))"""), peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "abc 3 true nil"
    warnings = [ln for ln in store.log_lines(ru) if ln["level"] == "WARNING" and ln["source"] == "ate"]
    assert len(warnings) == 1, "a partial read on timeout must log one WARNING at source ate"
    assert "timed out after 3 byte(s)" in warnings[0]["text"]


def test_timeout_no_bytes_reports_err_timeout(app: QCoreApplication) -> None:
    """A read that times out with no bytes reports ``data = nil``, ``count = 0`` and ``err = "timeout"``."""
    peer = LoopbackPeer()  # sends nothing; the read waits out its timeout empty-handed
    peer.start()
    try:
        store, ru = _run_with_peer(app, _step("""
        local r = ATE.read(dev, {size=10, timeout=0.2}, function(res) return res end)  -- onError hands back the failure table
        ATE.report(Result.PASS, tostring(r.data).." "..r.count.." "..tostring(r.partial).." "..r.err)"""), peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "nil 0 false timeout"


def test_read_other_failure_through_lua(app: QCoreApplication) -> None:
    """A reset connection reaches Lua as ``{data = nil, count = 0, partial = false, err = <message>}``,
    and the following write returns ``{ok = false, count = 0, err = <same message>}``."""
    peer = LoopbackPeer(hangup="reset")  # the peer resets after the device's first byte
    peer.start()
    try:
        store, ru = _run_with_peer(app, _step("""
        local keep = function(res) return res end  -- onError hands back the failure table
        ATE.write(dev, {data = "p"}, keep)  -- this first byte makes the peer reset
        local r = ATE.read(dev, {timeout=2}, keep)
        local w = ATE.write(dev, {data = "x"}, keep)
        ATE.report(Result.PASS, tostring(r.data).."|"..r.count.."|"..tostring(r.partial).."|"..r.err
          .."|"..tostring(w.ok).."|"..w.count.."|"..w.err)"""), peer.port)
    finally:
        peer.stop()

    data, count, partial, err, ok, wcount, werr = _step_message(store, ru).split("|")
    assert (data, count, partial) == ("nil", "0", "false")
    assert "reset" in err.lower()
    assert (ok, wcount, werr) == ("false", "0", err)


def test_bad_call_params_isolated_and_name_field(app: QCoreApplication) -> None:
    """A bad per-call field (unknown, string/float/bool ``size``) is isolated (logged at ERROR, source
    ate, never onError), names the field, and ATE.read/ATE.write return the graceful halves
    ``false, "<file>:<line>: ATE.<fn>: <msg>"`` without touching the device."""
    peer = LoopbackPeer(payload=b"x")
    peer.start()
    try:
        store, ru = _run_with_peer(app, _step("""
        local out = {}
        for _, p in ipairs({{size = "oops"}, {size = 5.0}, {size = true}, {sizee = 1}, {size = 0}, {size = -1}}) do
          local ok, msg = ATE.read(dev, p)
          out[#out + 1] = tostring(ok) .. " " .. msg
        end
        local ok, msg = ATE.write(dev, {writeTerm = "\\n"})
        out[#out + 1] = tostring(ok) .. " " .. msg
        ATE.report(Result.PASS, table.concat(out, "\\n"))"""), peer.port)
    finally:
        peer.stop()

    lines = _step_message(store, ru).split("\n")
    assert len(lines) == 7
    # each message starts with the schedule's own file:line, then the function (spec: ATE errors)
    assert all(re.match(r"false \S*schedule\.tslua:\d+: ATE\.(read|write): ", ln) for ln in lines), lines
    assert "ATE.read: field 'size' must be one of int, got str" in lines[0]
    assert "ATE.read: field 'size' must be one of int, got float" in lines[1]
    assert "ATE.read: field 'size' must be one of int, got bool" in lines[2]
    assert "ATE.read: unknown field 'sizee'" in lines[3]
    assert "ATE.read: field 'size' must be a positive int, got 0" in lines[4]
    assert "ATE.read: field 'size' must be a positive int, got -1" in lines[5]
    assert "ATE.write: missing required field 'data'" in lines[6]
    error_logs = [ln for ln in store.log_lines(ru) if ln["level"] == "ERROR" and ln["source"] == "ate"]
    assert len(error_logs) == 7


def test_lua_read_removes_terminator_and_handles_timeout(app: QCoreApplication) -> None:
    """Raw bytes reach Lua intact (0xFF and 0x00 survive, ``#r.data == r.count``), a raw per-call
    terminator is stripped, and a following read that times out gives ``data = nil``, ``err = "timeout"``."""
    peer = LoopbackPeer(payload=b"\xff\x00A\xfe")
    peer.start()
    try:
        store, ru = _run_with_peer(app, _step("""
        local r = ATE.read(dev, {readTerm = string.char(254), timeout = 0.5})
        local t = ATE.read(dev, {timeout = 0.1}, function(res) return res end)  -- onError hands back the failure table
        ATE.report(Result.PASS, string.format("%d %d %d %d %d %s %s %s %d",
          string.byte(r.data, 1), string.byte(r.data, 2), string.byte(r.data, 3), #r.data, r.count,
          tostring(r.partial), tostring(t.data), t.err, t.count))"""), peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "255 0 65 3 3 false nil timeout 0"


def test_lua_write_sends_raw_0xFF(app: QCoreApplication) -> None:
    """A Lua string with non-UTF-8 bytes goes out verbatim (no re-encoding), with a raw terminator."""
    peer = LoopbackPeer()
    peer.start()
    try:
        store, ru = _run_with_peer(app, _step("""
        local w = ATE.write(dev, {data = string.char(255, 65), writeTerm = string.char(13, 128)})
        ATE.report(Result.PASS, "ok "..tostring(w.ok).." count "..w.count)"""), peer.port)
    finally:
        peer.stop()

    assert wait_for_disconnect(peer)
    assert peer.received[peer.connections[-1]] == b"\xffA\r\x80"
    assert _step_message(store, ru) == "ok true count 4"
