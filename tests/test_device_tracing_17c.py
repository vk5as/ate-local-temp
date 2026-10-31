"""Packet 17c — Device tracing: every `open`, `read`, `write` and `close` at Log.DEBUG.

Seam-1 coverage: a real schedule run against a live loopback peer (TCP and UDP, no mocks) with
`deviceTrace.enabled` on and off, and the lines read back from the run store. With tracing on, each
operation writes one DEBUG line with source "device" naming the Device (type + address), the bytes
escaped and truncated at `deviceTrace.maxBytes`, and the wall-clock time the call took in ms; the
line is owned by the Attempt or Pre-/Post-test Action that made the call, exactly like an
`ATE.log` line, and sits in the stored order at the point the call happened. Off (the default)
nothing is written and the registry wraps nothing. The HTML report drops DEBUG, so trace lines never
reach a report; the debug window keeps every level, so they show there.

The tracer and the handle wrapper live in :mod:`ate.devices.tracing`; :meth:`DeviceRegistry.open_device`
and :meth:`close_device` are where the operations are timed and logged.
"""

from __future__ import annotations

import re
from pathlib import Path

from PyQt6.QtCore import QCoreApplication

from ate.config import Config
from ate.engine.run_engine import RunEngine
from ate.report.html import render_report
from ate.storage.store import RunStore

from ._fixtures import LoopbackPeer, UdpLoopbackPeer, _drive
from .test_device_onerror import _dead_port
from tests._fixtures import scratch_directory

#: A trace line ends with the operation's wall-clock time, one decimal, in ms.
_TIMING = re.compile(r"\((\d+\.\d) ms\)$")

#: A schedule's own INFO lines, used to pin a trace line's position against the step's log.
_IO_STEP = r"""
return ScheduleParams("trace") :setSchedule({
  StepParams("io"):setStepNo("1.")
    :setAction(function()
      ATE.log(Log.INFO, "before open")
      local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="\n", writeTerm="\n"})
      ATE.write(dev, {data="*IDN?"})
      local r = ATE.read(dev, {timeout=2})
      ATE.log(Log.INFO, "after io")
      ATE.close(dev)
      ATE.report("PASS", "read " .. (r.count or 0) .. " byte(s)")
    end),
})
"""


def _run(
    app: QCoreApplication,
    source: str,
    port: int,
    *,
    trace: bool = False,
    max_bytes: int = 256,
) -> tuple[RunStore, str]:
    """Drive one schedule with a live port in ``__PORT__``; return ``(store, run_uuid)``.

    ``trace``/``max_bytes`` are the app config's ``deviceTrace.enabled``/``maxBytes``, handed to the
    engine the way the receive-buffer cap is (packet 17a)."""
    captured: dict[str, str] = {}

    def _capture(run_uuid: str) -> None:
        captured.setdefault("run_uuid", run_uuid)

    with scratch_directory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source.replace(LoopbackPeer.PORT_PLACEHOLDER, str(port)), encoding="utf-8", newline="\n")
        engine = RunEngine(
            store,
            str(schedule_path),
            device_trace_enabled=trace,
            device_trace_max_bytes=max_bytes,
        )
        engine.run_uuid_reported.connect(_capture)
        _drive(engine, app)

    assert captured.get("run_uuid"), "engine never reported a run key"
    return store, captured["run_uuid"]


def _trace(store: RunStore, run_uuid: str) -> list[dict]:
    """Every stored device trace line, in stored order."""
    return [ln for ln in store.log_lines(run_uuid) if ln["source"] == "device" and ln["level"] == "DEBUG"]


def _ms(text: str) -> float:
    """The operation time in ms claimed by a trace line's trailing `(n.n ms)`."""
    match = _TIMING.search(text)
    assert match, f"no ms timing in {text!r}"
    return float(match.group(1))


# --- off by default -------------------------------------------------------------


