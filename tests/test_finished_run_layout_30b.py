"""Packet 30b — the layout after a run finishes, against a shown 1280x800 window.

The staged layout used to re-show the setup stages above the run view when a run reached FINISHED.
The step list then shrank to about 68 px and the main window grew past its size (1280x830 in 29c,
1280x998 in 29d), pushing the status bar off-screen. These seam tests run a 20-step Test Run in each
layout and pin the geometry: the window keeps its size, the status bar stays inside it, the step
list is at least as tall as it was during the run, and the mockup variant C "New run" button (spec:
"New run clears nothing") returns to the setup stages with every field kept.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtWidgets import QApplication

from ate.app import launch
from ate.run_session.headless import SessionState
from ate.storage.store import RunStore

from ._fixtures import WRITES_PDF, add_long_schedule, fake_pdf_helper, write_ate_env

SHOTS = Path("/tmp/30b-shots")
SHOTS.mkdir(exist_ok=True)
WINDOW_SIZE = (1280, 800)
FIELDS = {"operator": "Ada Lovelace", "project": "PSU-200 Production", "uut_type": "PowerSupply", "serial": "PS-1"}


@pytest.fixture
def fake_pdf(monkeypatch):
    """Stand in for the QWebEngine PDF helper so a finished run's report job stays cheap."""
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", fake_pdf_helper(WRITES_PDF))


def _wait(qapp, predicate, timeout: float = 15.0) -> bool:
    """Pump the event loop until ``predicate`` holds; fail loudly when it never does."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if predicate():
            return True
    return False


def _shown_window(qapp, tmp_path, layout: str):
    """Launch the real MainWindow at 1280x800 in ``layout`` and show it offscreen."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    schedule = add_long_schedule(tmp_path, steps=20, fail_at=99)  # 20 steps, no Stop-on-Fail pause
    config = tmp_path / "layout-config.json"
    data = json.loads(env["config"].read_text(encoding="utf-8"))
    data["ui"] = {"layout": layout}
    config.write_text(json.dumps(data), encoding="utf-8")
    outcome = launch(["--config", str(config)], exec_=False, prompt=lambda path: True, alert=[])
    window = outcome.window
    window.resize(*WINDOW_SIZE)
    window.show()
    _wait(qapp, lambda: window.isVisible())
    return window, schedule


def _fill_and_start(qapp, window, schedule) -> None:
    """Fill the Operator's fields and start the run through the visible layout's own buttons."""
    screen = window.run_screen
    for key, value in FIELDS.items():
        getattr(screen.fields, f"{key}_edit").setText(value)
    picker = screen.picker.schedule_list
    for index in range(picker.count()):
        item = picker.item(index)
        if item.data(Qt.ItemDataRole.UserRole) == schedule:
            picker.setCurrentItem(item)
            break
    if hasattr(screen, "uut_operator_stage"):  # the staged screen walks its three stages
        screen.uut_operator_stage.next_button.click()
        screen.test_schedule_stage.run_button.click()
        screen.steps_options_stage.run_button.click()
    else:
        screen.start_button.click()
    assert _wait(qapp, lambda: window.session.state == SessionState.STARTED), "the run never started"


def _finish(window) -> None:
    """End any run still in progress so the session and store close clean."""
    session = window.session
    engine = getattr(session, "_engine", None)
    if engine is not None and engine.isRunning():
        if session.pause_pending():
            session.choose("ABORT")
        else:
            session.request_abort()
        _wait(QApplication.instance(), lambda: not engine.isRunning())
    from ate.report.pdf_job import stop_pdf_jobs

    stop_pdf_jobs()
    session.close()


def _status_bar_inside(window, screen) -> None:
    """The status bar's rectangle lies fully within the window's rectangle."""
    win = window.geometry()
    bar = screen.status_bar.geometry()
    assert win.left() <= bar.left() and bar.right() <= win.right(), f"status bar {bar} outside window {win}"
    assert win.top() <= bar.top() and bar.bottom() <= win.bottom(), f"status bar {bar} outside window {win}"


def test_staged_finished_run_keeps_the_window_size_and_the_status_bar(qapp, tmp_path, fake_pdf):
    window, schedule = _shown_window(qapp, tmp_path, "staged")
    staged = window.staged_screen
    _fill_and_start(qapp, window, schedule)
    assert _wait(qapp, lambda: staged.run_view.step_list.count() >= 20), "the step rows were never built"
    viewport_running = staged.run_view.step_list.viewport().height()

    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "the run never finished"
    for _ in range(200):
        qapp.processEvents()

    assert window.size() == QSize(*WINDOW_SIZE), f"the window grew to {window.size()} after the run finished"
    _status_bar_inside(window, staged.run_view)
    viewport_finished = staged.run_view.step_list.viewport().height()
    assert viewport_finished >= viewport_running, (
        f"the finished step list viewport {viewport_finished} px is smaller than its running height {viewport_running} px"
    )
    assert viewport_finished >= 100, f"the finished step list viewport collapsed to {viewport_finished} px (the 68 px collapse of 10a/29c/29d)"
    assert staged.run_view.new_run_button.isEnabled(), "the finished state offers no way to start the next run"
    assert staged.run_view.step_list.count() >= 20, "the finished run lost its step rows"
    window.grab().save(str(SHOTS / "staged-finished.png"))
    staged.run_view.new_run_button.click()
    qapp.processEvents()
    _finish(window)


def test_three_pane_finished_run_keeps_the_window_size_and_the_status_bar(qapp, tmp_path, fake_pdf):
    window, schedule = _shown_window(qapp, tmp_path, "threePane")
    three = window.three_pane_screen
    _fill_and_start(qapp, window, schedule)
    assert _wait(qapp, lambda: three.table.table.rowCount() >= 20), "the step rows were never built"
    viewport_running = three.table.table.viewport().height()

    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "the run never finished"
    for _ in range(200):
        qapp.processEvents()

    assert window.size() == QSize(*WINDOW_SIZE), f"the window grew to {window.size()} after the run finished"
    _status_bar_inside(window, three)
    viewport_finished = three.table.table.viewport().height()
    assert viewport_finished >= viewport_running, (
        f"the finished table viewport {viewport_finished} px is smaller than its running height {viewport_running} px"
    )
    window.grab().save(str(SHOTS / "three-pane-finished.png"))
    _finish(window)


def test_new_run_returns_to_the_setup_stage_with_every_field_kept(qapp, tmp_path, fake_pdf):
    window, schedule = _shown_window(qapp, tmp_path, "staged")
    staged = window.staged_screen
    _fill_and_start(qapp, window, schedule)
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "the run never finished"

    staged.run_view.new_run_button.click()
    for _ in range(100):
        qapp.processEvents()

    assert staged.steps_stack.isVisible(), "New run did not bring the setup stages back"
    assert staged.steps_stack.currentIndex() == 0, "New run did not return to the first setup stage"
    assert not staged.run_view.isVisible(), "the setup stages and the finished run view are stacked again"
    for key, value in FIELDS.items():
        assert getattr(staged.fields, f"{key}_edit").text() == value, f"New run cleared the {key} field"
    assert window.session.selected_schedule == schedule, "New run dropped the selected Test Schedule"
    window.grab().save(str(SHOTS / "after-new-run.png"))

    # The carried-over setup starts the next run straight from the stage: "New run clears nothing".
    staged.steps_options_stage.run_button.click()
    assert _wait(qapp, lambda: window.session.state == SessionState.STARTED), "the carried-over setup could not start a run"
    assert window.size() == QSize(*WINDOW_SIZE), "the second run resized the window"
    _finish(window)
