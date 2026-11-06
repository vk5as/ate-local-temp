"""Packet 41 — Start Run is available only when a run can actually start.

Both layouts enable the Start control only while all three conditions hold: every required
field (Project, Operator, UUT Type, Serial) has something in it after `strip()`, the Test
Schedule the picker shows is a runnable one, and no run is in progress. The same predicate
guards the start path itself, so a shortcut, a re-enabled button or a direct call to
`begin_run` starts nothing when starting is not valid. In the staged layout the fields stage
cannot be left — by Next, by Enter, or by a programmatic page jump — until the four fields
are filled; Back is always allowed.

Everything here is driven offscreen against a real :class:`HeadlessRunSession`: no mocks, and
the runs that do start are real schedules from the fixture Test Definition.
"""

import json
import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from ate.app import launch
from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.storage.store import RunStore
from ate.ui.run_widgets import BROKEN_MARKER
from ate.ui.staged_run_screen import StagedRunScreen
from ate.ui.theme import STYLESHEET, TOKENS
from ate.ui.three_pane_run_screen import ThreePaneRunScreen

from ._fixtures import PASSING, fill_operator_fields, write_ate_env


def _staged(qapp, tmp_path) -> StagedRunScreen:
    """A staged layout on a real session, with the picker populated."""
    env = write_ate_env(tmp_path)
    screen = StagedRunScreen(HeadlessRunSession(env["config"]))
    qapp.processEvents()
    return screen


def _three_pane(qapp, tmp_path) -> ThreePaneRunScreen:
    """The three-pane layout, built the same way."""
    env = write_ate_env(tmp_path)
    screen = ThreePaneRunScreen(HeadlessRunSession(env["config"]))
    qapp.processEvents()
    return screen


#: Both layouts, so every condition below is checked in each of them.
LAYOUTS = ("staged", "three-pane")

#: The four fields a Test Run cannot start without, as the form's `<name>_edit` attributes name them.
FIELDS = ("project", "operator", "uut_type", "serial")
#: What the error line calls each of them.
FIELD_LABELS = {"project": "Project", "operator": "Operator", "uut_type": "UUT Type", "serial": "Serial"}


def _build(qapp, tmp_path, layout):
    return _staged(qapp, tmp_path) if layout == "staged" else _three_pane(qapp, tmp_path)


def _pick(screen, name: str = "Passing schedule") -> None:
    """Select a runnable schedule through the picker, as an Operator would.

    The row is found by the exact name its user data carries, not by its text: the three-pane
    picker draws its usable rows as widgets, which leaves the item text empty (packet 25a)."""
    rows = screen.picker.schedule_list
    item = next(rows.item(i) for i in range(rows.count()) if rows.item(i).data(Qt.ItemDataRole.UserRole) == name)
    rows.setCurrentItem(item)


def _start_controls(screen) -> list:
    """Every control on this layout that starts a run.

    The staged layout has two: the schedule stage's "Run this schedule" and the steps stage's
    "Start run". The three-pane layout has one Start button."""
    if isinstance(screen, StagedRunScreen):
        return [screen.test_schedule_stage.run_button, screen.steps_options_stage.run_button]
    return [screen.start_button]


def _primary_buttons(screen) -> list:
    """Every button the theme's `#primary` rule is meant to fade: the Start controls and New run.

    Packet 41 review: the staged layout's New run button carries the `primary` object name, the
    three-pane one did not, so a disabled New run faded like an ordinary button in one layout and
    like the run control in the other."""
    return _start_controls(screen) + [screen.new_run_button if isinstance(screen, ThreePaneRunScreen)
                                      else screen.run_view.new_run_button]


def _view_state(screen) -> tuple:
    """What the layout is showing, so a test can prove a refused start changed nothing.

    The staged layout shows either the setup stages or the live Run view; the three-pane layout has
    one pane whose content is the setup step list or the finished run's results."""
    if isinstance(screen, StagedRunScreen):
        return (screen.steps_stack.currentIndex(), screen.steps_stack.isHidden(), screen.run_view.isHidden(),
                screen.run_view.abort_button.isHidden())
    return (screen.table.isHidden(), screen.counts.isHidden())