def test_tracing_off_by_default_records_no_device_lines(app: QCoreApplication) -> None:
    """The default (enabled=false) traces nothing: the step opens, writes, reads and closes with no
    device DEBUG line stored, while its own INFO lines are still there."""
    peer = LoopbackPeer(payload=b"ACME,PSU,1\r\n")
    peer.start()
    try:
        store, ru = _run(app, _IO_STEP, peer.port)  # no trace argument: the config default
    finally:
        peer.stop()

    assert _trace(store, ru) == []
    texts = [ln["text"] for ln in store.log_lines(ru)]
    assert "before open" in texts and "after io" in texts  # the run really did the I/O


# --- on: one line per operation, in order -------------------------------------


def test_open_write_read_close_each_traced_with_bytes_and_timing(app: QCoreApplication) -> None:
    """With tracing on, the step's four operations each get one DEBUG line (source device) naming
    `TCP <host>:<port>`, the escaped payload and a `(n.n ms)` timing, in call order."""
    peer = LoopbackPeer(payload=b"ACME,PSU,1\r\n")
    peer.start()
    try:
        store, ru = _run(app, _IO_STEP, peer.port, trace=True)
    finally:
        peer.stop()

    label = f"TCP 127.0.0.1:{peer.port}"
    texts = [ln["text"] for ln in _trace(store, ru)]
    assert len(texts) == 4, texts
    assert texts[0].startswith(f"open {label} ok "), texts[0]
    assert texts[1].startswith(f'write {label} 6 bytes "*IDN?\\n" '), texts[1]  # payload plus open writeTerm
    assert texts[2].startswith(f'read {label} 11 bytes "ACME,PSU,1\\r" '), texts[2]  # readTerm "\n" stripped
    assert texts[3].startswith(f"close {label} "), texts[3]
    for text in texts:
        _ms(text)
    assert all(label in text for text in texts)  # one label for one device


def test_trace_lines_are_owned_by_the_step_attempt(app: QCoreApplication) -> None:
    """Each trace line carries the Attempt that made the call, like an ATE.log line."""
    peer = LoopbackPeer(payload=b"ACME,PSU,1\r\n")
    peer.start()
    try:
        store, ru = _run(app, _IO_STEP, peer.port, trace=True)
    finally:
        peer.stop()

    node = next(n for n in store.nodes(ru) if n["kind"] == "step")
    (attempt,) = store.attempts(ru)
    lines = _trace(store, ru)
    assert len(lines) == 4
    for line in lines:
        assert line["node_seq"] == node["node_seq"], line
        assert line["attempt_no"] == attempt["attempt_no"], line
        assert line["action_seq"] is None, line


def test_a_pre_test_action_trace_line_is_owned_by_that_action(app: QCoreApplication) -> None:
    """A device opened in a Pre-test Action traces lines owned by the Action, not by any step."""
    peer = LoopbackPeer(payload=b"ping\n")
    peer.start()
    source = r"""
return ScheduleParams("trace")
  :setPreTestAction(function()
    local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="\n", writeTerm=""})
    ATE.read(dev, {timeout=2})
    ATE.close(dev)
  end)
  :setSchedule({
    StepParams("io"):setStepNo("1.")
      :setAction(function() ATE.report("PASS", "no device here") end),
  })
"""
    try:
        store, ru = _run(app, source, peer.port, trace=True)
    finally:
        peer.stop()

    lines = _trace(store, ru)
    assert len(lines) == 3, [ln["text"] for ln in lines]
    for line in lines:
        assert line["action_seq"] == 0, line
        assert line["node_seq"] is None and line["attempt_no"] is None, line


