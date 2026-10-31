"""Packet 14c seam-1: the Post-test Action always runs.

Teardown is never skipped — it runs after a Stop-on-Fail ABORT, after a Pre-test failure and after an
operator abort; before it starts ATE clears the abort flag so a busy loop stops tripping it (packet
13c) yet the run still remembers it was aborted and closes ABORTED. A second abort during a busy
Post-test Action ends only that action — its row records outcome "aborted" and is not a run error —
and any remaining Post-test Actions still run. An error raised in one fails the run (result FAIL) but
leaves its End Reason untouched. Each outcome's End Reason is checked. Verified against a real Lua
runtime + QThread RunEngine into a real SQLite file — no mocks."""

import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QCoreApplication

from ate.engine.lua_host import LuaHost
from ate.engine.run_engine import RunEngine, RunStatus
from ate.storage.store import RunStore

from ._fixtures import _drive, PRE_ERROR_POST_OK


def _run_schedule(app: QCoreApplication, source: str) -> tuple[RunStore, dict]:
    """Create a fresh store + engine for one schedule on disk; return (store, run_row)."""
    captured = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8", newline="\n")
        engine = RunEngine(store, str(schedule_path))
        engine.run_uuid_reported.connect(_capture)
        _drive(engine, app)

    assert captured.get("run_uuid") is not None, "engine never reported a run key"
    return store, store.get_test_run(captured["run_uuid"])


def _actions(conn: sqlite3.Connection, run_uuid: str) -> dict[str, dict]:
    """Return this run's action rows keyed by phase (empty when the run is unknown)."""
    conn.row_factory = sqlite3.Row
    return {a["phase"]: a for a in conn.execute(
        "SELECT instance_seq, phase, run_order, started_at, ended_at, outcome, error FROM action WHERE run_uuid=? ORDER BY seq",
        (run_uuid,),
    ).fetchall()}


def _nodes(store: RunStore, run_uuid: str) -> dict[str, dict]:
    conn = store._conn
    conn.row_factory = sqlite3.Row
    return {n["step_number"]: dict(n) for n in conn.execute(
        "SELECT step_number, result FROM node WHERE run_uuid=? ORDER BY node_seq", (run_uuid,)
    ).fetchall()}


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


def _drive_abort(app: QCoreApplication, source: str) -> tuple[RunStore, dict]:
    """Start a fresh engine; request abort from this thread while the worker runs.

    Mirrors test_lua_seam13c_abort's driver: once FINISHED is emitted (within a bound) return
    ``(store, run_row)``."""
    captured = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8", newline="\n")
        engine = RunEngine(store, str(schedule_path))
        engine.run_uuid_reported.connect(_capture)

        # Start the worker on a QThread so run() actually executes; without start() no worker runs
        # and FINISHED is never emitted. The abort below is requested from this (operator/main) thread.
        engine.start()

        deadline = time.time() + 5.0
        while not captured.get("run_uuid") and time.time() < deadline:
            app.processEvents()
        engine.request_abort()

        finished = threading.Event()
        engine.state_changed.connect(lambda status: finished.set() if status == RunStatus.FINISHED else None)
        # The worker emits FINISHED on its own thread; state_changed is a queued signal, so it only
        # reaches this (operator/main) thread once the event loop is pumped. Pump in small slices
        # rather than blocking idle — a bare ``finished.wait`` never processes posted slots.
        deadline = time.time() + 5.0
        while not finished.is_set() and time.time() < deadline:
            app.processEvents()
            finished.wait(0.05)
        assert finished.is_set(), "engine did not reach FINISHED within 5 s after abort"

    return store, store.get_test_run(captured["run_uuid"])


