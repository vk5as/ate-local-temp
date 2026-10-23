"""Ticket 03's seam-2 smoke test: the staged Run screen bound to a real session + store.

It builds :class:`StagedRunScreen` on top of a live :class:`HeadlessRunSession` and its
:class:`~ate.storage.store.RunStore`, then drives one short schedule offscreen through the
three setup stages into the Run view — no run logic here, only bindings to existing
interfaces. Every state update rides the session's ``state_changed`` signal (delivered by
``processEvents``), so the worker thread is never blocked and the GUI stays responsive.

Offscreen: a single :class:`QApplication` on the ``offscreen`` platform covers the module,
and no schedule is actually executed — results come back through the store as real rows.
"""

import pytest

from PyQt6.QtWidgets import (
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QTabWidget,
    QWidget,
)

from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.ui.staged_run_screen import StagedRunScreen

from ._fixtures import write_ate_env


def _wait_finished(qapp, session):
    """Poll processEvents until the session reaches FINISHED (or times out)."""
    for _ in range(3000):
        qapp.processEvents()
        if session._state == SessionState.FINISHED:
            return True
    return False


def _select_and_start(qapp, screen, schedule_name):
    """Select ``schedule_name`` and start a run through the Steps & options stage."""
    item = _find_item(screen.test_schedule_stage.schedule_list, schedule_name)
    screen.test_schedule_stage.schedule_list.setCurrentItem(item)
    # Preview the selected schedule's steps, then move to the final stage.
    screen._begin_run_from_selection()
    assert screen.steps_stack.currentIndex() == 2
    # Begin the run: capture fields and start through session.start().
    screen.steps_options_stage.run_button.click()


def _find_item(list_widget, substring):
    """Return the row whose text contains ``substring`` (raising if none match)."""
    for i in range(list_widget.count()):
        if substring in list_widget.item(i).text():
            return list_widget.item(i)
    raise AssertionError(f"no schedule row containing {substring!r}")


def test_stages_render_with_required_fields(qapp, tmp_path):
    """Every stage exposes the widgets it must, stacked in order with a Run view below."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)

    assert screen.session is session

    # Stage 0: UUT & Operator text fields.
    for attr in ("operator_edit", "project_edit", "uut_type_edit", "serial_edit"):
        widget = getattr(screen.uut_operator_stage, attr)
        assert isinstance(widget, QLineEdit)

    # Stage 1: schedule list + rescan button + notes label.
    assert isinstance(screen.test_schedule_stage.schedule_list, QListWidget)
    assert isinstance(screen.test_schedule_stage.rescan_button, QPushButton)
    assert isinstance(screen.test_schedule_stage.notes_label, QLabel)

    # The three setup stages live in a QStackedWidget, in order.
    assert isinstance(screen.steps_stack, QStackedWidget)
    assert [type(stage).__name__ for stage in (screen.steps_stack.widget(i) for i in range(3))] == [
        "UutOperatorStage",
        "TestScheduleStage",
        "StepsOptionsStage",
    ]

    # Run view: state header, live step list, full-width failure bar, Log/Summary tabs.
    assert isinstance(screen.run_view.state_header, QLabel)
    assert isinstance(screen.run_view.step_list, QListWidget)
    assert isinstance(screen.run_view.failure_bar, QLabel)
    assert isinstance(screen.run_view.tabs, QTabWidget)
    tab_labels = [screen.run_view.tabs.tabText(i) for i in range(screen.run_view.tabs.count())]
    assert "Log" in tab_labels and "Summary" in tab_labels


def test_project_prefilled_from_config_station_name(qapp, tmp_path):
    """The Project field is prefilled from the station name in config (BENCH-03)."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    assert screen.uut_operator_stage.project_edit.text() == "BENCH-03"