def test_trace_lines_sit_in_stored_order_between_the_step_log_lines(app: QCoreApplication) -> None:
    """The open trace line is stored after the step's `ATE.log` before it and before the one after it,
    so the trace interleaves with the schedule's own lines in call order."""
    peer = LoopbackPeer(payload=b"ACME,PSU,1\r\n")
    peer.start()
    try:
        store, ru = _run(app, _IO_STEP, peer.port, trace=True)
    finally:
        peer.stop()

    texts = [ln["text"] for ln in store.log_lines(ru)]
    opened = next(i for i, t in enumerate(texts) if t.startswith("open TCP"))
    assert texts.index("before open") < opened < texts.index("after io"), texts


# --- the bytes: escaping and truncation ---------------------------------------


def test_traced_bytes_are_escaped(app: QCoreApplication) -> None:
    """Printable ASCII stays as-is; \\n \\r \\t \\ and " are backslash escapes; anything else is \\xNN."""
    peer = LoopbackPeer(payload=b"ACME\x00\x01\xff,1\r\n")
    peer.start()
    source = r"""
return ScheduleParams("trace") :setSchedule({
  StepParams("io"):setStepNo("1.")
    :setAction(function()
      local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="\n", writeTerm="\r\n"})
      ATE.write(dev, {data="a\\b\"c"})
      ATE.read(dev, {timeout=2})
      ATE.close(dev)
      ATE.report("PASS", "escaped")
    end),
})
"""
    try:
        store, ru = _run(app, source, peer.port, trace=True)
    finally:
        peer.stop()

    texts = [ln["text"] for ln in _trace(store, ru)]
    # data `a\b"c` + writeTerm `\r\n` = 7 bytes; " and \ escaped, control bytes as \xNN.
    assert texts[1].startswith(f'write TCP 127.0.0.1:{peer.port} 7 bytes "a\\\\b\\"c\\r\\n" '), texts[1]
    # readTerm "\n" stripped, so the read carries the rest of the payload with \r and the three
    # non-printable bytes escaped.
    assert texts[2].startswith(f'read TCP 127.0.0.1:{peer.port} 10 bytes "ACME\\x00\\x01\\xff,1\\r" '), texts[2]


def test_truncation_at_max_bytes_reports_the_dropped_count(app: QCoreApplication) -> None:
    """Truncation cuts the RAW bytes at maxBytes and appends `… (+N bytes)`; read and write payloads
    are both cut, and nothing past the cap is quoted."""
    peer = LoopbackPeer(payload=b"abcdefghij\r\n")  # 12 raw bytes once the terminator is stripped
    peer.start()
    source = r"""
return ScheduleParams("trace") :setSchedule({
  StepParams("io"):setStepNo("1.")
    :setAction(function()
      local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="", writeTerm=""})
      ATE.write(dev, {data="0123456789"})
      ATE.read(dev, {timeout=2})
      ATE.close(dev)
      ATE.report("PASS", "truncated")
    end),
})
"""
    try:
        store, ru = _run(app, source, peer.port, trace=True, max_bytes=8)
    finally:
        peer.stop()

    texts = [ln["text"] for ln in _trace(store, ru)]
    assert texts[1].startswith(f'write TCP 127.0.0.1:{peer.port} 10 bytes "01234567"… (+2 bytes)'), texts[1]
    assert texts[2].startswith(f'read TCP 127.0.0.1:{peer.port} 12 bytes "abcdefgh"… (+4 bytes)'), texts[2]
    for text in texts:
        assert "ij" not in text


def test_max_bytes_of_zero_traces_no_bytes_but_still_the_operation(app: QCoreApplication) -> None:
    """maxBytes 0 quotes an empty payload and counts every byte as dropped, rather than skipping the
    operation."""
    peer = LoopbackPeer(payload=b"hello\n")
    peer.start()
    try:
        store, ru = _run(app, _IO_STEP, peer.port, trace=True, max_bytes=0)
    finally:
        peer.stop()

    texts = [ln["text"] for ln in _trace(store, ru)]
    assert texts[1].startswith(f'write TCP 127.0.0.1:{peer.port} 6 bytes ""… (+6 bytes)'), texts[1]
    assert texts[2].startswith(f'read TCP 127.0.0.1:{peer.port} 5 bytes ""… (+5 bytes)'), texts[2]


