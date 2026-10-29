"""Packet 25b — the layout setting and the View-menu switch, offscreen against real runs.

``ui.layout`` picks the layout the window opens in; the View menu's Layout group switches the
session's Run screen without touching a run in progress. Both layouts are built at launch and both
listen to the same session, so a switch is only a change of the visible pane: the hidden screen
keeps its state and its controls are inert while it is not the one showing.
"""

import inspect
import json
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QPushButton, QWidget

from ate.app import launch
from ate.run_session.headless import SessionState
from ate.storage.store import RunStore
from ate.ui.staged_run_screen import StagedRunScreen
from ate.ui.three_pane_run_screen import ThreePaneRunScreen

from ._fixtures import WRITES_PDF, _append_file, add_logging_input_schedule, fake_pdf_helper, write_ate_env

SHOTS = Path("/tmp/25b-r2-shots")
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


def _wait(qapp, predicate, timeout: float = 10.0) -> bool:
    """Pump the event loop until ``predicate`` is true, and say so loudly when it never is.

    Packet 25b review: a wait used to give up quietly after 10 s, so a run that never reached the
    state being waited for still "passed" while costing the suite ten seconds. Every wait here is for
    a signal or a state the run really reaches, and a timeout is a failure."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    raise AssertionError(
        f"no match for {inspect.getsource(predicate).strip()} within {timeout} s"
    )


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


def _finish(qapp, window) -> None:
    """End the run and its PDF helper, so the next test starts clean.

    The wait is on the worker stopping, not on the session's state label: re-picking a schedule after
    a run has finished puts the session back to SELECTING, so waiting for FINISHED here never landed
    and burned the whole timeout (packet 25b review). A Stop-on-Fail pause still outstanding is
    answered with Abort first — the worker blocks in the pause until someone chooses, and an abort
    requested from outside is not a choice."""
    session = window.session
    engine = getattr(session, "_engine", None)
    if engine is not None and engine.isRunning():
        if session.pause_pending():
            session.choose("ABORT")
        else:
            session.request_abort()
        _wait(qapp, lambda: not engine.isRunning())
    from ate.report.pdf_job import stop_pdf_jobs

    stop_pdf_jobs()


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


def _abort_button(screen) -> QPushButton:
    """The Abort control of either layout: the staged one keeps its in the Run view."""
    abort = getattr(screen, "abort_button", None)
    return abort if abort is not None else screen.run_view.abort_button


def _inert_widgets(screen) -> list[QWidget]:
    """Every control a layout that is not showing must not act on, or hold the keyboard for."""
    widgets = [
        screen.start_button,
        screen.fields.operator_edit,
        screen.fields.project_edit,
        screen.fields.uut_type_edit,
        screen.fields.serial_edit,
        screen.picker.filter_edit,
        screen.stop_on_fail_radio,
    ]
    abort = getattr(screen, "abort_button", None)
    if abort is not None:
        widgets.append(abort)
    return widgets


# -- only the layout on screen is live -----------------------------------------


@pytest.mark.parametrize("layout", ("staged", "threePane"))
def test_the_layout_not_showing_is_inert_from_launch(qapp, tmp_path, layout):
    """Whichever layout `ui.layout` names, the other is inactive: Start, Abort, Enter do nothing and
    none of its widgets holds the keyboard."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    schedule = add_logging_input_schedule(tmp_path)
    outcome, _ = _launch(env, tmp_path, ui={"layout": layout})
    window = outcome.window
    window.show()  # a widget that is not visible cannot take the focus at all
    qapp.processEvents()
    shown = window.run_screens[layout]
    hidden = window.run_screens["threePane" if layout == "staged" else "staged"]
    assert shown.is_active() is True, "the layout on screen is not marked active"
    assert hidden.is_active() is False, "the layout not on screen is active at launch"

    # No widget of the layout that is not showing can have the focus.
    focus = QApplication.focusWidget()
    for widget in _inert_widgets(hidden):
        assert widget is not focus and widget.hasFocus() is False, f"{widget.objectName() or widget} holds focus"

    # Its Start does not start a run, even though its own fields and picker are filled in.
    _fill_fields(hidden, {"operator": "Ada Lovelace", "project": "Board A", "uut_type": "PowerSupply", "serial": "PS-9"})
    assert _select_schedule(hidden, schedule)
    hidden.start_button.click()
    qapp.processEvents()
    assert window.session.state != SessionState.STARTED, "the hidden layout started a run"
    assert window.session.run_uuid is None

    # Its Abort does not abort a run the visible layout is running.
    asked, _prompts = _pick_and_start(qapp, window, schedule)
    seq, _attempt, _first_index = asked[0]
    index = 1  # the blocking INTEGER input; the POLL (index 2) is announced first
    assert _wait(qapp, lambda: any(a[2] == index for a in asked)), "the step is waiting for its input"
    hidden_abort = _abort_button(hidden)
    assert hidden_abort.isEnabled(), "both layouts keep their Abort live while a run is up"
    hidden_abort.click()
    qapp.processEvents()
    assert window.session.state == SessionState.STARTED, "the hidden layout aborted the run"

    # Enter in the hidden layout's waiting input answers nothing, and it does not have the focus.
    hidden_widget = hidden.input_widget(seq, index)
    assert hidden_widget is not None and hidden_widget.ok_button.isEnabled()
    assert QApplication.focusWidget() is not hidden_widget.line_edit
    QTest.keyClicks(hidden_widget.line_edit, "42")
    QTest.keyClick(hidden_widget.line_edit, Qt.Key.Key_Return)
    qapp.processEvents()
    assert hidden_widget.error_label.text() == "", "the hidden layout answered an input"
    assert window.session.store.input_values(window.session.run_uuid) == []

    shown_widget = shown.input_widget(seq, index)
    assert QApplication.focusWidget() is shown_widget.line_edit, "the waiting input did not take the focus"
    shown_widget.line_edit.setText("42")
    shown_widget.ok_button.click()
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED)
    rows = window.session.store.input_values(window.session.run_uuid)
    assert len(rows) == 1 and rows[0]["value"] == "42", rows
    run_row = window.session.store.get_test_run(window.session.run_uuid)
    assert run_row["end_reason"] != "ABORTED", "the hidden layout's Abort stopped the run"
    _finish(qapp, window)


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


