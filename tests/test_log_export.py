"""Packet 24a — Run log exporter (the UI-free module behind the History export dialog).

Seam-1: a real recorded run driven through the RunEngine against a loopback TCP peer —
Pre-test and Post-test Actions, a step logging at every level, a step whose device read
overflows the receive buffer (source ``device``), a step left open at run end (source
``ate``), and a retried step — is exported through :mod:`ate.report.log_export` and checked
byte-for-byte in CSV, JSON Lines and Text, plus each of the dialog's three filter groups.
Real engine, real SQLite, no mocks."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

import pytest

from PyQt6.QtCore import QCoreApplication

from ate.engine.run_engine import RunEngine, RunStatus
from ate.report.log_export import build_export_rows, export_log
from ate.storage.store import RunStore

from .test_tcp_read_write import LoopbackPeer


# The 64-byte cap against a 100-byte reply: the device read asks for exactly the cap, so it
# completes without a partial line, while the reader still drops the last 36 bytes (a real
# receive-buffer overflow, source device); the step never closes, which also logs a run-level
# "left open" WARNING (source ate).
EXPORT_SOURCE = """\
local attempts = 0
return ScheduleParams("Export log schedule", "pre/post actions, every level, a device and a retried step")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("levels"):setStepNo("1.")
      :setAction(function()
        ATE.log(Log.DEBUG, "debug line")
        ATE.log(Log.INFO, "info line")
        print("print line\\neight line two")
        ATE.log(Log.WARNING, "warn line")
        ATE.log(Log.ERROR, "error line")
        ATE.report(Result.PASS, "ok")
      end),
    StepParams("retry"):setStepNo("2.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.log(Log.INFO, "first attempt")
          ATE.report(Result.FAIL, "cold joint", "0.2V", "2.4V")
        else
          ATE.log(Log.INFO, "second attempt")
          ATE.report(Result.PASS, "ok now")
        end
      end),
    StepParams("device io"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="", writeTerm=""})
        local r = ATE.read(dev, {size = 64, timeout = 1})
        ATE.close(dev)
        ATE.report(Result.PASS, "read "..tostring(r.count))
      end),
    StepParams("open only"):setStepNo("4.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="", writeTerm=""})
        ATE.report(Result.PASS, "left open")
      end),
  })
  :setPreTestAction(function(passFail) ATE.log(Log.INFO, "pretest "..passFail) end)
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "posttest "..passFail) end)
"""


def _drive_export_run(app: QCoreApplication, source: str, *, port: int, buffer_bytes: int = 64) -> tuple[RunStore, dict]:
    """Drive ``source`` on a fresh engine with the UUT/offset this packet asserts on; answer the
    retry step's failure prompt RETRY. Returns ``(store, run_row)``."""
    captured: dict[str, str] = {}
    prompts: list[tuple[int, int]] = []

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    def _on_failure(node_seq: int, attempt_no: int) -> None:
        prompts.append((node_seq, attempt_no))
        engine.choose("RETRY")  # the only prompt this schedule raises

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source.replace(LoopbackPeer.PORT_PLACEHOLDER, str(port)), encoding="utf-8")
        engine = RunEngine(
            store,
            str(schedule_path),
            stop_on_fail=True,
            uut_type="UUT-A",
            uut_serial="SN-42",
            operator="me",
            utc_offset="+10:30",
            receive_buffer_bytes=buffer_bytes,
        )
        engine.run_uuid_reported.connect(_capture)
        engine.failure_prompt.connect(_on_failure)
        finished = threading.Event()
        engine.state_changed.connect(lambda status: finished.set() if status == RunStatus.FINISHED else None)
        engine.start()
        deadline = time.time() + 10.0
        while not finished.is_set():
            app.processEvents()
            if time.time() > deadline:
                raise TimeoutError("engine did not reach FINISHED within 10 s")

    assert captured.get("run_uuid") is not None, "engine never reported a run key"
    assert len(prompts) == 1, "the retry step must pause exactly once"
    return store, store.get_test_run(captured["run_uuid"])