# --- failures are traced too ----------------------------------------------------


def test_a_refused_open_is_traced_as_a_failed_open(app: QCoreApplication) -> None:
    """An open to a port nothing listens on still gets its line: `open … failed: <err> (n.n ms)`,
    owned by the Attempt; the step carries on through its onError.

    The timeout is well above the 2 s Windows spends retrying a closed port before it says refused:
    with less, the connect times out first and the line is true but says "timed out"."""
    port = _dead_port()
    source = r"""
return ScheduleParams("trace") :setSchedule({
  StepParams("io"):setStepNo("1.")
    :setAction(function()
      ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="", writeTerm=""}, function() return "skipped" end)
      ATE.report("PASS", "open failed but was traced")
    end),
})
"""
    store, ru = _run(app, source, port, trace=True)

    texts = [ln["text"] for ln in _trace(store, ru)]
    assert len(texts) == 1, texts
    assert texts[0].startswith(f"open TCP 127.0.0.1:{port} failed: "), texts[0]
    assert "refused" in texts[0].lower(), texts[0]
    assert _ms(texts[0]) >= 0.0


def test_a_traced_open_that_connects_keeps_its_on_error(app: QCoreApplication) -> None:
    """An `open` that succeeds under `deviceTrace.enabled` still remembers its onError.

    The host keys each open Device's onError in a WeakKeyDictionary of handles, so the handle must be
    weak-referenceable: with tracing on the handle is a `TracedDevice`, and a `__slots__` wrapper
    without `__weakref__` cannot be a weak key - the `ATE.open` call itself raises a TypeError before
    the Device is ever used (packet 29d). The handler given to `open` is the fallback for the Device's
    later calls, so the read that times out here answers with the value it returns."""
    peer = LoopbackPeer()  # accepts, sends nothing
    peer.start()
    source = r"""
return ScheduleParams("trace") :setSchedule({
  StepParams("io"):setStepNo("1.")
    :setAction(function()
      local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="\n", writeTerm=""},
        function(result, args) return "handled: " .. result.err end)
      local r = ATE.read(dev, {timeout=0.2})
      ATE.close(dev)
      ATE.report("PASS", "open onError carried the read: " .. r)
    end),
})
"""
    try:
        store, ru = _run(app, source, peer.port, trace=True)
    finally:
        peer.stop()

    texts = [ln["text"] for ln in _trace(store, ru)]
    assert any(t.startswith(f"open TCP 127.0.0.1:{peer.port} ok") for t in texts), texts
    assert not [ln for ln in store.log_lines(ru) if ln["level"] == "ERROR"], "the open did not raise"
    assert store.attempts(ru)[0]["msg"] == "open onError carried the read: handled: timeout"


def test_a_read_timeout_is_traced_with_its_error_and_the_wait(app: QCoreApplication) -> None:
    """A read that waits out its timeout on a peer that sends nothing traces 0 bytes with
    `err "timeout"`, and its timing is the wait it actually made."""
    peer = LoopbackPeer()  # accepts, sends nothing
    peer.start()
    source = r"""
return ScheduleParams("trace") :setSchedule({
  StepParams("io"):setStepNo("1.")
    :setAction(function()
      local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="\n", writeTerm=""})
      ATE.read(dev, {timeout=0.2}, function(r) return r.err end)
      ATE.close(dev)
      ATE.report("PASS", "timed out")
    end),
})
"""
    try:
        store, ru = _run(app, source, peer.port, trace=True)
    finally:
        peer.stop()

    texts = [ln["text"] for ln in _trace(store, ru)]
    assert texts[1].startswith(f'read TCP 127.0.0.1:{peer.port} 0 bytes "" err "timeout" '), texts[1]
    assert 150.0 <= _ms(texts[1]) < 5000.0, texts[1]  # the timeout really was waited out