def test_selecting_schedule_and_running_reaches_started_then_finished(qapp, tmp_path):
    """Selecting a schedule and running it drives IDLE -> SELECTING -> STARTED -> FINISHED."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)

    states: list[SessionState] = []
    session.state_changed.connect(states.append)

    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)  # to the Test Schedule stage

    item = _find_item(screen.test_schedule_stage.schedule_list, "Failing schedule")
    screen.test_schedule_stage.schedule_list.setCurrentItem(item)
    assert session.selected_schedule == "Failing schedule"
    # The notes label mirrors the selection.
    assert "Failing schedule" in screen.test_schedule_stage.notes_label.text()

    _select_and_start(qapp, screen, "Failing schedule")
    assert _wait_finished(qapp, session)

    row = session.store.get_test_run(session.run_uuid)
    assert row is not None and row["result"] == "FAIL"
    assert SessionState("SELECTING") in states
    assert SessionState("STARTED") in states
    assert SessionState("FINISHED") in states


def test_steps_show_live_result_after_run(qapp, tmp_path):
    """After a run, each step shows its live Result in the Run view's step list."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)

    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    _select_and_start(qapp, screen, "Passing schedule")
    assert _wait_finished(qapp, session)

    texts = [screen.run_view.step_list.item(i).text() for i in range(screen.run_view.step_list.count())]
    assert any("PASS" in text for text in texts)
    # The Run view's failure bar is clear on a passing run.
    assert screen.run_view.failure_bar.text() == ""


def test_current_step_is_enlarged(qapp, tmp_path):
    """The latest step is shown enlarged so the eye lands on where the run is."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)

    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    _select_and_start(qapp, screen, "Erroring schedule")  # two recorded steps
    assert _wait_finished(qapp, session)

    step_list = screen.run_view.step_list
    first = step_list.item(0)
    last = step_list.item(step_list.count() - 1)
    assert last.font().pointSize() > first.font().pointSize()


def test_failing_step_surfaces_in_failure_bar_and_run_view(qapp, tmp_path):
    """A failing run surfaces the failure in the full-width bar and per-step list."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)

    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    _select_and_start(qapp, screen, "Failing schedule")
    assert _wait_finished(qapp, session)  # refresh happens here via the state signal

    failure_bar = screen.run_view.failure_bar.text()
    assert "FAIL" in failure_bar or "ERROR" in failure_bar
    assert "LED colour" in failure_bar  # the failing step is named

    texts = [screen.run_view.step_list.item(i).text() for i in range(screen.run_view.step_list.count())]
    assert any("FAIL" in text for text in texts)


def test_run_view_is_hidden_until_a_run_starts(qapp, tmp_path):
    """The Run view is hidden before a run and shown once one starts."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    assert screen.run_view.isVisible() is False

    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    _select_and_start(qapp, screen, "Passing schedule")
    assert _wait_finished(qapp, session)
    assert screen.run_view.isVisible() is True


def test_new_run_keeps_previously_entered_fields(qapp, tmp_path):
    """A new run clears nothing: fields carry over into the next recorded run."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)

    # Run 1 with Operator and Project entered.
    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen.uut_operator_stage.project_edit.setText("Power board")
    screen._advance(1)
    _select_and_start(qapp, screen, "Passing schedule")
    assert _wait_finished(qapp, session)

    row1 = session.store.get_test_run(session.run_uuid)
    assert row1 is not None and row1["operator"] == "Ada" and row1["project"] == "Power board"
    # The run itself never clears the widgets.
    assert screen.uut_operator_stage.operator_edit.text() == "Ada"
    assert screen.uut_operator_stage.project_edit.text() == "Power board"

    # Blank the Operator, then start a new run — its value must still carry over (nothing cleared).
    screen._advance(1)  # back to stage 0 after FINISHED
    screen.uut_operator_stage.operator_edit.setText("")
    _select_and_start(qapp, screen, "Failing schedule")
    assert _wait_finished(qapp, session)

    row2 = session.store.get_test_run(session.run_uuid)
    assert row2 is not None and row2["operator"] == "Ada"  # carried over, not dropped
    assert row2["project"] == "Power board"


def test_stop_on_fail_is_prefilled_true(qapp, tmp_path):
    """The Steps & options stage prefills the stop-on-fail radio as checked (True)."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    assert screen.steps_options_stage.stop_on_fail_radio.isChecked() is True