@pytest.fixture(scope="module")
def export_run(app: QCoreApplication) -> tuple[RunStore, dict]:
    """One real recorded run shared by every test in this module (read-only after)."""
    peer = LoopbackPeer(payload=b"0123456789" * 10)  # 100 bytes; the 64-byte cap drops 36
    peer.start()
    try:
        store, row = _drive_export_run(app, EXPORT_SOURCE, port=peer.port)
    finally:
        peer.stop()
    assert row is not None
    assert row["result"] == "PASS" and row["end_reason"] == "COMPLETED"
    return store, row


def _local(dt_iso: str, offset: str) -> str:
    """The spec's local time: the stored UTC instant plus the run's offset, with ms."""
    dt = datetime.fromisoformat(dt_iso).astimezone(timezone.utc)
    sign = -1 if offset.startswith("-") else 1
    minutes = sign * (int(offset[1:3]) * 60 + int(offset[4:6]))
    local = dt + timedelta(minutes=minutes)
    return f"{local.strftime('%Y-%m-%d %H:%M:%S')}.{local.microsecond // 1000:03d} {offset}"


def test_rows_carry_every_field_in_recorded_order(export_run) -> None:
    """Checkbox 1 — every row carries the spec's fields: local time with ms and the run's
    offset, level, source, owner, schedule, Step Number, Step Name, Attempt and the unchanged
    text; rows keep recorded (seq) order, not owner grouping."""
    store, row = export_run
    lines = store.log_lines(row["run_uuid"])
    rows = build_export_rows(store, row["run_uuid"])

    assert len(rows) == len(lines) == 11
    # One line per recorded line, in order (the run-level WARNING is last).
    for line, exported in zip(lines, rows):
        assert exported.level == line["level"]
        assert exported.source == line["source"]
        assert exported.text == line["text"]  # unchanged, including the newline in the print
        assert exported.time == _local(line["at"], row["utc_offset"])

    # Owner: Pre-test, then the step lines, then Post-test, then the run-level line.
    assert [r.owner for r in rows] == [
        "Pre-test",
        "Step", "Step", "Step", "Step", "Step",
        "Step", "Step",
        "Step",
        "Post-test",
        "Run",
    ]

    # Schedule name: the run's schedule for every owned line, blank for the Run owner.
    assert [r.schedule for r in rows] == [row["schedule_name"]] * 10 + [None]

    # Step Number / Name / Attempt: blank unless the line is owned by a step.
    assert [r.step_number for r in rows] == [None] + ["1."] * 5 + ["2.", "2.", "3."] + [None, None]
    assert [r.step_name for r in rows] == [
        None] + ["levels"] * 5 + ["retry", "retry", "device io"] + [None, None]
    assert [r.attempt for r in rows] == [None, 1, 1, 1, 1, 1, 1, 2, 1, None, None]

    # The retried step's two lines carry their own Attempt numbers; the multi-line print
    # keeps its newline untouched.
    assert rows[3].text == "print line\neight line two"
    assert rows[6].attempt == 1 and rows[7].attempt == 2


def test_local_time_uses_the_run_offset(export_run) -> None:
    """Checkbox 1 — the offset is the run's, not the host's: a +10:30 run shows the instant
    plus ten and a half hours, formatted ``YYYY-MM-DD HH:MM:SS.mmm +10:30``."""
    store, row = export_run
    line = store.log_lines(row["run_uuid"])[0]
    row0 = build_export_rows(store, row["run_uuid"])[0]

    expected = _local(line["at"], row["utc_offset"])
    assert row0.time == expected
    # The format itself: a space before a signed, colon-separated offset ending in :30.
    date, clock, offset = row0.time.split(" ")
    assert len(date) == 10 and len(clock.split(":")[0]) == 2
    assert offset.startswith(("-", "+")) and offset[1:3].isdigit() and offset.endswith(":30")
    # The milliseconds are the last three digits of the clock (after the dot at index 8).
    assert clock[9:12].isdigit() and len(clock) == 12
