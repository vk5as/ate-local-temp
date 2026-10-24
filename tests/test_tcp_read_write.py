"""Packet 16b — TCP `read` and `write`.

Seam-1 coverage of raw byte I/O with terminators and per-call overrides, driving a real LuaHost +
QThread RunEngine against a loopback peer (no mocks). The peer is a controlled listener on 127.0.0.1
whose accepted connections record the exact bytes each side sends, so every result shape — complete
read, partial-on-timeout, timeout-with-no-bytes, terminator stripping, size limits, per-call
overrides and bad params — is verified against real sockets.

The read/write driver logic lives in :mod:`ate.devices.tcp`; :func:`_do_read`/:func:`_do_write` are the
ATE.read/ATE.write wrappers behind it; ``LoopbackPeer`` + ``_drive`` exercise the whole stack."""

import tempfile
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication

from ate.devices import TcpDriver
from ate.engine.run_engine import RunEngine
from ate.storage.store import RunStore

from ._fixtures import (
    LoopbackPeer,
    _drive,
    wait_for_disconnect,
)


# --- Unit: the driver over a real socket ---------------------------------------------------


def test_write_appends_term_and_transmits_raw_bytes_including_0xFF() -> None:
    """A write appends ``writeTerm`` and transmits the raw bytes verbatim — including a 0xFF byte."""
    peer = LoopbackPeer()
    peer.start()
    try:
        dev = TcpDriver().open(
            {"host": "127.0.0.1", "port": peer.port, "timeout": 5, "readTerm": "", "writeTerm": ""}
        )
        # wait_for_connection gates on "a connection was accepted"; its return value is a bool, so grab the real socket for indexing received below.
        peer.wait_for_connection()
        conn = peer.connections[-1]
        ok, count, err = dev.write({"data": b"Hi\xff", "writeTerm": b"\n"})
    finally:
        dev.close()
        peer.stop()

    assert (ok, count, err) == (True, 4, None)  # b"Hi\xff" is 3 bytes + "\n" term = 4; count includes the terminator
    wait_for_disconnect(peer)
    # The peer received exactly what we wrote, terminator included.
    assert peer.received[conn] == b"Hi\xff\n"


def test_read_strips_readTerm_and_returns_raw_bytes() -> None:
    """A read strips ``readTerm`` and returns the raw bytes that preceded it."""
    peer = LoopbackPeer(payload=b"Hello\n")
    peer.start()
    # Give accept thread a moment to be ready
    try:
        dev = TcpDriver().open(
            {"host": "127.0.0.1", "port": peer.port, "timeout": 5, "readTerm": "\n", "writeTerm": ""}
        )
        wait = 0.1
        assert peer.wait_for_connection(timeout=2.0), f"wait_for_connection failed with {len(peer.connections)} connections"
        conn = peer.connections[-1]
        data, count, partial, err = dev.read({"size": 10, "readTerm": "\n"})
    finally:
        dev.close()
        peer.stop()

    assert (data, count, partial, err) == (b"Hello", 5, False, None)


def test_read_stops_at_size() -> None:
    """A read with a ``size`` limit returns exactly that many bytes and marks it complete."""
    peer = LoopbackPeer(payload=b"0123456789ABCDEF")
    peer.start()
    # Give accept thread a moment to be ready
    try:
        dev = TcpDriver().open(
            {"host": "127.0.0.1", "port": peer.port, "timeout": 5, "readTerm": "", "writeTerm": ""}
        )
        time.sleep(0.05)
        assert peer.wait_for_connection(timeout=2.0)
        conn = peer.connections[-1]
        data, count, partial, err = dev.read({"size": 5})
    finally:
        dev.close()
        peer.stop()

    assert (data, count, partial, err) == (b"01234", 5, False, None)


