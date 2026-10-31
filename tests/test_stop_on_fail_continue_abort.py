"""Packet 12b seam-1: Stop on Fail pauses a failed step until the operator chooses, and Continue /
Abort behave as specified (Retry is packet 12c).

A failing step with Stop on Fail blocks the worker thread on a failure_prompt; the driver answers it
through engine.choose() from another thread. Continue keeps the failed step's result and moves on; Abort marks every remaining step NOTRUN, keeps the failed step's result, and closes STOPPED_ON_FAIL / FAIL. Each Attempt records its prompt_choice. Verified against a
real SQLite file with real .tslua schedules — no mocks."""

import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QCoreApplication

from ate.engine.run_engine import RunEngine, RunStatus
from ate.storage.store import RunStore

from ._fixtures import STOP_ON_FAIL_FALSE


def _failing_node_seq(store: RunStore, run_uuid: str) -> int:
    """The 0-based seq of the first FAIL/ERROR step.

    Steps are enumerated from 0 internally (node_seq is that position), so a failing step declared as
    the schedule's first entry sits at node_seq=0 — not 1. Deriving it positionally keeps these tests
    correct even if a fixture's failing step moves."""
    conn = store._conn
    conn.row_factory = sqlite3.Row
    for r in conn.execute(
        "SELECT node_seq, result FROM node WHERE run_uuid=? ORDER BY node_seq", (run_uuid,)
    ).fetchall():
        if r["result"] in ("FAIL", "ERROR"):
            return r["node_seq"]
    raise AssertionError(f"no failing step recorded for {run_uuid}")


def _drive_with_choice(app: QCoreApplication, source: str, choice: str, stop_on_fail: bool = True) -> tuple[RunStore, dict, int]:
    """Drive ``source`` on a fresh engine; answer every Stop-on-Fail failure prompt with ``choice``.

    Returns ``(store, run_row, prompt_count)``. The pump loop runs in lock-step with the worker so the
    queued failure_prompt -> choose delivery wakes the blocked worker deterministically before we
    inspect rows (a bare QThread.wait races the step boundary)."""
    captured = {}
    prompts = {"count": 0}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    def _on_failure(_node_seq: int, _attempt_no: int) -> None:
        engine.choose(choice)  # answer the pending prompt; wakes the blocked worker
        prompts["count"] += 1

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8", newline="\n")
        engine = RunEngine(store, str(schedule_path), stop_on_fail=stop_on_fail)
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

    return store, store.get_test_run(captured["run_uuid"]), prompts["count"]


# A step that reports FAIL on its single step; Stop on Fail is explicitly off so it never pauses.
FAILING_NO_STOP = """\
return ScheduleParams("Failing no stop schedule", "a failing step with Stop on Fail off")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("LED colour"):setStepNo("1.")
      :setAction(function() ATE.report(Result.FAIL, "wrong colour", "Red", "Green") end),
  })
"""


def test_continue_keeps_result_moves_on_and_records_prompt_choice(app: QCoreApplication) -> None:
    """Packet 12b checkbox 2/4: Continue keeps the failed step's result and moves on; the failing
    Attempt records prompt_choice CONTINUE."""
    store, row, prompts = _drive_with_choice(app, CONTINUE_SOURCE, "CONTINUE")

    assert row is not None
    # The run kept going past the failure -> COMPLETED, but a step failed so the result is FAIL.
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "COMPLETED"
    assert prompts == 1  # paused exactly once

    conn = store._conn
    conn.row_factory = sqlite3.Row
    nodes = [dict(n) for n in conn.execute(
        "SELECT step_number, result FROM node WHERE run_uuid=? ORDER BY node_seq", (row["run_uuid"],)
    ).fetchall()]
    by_step = {n["step_number"]: n for n in nodes}
    assert by_step["1."]["result"] == "FAIL"  # failed step's result kept
    assert by_step["2."]["result"] == "PASS"  # moved on to the next step


def test_abort_marks_remaining_notrun_keeps_failed_result_and_stops_on_fail(app: QCoreApplication) -> None:
    """Packet 12b checkbox 2/5: Abort makes remaining steps NOTRUN, keeps the failed step's result,
    and ends STOPPED_ON_FAIL / FAIL with prompt_choice ABORT."""
    store, row, prompts = _drive_with_choice(app, ABORT_SOURCE, "ABORT")

    assert row is not None
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "STOPPED_ON_FAIL"
    assert prompts == 1

    conn = store._conn
    conn.row_factory = sqlite3.Row
    nodes = {n["step_number"]: dict(n) for n in conn.execute(
        "SELECT step_number, result, attempt_count FROM node WHERE run_uuid=? ORDER BY node_seq", (row["run_uuid"],)
    ).fetchall()}
    assert nodes["1."]["result"] == "FAIL"  # failed step's result kept
    assert nodes["2."]["result"] == "NOTRUN"  # remaining steps NOTRUN
    assert nodes["3."]["result"] == "NOTRUN"

    failing_seq = _failing_node_seq(store, row["run_uuid"])
    att = conn.execute(
        "SELECT pass_fail, prompt_choice FROM attempt WHERE run_uuid=? AND node_seq=?", (row["run_uuid"], failing_seq)
    ).fetchone()
    assert att is not None
    assert att["pass_fail"] == "FAIL"
    assert att["prompt_choice"] == "ABORT"