# -- a finished run, a pause, and the keyboard survive the switch -------------

PAUSE_AND_BLOCK = """\
return ScheduleParams("Pause and block schedule", "a logged failure, then a blocking step")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("first"):setStepNo("1.")
      :setAction(function()
        ATE.log(Log.INFO, "the first step is failing")
        ATE.report(Result.FAIL, "wrong colour", "Red", "Green")
      end),
    StepParams("second"):setStepNo("2.")
      :setInputs({ [1] = { desc = "voltage", type = "INTEGER" } })
      :setAction(function()
        ATE.log(Log.DEBUG, "probe chatter before the ask")
        local v = ATE.input(1)
        ATE.report(Result.PASS, "voltage " .. v, "7", "7")
      end),
  })
"""


def _add_pause_and_block_schedule(tmp_path: Path) -> str:
    """A schedule that fails (pausing the run), logs, then blocks on an input: results, a log and a
    run still in progress, all at once."""
    _append_file(tmp_path / "defs", "pause_block.tslua", PAUSE_AND_BLOCK)
    return "Pause and block schedule"


def _staged_step_texts(screen) -> list[str]:
    """The staged Run view's step rows, as the Operator reads them."""
    return [screen.run_view.step_list.item(i).text() for i in range(screen.run_view.step_list.count())]


def _three_step_results(screen) -> dict[int, tuple[str, str]]:
    """``node_seq -> (Result, Attempts)`` for every step row in the three-pane centre table."""
    results = {}
    for seq, row in screen.table._step_rows.items():
        results[seq] = (screen.table.cell(row, 2), screen.table.cell(row, 3))
    return results


