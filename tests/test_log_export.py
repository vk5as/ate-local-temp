"""Packet 24a — Run log exporter (the UI-free module behind the History export dialog).

Seam-1: a real recorded run driven through the RunEngine against a loopback TCP peer —
Pre-test and Post-test Actions, a step logging at every level, a step whose device read
overflows the receive buffer (source ``device``), a step left open at run end (source
``ate``), and a retried step — is exported through :mod:`ate.report.log_export` and checked
byte-for-byte in CSV, JSON Lines and Text, plus each of the dialog's three filter groups.
Real engine, real SQLite, no mocks."""

from __future__ import annotations

import csv
import io
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

import pytest

from PyQt6.QtCore import QCoreApplication

from ate.engine.run_engine import RunEngine, RunStatus
from ate.report.log_export import build_export_rows, export_log

# The field names exactly as log-export.md lines 32-42 list them; pinned here (not imported from the
# module) so a rename in the exporter fails the tests.
SPEC_FIELDS = ["time", "level", "source", "owner", "schedule", "step_number", "step_name", "attempt", "text"]
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


# --- Checkbox 2: the dialog's three filter groups -----------------------------------------


def test_level_filter(export_run) -> None:
    """Checkbox 2 — levels: only the ticked levels are exported, recorded order kept."""
    store, row = export_run
    uuid = row["run_uuid"]

    assert [r.level for r in build_export_rows(store, uuid, levels={"DEBUG"})] == ["DEBUG"]

    wide = build_export_rows(store, uuid, levels={"WARNING", "ERROR"})
    assert len(wide) == 4
    assert [r.level for r in wide] == ["WARNING", "ERROR", "WARNING", "WARNING"]
    assert [r.source for r in wide] == ["schedule", "schedule", "device", "ate"]


def test_source_filter(export_run) -> None:
    """Checkbox 2 — sources: only the ticked sources are exported."""
    store, row = export_run
    uuid = row["run_uuid"]

    dev = build_export_rows(store, uuid, sources={"device"})
    assert len(dev) == 1
    assert dev[0].level == "WARNING" and dev[0].source == "device" and dev[0].owner == "Step"
    assert dev[0].step_number == "3." and "overflow" in dev[0].text

    sched = build_export_rows(store, uuid, sources={"schedule"})
    assert len(sched) == 9  # everything but the device overflow and the run-level line
    assert all(r.source == "schedule" for r in sched)


def test_owner_filter(export_run) -> None:
    """Checkbox 2 — owners: one node ticks its lines; a step brings all its Attempts."""
    store, row = export_run
    uuid = row["run_uuid"]

    # One step: both Attempts exported, in order, each with its own number.
    retried = build_export_rows(store, uuid, owners={"step:1"})
    assert len(retried) == 2
    assert [r.attempt for r in retried] == [1, 2]
    assert all(r.owner == "Step" and r.step_number == "2." for r in retried)

    device_step = build_export_rows(store, uuid, owners={"step:2"})
    assert len(device_step) == 1 and device_step[0].source == "device"
    assert device_step[0].step_number == "3." and device_step[0].attempt == 1

    phases = build_export_rows(store, uuid, owners={"pretest:0", "posttest:0"})
    assert [r.owner for r in phases] == ["Pre-test", "Post-test"]
    assert all(r.schedule == row["schedule_name"] for r in phases)

    run = build_export_rows(store, uuid, owners={"run"})
    assert len(run) == 1 and run[0].owner == "Run"
    assert run[0].schedule is None and run[0].attempt is None
    assert "left open at end of run" in run[0].text


def test_combined_filters(export_run) -> None:
    """Checkbox 2 — the groups intersect: a line is exported only when all ticked groups allow it."""
    store, row = export_run
    uuid = row["run_uuid"]

    picked = build_export_rows(store, uuid, levels={"INFO"}, sources={"schedule"}, owners={"step:0"})
    assert [r.text for r in picked] == ["info line", "print line\neight line two"]

    only_run = build_export_rows(store, uuid, levels={"WARNING"}, sources={"ate"})
    assert [r.owner for r in only_run] == ["Run"]

    # A contradiction selects nothing: export_log reports 0 and writes header-only files.
    td = tempfile.mkdtemp()
    for fmt, name in (("csv", "r.csv"), ("jsonl", "r.jsonl"), ("text", "r.txt")):
        path = Path(td) / name
        assert export_log(store, uuid, path, fmt, levels={"DEBUG"}, sources={"device"}) == 0

    assert (Path(td) / "r.csv").read_bytes()[:3] == b"\xef\xbb\xbf"
    assert (Path(td) / "r.csv").read_text(encoding="utf-8-sig").splitlines() == [",".join(SPEC_FIELDS)]
    assert (Path(td) / "r.jsonl").read_bytes() == b""
    text_lines = (Path(td) / "r.txt").read_text(encoding="utf-8").splitlines()
    assert text_lines[0].startswith("Run UUID: ")
    assert text_lines[4] == "Filters: levels=DEBUG; sources=device"
    assert len(text_lines) == 6  # header only, no log lines