def test_a_partial_read_is_traced_with_the_partial_flag(app: QCoreApplication) -> None:
    """A read the deadline catches mid-bytes traces the bytes it took and marks it `partial`."""
    peer = LoopbackPeer(payload=b"abc")  # no terminator: the deadline catches it mid-message
    peer.start()
    source = r"""
return ScheduleParams("trace") :setSchedule({
  StepParams("io"):setStepNo("1.")
    :setAction(function()
      local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="\n", writeTerm=""})
      ATE.read(dev, {timeout=0.2})
      ATE.close(dev)
      ATE.report("PASS", "partial read")
    end),
})
"""
    try:
        store, ru = _run(app, source, peer.port, trace=True)
    finally:
        peer.stop()

    texts = [ln["text"] for ln in _trace(store, ru)]
    assert texts[1].startswith(f'read TCP 127.0.0.1:{peer.port} 3 bytes "abc" partial '), texts[1]
    assert 150.0 <= _ms(texts[1]) < 5000.0, texts[1]


def test_a_write_error_is_traced(app: QCoreApplication) -> None:
    """A write on a connection the peer hung up on traces the payload it could not send, with the
    failure in `err "..."`; the device's own WARNING (packet 17b) is separate and stays a WARNING."""
    peer = LoopbackPeer(payload=b"hi\n", hangup="close")  # replies, then hangs up
    peer.start()
    source = r"""
return ScheduleParams("trace") :setSchedule({
  StepParams("io"):setStepNo("1.")
    :setAction(function()
      local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="\n", writeTerm=""})
      ATE.read(dev, {timeout=2})
      ATE.sleep(0.3)
      ATE.write(dev, {data="bye"}, function(r) return r.err end)
      ATE.close(dev)
      ATE.report("PASS", "write failed but was traced")
    end),
})
"""
    try:
        store, ru = _run(app, source, peer.port, trace=True)
    finally:
        peer.stop()

    texts = [ln["text"] for ln in _trace(store, ru)]
    assert len(texts) == 4, texts
    assert texts[2].startswith(f'write TCP 127.0.0.1:{peer.port} 0 bytes "bye" err '), texts[2]
    for text in texts:
        _ms(text)


def test_a_device_left_open_is_traced_when_the_engine_closes_it(app: QCoreApplication) -> None:
    """The run-end teardown close is traced as well, with no owner (the run as a whole), next to the
    packet 16a WARNING that names the leftover Device."""
    peer = LoopbackPeer(payload=b"hi\n")
    peer.start()
    source = r"""
return ScheduleParams("trace") :setSchedule({
  StepParams("io"):setStepNo("1.")
    :setAction(function()
      ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="", writeTerm=""})
      ATE.report("PASS", "device left open")
    end),
})
"""
    try:
        store, ru = _run(app, source, peer.port, trace=True)
    finally:
        peer.stop()

    lines = [ln for ln in store.log_lines(ru) if ln["source"] == "device"]
    opened = [ln for ln in lines if ln["level"] == "DEBUG" and ln["text"].startswith(f"open TCP 127.0.0.1:{peer.port} ok ")]
    closed = [ln for ln in lines if ln["level"] == "DEBUG" and ln["text"].startswith(f"close TCP 127.0.0.1:{peer.port} ")]
    assert len(opened) == 1 and len(closed) == 1, [ln["text"] for ln in lines]
    assert closed[0]["node_seq"] is None and closed[0]["attempt_no"] is None  # run-level, like the WARNING
    assert any(ln["level"] == "WARNING" and "left open at end of run" in ln["text"] for ln in store.log_lines(ru))
    _ms(closed[0]["text"])


# --- UDP ----------------------------------------------------------------------


