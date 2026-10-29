"""Packet 30a seam tests: bare ``ate`` opens the application.

Every launch here uses ``exec_=False`` and injected dialogs, so nothing blocks on a real
:class:`~PyQt6.QtWidgets.QMessageBox`; config and data live in the test's ``tmp_path``. The screens
are the real :class:`StagedRunScreen` and :class:`HistoryScreen` bound to a real
:class:`HeadlessRunSession` and store, exactly as the screen tests build them.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest
from PyQt6.QtWidgets import QMainWindow

from ate._version import __version__
from ate.app import launch
from ate.cli import main
from ate.run_session.headless import SessionState
from ate.storage.store import SCHEMA_VERSION, RunStore
from ate.ui.history_screen import HistoryScreen
from ate.ui.staged_run_screen import StagedRunScreen

from ._fixtures import WRITES_PDF, fake_pdf_helper, write_ate_env


@pytest.fixture
def fake_pdf(monkeypatch):
    """Stand in for the QWebEngine PDF helper so a finished run's report job stays cheap."""
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", fake_pdf_helper(WRITES_PDF))


def _launch(env, exec_=False, prompt=lambda path: True, ui=None, tmp_path=None):
    """Launch against ``env`` with the dialogs captured instead of shown. Returns (outcome, alerts)."""
    config_path = env["config"]
    if ui is not None:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        data["ui"] = ui
        config_path = tmp_path / "ui-config.json"
        config_path.write_text(json.dumps(data), encoding="utf-8")
    alerts: list[str] = []
    outcome = launch(
        ["--config", str(config_path)],
        exec_=exec_,
        prompt=prompt,
        alert=alerts.append,
    )
    return outcome, alerts


def _db_bytes(path):
    """The database file's bytes, or None when it does not exist."""
    return path.read_bytes() if path.exists() else None


def test_bare_launch_builds_a_main_window_with_the_run_screen(qapp, tmp_path):
    """``ate`` with no subcommand builds the main window, Run screen first, and does not loop."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])  # a migrated database, so launch is not refused
    outcome, alerts = _launch(env, tmp_path=tmp_path)

    assert outcome.code == 0, alerts
    window = outcome.window
    assert isinstance(window, QMainWindow)
    assert __version__ in window.windowTitle(), "the title carries the version"
    assert isinstance(window.run_screen, StagedRunScreen)
    assert isinstance(window.history_screen, HistoryScreen)
    assert window.run_screen.run_view.debug_window is not None, "the Run view carries its debug pane"
    assert window.stack.currentWidget() is window.run_screen, "the Run screen is the default view"
    assert window.centralWidget() is window.stack


def test_launch_scans_the_configured_directories(qapp, tmp_path):
    """Constructing the Run screen scans the config's Test Definition directories (spec 31)."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    outcome, alerts = _launch(env, tmp_path=tmp_path)
    schedules = outcome.window.run_screen.test_schedule_stage.schedule_list
    names = [schedules.item(i).text() for i in range(schedules.count())]
    assert any("Passing schedule" in name for name in names), names


def test_launch_closes_a_run_left_open_by_a_crash(qapp, tmp_path):
    """Spec 98: an interrupted run is closed as INTERRUPTED/FAIL at launch."""
    env = write_ate_env(tmp_path)
    store = RunStore(env["db"])
    run_uuid = store.insert_test_run({"uut_type": "PowerSupply", "uut_serial": "PS-1", "station_name": "BENCH-03"})["run_uuid"]
    outcome, alerts = _launch(env, tmp_path=tmp_path)
    assert outcome.code == 0, alerts
    row = store.get_test_run(run_uuid)
    assert row["end_reason"] == "INTERRUPTED" and row["result"] == "FAIL", row
    assert outcome.window.app_log.path.exists(), "one application log is opened per launch"


def test_history_switch_shows_the_history_screen(qapp, tmp_path):
    """The View menu's History action swaps the central widget to the History screen."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    outcome, alerts = _launch(env, tmp_path=tmp_path)
    window = outcome.window
    assert window.history_action.isEnabled()
    window.history_action.trigger()
    qapp.processEvents()
    assert window.stack.currentWidget() is window.history_screen
    window.run_action.trigger()
    qapp.processEvents()
    assert window.stack.currentWidget() is window.run_screen


def _start_a_blocking_run(qapp, window):
    """Start a schedule that blocks on an Operator input, so the run stays in progress.

    `start()` announces STARTED on this thread, so the run is in progress before the worker
    reaches its step; the input ask arrives through the event loop."""
    session = window.session
    asked = []
    session.input_asked.connect(lambda seq, attempt, index: asked.append((seq, attempt, index)))
    session.select_schedule("Blocking input schedule")
    session.set_operator("op")
    session.set_uut_type("PowerSupply")
    session.set_uut_serial("PS-1")
    session.start()
    assert window.run_screen.state == SessionState.STARTED, "the run is in progress"
    deadline = time.time() + 5
    while time.time() < deadline and not asked:
        qapp.processEvents()
    return asked


def _finish_run(qapp, window, asked):
    """Answer the blocking step and wait for the run, and its report job, to end."""
    assert asked, "the step asked for its input"
    written = []
    window.session.report_written.connect(lambda html, pdf: written.append(html))
    window.session.supply_input(*asked[0], 42)
    deadline = time.time() + 10
    while time.time() < deadline and window.run_screen.state != SessionState.FINISHED:
        qapp.processEvents()
    while time.time() < deadline and not written:
        qapp.processEvents()
    return written


def test_history_is_disabled_during_a_run_when_the_config_forbids_it(qapp, tmp_path, fake_pdf):
    """``ui.allowHistoryDuringRun: false`` disables the History switch while a run is up."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    outcome, alerts = _launch(env, tmp_path=tmp_path, ui={"allowHistoryDuringRun": False})
    window = outcome.window
    assert window.history_action.isEnabled()  # not running yet

    asked = _start_a_blocking_run(qapp, window)
    assert window.history_action.isEnabled() is False
    window.show_history()
    assert window.stack.currentWidget() is window.run_screen, "History stays out of reach"

    _finish_run(qapp, window, asked)
    assert window.history_action.isEnabled()
    window.show_history()
    assert window.stack.currentWidget() is window.history_screen


