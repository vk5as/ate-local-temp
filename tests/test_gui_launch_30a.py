"""Packet 30a seam tests: bare ``ate`` opens the application.

Every launch here uses ``exec_=False`` and injected dialogs, so nothing blocks on a real
:class:`~PyQt6.QtWidgets.QMessageBox`; config and data live in the test's ``tmp_path``. The screens
are the real :class:`StagedRunScreen` and :class:`HistoryScreen` bound to a real
:class:`HeadlessRunSession` and store, exactly as the screen tests build them.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QMainWindow, QMessageBox

from ate._version import __version__
from ate.app import _alert_dialog, _missing_config_dialog, launch
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


def test_launch_keeps_its_qapplication_alive_without_pytest_qt(tmp_path):
    """In a plain subprocess (no pytest-qt) the QApplication created by launch() must outlive
    launch itself: the window, its widgets and the session stay usable after launch returns."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    root = Path(__file__).resolve().parent.parent
    script = (
        "from PyQt6.QtWidgets import QApplication\n"
        "from ate.app import launch\n"
        f"out = launch(['--config', {str(env['config'])!r}], exec_=False)\n"
        "assert out.code == 0, out.code\n"
        "assert out.app is not None and out.app is QApplication.instance(), 'launch lost its QApplication'\n"
        "window = out.window\n"
        "assert window is not None and window.findChild(object) is not None, 'window was destroyed'\n"
        "assert window.session.store is not None and window.session.config is not None\n"
        "print('ALIVE')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(root),
        env={**os.environ, "PYTHONPATH": "src", "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "ALIVE" in proc.stdout, proc.stderr


def test_launch_reads_the_config_exactly_once(qapp, tmp_path, monkeypatch):
    """Spec: the config is 'read once at launch' — the session must not re-read the file (30a review)."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    import ate.app as app_module
    import ate.run_session.headless as headless_module
    from ate.config import read_config

    reads = []

    def counting(path, **kwargs):
        reads.append(str(path))
        return read_config(path, **kwargs)

    monkeypatch.setattr(app_module, "read_config", counting)
    monkeypatch.setattr(headless_module, "read_config", counting)
    outcome, alerts = _launch(env, tmp_path=tmp_path)
    assert outcome.code == 0, alerts
    assert len(reads) == 1, f"read_config called {len(reads)} times: {reads}"


class _TickingClock:
    """A datetime stand-in whose now() steps forward a minute per call, so two separate
    open_app_log calls would name two different files (30a review item 4)."""

    def __init__(self) -> None:
        self._i = 0

    def now(self) -> datetime:
        self._i += 1
        return datetime(2026, 10, 29, 12, 0, 0) + timedelta(minutes=self._i)


def test_one_application_log_per_launch_carries_launch_and_session_lines(qapp, tmp_path, monkeypatch):
    """The launch's AppLog is handed to the session: one file, both the launch line and the
    interrupted-run line the session logs through it."""
    from datetime import timedelta

    env = write_ate_env(tmp_path)
    store = RunStore(env["db"])
    store.insert_test_run({"uut_type": "PowerSupply", "uut_serial": "PS-1", "station_name": "BENCH-03"})
    import ate.log as log_module

    monkeypatch.setattr(log_module, "datetime", _TickingClock())
    outcome, alerts = _launch(env, tmp_path=tmp_path)
    assert outcome.code == 0, alerts
    logs = sorted((tmp_path / "<data>" / "logs").glob("ate-app-*.log"))
    assert len(logs) == 1, f"expected one application log, found {[p.name for p in logs]}"
    text = logs[0].read_text()
    assert "launched with config" in text, "the launch line is missing"
    assert "interrupted run" in text, "the session's interrupted-run line did not reach the launch log"


def test_schema_refusal_is_logged_to_the_application_log(tmp_path):
    """A refused launch (missing database) writes its reason to the application log (30a review)."""
    env = write_ate_env(tmp_path)
    outcome, alerts = _launch(env, tmp_path=tmp_path)
    assert outcome.code == 1 and outcome.window is None
    logs = list((tmp_path / "<data>" / "logs").glob("ate-app-*.log"))
    assert logs, "the launch opened no application log"
    text = logs[0].read_text()
    assert "launch refused" in text and "ate db migrate" in text, text


def test_bare_ate_dispatches_to_the_gui_launcher(monkeypatch, capsys):
    """No subcommand, with or without --config, goes to the GUI launcher."""
    import ate.app

    seen = []
    monkeypatch.setattr(
        ate.app,
        "launch",
        lambda argv, **kwargs: seen.append(argv) or ate.app.LaunchOutcome(0),
    )
    assert main([]) == 0
    assert main(["--config", "some/config.json"]) == 0
    assert seen == [[], ["--config", "some/config.json"]]
    assert seen[-1] == ["--config", "some/config.json"], "the --config flag reaches the launcher"


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


def test_subcommands_still_tolerate_extra_arguments(capsys, tmp_path):
    """The base CLI used parse_known_args: stray tokens after db/setup were ignored (30a review)."""
    db = tmp_path / "ate.db"
    code = main(["db", "status", str(db), "extra", "--junk"])
    err = capsys.readouterr().err
    assert code == 1 and "unrecognized" not in err, f"argparse rejected stray args: {code} {err!r}"
    code = main(["db", "migrate", str(db), "--yes", "stray"])
    assert code == 0, capsys.readouterr().err
    code = main(["setup", "--root", str(tmp_path / "station"), "--bogus"])
    assert code == 0, capsys.readouterr().err


def test_unknown_command_exits_one_with_the_old_hint(capsys):
    """``ate bogus`` is exit 1 with the 'no command given / try …' hint, not argparse usage (exit 2)."""
    assert main(["bogus"]) == 1
    err = capsys.readouterr().err
    assert "no command given" in err and "try 'ate" in err, err
    assert "unrecognized" not in err


def test_db_migrate_yes_without_path_does_not_take_yes_as_the_path(capsys, tmp_path, monkeypatch):
    """``ate db migrate --yes`` (no path) migrates ate.db; --yes may appear anywhere in the db args."""
    monkeypatch.chdir(tmp_path)
    assert main(["db", "migrate", "--yes"]) == 0
    assert (tmp_path / "ate.db").exists(), capsys.readouterr().err
    assert not (tmp_path / "--yes").exists(), "the --yes flag was parsed as the database path"
    assert "created or upgraded database at ate.db" in capsys.readouterr().out


def test_help_does_not_advertise_the_hidden_pdf_helper(capsys):
    """``ate --help`` usage must not list the hidden `_pdf` helper (30a review item 12)."""
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "_pdf" not in out, f"hidden helper leaked into help: {out!r}"
    assert "db" in out and "setup" in out


# -- the real default dialogs (30a review item 1) --------------------------------------


def _stub_dialogs(monkeypatch, boxes, choose):
    """Make QMessageBox.exec return at once, recording each box, with clickedButton answering ``choose``.

    This runs the real `_missing_config_dialog`/`_alert_dialog` code without blocking on a modal box."""
    from PyQt6.QtWidgets import QPushButton

    def fake_exec(self, *args):
        boxes.append(self)
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: choose(self))
    return boxes


def _button(box, text):
    """The box's button with this exact text — how a test 'clicks' a stubbed dialog."""
    from PyQt6.QtWidgets import QPushButton

    return next(b for b in box.findChildren(QPushButton) if b.text() == text)


def test_default_missing_config_dialog_builds_and_create_choice_returns_true(qapp, monkeypatch):
    """The real prompt constructs (it crashed when the QApplication was passed as parent) and
    answering "Create default config and launch" returns True."""
    boxes: list[QMessageBox] = []
    _stub_dialogs(monkeypatch, boxes, lambda self: _button(self, "Create default config and launch"))
    assert _missing_config_dialog("/nowhere/config.json") is True
    box = boxes[0]
    assert box.parent() is None, "the prompt must not be parented to the QApplication"
    assert box.text() == "No config file at /nowhere/config.json"
    assert box.informativeText() == "Create a default config and launch, or stop?"


def test_default_missing_config_dialog_abort_choice_returns_false(qapp, monkeypatch):
    boxes: list[QMessageBox] = []
    _stub_dialogs(monkeypatch, boxes, lambda self: _button(self, "Abort"))
    assert _missing_config_dialog("/nowhere/config.json") is False


def test_default_alert_dialog_builds_with_no_parent(qapp, monkeypatch):
    boxes: list[QMessageBox] = []
    _stub_dialogs(monkeypatch, boxes, lambda self: _button(self, "OK"))
    _alert_dialog("this ATE is too old")
    assert boxes and boxes[0].text() == "this ATE is too old"
    assert boxes[0].parent() is None


def test_bare_launch_with_default_dialogs_exits_one_on_abort(qapp, tmp_path, monkeypatch):
    """``ate --config <missing>`` with the real dialogs: prompt, one clear alert, exit 1."""
    boxes: list[QMessageBox] = []
    _stub_dialogs(monkeypatch, boxes, lambda self: _button(self, "Abort"))
    config = tmp_path / "gone.json"
    outcome = launch(["--config", str(config)], exec_=False)
    assert outcome.code == 1
    assert len(boxes) == 2, "prompt then alert — exactly two boxes on the abort path"
    assert boxes[0].parent() is None and boxes[1].parent() is None
    alert = boxes[1].text()
    assert alert == f"config file not found at {config.resolve()}", f"duplicated wording: {alert!r}"
