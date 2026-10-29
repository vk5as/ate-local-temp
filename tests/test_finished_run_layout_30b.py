"""Packet 30b — the layout after a run finishes, and the New run that follows it.

The staged layout used to re-show the setup stages above the run view when a run reached FINISHED.
The step list then shrank to about 68 px and the main window grew past its size (1280x830 in 29c,
1280x998 in 29d), pushing the status bar off-screen. These seam tests run a 20-step Test Run in each
layout and pin the geometry: the window keeps its size, the status bar stays inside it, the step list
is at least as tall as it was during the run, and the bottom band grows with the window instead of
against a ceiling.

The rework adds the rule the review found missing: a View action never changes session state. Only
the Operator's own actions — New run, picking a schedule, Start, Abort — move the session between
states, so switching layouts, revealing a run view, or filling the fields from History leaves a
FINISHED session FINISHED. Screenshots are opt-in: set ``ATE_30B_SHOTS`` to a directory to write them
for review.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtWidgets import QApplication, QWIDGETSIZE_MAX, QWidget

from ate.app import launch
from ate.run_session.headless import SessionState
from ate.storage.store import RunStore

from ._fixtures import WRITES_PDF, add_long_schedule, fake_pdf_helper, write_ate_env

WINDOW_SIZE = (1280, 800)
TALL_WINDOW = (1920, 1200)
STEP_LIST_FLOOR = 150  # the code's own step-list minimum (packet 30b)
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


def _shown_window(qapp, tmp_path, layout: str, size=WINDOW_SIZE, steps: int = 20, fail_at: int = 99):
    """Launch the real MainWindow at ``size`` in ``layout`` and show it offscreen."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    schedule = add_long_schedule(tmp_path, steps=steps, fail_at=fail_at)  # no Stop-on-Fail pause unless fail_at <= steps
    config = tmp_path / "layout-config.json"
    data = json.loads(env["config"].read_text(encoding="utf-8"))
    data["ui"] = {"layout": layout}
    config.write_text(json.dumps(data), encoding="utf-8")
    outcome = launch(["--config", str(config)], exec_=False, prompt=lambda path: True, alert=[])
    window = outcome.window
    window.resize(*size)
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


def _visible_rects(widget: QWidget) -> list:
    """Every widget on screen inside ``widget``, with its geometry — the layout's painted footprint."""
    return [child.geometry() for child in widget.findChildren(QWidget) if child.isVisible()]


def _no_overlay(window, shown, hidden) -> None:
    """Nothing the hidden layout paints lands on top of the layout the Operator is looking at."""
    assert not hidden.isVisible(), f"the {hidden.__class__.__name__} page is visible while it is not the current one"
    for rect in _visible_rects(hidden):
        for other in _visible_rects(shown):
            assert not rect.intersects(other), f"hidden layout rect {rect} overlaps the visible layout's {other}"


# -- 1. a hidden layout stays hidden -------------------------------------------


def test_staged_finished_run_keeps_the_window_size_and_the_status_bar(qapp, tmp_path, fake_pdf):
    """The packet's own seam: a finished 20-step run keeps the window's size and the bar on screen."""
    window, schedule = _shown_window(qapp, tmp_path, "staged")
    staged = window.staged_screen
    _fill_and_start(qapp, window, schedule)
    assert _wait(qapp, lambda: staged.run_view.step_list.count() >= 20), "the step rows were never built"
    viewport_running = staged.run_view.step_list.viewport().height()

    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "the run never finished"
    for _ in range(200):
        qapp.processEvents()

    assert window.size() == QSize(*WINDOW_SIZE), f"the window grew to {window.size()} after the run finished"
    # The mockup's finished state (variant C) is the run view alone: no setup stage stacked above it.
    assert not staged.steps_stack.isVisible(), "the setup stages reappeared above the finished run view"
    assert staged.run_view.isVisible() and staged.run_view.tabs.isVisible(), "the tabs went off-screen"
    _status_bar_inside(window, staged.run_view)
    viewport_finished = staged.run_view.step_list.viewport().height()
    assert viewport_finished >= viewport_running, (
        f"the finished step list viewport {viewport_finished} px is smaller than its running height {viewport_running} px"
    )
    assert viewport_finished >= STEP_LIST_FLOOR, (
        f"the finished step list viewport collapsed to {viewport_finished} px (the 68 px collapse of 10a/29c/29d)"
    )
    assert staged.run_view.new_run_button.isEnabled(), "the finished state offers no way to start the next run"
    assert staged.run_view.step_list.count() >= 20, "the finished run lost its step rows"
    window.grab().save(str(tmp_path / "staged-finished.png"))
    _finish(window)