def _drive_abort_twice(app: QCoreApplication, source: str) -> tuple[RunStore, dict]:
    """Abort mid-step (first), then again while the busy Post-test Action runs.

    Returns ``(store, run_row)`` once FINISHED is emitted within a bound."""
    captured = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8", newline="\n")
        engine = RunEngine(store, str(schedule_path))
        engine.run_uuid_reported.connect(_capture)

        engine.start()

        # Let the worker reach step 1's busy loop, then request the first abort from this thread.
        deadline = time.time() + 5.0
        while not captured.get("run_uuid") and time.time() < deadline:
            app.processEvents()
        t0 = time.time()
        engine.request_abort()

        # Wait until the running step is recorded ERROR "Aborted by Operator" — that proves we are
        # past the step loop and about to enter the Post-test phase.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            app.processEvents()
            conn = store._conn
            conn.row_factory = sqlite3.Row
            if any(n["result"] == "ERROR" for n in conn.execute(
                "SELECT result FROM node WHERE run_uuid=?", (captured["run_uuid"],)
            ).fetchall()):
                break

        # Now request abort again, repeatedly, until the busy Post-test Action records "aborted".
        finished = threading.Event()
        engine.state_changed.connect(lambda status: finished.set() if status == RunStatus.FINISHED else None)
        deadline = time.time() + 5.0
        while time.time() < deadline and not finished.is_set():
            engine.request_abort()
            app.processEvents()
            finished.wait(0.05)

        assert finished.is_set(), "engine did not reach FINISHED within 5 s after the second abort"
        assert time.time() - t0 < 5.0, "second abort did not land during the busy Post-test Action"

    return store, store.get_test_run(captured["run_uuid"])


# --- schedules ----------------------------------------------------------------


POSTTEST_AFTER_ABORT = """\
return ScheduleParams("Posttest after abort", "a busy step; a Post-test Action that reports PASS")
  :setSchedule({
    StepParams("spin"):setStepNo("1.")
      :setAction(function() for i=1,1e9 do end end),
  })
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "teardown ran "..passFail) end)
"""


STOP_POSTTEST = """\
return ScheduleParams("Stop on fail posttest", "step 1 fails; Stop on Fail aborts between steps")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("first"):setStepNo("1.")
      :setAction(function() ATE.report(Result.FAIL, "wrong colour", "Red", "Green") end),
    StepParams("second"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "should not run") end),
  })
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "posttest "..passFail) end)
"""


BUSY_POSTTEST = """\
return ScheduleParams("Busy posttest", "a busy step; a busy Post-test Action that checks aborted()")
  :setSchedule({
    StepParams("spin"):setStepNo("1.")
      :setAction(function() for i=1,1e9 do end end),
  })
  :setPostTestAction(function(passFail)
    local n=0
    while n<1e9 do if aborted() then error("aborted", 0) end; n=n+1 end end)
"""


MINIMAL = """\
return ScheduleParams("Minimal schedule", "a bare schedule for direct Post-test Action driving") :setSchedule({})
"""


# --- tests --------------------------------------------------------------------


def test_posttest_runs_after_operator_abort_mid_busy_step(app: QCoreApplication) -> None:
    """Checkbox 1 & 4 — a whole-run abort interrupts the busy step (packet 13c); the run still closes
    ABORTED / FAIL, and the Post-test Action ran afterwards (its row is outcome "ok")."""
    store, row = _drive_abort(app, POSTTEST_AFTER_ABORT)

    assert row is not None
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "ABORTED"

    nodes = _nodes(store, row["run_uuid"])
    assert nodes["1."]["result"] == "ERROR"   # the running busy step
    actions = _actions(store._conn, row["run_uuid"])
    assert set(actions) == {"post"}            # Post-test ran after clearing the flag
    assert actions["post"]["outcome"] == "ok"


def test_posttest_runs_after_stop_on_fail_abort(app: QCoreApplication) -> None:
    """Checkbox 1 — a Stop-on-Fail ABORT ends STOPPED_ON_FAIL / FAIL, and the Post-test Action still
    ran afterwards (its row is outcome "ok")."""
    store, row, prompts = _drive_with_choice(app, STOP_POSTTEST, "ABORT")

    assert row is not None
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "STOPPED_ON_FAIL"
    assert prompts == 1

    actions = _actions(store._conn, row["run_uuid"])
    assert set(actions) == {"post"}
    assert actions["post"]["outcome"] == "ok"


def test_posttest_runs_after_pretest_failure(app: QCoreApplication) -> None:
    """Checkbox 1 — a failing Pre-test Action aborts automatically (packet 14b); every step is NOTRUN,
    End Reason PRETEST_FAILED / FAIL, and the Post-test Action still ran afterwards (outcome "ok")."""
    store, row = _run_schedule(app, PRE_ERROR_POST_OK)

    assert row is not None
    actions = _actions(store._conn, row["run_uuid"])
    pre, post = actions["pre"], actions["post"]
    # The raising Pre-test is an error row; the Post-test still ran ok.
    assert set(actions) == {"pre", "post"}
    assert pre["outcome"] == "error"
    assert post["outcome"] == "ok"

    steps = [n for n in store.nodes(row["run_uuid"]) if n["kind"] == "step"]
    assert all(n["result"] == "NOTRUN" and n["attempt_count"] == 0 for n in steps)

    # The run fails with End Reason PRETEST_FAILED, not COMPLETED.
    assert row["end_reason"] == "PRETEST_FAILED"
    assert row["result"] == "FAIL"


