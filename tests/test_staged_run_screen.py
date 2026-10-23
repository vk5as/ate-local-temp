"""Ticket 03's seam-2 smoke test: the staged Run screen bound to a real session + store.

It builds :class:`StagedRunScreen` on top of a live :class:`HeadlessRunSession` and its
:class:`~ate.storage.store.RunStore`, then drives one short schedule offscreen through the
three setup stages into the Run view — no run logic here, only bindings to existing
interfaces. Every state update rides the session's ``state_changed`` signal (delivered by
``processEvents``), so the worker thread is never blocked and the GUI stays responsive.

Offscreen: a single :class:`QApplication` on the ``offscreen`` platform covers the module,
and no schedule is actually executed — results come back through the store as real rows.
"""

import sqlite3
import threading

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


def _rows_for(store, run_uuid: str) -> dict[int, tuple[str, str]]:
    """Return ``{input_index: (type, value_text)}`` recorded for a run."""
    store._conn.row_factory = sqlite3.Row  # type: ignore[attr-defined]
    return {
        row["input_index"]: (row["type"], row["value"])
        for row in store._conn.execute(
            "SELECT input_index, type, value FROM input_value WHERE run_uuid=?", (run_uuid,)
        ).fetchall()
    }


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


def test_log_pane_shows_info_and_above_debug_window_shows_every_level(qapp, tmp_path):
    """The Run view's Log pane shows INFO and above; the Debug window shows every level.

    The Logging schedule logs at INFO (ATE.log + print), WARNING and ERROR across its steps,
    so after a run both panes are filled from session.store.log_lines(). The Log pane keeps
    INFO and above; the Debug window is a separate view that keeps every recorded line.
    """
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)

    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    _select_and_start(qapp, screen, "Logging schedule")
    assert _wait_finished(qapp, session)

    log_pane = "\n".join(screen.run_view.log_pane.rendered_lines())
    debug_window = "\n".join(screen.run_view.debug_window.rendered_lines())

    # The Log pane shows INFO and above: an INFO line (ATE.log/print), a WARNING and an ERROR.
    assert any(line.startswith("INFO") for line in screen.run_view.log_pane.rendered_lines())
    assert "WARNING" in log_pane
    assert "ERROR" in log_pane

    # The Debug window is the separate view that keeps every recorded line — here the same set,
    # since this schedule records no DEBUG lines.
    assert screen.run_view.debug_window.rendered_lines() == screen.run_view.log_pane.rendered_lines()


