"""Packet 13b seam-1: ATE.sleep and a blocked ATE.input both wake on an operator abort.

The abort flag is now waitable (packet 13b): ATE.sleep waits on it, and a blocking ATE.input's
get_raw slices its queue wait so the flag is observed within 50 ms. An abort requested while
either is in flight raises "aborted" into Lua through the same path as the count hook (13c):
the running step records ERROR "Aborted by Operator", every later step NOTRUN, and the run
closes ABORTED / FAIL within story 83's bound of request_abort(): it should stop within 0.5 s (past
that a test warns, and stays visible in CI's summary) and must stop within 10 s (past that it fails).
Verified against a real lupa runtime and end-to-end through the QThread run engine into a real SQLite
file — no mocks, but for the stalls the tests of the bound inject."""

import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

import pytest
from PySide6.QtCore import QCoreApplication

from ate.engine.lua_host import _AbortFlag, _AteTable
from ate.engine.run_engine import RunEngine, RunStatus
from ate.storage.store import RunStore
from tests._fixtures import ABORT_MUST_STOP_S, ABORT_SHOULD_STOP_S, SlowAbortWarning, scratch_directory, wait_for_aborted_run


def _drive_abort(
    app: QCoreApplication,
    source: str,
    *,
    wait_input_asked: bool = False,
    prepare: Callable[[RunStore, threading.Event, list], None] | None = None,
    must_stop_s: float = ABORT_MUST_STOP_S,
) -> tuple[RunStore, dict]:
    """Start a fresh engine for one schedule, abort it once its action is blocked in an ATE call, and
    return ``(store, run_row)``, asserting the abort's bound.

    ``prepare(store, blocked, timeline)`` is for a test that decides itself when to abort, by setting
    ``blocked`` from whatever it injected.

    The clock runs from ``request_abort()`` to the engine reaching FINISHED, read on the main thread
    (``time.monotonic()``). It starts only when the worker has shown it is blocked: inside ATE.sleep's
    wait on the abort flag, or, with ``wait_input_asked``, having announced a step input. So nothing a
    run does before its action starts, on any machine, is counted as abort latency, and whatever is
    counted is what an Operator would wait for. Past 10 s it fails and past 0.5 s it warns, each printing
    the worker's store calls, which tell a stalled write from a late wake-up."""
    captured = {}
    blocked = threading.Event()
    timeline: list[tuple[float, str]] = []

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with scratch_directory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8", newline="\n")
        engine = RunEngine(store, str(schedule_path))
        engine.run_uuid_reported.connect(_capture)
        finished = threading.Event()
        engine.state_changed.connect(lambda status: finished.set() if status == RunStatus.FINISHED else None)
        if wait_input_asked:
            engine.input_asked.connect(lambda *_args: blocked.set())
        real_wait, real_run = engine._abort_flag.wait, store._run

        def _wait(timeout: float | None = None) -> bool:
            blocked.set()
            woke = real_wait(timeout)
            if woke:
                timeline.append((time.monotonic(), "ATE.sleep woke"))
            return woke

        def _run(stmt) -> object:
            result = real_run(stmt)
            timeline.append((time.monotonic(), f"{type(stmt).__name__} {getattr(getattr(stmt, 'table', None), 'name', '?')} done"))
            return result

        engine._abort_flag.wait, store._run = _wait, _run
        if prepare is not None:
            prepare(store, blocked, timeline)

        engine.start()

        deadline = time.time() + 5.0
        while not blocked.is_set() and time.time() < deadline:
            app.processEvents()
            blocked.wait(0.01)
        assert blocked.is_set(), "the action did not block in an ATE call within 5 s"

        t0 = time.monotonic()
        engine.request_abort()

        try:
            wait_for_aborted_run(app, finished, t0, must_stop_s=must_stop_s, timeline=timeline)
        finally:
            engine.wait(60_000)  # a failed bound still must not leave the worker writing to a removed directory
        app.processEvents()  # the uuid signal, if the wait above saw the action block first
        assert captured.get("run_uuid"), "run did not report its uuid"

    return store, store.get_test_run(captured["run_uuid"])


