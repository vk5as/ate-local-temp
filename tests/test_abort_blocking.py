"""Packet 13b seam-1: ATE.sleep and a blocked ATE.input both wake on an operator abort.

The abort flag is now waitable (packet 13b): ATE.sleep waits on it, and a blocking ATE.input's
get_raw slices its queue wait so the flag is observed within 50 ms. An abort requested while
either is in flight raises "aborted" into Lua through the same path as the count hook (13c):
the running step records ERROR "Aborted by Operator", every later step NOTRUN, and the run
closes ABORTED / FAIL within a fraction of a second of request_abort(). Verified against a
real lupa runtime and end-to-end through the QThread run engine into a real SQLite file —
no mocks."""

import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QCoreApplication

from ate.engine.run_engine import RunEngine, RunStatus
from ate.storage.store import RunStore


def _drive_abort(
    app: QCoreApplication,
    source: str,
    *,
    pre_abort_s: float = 0.1,
    wait_input_asked: bool = False,
) -> tuple[RunStore, dict, float]:
    """Start a fresh engine for one schedule; request an abort and return (store, run_row, elapsed).

    ``elapsed`` is wall-clock seconds from ``request_abort()`` to the engine reaching FINISHED
    (measured with ``time.monotonic()`` on the main thread). ``wait_input_asked`` waits until the
    worker has announced a step input, so the abort lands while the action is blocked in
    ATE.input. An abort that lands at the step boundary instead of mid-call records the same
    result, so the timing bound is not racing the schedule."""
    captured = {}
    input_asked = threading.Event()

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8")
        engine = RunEngine(store, str(schedule_path))
        engine.run_uuid_reported.connect(_capture)
        if wait_input_asked:
            engine.input_asked.connect(lambda *_args: input_asked.set())

        engine.start()

        deadline = time.time() + 5.0
        seen_asked = input_asked.is_set()
        while (not captured.get("run_uuid") or (wait_input_asked and not seen_asked)) and time.time() < deadline:
            app.processEvents()
            seen_asked = input_asked.is_set()
        assert captured.get("run_uuid"), "run did not report its uuid within 5 s"

        time.sleep(pre_abort_s)  # let the worker reach the blocking ATE call
        t0 = time.monotonic()
        engine.request_abort()

        finished = threading.Event()
        engine.state_changed.connect(lambda status: finished.set() if status == RunStatus.FINISHED else None)
        # The worker emits FINISHED on its own thread; state_changed is a queued signal, so it only
        # reaches this (operator/main) thread once the event loop is pumped. Pump in small slices
        # rather than blocking idle.
        while not finished.is_set() and time.monotonic() - t0 < 5.0:
            app.processEvents()
            finished.wait(0.05)
        assert finished.is_set(), "engine did not reach FINISHED within 5 s after abort"

    return store, store.get_test_run(captured["run_uuid"]), time.monotonic() - t0


def _drive_clean(app: QCoreApplication, source: str) -> tuple[RunStore, dict]:
    """Drive ``source`` to completion with no abort; returns ``(store, run_row)``."""
    captured = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8")
        engine = RunEngine(store, str(schedule_path))
        engine.run_uuid_reported.connect(_capture)
        engine.start()

        deadline = time.time() + 10.0
        while not captured.get("run_uuid") and time.time() < deadline:
            app.processEvents()
        finished = threading.Event()
        engine.state_changed.connect(lambda status: finished.set() if status == RunStatus.FINISHED else None)
        deadline = time.time() + 10.0
        while not finished.is_set() and time.time() < deadline:
            app.processEvents()
            finished.wait(0.05)
        assert finished.is_set(), "engine did not reach FINISHED within 10 s"

    return store, store.get_test_run(captured["run_uuid"])


def _nodes(store: RunStore, run_uuid: str) -> dict[str, dict]:
    conn = store._conn
    conn.row_factory = sqlite3.Row
    return {
        n["step_number"]: dict(n)
        for n in conn.execute(
            "SELECT step_number, result FROM node WHERE run_uuid=? ORDER BY node_seq",
            (run_uuid,),
        ).fetchall()
    }


def _attempt_msg(store: RunStore, run_uuid: str, node_seq: int, attempt_no: int = 1) -> str:
    conn = store._conn
    row = conn.execute(
        "SELECT msg FROM attempt WHERE run_uuid=? AND node_seq=? AND attempt_no=?",
        (run_uuid, node_seq, attempt_no),
    ).fetchone()
    assert row is not None, f"no attempt recorded for node {node_seq}"
    return row["msg"]


def _posttest_outcome(store: RunStore, run_uuid: str) -> str:
    conn = store._conn
    row = conn.execute(
        "SELECT outcome FROM action WHERE run_uuid=? AND phase='post' ORDER BY instance_seq",
        (run_uuid,),
    ).fetchall()
    assert row, "no Post-test Action recorded"
    return row[0]["outcome"]


# --- tests -----------------------------------------------------------------


def test_abort_interrupts_ate_sleep_within_bound(app: QCoreApplication) -> None:
    """Checkbox: an abort during ATE.sleep(30) ends the run ABORTED within 0.5 s of
    request_abort(); the running step records ERROR "Aborted by Operator" and later steps NOTRUN."""
    store, row, elapsed = _drive_abort(app, SLEEP_SOURCE)

    assert row is not None
    assert elapsed < 0.5, f"abort took {elapsed:.3f} s; must stop within 0.5 s"
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "ABORTED"

    nodes = _nodes(store, row["run_uuid"])
    assert nodes["1."]["result"] == "ERROR"  # the running sleep step
    assert _attempt_msg(store, row["run_uuid"], 0) == "Aborted by Operator"
    assert nodes["2."]["result"] == "NOTRUN"  # later steps never ran