def test_udp_operations_are_traced_with_the_same_shape(app: QCoreApplication) -> None:
    """UDP open/write/read/close trace lines name the Device as `UDP <host>:<port>`; a UDP write has
    no terminator, so the quoted bytes are exactly the datagram."""
    peer = UdpLoopbackPeer(datagrams=[b"pong\r\n"])
    peer.start()
    source = r"""
return ScheduleParams("trace") :setSchedule({
  StepParams("io"):setStepNo("1.")
    :setAction(function()
      local dev = ATE.open(DeviceType.UDP, {host="127.0.0.1", port=__PORT__, localPort=0, timeout=5})
      ATE.write(dev, {data="go"})
      ATE.read(dev, {timeout=2})
      ATE.close(dev)
      ATE.report("PASS", "udp traced")
    end),
})
"""
    try:
        store, ru = _run(app, source, peer.port, trace=True)
    finally:
        peer.stop()

    label = f"UDP 127.0.0.1:{peer.port}"
    texts = [ln["text"] for ln in _trace(store, ru)]
    assert len(texts) == 4, texts
    assert texts[0].startswith(f"open {label} ok "), texts[0]
    assert texts[1].startswith(f'write {label} 2 bytes "go" '), texts[1]
    assert texts[2].startswith(f'read {label} 6 bytes "pong\\r\\n" '), texts[2]
    assert texts[3].startswith(f"close {label} "), texts[3]
    for text in texts:
        _ms(text)


def test_a_udp_read_timeout_is_traced(app: QCoreApplication) -> None:
    """A UDP read that waits out its timeout with nothing queued traces 0 bytes with `err "timeout"`,
    exactly as a stream read does; the open and close are still traced."""
    peer = UdpLoopbackPeer()  # listening, but never sends anything
    peer.start()
    source = r"""
return ScheduleParams("trace") :setSchedule({
  StepParams("io"):setStepNo("1.")
    :setAction(function()
      local dev = ATE.open(DeviceType.UDP, {host="127.0.0.1", port=__PORT__, localPort=0, timeout=5})
      ATE.write(dev, {data="go"})
      ATE.read(dev, {timeout=0.2}, function(r) return r.err end)
      ATE.close(dev)
      ATE.report("PASS", "udp timed out")
    end),
})
"""
    try:
        store, ru = _run(app, source, peer.port, trace=True)
    finally:
        peer.stop()

    texts = [ln["text"] for ln in _trace(store, ru)]
    assert len(texts) == 4, texts
    assert texts[2].startswith(f'read UDP 127.0.0.1:{peer.port} 0 bytes "" err "timeout" '), texts[2]
    assert 150.0 <= _ms(texts[2]) < 5000.0, texts[2]


# --- never in a report ----------------------------------------------------------


def test_trace_lines_never_appear_in_the_html_report(app: QCoreApplication) -> None:
    """DEBUG lines stay in the database and the debug window: the rendered report contains no trace
    line, even though the run stored four of them."""
    peer = LoopbackPeer(payload=b"ACME,PSU,1\r\n")
    peer.start()
    try:
        store, ru = _run(app, _IO_STEP, peer.port, trace=True)
    finally:
        peer.stop()

    assert len(_trace(store, ru)) == 4
    html = render_report(store, ru, Config(path=Path("/tmp/ate-trace-report-test")))
    assert "open TCP" not in html and 'bytes "' not in html
    assert "DEBUG" not in html


def test_every_escape_and_raw_byte_truncation() -> None:
    """Each simple escape, \\xNN for the rest, and the cap counted in raw bytes, not escaped text."""
    from ate.devices.tracing import quoted_bytes

    assert quoted_bytes(b'\t\n\r\\"a~', 64) == '"\\t\\n\\r\\\\\\"a~"'
    # Ten control bytes capped at 8: eight \xNN escapes survive (raw cut), then the dropped count.
    assert quoted_bytes(bytes(range(10)), 8) == '"' + "".join(f"\\x{b:02x}" for b in range(8)) + '"\u2026 (+2 bytes)'
