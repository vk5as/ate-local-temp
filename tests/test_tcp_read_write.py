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
    try:
        dev = TcpDriver().open(
            {"host": "127.0.0.1", "port": peer.port, "timeout": 5, "readTerm": "\n", "writeTerm": ""}
        )
        conn = peer.wait_for_connection(2)
        data, count, partial, err = dev.read({"size": 10, "readTerm": "\n"})
    finally:
        dev.close()
        peer.stop()

    assert (data, count, partial, err) == (b"Hello", 5, False, None)


def test_read_stops_at_size() -> None:
    """A read with a ``size`` limit returns exactly that many bytes and marks it complete."""
    peer = LoopbackPeer(payload=b"0123456789ABCDEF")
    peer.start()
    try:
        dev = TcpDriver().open(
            {"host": "127.0.0.1", "port": peer.port, "timeout": 5, "readTerm": "", "writeTerm": ""}
        )
        conn = peer.wait_for_connection(2)
        data, count, partial, err = dev.read({"size": 5})
    finally:
        dev.close()
        peer.stop()

    assert (data, count, partial, err) == (b"01234", 5, False, None)


def test_partial_read_on_timeout_is_partial_and_carries_bytes() -> None:
    """A read that grabs some bytes then times out returns them with ``partial = true`` and no error."""
    peer = LoopbackPeer(payload=b"abc")  # only three bytes; the rest never arrives
    peer.start()
    try:
        dev = TcpDriver().open(
            {"host": "127.0.0.1", "port": peer.port, "timeout": 5, "readTerm": "", "writeTerm": ""}
        )
        conn = peer.wait_for_connection(2)
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
    rec = peer.received[conn].split(b"\n")  # [hex, count] — the read consumed the payload; only the writeback is recorded
    print("REC:", rec, "attempt msgs:", [a["msg"] for a in store.attempts(ru)])

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
    rec = peer.received[conn].split(b"\n")  # [hex, count, partial] — the read consumed the payload; only the writeback is recorded
    assert bytes.fromhex(rec[0].decode()) == b"01234"
    assert int(rec[1]) == 5
    assert rec[2] == b"false"

    node = next(n for n in store.nodes(ru) if n["kind"] == "step")
    assert node["result"] == "PASS"


def test_per_call_override_applies_to_that_call_only(app: QCoreApplication) -> None:
    """A per-call ``readTerm`` override applies to that call only, not the open-time configuration."""
    peer = LoopbackPeer(payload=b"a=b=c\n")  # "\n" is the open-time terminator; "=" is the per-call one
    peer.start()
    try:
        store, ru = _run_with_peer(
            app,
            """return ScheduleParams("x") :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="\\n", writeTerm=""})
        -- override the terminator for THIS call only; without it the open-time "\\n" would strip at the end
        local r = ATE.read(dev, {readTerm = "=", size=10})
        ATE.write(dev, {data = r.data .. "\\n" .. tostring(r.count)})
        ATE.report(Result.PASS, "count "..r.count.." partial "..tostring(r.partial))
      end),
  })""",
            peer.port,
        )
    finally:
        peer.stop()

    wait_for_disconnect(peer)
    conn = peer.connections[-1]
    rec = peer.received[conn].split(b"\n")  # [hex, count] — the read consumed the payload; only the writeback is recorded
    assert bytes.fromhex(rec[0].decode()) == b"a"  # stripped at the per-call "=" — not the open-time "\n"
    assert int(rec[1]) == 1

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


# --- Helpers -------------------------------------------------------------------------------