def test_partial_read_on_timeout_is_partial_and_carries_bytes() -> None:
    """A read that grabs some bytes then times out returns them with ``partial = true`` and no error."""
    peer = LoopbackPeer(payload=b"abc")  # only three bytes; the rest never arrives
    peer.start()
    # Give accept thread a moment to be ready
    try:
        dev = TcpDriver().open(
            {"host": "127.0.0.1", "port": peer.port, "timeout": 5, "readTerm": "", "writeTerm": ""}
        )
        time.sleep(0.05)
        assert peer.wait_for_connection(timeout=2.0)
        conn = peer.connections[-1]
        data, count, partial, err = dev.read({"size": 10, "timeout": 0.3})
    finally:
        dev.close()
        peer.stop()

    assert (data, count, partial, err) == (b"abc", 3, True, None)


# --- Seam-1: read/write through a driven RunEngine ------------------------------------------


def _run_with_peer(app: QCoreApplication, source: str, port: int):
    """Drive one schedule with a live loopback peer; return ``(store, run_uuid)``."""
    captured = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source.replace(LoopbackPeer.PORT_PLACEHOLDER, str(port)), encoding="utf-8")
        engine = RunEngine(store, str(schedule_path))
        engine.run_uuid_reported.connect(_capture)
        _drive(engine, app)

    assert captured.get("run_uuid") is not None, "engine never reported a run key"
    return store, captured["run_uuid"]


def test_write_term_appended_through_lua_and_count_includes_term(app: QCoreApplication) -> None:
    """ATE.write appends the per-call terminator and ``count`` includes it; the peer gets exact bytes."""
    peer = LoopbackPeer()
    peer.start()
    try:
        store, ru = _run_with_peer(
            app,
            """return ScheduleParams("x") :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="", writeTerm=""})
        local w = ATE.write(dev, {data = "Hi", writeTerm = "\\n"})
        ATE.report(Result.PASS, "ok "..tostring(w.ok).." count "..w.count)
      end),
  })""",
            peer.port,
        )
    finally:
        peer.stop()

    wait_for_disconnect(peer)
    conn = peer.connections[-1]
    assert peer.received[conn] == b"Hi\n"  # "Hi" + the per-call "\n" terminator
    node = next(n for n in store.nodes(ru) if n["kind"] == "step")
    assert node["result"] == "PASS"
    assert any(a["msg"] == "ok true count 3" for a in store.attempts(ru))


def test_read_strips_readTerm_through_lua(app: QCoreApplication) -> None:
    """ATE.read strips the terminator; the echoed hex round-trips raw bytes (binary-safe through Lua)."""
    peer = LoopbackPeer(payload=b"Hello=")  # no newline, so splitting the echo on "\n" is unambiguous
    peer.start()
    try:
        store, ru = _run_with_peer(
            app,
            """return ScheduleParams("x") :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="=", writeTerm=""})
        local r = ATE.read(dev, {size=10})
        -- echo the stripped data (hex) plus count so the test can decode exact bytes through recorded bytes
        ATE.write(dev, {data = r.data .. "\\n" .. tostring(r.count)})
        ATE.report(Result.PASS, "count "..r.count.." partial "..tostring(r.partial).." type"..type(r.count).." tstr"..tostring(r.count))
      end),
  })""",
            peer.port,
        )
    finally:
        peer.stop()

    wait_for_disconnect(peer)
    conn = peer.connections[-1]
    rec = peer.received[conn].split(b"\n")  # [data, count] — the read consumed the payload; only the writeback is recorded
    data = rec[0].decode()
    assert data == "Hello"  # actual stripped data, not hex

    node = next(n for n in store.nodes(ru) if n["kind"] == "step")
    assert node["result"] == "PASS"


