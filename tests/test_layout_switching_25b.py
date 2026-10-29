"""Packet 25b — the layout setting and the View-menu switch, offscreen against real runs.

``ui.layout`` picks the layout the window opens in; the View menu's Layout group switches the
session's Run screen without touching a run in progress. Both layouts are built at launch and both
listen to the same session, so a switch is only a change of the visible pane: the hidden screen
keeps its state and its controls are inert while it is not the one showing.
"""

import json
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QWidget

from ate.app import launch
from ate.run_session.headless import SessionState
from ate.storage.store import RunStore
from ate.ui.staged_run_screen import StagedRunScreen
from ate.ui.three_pane_run_screen import ThreePaneRunScreen

from ._fixtures import WRITES_PDF, add_logging_input_schedule, fake_pdf_helper, write_ate_env

SHOTS = Path("/tmp/25b-shots")
FIELDS = ("operator", "project", "uut_type", "serial")


@pytest.fixture
def fake_pdf(monkeypatch):
    """Stand in for the QWebEngine PDF helper so a finished run's report job stays cheap."""
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", fake_pdf_helper(WRITES_PDF))


def _launch(env, tmp_path, ui=None):
    """Launch against ``env``, splicing ``ui`` into the config first; dialogs are injected."""
    config = env["config"]
    if ui is not None:
        data = json.loads(config.read_text(encoding="utf-8"))
        data["ui"] = ui
        config = tmp_path / "layout-config.json"
        config.write_text(json.dumps(data), encoding="utf-8")
    alerts: list[str] = []
    outcome = launch(
        ["--config", str(config)],
        exec_=False,
        prompt=lambda path: True,
        alert=alerts.append,
    )
    return outcome, alerts


def _wait(qapp, predicate, timeout=10.0) -> bool:
    """Pump the event loop until ``predicate`` is true, or give up."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _pick_and_start(qapp, window, schedule, stop_on_fail=True, **fields):
    """Select ``schedule`` on the session and start a run; return (input asks, failure prompts)."""
    session = window.session
    asked: list[tuple[int, int, int]] = []
    prompts: list[tuple[int, int]] = []
    session.input_asked.connect(lambda seq, attempt, index: asked.append((seq, attempt, index)))
    session.failure_prompt.connect(lambda seq, attempt: prompts.append((seq, attempt)))
    session.select_schedule(schedule)
    session.set_operator(fields.get("operator", "Ada Lovelace"))
    session.set_project(fields.get("project", "PSU-200 Production"))
    session.set_uut_type(fields.get("uut_type", "PowerSupply"))
    session.set_uut_serial(fields.get("uut_serial", "PS-1"))
    session.set_stop_on_fail(stop_on_fail)
    session.start()
    assert window.session.state == SessionState.STARTED, "the run is in progress"
    _wait(qapp, lambda: asked or prompts)
    return asked, prompts


def _finish(qapp, window):
    """Abort whatever is still running and let the report helper exit, so teardown is clean."""
    if window.session.state != SessionState.FINISHED:
        try:
            window.session.request_abort()
        except Exception:
            pass
        _wait(qapp, lambda: window.session.state == SessionState.FINISHED)
    from ate.report.pdf_job import _live_pdf_jobs

    deadline = time.time() + 12
    while _live_pdf_jobs and time.time() < deadline:
        qapp.processEvents()


def _select_schedule(screen, name) -> bool:
    """Make the picker's row for ``name`` current, as the Operator's click would."""
    list_widget = screen.picker.schedule_list
    for index in range(list_widget.count()):
        item = list_widget.item(index)
        if item.data(Qt.ItemDataRole.UserRole) == name:
            list_widget.setCurrentItem(item)
            return True
    return False


def _fill_fields(screen, values: dict[str, str]) -> None:
    """Type the setup fields on one layout."""
    for key, text in values.items():
        getattr(screen.fields, f"{key}_edit").setText(text)


def _field_texts(screen) -> dict[str, str]:
    """What every setup field on this layout currently says."""
    return {key: getattr(screen.fields, f"{key}_edit").text() for key in FIELDS}


# -- the configured layout -----------------------------------------------------


def test_default_config_launches_the_staged_screen(qapp, tmp_path):
    """No ``ui.layout`` means the staged Run screen is the one the window opens on."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    outcome, alerts = _launch(env, tmp_path)
    window = outcome.window
    assert isinstance(window.run_screen, StagedRunScreen), alerts
    assert window.stack.currentWidget() is window.run_screen
    assert window.staged_layout_action.isChecked()
    assert not window.three_pane_layout_action.isChecked()