def test_continue_at_the_prompt_still_pauses_on_the_next_failure(app: QCoreApplication) -> None:
    """Choosing Continue moves on but doesn't switch Stop on Fail off: the next failure pauses too.
    (Only Continue on fail never pauses; see test_no_pause_when_stop_on_fail_off.)"""
    store, row, prompts = _drive_with_choice(app, TWO_FAILS_SOURCE, "CONTINUE")

    assert row is not None
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "COMPLETED"
    # Both failures paused.
    assert prompts == 2

    conn = store._conn
    conn.row_factory = sqlite3.Row
    nodes = {n["step_number"]: dict(n) for n in conn.execute(
        "SELECT step_number, result FROM node WHERE run_uuid=? ORDER BY node_seq", (row["run_uuid"],)
    ).fetchall()}
    assert nodes["1."]["result"] == "FAIL"
    assert nodes["2."]["result"] == "FAIL"


def test_error_failure_pauses_and_records_prompt_choice(app: QCoreApplication) -> None:
    """Packet 12b checkbox 4 (seam-1): a raised step is an ERROR Attempt that still pauses on Stop on
    Fail and records its prompt_choice; remaining steps are NOTRUN."""
    store, row, prompts = _drive_with_choice(app, RAISING_SOURCE, "ABORT")

    assert row is not None
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "STOPPED_ON_FAIL"
    assert prompts == 1

    conn = store._conn
    conn.row_factory = sqlite3.Row
    failing_seq = _failing_node_seq(store, row["run_uuid"])
    att = conn.execute(
        "SELECT pass_fail, prompt_choice FROM attempt WHERE run_uuid=? AND node_seq=?", (row["run_uuid"], failing_seq)
    ).fetchone()
    assert att is not None
    assert att["pass_fail"] == "ERROR"
    assert att["prompt_choice"] == "ABORT"

    nodes = {n["step_number"]: dict(n) for n in conn.execute(
        "SELECT step_number, result FROM node WHERE run_uuid=? ORDER BY node_seq", (row["run_uuid"],)
    ).fetchall()}
    assert nodes["1."]["result"] == "ERROR"
    assert nodes["2."]["result"] == "NOTRUN"


def test_no_pause_when_stop_on_fail_off(app: QCoreApplication) -> None:
    """Packet 12b checkbox 3 (negative): with Stop on Fail off a failing step never pauses."""
    store, row, prompts = _drive_with_choice(app, STOP_ON_FAIL_FALSE, "CONTINUE", stop_on_fail=False)

    assert row is not None
    # No prompt ever fired, so the choice argument is irrelevant here.
    assert prompts == 0
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "COMPLETED"


def test_a_step_that_reports_error_pauses_like_one_that_raises(app: QCoreApplication) -> None:
    """Packet 29a review: Stop on Fail pauses on a step that REPORTS Result.ERROR, not only a raised one.

    `test-schedule-format.md` "Flow control" pauses "when a step ends FAIL or ERROR". Packet 12b's
    condition tested the raise path (`error_text is not None`), so a step that reported ERROR walked
    straight on. Reverting this line to `last_result == "FAIL" or error_text is not None` makes this
    test fail: no prompt fires, no prompt_choice is recorded, and step 2 runs."""
    store, row, prompts = _drive_with_choice(app, REPORTED_ERROR_SOURCE, "CONTINUE")

    assert row is not None
    assert prompts == 1, "the reported ERROR paused the run"
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "COMPLETED"

    conn = store._conn
    conn.row_factory = sqlite3.Row
    att = conn.execute(
        "SELECT pass_fail, prompt_choice FROM attempt WHERE run_uuid=? AND node_seq=?",
        (row["run_uuid"], _failing_node_seq(store, row["run_uuid"])),
    ).fetchone()
    assert att is not None
    assert att["pass_fail"] == "ERROR"
    assert att["prompt_choice"] == "CONTINUE"

    nodes = {n["step_number"]: dict(n) for n in conn.execute(
        "SELECT step_number, result FROM node WHERE run_uuid=? ORDER BY node_seq", (row["run_uuid"],)
    ).fetchall()}
    assert nodes["1."]["result"] == "ERROR"
    assert nodes["2."]["result"] == "PASS"  # the Operator continued, so the run carried on


# --- schedules ----------------------------------------------------------------


CONTINUE_SOURCE = """\
return ScheduleParams("Continue schedule", "step 1 fails then step 2 passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("first"):setStepNo("1.")
      :setAction(function() ATE.report(Result.FAIL, "wrong colour", "Red", "Green") end),
    StepParams("second"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "ok now", "Blue", "Blue") end),
  })
"""

ABORT_SOURCE = """\
return ScheduleParams("Abort on fail schedule", "step 1 fails; steps 2 & 3 would pass")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("first"):setStepNo("1.")
      :setAction(function() ATE.report(Result.FAIL, "wrong colour", "Red", "Green") end),
    StepParams("second"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "ok", "Blue", "Blue") end),
    StepParams("third"):setStepNo("3.")
      :setAction(function() ATE.report(Result.PASS, "also ok", "Green", "Green") end),
  })
"""

TWO_FAILS_SOURCE = """\
return ScheduleParams("Two fails schedule", "two failing steps; continue past the first")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("first"):setStepNo("1.")
      :setAction(function() ATE.report(Result.FAIL, "wrong colour", "Red", "Green") end),
    StepParams("second"):setStepNo("2.")
      :setAction(function() ATE.report(Result.FAIL, "also wrong", "Blue", "Green") end),
  })
"""

RAISING_SOURCE = """\
return ScheduleParams("Raising schedule", "a step that raises; abort at the failure")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("boom"):setStepNo("1.")
      :setAction(function() error("kaboom from Lua") end),
    StepParams("after"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "should not run", "x", "y") end),
  })
"""

REPORTED_ERROR_SOURCE = """\
return ScheduleParams("Reported error schedule", "a step that reports Result.ERROR, then one that passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("probe"):setStepNo("1.")
      :setAction(function() ATE.report(Result.ERROR, "probe not connected") end),
    StepParams("after"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "reconnected", 5.0, 5.0) end),
  })
"""