def test_three_pane_finished_run_keeps_the_window_size_and_the_status_bar(qapp, tmp_path, fake_pdf):
    """The three-pane layout is checked against the same collapse."""
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
    window.grab().save(str(tmp_path / "three-pane-finished.png"))
    _finish(window)


def test_a_hidden_layout_never_paints_over_the_visible_one(qapp, tmp_path, fake_pdf):
    """Packet 30b review: revealing a run view must not show the layout that is not on screen.

    The staged screen's ``_reveal_run_view`` called ``self.show()`` on the whole screen, which
    un-hides the page the window's QStackedWidget had hidden: while a run was up, and after it
    finished, the staged header, step list and setup fields painted over the three-pane view."""
    window, schedule = _shown_window(qapp, tmp_path, "threePane")
    three = window.three_pane_screen
    staged = window.staged_screen
    assert window.stack.currentWidget() is three, "the window is not showing the configured layout"

    _fill_and_start(qapp, window, schedule)
    # Through the whole run, and up to FINISHED, the staged page stays the page nobody sees.
    deadline = time.time() + 15.0
    while time.time() < deadline:
        qapp.processEvents()
        assert not staged.isVisible(), "the hidden staged layout painted over the three-pane run"
        if window.session.state == SessionState.FINISHED:
            break
    assert window.session.state == SessionState.FINISHED, "the run never finished"
    for _ in range(200):
        qapp.processEvents()
    _no_overlay(window, three, staged)
    assert not staged.run_view.isVisible() and not staged.run_view.status_bar.isVisible(), "a staged widget is on screen"
    assert not staged.steps_stack.isVisible(), "the staged setup stages are on screen behind the run"
    assert three.table.table.rowCount() >= 20, "the visible layout lost its step rows"
    _finish(window)


def test_new_run_leaves_no_overlay_in_either_layout(qapp, tmp_path, fake_pdf):
    """The same rule applies to New run: it changes the page inside the layout, not which layout is up."""
    window, schedule = _shown_window(qapp, tmp_path, "threePane")
    three = window.three_pane_screen
    staged = window.staged_screen
    _fill_and_start(qapp, window, schedule)
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "the run never finished"
    three.new_run_button.click()
    for _ in range(100):
        qapp.processEvents()
    _no_overlay(window, three, staged)
    _finish(window)


# -- 2 and 3. New run -------------------------------------------------------------


def test_new_run_unlocks_the_session_and_carries_everything_over(qapp, tmp_path, fake_pdf):
    """Packet 30b review: New run only switched pages, so the session stayed FINISHED.

    A FINISHED session has its step boxes locked and its options shut, so stage 3 was inert. Picking
    the schedule again is the Operator action that returns it to SELECTING (spec 37), and the second
    run then honours a step toggle made on the visible stage."""
    window, schedule = _shown_window(qapp, tmp_path, "staged")
    staged = window.staged_screen
    _fill_and_start(qapp, window, schedule)
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "the run never finished"

    staged.run_view.new_run_button.click()
    for _ in range(100):
        qapp.processEvents()

    assert window.session.state == SessionState.SELECTING, "New run left the session FINISHED: the next run cannot be set up"
    assert staged.steps_stack.isVisible() and staged.steps_stack.currentIndex() == 0, "New run did not return to stage 1"
    assert not staged.run_view.isVisible(), "the setup stages and the finished run view are stacked again"
    for key, value in FIELDS.items():
        assert getattr(staged.fields, f"{key}_edit").text() == value, f"New run cleared the {key} field"
    assert window.session.selected_schedule == schedule, "New run dropped the selected Test Schedule"

    # Click through the visible stages, and toggle a step box on the way: the boxes are live again.
    staged.uut_operator_stage.next_button.click()
    qapp.processEvents()
    assert staged.test_schedule_stage.run_button.isEnabled(), "stage 2 lost the carried-over schedule"
    staged.test_schedule_stage.run_button.click()
    for _ in range(100):
        qapp.processEvents()
    assert staged.steps_stack.currentIndex() == 2, "stage 3 never came up"
    step_item = staged.steps_options_stage.step_list.item(19)
    assert step_item.checkState() == Qt.CheckState.Checked, "the carried-over step list is not showing its boxes"
    step_item.setCheckState(Qt.CheckState.Unchecked)
    for _ in range(100):
        qapp.processEvents()
    assert staged.session.node_enables.get(19) is False, "the step toggle never reached the session"
    assert staged.steps_options_stage.stop_on_fail_radio.isChecked() is True, "New run reset the On failure choice"

    staged.steps_options_stage.run_button.click()
    assert _wait(qapp, lambda: window.session.state == SessionState.STARTED), "the carried-over setup could not start a run"
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "the second run never finished"
    nodes = {n["step_number"]: n for n in window.session.store.nodes(window.session.run_uuid)}
    assert nodes["20."]["result"] == "NOTRUN", f"the toggled step still ran: {nodes['20.']}"
    assert nodes["19."]["result"] == "PASS", "the run stopped at the step before the one that was turned off"
    assert window.size() == QSize(*WINDOW_SIZE), "the second run resized the window"
    _finish(window)


