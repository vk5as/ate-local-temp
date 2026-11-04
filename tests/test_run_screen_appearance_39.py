"""Ticket 39 review pass — one regression test for each appearance defect the reviews found.

Each test below pins the behaviour the reviews accepted, and each one fails against the code as
it stood when those reviews were written:

1. `StepRowDelegate.paint` asked a `QBrush` for `brushStyle()`, which PySide6 does not have, so
   every step row in both layouts painted nothing at all.
2. The staged Run view rebuilt its rows from `store.nodes()`, which has a row only once a step's
   last Attempt is recorded — the steps the run had not reached, and their Step Input widgets,
   disappeared from the list while the run was still going.
3. The collapsed-input summary label had no parent and was never added to the widget's layout, so
   it was a top-level window of its own.
4. The one-second header timer kept ticking after the window closed, reading the run row from a
   store that MainWindow had already closed.
5. `min-height: 32px` applied to every `QPushButton` pushed the window's minimum height to 887 px,
   above a 1366x768 screen.
6. The finished run's refresh cleared the Log pane and the shared Debug window instead of
   appending the lines the live refresh had not reached.
7. A badge was sized by eye (72 px), too narrow for "NOREPORT", so the word was dropped; the
   three-pane Result column was fixed at 78 px whatever the run showed.

The second pass (the reviews re-run after those seven were fixed) found five more, each pinned
below in the same way:

8. `StepRowDelegate.paint` chose its ink from the row's *state* — white whenever the row was
   selected — so a selected current or failed row kept its pale tint with white text on it
   (1.23:1), and a selected plain row's Actual/Expected line was muted ink on solid primary
   (1.01:1).
9. `RunStateChip.set_state` repainted without invalidating the chip's geometry, so a header that
   started READY kept the chip at 140 px and squeezed "INPUT NEEDED", which needs 223 px.
10. `FailurePromptBar`'s buttons kept their own size hints: 106 px, 78 px and 79 px, three
    unequal buttons where D6 asks for three equal ones.
11. The staged Run view drew a paused step's Result from its node row, which the engine writes
    only after the Operator answers, so switching layouts mid-pause left the failed row with no
    badge while the prompt bar still named the failure.
12. `_deferred_run_uuid` outlived the run it came from: a layout hidden through a finished run
    drew that run when the Operator switched to it, even when a new run was already running and
    waiting for an input.

Everything here is offscreen and against real sessions; the two pixel checks grab the widget
itself, so they read what a person sees rather than what a role holds.
"""

import sqlite3
import time

import json

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor, QFontMetrics
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QPushButton

from ate.app import launch
from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.storage.store import RunStore
from ate.ui.run_widgets import (
    STEP_DETAIL_ROLE,
    STEP_EMPHASIS_ROLE,
    STEP_RESULT_ROLE,
    FailurePromptBar,
    RunStateChip,
    StepRowDelegate,
    load_schedule_steps,
)
from ate.ui.staged_run_screen import StagedRunScreen
from ate.ui.theme import (
    BADGE_GLYPH_GAP,
    BADGE_PAD_LEFT,
    BADGE_PAD_RIGHT,
    BADGE_WORD_PX,
    CHIP_STYLES,
    TOKENS,
    badge_side,
    badge_width_for,
    badge_word_px,
    contrast,
    ui_font,
)
from ate.ui.three_pane_run_screen import ThreePaneRunScreen

from ._fixtures import add_long_schedule, write_ate_env

#: The screen size the review measured against: a Station on a netbook.
NETBOOK = (1366, 768)


def _find_item(list_widget, substring):
    """Return the row whose text contains ``substring`` (raising if none match)."""
    for i in range(list_widget.count()):
        if substring in list_widget.item(i).text():
            return list_widget.item(i)
    raise AssertionError(f"no schedule row containing {substring!r}")


def _select_and_start(qapp, screen, schedule_name: str) -> None:
    """Select ``schedule_name`` and start a run through the Steps & options stage."""
    item = _find_item(screen.test_schedule_stage.schedule_list, schedule_name)
    screen.test_schedule_stage.schedule_list.setCurrentItem(item)
    screen._begin_run_from_selection()
    assert screen.steps_stack.currentIndex() == 2
    screen.steps_options_stage.run_button.click()