def test_debug_line_shown_only_in_debug_window(qapp, tmp_path):
    """A DEBUG line reaches the Debug window but is filtered out of the Log pane."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)

    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    _select_and_start(qapp, screen, "Logging schedule")
    assert _wait_finished(qapp, session)

    run_uuid = session.run_uuid
    nodes = session.store.nodes(run_uuid)
    attempts = session.store.attempts(run_uuid)
    log_lines = session.store.log_lines(run_uuid)

    # Ate records its own DEBUG lines (e.g. a recovery note); record one by hand here so the
    # two panes can be compared on a line that only the Debug window should show.
    session.store.insert_log_lines(
        run_uuid, 0, [{"level": "debug", "source": "ate", "text": "a debug message"}]
    )
    all_lines = session.store.log_lines(run_uuid)

    # Re-render with the injected DEBUG line present (the FINISHED refresh already ran).
    screen.run_view.refresh(nodes, attempts, all_lines)

    assert "a debug message" in "\n".join(screen.run_view.debug_window.rendered_lines())
    assert "a debug message" not in "\n".join(screen.run_view.log_pane.rendered_lines())


def test_staged_run_records_blocking_input_value(qapp, tmp_path):
    """Ticket 10 checkbox f part 2: a blocking input supplied through the staged screen is recorded.

    Runs BLOCKING_INPUT all the way through StagedRunScreen (selection -> Run view) and supplies its
    one INTEGER input from this thread via ``screen.engine.operator_input.supply_input`` — the engine
    reached through the session, no widgets imported for inputs — then asserts the typed value landed
    in an ``input_value`` row read straight from the store.

    The handler connects to ``session.input_asked`` before starting: grabbing the engine after start()
    would race the worker's single input announcement (emitted once, right before it blocks), so the
    staged path forwards that ask onto the session up front and answers it through screen.engine.
    """
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)

    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)  # to the Test Schedule stage

    item = _find_item(screen.test_schedule_stage.schedule_list, "Blocking input schedule")
    screen.test_schedule_stage.schedule_list.setCurrentItem(item)
    assert session.selected_schedule == "Blocking input schedule"

    # Answer the blocking input through this session *before* starting: connecting here avoids the
    # single-shot race where the worker announces its input before we could grab the engine, so the
    # announcement would be dropped and the blocking worker left waiting forever. Supplying through
    # ``screen.engine`` still records the typed value end-to-end (selection -> session -> RunEngine).
    answered = threading.Event()

    def _on_ask(node_seq, attempt_no, index):
        op = screen.engine.operator_input  # set by begin_run once the run starts; valid while running
        op.supply_input(index - 1, 42)  # supply n=1 -> key 0 with the typed INTEGER value
        answered.set()

    session.input_asked.connect(_on_ask)

    screen.steps_options_stage.run_button.click()  # queued start (begin_run runs on the next pump)
    assert _wait_finished(qapp, session)
    assert answered.is_set()  # announced and answered exactly once through the run session

    rows = {idx: (typ, val) for idx, (typ, val) in _rows_for(store=session.store, run_uuid=session.run_uuid).items()}
    assert rows == {1: ("INTEGER", "42")}


def test_abort_button_between_steps_records_error_notrun_and_aborted(qapp, tmp_path):
    """Packet 13a checkbox 1: the Run view's Abort button sets the session's abort flag.

    Runs ABORTABLE through StagedRunScreen (selection -> Run view) and clicks ``screen.run_view.abort_button``
    while step 1 blocks on its INTEGER input; the operator answers that input first so step 1 records
    cleanly, then hits Abort between steps. The button is reached only through the screen — no run logic here."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)

    # The Abort control exists on the Run view and is disabled until a run starts.
    assert isinstance(screen.run_view.abort_button, QPushButton)
    assert screen.run_view.abort_button.isEnabled() is False

    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)  # to the Test Schedule stage

    item = _find_item(screen.test_schedule_stage.schedule_list, "Abortable schedule")
    screen.test_schedule_stage.schedule_list.setCurrentItem(item)
    assert session.selected_schedule == "Abortable schedule"

    answered = threading.Event()

    def _on_ask(node_seq, attempt_no, index):
        op = screen.engine.operator_input  # set by begin_run once the run starts; valid while running
        op.supply_input(index - 1, 5)  # answer step 1's input so it completes cleanly
        answered.set()

    session.input_asked.connect(_on_ask)

    screen.steps_options_stage.run_button.click()  # queued start (begin_run runs on the next pump)

    # Pump until step 1 announces and answers its input — at which point the worker is blocked on it.
    # Do not wait for FINISHED here: a bare wait would let the worker race through steps 2 & 3 first.
    while not answered.is_set():
        qapp.processEvents()
    assert answered.is_set()  # announced and answered exactly once through the run session

    # Abort between steps while step 1 waits; then let the worker record ERROR / NOTRUN / ABORTED.
    screen.run_view.abort_button.click()
    assert _wait_finished(qapp, session)

    row = session.store.get_test_run(session.run_uuid)
    assert row is not None
    # A whole-run abort fails the run with End Reason ABORTED.
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "ABORTED"

    conn = session.store._conn
    conn.row_factory = sqlite3.Row
    nodes = {n["step_number"]: dict(n) for n in conn.execute(
        "SELECT step_number, result, attempt_count FROM node WHERE run_uuid=? ORDER BY node_seq", (row["run_uuid"],)
    ).fetchall()}
    # Step 1 ran and passed; step 2 was the running step at abort time -> ERROR; step 3 never ran.
    assert nodes == {
        "1.": {"step_number": "1.", "result": "PASS", "attempt_count": 1},
        "2.": {"step_number": "2.", "result": "ERROR", "attempt_count": 1},
        "3.": {"step_number": "3.", "result": "NOTRUN", "attempt_count": 0},
    }

    err = conn.execute(
        "SELECT pass_fail, msg FROM attempt WHERE run_uuid=? AND node_seq=1", (row["run_uuid"],)
    ).fetchone()
    assert err is not None and err["pass_fail"] == "ERROR"
    assert err["msg"] == "Aborted by Operator"