def test_three_pane_new_run_unlocks_the_session(qapp, tmp_path, fake_pdf):
    """Variant B's finished header offers "New run"; here it sits with Start and Abort in the setup pane."""
    window, schedule = _shown_window(qapp, tmp_path, "threePane")
    three = window.three_pane_screen
    _fill_and_start(qapp, window, schedule)
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "the run never finished"
    assert three.new_run_button.isEnabled(), "the finished three-pane layout offers no way to start the next run"

    three.new_run_button.click()
    for _ in range(100):
        qapp.processEvents()

    assert window.session.state == SessionState.SELECTING, "New run left the three-pane session FINISHED"
    assert three.table.table.rowCount() >= 20, "New run emptied the setup step list"
    assert not three.counts.isVisible(), "the finished run's tallies still sit on the setup pane"
    boxes = three.table.checks
    assert all(box.isEnabled() for box in boxes.values()), "New run left the step boxes locked"
    boxes[19].toggle()  # the Operator turns one step off for the next run
    for _ in range(100):
        qapp.processEvents()
    assert window.session.node_enables.get(19) is False, "the three-pane toggle never reached the session"

    _fill_and_start(qapp, window, schedule)
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "the second run never finished"
    nodes = {n["step_number"]: n for n in window.session.store.nodes(window.session.run_uuid)}
    assert nodes["20."]["result"] == "NOTRUN", f" the toggle did not carry into the second run: {nodes['20.']}"
    assert window.session.selected_schedule == schedule, "the second run lost the carried-over schedule"
    _finish(window)


def test_history_new_run_lands_on_the_visible_prefilled_stage_one(qapp, tmp_path, fake_pdf):
    """Packet 30b review: History's "New run for this UUT" filled stage 1 fields the finished view hides.

    It goes through the same New run path as the run header's button, so the Operator ends up on the
    visible, prefilled setup with the session SELECTING."""
    window, schedule = _shown_window(qapp, tmp_path, "staged")
    staged = window.staged_screen
    _fill_and_start(qapp, window, schedule)
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "the run never finished"

    window.show_history()
    qapp.processEvents()
    model = window.history_screen.table_view.model()
    window.history_screen.table_view.setCurrentIndex(model.index(0, 0))  # the finished run's row
    qapp.processEvents()
    button = window.history_screen.new_run_button
    assert button.isEnabled(), "History offers no New run after a finished run"
    button.click()
    for _ in range(100):
        qapp.processEvents()

    assert window.stack.currentWidget() is staged, "History's New run did not return to the Run screen"
    assert window.session.state == SessionState.SELECTING, "History's New run left the session FINISHED"
    assert staged.steps_stack.isVisible() and staged.steps_stack.currentIndex() == 0, "History's New run did not show stage 1"
    assert staged.uut_operator_stage.uut_type_edit.text() == "PowerSupply", "History did not prefill the UUT Type"
    assert staged.uut_operator_stage.serial_edit.text() == "PS-1", "History did not prefill the serial"
    assert staged.uut_operator_stage.project_edit.text() == "PSU-200 Production", "History did not prefill the Project"
    assert window.session.selected_schedule == schedule, "History's New run lost the schedule"
    _finish(window)


def test_a_layout_switch_after_a_run_leaves_the_session_finished(qapp, tmp_path, fake_pdf):
    """Packet 30b review: switching layouts re-selected the schedule and moved FINISHED → SELECTING.

    The staged layout then lost its finished run view and its New run button for good: a View action
    must not move the session between states."""
    window, schedule = _shown_window(qapp, tmp_path, "staged")
    staged = window.staged_screen
    three = window.three_pane_screen
    _fill_and_start(qapp, window, schedule)
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "the run never finished"

    window.set_layout("threePane")
    for _ in range(100):
        qapp.processEvents()
    assert window.session.state == SessionState.FINISHED, "switching layouts unlocked a finished session"
    assert three.table.table.rowCount() >= 20, "the three-pane layout switched away from the finished run"
    _no_overlay(window, three, staged)

    window.set_layout("staged")
    for _ in range(100):
        qapp.processEvents()
    assert window.session.state == SessionState.FINISHED, "switching back unlocked the finished session"
    assert not staged.steps_stack.isVisible(), "the switch re-showed a setup stage above the finished run"
    assert staged.run_view.new_run_button.isEnabled(), "the staged layout lost its New run button"
    assert staged.run_view.isVisible() and staged.run_view.step_list.count() >= 20, "the finished run vanished from the staged layout"
    _no_overlay(window, staged, three)
    _finish(window)