def _wait_finished(qapp, session) -> bool:
    """Pump the event loop until the session reaches FINISHED (or time out).

    A Stop-on-Fail pause blocks the worker until someone answers the prompt, so a test driving a
    failing schedule answers Continue while pumping, as the other headless drivers do."""
    session.failure_prompt.connect(lambda *_seen: session.choose("CONTINUE"))
    deadline = time.monotonic() + 20.0  # a time bound, not a pump count: load-independent
    while time.monotonic() < deadline:
        qapp.processEvents()
        if session.state == SessionState.FINISHED:
            return True
        time.sleep(0.005)
    return False


def _count_pixels(image, color: str) -> int:
    """How many pixels of ``image`` are exactly ``color``.

    The painted rows and badges are the only place these token colours appear in the widget, so
    a count is enough to say whether the delegate drew them."""
    target = QColor(color).rgb()
    return sum(
        1 for y in range(image.height()) for x in range(image.width()) if image.pixel(x, y) == target
    )


def test_painted_step_rows_draw_their_fill_and_their_badge(qapp):
    """Defect 1: the delegates paint, and a row with no background of its own is not left unpainted.

    `_row_fill` reads the item's own brush; before the fix it asked the brush for
    `brushStyle()` — a method PySide6 does not have — and the resulting AttributeError left the
    whole step list undrawn, badges included."""
    list_widget = QListWidget()
    list_widget.setItemDelegate(StepRowDelegate())
    tinted = QListWidgetItem("1. LED colour")
    tinted.setData(STEP_RESULT_ROLE, "PASS")
    tinted.setBackground(QColor(TOKENS["primary_tint"]))
    list_widget.addItem(tinted)
    current = QListWidgetItem("2. Fan speed")
    current.setData(STEP_EMPHASIS_ROLE, "current")
    list_widget.addItem(current)
    list_widget.resize(320, 130)
    list_widget.show()
    qapp.processEvents()

    image = list_widget.grab().toImage()
    origin = list_widget.viewport().pos()

    def inside(item):
        row = list_widget.visualItemRect(item)
        return [
            (origin.x() + x, origin.y() + y)
            for y in range(row.top() + 2, row.bottom() - 2)
            for x in range(row.left() + 6, row.right() - 2)
        ]

    tint = QColor(TOKENS["primary_tint"]).rgb()
    painted = sum(1 for x, y in inside(tinted) if image.pixel(x, y) == tint)
    assert painted > len(inside(tinted)) // 2, "the tinted step row drew no background"

    row = list_widget.visualItemRect(current)
    bar = QColor(TOKENS["primary"]).rgb()
    left_bar = sum(
        1 for y in range(row.top() + 2, row.bottom() - 2)
        for x in range(row.left(), row.left() + 5)
        if image.pixel(origin.x() + x, origin.y() + y) == bar
    )
    assert left_bar > 20, "the emphasis bar was not drawn"
    assert _count_pixels(image, TOKENS["pass_fill"]) > 20, "the PASS badge was not painted"