def test_history_stays_available_during_a_run_by_default(qapp, tmp_path, fake_pdf):
    """The default config allows browsing History while a run is in progress."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    outcome, alerts = _launch(env, tmp_path=tmp_path)
    window = outcome.window
    asked = _start_a_blocking_run(qapp, window)
    assert window.history_action.isEnabled()
    window.show_history()
    assert window.stack.currentWidget() is window.history_screen
    _finish_run(qapp, window, asked)
    assert window.history_action.isEnabled()


def test_missing_config_abort_exits_non_zero(tmp_path):
    """Answering the missing-config prompt with Abort writes nothing and exits non-zero."""
    config = tmp_path / "config.json"
    alerts: list[str] = []
    outcome = launch(["--config", str(config)], exec_=False, prompt=lambda path: False, alert=alerts.append)
    assert outcome.code == 1
    assert outcome.window is None
    assert not config.exists(), "Abort must not create a config"
    assert str(config) in alerts[0], alerts


def test_missing_config_create_writes_the_config_and_launches(tmp_path):
    """Answering with Create writes the default config, and the launch then opens the window."""
    config = tmp_path / "config.json"
    alerts: list[str] = []
    launch(["--config", str(config)], exec_=False, prompt=lambda path: True, alert=alerts.append)
    assert config.exists(), alerts
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["format"] == 1 and data["station"]["uuid"], data

    # The generated defaults point at <data>/ate.db; migrate that database, then launch again.
    data_dir = config.parent / "<data>"
    data_dir.mkdir(exist_ok=True)
    RunStore(data_dir / "ate.db")
    outcome = launch(["--config", str(config)], exec_=False, prompt=lambda path: True, alert=alerts.append)
    assert outcome.code == 0, alerts
    assert outcome.window is not None


def test_launch_refuses_a_missing_database(tmp_path):
    """A database that has never been created is refused with the migrate message (spec 22)."""
    env = write_ate_env(tmp_path)
    outcome, alerts = _launch(env, tmp_path=tmp_path)
    assert outcome.code == 1 and outcome.window is None
    assert "ate db migrate" in alerts[0], alerts
    assert not env["db"].exists(), "the launch check never creates the database"


def test_launch_refuses_an_older_database_without_migrating(tmp_path):
    """A database built by an older ATE is refused, and its bytes are untouched (spec 22)."""
    env = write_ate_env(tmp_path)
    db = env["db"]
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE test_run (run_uuid TEXT)")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
    conn.commit()
    conn.close()
    before = _db_bytes(db)
    outcome, alerts = _launch(env, tmp_path=tmp_path)
    assert outcome.code == 1 and outcome.window is None
    assert "ate db migrate" in alerts[0], alerts
    assert _db_bytes(db) == before, "the GUI never migrates"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION - 1
    finally:
        conn.close()


def test_launch_refuses_a_newer_database(tmp_path):
    """A database built by a newer ATE is refused with 'this ATE is too old' (spec 23)."""
    env = write_ate_env(tmp_path)
    db = env["db"]
    RunStore(db)
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    before = _db_bytes(db)
    outcome, alerts = _launch(env, tmp_path=tmp_path)
    assert outcome.code == 1 and outcome.window is None
    assert "this ATE is too old" in alerts[0], alerts
    assert _db_bytes(db) == before, "the GUI never downgrades the schema"


def test_version_flag_prints_the_version_and_exits_zero(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_help_prints_usage_and_exits_zero(capsys):
    """Bare ``--help`` is usage, not the version, and exits 0."""
    with pytest.raises(SystemExit) as exit_code:
        main(["--help"])
    assert exit_code.value.code == 0
    out = capsys.readouterr().out
    assert "usage: ate" in out and "db" in out and "setup" in out


def test_subcommand_help_does_not_run_the_subcommand(capsys, tmp_path):
    """``ate setup --help`` prints setup's help and prepares nothing."""
    root = tmp_path / "station"
    with pytest.raises(SystemExit) as exit_code:
        main(["setup", "--help"])
    assert exit_code.value.code == 0
    out = capsys.readouterr().out
    assert "--root" in out
    assert not root.exists(), "help must not run setup"


def test_db_help_and_config_flag_parse(capsys, tmp_path):
    """``ate db --help`` and a ``--config`` before a subcommand both parse cleanly."""
    with pytest.raises(SystemExit) as exit_code:
        main(["--config", str(tmp_path / "config.json"), "db", "--help"])
    assert exit_code.value.code == 0
    assert "usage: ate db" in capsys.readouterr().out