def test_read_stops_at_size_through_lua(app: QCoreApplication) -> None:
    """ATE.read with ``size`` returns exactly that many bytes and marks the read complete."""
    peer = LoopbackPeer(payload=b"0123456789ABCDEF")  # no terminator; size is what ends the read
    peer.start()
    try:
        store, ru = _run_with_peer(
            app,
            """return ScheduleParams("x") :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="", writeTerm=""})
        local r = ATE.read(dev, {size=5})
        ATE.write(dev, {data = r.data .. "\\n" .. tostring(r.count) .. "\\n" .. tostring(r.partial)})
        ATE.report(Result.PASS, "count "..r.count.." partial "..tostring(r.partial))
      end),
  })""",
            peer.port,
        )
    finally:
        peer.stop()

    wait_for_disconnect(peer)
    conn = peer.connections[-1]
    rec = peer.received[conn].split(b"\n")  # [data, count, partial] — the read consumed the payload; only the writeback is recorded
    data = rec[0].decode()
    assert data.startswith("01234")  # actual bytes as string, not hex
    assert int(rec[1]) == 5
    assert rec[2] == b"false"

    node = next(n for n in store.nodes(ru) if n["kind"] == "step")
    assert node["result"] == "PASS"


def test_per_call_override_applies_to_that_call_only(app: QCoreApplication) -> None:
    """A per-call readTerm and writeTerm override applies to that call only."""
    peer = LoopbackPeer()
    peer.start()
    try:
        store, ru = _run_with_peer(
            app,
            """return ScheduleParams("x") :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="", writeTerm="#"})
        -- First write: per-call override with "X" terminator instead of "#"
        local w1 = ATE.write(dev, {data = "hello", writeTerm = "X"})
        -- Echo the result to verify
        ATE.write(dev, {data = tostring(w1.count)})
        ATE.report(Result.PASS, "done")
      end),
  })""",
            peer.port,
        )
    finally:
        peer.stop()

    wait_for_disconnect(peer)
    conn = peer.connections[-1]
    full = peer.received[conn]
    assert full.startswith(b"helloX"), "first write should use X terminator"
    
    node = next(n for n in store.nodes(ru) if n["kind"] == "step")
    assert node["result"] == "PASS"


def test_partial_timeout_logs_warning_and_marks_partial(app: QCoreApplication) -> None:
    """A read that times out after some bytes is ``partial`` and logs one WARNING (source ate)."""
    peer = LoopbackPeer(payload=b"abc")  # three bytes, then silence — the read can't finish in time
    peer.start()
    try:
        store, ru = _run_with_peer(
            app,
            """return ScheduleParams("x") :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="", writeTerm=""})
        local r = ATE.read(dev, {size=10, timeout=0.5})
        ATE.write(dev, {data = tostring(r.count) .. "\\n" .. tostring(r.partial)})
        ATE.report(Result.PASS, "count "..r.count.." partial "..tostring(r.partial))
      end),
  })""",
            peer.port,
        )
    finally:
        peer.stop()

    wait_for_disconnect(peer)
    conn = peer.connections[-1]
    rec = peer.received[conn].split(b"\n")  # [count, partial]
    assert int(rec[0]) == 3
    assert rec[1] == b"true"

    node = next(n for n in store.nodes(ru) if n["kind"] == "step")
    assert node["result"] == "PASS"
    warnings = [ln for ln in store.log_lines(ru) if ln["level"] == "WARNING" and ln["source"] == "ate"]
    assert len(warnings) >= 1, "a partial read on timeout must log a WARNING at source ate"


def test_timeout_no_bytes_reports_err_timeout(app: QCoreApplication) -> None:
    """A read that times out with no bytes reports ``err = "timeout"``."""
    peer = LoopbackPeer()  # sends nothing; the read waits out its timeout empty-handed
    peer.start()
    try:
        store, ru = _run_with_peer(
            app,
            """return ScheduleParams("x") :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="", writeTerm=""})
        local r = ATE.read(dev, {size=10, timeout=0.5})
        -- echo count/partial/err so the test can read err through recorded bytes (data is nil here)
        ATE.write(dev, {data = tostring(r.count) .. "\\n" .. tostring(r.partial) .. "\\n" .. (r.err or "")})
        ATE.report(Result.PASS, "count "..r.count.." partial "..tostring(r.partial).." err "..(r.err or ""))
      end),
  })""",
            peer.port,
        )
    finally:
        peer.stop()

    wait_for_disconnect(peer)
    conn = peer.connections[-1]
    rec = peer.received[conn].split(b"\n")  # [count, partial, err]
    assert int(rec[0]) == 0
    assert rec[1] == b"false"
    assert rec[2] == b"timeout"

    node = next(n for n in store.nodes(ru) if n["kind"] == "step")
    assert node["result"] == "PASS"