def test_the_three_pane_result_column_paints_its_badge(qapp, tmp_path):
    """Defect 1, three-pane side: `ResultBadgeDelegate` paints the cell, and the plain cells keep
    Fusion's ordinary drawing."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = ThreePaneRunScreen(session)
    screen.resize(900, 600)
    screen.show()
    qapp.processEvents()
    screen.table.load_steps(load_schedule_steps(session, "Logging schedule"))
    screen.table.show_state(
        [{"node_seq": 0, "step_number": "1.", "step_name": "logging", "kind": "step", "result": "PASS", "attempt_count": 1}],
        [{"node_seq": 0, "attempt_no": 1, "actual": "", "expected": "", "msg": "ok"}],
    )
    qapp.processEvents()
    image = screen.table.table.viewport().grab().toImage()
    assert _count_pixels(image, TOKENS["pass_fill"]) > 20, "the Result cell drew no badge"
    assert _count_pixels(image, TOKENS["pass_ink"]) > 10, "the badge drew no word or glyph"


def test_badge_words_fit_their_badges(qapp):
    """Defect 7: the width a Result word needs is measured from the font, so no badge drops it.

    "NOREPORT" needed 103 px and the badge was 72 px, so the word was never drawn and the badge
    read as a bare shape."""
    for word in ("PASS", "FAIL", "ERROR", "NOTRUN", "NOREPORT", "FORCED_NOTRUN"):
        width = badge_width_for(word)
        advance = QFontMetrics(ui_font(BADGE_WORD_PX, bold=True)).horizontalAdvance(word)
        assert width >= advance + 4 + 6 + 4, f"{word} needs {advance} px in a {width} px badge"
        assert badge_word_px(word, width) == BADGE_WORD_PX, f"{word} does not fit the width it asks for"
        # A badge narrower than the natural size draws the word smaller, and drops it only when
        # even the minimum size cannot fit.
        assert badge_word_px(word, advance - 1) < BADGE_WORD_PX, f"{word} is drawn at full size in a narrower badge"
        assert badge_word_px(word, 6) == 0, f"{word} was drawn in a 6 px badge"


def test_the_result_column_widens_for_the_widest_result_the_run_shows(qapp, tmp_path):
    """Defect 7, three-pane side: the Result column is sized to the widest Result in the table.

    78 px is enough for PASS but not for NOREPORT or FORCED_NOTRUN, and the delegate then had
    nowhere to put the word."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = ThreePaneRunScreen(session)
    screen.show()
    qapp.processEvents()
    screen.table.load_steps(load_schedule_steps(session, "Logging schedule"))
    nodes = [
        {"node_seq": 0, "step_number": "1.", "step_name": "silent", "kind": "step", "result": "NOREPORT", "attempt_count": 1},
        {"node_seq": 1, "step_number": "2.", "step_name": "skipped", "kind": "step", "result": "FORCED_NOTRUN", "attempt_count": 1},
    ]
    attempts = [{"node_seq": seq, "attempt_no": 1, "actual": "", "expected": "", "msg": ""} for seq in (0, 1)]
    screen.table.show_state(nodes, attempts)

    width = screen.table.table.columnWidth(2)
    assert width >= badge_width_for("FORCED_NOTRUN"), f"a {width} px column cannot draw FORCED_NOTRUN"
    assert badge_word_px("FORCED_NOTRUN", width - 8) == BADGE_WORD_PX, "the widest word is drawn too small"


def test_a_live_refresh_keeps_the_steps_the_run_has_not_reached(qapp, tmp_path):
    """Defect 2: while the run is up, every step of the loaded schedule keeps its row — and its
    Step Input widget stays attached to it.

    "Abortable two-input schedule" waits on an INTEGER in step 1 with steps 2 and 3 behind it.
    A refresh built from `store.nodes()` alone had one row, and the widget for step 2 went with
    the rows it deleted: the Operator could never answer it."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    screen.show()
    qapp.processEvents()
    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    asked: list[tuple[int, int]] = []
    session.input_asked.connect(lambda seq, attempt_no, index: asked.append((seq, index)))
    _select_and_start(qapp, screen, "Abortable two-input schedule")

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not asked:
        qapp.processEvents()
    assert asked, "the run never reached its first blocking input"

    step_list = screen.run_view.step_list
    texts = [step_list.item(i).text() for i in range(step_list.count())]
    assert any(text.startswith("2.") for text in texts), texts
    assert any(text.startswith("3.") for text in texts), texts
    future = screen.run_view.input_widget(1, 1)
    assert future is not None, "the future step has no input widget"
    assert step_list.itemWidget(future.input_item) is future, "the widget is not on its row"
    assert future.isVisible(), "the future step's input row is not drawn"

    def _answer(seq: int, value: str) -> None:
        """Answer a waiting input through its widget, the way an Operator would."""
        widget = screen.run_view.input_widget(seq, 1)
        widget.line_edit.setText(value)
        widget.ok_button.click()

    _answer(asked[0][0], "12")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and (1, 1) not in asked:
        qapp.processEvents()
    assert (1, 1) in asked, "the run never reached the step whose widget survived the refresh"
    _answer(1, "34")
    assert _wait_finished(qapp, session), "the run did not finish"
    results = [step_list.item(i).data(STEP_RESULT_ROLE) for i in range(step_list.count())]
    assert results.count("PASS") == 3, results


def test_the_summary_label_is_inside_its_input_widget(qapp, tmp_path):
    """Defect 3: the one-line summary of a future step belongs to the widget, not to the screen.

    The label had no parent and was never added to the widget's layout, so `set_summary(True)` on
    a collapsed future input showed a separate window next to the Run view, and the row kept the
    height of whichever state it was first given."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    screen.show()
    qapp.processEvents()
    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    _select_and_start(qapp, screen, "Abortable two-input schedule")
    qapp.processEvents()

    widget = screen.run_view.input_widget(1, 1)
    assert widget is not None
    assert widget.summary_label.parent() is widget, "the summary label is a window of its own"
    layout = widget.layout()
    assert any(widget.summary_label is layout.itemAt(i).widget() for i in range(layout.count())), (
        "the summary label is not in the widget's layout"
    )
    assert widget.summary_label.isVisible() is True, "a collapsed input shows no summary"

    collapsed = widget.sizeHint().height()
    widget.set_summary(False)
    expanded = widget.sizeHint().height()
    assert expanded > collapsed, f"the row does not grow when the input opens: {collapsed} -> {expanded}"
    assert widget.input_item.sizeHint().height() >= expanded, "the row keeps the collapsed height"
    assert widget.summary_label.wordWrap(), "a long description runs out of the row"