def test_a_finished_run_keeps_its_results_across_both_switches(qapp, tmp_path):
    """Finish a run with a FAIL step and a PASS step, then switch both ways: the results, the
    Attempts and the log are still on screen in the layout that comes to the front."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    schedule = _add_pause_and_block_schedule(tmp_path)
    outcome, _ = _launch(env, tmp_path)
    window = outcome.window
    window.show()
    qapp.processEvents()
    asked, prompts = _pick_and_start(qapp, window, schedule)
    assert prompts, "the failing step paused the run"
    window.staged_screen.run_view.failure_prompt_bar.continue_button.click()
    qapp.processEvents()
    assert window.session.state == SessionState.STARTED, "the run resumed"
    assert _wait(qapp, lambda: any(a[2] == 1 for a in asked)), "step 2 never asked for its input"
    answer = window.staged_screen.input_widget(1, 1)
    answer.line_edit.setText("7")
    answer.ok_button.click()
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED)
    qapp.processEvents()
    staged, three = window.staged_screen, window.three_pane_screen

    rows = _staged_step_texts(staged)
    assert any("FAIL" in row for row in rows) and any("PASS" in row for row in rows), rows
    assert staged.log_pane.rendered_lines(), "the staged log lost the run's log"

    window.three_pane_layout_action.trigger()
    qapp.processEvents()
    results = _three_step_results(three)
    assert results.get(0) == ("FAIL", "1"), results
    assert results.get(1) == ("PASS", "1"), results
    assert any("the first step is failing" in line for line in three.log_pane.rendered_lines()), three.log_pane.rendered_lines()
    assert any("probe chatter" in line for line in three.debug_window.rendered_lines())

    window.staged_layout_action.trigger()
    qapp.processEvents()
    rows = _staged_step_texts(staged)
    assert any("FAIL" in row for row in rows) and any("PASS" in row for row in rows), rows
    assert any("the first step is failing" in line for line in staged.log_pane.rendered_lines()), (
        "the log went empty when staged came back"
    )
    first_row = three.table.step_row(0)
    assert three.table.cell(first_row, 4) == "Red" and three.table.cell(first_row, 5) == "Green"
    assert "wrong colour" in three.table.cell(first_row, 6)


def test_a_finished_run_is_drawn_only_when_the_hidden_layout_comes_back(qapp, tmp_path):
    """The layout that is not showing does not read the finished run; it draws it when switched to."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    schedule = _add_pause_and_block_schedule(tmp_path)
    outcome, _ = _launch(env, tmp_path, ui={"layout": "threePane"})
    window = outcome.window
    window.show()
    qapp.processEvents()
    staged, three = window.staged_screen, window.three_pane_screen

    asked, _ = _pick_and_start(qapp, window, schedule)
    three.table._prompt_bars[0].continue_button.click()
    qapp.processEvents()
    assert _wait(qapp, lambda: any(a[2] == 1 for a in asked)), "step 2 never asked for its input"
    answer = three.input_widget(1, 1)
    answer.line_edit.setText("7")
    answer.ok_button.click()
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED)
    qapp.processEvents()

    # The visible layout has the results; the hidden one has not read them yet.
    assert any(result == "PASS" for result, _ in _three_step_results(three).values()), (
        "the visible layout lost the run"
    )
    hidden_rows = _staged_step_texts(staged)
    assert not any("PASS" in row for row in hidden_rows), (
        f"the hidden staged layout read the finished run while it was not showing: {hidden_rows}"
    )

    window.staged_layout_action.trigger()
    qapp.processEvents()
    rows = _staged_step_texts(staged)
    assert any("FAIL" in row for row in rows) and any("PASS" in row for row in rows), rows
    assert any("the first step is failing" in line for line in staged.log_pane.rendered_lines())
    _finish(qapp, window)

    # The other way round: a run finished while the three-pane layout was hidden is drawn when the
    # Operator switches to it, and not before.
    outcome2, _ = _launch(env, tmp_path)
    window2 = outcome2.window
    window2.show()
    qapp.processEvents()
    staged2, three2 = window2.staged_screen, window2.three_pane_screen
    asked2, prompts2 = _pick_and_start(qapp, window2, schedule)
    staged2.run_view.failure_prompt_bar.continue_button.click()
    qapp.processEvents()
    assert _wait(qapp, lambda: any(a[2] == 1 for a in asked2)), "step 2 never asked for its input"
    answer2 = staged2.input_widget(1, 1)
    answer2.line_edit.setText("7")
    answer2.ok_button.click()
    assert _wait(qapp, lambda: window2.session.state == SessionState.FINISHED)
    qapp.processEvents()

    hidden = _three_step_results(three2)
    assert not any(result for result, _ in hidden.values()), (
        f"the hidden three-pane layout read the finished run while it was not showing: {hidden}"
    )
    window2.three_pane_layout_action.trigger()
    qapp.processEvents()
    results = _three_step_results(three2)
    assert results.get(0) == ("FAIL", "1"), results
    assert results.get(1) == ("PASS", "1"), results
    assert three2.log_pane.rendered_lines(), "the three-pane log stayed empty after the switch"
    _finish(qapp, window2)