def test_abort_interrupts_blocked_ate_input_within_bound(app: QCoreApplication) -> None:
    """Checkbox: an abort while ATE.input blocks also ends the run ABORTED within 0.5 s;
    the running step records ERROR "Aborted by Operator" and later steps NOTRUN."""
    store, row, elapsed = _drive_abort(app, BLOCKED_INPUT_SOURCE, wait_input_asked=True)

    assert row is not None
    assert elapsed < 0.5, f"abort took {elapsed:.3f} s; must stop within 0.5 s"
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "ABORTED"

    nodes = _nodes(store, row["run_uuid"])
    assert nodes["1."]["result"] == "ERROR"  # the running input step
    assert _attempt_msg(store, row["run_uuid"], 0) == "Aborted by Operator"
    assert nodes["2."]["result"] == "NOTRUN"  # later steps never ran


def test_small_sleep_completes_step(app: QCoreApplication) -> None:
    """ATE.sleep(0.05) waits and returns normally: the step passes and the run completes."""
    store, row = _drive_clean(app, SHORT_SLEEP_SOURCE)

    assert row is not None
    assert row["result"] == "PASS"
    assert row["end_reason"] == "COMPLETED"

    nodes = _nodes(store, row["run_uuid"])
    assert nodes["1."]["result"] == "PASS"


def test_negative_sleep_argument_errors_step(app: QCoreApplication) -> None:
    """A negative ATE.sleep argument errors the step (ERROR attempt naming ATE.sleep) without
    aborting the run: later steps still execute and the run ends COMPLETED / FAIL."""
    store, row = _drive_clean(app, NEGATIVE_SLEEP_SOURCE)

    assert row is not None
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "COMPLETED"  # a bad call is a step error, not an abort

    nodes = _nodes(store, row["run_uuid"])
    assert nodes["1."]["result"] == "ERROR"
    assert "ATE.sleep" in _attempt_msg(store, row["run_uuid"], 0)
    assert nodes["2."]["result"] == "PASS"  # the run keeps going after a step error


def test_nonnumber_sleep_argument_errors_step(app: QCoreApplication) -> None:
    """A non-number ATE.sleep argument errors the step the same way."""
    store, row = _drive_clean(app, NONNUMBER_SLEEP_SOURCE)

    assert row is not None
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "COMPLETED"

    nodes = _nodes(store, row["run_uuid"])
    assert nodes["1."]["result"] == "ERROR"
    assert "ATE.sleep" in _attempt_msg(store, row["run_uuid"], 0)
    assert nodes["2."]["result"] == "PASS"


def test_posttest_actions_run_after_abort(app: QCoreApplication) -> None:
    """An abort during a step still lets the Post-test Action run (story 84): its row records ok."""
    store, row, elapsed = _drive_abort(app, POSTTEST_SOURCE)

    assert row is not None
    assert elapsed < 0.5, f"abort took {elapsed:.3f} s; must stop within 0.5 s"
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "ABORTED"

    nodes = _nodes(store, row["run_uuid"])
    assert nodes["1."]["result"] == "ERROR"
    assert _posttest_outcome(store, row["run_uuid"]) == "ok"


# --- schedules ---------------------------------------------------------------


SLEEP_SOURCE = """\
return ScheduleParams("Sleep schedule", "step 1 sleeps 30 s, step 2 would pass")
  :setSchedule({
    StepParams("sleep"):setStepNo("1.")
      :setAction(function() ATE.sleep(30) end),
    StepParams("after"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "should not run") end),
  })
"""

BLOCKED_INPUT_SOURCE = """\
return ScheduleParams("Blocked input schedule", "step 1 blocks on an input, step 2 would pass")
  :setSchedule({
    StepParams("ask"):setStepNo("1.")
      :setInputs({ [1] = { desc = "voltage", type = "INTEGER" } })
      :setAction(function() local v = ATE.input(1) end),
    StepParams("after"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "should not run") end),
  })
"""

SHORT_SLEEP_SOURCE = """\
return ScheduleParams("Short sleep schedule", "step 1 sleeps 0.05 s")
  :setSchedule({
    StepParams("sleep"):setStepNo("1.")
      :setAction(function() ATE.sleep(0.05); ATE.report(Result.PASS, "slept") end),
  })
"""

NEGATIVE_SLEEP_SOURCE = """\
return ScheduleParams("Negative sleep schedule", "step 1 sleeps a negative time")
  :setSchedule({
    StepParams("bad"):setStepNo("1.")
      :setAction(function() ATE.sleep(-1) end),
    StepParams("after"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "still runs") end),
  })
"""

NONNUMBER_SLEEP_SOURCE = """\
return ScheduleParams("Nonnumber sleep schedule", "step 1 sleeps a non-number")
  :setSchedule({
    StepParams("bad"):setStepNo("1.")
      :setAction(function() ATE.sleep("soon") end),
    StepParams("after"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "still runs") end),
  })
"""

POSTTEST_SOURCE = """\
return ScheduleParams("Posttest after abort schedule", "step 1 sleeps, posttest logs")
  :setSchedule({
    StepParams("sleep"):setStepNo("1.")
      :setAction(function() ATE.sleep(30) end),
  })
  :setPostTestAction(function() ATE.log(Log.INFO, "posttest ran") end)
"""