def test_closing_the_window_stops_the_header_timer_before_the_store_closes(qapp, tmp_path):
    """Defect 4: MainWindow releases the session's store, and after that nothing may read it.

    The one-second header timer kept running, so a tick landed after `session.close()` and
    `store.get_test_run` raised `sqlite3.ProgrammingError: Cannot operate on a closed database`
    inside the Qt event loop."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    alerts: list[str] = []
    outcome = launch(["--config", str(env["config"])], exec_=False, prompt=lambda path: True, alert=alerts.append)
    window = outcome.window
    screen = window.run_screen
    assert screen._timer.isActive(), "the header timer runs while the window is open"

    window.close()
    qapp.processEvents()
    assert screen._timer.isActive() is False, "the header timer outlived the window"
    assert screen._released is True
    assert window.debug_window.isVisible() is False, "the Debug window outlived the window that owns it"

    # The store really is closed, and the timer's own slot still does nothing when called.
    try:
        window.session.store.get_test_run("no-such-run")
    except sqlite3.ProgrammingError:
        pass  # closed: exactly what the guards below have to survive
    else:
        raise AssertionError("the session's store was still open after the window closed")
    screen._update_header()
    screen._sync_from_store()
    screen._refresh_log()
    qapp.processEvents()


def test_the_window_still_fits_a_1366x768_screen(qapp, tmp_path):
    """Defect 5: only the Run buttons are bench-sized, so the window's floor stays under a netbook.

    `min-height: 32px` on every `QPushButton` made History's buttons 46 px tall; the Run screens
    and the History screen share one `QStackedWidget`, so the whole window's minimum height rose
    to 887 px."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    alerts: list[str] = []
    outcome = launch(["--config", str(env["config"])], exec_=False, prompt=lambda path: True, alert=alerts.append)
    window = outcome.window
    hint = window.minimumSizeHint()
    assert hint.width() <= NETBOOK[0] and hint.height() <= NETBOOK[1], f"the window needs {hint.width()}x{hint.height()}"

    run_buttons = [b for b in window.findChildren(QPushButton) if b.objectName() in ("primary", "danger")]
    assert run_buttons, "no Run button carries the primary/danger name"
    for button in run_buttons:
        assert button.sizeHint().height() >= 32, f"{button.text()!r} is {button.sizeHint().height()} px tall"

    # Everything else keeps Fusion's compact height: it is History's 46 px buttons that made the
    # shared QStackedWidget 887 px tall, and the window's minimum is the tallest of the two.
    history_buttons = window.history_screen.findChildren(QPushButton)
    assert history_buttons, "the History screen has no buttons"
    assert all(b.sizeHint().height() < 32 for b in history_buttons), "History's buttons are bench-sized"