def _drive_clean(app: QCoreApplication, source: str) -> tuple[RunStore, dict]:
    """Drive ``source`` to completion with no abort; returns ``(store, run_row)``."""
    captured = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with scratch_directory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8", newline="\n")
        engine = RunEngine(store, str(schedule_path))
        engine.run_uuid_reported.connect(_capture)
        finished = threading.Event()
        engine.state_changed.connect(lambda status: finished.set() if status == RunStatus.FINISHED else None)
        engine.start()

        deadline = time.time() + 10.0
        while not captured.get("run_uuid") and time.time() < deadline:
            app.processEvents()
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
    """Checkbox: an abort during ATE.sleep(30) ends the run ABORTED within story 83's bound of
    request_abort(); the running step records ERROR "Aborted by Operator" and later steps NOTRUN."""
    store, row = _drive_abort(app, SLEEP_SOURCE)

    assert row is not None
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "ABORTED"

    nodes = _nodes(store, row["run_uuid"])
    assert nodes["1."]["result"] == "ERROR"  # the running sleep step
    assert _attempt_msg(store, row["run_uuid"], 0) == "Aborted by Operator"
    assert nodes["2."]["result"] == "NOTRUN"  # later steps never ran


def test_abort_interrupts_blocked_ate_input_within_bound(app: QCoreApplication) -> None:
    """Checkbox: an abort while ATE.input blocks also ends the run ABORTED within story 83's bound;
    the running step records ERROR "Aborted by Operator" and later steps NOTRUN."""
    store, row = _drive_abort(app, BLOCKED_INPUT_SOURCE, wait_input_asked=True)

    assert row is not None
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "ABORTED"

    nodes = _nodes(store, row["run_uuid"])
    assert nodes["1."]["result"] == "ERROR"  # the running input step
    assert _attempt_msg(store, row["run_uuid"], 0) == "Aborted by Operator"
    assert nodes["2."]["result"] == "NOTRUN"  # later steps never ran


def _stall_in_the_commit(seconds: float) -> Callable[[RunStore, threading.Event, list], None]:
    """Stall the worker inside the store call that writes ``LOGGED_BEFORE_SLEEP``, as a slow disk would."""

    def prepare(store: RunStore, blocked: threading.Event, timeline: list) -> None:
        real_insert = store.insert_log_lines

        def insert_log_lines(run_uuid, seq, lines, **kwargs) -> None:
            if any(line["text"] == LOGGED_BEFORE_SLEEP for line in lines):
                timeline.append((time.monotonic(), f"store call stalled for {seconds} s"))
                blocked.set()
                time.sleep(seconds)
            real_insert(run_uuid, seq, lines, **kwargs)

        store.insert_log_lines = insert_log_lines

    return prepare


def test_abort_during_a_stalled_store_call_ends_the_run_and_warns(app: QCoreApplication) -> None:
    """The window `_drive_abort`'s own clock never opens: ATE.sleep commits just before its wait, so an
    abort can land inside a commit. It cannot cut one short, so a 2 s stall misses the 0.5 s aim, says
    so with what the worker was doing, and still meets the 10 s requirement with a clean record."""
    with pytest.warns(SlowAbortWarning, match=r"abort took \d\.\d+ s; should stop within 0\.5 s; worker: .*store call stalled") as seen:
        store, row = _drive_abort(app, COMMIT_THEN_SLEEP_SOURCE, prepare=_stall_in_the_commit(2.0))

    (warning,) = [w for w in seen if w.category is SlowAbortWarning]
    elapsed = float(str(warning.message).split()[2])
    assert 1.5 < elapsed < ABORT_MUST_STOP_S, f"the stall was not what the abort waited for: {warning.message}"
    assert row["result"] == "FAIL" and row["end_reason"] == "ABORTED"
    nodes = _nodes(store, row["run_uuid"])
    assert nodes["1."]["result"] == "ERROR" and nodes["2."]["result"] == "NOTRUN"
    assert LOGGED_BEFORE_SLEEP in [line["text"] for line in store.log_lines(row["run_uuid"])], "the stalled write was lost"


def test_a_stall_past_the_must_stop_bound_fails_and_the_bound_is_the_requirement(app: QCoreApplication) -> None:
    """The bound can fail: the same stall outlasting a short injected bound does, naming what the
    worker was doing. The default it stands in for is what story 83 says."""
    with pytest.raises(AssertionError, match=r"must stop within 0\.3 s; worker: .*store call stalled"):
        _drive_abort(app, COMMIT_THEN_SLEEP_SOURCE, prepare=_stall_in_the_commit(1.0), must_stop_s=0.3)
    assert (ABORT_SHOULD_STOP_S, ABORT_MUST_STOP_S) == (0.5, 10.0)