def test_a_pause_answered_in_one_layout_leaves_no_prompt_in_the_other(qapp, tmp_path):
    """Answer a Stop-on-Fail pause in one layout: the other shows no prompt, and its Abort aborts
    the run on the first click rather than answering a prompt that is no longer there."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    schedule = _add_pause_and_block_schedule(tmp_path)
    outcome, _ = _launch(env, tmp_path)
    window = outcome.window
    window.show()
    qapp.processEvents()
    staged, three = window.staged_screen, window.three_pane_screen

    # Start in the three-pane layout and answer the pause there.
    window.three_pane_layout_action.trigger()
    qapp.processEvents()
    _asked, prompts = _pick_and_start(qapp, window, schedule)
    assert prompts, "the failing step paused the run"
    bar = three.table._prompt_bars[prompts[0][0]]
    assert bar.isVisible()
    bar.continue_button.click()
    qapp.processEvents()
    assert _wait(qapp, lambda: not window.session.pause_pending()), "the pause never resumed"
    assert window.session.state == SessionState.STARTED, "the run is still up"

    # Both layouts say the run is running again: no bar, no banner, no "Paused".
    assert staged.run_view.failure_prompt_bar.isVisible() is False, "the hidden layout kept its prompt bar"
    assert staged._failed_row is None, "the hidden layout still highlights the paused step"
    assert three.table.banner.isVisible() is False
    assert three.status_bar.status_label.text() == "Running"
    assert staged.run_view.status_bar.status_label.text() != "Paused"

    window.staged_layout_action.trigger()
    qapp.processEvents()
    assert staged.run_view.failure_prompt_bar.isVisible() is False, "the prompt bar came back with the layout"
    assert staged.run_view.status_bar.status_label.text() != "Paused"
    assert staged.run_view.state_header.text() != "WAITING"

    # One click of Abort on this layout aborts the run.
    window.staged_screen.run_view.abort_button.click()
    qapp.processEvents()
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "Abort did nothing"
    row = window.session.store.get_test_run(window.session.run_uuid)
    assert row["end_reason"] == "ABORTED", row
    _finish(qapp, window)


def test_the_mirror_case_pause_answered_in_staged_leaves_the_three_pane_clean(qapp, tmp_path):
    """Same from the other side: answer the pause in staged, switch to three-pane, one Abort aborts."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    schedule = _add_pause_and_block_schedule(tmp_path)
    outcome, _ = _launch(env, tmp_path)
    window = outcome.window
    window.show()
    qapp.processEvents()
    _asked, prompts = _pick_and_start(qapp, window, schedule)
    window.staged_screen.run_view.failure_prompt_bar.continue_button.click()
    qapp.processEvents()
    assert _wait(qapp, lambda: not window.session.pause_pending()), "the pause never resumed"
    assert window.staged_screen.run_view.failure_prompt_bar.isVisible() is False

    window.three_pane_layout_action.trigger()
    qapp.processEvents()
    three = window.three_pane_screen
    assert three.table.banner.isVisible() is False, "the three-pane kept the pause banner"
    assert three._paused_seq is None, "the three-pane still thinks a step is paused"
    assert not [bar for bar in three.table._prompt_bars.values() if bar.isVisible()], "a stale prompt bar"
    assert three.status_bar.status_label.text() != "Paused"

    three.abort_button.click()
    qapp.processEvents()
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "one Abort click did nothing"
    row = window.session.store.get_test_run(window.session.run_uuid)
    assert row["end_reason"] == "ABORTED", row
    _finish(qapp, window)