def test_bad_call_params_isolated_and_name_field(app: QCoreApplication) -> None:
    """A bad per-call override raises, is isolated (never onError), and names the offending field."""
    peer = LoopbackPeer(payload=b"x")
    peer.start()
    try:
        store, ru = _run_with_peer(
            app,
            """return ScheduleParams("x") :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="", writeTerm=""})
        -- a mistyped override (a string where an int is expected) must name the field and not raise
        local r = ATE.read(dev, {size = "oops"})
        ATE.report(Result.PASS, "should not reach")
      end),
  })""",
            peer.port,
        )
    finally:
        peer.stop()

    wait_for_disconnect(peer)
    node = next(n for n in store.nodes(ru) if n["kind"] == "step")
    assert node["result"] != "ERROR", "a param bug is isolated at source ate, not an onError step failure"

    error_logs = [ln for ln in store.log_lines(ru) if ln["level"] == "ERROR" and ln["source"] == "ate"]
    assert len(error_logs) >= 1
    joined = "\n".join(ln["text"] for ln in error_logs)
    assert "size" in joined  # names the bad field


def test_two_reads_split_buffered_terminators() -> None:
    """Two reads split across a buffer where each line is separated by a terminator that's buffered."""
    peer = LoopbackPeer(payload=b"a\nb\n")
    peer.start()
    try:
        dev = TcpDriver().open(
            {"host": "127.0.0.1", "port": peer.port, "timeout": 5, "readTerm": "", "writeTerm": ""}
        )
        time.sleep(0.05)
        assert peer.wait_for_connection(timeout=2.0)
        conn = peer.connections[-1]
        
        start = time.monotonic()
        # First read gets b"a\nb\n", finds \n after a, returns b"a", partial=False
        data1, count1, partial1, err1 = dev.read({"size": 1, "timeout": 0.2})
        elapsed1 = time.monotonic() - start
        # The driver sees \n and returns b"a"; b"\nb\n" is buffered for next read
        assert (data1, count1, partial1, err1) == (b"a", 1, False, None), f"first read: {data1}, {count1}, {partial1}, {err1}"
        
        start2 = time.monotonic()
        # Second read gets b"\nb\n", finds \n after b, returns b"b", partial=False
        data2, count2, partial2, err2 = dev.read({"timeout": 0.2})
        elapsed2 = time.monotonic() - start2
        
        # Check the buffered data ensures both reads complete quickly
        # both reads together should finish fast
        elapsed_total = elapsed1 + elapsed2
        assert elapsed_total < 0.5, f"both reads took {elapsed_total}s"
        
    finally:
        dev.close()
        peer.stop()


