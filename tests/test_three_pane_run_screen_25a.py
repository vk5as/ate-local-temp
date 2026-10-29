"""Packet 25a — the three-pane Run screen, driven offscreen against real schedules.

Left pane: the Operator's fields, the filterable schedule picker with errors and mismatches inline,
Start/Abort and the live Result counts. Centre pane: the step table (Result, Attempts, Actual,
Expected, Message), the Step Inputs under their step, and a Stop-on-Fail prompt inline under the
failed step with the sticky banner above the table. Right pane: the log. The status bar runs along
the bottom of all three.

Every test drives the real :class:`ThreePaneRunScreen` on a real :class:`HeadlessRunSession`: nothing
here mocks the engine.
"""

import hashlib
import json
import time
from collections import Counter
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtWidgets import (
    QCheckBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSplitter,
    QTableWidget,
)

from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.ui.staged_run_screen import StagedRunScreen
from ate.ui.three_pane_run_screen import COLUMNS, ThreePaneRunScreen

from ._fixtures import add_header_schedule, add_logging_input_schedule, add_long_schedule, write_ate_env

SHOTS = Path("/tmp/25a-shots")
R2_SHOTS = Path("/tmp/25a-r2-shots")


def _wait_finished(qapp, session, timeout=10.0):
    """Pump events until the session reaches FINISHED."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if session.state == SessionState.FINISHED:
            return True
        time.sleep(0.005)
    return False


def _settle(qapp, session, timeout=5.0):
    """Let the run's report helper finish before the session goes out of scope.

    Every finished run starts a PDF helper process (packet 22a); if the session is destroyed while
    it is still running, Qt aborts the test process at teardown."""
    finished = {"done": False}
    session.report_written.connect(lambda *_seen: finished.__setitem__("done", True))
    deadline = time.monotonic() + timeout
    while not finished["done"] and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    session.close()


def _pick(screen, name):
    """Select the picker row that stands for a schedule whose name contains ``name``."""
    list_widget = screen.picker.schedule_list
    for i in range(list_widget.count()):
        item = list_widget.item(i)
        widget = list_widget.itemWidget(item)
        if widget is None:  # a plain text row names the schedule itself
            if name in item.text():
                list_widget.setCurrentItem(item)
                return item
            continue
        # A widget row paints its name on a label; the broken ones start with ⛔.
        for label in widget.findChildren(QLabel):
            if name in label.text():
                list_widget.setCurrentItem(item)
                return item
    raise AssertionError(f"no schedule row containing {name!r}")


def _row_of(screen, step_number):
    """The table row whose Step cell starts with the given Step Number."""
    for seq, row in screen.table._step_rows.items():
        if screen.table.cell(row, 1).strip().startswith(step_number):
            return seq, row
    raise AssertionError(f"no step row for step {step_number!r}")


def _widget_row(screen, widget) -> int:
    """The table row a cell widget sits in, for asserting an input is under its own step."""
    table = screen.table.table
    for row in range(table.rowCount()):
        if table.cellWidget(row, 0) is widget:
            return row
    raise AssertionError(f"{widget} is not in any table row")


def _rect(widget):
    """A widget's rectangle in screen coordinates: a child's geometry is relative to its parent."""
    rect = widget.geometry()
    parent = widget.parent()
    while parent is not None:
        rect = rect.translated(parent.pos())
        parent = parent.parent()
    return rect


def _painter_of(widget, pane):
    """The container that paints ``widget``: the pane itself, or the scroll viewport it sits in.

    Widgets in different containers are placed by the layout and clipped by the view that holds
    them, so only widgets sharing a container can end up drawn on top of one another."""
    node = widget
    while node is not None and node is not pane:
        if node.objectName() == "qt_scrollarea_viewport":
            return node
        node = node.parent()
    return pane


def _flag_a_mismatch(tmp_path) -> str:
    """Write a wrong checksum for one schedule's own file into the demo Test Definition."""
    defs = tmp_path / "defs"
    atedef = json.loads((defs / "demo.atedef").read_text(encoding="utf-8"))
    for entry in atedef["files"]:
        if entry["path"] == "passing.tslua":
            entry["sha256"] = "0" * 64
            break
    (defs / "demo.atedef").write_text(json.dumps(atedef), encoding="utf-8")
    return "passing.tslua"


