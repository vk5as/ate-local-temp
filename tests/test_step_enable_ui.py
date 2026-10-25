"""Packet 11b seam test: the Steps & options stage checkboxes drive step enables.

The widget holds no logic of its own: each checkbox toggle is one call into
``session.toggle_node(seq, enabled)`` (packet 11a), applied at start(). Toggles are
locked while a run is in progress and stay locked until it ends.
"""

from PyQt6.QtCore import Qt

from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.ui.staged_run_screen import StagedRunScreen

from ._fixtures import write_ate_env


def _drain_report(qapp, session) -> None:
    """Pump events until the run-end PDF helper reports back (ticket 22b).

    A finished run with a config starts a helper process in a session-owned QProcess; if
    the test drops the session while it is still running, the later signal delivery reaches
    a dead object and aborts the interpreter. Draining ``report_written`` leaves the helper
    fully done before the test ends (the pattern test_failure_bar uses)."""
    done = []
    session.report_written.connect(lambda _html, _pdf: done.append(True))
    for _ in range(3000):
        qapp.processEvents()
        if done:
            return
    raise AssertionError("report_written never fired")


def _wait_finished(qapp, session) -> bool:
    """Poll processEvents until the session reaches FINISHED (or times out).

    A Stop-on-Fail pause blocks the worker until an operator answers a prompt; answer
    Continue here while pumping, mirroring the headless drivers in test_staged_run_screen."""
    def _continue_on_failure(_node_seq: int, _attempt_no: int) -> None:
        session.choose("CONTINUE")

    session.failure_prompt.connect(_continue_on_failure)
    for _ in range(3000):
        qapp.processEvents()
        if session._state == SessionState.FINISHED:
            return True
    return False


def _find_item(list_widget, substring: str):
    """Return the row whose text contains ``substring`` (raising if none match)."""
    for i in range(list_widget.count()):
        if substring in list_widget.item(i).text():
            return list_widget.item(i)
    raise AssertionError(f"no schedule row containing {substring!r}")


def _select_and_load(screen, schedule_name: str) -> None:
    """Select ``schedule_name`` and load its steps into the Steps & options stage."""
    item = _find_item(screen.test_schedule_stage.schedule_list, schedule_name)
    screen.test_schedule_stage.schedule_list.setCurrentItem(item)
    screen._begin_run_from_selection()
    assert screen.steps_stack.currentIndex() == 2


def test_untick_default_on_step_records_disabled_notrun(qapp, tmp_path) -> None:
    """Unticking a default-on step in stage 3 flips its recorded ``enabled`` to false.

    "Enable/disable schedule" has a=0 (on), b=1 (:setEnable(false)), c=2 (on). Unticking row 0
    (step a, default on) reaches the session as ``toggle_node(0, False)``; the run then
    records a NOTRUN with (default_enabled, enabled) = (1, 0) and no Attempt, b stays NOTRUN,
    and c runs."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)

    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    _select_and_load(screen, "Enable/disable schedule")

    first = screen.steps_options_stage.step_list.item(0)
    assert first.checkState() == Qt.CheckState.Checked  # a defaults on

    # The checkbox reflects the step's default enabled state: row 1 is off by default.
    assert screen.steps_options_stage.step_list.item(1).checkState() == Qt.CheckState.Unchecked

    # A toggle is one call into the session — no logic in the widget.
    first.setCheckState(Qt.CheckState.Unchecked)
    assert session._pending_node_enables == {0: False}

    screen.steps_options_stage.run_button.click()
    assert _wait_finished(qapp, session)
    _drain_report(qapp, session)

    nodes = {n["step_number"]: n for n in session.store.nodes(session.run_uuid)}
    assert nodes["1."]["result"] == "NOTRUN"
    assert nodes["1."]["default_enabled"] == 1
    assert nodes["1."]["enabled"] == 0
    assert nodes["2."]["result"] == "NOTRUN"
    assert nodes["3."]["result"] == "PASS"
    # The disabled step records no Attempt row at all.
    assert not any(a["node_seq"] == 0 for a in session.store.attempts(session.run_uuid))


def test_boxes_stay_enabled_and_selectable_with_check_state(qapp, tmp_path) -> None:
    """A row keeps ItemIsEnabled and ItemIsSelectable even when locked; only
    ItemIsUserCheckable toggles with the run lock, and the box keeps showing its
    check state (packet 11b fix: dropping ItemIsEnabled disabled the row in setup)."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)

    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    _select_and_load(screen, "Enable/disable schedule")

    first = screen.steps_options_stage.step_list.item(0)
    # In setup the row is enabled, selectable and user-checkable; the boxes show the defaults.
    assert first.flags() & Qt.ItemFlag.ItemIsEnabled
    assert first.flags() & Qt.ItemFlag.ItemIsSelectable
    assert first.flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert first.checkState() == Qt.CheckState.Checked
    second = screen.steps_options_stage.step_list.item(1)
    assert second.checkState() == Qt.CheckState.Unchecked

    screen.steps_options_stage.run_button.click()
    qapp.processEvents()  # deliver the STARTED signal to the screen
    assert session._state == SessionState.STARTED

    # The lock removes only ItemIsUserCheckable: the box still shows its check state
    # and the row stays enabled and selectable.
    assert not (first.flags() & Qt.ItemFlag.ItemIsUserCheckable)
    assert first.checkState() == Qt.CheckState.Checked
    assert first.flags() & Qt.ItemFlag.ItemIsEnabled
    assert first.flags() & Qt.ItemFlag.ItemIsSelectable
    assert second.checkState() == Qt.CheckState.Unchecked

    # Let the run finish and drain its PDF helper before the test drops the session.
    assert _wait_finished(qapp, session)
    _drain_report(qapp, session)