def test_a_layout_switch_mid_run_does_not_change_the_session_state(qapp, tmp_path, fake_pdf):
    """A View switch is not an Operator action: it cannot start or restart anything.

    The 60-step schedule pauses on a Stop-on-Fail failure at step 30, so the run is provably still
    up when the layout is switched back and forth."""
    window, schedule = _shown_window(qapp, tmp_path, "staged", steps=60, fail_at=30)
    _fill_and_start(qapp, window, schedule)
    assert _wait(qapp, lambda: window.session.pause_pending()), "the run never paused on its failure"
    assert window.session.state == SessionState.STARTED
    before = window.session.selected_schedule
    window.set_layout("threePane")
    qapp.processEvents()
    window.set_layout("staged")
    qapp.processEvents()
    assert window.session.state == SessionState.STARTED, "switching layouts changed a running session's state"
    assert window.session.selected_schedule == before, "switching layouts changed the schedule"
    _finish(window)


# -- 5, 6, 7. the run view's text and its room ----------------------------------


def test_the_log_tab_holds_only_the_current_run(qapp, tmp_path, fake_pdf):
    """Packet 30b review: the Log tab appended, so a second run read as one long log."""
    window, schedule = _shown_window(qapp, tmp_path, "staged")
    staged = window.staged_screen
    _fill_and_start(qapp, window, schedule)
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "the first run never finished"
    first_lines = staged.run_view.log_tab.text().splitlines()
    assert len(first_lines) == 20, f"the Log tab did not hold the first run's 20 Attempt lines: {len(first_lines)}"

    staged.run_view.new_run_button.click()
    for _ in range(100):
        qapp.processEvents()
    staged.steps_options_stage.run_button.click()
    assert _wait(qapp, lambda: window.session.state == SessionState.STARTED), "the second run never started"
    assert staged.run_view.log_tab.text() == "", "the finished run's log is still on screen while the new run is up"
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "the second run never finished"
    second_lines = staged.run_view.log_tab.text().splitlines()
    assert len(second_lines) == 20, f"the Log tab accumulated runs: {len(second_lines)} lines after the second run"
    assert second_lines == first_lines, "the Log tab is not the current run's own lines"
    _finish(window)


def test_the_log_and_summary_panes_are_read_only(qapp, tmp_path, fake_pdf):
    """The Operator reads the Log and Summary; the run writes them."""
    window, schedule = _shown_window(qapp, tmp_path, "staged")
    staged = window.staged_screen
    _fill_and_start(qapp, window, schedule)
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "the run never finished"
    for pane in (staged.run_view.log_tab, staged.run_view.summary_tab):
        assert pane.isReadOnly(), "the Log/Summary pane is editable"
        pane.setPlainText("typed by the run")  # the run may still write it
        assert pane.text() == "typed by the run", "a read-only pane cannot be written by the run"
    _finish(window)