def _add_debug_schedule(tmp_path) -> str:
    """Add a Stop-on-Fail schedule that logs at INFO and DEBUG, so the log filter is testable."""
    defs = tmp_path / "defs"
    src = (
        'return ScheduleParams("Debug pause schedule", "logs at INFO and DEBUG, then fails")\n'
        '  :setStopOnFail(true)\n'
        '  :setSchedule({\n'
        '    StepParams("first"):setStepNo("1.")\n'
        '      :setAction(function() ATE.log(Log.INFO, "step one running") ATE.log(Log.DEBUG, "hidden chatter") ATE.report(Result.FAIL, "wrong", "Red", "Green") end),\n'
        '    StepParams("second"):setStepNo("2.")\n'
        '      :setAction(function() ATE.report(Result.PASS, "ok") end),\n'
        '  })\n'
    )
    (defs / "debug_pause.tslua").write_text(src, encoding="utf-8")
    atedef = json.loads((defs / "demo.atedef").read_text(encoding="utf-8"))
    atedef["files"].append({"path": "debug_pause.tslua", "sha256": hashlib.sha256(src.encode("utf-8")).hexdigest()})
    (defs / "demo.atedef").write_text(json.dumps(atedef), encoding="utf-8")
    return "Debug pause schedule"


def _break_a_schedule(tmp_path) -> str:
    """Add a schedule that raises on load to the demo Test Definition, so the picker has a broken row."""
    defs = tmp_path / "defs"
    bad = defs / "bad.tslua"
    bad.write_text("this is not valid lua\n", encoding="utf-8")
    atedef = json.loads((defs / "demo.atedef").read_text(encoding="utf-8"))
    atedef["files"].append({"path": "bad.tslua", "sha256": hashlib.sha256(bad.read_bytes()).hexdigest()})
    (defs / "demo.atedef").write_text(json.dumps(atedef), encoding="utf-8")
    return "bad.tslua"


def _start_staged_run(screen, schedule_name):
    """Select a schedule on the staged screen and start it from the Steps & options stage."""
    list_widget = screen.test_schedule_stage.schedule_list
    for i in range(list_widget.count()):
        if schedule_name in list_widget.item(i).text():
            list_widget.setCurrentItem(list_widget.item(i))
            break
    else:
        raise AssertionError(f"no schedule row containing {schedule_name!r}")
    screen._begin_run_from_selection()
    screen.steps_options_stage.run_button.click()


def test_a_sixty_step_run_finishes_through_both_layouts(qapp, tmp_path):
    """Packet 25a review: a long run through either layout reaches FINISHED, not "database is locked".

    `RunStore._run` used to return the open Result, and the caller read it after the connection had
    gone back to the pool. The run worker then drew that same sqlite3 connection out of the pool and
    INSERTed on it while the GUI thread's read statement was still unfinalised — SQLite answered BUSY
    and the run died mid-schedule. Sixty steps is long enough for both screens to read the store while
    the worker is still writing: the three-pane table syncs on every step and the staged screen
    rebuilds its step list at the Stop-on-Fail pause. The whole thing must be deterministic and well
    inside ten seconds."""
    env = write_ate_env(tmp_path)
    name = add_long_schedule(tmp_path)
    elapsed = []

    for build in (ThreePaneRunScreen, StagedRunScreen):
        session = HeadlessRunSession(env["config"])
        session.failure_prompt.connect(lambda *_seen: session.choose("CONTINUE"))
        screen = build(session)
        screen.resize(1280, 800)
        screen.show()
        qapp.processEvents()
        if build is ThreePaneRunScreen:
            _pick(screen, name)
            screen.fields.operator_edit.setText("Ada")
            screen.start_button.click()
        else:
            screen.uut_operator_stage.operator_edit.setText("Ada")
            screen._advance(1)
            _start_staged_run(screen, name)

        started = time.monotonic()
        assert _wait_finished(qapp, session), f"{build.__name__} never reached FINISHED"
        elapsed.append(time.monotonic() - started)

        run = session.store.get_test_run(session.run_uuid)
        nodes = [n for n in session.store.nodes(session.run_uuid) if n.get("kind") == "step"]
        assert run is not None, f"{build.__name__}: the run was never recorded"
        assert len(nodes) == 60, f"{build.__name__}: {len(nodes)} of 60 steps recorded"
        results = Counter(n.get("result") for n in nodes)
        assert results["FAIL"] == 1 and results["PASS"] == 59, results
        assert run["result"] == "FAIL"
        assert run["end_reason"] == "COMPLETED", run["end_reason"]
        assert screen.state == SessionState.FINISHED
        _settle(qapp, session)
        screen.close()

    assert max(elapsed) < 10.0, f"a 60-step run took {max(elapsed):.1f} s: {elapsed}"


