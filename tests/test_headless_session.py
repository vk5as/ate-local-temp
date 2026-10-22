"""Headless run session: config -> store -> catalog -> engine, driven offscreen.

Ticket 02's seam-1 test. A passing and a failing schedule each run headless (no display)
and are read back through :class:`RunStore` with the right results; fields set before start
are recorded on the run row; interrupted runs left open by a crash are closed as INTERRUPTED
at launch (spec rule 98). No widgets, no mocks.
"""

import sqlite3

from PyQt6.QtCore import QCoreApplication

from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.storage.store import RunStore

from ._fixtures import FAILING, PASSING, write_ate_env


def _wait_finished(session: HeadlessRunSession) -> bool:
    """Poll processEvents until the session reaches FINISHED (or times out)."""
    for _ in range(2000):
        QCoreApplication.processEvents()
        if session._state == SessionState.FINISHED:
            return True
    return False


def test_passing_schedule_runs_headless_and_reads_back(tmp_path) -> None:
    """A selected schedule runs headless and records a PASS run with the Operator's fields."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    states = []
    session.state_changed.connect(states.append)

    session.set_operator("Ada")
    session.set_project("Power board")
    session.set_uut_type("PSU")
    session.set_uut_serial("PSU-001")
    session.select_schedule("Passing schedule")

    engine = session.start()
    assert _wait_finished(session)

    row = session.store.get_test_run(session.run_uuid)
    assert row is not None
    assert row["result"] == "PASS"
    assert row["end_reason"] == "COMPLETED"
    # Fields set before start are recorded on the run row.
    assert row["operator"] == "Ada"
    assert row["project"] == "Power board"
    assert row["uut_type"] == "PSU"
    assert row["uut_serial"] == "PSU-001"
    # Station identity comes from the config.
    assert row["station_name"] == "BENCH-03"
    assert row["schedule_name"] == "Passing schedule"

    # Observable state progressed IDLE -> SELECTING -> STARTED -> FINISHED.
    assert SessionState("SELECTING") in states
    assert SessionState("STARTED") in states
    assert SessionState("FINISHED") in states


def test_failing_schedule_runs_headless_and_reads_back(tmp_path) -> None:
    """A selected schedule that reports FAIL runs headless and records a FAIL run."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    session.select_schedule("Failing schedule")
    engine = session.start()
    assert _wait_finished(session)

    row = session.store.get_test_run(session.run_uuid)
    assert row is not None
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "COMPLETED"


def test_interrupted_runs_are_closed_at_launch(tmp_path) -> None:
    """A run left open by a crash is closed as INTERRUPTED/FAIL before the next run starts."""
    env = write_ate_env(tmp_path)
    db_path = env["db"]

    # Simulate a crashed previous run: an open row (no end time) in the database.
    store = RunStore(db_path)
    interrupted_uuid = store.insert_test_run(
        {"run_uuid": "deadbeef-0000-0000-0000-000000000001", "operator": "ghost"}
    )["run_uuid"]
    assert store.get_test_run(interrupted_uuid)["ended_at"] is None

    log_lines: list[str] = []
    session = HeadlessRunSession(env["config"])
    session.on_log = lambda level, source, text: log_lines.append(f"{level}:{text}")

    session.select_schedule("Passing schedule")
    engine = session.start()
    assert _wait_finished(session)

    # The crashed run was recovered as INTERRUPTED / FAIL.
    closed = store.get_test_run(interrupted_uuid)
    assert closed is not None
    assert closed["end_reason"] == "INTERRUPTED"
    assert closed["result"] == "FAIL"

    # ATE logged that it did so (spec rule 98).
    assert any("interrupted" in line for line in log_lines)

    # The new run still records cleanly alongside the recovered one.
    row = session.store.get_test_run(session.run_uuid)
    assert row is not None and row["result"] == "PASS"
    assert row["end_reason"] == "COMPLETED"


def test_start_without_selection_raises(tmp_path) -> None:
    """Starting with no schedule selected is a clear error, not a crash."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    try:
        session.start()
    except Exception as exc:  # noqa: BLE001 - any selection error is acceptable here
        assert "no schedule selected" in str(exc)