def test_ui_layout_three_pane_launches_the_three_pane_screen(qapp, tmp_path):
    """``ui.layout: threePane`` opens the three-pane Run screen, and the menu marks it."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    outcome, alerts = _launch(env, tmp_path, ui={"layout": "threePane"})
    window = outcome.window
    assert isinstance(window.run_screen, ThreePaneRunScreen), alerts
    assert window.stack.currentWidget() is window.run_screen
    assert window.three_pane_layout_action.isChecked()
    assert not window.staged_layout_action.isChecked()


def test_an_invalid_layout_refuses_the_launch(qapp, tmp_path):
    """A layout word the config does not know stops the launch with its reason."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    outcome, alerts = _launch(env, tmp_path, ui={"layout": "panorama"})
    assert outcome.code == 1 and outcome.window is None
    assert any("ui.layout" in message for message in alerts), alerts


# -- switching before a run ----------------------------------------------------


def test_the_view_menu_switches_layouts_before_a_run_and_carries_the_fields(qapp, tmp_path):
    """The Layout group swaps the screen and carries the Operator's setup across the switch."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    outcome, _ = _launch(env, tmp_path)
    window = outcome.window
    staged = window.staged_screen

    _fill_fields(staged, {"operator": "Ada", "project": "Board A", "uut_type": "PowerSupply", "serial": "PS-77"})
    assert _select_schedule(staged, "Blocking input schedule")
    staged.steps_options_stage.continue_all_radio.setChecked(True)

    window.three_pane_layout_action.trigger()
    qapp.processEvents()
    assert window.stack.currentWidget() is window.three_pane_screen
    three = window.three_pane_screen
    assert _field_texts(three) == {
        "operator": "Ada",
        "project": "Board A",
        "uut_type": "PowerSupply",
        "serial": "PS-77",
    }
    assert three.picker.selected_name() == "Blocking input schedule"
    assert three.stop_on_fail_radio.isChecked() is False
    assert three.continue_all_radio.isChecked() is True
    assert three.table.table.rowCount() > 0, "the picked schedule's steps preview came along too"

    # And back again: the same values, the same pick.
    window.staged_layout_action.trigger()
    qapp.processEvents()
    assert window.stack.currentWidget() is staged
    assert _field_texts(staged) == {
        "operator": "Ada",
        "project": "Board A",
        "uut_type": "PowerSupply",
        "serial": "PS-77",
    }
    assert staged.picker.selected_name() == "Blocking input schedule"
    assert staged.steps_options_stage.continue_all_radio.isChecked() is True


def test_history_is_still_reachable_after_a_layout_switch(qapp, tmp_path):
    """Layout switching is independent of the History view from 30a."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    outcome, _ = _launch(env, tmp_path)
    window = outcome.window
    window.three_pane_layout_action.trigger()
    qapp.processEvents()
    window.history_action.trigger()
    qapp.processEvents()
    assert window.stack.currentWidget() is window.history_screen
    window.run_action.trigger()
    qapp.processEvents()
    assert window.stack.currentWidget() is window.three_pane_screen


# -- switching mid-run ---------------------------------------------------------