def test_three_panes_are_built_and_split_25_50_25(qapp, tmp_path):
    """The screen is a QSplitter of three panes with the status bar beneath them."""
    env = write_ate_env(tmp_path)
    screen = ThreePaneRunScreen(HeadlessRunSession(env["config"]))
    screen.resize(1280, 800)
    screen.show()
    qapp.processEvents()

    assert isinstance(screen.splitter, QSplitter)
    assert screen.splitter.orientation() == Qt.Orientation.Horizontal
    assert [screen.splitter.widget(i) for i in range(3)] == [screen.left_pane, screen.centre_pane, screen.right_pane]
    sizes = screen.splitter.sizes()
    total = sum(sizes)
    assert abs(sizes[0] / total - 0.25) < 0.08, sizes
    assert abs(sizes[1] / total - 0.50) < 0.08, sizes
    # The status bar is the last widget of the root layout, under all three panes.
    layout = screen.layout()
    assert layout.itemAt(layout.count() - 1).widget() is screen.status_bar
    assert screen.status_bar.status_label.text() == "Ready"
    # Everything sits inside its own pane, and nothing overlaps.
    for pane, child in (
        (screen.left_pane, screen.picker.schedule_list),
        (screen.left_pane, screen.fields.project_edit),
        (screen.left_pane, screen.start_button),
        (screen.centre_pane, screen.table.table),
        (screen.right_pane, screen.log_pane),
    ):
        assert child.isVisible(), f"{child} is not visible in its pane"
        assert _rect(pane).contains(_rect(child)), f"{child} sits outside its pane"
    left, centre, right = (_rect(screen.splitter.widget(i)) for i in range(3))
    assert left.right() <= centre.left() and centre.right() <= right.left()
    assert _rect(screen.status_bar).top() >= max(left.bottom(), centre.bottom(), right.bottom())
    # The step table must not need a sideways scrollbar inside its pane: the Message column
    # stretches to whatever width is left rather than adding fixed columns beyond it.
    qapp.processEvents()
    header = screen.table.table.horizontalHeader()
    viewport = screen.table.table.viewport()
    table_width = sum(header.sectionSize(i) for i in range(screen.table.table.columnCount()))
    assert viewport.width() - 1 <= table_width <= viewport.width() + 1, (
        f"the step table is {table_width} px wide in a {viewport.width()} px viewport"
    )
    # Every header word fits in its own column: no column is so narrow that its name is clipped.
    metrics = QFontMetrics(screen.table.table.font())
    for index, name in enumerate(COLUMNS):
        assert header.sectionSize(index) >= metrics.horizontalAdvance(name), f"the {name!r} header is clipped"


def test_left_pane_shows_fields_counts_and_a_filterable_list(qapp, tmp_path):
    """Project prefills from defaultProject; the filter narrows the list; Start needs both."""
    env = write_ate_env(tmp_path)
    screen = ThreePaneRunScreen(HeadlessRunSession(env["config"]))
    screen.show()
    qapp.processEvents()

    assert isinstance(screen.table.table, QTableWidget)
    assert screen.fields.project_edit.text() == "Power board"
    assert screen.fields.operator_edit.placeholderText() == "required"
    list_widget = screen.picker.schedule_list
    assert isinstance(list_widget, QListWidget)
    before = list_widget.count()
    screen.picker.filter_edit.setText("Blocking")
    assert 0 < list_widget.count() < before
    screen.picker.filter_edit.setText("")

    # Every row names its schedule and the Media Release it came from (spec: name and Media Release).
    for i in range(list_widget.count()):
        widget = list_widget.itemWidget(list_widget.item(i))
        assert widget is not None, f"row {i} is not a schedule row"
        texts = [label.text() for label in widget.findChildren(QLabel)]
        assert texts[0] and "REL-A" in texts[1], texts

    # An empty Project blocks Start, exactly as on the staged screen.
    _pick(screen, "Passing schedule")
    screen.fields.project_edit.setText("")
    assert screen.start_button.isEnabled() is False
    assert "Project is required" in screen.error_label.text()
    screen.fields.project_edit.setText("Power board")
    assert screen.start_button.isEnabled() is True

    # The "On failure" pair follows the picked schedule's :setStopOnFail, and the Operator can still flip it.
    _pick(screen, "Stop on fail false schedule")
    assert screen.continue_all_radio.isChecked() and not screen.stop_on_fail_radio.isChecked()
    _pick(screen, "Fail then pass schedule")
    assert screen.stop_on_fail_radio.isChecked() and not screen.continue_all_radio.isChecked()
    screen.continue_all_radio.setChecked(True)
    assert screen.stop_on_fail_radio.isChecked() is False