def test_the_waiting_input_has_the_focus_after_a_mid_run_switch(qapp, tmp_path):
    """Switch layouts while an input is waiting, then answer it from the keyboard alone."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    schedule = add_logging_input_schedule(tmp_path)
    outcome, _ = _launch(env, tmp_path)
    window = outcome.window
    window.show()
    qapp.processEvents()
    asked, _ = _pick_and_start(qapp, window, schedule)
    seq = asked[0][0]
    index = 1  # the blocking INTEGER input
    assert _wait(qapp, lambda: any(a[2] == index for a in asked)), "the step is waiting for its input"

    window.three_pane_layout_action.trigger()
    qapp.processEvents()
    three = window.three_pane_screen
    waiting = three.input_widget(seq, index)
    assert waiting is not None and waiting.line_edit is not None
    assert QApplication.focusWidget() is waiting.line_edit, "the new layout did not take the keyboard"
    QTest.keyClicks(QApplication.focusWidget(), "42")
    QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Return)
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED)
    rows = window.session.store.input_values(window.session.run_uuid)
    assert len(rows) == 1 and rows[0]["value"] == "42", rows
    _finish(qapp, window)

    # And the way back: focus follows the layout that is showing when it is paused.
    outcome2, _ = _launch(env, tmp_path, ui={"layout": "threePane"})
    window2 = outcome2.window
    window2.show()
    qapp.processEvents()
    _asked2, prompts2 = _pick_and_start(qapp, window2, "Fail then pass schedule")
    window2.staged_layout_action.trigger()
    qapp.processEvents()
    staged2 = window2.staged_screen
    assert QApplication.focusWidget() is staged2.run_view.failure_prompt_bar.continue_button, (
        "the paused layout did not focus its prompt bar"
    )
    _finish(qapp, window2)


def test_a_step_box_on_the_hidden_layout_does_not_change_the_run(qapp, tmp_path):
    """Ticking a step's enable box on the layout that is not showing reaches no session."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    outcome, _ = _launch(env, tmp_path)
    window = outcome.window
    window.show()
    qapp.processEvents()
    staged, three = window.staged_screen, window.three_pane_screen
    assert _select_schedule(staged, "Fail then pass schedule")
    staged.test_schedule_stage.run_button.click()  # previews the steps, boxes still editable
    qapp.processEvents()
    assert window.session.node_enables is None

    window.three_pane_layout_action.trigger()
    qapp.processEvents()
    # The staged layout is hidden now: un-ticking one of its boxes must not reach the session.
    staged.steps_options_stage.step_list.item(1).setCheckState(Qt.CheckState.Unchecked)
    qapp.processEvents()
    assert window.session.node_enables is None, "the hidden layout toggled a step"
    assert three.table.checks[1].isChecked() is True, "the visible layout's box followed the hidden one"

    # Switch back and the same box on the now-hidden three-pane layout is inert too.
    window.staged_layout_action.trigger()
    qapp.processEvents()
    three.table.checks[0].setChecked(False)
    qapp.processEvents()
    assert window.session.node_enables is None, "the hidden three-pane toggled a step"
    assert staged.steps_options_stage.step_list.item(0).checkState() == Qt.CheckState.Checked


def test_a_hidden_staged_layouts_prompt_and_abort_answer_nothing(qapp, tmp_path):
    """While the staged layout is hidden, its prompt bar and Abort reach neither the pause nor the run."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    schedule = _add_pause_and_block_schedule(tmp_path)
    outcome, _ = _launch(env, tmp_path, ui={"layout": "threePane"})
    window = outcome.window
    window.show()
    qapp.processEvents()
    three, staged = window.three_pane_screen, window.staged_screen
    assert three.is_active() and not staged.is_active()

    _pick_and_start(qapp, window, schedule)
    assert window.session.pause_pending(), "the run is paused on the failure"
    assert three.table.banner.isVisible(), "the visible layout shows the pause"

    staged.run_view.failure_prompt_bar.continue_button.click()
    staged.run_view.abort_button.click()
    qapp.processEvents()
    assert window.session.pause_pending(), "the hidden layout answered the pause"
    assert window.session.state == SessionState.STARTED, "the hidden layout ended the run"
    assert all(row.get("prompt_choice") is None for row in window.session.store.attempts(window.session.run_uuid))

    three.table._prompt_bars[0].continue_button.click()
    qapp.processEvents()
    assert _wait(qapp, lambda: not window.session.pause_pending()), "the visible layout did not resume the run"
    assert any(row.get("prompt_choice") == "CONTINUE" for row in window.session.store.attempts(window.session.run_uuid))
    _finish(qapp, window)


def test_a_hidden_three_pane_layouts_prompt_and_abort_answer_nothing(qapp, tmp_path):
    """The mirror case: switch away mid-pause and the three-pane layout, still showing the prompt it
    painted, cannot answer it or abort the run."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    schedule = _add_pause_and_block_schedule(tmp_path)
    outcome, _ = _launch(env, tmp_path, ui={"layout": "threePane"})
    window = outcome.window
    window.show()
    qapp.processEvents()
    three, staged = window.three_pane_screen, window.staged_screen

    _pick_and_start(qapp, window, schedule)
    assert window.session.pause_pending(), "the run is paused on the failure"
    assert three.table._prompt_bars[0].isVisible(), "the visible layout painted its prompt"

    window.staged_layout_action.trigger()
    qapp.processEvents()
    assert three.is_active() is False and staged.is_active()
    # The hidden layout's own prompt signal is inert: it cannot answer the pause.
    three.table.prompt_choice.emit("CONTINUE")
    three.abort_button.click()
    qapp.processEvents()
    assert window.session.pause_pending(), "the hidden layout answered the pause"
    assert window.session.state == SessionState.STARTED, "the hidden layout ended the run"

    staged.run_view.failure_prompt_bar.continue_button.click()
    qapp.processEvents()
    assert _wait(qapp, lambda: not window.session.pause_pending()), "the visible layout did not resume the run"
    assert three._paused_seq is None and three.table.banner.isVisible() is False, "the hidden layout kept the pause"
    _finish(qapp, window)