def _wait_finished(qapp, session, timeout=10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if session.state == SessionState.FINISHED:
            return True
        time.sleep(0.005)
    return False


def _wait_for_run_uuid(qapp, session, timeout=10.0) -> str:
    """The run uuid, once the store has opened the run: `start()` queues it, so it is not there yet."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if session.run_uuid:
            return session.run_uuid
        time.sleep(0.005)
    raise AssertionError("the run never got a uuid")


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("field", FIELDS)
def test_each_required_field_holds_start_back_on_its_own(qapp, tmp_path, layout, field):
    """One blank field is enough to keep Start off, whichever one it is.

    Packet 41 review: the original test filled the fields one after another, so a `can_start()`
    that ignored UUT Type still passed — Project, Operator and Serial were enough to hold Start
    down while UUT Type was empty, and the later fills re-enabled it. Each field is now blank on
    its own, with the other three filled, in both layouts."""
    screen = _build(qapp, tmp_path, layout)
    _pick(screen)
    fill_operator_fields(screen)
    qapp.processEvents()
    if field == "project":
        assert screen.fields.project_edit.text().strip(), "the fixture's defaultProject should prefill Project"

    label = FIELD_LABELS[field]
    edit = getattr(screen.fields, f"{field}_edit")
    for value in ("", "   "):
        edit.setText(value)
        qapp.processEvents()
        assert screen.fields.blank_fields() == [label], f"{label} = {value!r} is not read as blank"
        assert not screen.can_start(), f"{label} blank but can_start() says a run may start ({layout})"
        for button in _start_controls(screen):
            assert not button.isEnabled(), f"{label} blank but {button.text()} is enabled ({layout})"
        if layout == "staged":
            stage = screen.uut_operator_stage
            assert not stage.next_button.isEnabled(), f"Next is live with {label} blank"
            assert label in stage.next_hint_label.text(), stage.next_hint_label.text()

    edit.setText("PS-9")
    qapp.processEvents()
    assert screen.can_start(), f"{label} filled but can_start() still says no ({layout})"
    for button in _start_controls(screen):
        assert button.isEnabled(), f"Start stayed disabled with every field filled ({layout})"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_field_of_only_spaces_counts_as_blank(qapp, tmp_path, layout):
    """An Operator who types spaces has still not filled the field."""
    screen = _build(qapp, tmp_path, layout)
    _pick(screen)
    fill_operator_fields(screen)
    qapp.processEvents()
    assert screen.can_start()

    screen.fields.serial_edit.setText("   ")
    qapp.processEvents()
    assert not screen.can_start(), "whitespace-only Serial reads as filled"
    for button in _start_controls(screen):
        assert not button.isEnabled(), f"whitespace-only Serial left {button.text()} enabled ({layout})"

    screen.fields.serial_edit.setText("PS-1")
    qapp.processEvents()
    assert screen.can_start(), "a real Serial did not re-enable Start"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_no_runnable_schedule_selected_keeps_start_disabled(qapp, tmp_path, layout):
    """Filled fields with nothing picked — or a broken row made current — cannot start a run."""
    screen = _build(qapp, tmp_path, layout)
    fill_operator_fields(screen)
    qapp.processEvents()

    assert screen.session.selected_schedule is None, "the picker should start with nothing selected"
    assert not screen.can_start(), "can_start() says a run may start with no schedule selected"
    for button in _start_controls(screen):
        assert not button.isEnabled(), f"{button.text()} is enabled with no schedule selected ({layout})"

    _pick(screen)
    qapp.processEvents()
    assert screen.can_start()

    screen.session.clear_selection()
    screen.picker.filter_edit.setText("nothing matches this")
    qapp.processEvents()
    assert screen.picker.selected_name() is None, "the filter left a stale selection"
    assert not screen.can_start(), "clearing the selection did not take Start away"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_start_is_disabled_from_the_moment_a_run_starts_until_it_finishes(qapp, tmp_path, layout):
    """A run in progress takes Start away; it comes back when the run is over."""
    screen = _build(qapp, tmp_path, layout)
    _pick(screen)
    fill_operator_fields(screen)
    qapp.processEvents()
    assert screen.can_start()

    start = screen.start_button if layout == "three-pane" else screen.steps_options_stage.run_button
    start.click()
    qapp.processEvents()

    assert screen.session.state == SessionState.STARTED, "the run never started"
    assert not screen.can_start(), "Start is available while a run is in progress"
    for button in _start_controls(screen):
        assert not button.isEnabled(), f"{button.text()} is clickable during a run ({layout})"

    assert _wait_finished(qapp, screen.session), "the run did not finish"
    assert screen.can_start(), "the run finished but Start stayed disabled"
    for button in _start_controls(screen):
        assert button.isEnabled(), f"{button.text()} stayed disabled after FINISHED ({layout})"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_starting_through_the_start_path_directly_is_refused(qapp, tmp_path, layout):
    """`begin_run` guards itself: a direct call with a blank field starts nothing.

    The button state is not the guard: a test, a shortcut or a re-enabled button can reach the
    slot without the Operator clicking a live Start."""
    screen = _build(qapp, tmp_path, layout)
    _pick(screen)
    fill_operator_fields(screen)
    screen.fields.operator_edit.setText("")
    qapp.processEvents()

    screen.begin_run()
    qapp.processEvents()
    assert screen.session.run_uuid is None, "begin_run started a run with an empty Operator"
    assert screen.session.state != SessionState.STARTED, "begin_run started a run despite a blank field"
    assert screen.engine is None, "begin_run built an engine for a run that could not start"

    screen.fields.operator_edit.setText("Ada")
    qapp.processEvents()
    screen.begin_run()
    qapp.processEvents()
    assert screen.session.state == SessionState.STARTED, "the same call with the fields filled started nothing"
    _wait_finished(qapp, screen.session)


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("field", FIELDS)
def test_the_start_path_refuses_a_direct_call_with_one_field_blank(qapp, tmp_path, layout, field):
    """The direct start path refuses when exactly one required field is blank."""
    screen = _build(qapp, tmp_path, layout)
    _pick(screen)
    fill_operator_fields(screen)
    qapp.processEvents()
    label = FIELD_LABELS[field]
    getattr(screen.fields, f"{field}_edit").setText("")
    qapp.processEvents()
    assert not screen.can_start(), f"can_start() says a run may start with {label} blank ({layout})"

    state, uuid, view = screen.session.state, screen.session.run_uuid, _view_state(screen)
    for action in (screen.begin_run,):
        action()
        qapp.processEvents()
        name = action.__name__
        assert screen.session.run_uuid is None, f"{name} opened a run with {label} blank ({layout})"
        assert screen.engine is None, f"{name} built an engine for a run that could not start ({layout})"
        assert screen.session.state == state, f"{name} changed the session state with {label} blank ({layout})"
        assert _view_state(screen) == view, f"{name} changed what the layout shows ({layout})"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_start_path_refuses_every_direct_call_while_a_run_is_going(qapp, tmp_path, layout):
    """Nothing in the start path starts a second run, or resets the layout, while one is going."""
    screen = _build(qapp, tmp_path, layout)
    _pick(screen, "Blocking input schedule")
    fill_operator_fields(screen)
    qapp.processEvents()
    screen.begin_run()
    qapp.processEvents()
    assert screen.session.state == SessionState.STARTED, "the blocking run never started"
    uuid = _wait_for_run_uuid(qapp, screen.session)
    engine, view = screen.engine, _view_state(screen)

    for action in (screen.begin_run, screen.new_run, screen.start_new_run):
        action()
        qapp.processEvents()
        name = action.__name__
        assert screen.session.state == SessionState.STARTED, f"{name} unlocked a session mid-run ({layout})"
        assert screen.session.run_uuid == uuid, f"{name} replaced the run in progress ({layout})"
        assert screen.engine is engine, f"{name} built a second engine for the same run ({layout})"
        assert _view_state(screen) == view, f"{name} changed what the layout shows mid-run ({layout})"

    screen.session.request_abort()
    assert _wait_finished(qapp, screen.session), "the aborted run never finished"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_new_run_is_refused_until_starting_is_valid(qapp, tmp_path, layout):
    """The "New run" control unlocks the session for the next run; only a run in progress stops it.

    Packet 41's final design: New run is gated on the run, not on the setup. A blank field or a
    schedule that cannot run is a message on the error line, not a lock, so New run works with the
    fields incomplete and the Operator fixes them on the setup stage before Start."""
    screen = _build(qapp, tmp_path, layout)
    screen.start_new_run()
    qapp.processEvents()
    assert screen.session.state == SessionState.IDLE, "New run unlocked a session with nothing selected"

    _pick(screen)
    fill_operator_fields(screen)
    qapp.processEvents()
    screen.begin_run()
    qapp.processEvents()
    assert _wait_finished(qapp, screen.session)

    screen.start_new_run()
    qapp.processEvents()
    assert screen.session.state == SessionState.SELECTING, "New run did not open the session for the next run"
    assert screen.session.selected_schedule == "Passing schedule"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_new_run_keeps_a_refusal_nothing_has_changed(qapp, tmp_path, layout):
    """Packet 41 review: New run clears nothing, so the refusal it inherits stays said.

    The message came from a Start the Operator already tried. New run moves no field and picks no
    schedule, so the reason the attempt failed must still be on the line. An input change — or a
    fresh Start — is what retires it."""
    screen = _build(qapp, tmp_path, layout)
    _pick(screen)
    fill_operator_fields(screen)
    qapp.processEvents()
    (tmp_path / "defs" / "passing.tslua").unlink()
    screen.begin_run()
    qapp.processEvents()
    message = screen.error_label.text()
    assert "not available" in message, "the refused start left no message"

    screen.start_new_run()
    qapp.processEvents()
    assert screen.error_label.text() == message, f"New run erased a refusal nothing changed ({layout})"
    assert screen.session.state != SessionState.STARTED, "New run started a run it was only meant to unlock"

    screen.fields.project_edit.setText("PSU-200 Production")
    qapp.processEvents()
    assert screen.error_label.text() != message, "an input change left the old refusal on the line"


def test_three_pane_new_run_with_no_selection_keeps_the_finished_run(qapp, tmp_path):
    """Packet 41 review: called directly with nothing picked, New run wiped a layout that had a run on it.

    With no schedule to show there is nothing to prepare, so the finished run's rows, tallies and
    log stay exactly where the Operator left them."""
    screen = _three_pane(qapp, tmp_path)
    _pick(screen, "Logging schedule")
    fill_operator_fields(screen)
    screen.begin_run()
    qapp.processEvents()
    assert _wait_finished(qapp, screen.session), "the run never finished"
    for _ in range(50):
        qapp.processEvents()

    rows = screen.table.table.rowCount()
    counts_hidden = screen.counts.isHidden()
    log_lines = screen.log_pane.rendered_lines()
    assert rows and log_lines, "the finished run was not on screen to begin with"

    screen.session.clear_selection()
    screen.start_new_run()
    qapp.processEvents()

    assert screen.table.table.rowCount() == rows, "New run with no selection emptied the step table"
    assert screen.counts.isHidden() is counts_hidden, "New run with no selection changed the tallies"
    assert screen.log_pane.rendered_lines() == log_lines, "New run with no selection cleared the run's log"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_restored_schedule_can_retry_without_rescan(qapp, tmp_path, layout):
    screen = _build(qapp, tmp_path, layout)
    _pick(screen)
    fill_operator_fields(screen)
    path = tmp_path / "defs" / "passing.tslua"
    path.unlink()
    screen.start_button.click()
    message = screen.error_label.text()
    assert "not available" in message
    path.write_text(PASSING, encoding="utf-8", newline="\n")
    _pick(screen)  # re-picking the current row emits no selection change
    screen.start_button.click()
    assert screen.session.state == SessionState.STARTED, "restoring the file without Rescan must allow retry"
    assert screen.error_label.text() == ""
    assert screen._refusal == ""
    assert _wait_finished(qapp, screen.session)


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_refusal_message_survives_refresh_until_an_input_moves(qapp, tmp_path, layout):
    """A refusal is a persistent message; valid inputs still allow another attempt."""
    env = write_ate_env(tmp_path)
    screen = _staged(qapp, tmp_path) if layout == "staged" else _three_pane(qapp, tmp_path)
    _pick(screen)
    fill_operator_fields(screen)
    qapp.processEvents()

    (env["defs_dir"] / "passing.tslua").unlink()  # the picked schedule broke without a Rescan
    screen.start_button.click()
    qapp.processEvents()
    message = screen.error_label.text()
    assert message.startswith(BROKEN_MARKER) and "not available" in message, message
    assert screen.start_button.isEnabled(), f"a refusal blocked retry ({layout})"
    assert screen.session.run_uuid is None and screen.engine is None, "the refused Start opened a run"
    assert screen.session.store.query_runs() == [], "the refused Start wrote a run row in the store"

    screen._refresh_start_state()  # a refresh the Operator did not cause keeps the refusal
    qapp.processEvents()
    assert screen.error_label.text() == message, f"the refresh erased the refusal, leaving {screen.error_label.text()!r}"
    assert screen.start_button.isEnabled(), "a refresh blocked retry"
    assert screen.error_label.property("error_owner") == "refused"

    screen.fields.operator_edit.setText("Ada Lovelace")  # the Operator changed an input
    qapp.processEvents()
    assert screen.error_label.text() == "", f"a stale refusal outlived the inputs it was about: {screen.error_label.text()!r}"
    assert screen.start_button.isEnabled(), f"Start stayed disabled after the inputs changed ({layout})"
    assert screen.can_start(), "can_start() still reports a refusal after an input change"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_refusal_probe_a_keystroke_a_blank_field_and_a_rescan_in_turn(qapp, tmp_path, layout):
    """The exact probe the review ran, in both layouts.

    Pick "Passing schedule", delete `defs/passing.tslua`, click Start: the line reads "schedule
    'Passing schedule' is not available" and retry remains available. Packet 41 fix2 review
    found what happened next: only a run that started cleared that message, so one keystroke in any
    field left Start live under the same stale line, a blank Operator still blamed the schedule, and
    a Rescan that brought the file back changed nothing. Now every input change clears the refusal
    and re-runs the layout's own start refresh."""
    env = write_ate_env(tmp_path)
    screen = StagedRunScreen(HeadlessRunSession(env["config"])) if layout == "staged" else ThreePaneRunScreen(HeadlessRunSession(env["config"]))
    qapp.processEvents()
    _pick(screen, "Passing schedule")
    fill_operator_fields(screen)
    qapp.processEvents()

    (env["defs_dir"] / "passing.tslua").unlink()
    screen.start_button.click()
    qapp.processEvents()
    refusal = screen.error_label.text()
    assert "Passing schedule" in refusal and "not available" in refusal, refusal
    for button in _start_controls(screen):
        assert button.isEnabled(), f"{button.text()} blocks retry ({layout})"

    QTest.keyClick(screen.fields.serial_edit, Qt.Key_9)  # one keystroke, in any field
    qapp.processEvents()
    assert screen.error_label.text() == "", f"the line still blames a schedule nobody changed: {screen.error_label.text()!r}"
    for button in _start_controls(screen):
        assert button.isEnabled(), f"Start stayed off after an input change ({layout})"

    screen.fields.operator_edit.setText("")
    qapp.processEvents()
    named = screen.error_label.text()
    assert "Operator is required" in named, named
    assert "Passing schedule" not in named, f"a blank field still blames the schedule: {named!r}"
    assert not screen.can_start(), "a blank Operator did not keep Start off"

    (env["defs_dir"] / "passing.tslua").write_text(PASSING, encoding="utf-8", newline="\n")
    fill_operator_fields(screen)
    screen.picker.rescan_button.click()
    qapp.processEvents()
    assert screen.error_label.text() == "", f"the Rescan left the old refusal on the line: {screen.error_label.text()!r}"
    assert screen.can_start(), "the schedule is runnable again but Start is still off"
    screen.begin_run()
    qapp.processEvents()
    assert screen.session.state == SessionState.STARTED, "the restored schedule still refused to start"
    screen.session.request_abort()
    assert _wait_finished(qapp, screen.session), "the aborted run never finished"


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("check", ("owner", "source"))
def test_a_layout_switch_carries_the_refusal_to_the_other_layout(qapp, tmp_path, layout, check):
    """A refusal the Start path wrote belongs to the session, not to the layout that wrote it.

    Packet 41 fix2 review: refusing a Start in one layout and switching to the other showed an empty
    error line and a live Start — `_carry_over` copies the fields, the filter and the picked schedule,
    but not the reason the session just gave. Both directions are checked: out of the layout that
    refused and back into it."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])  # the launched window needs a migrated database, like every other launch test
    data = json.loads(env["config"].read_text(encoding="utf-8"))
    data["ui"] = {"layout": "staged" if layout == "staged" else "threePane"}
    config = tmp_path / "switch-config.json"
    config.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    window = launch(["--config", str(config)], exec_=False, prompt=lambda path: True, alert=[]).window
    window.show()
    qapp.processEvents()

    source = window.run_screen
    _pick(source)
    fill_operator_fields(source)
    qapp.processEvents()
    (env["defs_dir"] / "passing.tslua").unlink()
    source.start_button.click()
    qapp.processEvents()
    message = source.error_label.text()
    assert "not available" in message, message
    assert source.can_start(), "the refusing layout blocks retry"

    for target_name in ("threePane" if layout == "staged" else "staged",
                        "staged" if layout == "staged" else "threePane"):
        window.set_layout(target_name)
        qapp.processEvents()
        target = window.run_screen
        assert target.error_label.text() == message, f"the switch dropped the refusal: {target.error_label.text()!r}"
        if check == "owner":
            assert target.error_label.property("error_owner") == "refused", "carried refusal must retain its owner"
        else:
            assert source._refusal == "", "hidden source must not retain a carried refusal"
            assert source.error_label.text() == ""
        assert target.can_start(), f"{target_name} blocks retry"
        for button in _start_controls(target):
            assert button.isEnabled(), f"{button.text()} blocks retry in {target_name}"
        source = target

    target.fields.serial_edit.setText("Changed serial")
    assert target._refusal == ""
    window.set_layout("threePane" if window.layout == "staged" else "staged")
    assert window.run_screen._refusal == "", "switching back revived a stale refusal"
    assert window.run_screen.error_label.text() == ""
    window.session.close()


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_start_problem_is_replaced_when_its_own_reason_changes(qapp, tmp_path, layout):
    """The refresh still updates the line while it holds the message it wrote."""
    screen = _build(qapp, tmp_path, layout)
    _pick(screen)
    fill_operator_fields(screen)
    qapp.processEvents()
    assert screen.error_label.text() == ""

    screen.fields.serial_edit.setText("")
    qapp.processEvents()
    assert "Serial is required" in screen.error_label.text(), screen.error_label.text()
    screen.fields.serial_edit.setText("PS-1")
    qapp.processEvents()
    assert screen.error_label.text() == "", "the start message outlived the field that caused it"


def test_a_refused_new_run_keeps_the_live_run_view_on_screen(qapp, tmp_path):
    """New run clicked while a run is going leaves the Run view and its Abort alone.

    Packet 41 review: the staged layout's `start_new_run` went on to swap the stages back in after
    `new_run` refused the action, so the Operator looking at a live run got the setup stages — with
    the Abort button, the step rows and the Log tab all hidden — while the run carried on."""
    screen = _staged(qapp, tmp_path)
    _pick(screen, "Blocking input schedule")  # a run that stays STARTED until something answers
    fill_operator_fields(screen)
    qapp.processEvents()
    screen.begin_run()
    qapp.processEvents()
    assert screen.session.state == SessionState.STARTED, "the blocking run never started"
    run_uuid = _wait_for_run_uuid(qapp, screen.session)
    before = _view_state(screen)
    assert not screen.run_view.isHidden() and not screen.run_view.abort_button.isHidden(), before

    screen.start_new_run()
    qapp.processEvents()

    assert screen.session.state == SessionState.STARTED, "New run unlocked a session mid-run"
    assert screen.session.run_uuid == run_uuid, "New run ended or replaced the run in progress"
    assert not screen.run_view.isHidden(), "the live Run view was hidden by a start that was refused"
    assert not screen.run_view.abort_button.isHidden(), "Abort went with the Run view: the run cannot be stopped"
    assert screen.steps_stack.isHidden(), "the setup stages were shown again while a run is going"
    screen.session.request_abort()  # the blocking step is only waiting on an input: end the run
    assert _wait_finished(qapp, screen.session), "the aborted run never finished"


def test_new_run_keeps_the_stages_hidden_for_a_run_that_is_still_going(qapp, tmp_path):
    """The three-pane layout refuses the same call without touching its panes."""
    screen = _three_pane(qapp, tmp_path)
    _pick(screen, "Blocking input schedule")
    fill_operator_fields(screen)
    qapp.processEvents()
    screen.begin_run()
    qapp.processEvents()
    assert screen.session.state == SessionState.STARTED
    run_uuid = _wait_for_run_uuid(qapp, screen.session)
    before = _view_state(screen)

    screen.start_new_run()
    qapp.processEvents()

    assert screen.session.state == SessionState.STARTED and screen.session.run_uuid == run_uuid
    assert _view_state(screen) == before, "the refused New run changed what the layout shows"
    assert not screen.abort_button.isHidden(), "Abort was taken away from a run still in progress"
    screen.session.request_abort()
    assert _wait_finished(qapp, screen.session), "the aborted run never finished"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_new_run_returns_to_setup_with_blank_fields(qapp, tmp_path, layout):
    """New run opens setup even when a required field needs repair."""
    screen = _build(qapp, tmp_path, layout)
    start = screen.start_button if layout == "three-pane" else screen.steps_options_stage.run_button
    new_run = screen.new_run_button if layout == "three-pane" else screen.run_view.new_run_button
    _pick(screen)
    fill_operator_fields(screen)
    qapp.processEvents()
    start.click()
    qapp.processEvents()

    assert not new_run.isEnabled(), "New run is live while a run is still going"
    assert _wait_finished(qapp, screen.session)
    assert new_run.isEnabled(), "the finished run offers no way to start the next one"

    screen.fields.operator_edit.setText("")
    qapp.processEvents()
    assert new_run.isEnabled(), "blank fields must not block returning to setup"
    new_run.click()
    assert screen.session.state == SessionState.SELECTING
    assert screen.fields.operator_edit.text() == ""
    assert not screen.can_start()
    if layout == "staged":
        assert not screen.steps_stack.isHidden() and screen.steps_stack.currentIndex() == 0
    else:
        assert screen.counts.isHidden()
        assert all(box.isEnabled() for box in screen.table.checks.values())


def test_prefilled_fields_count_as_filled(qapp, tmp_path):
    """A new run that keeps the previous Operator, UUT Type and Serial is ready to start (packet 30c)."""
    env = write_ate_env(tmp_path)
    screen = StagedRunScreen(HeadlessRunSession(env["config"]))
    _pick(screen)
    fill_operator_fields(screen)
    qapp.processEvents()
    screen.begin_run()
    qapp.processEvents()
    assert _wait_finished(qapp, screen.session) and screen.session.state == SessionState.FINISHED

    screen.start_new_run()
    qapp.processEvents()
    for edit in (screen.fields.project_edit, screen.fields.operator_edit, screen.fields.uut_type_edit,
                 screen.fields.serial_edit):
        assert edit.text().strip(), f"{edit.objectName() or edit.placeholderText()} was cleared by the new run"
    assert screen.can_start(), "the carried-over fields do not count as filled"
    assert screen.steps_options_stage.run_button.isEnabled()
    assert screen.uut_operator_stage.next_button.isEnabled()


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_schedule_selected_on_the_session_counts_as_the_selection(qapp, tmp_path, layout):
    """A pick made outside the picker (History, a carried-over layout) still counts (packets 30c, 30d)."""
    screen = _build(qapp, tmp_path, layout)
    screen.session.select_schedule("Passing schedule")
    fill_operator_fields(screen)
    qapp.processEvents()
    assert screen.can_start(), "can_start() ignored the schedule the session already has"
    for button in _start_controls(screen):
        assert button.isEnabled(), f"{button.text()} stayed disabled for a schedule picked outside the picker ({layout})"


def test_the_disabled_primary_button_is_faded_by_the_stylesheet(qapp, tmp_path):
    """The faded look comes from the one application stylesheet, not from an inline style."""
    assert "QPushButton#primary:disabled" in STYLESHEET, "theme.py has no :disabled rule for the primary button"
    rule = STYLESHEET.split("QPushButton#primary:disabled", 1)[1].split("}", 1)[0]
    assert TOKENS["primary_tint"] in rule and TOKENS["ink_disabled"] in rule, rule

    for screen in (_staged(qapp, tmp_path), _three_pane(qapp, tmp_path)):
        for button in _primary_buttons(screen):
            assert button.objectName() == "primary", f"{button.text()} carries no #primary object name"
            assert not button.styleSheet(), f"{button.text()} is styled inline instead of by the theme"
            assert not button.isEnabled(), f"{button.text()} should start disabled on a fresh layout"


def test_the_schedule_stage_says_why_its_run_button_is_greyed(qapp, tmp_path):
    """Stage 2's "Run this schedule" names its own reason, on a line stage 2 draws.

    Packet 41 fix2 review: the reason went to stage 3's `error_label`, which is not on screen while
    the Operator is still on stage 2, so the button simply looked dead."""
    screen = _staged(qapp, tmp_path)
    screen.show()
    fill_operator_fields(screen)
    qapp.processEvents()
    screen._advance(1)
    qapp.processEvents()

    stage = screen.test_schedule_stage
    hint = stage.run_hint_label
    assert screen.steps_stack.currentWidget() is stage, "the Operator is not on the schedule stage"
    assert not stage.run_button.isEnabled(), "Run this schedule is live with nothing selected"
    assert "No runnable Test Schedule is selected" in hint.text(), hint.text()
    assert hint.objectName() == "run_error", f"the hint is styled {hint.styleSheet()!r}, not by the theme's #run_error"
    assert not hint.styleSheet(), "the hint paints its own colour instead of the theme's error style"
    assert hint.parent() is stage, "the reason is not a widget of the stage the Operator is on"
    assert stage.isVisible() and hint.isVisible(), "the reason is on a widget stage 2 does not show"

    _pick(screen)
    qapp.processEvents()
    assert stage.run_button.isEnabled(), "the schedule is runnable but the button stayed greyed"
    assert hint.text() == "", f"the reason outlived the reason: {hint.text()!r}"

    screen.fields.serial_edit.setText("")
    qapp.processEvents()
    assert "Serial is required" in hint.text(), hint.text()


def test_the_fields_stage_says_why_next_is_disabled(qapp, tmp_path):
    """The greyed Next is not just grey: the line beside it names the field still missing.

    Packet 41 review: on the fields stage the only hint that Serial is missing is the word
    `required` in its placeholder, while the message that names it sits on the Steps stage the
    Operator cannot reach until every field is filled."""
    screen = _staged(qapp, tmp_path)
    _pick(screen)
    fill_operator_fields(screen)
    screen.fields.serial_edit.setText("")
    qapp.processEvents()

    stage = screen.uut_operator_stage
    hint = stage.next_hint_label
    assert not stage.next_button.isEnabled(), "Next is live with Serial blank"
    assert "Serial" in hint.text(), hint.text()
    assert hint.objectName() == "run_error", f"the hint is styled {hint.styleSheet()!r}, not by the theme's error line"
    assert not hint.styleSheet(), "the hint paints its own colour instead of the theme's #run_error"

    screen.fields.operator_edit.setText("")
    qapp.processEvents()
    named = hint.text()
    assert "Operator" in named and "Serial" in named, named

    fill_operator_fields(screen)
    qapp.processEvents()
    assert hint.text() == "", "the hint outlived the field it named"
    assert stage.next_button.isEnabled()


def test_a_blank_field_shows_which_field_is_missing(qapp, tmp_path):
    """The error line names the missing fields, and Project alone keeps its old wording."""
    screen = _staged(qapp, tmp_path)
    _pick(screen)
    qapp.processEvents()

    assert "Operator, UUT Type and Serial are required" in screen.steps_options_stage.error_label.text(), screen.steps_options_stage.error_label.text()
    screen.fields.operator_edit.setText("Ada")
    screen.fields.uut_type_edit.setText("PSU-200")
    screen.fields.serial_edit.setText("PS-1")
    qapp.processEvents()
    assert screen.steps_options_stage.error_label.text() == ""

    screen.fields.serial_edit.setText("")
    qapp.processEvents()
    assert "Serial is required" in screen.steps_options_stage.error_label.text()
    screen.fields.operator_edit.setText("")
    qapp.processEvents()
    named = screen.steps_options_stage.error_label.text()
    assert "Operator and Serial are required" in named, named

    # Project on its own still reads as the message the layouts have carried since packet 30a r3.
    fill_operator_fields(screen)
    screen.fields.serial_edit.setText("PS-1")
    screen.fields.project_edit.setText("")
    qapp.processEvents()
    assert screen.fields.blank_fields() == ["Project"], screen.fields.blank_fields()
    assert screen.steps_options_stage.error_label.text() == f"{BROKEN_MARKER} Project is required before a Test Run can start"

    stage = screen.uut_operator_stage
    for label, edit in ((stage.operator_label, stage.operator_edit), (stage.uut_type_label, stage.uut_type_edit),
                        (stage.serial_label, stage.serial_edit)):
        assert edit.placeholderText() == "required", f"{label.text()} has no required marker"


def test_enter_in_a_field_does_not_leave_the_fields_stage(qapp, tmp_path):
    """The keyboard route forward is blocked the same way the Next button is."""
    screen = _staged(qapp, tmp_path)
    screen.show()
    qapp.processEvents()
    assert screen.steps_stack.currentIndex() == 0

    assert not screen.uut_operator_stage.next_button.isEnabled(), "Next is live with blank fields"
    QTest.keyClick(screen.uut_operator_stage.project_edit, Qt.Key_Return)
    qapp.processEvents()
    assert screen.steps_stack.currentIndex() == 0, "Enter in a field advanced past unfilled fields"

    fill_operator_fields(screen)
    qapp.processEvents()
    assert screen.uut_operator_stage.next_button.isEnabled()
    screen.uut_operator_stage.next_button.click()
    qapp.processEvents()
    assert screen.steps_stack.currentIndex() == 1


def test_forward_navigation_is_blocked_by_every_route(qapp, tmp_path):
    """Next, a direct `_advance`, and "Run this schedule" all stop at the fields stage."""
    screen = _staged(qapp, tmp_path)
    screen.show()
    qapp.processEvents()

    screen.uut_operator_stage.next_button.click()
    screen._advance(1)
    screen._advance(2)
    screen.test_schedule_stage.run_button.click()
    screen._begin_run_from_selection()
    qapp.processEvents()
    assert screen.steps_stack.currentIndex() == 0, "the fields stage was left without the fields filled"

    fill_operator_fields(screen)
    qapp.processEvents()
    screen._advance(2)
    qapp.processEvents()
    assert screen.steps_stack.currentIndex() == 2, "filled fields should allow the jump to the steps stage"


def test_back_is_always_allowed(qapp, tmp_path):
    """Blank fields never trap the Operator on a stage they are already on."""
    screen = _staged(qapp, tmp_path)
    screen.show()
    qapp.processEvents()
    screen._advance(1)  # forward off stage 0 is refused while the fields are blank
    assert screen.steps_stack.currentIndex() == 0

    fill_operator_fields(screen)
    screen._advance(2)
    qapp.processEvents()
    screen.fields.serial_edit.setText("")
    qapp.processEvents()
    screen._advance(1)
    screen._advance(0)
    qapp.processEvents()
    assert screen.steps_stack.currentIndex() == 0, "Back was blocked by a blank field"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_run_in_progress_leaves_the_error_line_blank(qapp, tmp_path, layout):
    """A run already going is not an error, and the faded Start says it plainly.

    Packet 41 fix2 review: mid-run the red line read "× A Test Run is already running" on every run,
    which reads as something being wrong when nothing is. The line says nothing while a run is up;
    the guard that keeps Start off is still checked here."""
    screen = _build(qapp, tmp_path, layout)
    _pick(screen, "Blocking input schedule")
    fill_operator_fields(screen)
    qapp.processEvents()
    screen.begin_run()
    qapp.processEvents()
    assert screen.session.state == SessionState.STARTED, "the blocking run never started"
    assert screen.error_label.text() == "", f"the error line reports a run in progress: {screen.error_label.text()!r}"
    for button in _start_controls(screen):
        assert not button.isEnabled(), f"{button.text()} is live while a run is going ({layout})"
    screen.session.request_abort()
    assert _wait_finished(qapp, screen.session), "the aborted run never finished"
    assert screen.error_label.text() == ""
    for button in _start_controls(screen):
        assert button.isEnabled(), f"Start stayed off after the run finished ({layout})"


def test_a_disabled_abort_button_is_faded_by_the_stylesheet(qapp, tmp_path):
    """`QPushButton#danger` outranks the plain `QPushButton:disabled` rule, so it needs its own.

    Packet 41 fix2 review: a disabled Abort kept its red outline and its surface colour and looked
    clickable. The theme now fades it like every other disabled button."""
    assert "QPushButton#danger:disabled" in STYLESHEET, "theme.py has no :disabled rule for the danger button"
    rule = STYLESHEET.split("QPushButton#danger:disabled", 1)[1].split("}", 1)[0]
    assert TOKENS["subtle"] in rule and TOKENS["ink_disabled"] in rule, rule

    for screen in (_staged(qapp, tmp_path), _three_pane(qapp, tmp_path)):
        button = screen.abort_button if isinstance(screen, ThreePaneRunScreen) else screen.run_view.abort_button
        assert button.objectName() == "danger", "the Abort control carries no #danger object name"
        assert not button.isEnabled(), "Abort is not disabled on a layout with no run to abort"


def test_the_two_messages_name_the_blank_fields_from_one_place(qapp, tmp_path):
    """The fields stage's hint and the start problem list the blanks in the same words.

    Packet 41 fix2: `_fields_problem` repeated `_start_problem`'s missing-field branch, so the list of
    blanks — singular for one, joined in the form's order for several — was written twice. Both
    messages now read it from `_missing_field_phrase`, so changing one has to show up in the other."""
    screen = _staged(qapp, tmp_path)
    _pick(screen)
    fill_operator_fields(screen)
    screen.fields.operator_edit.setText("")
    screen.fields.serial_edit.setText("")
    qapp.processEvents()

    start = screen._start_problem()
    fields = screen._fields_problem()
    assert start == f"{BROKEN_MARKER} Operator and Serial are required before a Test Run can start", start
    assert fields == f"{BROKEN_MARKER} Fill in Operator and Serial to continue", fields
    assert screen._missing_field_phrase(["Serial"]) == "Serial"
    assert screen._missing_field_phrase(["Operator", "Serial", "Project"]) == "Operator, Serial and Project"

    screen.fields.serial_edit.setText("PS-1")
    qapp.processEvents()
    assert screen._start_problem() == f"{BROKEN_MARKER} Operator is required before a Test Run can start", screen._start_problem()
    assert screen._fields_problem() == f"{BROKEN_MARKER} Fill in Operator to continue", screen._fields_problem()