def test_a_broken_row_shows_its_error_inline_and_cannot_be_started(qapp, tmp_path):
    """A broken schedule names its error in its own row and never becomes the selection."""
    env = write_ate_env(tmp_path)
    name = _break_a_schedule(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = ThreePaneRunScreen(session)
    screen.show()
    qapp.processEvents()

    list_widget = screen.picker.schedule_list
    assert name not in [list_widget.item(i).text() for i in range(list_widget.count())]
    screen.picker.rescan_button.click()

    broken = [
        list_widget.item(i)
        for i in range(list_widget.count())
        if list_widget.item(i).data(Qt.ItemDataRole.UserRole + 1)
    ]
    assert broken, "the picker lists no broken schedule rows"
    item = broken[0]
    row_widget = list_widget.itemWidget(item)
    error_label = row_widget.findChild(QLabel, "schedule_error")
    assert error_label is not None and error_label.text(), "the broken row shows no inline error"
    assert error_label.wordWrap()
    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsSelectable)  # even if forced selectable
    list_widget.setCurrentItem(item)
    assert not session.selected_schedule
    assert screen.start_button.isEnabled() is False


def test_a_mismatched_schedule_flags_its_files_inside_its_row(qapp, tmp_path):
    """Spec: the schedule list shows errors and mismatches inline, in the row it belongs to."""
    env = write_ate_env(tmp_path)
    bad_file = _flag_a_mismatch(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = ThreePaneRunScreen(session)
    screen.show()
    qapp.processEvents()

    list_widget = screen.picker.schedule_list
    notes = [
        widget.findChild(QLabel, "schedule_mismatch")
        for i in range(list_widget.count())
        if (widget := list_widget.itemWidget(list_widget.item(i))) is not None
    ]
    note = next((n for n in notes if n is not None), None)
    assert note is not None, "no schedule row carries a mismatch note inline"
    assert bad_file in note.text()
    assert note.wordWrap()

    # A flagged row is still a schedule the Operator can start from.
    _pick(screen, "Passing schedule")
    assert session.selected_schedule == "Passing schedule"
    assert screen.start_button.isEnabled() is True


def test_running_a_schedule_through_the_widgets_finishes(qapp, tmp_path):
    """Pick, fill, Start, answer one blocking input through its widget: the run reaches FINISHED."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = ThreePaneRunScreen(session)
    screen.resize(1280, 800)
    screen.show()
    qapp.processEvents()

    screen.fields.operator_edit.setText("Ada")
    screen.fields.uut_type_edit.setText("PSU-200")
    screen.fields.serial_edit.setText("1234")
    _pick(screen, "Blocking input schedule")
    assert session.selected_schedule == "Blocking input schedule"
    screen.start_button.click()

    answered = False
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        widget = screen.table.input_widget(0, 1)
        if widget is not None and widget.line_edit is not None and widget.line_edit.isEnabled():
            widget.line_edit.setText("7")
            widget.ok_button.click()
            answered = True
            break
        time.sleep(0.01)
    assert answered, "the blocking input never became live in its widget"
    assert _wait_finished(qapp, session)

    row = session.store.attempts(session.run_uuid)[0]
    assert row["pass_fail"] == "PASS"
    run = session.store.get_test_run(session.run_uuid)
    assert run["operator"] == "Ada" and run["result"] == "PASS"
    assert screen.table.input_widget(0, 1).line_edit.text() == "7"  # a typed input keeps the answer in its edit
    # The Step Input sits in its own row directly under the step that declared it, spanning the
    # whole table so it reads as part of that step rather than as a step of its own.
    _, step_row = _row_of(screen, "1.")
    input_row = _widget_row(screen, screen.table.input_widget(0, 1))
    assert input_row == step_row + 1, "the Step Input is not under the step that declared it"
    assert screen.table.table.columnSpan(input_row, 0) == len(COLUMNS), "the input row is not spanned"
    _settle(qapp, session)


def test_the_table_reports_result_attempts_actual_expected_message(qapp, tmp_path):
    """A passing step and a failing step each show their Attempt's Actual, Expected and Message."""
    env = write_ate_env(tmp_path)

    session = HeadlessRunSession(env["config"])
    session.failure_prompt.connect(lambda *_seen: session.choose("CONTINUE"))
    screen = ThreePaneRunScreen(session)
    screen.show()
    qapp.processEvents()
    _pick(screen, "Failing schedule")
    screen.start_button.click()
    assert _wait_finished(qapp, session)
    _, row = _row_of(screen, "1.")
    assert screen.table.cell(row, COLUMNS.index("Result")) == "FAIL"
    assert screen.table.cell(row, COLUMNS.index("Attempts")) == "1"
    assert screen.table.cell(row, COLUMNS.index("Actual")) == "Red"
    assert screen.table.cell(row, COLUMNS.index("Expected")) == "Green"
    assert "wrong colour" in screen.table.cell(row, COLUMNS.index("Message"))
    _settle(qapp, session)

    session2 = HeadlessRunSession(env["config"])
    screen2 = ThreePaneRunScreen(session2)
    screen2.show()
    qapp.processEvents()
    _pick(screen2, "Passing schedule")
    screen2.start_button.click()
    assert _wait_finished(qapp, session2)
    _, row = _row_of(screen2, "1.")
    assert screen2.table.cell(row, COLUMNS.index("Result")) == "PASS"
    assert screen2.table.cell(row, COLUMNS.index("Attempts")) == "1"
    assert screen2.table.cell(row, COLUMNS.index("Message")) == "ok"
    headers = [screen2.table.table.horizontalHeaderItem(i).text() for i in range(screen2.table.table.columnCount())]
    assert headers == list(COLUMNS)
    assert COLUMNS[1:] == ("Step", "Result", "Attempts", "Actual", "Expected", "Message")
    # A finished run leaves its rows plain: clearing a tint must not leave the row black.
    for column in range(len(COLUMNS)):
        item = screen2.table.table.item(row, column)
        if item is not None:
            assert item.background().color().name().lower() == "#ffffff", f"column {COLUMNS[column]} is tinted"
    _settle(qapp, session2)


def test_a_retry_shows_the_attempt_count_in_the_table(qapp, tmp_path):
    """Retried steps show their Attempts, and the Result follows the last Attempt."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = ThreePaneRunScreen(session)
    screen.show()
    qapp.processEvents()

    def _answer(_node_seq, _attempt_no):
        screen.table._prompt_bars[_node_seq].retry_button.click()

    session.failure_prompt.connect(_answer)
    _pick(screen, "Passes on retry schedule")
    screen.start_button.click()
    assert _wait_finished(qapp, session)
    _, row = _row_of(screen, "1.")
    assert screen.table.cell(row, COLUMNS.index("Result")) == "PASS"
    assert screen.table.cell(row, COLUMNS.index("Attempts")) == "2"
    assert screen.table.cell(row, COLUMNS.index("Actual")) == "2.4V"
    _settle(qapp, session)


def test_counts_are_live_and_the_log_shows_info_and_above(qapp, tmp_path):
    """The left pane tallies Results as the run goes, and the log pane keeps DEBUG out."""
    env = write_ate_env(tmp_path)
    _add_debug_schedule(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = ThreePaneRunScreen(session)
    screen.show()
    qapp.processEvents()

    _pick(screen, "Debug pause schedule")
    live = []

    def _snapshot_while_paused(*_seen):
        """Read the tallies the moment the run pauses: step 1 has a Result, step 2 has none yet."""
        screen._sync_from_store()
        live.append(screen.counts.counts())

    # Snapshot before answering: the pause is mid-run, so the tallies are only partly filled.
    session.failure_prompt.connect(_snapshot_while_paused)
    session.failure_prompt.connect(lambda *_seen: session.choose("CONTINUE"))
    screen.start_button.click()
    assert _wait_finished(qapp, session)
    assert live, "the counts never moved while the run was up"
    assert live[0]["FAIL"] == 1 and live[0]["PASS"] == 0, live[0]
    assert screen.counts.counts()["FAIL"] == 1 and screen.counts.counts()["PASS"] == 1
    assert screen.counts.isVisible()
    info = screen.log_pane.rendered_lines()
    debug = screen.debug_window.rendered_lines()
    assert info, "the log pane rendered nothing at all"
    assert len(debug) > len(info), f"the log pane kept every level: {len(info)} of {len(debug)}"
    assert any("hidden chatter" in line for line in debug), "the debug window lost the DEBUG line"
    assert not any("hidden chatter" in line for line in info), "the log pane rendered a DEBUG line"
    _settle(qapp, session)


def test_stop_on_fail_pause_shows_the_inline_prompt_and_banner(qapp, tmp_path):
    """A pause puts Continue/Retry/Abort directly under the failed step and a sticky banner above."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = ThreePaneRunScreen(session)
    screen.resize(1280, 800)
    screen.show()
    qapp.processEvents()

    answered = []
    screen.table.prompt_choice.connect(answered.append)
    _pick(screen, "Fail then pass schedule")
    # Before a run starts the centre pane already holds the schedule's steps and their boxes:
    # two step rows, each with its own hidden prompt row beneath it.
    assert screen.table.table.rowCount() == 4, "the steps are not in the table before the run"
    assert len(screen.table.checks) == 2, "no enable box was built for the steps"
    for seq, step_row in screen.table._step_rows.items():
        assert screen.table.table.cellWidget(step_row, 0) is screen.table.checks[seq], "the box is not in its row"
        assert screen.table.table.isRowHidden(step_row + 1), "the step's prompt row is not hidden before a pause"
    screen.start_button.click()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if screen.table.banner.isVisible():
            break
        time.sleep(0.01)
    assert screen.table.banner.isVisible(), "the sticky banner never appeared"
    assert "Continue, Retry or Abort" in screen.table.banner.text()

    seq, row = _row_of(screen, "1.")
    prompt_row = screen.table._prompt_rows[seq]
    bar = screen.table._prompt_bars[seq]
    assert bar.isVisible() and screen.table.table.cellWidget(prompt_row, 0) is bar
    # The prompt row is the row immediately after the failed step's row.
    assert prompt_row == row + 1
    assert screen.table.table.columnSpan(prompt_row, 0) == len(COLUMNS), "the prompt is not spanned across the table"
    assert bar.step_label.text() == "Step 1. FAIL"
    assert screen.table.table.item(row, COLUMNS.index("Result")).text() == "FAIL"
    assert screen.table.table.item(row, COLUMNS.index("Result")).background().color().name().lower().startswith("#fff0f0")
    other_row = screen.table._step_rows[1]
    assert screen.table.table.item(other_row, COLUMNS.index("Result")).background().color().name().lower() == "#ffffff", "a step not paused is tinted"
    assert screen.status_bar.status_label.text() == "Paused"

    bar.continue_button.click()
    qapp.processEvents()
    assert answered == ["CONTINUE"]
    assert screen.table.banner.isVisible() is False
    assert screen.table.table.isRowHidden(prompt_row)
    assert screen.status_bar.status_label.text() == "Running"
    assert _wait_finished(qapp, session)
    assert screen.status_bar.status_label.text() == "Finished"
    assert screen.table.table.item(row, COLUMNS.index("Result")).text() == "FAIL"
    assert not screen.table.table.item(row, COLUMNS.index("Result")).background().color().name().lower().startswith("#fff0f0")
    _settle(qapp, session)


def test_abort_answers_a_pending_prompt_from_the_left_pane(qapp, tmp_path):
    """The Abort control ends the run: it answers the pause when one is up."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = ThreePaneRunScreen(session)
    screen.show()
    qapp.processEvents()
    _pick(screen, "Fail then pass schedule")
    screen.start_button.click()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if screen.abort_button.isEnabled() and screen.table.banner.isVisible():
            break
        time.sleep(0.01)
    screen.abort_button.click()
    assert _wait_finished(qapp, session)
    run = session.store.get_test_run(session.run_uuid)
    assert run["result"] == "FAIL"
    results = [node["result"] for node in session.store.nodes(session.run_uuid) if node["kind"] == "step"]
    assert "NOTRUN" in results
    _settle(qapp, session)


def test_steps_are_enablable_before_the_run_under_a_header(qapp, tmp_path):
    """The centre pane's boxes toggle steps before a run starts, headers included (packet 15d)."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = ThreePaneRunScreen(session)
    screen.show()
    qapp.processEvents()
    _pick(screen, "Enable/disable schedule")
    boxes = screen.table.checks
    assert boxes, "the step table has no enable boxes"
    for box in boxes.values():
        assert box.isEnabled()
    step_seq = min(boxes)
    boxes[step_seq].setChecked(False)
    qapp.processEvents()
    assert session.node_enables.get(step_seq) is False
    _pick(screen, "Passing schedule")
    screen.start_button.click()
    assert _wait_finished(qapp, session)
    _settle(qapp, session)


def test_no_visible_text_overlaps_and_every_pane_holds_its_own_widgets(qapp, tmp_path):
    """While a run is paused, nothing drawn in one pane collides with another."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    session.failure_prompt.connect(lambda *_seen: session.choose("CONTINUE"))
    screen = ThreePaneRunScreen(session)
    screen.resize(1280, 800)
    screen.show()
    qapp.processEvents()
    _pick(screen, "Fail then pass schedule")
    screen.start_button.click()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if screen.table.banner.isVisible():
            break
        time.sleep(0.01)
    # Answer the pause through the inline bar so the run finishes on its own.
    if screen.table.banner.isVisible():
        screen.table._prompt_bars[0].continue_button.click()
        qapp.processEvents()
    assert _wait_finished(qapp, session)

    panes = [screen.left_pane, screen.centre_pane, screen.right_pane]
    for pane in panes:
        groups: dict = {}
        for widget in pane.findChildren((QLabel, QPushButton, QCheckBox, QLineEdit)):
            if not widget.isVisible():
                continue
            groups.setdefault(_painter_of(widget, pane), []).append(widget)
        assert any(groups.values()), f"nothing is drawn in {pane}"
        for painter, widgets in groups.items():
            if painter is not pane:
                assert _rect(pane).contains(_rect(painter)), f"{painter} spills out of its pane"
            for index, first in enumerate(widgets):
                for other in widgets[index + 1:]:
                    if _rect(first).intersected(_rect(other)).width() > 0:
                        raise AssertionError(
                            f"{first.text() or first.objectName()} overlaps {other.text() or other.objectName()}"
                        )
    for pane in panes:
        assert _rect(pane).width() > 0 and _rect(pane).height() > 0
    # The step table fills the centre pane's width, so no cell can spill out of it.
    table_rect = _rect(screen.table.table)
    assert _rect(screen.centre_pane).contains(table_rect)
    assert screen.table.table.rowCount() > 0
    _settle(qapp, session)


def test_the_four_states_are_captured_as_screenshots(qapp, tmp_path):
    """Setup, running with a waiting input, paused on a failure, and finished with counts."""
    SHOTS.mkdir(parents=True, exist_ok=True)

    def shot(name, screen):
        screen.grab().save(str(SHOTS / name))

    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = ThreePaneRunScreen(session)
    screen.resize(1280, 800)
    screen.show()
    qapp.processEvents()
    _pick(screen, "Fail then pass schedule")
    screen.fields.operator_edit.setText("Ada")
    shot("setup-schedule-list.png", screen)

    screen.start_button.click()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if screen.table.banner.isVisible():
            break
        time.sleep(0.01)
    shot("paused-stop-on-fail.png", screen)
    screen.table._prompt_bars[0].continue_button.click()
    qapp.processEvents()
    assert _wait_finished(qapp, session)
    shot("finished-counts.png", screen)
    _settle(qapp, session)

    session2 = HeadlessRunSession(env["config"])
    screen2 = ThreePaneRunScreen(session2)
    screen2.resize(1280, 800)
    screen2.show()
    qapp.processEvents()
    _pick(screen2, "Blocking input schedule")
    screen2.fields.serial_edit.setText("1234")
    screen2.start_button.click()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        widget = screen2.table.input_widget(0, 1)
        if widget is not None and widget.line_edit is not None and widget.line_edit.isEnabled():
            shot("running-input-waiting.png", screen2)
            break
        time.sleep(0.01)
    session2.request_abort()
    _wait_finished(qapp, session2)
    _settle(qapp, session2)

    for name in ("setup-schedule-list.png", "paused-stop-on-fail.png", "running-input-waiting.png", "finished-counts.png"):
        assert (SHOTS / name).exists(), name
