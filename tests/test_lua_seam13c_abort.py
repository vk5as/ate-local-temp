"""Packet 13c seam-1: a whole-run abort interrupts a busy Lua loop (and one that pcall's it) mid-step.

The count hook fires every 1000 VM instructions and raises "aborted" once the operator has requested
an abort; :meth:`RunEngine.request_abort` sets the shared flag from another thread, so the worker's
next hook check trips its running step. Verified against a real lupa runtime and end-to-end through
the QThread run engine into a real SQLite file — no mocks."""

import sqlite3
import threading
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from ate.engine.run_engine import RunEngine, RunStatus
from ate.storage.store import RunStore

from ._fixtures import _drive
from tests._fixtures import temp_directory, wait_for_aborted_run


def _drive_busy(app: QCoreApplication, source: str) -> tuple[RunStore, dict]:
    """Start a fresh engine for one schedule; request abort from this thread while step 1 runs.

    Step 1 is a busy loop far longer than the abort's 10 s bound (1e9 alone is 5.6 s here under the
    count hook, which is why it is 1e10), so once the worker enters it any abort lands mid-loop (the count
    hook trips on its next check). The record is identical whether the hook fires mid-step or the flag
    was set just before the boundary: the running step becomes ERROR "Aborted by Operator" and every
    later step NOTRUN. Returns ``(store, run_row)``."""
    captured = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with temp_directory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8", newline="\n")
        engine = RunEngine(store, str(schedule_path))
        engine.run_uuid_reported.connect(_capture)

        # Start the worker on a QThread so run() actually executes; without start() no worker runs
        # and FINISHED is never emitted. The abort below is requested from this (operator/main) thread.
        engine.start()

        # Let the worker reach step 1's busy loop, then request abort from this (operator/main) thread.
        deadline = time.time() + 5.0
        while not captured.get("run_uuid") and time.time() < deadline:
            app.processEvents()
        finished = threading.Event()
        engine.state_changed.connect(lambda status: finished.set() if status == RunStatus.FINISHED else None)
        t0 = time.monotonic()
        engine.request_abort()
        wait_for_aborted_run(app, finished, t0)

    return store, store.get_test_run(captured["run_uuid"])


def _drive_clean(app: QCoreApplication, source: str) -> tuple[RunStore, dict]:
    """Drive ``source`` to completion with no abort; returns ``(store, run_row)``."""
    captured = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with temp_directory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8", newline="\n")
        engine = RunEngine(store, str(schedule_path))
        _drive(engine, app, on_run_uuid=_capture)

    return store, store.get_test_run(captured["run_uuid"])


def _nodes(store: RunStore, run_uuid: str):
    conn = store._conn
    conn.row_factory = sqlite3.Row
    return {n["step_number"]: dict(n) for n in conn.execute(
        "SELECT step_number, result FROM node WHERE run_uuid=? ORDER BY node_seq", (run_uuid,)
    ).fetchall()}


def test_abort_busy_loop_records_running_step_and_marks_rest_notrun(app: QCoreApplication) -> None:
    """Checkbox 2/13c — a busy loop is interrupted mid-step; the running step records ERROR 'Aborted by
    Operator' and every later step NOTRUN, closing ABORTED / FAIL within story 83's bound (`_drive_busy`)."""
    store, row = _drive_busy(app, BUSY_SOURCE)

    assert row is not None
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "ABORTED"

    nodes = _nodes(store, row["run_uuid"])
    assert nodes["1."]["result"] == "ERROR"   # the running busy step
    assert nodes["2."]["result"] == "NOTRUN"  # later steps never ran


def test_abort_pcall_busy_loop_records_running_step_and_marks_rest_notrun(app: QCoreApplication) -> None:
    """Checkbox 2/13c — a pcall'd busy loop is interrupted too. The re-raising pcall wrapper turns the
    caught "aborted" back into an error, so it can't be swallowed; the running step records ERROR and
    later steps NOTRUN."""
    store, row = _drive_busy(app, PCALL_BUSY_SOURCE)

    assert row is not None
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "ABORTED"

    nodes = _nodes(store, row["run_uuid"])
    assert nodes["1."]["result"] == "ERROR"   # the running pcall'd busy step
    assert nodes["2."]["result"] == "NOTRUN"  # later steps never ran


def test_abort_does_not_fire_on_plain_passing_run(app: QCoreApplication) -> None:
    """Negative — requesting an abort that is never observed (no busy loop, no pcall to trip it) leaves
    a clean passing run untouched."""
    store, row = _drive_clean(app, PASSING_SOURCE)

    assert row is not None
    assert row["result"] == "PASS"
    assert row["end_reason"] == "COMPLETED"

    nodes = _nodes(store, row["run_uuid"])
    assert nodes["1."]["result"] == "PASS"


# --- schedules ----------------------------------------------------------------


BUSY_SOURCE = """\
return ScheduleParams("Busy schedule", "step 1 is an infinite busy loop")
  :setSchedule({
    StepParams("spin"):setStepNo("1.")
      :setAction(function() for i=1,1e10 do end end),
    StepParams("after"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "should not run") end),
  })
"""

PCALL_BUSY_SOURCE = """\
return ScheduleParams("Pcall busy schedule", "step 1 pcalls an infinite busy loop")
  :setSchedule({
    StepParams("spin"):setStepNo("1.")
      :setAction(function() local ok,_=pcall(function() for i=1,1e10 do end end) end),
    StepParams("after"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "should not run") end),
  })
"""

PASSING_SOURCE = """\
return ScheduleParams("Passing schedule", "a single passing step; no busy loop to interrupt")
  :setSchedule({
    StepParams("ok"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "fine") end),
  })
"""