def test_export_log_rejects_unknown_format(export_run) -> None:
    store, row = export_run
    td = tempfile.mkdtemp()
    with pytest.raises(ValueError, match="unknown export format"):
        export_log(store, row["run_uuid"], Path(td) / "r.docx", "docx")


# --- Checkbox 3: the three writers, byte-for-byte ----------------------------------------


def test_csv_output(export_run) -> None:
    """Checkbox 3 — CSV: UTF-8 with BOM, a header row of the field names, minimal quoting, no
    filter record; the multi-line print's newline stays in one (quoted) cell."""
    store, row = export_run
    uuid = row["run_uuid"]
    td = tempfile.mkdtemp()
    path = Path(td) / "run.csv"
    assert export_log(store, uuid, path, "csv") == 11

    raw = path.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf"  # the BOM Excel expects

    text = raw.decode("utf-8-sig")
    # CRLF record ends are stripped so csv sees the fields the way the writer produced them;
    # the quoted text cell's embedded newline survives the round trip.
    rows = [list(r) for r in csv.reader(io.StringIO(text.replace("\r\n", "\n")))]
    assert rows[0] == SPEC_FIELDS
    rows = rows[1:]
    assert len(rows) == 11
    assert [r[3] for r in rows] == ["Pre-test"] + ["Step"] * 8 + ["Post-test", "Run"]
    assert [r[4] for r in rows] == [row["schedule_name"]] * 10 + [""]
    assert [r[5] for r in rows] == [""] + ["1."] * 5 + ["2.", "2.", "3."] + ["", ""]
    assert [r[6] for r in rows] == [""] + ["levels"] * 5 + ["retry", "retry", "device io"] + ["", ""]
    assert [r[7] for r in rows] == ["", "1", "1", "1", "1", "1", "1", "2", "1", "", ""]
    # The print row is quoted; its text cell keeps the newline untouched.
    assert rows[3][8] == "print line\neight line two"
    assert '"print line' in text


def test_jsonl_output(export_run) -> None:
    """Checkbox 3 — JSON Lines: one object per line, the field names above, blanks are null,
    Step Number a string, Attempt a number, and the multi-line print is one physical line."""
    store, row = export_run
    uuid = row["run_uuid"]
    td = tempfile.mkdtemp()
    path = Path(td) / "run.jsonl"
    assert export_log(store, uuid, path, "jsonl") == 11

    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    lines = raw.decode("utf-8").splitlines()
    assert len(lines) == 11

    objs = [json.loads(line) for line in lines]
    assert all(list(o) == SPEC_FIELDS for o in objs)  # names and order

    # Blanks are null, not empty strings.
    assert objs[0]["owner"] == "Pre-test" and objs[0]["step_number"] is None and objs[0]["attempt"] is None
    assert objs[10]["owner"] == "Run" and objs[10]["schedule"] is None and objs[10]["attempt"] is None

    # Step fields: a string Step Number, a numeric Attempt.
    assert objs[6]["step_number"] == "2." and objs[6]["attempt"] == 1 and isinstance(objs[6]["attempt"], int)
    assert objs[7]["step_number"] == "2." and objs[7]["attempt"] == 2

    # The multi-line print's newline survives as a real newline in the decoded object.
    assert objs[3]["text"] == "print line\neight line two"


def test_text_output(export_run) -> None:
    """Checkbox 3 — Text: a header naming the run, the UUT and the filters (none), then one line
    per log line in the spec's layout, with the continuation of the multi-line print indented
    under its text and each Step owner written as ``Step <Step Number>#<attempt>``."""
    store, row = export_run
    uuid = row["run_uuid"]
    td = tempfile.mkdtemp()
    path = Path(td) / "run.txt"
    assert export_log(store, uuid, path, "text") == 11

    lines = path.read_text(encoding="utf-8").split("\n")
    assert lines[-1] == ""  # trailing newline
    lines = lines[:-1]
    assert len(lines) == 18  # 6 header lines + 11 rows + the one continuation line

    assert lines[0] == f"Run UUID: {row['run_uuid']}"
    assert lines[1] == f"UUT Type: {row['uut_type']}, Serial: {row['uut_serial']}"
    assert lines[2] == f"Test Schedule: {row['schedule_name']}"
    assert lines[3].startswith("Start: ")
    assert lines[4] == "Filters: none"
    assert lines[5] == ""

    recs = store.log_lines(uuid)
    times = [_local(rec["at"], row["utc_offset"]) for rec in recs]
    owners = (
        ["Pre-test"] + ["Step 1.#1"] * 5 + ["Step 2.#1", "Step 2.#2"] + ["Step 3.#1"] + ["Post-test"] + ["Run"]
    )

    b = 6
    for i, rec in enumerate(recs):
        prefix = f"{times[i]}  {rec['level']:<9}{rec['source']:<10}{owners[i]:<12}"
        first, *rest = rec["text"].split("\n")
        assert lines[b] == prefix + first
        b += 1
        for continuation in rest:
            assert lines[b] == " " * len(prefix) + continuation
            b += 1
    assert b == len(lines)