def test_a_mid_run_switch_answers_the_input_in_the_new_layout(qapp, tmp_path, fake_pdf):
    """Switch layouts while a step waits on an input, answer in the new one: one answer, run ends."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    schedule = add_logging_input_schedule(tmp_path)  # logs before it blocks, so the log panes have lines
    outcome, _ = _launch(env, tmp_path)
    window = outcome.window
    asked, _ = _pick_and_start(qapp, window, schedule)
    assert asked, "the step announced its inputs"
    seq = asked[0][0]
    index = 1  # the blocking INTEGER input; the POLL (index 2) is announced first
    assert _wait(qapp, lambda: any(a[2] == index for a in asked)), "the step is waiting for its input"
    staged = window.staged_screen
    waiting = staged.input_widget(seq, index)
    assert waiting is not None and waiting.ok_button.isEnabled()

    window.three_pane_layout_action.trigger()
    qapp.processEvents()
    three = window.three_pane_screen
    assert window.stack.currentWidget() is three
    # The run is untouched: the hidden layout still says the run is up, and the new one shows it.
    assert staged.state == SessionState.STARTED
    assert three.state == SessionState.STARTED
    new_waiting = three.input_widget(seq, index)
    assert new_waiting is not None and new_waiting.ok_button.isEnabled(), "the new layout shows the waiting input"
    assert three.status_bar.status_label.text() == "Running"
    assert three.status_bar.status_total.text().startswith("Run ")
    # The worker's log is live on both layouts, line for line.
    assert _wait(qapp, lambda: staged.log_pane.rendered_lines()), "the worker's log reached the log pane"
    assert three.log_pane.rendered_lines() == staged.log_pane.rendered_lines()

    new_waiting.line_edit.setText("42")
    new_waiting.ok_button.click()
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED)

    rows = window.session.store.input_values(window.session.run_uuid)
    assert len(rows) == 1, f"exactly one answer recorded, got {rows}"
    assert rows[0]["value"] == "42"

    # The answers and results are on screen whichever layout is showing.
    assert three.log_pane.rendered_lines()
    window.staged_layout_action.trigger()
    qapp.processEvents()
    assert window.stack.currentWidget() is staged
    # Re-selecting the schedule unlocks the step boxes for the next run (spec 37), so the session is
    # no longer FINISHED here; the finished run's results are still on this layout.
    assert staged.run_view.status_bar.status_label.text() == "Finished"
    assert three.status_bar.status_label.text() == "Finished"
    shown = staged.input_widget(seq, index)
    assert shown is not None and shown.line_edit.text() == "42"
    step_rows = [staged.run_view.step_list.item(i).text() for i in range(staged.run_view.step_list.count())]
    assert any("PASS" in row for row in step_rows), step_rows


def test_the_hidden_layout_ignores_its_own_clicks(qapp, tmp_path, fake_pdf):
    """Controls on the layout that is not showing cannot touch the run."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    outcome, _ = _launch(env, tmp_path)
    window = outcome.window
    asked, _ = _pick_and_start(qapp, window, "Blocking input schedule")
    seq, attempt, index = asked[0]
    window.three_pane_layout_action.trigger()
    qapp.processEvents()

    hidden = window.staged_screen
    assert hidden.is_active() is False
    assert window.three_pane_screen.is_active() is True

    # Clicking the hidden layout's Start, Abort and input all do nothing.
    run_uuid = window.session.run_uuid
    hidden.start_button.click()
    hidden.run_view.abort_button.click()
    hidden_widget = hidden.input_widget(seq, index)
    assert hidden_widget is not None and hidden_widget.ok_button.isEnabled()
    hidden_widget.ok_button.click()
    qapp.processEvents()
    assert hidden_widget.error_label.text() == "", "the hidden layout answered, which it must not"
    assert window.session.run_uuid == run_uuid, "no second run started"
    assert window.session.state == SessionState.STARTED

    # The visible layout answers, and the run finishes on that one answer.
    shown = window.three_pane_screen.input_widget(seq, index)
    shown.line_edit.setText("7")
    shown.ok_button.click()
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED)
    rows = window.session.store.input_values(window.session.run_uuid)
    assert len(rows) == 1 and rows[0]["value"] == "7"
    _finish(qapp, window)


def test_a_pause_switch_shows_the_prompt_in_the_new_layout_and_continue_resumes(qapp, tmp_path, fake_pdf):
    """Switch layouts while a Stop-on-Fail pause waits, then Continue in the new layout."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    outcome, _ = _launch(env, tmp_path)
    window = outcome.window
    window.show()
    qapp.processEvents()
    _asked, prompts = _pick_and_start(qapp, window, "Fail then pass schedule")
    assert prompts, "the failing step prompted the Operator"
    staged = window.staged_screen
    assert staged.run_view.failure_prompt_bar.isVisible()

    window.three_pane_layout_action.trigger()
    qapp.processEvents()
    three = window.three_pane_screen
    seq = prompts[0][0]
    assert three.table.banner.isVisible(), "the pause is visible in the layout that is showing"
    bar = three.table._prompt_bars.get(seq)
    assert bar is not None and bar.isVisible()
    assert "Step 1." in bar.step_label.text()
    assert three.status_bar.status_label.text() == "Paused"

    bar.continue_button.click()
    qapp.processEvents()
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED)
    nodes = window.session.store.nodes(window.session.run_uuid)
    results = {node["node_seq"]: node.get("result") for node in nodes if node.get("kind") == "step"}
    assert results.get(1) == "PASS", f"Continue let the second step run: {results}"
    assert results.get(0) == "FAIL"
    _finish(qapp, window)


def test_allow_layout_switch_false_disables_the_switch(qapp, tmp_path):
    """``ui.allowLayoutSwitch: false`` fixes the Station to its configured layout."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    outcome, _ = _launch(env, tmp_path, ui={"allowLayoutSwitch": False})
    window = outcome.window
    assert window.staged_layout_action.isEnabled() is False
    assert window.three_pane_layout_action.isEnabled() is False
    window.three_pane_layout_action.trigger()
    qapp.processEvents()
    window.set_layout("threePane")
    qapp.processEvents()
    assert isinstance(window.run_screen, StagedRunScreen)
    assert window.stack.currentWidget() is window.staged_screen
    assert window.staged_layout_action.isChecked()