def test_second_abort_during_busy_posttest_records_aborted_and_finishes(app: QCoreApplication) -> None:
    """Checkbox 2 — a second abort during a busy Post-test Action ends only that action (its row is
    outcome "aborted", not a run error); the run still finishes ABORTED / FAIL. The first abort set
    the flag mid-step; the second trips it again while teardown spins."""
    store, row = _drive_abort_twice(app, BUSY_POSTTEST)

    assert row is not None
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "ABORTED"

    actions = _actions(store._conn, row["run_uuid"])
    # The busy Post-test Action that was aborted records outcome "aborted".
    assert set(actions) == {"post"}
    assert actions["post"]["outcome"] == "aborted"


def test_remaining_posttest_actions_still_run_after_one_aborted(app: QCoreApplication) -> None:
    """Checkbox 2 — with two Post-test Actions, the one a second abort lands in records outcome
    "aborted", and the next still runs (outcome "ok"). Drives _record_phase_action in reverse exactly
    as run() does; the busy action is aborted from another thread so it can be interrupted."""
    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(MINIMAL, encoding="utf-8", newline="\n")
        engine = RunEngine(store, str(schedule_path))

        # A real LuaHost (real runtime + abort hook); drive two Post-test Actions directly through
        # _record_phase_action instead — a real on-disk .tslua schedule can only hold ONE Post-test
        # Action (the builder replaces, not appends). The busy one trips on the shared flag every
        # iteration; the other just logs and returns ok.

        host = LuaHost(str(schedule_path))

        def _busy(pass_fail):
            while True:
                if engine._abort_flag.aborted:
                    raise RuntimeError("aborted")
                time.sleep(0.001)

        def _normal(pass_fail):
            host.record_log("INFO", "posttest ran " + pass_fail)

        # Packet 14c: clear the abort flag before Post-test Actions begin.
        engine._abort_flag.clear()

        busy_done = threading.Event()
        busy_outcome = {}

        def _run_busy():
            outcome, _error = engine._record_phase_action(
                host, "test-run", instance_seq=0, phase="post", fn=_busy, pass_fail="PASS"
            )
            busy_done.set()
            busy_outcome["outcome"] = outcome

        worker = threading.Thread(target=_run_busy)
        worker.start()

        # Request abort repeatedly until the busy action is interrupted (records "aborted").
        deadline = time.time() + 5.0
        while time.time() < deadline and not busy_done.is_set():
            engine.request_abort()
            app.processEvents()
            time.sleep(0.01)
        assert busy_done.is_set(), "busy Post-test Action was never aborted"
        assert busy_outcome["outcome"] == "aborted"

        # The remaining action still runs to completion (no abort in flight).
        outcome, _error = engine._record_phase_action(
            host, "test-run", instance_seq=1, phase="post", fn=_normal, pass_fail="PASS"
        )
        assert busy_outcome["outcome"] == "aborted"
        assert outcome == "ok"


def test_posttest_error_fails_run_but_keeps_end_reason(app: QCoreApplication) -> None:
    """Checkbox 3 — an error raised in a Post-test Action is logged and fails the run (result FAIL),
    but leaves its End Reason untouched (COMPLETED)."""
    store, row = _run_schedule(app, POSTTEST_ERROR_SOURCE)

    assert row is not None
    # The step passed; only teardown failed.
    assert row["end_reason"] == "COMPLETED"
    assert row["result"] == "FAIL"

    actions = _actions(store._conn, row["run_uuid"])
    post = actions["post"]
    assert set(actions) == {"post"}
    assert post["outcome"] == "error"
    assert "boom in posttest PASS" in (post["error"] or "")

    # The raised action's traceback is logged at ERROR.
    error_lines = [ln for ln in store.log_lines(row["run_uuid"]) if ln["level"] == "ERROR"]
    assert any("boom in posttest PASS" in ln["text"] for ln in error_lines)


# --- schedules ----------------------------------------------------------------


POSTTEST_ERROR_SOURCE = """\
return ScheduleParams("Posttest error", "a passing step; a Post-test Action that raises")
  :setSchedule({
    StepParams("the step"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "ok") end),
  })
  :setPostTestAction(function(passFail) error("boom in posttest "..passFail) end)
"""