def test_abort_interrupts_a_busy_loop_with_no_ate_calls_within_bound(app: QCoreApplication, monkeypatch) -> None:
    """Story 83's "even in a busy Lua loop". The clock starts inside the loop: the count hook reads the
    flag once per 1000 instructions, so a thousand reads is more Lua than the whole schedule outside it."""
    real_aborted = _AbortFlag.aborted.fget
    reads: list[None] = []
    in_loop: list[threading.Event] = []

    def counted(flag: _AbortFlag) -> bool:
        reads.append(None)
        if len(reads) >= 1000 and in_loop:
            in_loop[0].set()
        return real_aborted(flag)

    monkeypatch.setattr(_AbortFlag, "aborted", property(counted))
    store, row = _drive_abort(app, BUSY_LOOP_SOURCE, prepare=lambda _store, blocked, _timeline: in_loop.append(blocked))

    assert row["result"] == "FAIL" and row["end_reason"] == "ABORTED"
    nodes = _nodes(store, row["run_uuid"])
    assert nodes["1."]["result"] == "ERROR" and nodes["2."]["result"] == "NOTRUN"
    assert _attempt_msg(store, row["run_uuid"], 0) == "Aborted by Operator"


def test_small_sleep_completes_step(app: QCoreApplication, monkeypatch) -> None:
    """A fractional ATE.sleep really waits and returns normally: the step passes, the run completes,
    and the call itself takes at least the time asked for (so a no-op or a sleep that truncates
    fractions fails). The call is timed on the worker: two whole runs compared from this thread differ
    by far more than the sleep on a Windows runner, the first one carrying the cold start."""
    real_sleep = _AteTable.sleep
    slept: list[float] = []

    def _timed_sleep(table: _AteTable, seconds: float) -> bool:
        t0 = time.monotonic()
        result = real_sleep(table, seconds)
        slept.append(time.monotonic() - t0)
        return result

    monkeypatch.setattr(_AteTable, "sleep", _timed_sleep)
    store, row = _drive_clean(app, SHORT_SLEEP_SOURCE.replace("ATE.sleep(0.05)", "ATE.sleep(0.4)"))
    assert len(slept) == 1 and slept[0] >= 0.4, slept

    assert row is not None
    assert row["result"] == "PASS"
    assert row["end_reason"] == "COMPLETED"

    nodes = _nodes(store, row["run_uuid"])
    assert nodes["1."]["result"] == "PASS"


def test_huge_sleep_argument_errors_step_naming_ate_sleep(app: QCoreApplication) -> None:
    """A finite but enormous ATE.sleep (past what a wait can express) errors the step with a message
    naming ATE.sleep, not a raw overflow error."""
    store, row = _drive_clean(app, NEGATIVE_SLEEP_SOURCE.replace("ATE.sleep(-1)", "ATE.sleep(1e300)"))
    msgs = [a["msg"] for a in store.attempts(row["run_uuid"]) if a["pass_fail"] == "ERROR"]
    assert msgs and "ATE.sleep" in msgs[0], msgs


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
    """An abort during a step still lets the Post-test Action run (story 84), within the abort's
    bound: its row records ok."""
    store, row = _drive_abort(app, POSTTEST_SOURCE)

    assert row is not None
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

LOGGED_BEFORE_SLEEP = "committed just before the wait"

COMMIT_THEN_SLEEP_SOURCE = f"""\
return ScheduleParams("Commit schedule", "step 1 logs, so its sleep commits before it waits")
  :setSchedule({{
    StepParams("sleep"):setStepNo("1.")
      :setAction(function() ATE.log(Log.INFO, "{LOGGED_BEFORE_SLEEP}"); ATE.sleep(30) end),
    StepParams("after"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "should not run") end),
  }})
"""

# Outlasts the 10 s bound by far (1e9 is 5.6 s here, under the count hook), and still ends if abort breaks.
BUSY_LOOP_SOURCE = """\
return ScheduleParams("Busy schedule", "step 1 spins and makes no ATE call")
  :setSchedule({
    StepParams("spin"):setStepNo("1.")
      :setAction(function() for i=1,1e10 do end end),
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