def test_a_finished_refresh_appends_to_the_log_it_was_reading(qapp, tmp_path):
    """Defect 6: the finished run's refresh adds the lines the live refresh had not reached.

    Clearing the panes threw the Operator back to the top of the log they were reading, and the
    Debug window is shared by both layouts, so clearing it reset the other layout's reading
    position too. The log here is long enough that the pane actually scrolls, which is the only
    case where "rewriting the pane" and "appending to it" differ on screen."""
    env = write_ate_env(tmp_path)
    add_long_schedule(tmp_path, steps=40, fail_at=10)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    screen.show()
    qapp.processEvents()
    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    _select_and_start(qapp, screen, "Long 60 schedule")
    assert _wait_finished(qapp, session)

    log_pane = screen.run_view.log_pane
    bar = log_pane.text_widget().verticalScrollBar()
    assert bar.maximum() > 0, "the log is too short for this to say anything"
    bar.setValue(bar.maximum() // 2)  # the Operator is in the middle of the log
    where = bar.value()
    before = log_pane.rendered_lines()

    screen.run_view.refresh(
        session.store.nodes(session.run_uuid),
        session.store.attempts(session.run_uuid),
        session.store.log_lines(session.run_uuid),
    )
    assert log_pane.rendered_lines() == before, "the finished refresh rewrote the Log pane"
    assert bar.value() == where, f"the finished refresh moved the reader from {where} to {bar.value()}"
    assert screen.run_view.debug_window.rendered_lines(), "the shared Debug window was emptied"
def _readable_ink(image, origin, rect):
    """The ink a painted row actually draws, and its contrast against the row's fill.

    The row's own background is the most common colour in the text area; the ink is the
    most common colour that is not it. Anti-aliasing leaves only a handful of pixels of each
    blend, so a colour with ten or more pixels is a glyph, not an edge."""
    counts: dict[str, int] = {}
    for y in range(rect.top() + 2, rect.bottom() - 2):
        for x in range(rect.left() + 6, rect.left() + 90):
            colour = QColor(image.pixel(origin.x() + x, origin.y() + y)).name()
            counts[colour] = counts.get(colour, 0) + 1
    fill = max(counts, key=counts.get)
    inks = {colour: count for colour, count in counts.items() if colour != fill and count >= 10}
    if not inks:
        return fill, fill, 1.0
    ink = max(inks, key=lambda colour: contrast(colour, fill))
    return fill, ink, contrast(ink, fill)


def test_a_selected_step_row_is_readable_in_every_state(qapp):
    """Defect 8: the ink is chosen from the background actually painted, not from the row's state.

    `StepRowDelegate.paint` filled an emphasis row with its pale tint and then took the pen from
    the selection flag, so a selected current row read white on `#dbe8fb` (1.23:1) and a selected
    failed row white on `#fde8e6` (1.24:1) — the step's name was invisible. A selected plain row,
    filled solid primary, drew its Actual/Expected line in the muted ink (1.01:1)."""
    list_widget = QListWidget()
    list_widget.setItemDelegate(StepRowDelegate())
    plain = QListWidgetItem("1. LED colour")
    plain.setData(STEP_RESULT_ROLE, "PASS")
    plain.setData(STEP_DETAIL_ROLE, "Actual Green · Expected Green")
    current = QListWidgetItem("2. Fan speed")
    current.setData(STEP_EMPHASIS_ROLE, "current")
    current.setData(STEP_DETAIL_ROLE, "Actual 3 · Expected 4")
    failed = QListWidgetItem("3. Relay click")
    failed.setData(STEP_EMPHASIS_ROLE, "failed")
    failed.setData(STEP_RESULT_ROLE, "FAIL")
    for item in (plain, current, failed):
        # The height the Run view gives a step row: 30 px, or 38 px when it carries the
        # Actual/Expected line (`RunView._render_rows` sets exactly these hints).
        item.setSizeHint(QSize(0, 38 if item.data(STEP_DETAIL_ROLE) else 30))
        list_widget.addItem(item)
    list_widget.resize(320, 160)
    list_widget.show()
    qapp.processEvents()

    for item in (plain, current, failed):
        list_widget.setCurrentItem(item)
        qapp.processEvents()
        image = list_widget.grab().toImage()
        origin = list_widget.viewport().pos()
        fill, ink, ratio = _readable_ink(image, origin, list_widget.visualItemRect(item))
        assert ratio >= 4.5, f"{item.text()!r}: {ink} on {fill} is {ratio:.2f}:1"
        # A pale tint never carries white ink, whatever the selection state.
        if fill in (TOKENS["primary_tint"], TOKENS["failed_tint"]):
            assert ink == TOKENS["ink"], f"{item.text()!r} draws {ink} on the pale {fill}"
        else:
            assert fill == TOKENS["primary"], f"{item.text()!r} was not filled with the selection colour"
            assert ink == TOKENS["white"], f"{item.text()!r} draws {ink} on solid {fill}"


def _chip_word_width(chip) -> tuple[int, int]:
    """The width the chip's word needs, and the width the chip actually has."""
    advance = QFontMetrics(ui_font(RunStateChip.WORD_PX, bold=True)).horizontalAdvance(chip.state_word())
    needed = BADGE_PAD_LEFT + badge_side(chip.height()) + BADGE_GLYPH_GAP + advance + BADGE_PAD_RIGHT
    return needed, chip.width()


def _chip_word_reaches(chip) -> int:
    """How far right the chip's word reaches in its own pixels.

    The glyph sits in the 4..(4+side) box; anything painted beyond it is the word. A chip too
    narrow for the word draws the glyph only, so the extent is the direct measure of "the word
    is on the chip"."""
    fill, ink, _ = CHIP_STYLES[chip.state_kind()]
    target = QColor(ink).rgb()
    image = chip.grab().toImage()
    return max(
        (x for y in range(image.height()) for x in range(image.width()) if image.pixel(x, y) == target),
        default=-1,
    )


def test_the_state_chip_makes_room_for_its_state_word(qapp, tmp_path):
    """Defect 9: changing the chip's state invalidates its geometry, in both layouts.

    `set_state` called `update()`, which repaints but tells no layout anything, so a header that
    began at READY kept the chip at its 140 px hint while the run needed 223 px for "INPUT NEEDED",
    and the badge drew the 22 px word squeezed or dropped."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    for layout, screen in (("staged", StagedRunScreen(session)), ("three-pane", ThreePaneRunScreen(session))):
        screen.resize(1280, 800)
        screen.show()
        qapp.processEvents()
        chip = screen.run_header.state_chip

        chip.set_state("ready", "READY")
        qapp.processEvents()
        needed, width = _chip_word_width(chip)
        assert width >= needed, f"{layout}: READY needs {needed} px, the chip has {width}"

        chip.set_state("input", "INPUT NEEDED")
        qapp.processEvents()
        needed, width = _chip_word_width(chip)
        assert width >= needed, f"{layout}: INPUT NEEDED needs {needed} px, the chip stayed {width}"
        # The word is drawn at the chip's own 22 px, not shrunk to fit a stale 140 px chip: the
        # ink reaches past the glyph box, which only happens when the word is painted.
        glyph_right = BADGE_PAD_LEFT + badge_side(chip.height()) + BADGE_GLYPH_GAP
        assert _chip_word_reaches(chip) > glyph_right + 40, (
            f"{layout}: INPUT NEEDED is not painted in a {width} px chip"
        )
        assert badge_word_px(
            "INPUT NEEDED",
            width - BADGE_PAD_LEFT - badge_side(chip.height()) - BADGE_GLYPH_GAP - BADGE_PAD_RIGHT,
            RunStateChip.WORD_PX,
        ) == RunStateChip.WORD_PX, f"{layout}: INPUT NEEDED is drawn smaller than 22 px in a {width} px chip"

        # And back again: a shorter word does not leave the header holding the wider chip.
        chip.set_state("pass", "PASS")
        qapp.processEvents()
        needed, width = _chip_word_width(chip)
        assert width >= needed, f"{layout}: PASS needs {needed} px, the chip has {width}"
        assert chip.sizeHint().width() == max(
            140, QFontMetrics(ui_font(RunStateChip.WORD_PX, bold=True)).horizontalAdvance("PASS") + 40
        )
    session.close()


def _layout_config(tmp_path, layout: str):
    """The fixture config with `ui.layout` set to one of the two Run layouts, and its store open."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    config = tmp_path / "config.json"
    data = json.loads(env["config"].read_text(encoding="utf-8"))
    data.setdefault("ui", {})["layout"] = layout
    config.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    return config, env


def test_the_prompt_bar_has_three_buttons_of_the_same_width(qapp, tmp_path):
    """Defect 10: Continue, Retry and Abort measure the same width, with Abort 16 px apart.

    Each button took its own size hint — 106 px for "Continue", 78 px for "Retry", 79 px for
    "Abort" — so the bar read as one wide button and two narrow ones. D6 asks for three equal
    buttons, and the fix is measured here off the widgets in the bar as it is actually laid out."""
    config, _env = _layout_config(tmp_path, "staged")
    window = launch(["--config", str(config)], exec_=False, prompt=lambda path: True, alert=[]).window
    window.resize(1280, 800)
    window.show()
    qapp.processEvents()
    bar = window.staged_screen.run_view.failure_prompt_bar
    bar.show_prompt("1.", "FAIL", "wrong colour", actual="Red", expected="Green")
    qapp.processEvents()

    buttons = (bar.continue_button, bar.retry_button, bar.abort_button)
    widths = [button.width() for button in buttons]
    assert len(set(widths)) == 1, f"the prompt buttons are different widths: {widths}"
    assert min(widths) >= 80, f"the prompt buttons are {widths} px wide: too narrow to read"
    # Every word still fits its button: the width is the widest word's own measurement.
    for button in buttons:
        advance = QFontMetrics(ui_font(15)).horizontalAdvance(button.text())
        assert button.width() > advance, f"{button.text()!r} needs {advance} px in a {button.width()} px button"

    gaps = [
        bar.retry_button.x() - (bar.continue_button.x() + bar.continue_button.width()),
        bar.abort_button.x() - (bar.retry_button.x() + bar.retry_button.width()),
    ]
    assert gaps[1] - gaps[0] == 16, f"Abort is set {gaps[1]} px apart, its peers {gaps[0]} px"
    assert all(button.autoDefault() is False for button in buttons), "a prompt button is the Enter default"
    assert all(button.sizeHint().height() >= 32 for button in buttons), "the prompt buttons are not bench-sized"
    window.close()
    qapp.processEvents()


def test_a_pause_keeps_the_failed_step_badge_across_a_layout_switch(qapp, tmp_path):
    """Defect 11: a paused step keeps its Result badge when the Operator switches layouts mid-pause.

    The engine writes a step's node row only after the Operator answers the Stop-on-Fail prompt, so
    the staged view's row builder — which overlays the store's rows on the loaded schedule — had
    nothing to read for the paused step. Switching to the three-pane view and back ran
    `_sync_from_store`, which rebuilt the rows from the store and drew the failed step with no
    badge, while the prompt bar still said `Step 1. FAIL`."""
    config, _env = _layout_config(tmp_path, "staged")
    window = launch(["--config", str(config)], exec_=False, prompt=lambda path: True, alert=[]).window
    window.resize(1280, 800)
    window.show()
    qapp.processEvents()
    staged = window.staged_screen
    asked_pause = []
    staged.session.failure_prompt.connect(lambda seq, attempt: asked_pause.append((seq, attempt)))

    item = _find_item(staged.test_schedule_stage.schedule_list, "Fail then pass schedule")
    staged.test_schedule_stage.schedule_list.setCurrentItem(item)
    staged._begin_run_from_selection()
    staged.steps_options_stage.run_button.click()

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and not asked_pause:
        qapp.processEvents()
    assert asked_pause, "the run never paused on a failure"

    step_row = _find_item(staged.run_view.step_list, "1.")
    assert step_row.data(STEP_RESULT_ROLE) == "FAIL", "the paused step lost its Result before the switch"

    window.set_layout("threePane")
    qapp.processEvents()
    window.set_layout("staged")
    qapp.processEvents()

    step_row = _find_item(staged.run_view.step_list, "1.")
    assert step_row.data(STEP_RESULT_ROLE) == "FAIL", "the switch to the other layout dropped the paused step's badge"
    detail = step_row.data(STEP_DETAIL_ROLE) or ""
    assert "Red" in detail and "Green" in detail, f"the paused row lost its measurements: {detail!r}"
    assert step_row.data(STEP_EMPHASIS_ROLE) == "failed", "the paused row lost its emphasis"
    assert staged.run_view.failure_prompt_bar.isVisible(), "the prompt bar hid itself while the pause was still up"

    # Answering the pause clears the overlay, so a finished run reads from the store as before.
    staged.session.choose("CONTINUE")
    assert _wait_finished(qapp, staged.session), "the run did not finish after the choice"
    step_row = _find_item(staged.run_view.step_list, "1.")
    assert step_row.data(STEP_RESULT_ROLE) == "FAIL"
    window.close()
    qapp.processEvents()


def test_a_new_run_replaces_the_deferred_finished_run(qapp, tmp_path):
    """Defect 12: a deferred read of a finished run does not survive the run it came from.

    The three-pane layout finishes a run while the staged layout is hidden, so the staged layout
    defers that run's rows. Starting a second run — which blocks on a Step Input — and then
    switching to the staged layout drew the *first* run: the header read PASS, the step rows carried
    the finished run's results, and `RunView.refresh` deactivated every Step Input, so the input
    the current run was waiting on could not be answered."""
    config, _env = _layout_config(tmp_path, "threePane")
    window = launch(["--config", str(config)], exec_=False, prompt=lambda path: True, alert=[]).window
    window.resize(1280, 800)
    window.show()
    qapp.processEvents()
    three = window.three_pane_screen
    staged = window.staged_screen

    def pick(name: str) -> None:
        picker = three.picker.schedule_list
        for index in range(picker.count()):
            if picker.item(index).data(Qt.ItemDataRole.UserRole) == name:
                picker.setCurrentItem(picker.item(index))
                return
        raise AssertionError(f"no schedule named {name!r} in the picker")

    pick("Logging schedule")
    three.fields.operator_edit.setText("Ada")
    three.fields.project_edit.setText("Packet 39")
    three.start_button.click()
    assert _wait_finished(qapp, three.session), "the first run did not finish"
    first_run = three.session.run_uuid
    assert first_run is not None
    assert staged._deferred_run_uuid == first_run, "the hidden layout did not defer the finished run"

    three.new_run_button.click()
    qapp.processEvents()
    asked: list[tuple[int, int]] = []
    three.session.input_asked.connect(lambda seq, attempt, index: asked.append((seq, index)))
    pick("Abortable two-input schedule")
    three.start_button.click()

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and not asked:
        qapp.processEvents()
    assert asked, "the second run never reached its blocking input"
    second_run = three.session.run_uuid
    assert second_run != first_run, "the second run did not start"

    window.set_layout("staged")
    qapp.processEvents()

    assert staged._deferred_run_uuid is None, "the staged layout still holds the finished run to draw"
    assert staged.session.run_uuid == second_run
    step_list = staged.run_view.step_list
    results = [step_list.item(i).data(STEP_RESULT_ROLE) for i in range(step_list.count())]
    assert results.count("PASS") == 0, f"the staged view drew the previous run's results: {results}"
    assert staged.run_header.state_chip.state_word() == "INPUT NEEDED", (
        f"the header reads {staged.run_header.state_chip.state_word()!r} while an input is waiting"
    )
    widget = staged.run_view.input_widget(0, 1)
    assert widget is not None, "the staged layout has no widget for the input the run is waiting on"
    assert widget.line_edit.isEnabled(), "the current run's waiting input is disabled"

    widget.line_edit.setText("12")
    widget.ok_button.click()
    qapp.processEvents()
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and (1, 1) not in asked:
        qapp.processEvents()
    assert (1, 1) in asked, "answering the staged layout's input did not let the run go on"
    _wait_finished(qapp, staged.session)
    window.close()
    qapp.processEvents()