def test_boxes_lock_while_a_run_is_up_and_unlock_after_reselect(qapp, tmp_path) -> None:
    """The step checkboxes are user-checkable in setup and lose the flag once the run starts.

    While the run is in progress the boxes are not user-checkable (the check flag is gone)
    and a toggle attempt is rejected: it changes nothing the run will record. The lock
    holds until the run ends, but not past it: re-selecting the schedule returns the session
    to SELECTING, so the boxes are checkable again and accept the next run's toggles (spec
    37: the schedule and options carry over into the next run)."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)

    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    _select_and_load(screen, "Enable/disable schedule")

    # Checkable before start.
    first = screen.steps_options_stage.step_list.item(0)
    assert first.flags() & Qt.ItemFlag.ItemIsUserCheckable

    screen.steps_options_stage.run_button.click()
    qapp.processEvents()  # deliver the STARTED signal to the screen
    assert session._state == SessionState.STARTED

    # Not user-checkable while the run is in progress.
    assert not (first.flags() & Qt.ItemFlag.ItemIsUserCheckable)

    # A toggle attempt while the run is up is rejected — the box state changes but the
    # override is never stored.
    first.setCheckState(Qt.CheckState.Unchecked)
    assert session._pending_node_enables is None

    # The run finishes with the box still locked...
    assert _wait_finished(qapp, session)
    assert not (first.flags() & Qt.ItemFlag.ItemIsUserCheckable)
    _drain_report(qapp, session)

    # The rejected toggle changed nothing: step a ran with its default enabled=1.
    nodes = {n["step_number"]: n for n in session.store.nodes(session.run_uuid)}
    assert nodes["1."]["enabled"] == 1
    assert nodes["1."]["result"] == "PASS"

    # Re-selecting the same schedule returns the session to SELECTING...
    item = _find_item(screen.test_schedule_stage.schedule_list, "Enable/disable schedule")
    screen.test_schedule_stage.schedule_list.selectionModel().clearSelection()
    screen.test_schedule_stage.schedule_list.setCurrentItem(item)
    qapp.processEvents()
    assert session._state == SessionState.SELECTING
    # ...and the boxes are checkable again.
    assert first.flags() & Qt.ItemFlag.ItemIsUserCheckable

    # And toggle_node accepts again.
    session.toggle_node(0, False)
    assert session._pending_node_enables == {0: False}


def test_same_schedule_reselction_carries_overrides_and_seeds_boxes(qapp, tmp_path) -> None:
    """Re-selecting the same schedule carries its step overrides, and the boxes show them.

    Unticking row 0 of "Enable/disable schedule" stores {0: False}; running it then
    re-selecting the same schedule keeps that override on the session (spec 37: options
    carry over) and the Steps stage seeds its boxes from it, so row 0 shows unchecked
    while the untouched row 2 keeps its default-checked state."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)

    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    _select_and_load(screen, "Enable/disable schedule")

    first = screen.steps_options_stage.step_list.item(0)
    first.setCheckState(Qt.CheckState.Unchecked)
    assert session._pending_node_enables == {0: False}

    screen.steps_options_stage.run_button.click()
    assert _wait_finished(qapp, session)
    _drain_report(qapp, session)

    # Re-select the same schedule; the override must survive.
    item = _find_item(screen.test_schedule_stage.schedule_list, "Enable/disable schedule")
    screen.test_schedule_stage.schedule_list.selectionModel().clearSelection()
    screen.test_schedule_stage.schedule_list.setCurrentItem(item)
    qapp.processEvents()
    assert session._state == SessionState.SELECTING
    assert session._pending_node_enables == {0: False}

    # The Steps stage seeds its boxes from the carried override: row 0 unchecked,
    # row 2 (never touched) still at its default-on state.
    screen._begin_run_from_selection()
    assert screen.steps_options_stage.step_list.item(0).checkState() == Qt.CheckState.Unchecked
    assert screen.steps_options_stage.step_list.item(2).checkState() == Qt.CheckState.Checked


def test_different_schedule_selection_clears_overrides_and_uses_defaults(qapp, tmp_path) -> None:
    """Switching to another schedule clears the previous schedule's step overrides.

    Unticking row 0 of "Enable/disable schedule" stores {0: False}; switching to
    "Passing schedule" clears that so a next run records the new schedule's own defaults
    (a single enabled step, checked box) rather than the stale override."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)

    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    _select_and_load(screen, "Enable/disable schedule")

    first = screen.steps_options_stage.step_list.item(0)
    first.setCheckState(Qt.CheckState.Unchecked)
    assert session._pending_node_enables == {0: False}

    screen.steps_options_stage.run_button.click()
    assert _wait_finished(qapp, session)
    _drain_report(qapp, session)

    # Switching schedules clears the previous schedule's overrides.
    other = _find_item(screen.test_schedule_stage.schedule_list, "Passing schedule")
    screen.test_schedule_stage.schedule_list.selectionModel().clearSelection()
    screen.test_schedule_stage.schedule_list.setCurrentItem(other)
    qapp.processEvents()
    assert session._state == SessionState.SELECTING
    assert session.selected_schedule == "Passing schedule"
    assert session._pending_node_enables is None

    # The Steps stage seeds its boxes from the new schedule's defaults.
    screen._begin_run_from_selection()
    assert screen.steps_options_stage.step_list.item(0).checkState() == Qt.CheckState.Checked