def test_abort_on_the_visible_layout_answers_a_pending_pause_with_abort(qapp, tmp_path):
    """While the run is paused, the layout on screen aborts the run through the pause, not around it."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    schedule = _add_pause_and_block_schedule(tmp_path)
    outcome, _ = _launch(env, tmp_path)
    window = outcome.window
    window.show()
    qapp.processEvents()
    _pick_and_start(qapp, window, schedule)
    assert window.session.pause_pending()

    window.staged_screen.run_view.abort_button.click()
    qapp.processEvents()
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED), "Abort did not end the run"
    uuid = window.session.run_uuid
    row = window.session.store.get_test_run(uuid)
    # An ABORT at a Stop-on-Fail pause stops the run from that pause (packet 12b), which the store
    # records as End Reason STOPPED_ON_FAIL with the choice stamped on the failed Attempt.
    assert row["end_reason"] == "STOPPED_ON_FAIL", row
    attempts = window.session.store.attempts(uuid)
    assert attempts[0]["prompt_choice"] == "ABORT", attempts
    _finish(qapp, window)

    # The same from the other layout: switch mid-pause and the new layout's Abort answers it.
    outcome2, _ = _launch(env, tmp_path)
    window2 = outcome2.window
    window2.show()
    qapp.processEvents()
    _pick_and_start(qapp, window2, schedule)
    assert window2.session.pause_pending()
    window2.three_pane_layout_action.trigger()
    qapp.processEvents()
    assert window2.three_pane_screen.table.banner.isVisible(), "the pause did not follow the switch"
    window2.three_pane_screen.abort_button.click()
    qapp.processEvents()
    assert _wait(qapp, lambda: window2.session.state == SessionState.FINISHED), "Abort did not end the run"
    uuid2 = window2.session.run_uuid
    assert window2.session.store.get_test_run(uuid2)["end_reason"] == "STOPPED_ON_FAIL"
    assert window2.session.store.attempts(uuid2)[0]["prompt_choice"] == "ABORT"
    _finish(qapp, window2)


def test_a_hidden_layout_defers_its_store_reads_until_it_is_showing(qapp, tmp_path):
    """The layout that is not showing reads the store once, when it comes to the front."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    schedule = _add_pause_and_block_schedule(tmp_path)
    outcome, _ = _launch(env, tmp_path)
    window = outcome.window
    window.show()
    qapp.processEvents()
    three = window.three_pane_screen
    syncs: list[int] = []
    original_sync = three._sync_from_store
    three._sync_from_store = lambda: (syncs.append(1), original_sync())

    asked, prompts = _pick_and_start(qapp, window, schedule)
    assert not syncs, f"the hidden three-pane synced {len(syncs)} times while hidden"
    assert not three.log_pane.rendered_lines(), "the hidden pane read a log nobody was looking at"
    assert three._store_dirty is True, "the hidden layout did not remember what it skipped"
    window.staged_screen.run_view.failure_prompt_bar.continue_button.click()
    qapp.processEvents()
    assert _wait(qapp, lambda: any(a[2] == 1 for a in asked)), "step 2 never asked for its input"
    answer = window.staged_screen.input_widget(1, 1)
    answer.line_edit.setText("7")
    answer.ok_button.click()
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED)
    qapp.processEvents()
    assert not syncs, f"the hidden three-pane synced {len(syncs)} times while hidden"
    assert three._store_dirty is True, "the hidden layout did not remember what it skipped"

    window.three_pane_layout_action.trigger()
    qapp.processEvents()
    assert len(syncs) == 1, f"switching synced {len(syncs)} times, expected one deferred sync"
    assert three._store_dirty is False
    assert _three_step_results(three) == {0: ("FAIL", "1"), 1: ("PASS", "1")}
    assert any("the first step is failing" in line for line in three.log_pane.rendered_lines())
    _finish(qapp, window)


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


