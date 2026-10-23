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

from ._fixtures import FAILING, LOGGING, PASSING, write_ate_env


def _wait_finished(session: HeadlessRunSession) -> bool:
    """Poll an application's processEvents until the session reaches FINISHED (or times out).

    Queued cross-thread signals are only delivered once a QCoreApplication exists to pump them;
    this offscreen build delivers nothing through bare ``QCoreApplication.processEvents()`` when
    no instance is present. If none exists, create one so delivery works. The instance is local
    to the test and gone by the next one, so nothing Qt lingers for interpreter shutdown."""
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    for _ in range(2000):
        app.processEvents()
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


def test_interrupted_run_recovery_is_logged_in_the_app_log(tmp_path) -> None:
    """A run left open by a crash is closed at launch via the real store API and logged.

    Ticket 04's ticket-02 seam-2 test: no on_log override here, so the session opens its own
    application log for the launch and recovery must be recorded there — proving the recovery
    uses the real :class:`RunStore` (not a mock) and that ATE keeps one file per launch.
    """
    env = write_ate_env(tmp_path)  # config has no on_log; the session wires its own app log

    store = RunStore(env["db"])
    interrupted_uuid = store.insert_test_run(
        {"run_uuid": "deadbeef-0000-0000-0000-000000000001", "operator": "ghost"}
    )["run_uuid"]
    assert store.get_test_run(interrupted_uuid)["ended_at"] is None  # open row = crashed run

    session = HeadlessRunSession(env["config"])  # on_log defaults to the launch application log
    session.select_schedule("Passing schedule")
    engine = session.start()
    assert _wait_finished(session)

    # The store (real API, no mocks) closed the crashed run as INTERRUPTED / FAIL.
    closed = store.get_test_run(interrupted_uuid)
    assert closed is not None
    assert closed["end_reason"] == "INTERRUPTED"
    assert closed["result"] == "FAIL"

    # ATE's application log for this launch recorded the recovery (ticket 04).
    assert session._app_log is not None
    lines = [ln.strip() for ln in session._app_log.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert any("interrupted" in line.lower() for line in lines)


def test_schedule_logs_are_recorded_with_levels_sources_and_owners(tmp_path) -> None:
    """ATE.log at every level and print are recorded as log lines with source, owner and text."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    states = []
    session.state_changed.connect(states.append)

    session.set_operator("Ada")
    session.select_schedule("Logging schedule")

    engine = session.start()
    assert _wait_finished(session)

    row = session.store.get_test_run(session.run_uuid)
    assert row is not None and row["result"] == "PASS"  # every step reported PASS

    lines_by_seq = {ln["seq"]: ln for ln in session.store.log_lines(session.run_uuid)}

    # Step 1 logs INFO via ATE.log and again via print (redirected to INFO); steps 2 and 3
    # log WARNING and ERROR. Four schedule-sourced lines, one per recorded event; each gets
    # its own seq in emission order (a batch can hold several).
    schedule = sorted(
        (ln for ln in lines_by_seq.values() if ln["source"] == "schedule"),
        key=lambda ln: ln["seq"],
    )
    assert [(ln["seq"], ln["level"], ln["text"]) for ln in schedule] == [
        (0, "INFO", "step one running"),
        (1, "INFO", "printed here"),  # print maps to INFO and is recorded too; distinct seq within the batch
        (2, "WARNING", "value 12"),
        (4, "ERROR", "something failed"),  # ate WARNING for warn step took seq 3
    ]

    # Every schedule line is owned by the Attempt of the step that emitted it. Ownership
    # (node_seq/attempt_no) is set once per batch and shared; it is not the line's own seq.
    expected_owner = [0, 0, 1, 2]  # owning step per recorded schedule event, in run order
    for ln, owner in zip(schedule, expected_owner):
        assert ln["node_seq"] == owner and ln["attempt_no"] == 1
        assert ln["action_seq"] is None


def test_error_level_log_line_is_recorded(tmp_path) -> None:
    """A step that logs at ERROR records an ERROR log line owned by its Attempt."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    session.select_schedule("Logging schedule")
    engine = session.start()
    assert _wait_finished(session)

    lines_by_seq = {ln["seq"]: ln for ln in session.store.log_lines(session.run_uuid)}
    error_line = next(ln for ln in lines_by_seq.values() if ln["level"] == "ERROR")
    assert (error_line["source"], error_line["text"]) == ("schedule", "something failed")
    assert error_line["node_seq"] == 2 and error_line["attempt_no"] == 1