def test_the_bottom_band_grows_with_the_window_not_against_a_ceiling(qapp, tmp_path, fake_pdf):
    """Packet 30b review: the tabs, log and debug panes had absolute ceilings (220/150/110).

    They now have floors and stretch factors, so a taller window gives them more room while the
    step list keeps its floor and the 1280x800 window keeps its size."""
    small, schedule = _shown_window(qapp, tmp_path, "staged")
    view = small.staged_screen.run_view
    assert view.tabs.maximumHeight() == QWIDGETSIZE_MAX, "the tabs still carry a ceiling"
    assert view.log_pane.maximumHeight() == QWIDGETSIZE_MAX, "the log pane still carries a ceiling"
    assert view.debug_window.maximumHeight() == QWIDGETSIZE_MAX, "the debug window still carries a ceiling"
    assert view.step_list.minimumHeight() == STEP_LIST_FLOOR, "the step list lost its floor"

    _fill_and_start(qapp, small, schedule)
    assert _wait(qapp, lambda: small.session.state == SessionState.FINISHED), "the run never finished"
    for _ in range(200):
        qapp.processEvents()
    assert small.size() == QSize(*WINDOW_SIZE), f"the finished run grew the window to {small.size()}"
    running = view.step_list.viewport().height()
    finished = view.step_list.viewport().height()
    assert finished >= STEP_LIST_FLOOR, f"the step list viewport collapsed to {finished} px"
    log_small = view.log_pane.height()
    tabs_small = view.tabs.height()
    debug_small = view.debug_window.height()
    _finish(small)

    big, schedule = _shown_window(qapp, tmp_path, "staged", TALL_WINDOW)
    big_view = big.staged_screen.run_view
    _fill_and_start(qapp, big, schedule)
    assert _wait(qapp, lambda: big.session.state == SessionState.FINISHED), "the run never finished"
    for _ in range(200):
        qapp.processEvents()
    assert big.size() == QSize(*TALL_WINDOW), f"the finished run grew the tall window to {big.size()}"
    assert big_view.log_pane.height() > log_small, (
        f"the log pane did not grow with the window: {big_view.log_pane.height()} px at {TALL_WINDOW} vs {log_small} px at {WINDOW_SIZE}"
    )
    assert big_view.tabs.height() > tabs_small, "the Log/Summary tabs did not grow with the window"
    assert big_view.debug_window.height() > debug_small, "the debug pane did not grow with the window"
    # The stretch order is what makes the step list dominate the view at both sizes.
    assert view.step_list.viewport().height() > log_small, "the step list is not the view's largest pane"
    assert big_view.step_list.viewport().height() > big_view.log_pane.height(), "the step list is not the view's largest pane"
    _finish(big)


# -- opt-in screenshots for review ----------------------------------------------


def test_30b_review_screenshots(qapp, tmp_path, fake_pdf):
    """Write the review set to ``ATE_30B_SHOTS`` (e.g. /tmp/30b-r2-shots) for a human to look at.

    Every other test in this file writes its shots under ``tmp_path``; this one is opt-in so the suite
    stays hermetic without it."""
    shots = Path(os.environ.get("ATE_30B_SHOTS", ""))
    if not shots:
        pytest.skip("review screenshots are opt-in: set ATE_30B_SHOTS to a directory")
    shots.mkdir(parents=True, exist_ok=True)

    def shot(window, name) -> None:
        for _ in range(50):
            qapp.processEvents()
        window.grab().save(str(shots / name))

    window, schedule = _shown_window(qapp, tmp_path, "threePane")
    _fill_and_start(qapp, window, schedule)
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "the run never finished"
    shot(window, "three-pane-finished.png")
    _no_overlay(window, window.three_pane_screen, window.staged_screen)
    _finish(window)

    window, schedule = _shown_window(qapp, tmp_path, "staged")
    staged = window.staged_screen
    _fill_and_start(qapp, window, schedule)
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "the run never finished"
    shot(window, "staged-finished.png")

    staged.run_view.new_run_button.click()
    for _ in range(100):
        qapp.processEvents()
    shot(window, "staged-after-new-run-stage1.png")

    staged.uut_operator_stage.next_button.click()
    staged.test_schedule_stage.run_button.click()
    for _ in range(100):
        qapp.processEvents()
    staged.steps_options_stage.step_list.item(19).setCheckState(Qt.CheckState.Unchecked)
    shot(window, "staged-stage3-after-new-run-toggled.png")
    _finish(window)

    window, schedule = _shown_window(qapp, tmp_path, "staged")
    staged = window.staged_screen
    _fill_and_start(qapp, window, schedule)
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "the run never finished"
    window.show_history()
    qapp.processEvents()
    model = window.history_screen.table_view.model()
    window.history_screen.table_view.setCurrentIndex(model.index(0, 0))
    qapp.processEvents()
    window.history_screen.new_run_button.click()
    for _ in range(100):
        qapp.processEvents()
    shot(window, "history-new-run.png")
    _finish(window)

    window, schedule = _shown_window(qapp, tmp_path, "staged")
    staged = window.staged_screen
    _fill_and_start(qapp, window, schedule)
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "the run never finished"
    window.set_layout("threePane")
    qapp.processEvents()
    window.set_layout("staged")
    for _ in range(100):
        qapp.processEvents()
    shot(window, "staged-after-switch-round-trip.png")
    assert window.session.state == SessionState.FINISHED
    _finish(window)

    window, schedule = _shown_window(qapp, tmp_path, "staged", TALL_WINDOW)
    _fill_and_start(qapp, window, schedule)
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "the run never finished"
    shot(window, "staged-finished-1920x1200.png")
    _finish(window)