def test_a_retried_step_shows_its_attempts_on_both_layouts(qapp, tmp_path):
    """A step retried after a pause shows its attempt count in the staged list and the centre table."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    outcome, _ = _launch(env, tmp_path)
    window = outcome.window
    window.show()
    qapp.processEvents()
    staged, three = window.staged_screen, window.three_pane_screen

    _pick_and_start(qapp, window, "Passes on retry schedule")
    staged.run_view.failure_prompt_bar.retry_button.click()
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED)
    qapp.processEvents()

    rows = _staged_step_texts(staged)
    assert any("2 Attempts" in row for row in rows), rows
    assert any("PASS" in row for row in rows), rows

    window.three_pane_layout_action.trigger()
    qapp.processEvents()
    assert _three_step_results(three).get(0) == ("PASS", "2"), _three_step_results(three)

    window.staged_layout_action.trigger()
    qapp.processEvents()
    assert any("2 Attempts" in row for row in _staged_step_texts(staged)), "the count vanished on the way back"
    _finish(qapp, window)


def test_the_two_review_shots_are_saved(qapp, tmp_path):
    """The two states the reviewer asked to see, at 1280x800.

    ``three-pane-after-finished-run.png``: a run finished in the staged layout, then switched to the
    three-pane layout — the results, the log and the counts are still there.
    ``staged-after-pause-answered-in-three-pane.png``: a Stop-on-Fail pause answered in the three-pane
    layout, then switched back to staged — no prompt bar, no "Paused", nothing stale."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    schedule = _add_pause_and_block_schedule(tmp_path)

    # Shot 1: finish in staged, switch to three-pane.
    outcome, _ = _launch(env, tmp_path)
    window = outcome.window
    window.show()
    qapp.processEvents()
    asked, prompts = _pick_and_start(qapp, window, schedule)
    window.staged_screen.run_view.failure_prompt_bar.continue_button.click()
    qapp.processEvents()
    assert _wait(qapp, lambda: any(a[2] == 1 for a in asked))
    answer = window.staged_screen.input_widget(1, 1)
    answer.line_edit.setText("7")
    answer.ok_button.click()
    assert _wait(qapp, lambda: window.session.state == SessionState.FINISHED)
    qapp.processEvents()
    window.three_pane_layout_action.trigger()
    qapp.processEvents()
    three = window.three_pane_screen
    assert _three_step_results(three) == {0: ("FAIL", "1"), 1: ("PASS", "1")}
    assert three.log_pane.rendered_lines() and three.debug_window.rendered_lines()
    shot = _window_shot(window)
    assert shot.save(str(SHOTS / "three-pane-after-finished-run.png")), "shot 1 did not save"
    _finish(qapp, window)

    # Shot 2: answer the pause in three-pane, switch back to staged.
    outcome2, _ = _launch(env, tmp_path, ui={"layout": "threePane"})
    window2 = outcome2.window
    window2.show()
    qapp.processEvents()
    _pick_and_start(qapp, window2, schedule)
    window2.three_pane_screen.table._prompt_bars[0].continue_button.click()
    qapp.processEvents()
    assert _wait(qapp, lambda: not window2.session.pause_pending())
    window2.staged_layout_action.trigger()
    qapp.processEvents()
    staged2 = window2.staged_screen
    assert staged2.run_view.failure_prompt_bar.isVisible() is False
    assert staged2.run_view.status_bar.status_label.text() != "Paused"
    shot2 = _window_shot(window2)
    assert shot2.save(str(SHOTS / "staged-after-pause-answered-in-three-pane.png")), "shot 2 did not save"
    _finish(qapp, window2)


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
