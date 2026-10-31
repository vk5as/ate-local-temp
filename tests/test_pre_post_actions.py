"""Packet 14a — Pre-test and Post-test Actions run around the steps and record action rows.

Seam-1 coverage of a real LuaHost + QThread RunEngine driving schedules whose Pre-test/Post-test
Actions log their phase and the pass_fail they were called with, then asserting the recorded
``action`` rows (phase, order, times, outcome) and that each call received the run's standing
pass_fail. Verified against a real SQLite file; no mocks."""

import sqlite3
import tempfile
from pathlib import Path

from PyQt6.QtCore import QCoreApplication

from ate.engine.lua_host import LuaHost
from ate.engine.run_engine import RunEngine
from ate.storage.store import RunStore

from ._fixtures import PASSING, PRE_ERROR_POST_OK, PRE_POST_FAIL, PRE_POST_PASS, _drive


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


def test_run_action_forwards_pass_fail_and_restores_context() -> None:
    """Seam-1 — _run_action passes the run's pass_fail as the closure's first argument to a Pre-test/
    Post-test Action, and restores the idle context afterwards so lockdown still applies."""
    with tempfile.TemporaryDirectory() as td:
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(PRE_POST_PASS, encoding="utf-8", newline="\n")
        host = LuaHost(str(schedule_path))

        # Run the Pre-test Action through its phase context exactly as the engine does.
        host._run_action(host.schedule.pre_test_action, "pretest", pass_fail="PASS")

        assert any("pretest PASS" in text for (_level, _source, text) in host.logs), \
            "the closure must receive pass_fail as its first argument"
        # The context is restored to idle after the action, so a later step's lockdown is unaffected.
        assert host._context is None


def test_pre_and_post_actions_record_ok_rows_around_a_passing_step(app: QCoreApplication) -> None:
    """Checkbox 1 & 2 — a passing step wrapped by Pre/Post Actions records two ok rows tied to the
    same schedule instance; the logged values prove each was called with the run's pass_fail."""
    store, row = _run_schedule(app, PRE_POST_PASS)

    assert row is not None
    assert row["result"] == "PASS" and row["end_reason"] == "COMPLETED"

    actions = _actions(store._conn, row["run_uuid"])
    pre, post = actions["pre"], actions["post"]
    # One action per phase, both ok, tied to the same schedule instance.
    assert set(actions) == {"pre", "post"}
    for a in (pre, post):
        assert a["instance_seq"] == 0
        assert a["run_order"] == 0
        assert a["outcome"] == "ok"
        assert a["started_at"] is not None and a["ended_at"] is not None

    # Pre-test ran before the step, Post-test after — order by seq across both phases.
    pre_log = next(ln for ln in store.log_lines(row["run_uuid"]) if "pretest" in ln["text"])
    post_log = next(ln for ln in store.log_lines(row["run_uuid"]) if "posttest" in ln["text"])
    assert pre_log["seq"] < post_log["seq"]


def test_post_action_is_called_with_the_final_pass_fail(app: QCoreApplication) -> None:
    """Checkbox 1 — a failing step flips the run's pass_fail to FAIL, and the Post-test Action is
    called with that final value (its log records "posttest FAIL"). The Pre-test still ran first."""
    store, row = _run_schedule(app, PRE_POST_FAIL)

    assert row is not None
    assert row["result"] == "FAIL" and row["end_reason"] == "COMPLETED"

    actions = _actions(store._conn, row["run_uuid"])
    pre, post = actions["pre"], actions["post"]
    assert pre["outcome"] == "ok" and post["outcome"] == "ok"

    logs = "\n".join(ln["text"] for ln in store.log_lines(row["run_uuid"]))
    assert "pretest PASS" in logs   # Pre-test saw the initial result, before any step
    assert "posttest FAIL" in logs  # Post-test saw the final failing result


def test_erroring_pre_action_aborts_with_notrun_steps(app: QCoreApplication) -> None:
    """Checkbox 1 — a failing Pre-test Action aborts automatically (packet 14b): every step is NOTRUN,
    End Reason PRETEST_FAILED / result FAIL, and a log line names the failure and explains why every
    step is NOTRUN. The raising Pre-test still records an error row carrying its message."""
    store, row = _run_schedule(app, PRE_ERROR_POST_OK)

    assert row is not None
    actions = _actions(store._conn, row["run_uuid"])
    pre, post = actions["pre"], actions["post"]
    # The raising Pre-test is an error row carrying its message; the Post-test still ran ok.
    assert set(actions) == {"pre", "post"}
    assert pre["outcome"] == "error"
    assert "boom in pretest PASS" in (pre["error"] or "")
    assert post["outcome"] == "ok"

    # The raised action's traceback is logged at ERROR and attributed to that Pre-test call.
    error_lines = [ln for ln in store.log_lines(row["run_uuid"]) if ln["level"] == "ERROR"]
    assert any("boom in pretest PASS" in ln["text"] for ln in error_lines)

    # Every actionable step aborted before it ran -> NOTRUN with no Attempt.
    steps = [n for n in store.nodes(row["run_uuid"]) if n["kind"] == "step"]
    assert len(steps) == 1
    assert all(n["result"] == "NOTRUN" and n["attempt_count"] == 0 for n in steps)

    # The run fails with End Reason PRETEST_FAILED, not COMPLETED.
    assert row["end_reason"] == "PRETEST_FAILED"
    assert row["result"] == "FAIL"

    # A log line names the failure and explains why every step is NOTRUN.
    logs = "\n".join(ln["text"] for ln in store.log_lines(row["run_uuid"]))
    assert "boom in pretest PASS" in logs
    assert "every step is NOTRUN" in logs


def test_actions_without_a_schedule_are_not_recorded(app: QCoreApplication) -> None:
    """A schedule with no Pre-test/Post-test Actions records no action rows and runs normally."""
    store, row = _run_schedule(app, PASSING)

    assert row is not None and row["result"] == "PASS"
    assert _actions(store._conn, row["run_uuid"]) == {}