def test_a_switch_never_writes_the_config(qapp, tmp_path):
    """Switching layouts is for this session only: the config file on disk is untouched."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    outcome, _ = _launch(env, tmp_path)
    window = outcome.window
    before = env["config"].read_text(encoding="utf-8")
    window.three_pane_layout_action.trigger()
    qapp.processEvents()
    window.staged_layout_action.trigger()
    qapp.processEvents()
    assert env["config"].read_text(encoding="utf-8") == before


# -- the screenshots -----------------------------------------------------------


def _window_shot(window) -> QImage:
    """A 1280x800 grab of the whole window, as a reviewer would see it."""
    window.resize(1280, 800)
    window.show()
    image = window.grab().toImage()
    assert window.isVisible() and image.width() == 1280 and image.height() == 800
    return image


def _menu_shot(window) -> QImage:
    """The View menu open, with its Layout submenu, drawn on top of the window it belongs to."""
    from PyQt6.QtGui import QPainter

    view = window.view_menu
    view.adjustSize()
    submenu = window.layout_menu
    submenu.adjustSize()
    image = _window_shot(window)
    painter = QPainter(image)
    view_pix = view.grab()
    sub_pix = submenu.grab()
    painter.drawPixmap(6, 30, view_pix)
    painter.drawPixmap(6, 30 + view_pix.height() + 6, sub_pix)
    painter.end()
    assert view_pix.width() > 80 and view_pix.height() > 50, "the View menu painted nothing"
    assert _dark_pixel_count(sub_pix.toImage()) > 100, "the Layout submenu painted nothing"
    return image


def _dark_pixel_count(image: QImage) -> int:
    """How many pixels of an image are dark enough to be text or lines."""
    return sum(
        1
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y).lightness() < 90
    )


def test_the_three_screenshot_states_are_saved_for_review(qapp, tmp_path, fake_pdf):
    """The View menu with the Layout choice, a mid-run three-pane switch, and the switch back."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    schedule = add_logging_input_schedule(tmp_path)
    outcome, _ = _launch(env, tmp_path)
    window = outcome.window

    menu_image = _menu_shot(window)
    assert menu_image.width() == 1280 and menu_image.height() == 800
    assert _dark_pixel_count(menu_image) > 400, "the open menu painted nothing"
    assert menu_image.save(str(SHOTS / "view-menu-open.png")), "the menu grab did not save"

    asked, _ = _pick_and_start(qapp, window, schedule)
    seq = asked[0][0]
    _wait(qapp, lambda: any(a[2] == 1 for a in asked))
    window.three_pane_layout_action.trigger()
    qapp.processEvents()
    _wait(qapp, lambda: window.three_pane_screen.log_pane.rendered_lines())
    three_shot = _window_shot(window)
    assert window.stack.currentWidget() is window.three_pane_screen
    assert window.three_pane_screen.input_widget(seq, 1).ok_button.isEnabled()
    assert three_shot.save(str(SHOTS / "mid-run-three-pane.png")), "the three-pane grab did not save"

    window.staged_layout_action.trigger()
    qapp.processEvents()
    staged_shot = _window_shot(window)
    assert window.stack.currentWidget() is window.staged_screen
    assert window.staged_screen.input_widget(seq, 1).ok_button.isEnabled()
    assert staged_shot.save(str(SHOTS / "switched-back-staged.png")), "the staged grab did not save"
    assert three_shot != staged_shot, "switching layouts changed nothing on screen"

    _finish(qapp, window)

    for name in ("view-menu-open.png", "mid-run-three-pane.png", "switched-back-staged.png"):
        path = SHOTS / name
        assert path.exists() and path.stat().st_size > 1000, name
        saved = QImage(str(path))
        assert saved.width() == 1280 and saved.height() == 800, name
        assert _dark_pixel_count(saved) > 300, f"{name} is blank"
    assert isinstance(window.staged_screen, StagedRunScreen) and isinstance(window.three_pane_screen, ThreePaneRunScreen)