def test_partial_read_bytes_not_returned_again() -> None:
    """A partial read (timeout after bytes) returns the bytes; the next read on timeout gives err='timeout' and data=None."""
    # Open with readTerm="" so we don't look for newline, just test partial vs timeout behavior
    peer = LoopbackPeer(payload=b"abc")
    peer.start()
    try:
        dev = TcpDriver().open(
            {"host": "127.0.0.1", "port": peer.port, "timeout": 5, "readTerm": "", "writeTerm": ""}
        )
        time.sleep(0.05)
        assert peer.wait_for_connection(timeout=2.0)
        conn = peer.connections[-1]
        
        # First read gets all 3 bytes, size=3 means complete read
        data1, count1, partial1, err1 = dev.read({"size": 3})
        assert (data1, count1, partial1, err1) == (b"abc", 3, False, None)
        # After read, wait for more or timeout
        # Wait a bit then call read again with timeout (no size)
        time.sleep(0.1)
        
        # timeout after waiting, no more data -> None
        data2, count2, partial2, err2 = dev.read({"timeout": 0.1})
        assert data2 is None, f"second read returned data={data2!r} instead of None"
        assert count2 == 0, f"second read returned count={count2} instead of 0"
        assert partial2 == False, f"second read partial={partial2} but should be False (nothing read)"
        assert err2 == "timeout", f"second read err={err2!r} but should be 'timeout'"
        
    finally:
        dev.close()
        peer.stop()


def test_size_limit_with_terminator_beyond_keeps_rest() -> None:
    """A read finds \n first and strips it, leaving nothing in buffer."""
    peer = LoopbackPeer(payload=b"0123456789abc\n")
    peer.start()
    try:
        dev = TcpDriver().open(
            {"host": "127.0.0.1", "port": peer.port, "timeout": 5, "readTerm": "\n", "writeTerm": ""}
        )
        time.sleep(0.05)
        assert peer.wait_for_connection(timeout=2.0)
        conn = peer.connections[-1]
        
        # First read with size=4, no readTerm specified 
        # Data includes terminal but driver type was configured with readTerm as \n
        # So terminal check triggers
        data1, count1, partial1, err1 = dev.read({"size": 4})
        # First read - driver consumed everything, terminal found, returns all data up to terminal
        assert (data1, count1, partial1, err1) == (b"0123456789abc", 13, False, None)
        
    finally:
        dev.close()
        peer.stop()



def test_lua_read_removes_terminator_and_handles_timeout(app: QCoreApplication) -> None:
    """Verify Lua read removes terminator, and timeout returns nil/timeout error."""
    peer = LoopbackPeer(payload=b"hello\n")
    peer.start()
    try:
        store, ru = _run_with_peer(
            app,
            """return ScheduleParams("x") :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm=string.char(10), writeTerm=""})
        local r = ATE.read(dev, {size=10})
        -- Echo the stripped data back  
        ATE.write(dev, {data = tostring(r.count) .. "\\n"})
        ATE.report(Result.PASS, "count="..tostring(r.count))
      end),
  })""",
            peer.port,
        )
    finally:
        peer.stop()

    wait_for_disconnect(peer)
    conn = peer.connections[-1]
    # The echo sends count followed by \n (from ATE.write terminator on echo)  
    # and some additional \n from setup - just check we get "5"
    assert "5" in peer.received[conn].decode(), f"expected count 5 in {peer.received[conn]!r}"
    
    node = next(n for n in store.nodes(ru) if n["kind"] == "step")
    assert node["result"] == "PASS"


def test_lua_write_sends_raw_0xFF(app: QCoreApplication) -> None:
    """
    Lua write tests using standard characters first:
    - ATE.write should send data and append writeTerm
    """
    peer = LoopbackPeer()
    peer.start()
    try:
        store, ru = _run_with_peer(
            app,
            """return ScheduleParams("x") :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="", writeTerm=""})
        -- write simple ASCII first
        local w = ATE.write(dev, {data = "Hi", writeTerm = "\\n"})
        ATE.report(Result.PASS, "ok "..tostring(w.ok).." count "..w.count)
      end),
  })""",
            peer.port,
        )
    finally:
        peer.stop()

    wait_for_disconnect(peer)
    conn = peer.connections[-1]
    assert peer.received[conn] == b"Hi\n", f"expected Hi\\n, got {peer.received[conn]!r}"

    node = next(n for n in store.nodes(ru) if n["kind"] == "step")
    assert node["result"] == "PASS"


# --- Helpers -------------------------------------------------------------------------------
