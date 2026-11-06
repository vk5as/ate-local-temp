"""Packet 40b — long and multiline Actual/Expected readable everywhere.

GitHub issue #3: "The entries in the central table still do not wrap text or expand. Also
should not be editable", and "Stage layout only shows single line of multiline reported info
… actual / expected results can be long and / or multiline".

The three-pane centre table word-wraps its text cells, sizes each step row to the text it
actually holds (and shrinks it back when a column widens), and closes itself to editing
entirely: no double-click, F2 or typing opens an editor. The staged `StepRowDelegate` paints
every line of a multiline Actual/Expected detail, wrapping long ones, and the row's size hint
grows to match. Short single-line rows keep the height they had before the packet (30 px in
the table, 30 px without a detail line and 38 px with one in the staged list). The failure
prompt grows to the height its wrapped message needs. The History screen and the Debug window
were checked for the same truncation.

Nothing here measures the code against itself: a cell is judged by the ink it paints, compared
with the same delegate given room to spare, or with Qt's own wrap of the same text at the same
width.

Review screenshots are opt-in: set ``ATE_40B_SHOTS`` to a directory to write them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import shiboken6
from PySide6.QtCore import QEvent, QObject, QPointF, Qt, QRect, QRectF
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QMouseEvent, QTextLayout, QTextOption
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QPlainTextEdit,
    QStyle,
    QStyleOptionViewItem,
    QStyleOptionButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ate.history.session import HistorySession
from ate.run_session.headless import HeadlessRunSession
from ate.storage.store import RunStore
from ate.ui.history_screen import HistoryScreen
from ate.ui.run_widgets import (
    DebugWindow,
    FailurePromptBar,
    STEP_DETAIL_ROLE,
    StepRowDelegate,
    WrappedTextDelegate,
    load_schedule_steps,
)
from ate.ui.staged_run_screen import StagedRunScreen
from ate.ui.theme import STYLESHEET, TOKENS
from ate.ui.three_pane_run_screen import (
    SHORT_STEP_ROW_HEIGHT,
    _INLINE_PROMPT_VIEWPORT_INSET,
    ThreePaneRunScreen,
)

from ._fixtures import write_ate_env

#: ~300 characters on one line, as the issue's "actual / expected results can be long" asks.
LONG_ACTUAL = (
    "the output voltage measured on channel two during the warm-up window drifts below the "
    "nominal band and the deviation grows with every sweep of the load until the reading never "
    "settles inside the pass band recorded against the golden unit on this bench at twenty "
    "degrees"
)
#: Four embedded newlines: the issue's "multiline reported info" the stage layout clipped.
EXPECTED_LINES = (
    "4.80V .. 5.20V nominal\n"
    "channel 1 within 50mV of the mean\n"
    "channel 2 within 50mV of the mean\n"
    "channel 3 within 50mV of the mean"
)
#: A value with no spaces at all — a hex dump, a device id, a path. Word wrap alone leaves it
#: as one line running out of the cell (40b review).
UNBREAKABLE = "0xDEADBEEF" * 38


def _three_pane_screen(qapp, tmp_path, width: int = 900) -> ThreePaneRunScreen:
    """A shown three-pane screen whose step table holds the Logging schedule's rows."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = ThreePaneRunScreen(session)
    screen.resize(width, 600)
    screen.show()
    qapp.processEvents()
    screen.table.load_steps(load_schedule_steps(session, "Logging schedule"))
    return screen


def _staged_screen(qapp, tmp_path, width: int = 900) -> StagedRunScreen:
    """A shown staged screen with the Run view revealed, so the step list has a real width."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    screen.resize(width, 700)
    screen.show()
    qapp.processEvents()
    screen._reveal_run_view()
    qapp.processEvents()
    screen.run_view.load_run_steps(load_schedule_steps(session, "Logging schedule"))
    return screen


def _long_state() -> tuple[list[dict], list[dict]]:
    """Two steps: one with the ~300-character Actual and the 4-line Expected, one short."""
    nodes = [
        {"node_seq": 0, "step_number": "1.", "step_name": "long one", "kind": "step",
         "result": "FAIL", "attempt_count": 1},
        {"node_seq": 1, "step_number": "2.", "step_name": "short one", "kind": "step",
         "result": "PASS", "attempt_count": 1},
    ]
    attempts = [
        {"node_seq": 0, "attempt_no": 1, "actual": LONG_ACTUAL, "expected": EXPECTED_LINES,
         "msg": "the rail did not settle"},
        {"node_seq": 1, "attempt_no": 1, "actual": "ok", "expected": "ok", "msg": "ok"},
    ]
    return nodes, attempts


def _ink(image: QImage) -> int:
    """The pixels of ``image`` carrying ink — the more of the value painted, the more ink."""
    count = 0
    for y in range(image.height()):
        for x in range(image.width()):
            colour = image.pixelColor(x, y)
            if 0.299 * colour.red() + 0.587 * colour.green() + 0.114 * colour.blue() < 180:
                count += 1
    return count


def _ink_positions(image: QImage, rect: QRect) -> set[tuple[int, int]]:
    """Which pixels inside ``rect`` carry ink — where the ink sits, not how much of it.

    Two renderings of the same value with the lines broken in different places have the same
    ink count and different ink positions, which is how a word cut in half is caught."""
    return {
        (x, y)
        for y in range(rect.top(), rect.bottom() + 1)
        for x in range(rect.left(), rect.right() + 1)
        if 0.299 * image.pixelColor(x, y).red()
        + 0.587 * image.pixelColor(x, y).green()
        + 0.114 * image.pixelColor(x, y).blue() < 180
    }


def _paint(delegate, index, width: int, height: int, widget) -> QImage:
    """What the delegate draws for ``index`` in a cell of ``width`` x ``height``."""
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, max(1, width), max(1, height))
    option.palette = widget.palette()
    option.widget = widget
    option.state = QStyle.StateFlag.State_Enabled
    option.font = index.data(Qt.ItemDataRole.FontRole) or widget.font()
    option.text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
    image = QImage(max(1, width), max(1, height), QImage.Format_RGB32)
    image.fill(Qt.white)
    painter = QPainter(image)
    delegate.paint(painter, option, index)
    painter.end()
    return image


def _cell_ink(table, row: int, column: int, height: int | None = None) -> int:
    """Ink the table paints in a cell, at the width Qt paints it and the given row height.

    Qt hands the delegate `visualRect(index)`, one grid line narrower than `columnWidth()`:
    measuring at the wider number counts ink the cell never shows (40b review, second round)."""
    delegate = table.itemDelegateForColumn(column) or table.itemDelegate()
    index = table.model().index(row, column)
    width = table.visualRect(index).width()
    return _ink(_paint(delegate, index, width, table.rowHeight(row) if height is None else height, table))


def _cell_text_rect(table, row: int, column: int) -> QRect:
    """Where the delegate draws a cell's text, in the coordinates of the image `_paint` makes:
    the cell Qt paints, inset by the style's margins."""
    margin = WrappedTextDelegate.TEXT_MARGIN
    rect = table.visualRect(table.model().index(row, column))
    return QRect(margin, margin, max(1, rect.width() - 2 * margin), max(1, rect.height() - 2 * margin))


def _cell_text_width(table, row: int, column: int) -> int:
    """The room the delegate has for text in this cell: the cell Qt paints, less its margins."""
    return _cell_text_rect(table, row, column).width()


def _reference_lines(text: str, font, width: int) -> list[str]:
    """The lines a cell ``width`` px wide can hold: broken at a space, and inside a word only
    when that word by itself is wider than the cell.

    This is the rule the 40b review asks for. Qt's `TextWordWrap` alone leaves a token with no
    space in it as one line running out of the cell, so the over-long tokens are cut where the
    column ends them; nothing else is ever cut inside a word."""
    metrics = QFontMetrics(font)
    width = max(1, width)
    lines = []
    for chunk in text.splitlines() or [""]:
        line = ""
        for word in chunk.split():
            if line and metrics.horizontalAdvance(f"{line} {word}") <= width:
                line = f"{line} {word}"
                continue
            if line:
                lines.append(line)
                line = ""
            while len(word) > 1 and metrics.horizontalAdvance(word) > width:
                lo, hi = 1, len(word)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if metrics.horizontalAdvance(word[:mid]) <= width:
                        lo = mid
                    else:
                        hi = mid - 1
                lines.append(word[:lo])
                word = word[lo:]
            line = word
        lines.append(line)
    return lines


def _paint_text(font, text: str, width: int, height: int, pen: QColor | None = None, *, wrap: bool = True) -> QImage:
    """Qt painting ``text`` with ordinary word wrap in a ``width`` x ``height`` box: the
    reference a cell is compared with.

    ``pen`` is the ink the widget draws text in. A reference drawn in black against a cell
    drawn in the palette's ink disagrees at every anti-aliased glyph edge — 332 pixels of
    "wrong place" for one line of text — so the comparison has to be made in the same colour
    (40b review, second round)."""
    image = QImage(max(1, width), max(1, height), QImage.Format_RGB32)
    image.fill(Qt.white)
    painter = QPainter(image)
    painter.setFont(font)
    if pen is not None:
        painter.setPen(pen)
    painter.drawText(QRect(0, 0, max(1, width), max(1, height)),
                     Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
                     | (Qt.TextFlag.TextWordWrap if wrap else Qt.TextFlag(0)), text)
    painter.end()
    return image


def _reference_ink(text: str, font, width: int) -> int:
    """Ink the word-wrap rule above paints for ``text`` in ``width`` px, tall enough for every
    line it needs, so nothing the cell has to show is left out of the reference."""
    return _ink(_paint_text(font, "\n".join(_reference_lines(text, font, width)), width, 3000))


def _qt_wrap_height(text: str, font, width: int) -> int:
    """How tall ``text`` is when wrapped into ``width`` px with the rule above — Qt measuring,
    not our code."""
    return QFontMetrics(font).boundingRect(
        QRect(0, 0, max(1, width), 3000),
        Qt.TextFlag.TextWordWrap,
        "\n".join(_reference_lines(text, font, width))
    ).height()


def _line_blocks(image: QImage, rect: QRect) -> list[tuple[int, int, int, int]]:
    """Each painted line of ``image`` inside ``rect`` as (top, bottom, first ink column, last).

    Lines of text are separated by rows with no ink in them, so the boundaries between them are
    where a value was broken — and a break inside a word ends that line flush against the edge
    of the text rect, which no break at a space does."""
    rows = []
    for y in range(rect.top(), rect.bottom() + 1):
        columns = [x for x in range(rect.left(), rect.right() + 1)
                   if 0.299 * image.pixelColor(x, y).red()
                   + 0.587 * image.pixelColor(x, y).green()
                   + 0.114 * image.pixelColor(x, y).blue() < 180]
        if columns:
            rows.append((y, min(columns), max(columns)))
    blocks = []
    for y, first, last in rows:
        if blocks and blocks[-1][1] == y - 1:
            blocks[-1][1] = y
            blocks[-1][2] = min(blocks[-1][2], first)
            blocks[-1][3] = max(blocks[-1][3], last)
        else:
            blocks.append([y, y, first, last])
    return [tuple(block) for block in blocks]


def _settle_refit(screen, qapp) -> None:
    """Let the incremental width backlog drain through timer turns and the event loop."""
    QTest.qWait(150)
    qapp.processEvents()


def _prompt_text_ink(bar) -> int:
    """The ink of the prompt's message alone, inside the label's own rect.

    Counting every dark pixel of the bar counts the bar's red background as ink, so a prompt
    whose message was clipped by its row still "showed all its text" (40b review, second round).
    The label is transparent, so inside its rect everything that is not the bar's own colour is
    text."""
    image = bar.grab().toImage()
    return _modal_ink(image, bar.message_label.geometry())


def _modal_ink(image: QImage, rect: QRect) -> int:
    """Pixels of ``image`` inside ``rect`` that are not the rect's own background colour.

    The modal colour of a rect is its background whatever the stylesheet says, so the reference
    never depends on the code under test painting the right colour."""
    # Widget geometry uses logical pixels; grab() returns physical pixels at
    # the screen's device ratio (including fractional Windows display scaling).
    ratio = image.devicePixelRatio()
    rect = QRectF(rect.x() * ratio, rect.y() * ratio,
                  rect.width() * ratio, rect.height() * ratio).toAlignedRect()
    colours = [(image.pixelColor(x, y).red(), image.pixelColor(x, y).green(), image.pixelColor(x, y).blue())
               for y in range(max(0, rect.top()), min(rect.bottom() + 1, image.height()))
               for x in range(max(0, rect.left()), min(rect.right() + 1, image.width()))]
    if not colours:
        return 0
    background = max(set(colours), key=colours.count)
    return sum(1 for colour in colours if colour != background)


def _room_for_prompt(bar, qapp, width: int, stylesheet: str = ""):
    """An identical bar laid out at ``width`` and given the height its message needs.

    A twin of the bar, same style and same text, is the reference for how much of the message
    should be painted: the label's own font is the unstyled 9 pt, not the 15 px the stylesheet
    gives it, so rendering the text by hand under-counts it (40b review, second round). The
    stylesheet the bar is drawn under has to be handed to the twin too, or the twin measures a
    different type from the bar the Operator sees."""
    twin = FailurePromptBar(bar.styleSheet() + stylesheet)
    twin.step_label.setText(bar.step_label.text())
    twin.message_label.setText(bar.message_label.text())
    twin.setFixedWidth(width)
    twin.sync_height_at(width)
    twin.show()
    qapp.processEvents()
    return twin


# -- the three-pane step table -----------------------------------------------------


def test_a_long_actual_and_multiline_expected_row_shows_all_of_its_text(qapp, tmp_path):
    """The issue's first half: the central table wraps and the row expands to show it all."""
    screen = _three_pane_screen(qapp, tmp_path, width=1280)
    table = screen.table.table
    assert table.wordWrap(), "the step table does not word-wrap its cells"

    nodes, attempts = _long_state()
    screen.table.show_state(nodes, attempts)
    qapp.processEvents()

    long_row = screen.table.step_row(0)
    short_row = screen.table.step_row(1)
    assert table.rowHeight(long_row) > table.rowHeight(short_row), "the long row did not grow"

    for column in (4, 5, 6):
        text = table.item(long_row, column).text()
        shown = _cell_ink(table, long_row, column)
        # The same cell with room to spare paints every line it holds; a row that clips loses
        # the ink of the lines below the row boundary.
        roomier = _cell_ink(table, long_row, column, height=2000)
        assert shown >= 0.95 * roomier, (
            f"the {table.horizontalHeaderItem(column).text()} cell shows {shown} of the "
            f"{roomier} ink pixels its {len(text)} characters need in a {table.rowHeight(long_row)} px row"
        )
        # And the row is no taller than the wrapped text asks.
        font = table.item(long_row, column).font()
        text_width = _cell_text_width(table, long_row, column)
        assert table.rowHeight(long_row) >= _qt_wrap_height(text, font, text_width) - 1, (
            f"a {table.rowHeight(long_row)} px row holds text wrapped into "
            f"{_qt_wrap_height(text, font, text_width)} px in its {text_width} px text rect"
        )


def test_an_unbroken_300_character_value_wraps_inside_its_column(qapp, tmp_path):
    """A hex dump or device id has no word to break at; it still has to read (40b review)."""
    screen = _three_pane_screen(qapp, tmp_path, width=1280)
    table = screen.table.table
    nodes, _ = _long_state()
    screen.table.show_state(nodes, [
        {"node_seq": 0, "attempt_no": 1, "actual": UNBREAKABLE, "expected": UNBREAKABLE, "msg": "x"},
        {"node_seq": 1, "attempt_no": 1, "actual": "ok", "expected": "ok", "msg": "ok"},
    ])
    qapp.processEvents()

    row, column = screen.table.step_row(0), 4
    font = table.item(row, column).font()
    text_width = _cell_text_width(table, row, column)
    # The whole value, wrapped by the rule the cell is held to: word boundaries, and only this
    # token cut, where the column ends it. That is what the cell has to show.
    needed = _reference_ink(UNBREAKABLE, font, text_width)
    shown = _cell_ink(table, row, column)
    assert shown >= 0.9 * needed, (
        f"the {len(UNBREAKABLE)} character value painted {shown} ink pixels in a {text_width} px text rect, "
        f"where the wrap needs {needed}: the value ran out of the cell"
    )
    assert table.rowHeight(row) >= _qt_wrap_height(UNBREAKABLE, font, text_width), (
        f"the {table.rowHeight(row)} px row is shorter than the wrapped value needs "
        f"({text_width} px of text in a {table.columnWidth(column)} px column)"
    )
    # Word wrap alone leaves a token with no spaces as one line: it has to be broken, so the
    # cell shows it as several lines rather than one long run of ink.
    image = _paint(table.itemDelegateForColumn(column), table.model().index(row, column),
                   table.visualRect(table.model().index(row, column)).width(), table.rowHeight(row), table)
    lines = _line_blocks(image, _cell_text_rect(table, row, column))
    assert len(lines) > 1, (
        f"the unbroken {len(UNBREAKABLE)} character value was painted as {len(lines)} line in a "
        f"{text_width} px text rect"
    )
    assert max(block[3] - block[2] + 1 for block in lines) <= text_width, (
        "a line of the unbroken value is wider than the cell it is painted in"
    )


def test_a_prose_value_is_never_cut_inside_a_word(qapp, tmp_path):
    """Ordinary words break at spaces, never in the middle of a word (40b review, second round).

    The delegate painted with `TextWordWrap | TextWrapAnywhere`, and "break anywhere" means
    "break where the line runs out": a 74 px Actual column read "c/hannel", "wi/ndow" and
    "me/asured". The ink the cell paints has to be exactly the ink Qt's own word wrap paints for
    the same text in the same rect. Line boundaries are checked against Qt word wrap;
    glyph ink may legitimately reach the edge with some fonts."""
    screen = _three_pane_screen(qapp, tmp_path, width=1280)
    table = screen.table.table
    nodes, attempts = _long_state()
    screen.table.show_state(nodes, attempts)
    qapp.processEvents()

    row, column = screen.table.step_row(0), 4
    index = table.model().index(row, column)
    delegate = table.itemDelegateForColumn(column)
    font = index.data(Qt.ItemDataRole.FontRole) or table.font()
    rect = _cell_text_rect(table, row, column)
    cell = _paint(delegate, index, rect.left() + rect.width() + WrappedTextDelegate.TEXT_MARGIN,
                  table.rowHeight(row), table)
    reference = _paint_text(font, "\n".join(_reference_lines(LONG_ACTUAL, font, rect.width())),
                            rect.width(), rect.height(), table.palette().text().color())
    painted = _ink_positions(cell, rect)
    expected = {(x + rect.left(), y + rect.top())
                for (x, y) in _ink_positions(reference, QRect(0, 0, rect.width(), rect.height()))}
    assert painted == expected, (
        f"the cell painted {len(painted)} ink pixels in {len(_line_blocks(cell, rect))} lines where Qt's word wrap "
        f"paints {len(expected)} in {len(_line_blocks(reference, QRect(0, 0, rect.width(), rect.height())))}: "
        f"{len(painted ^ expected)} of them are in the wrong place, on rows "
        f"{sorted({y for _, y in painted ^ expected})[:8]}"
    )
    wrapped = delegate._wrapped_text(LONG_ACTUAL, font, rect.width())
    layout = QTextLayout(wrapped.replace("\n", "\u2028"), font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.NoWrap)
    layout.setTextOption(option)
    layout.beginLayout()
    lines = []
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(rect.width())
        lines.append(wrapped[line.textStart():line.textStart() + line.textLength()].strip())
    layout.endLayout()
    assert " ".join(lines) == LONG_ACTUAL, "a painted line boundary splits a source word"


def test_a_row_is_measured_at_the_width_qt_paints_the_cell_in(qapp, tmp_path):
    """One grid line is a whole line of text (40b review, second round: Sol and Strata).

    `columnWidth()` is the section; the delegate gets `visualRect(index)`, which is a grid line
    narrower. Measuring the wrap at the wider number sizes the row for text that will not fit in
    the cell it is painted in."""
    screen = _three_pane_screen(qapp, tmp_path, width=1280)
    table = screen.table.table
    nodes, attempts = _long_state()
    attempts[0]["expected"] = "4.8V"
    attempts[0]["msg"] = "ok"
    screen.table.show_state(nodes, attempts)
    qapp.processEvents()
    row, column = screen.table.step_row(0), 4
    font = table.item(row, column).font()

    # A width at which the grid line changes how many lines the value needs, so the test is not
    # simply checking that a row is tall enough for text that fits either way.
    margin = 2 * WrappedTextDelegate.TEXT_MARGIN
    for width in range(60, 200):
        if (len(_reference_lines(LONG_ACTUAL, font, width - 1 - margin))
                > len(_reference_lines(LONG_ACTUAL, font, width - margin))):
            break
    else:
        raise AssertionError("no column width in range changes the line count by one grid line")

    table.setColumnWidth(column, width)
    _settle_refit(screen, qapp)
    # Ink painted in the cell at the height the row has, against the same cell given room to
    # spare: a row measured at the section width loses the lines the grid line pushes down.
    shown = _cell_ink(table, row, column)
    roomier = _cell_ink(table, row, column, height=3000)
    assert shown == roomier, (
        f"a {table.rowHeight(row)} px row measured for the {table.columnWidth(column)} px column cut "
        f"{roomier - shown} ink pixels off the value as it wraps into the "
        f"{table.visualRect(table.model().index(row, column)).width()} px cell it is painted in"
    )
    assert table.rowHeight(row) >= _qt_wrap_height(LONG_ACTUAL, font, _cell_text_width(table, row, column)), (
        f"the row is {table.rowHeight(row)} px while the text wrapped into its {table.columnWidth(column)} px "
        f"column needs {_qt_wrap_height(LONG_ACTUAL, font, _cell_text_width(table, row, column))} px"
    )


def test_a_column_resized_by_hand_refits_the_rows_it_changes(qapp, tmp_path):
    """Row heights follow the column widths: the Message column stretches with the pane, and
    a section the header resizes takes width off it (40b review)."""
    screen = _three_pane_screen(qapp, tmp_path, width=1000)
    nodes, attempts = _long_state()
    attempts[0]["actual"] = "4.8V"
    attempts[0]["expected"] = "4.8V"
    attempts[0]["msg"] = LONG_ACTUAL  # only the stretch Message column carries the long text
    screen.table.show_state(nodes, attempts)
    qapp.processEvents()
    table = screen.table.table
    row = screen.table.step_row(0)
    narrow_message, narrow = table.columnWidth(6), table.rowHeight(row)

    screen.resize(1400, 700)
    _settle_refit(screen, qapp)
    wide_message, wide = table.columnWidth(6), table.rowHeight(row)
    assert wide_message > narrow_message, "the Message column did not widen with the pane"
    assert wide < narrow, f"the row stayed {narrow} px when its column widened to {wide_message} px"
    assert wide >= SHORT_STEP_ROW_HEIGHT, "the row collapsed below the ordinary row height"

    # A section resized by hand changes the stretch column without resizing the viewport, so
    # only a header-aware refit notices the rows have to grow.
    table.setColumnWidth(4, 240)
    _settle_refit(screen, qapp)
    squeezed = table.columnWidth(6)
    grown = table.rowHeight(row)
    font = table.item(row, 6).font()
    text_width = _cell_text_width(table, row, 6)
    assert grown >= _qt_wrap_height(LONG_ACTUAL, font, text_width), (
        f"the Message column went to {squeezed} px but the row stayed {grown} px, short of the "
        f"{_qt_wrap_height(LONG_ACTUAL, font, text_width)} px its text wraps into in the "
        f"{text_width} px the cell is painted in"
    )
    assert _cell_ink(table, row, 6) >= 0.95 * _cell_ink(table, row, 6, height=2000), (
        "the narrowed Message column clipped the value at the bottom of the row"
    )

    table.setColumnWidth(4, 80)
    _settle_refit(screen, qapp)
    assert table.rowHeight(row) < grown, (
        f"the row stayed {table.rowHeight(row)} px after the Message column got its width back "
        f"({table.columnWidth(6)} px)"
    )


def test_a_column_narrowed_without_the_viewport_resizing_refits_its_rows(qapp, tmp_path):
    """The header hook on its own: narrowing a section moves the stretch column while the
    table's viewport keeps its size, so a viewport filter alone never sees the change."""
    screen = _three_pane_screen(qapp, tmp_path, width=1000)
    nodes, attempts = _long_state()
    attempts[0]["actual"] = "4.8V"
    attempts[0]["expected"] = "4.8V"
    attempts[0]["msg"] = LONG_ACTUAL  # the only long text, in the stretch Message column
    screen.table.show_state(nodes, attempts)
    qapp.processEvents()
    table = screen.table.table
    row = screen.table.step_row(0)
    font = table.item(row, 6).font()

    table.setColumnWidth(2, 30)
    _settle_refit(screen, qapp)
    squeezed = table.columnWidth(6)
    grown = table.rowHeight(row)
    needed = _qt_wrap_height(LONG_ACTUAL, font, _cell_text_width(table, row, 6))
    assert grown >= needed, (
        f"the Result column took {squeezed} px off the Message column without resizing the "
        f"viewport and the row stayed {grown} px, short of the {needed} px its text wraps into"
    )
    assert _cell_ink(table, row, 6) >= 0.95 * _cell_ink(table, row, 6, height=2000), (
        "the Message column was squeezed and its value was cut off at the bottom of the row"
    )


def test_no_cell_of_the_step_table_can_be_edited(qapp, tmp_path):
    """Issue #3, second half: double-click, F2 and typing must not open an editor."""
    screen = _three_pane_screen(qapp, tmp_path)
    table = screen.table.table
    assert table.editTriggers() == QTableWidget.EditTrigger.NoEditTriggers, "the table still takes edit triggers"

    nodes, attempts = _long_state()
    screen.table.show_state(nodes, attempts)
    row, column = screen.table.step_row(0), 4
    before = screen.table.cell(row, column)
    assert before, "the test needs a cell with content to try to edit"

    # A genuine double-click on the cell: this PySide6 build does not expose Qt.MouseState,
    # so the event is handed to the viewport as Qt's own double-click event type.
    viewport = table.viewport()
    rect = table.visualRect(table.model().index(row, column))
    double_click = QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(rect.center()),
                               QPointF(rect.center()), Qt.MouseButton.LeftButton,
                               Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(viewport, double_click)
    qapp.processEvents()
    QTest.keyClick(table, Qt.Key.Key_F2)
    QTest.keyClick(table, "z")
    qapp.processEvents()
    assert screen.table.cell(row, column) == before, "an edit attempt changed the cell's text"
    # Belt and braces: no item carries the editable flag, so even a stray trigger finds
    # nothing it can open an editor on.
    for any_row in range(table.rowCount()):
        for any_column in range(table.columnCount()):
            item = table.item(any_row, any_column)
            if item is not None:
                assert not item.flags() & Qt.ItemFlag.ItemIsEditable, (
                    f"cell ({any_row}, {any_column}) is still marked editable"
                )


def test_step_enable_boxes_still_answer_a_click(qapp, tmp_path):
    """The Enable column is a cell widget, not an item: NoEditTriggers must not silence it.

    QTest mouse events never reach a `QTableWidget` cell widget on the offscreen platform, so
    the box is worked with the keyboard it answers to, and with Qt's own check-state calls."""
    screen = _three_pane_screen(qapp, tmp_path)
    nodes, attempts = _long_state()
    screen.table.show_state(nodes, attempts)
    qapp.processEvents()

    box = screen.table.checks[0]
    box.setChecked(True)
    qapp.processEvents()
    assert screen.table.enabler.states[0] is True, "the step-enable box started off"

    box.setFocus()
    QTest.keyClick(box, Qt.Key.Key_Space)
    qapp.processEvents()
    assert not box.isChecked() and screen.table.enabler.states[0] is False, (
        "space on the focused enable box did not untick it"
    )
    QTest.keyClick(box, Qt.Key.Key_Space)
    qapp.processEvents()
    assert box.isChecked() and screen.table.enabler.states[0] is True, (
        "space again did not tick the enable box back"
    )
    assert screen.table.cell(0, 1), "the step row the box belongs to is not in the table"


def test_short_three_pane_rows_keep_todays_height(qapp, tmp_path):
    """A single-line row is exactly the height it had before the packet: the table's default."""
    default = QTableWidget(1, 1).rowHeight(0)  # today's height: untouched rows keep this
    screen = _three_pane_screen(qapp, tmp_path)
    screen.table.show_state(
        [{"node_seq": 0, "step_number": "1.", "step_name": "ok", "kind": "step",
          "result": "PASS", "attempt_count": 1}],
        [{"node_seq": 0, "attempt_no": 1, "actual": "Green", "expected": "Green", "msg": "ok"}],
    )
    row = screen.table.step_row(0)
    assert screen.table.table.rowHeight(row) == default, (
        f"a short row became {screen.table.table.rowHeight(row)} px, not {default} px"
    )


def test_one_step_update_measures_one_row_not_the_whole_table(qapp, tmp_path):
    """``show_state`` runs after every Attempt: rows whose text and widths are unchanged are not
    re-measured. Measuring every row after every Attempt is what the 40b review timed at a
    second per run."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    steps = load_schedule_steps(session, "Logging schedule")
    screen = _three_pane_screen(qapp, tmp_path, width=1200)
    screen.table.load_steps(steps * 25)  # a long schedule, as a real run has
    rows = list(screen.table._step_rows)
    nodes = [{"node_seq": seq, "step_number": f"{seq + 1}.", "step_name": f"step {seq}",
              "kind": "step", "result": "PASS", "attempt_count": 1} for seq in rows]
    attempts = [{"node_seq": seq, "attempt_no": 1, "actual": "ok", "expected": "ok", "msg": "ok"}
                for seq in rows]
    screen.table.show_state(nodes, attempts)
    first = screen.table.measured_rows  # the pass show_state itself runs
    qapp.processEvents()
    assert first >= 2, "the first paint measured nothing"

    # One step's Actual changes; every other row is already measured at the same widths.
    changed_seq = rows[5]
    attempts[5]["actual"] = "a different reading entirely"
    screen.table.show_state(nodes, attempts)
    measured = screen.table.measured_rows
    qapp.processEvents()
    assert measured == 1, f"one changed step re-measured {measured} of the {len(rows)} rows"
    assert screen.table.table.rowHeight(screen.table.step_row(changed_seq)) > 30, "the changed row did not grow"


def test_a_font_change_re_measures_the_rows_it_changes(qapp, tmp_path):
    """The cache that skips unchanged rows has to see the font change too (40b review, Sol).

    The signature keyed on bold and the *point* size only. A font set with a pixel size reports
    no point size at all, so 13 px and 26 px look identical to the cache and the row keeps the
    height measured for the old type; a family change is invisible for the same reason."""
    screen = _three_pane_screen(qapp, tmp_path, width=1280)
    table = screen.table.table
    nodes, attempts = _long_state()
    screen.table.show_state(nodes, attempts)
    qapp.processEvents()
    row = screen.table.step_row(0)

    def set_font(font):
        for column in range(1, table.columnCount()):
            item = table.item(row, column)
            if item is not None:
                item.setFont(font)

    # Start from a pixel-sized font: after that, both changes below leave the point size alone,
    # which is exactly what a point-size-only signature cannot see.
    small = QFont(table.item(row, 6).font())
    small.setPixelSize(13)
    set_font(small)
    screen.table.show_state(nodes, attempts)
    _settle_refit(screen, qapp)
    at_13 = table.rowHeight(row)

    for change, font in (("13 px → 26 px", QFont(small)), ("Sans Serif → Courier", QFont(small))):
        if change.startswith("13 px"):
            font.setPixelSize(26)
        else:
            font.setFamily("Courier")
        assert font.pointSize() == small.pointSize() and font.bold() == small.bold(), (
            f"the {change} case needs a font a point-size-only signature cannot tell apart"
        )
        assert QFontMetrics(font).horizontalAdvance("the output voltage") != QFontMetrics(
            small
        ).horizontalAdvance("the output voltage"), f"the {change} did not change the font at all"
        set_font(font)
        screen.table.show_state(nodes, attempts)  # the pass that decides which rows to measure
        qapp.processEvents()
        grown = table.rowHeight(row)
        for column in (1, 4, 5, 6):
            text = screen.table.cell(row, column)
            if not text:
                continue
            needed = _qt_wrap_height(text, font, _cell_text_width(table, row, column))
            assert grown >= needed, (
                f"changing the row's font ({change}) left it {grown} px tall — the height measured "
                f"for 13 px type — while {table.horizontalHeaderItem(column).text()} now wraps into "
                f"{needed} px in its {_cell_text_width(table, row, column)} px cell"
            )
            shown = _cell_ink(table, row, column)
            roomier = _cell_ink(table, row, column, height=3000)
            assert shown == roomier, (
                f"the {change} painted {shown} of the {roomier} ink pixels column "
                f"{table.horizontalHeaderItem(column).text()} needs in a {grown} px row"
            )


def test_dragging_the_window_refits_in_bounded_turns(qapp, tmp_path, monkeypatch):
    """Drive timer turns explicitly; judge Message at its changed visual width."""
    screen = _many_long_rows(qapp, tmp_path)
    owner, table = screen.table, screen.table.table
    calls = []
    original = owner._row_signature
    monkeypatch.setattr(owner, "_row_signature", lambda row: (calls.append(row), original(row))[1])
    old_width = table.columnWidth(6)
    for delta in range(1, 61):
        table.setColumnWidth(1, 150 + delta)
        calls.clear()
        owner._refit_timer.stop()
        owner._refit_timer.timeout.emit()
        assert len(calls) <= 32, f"a drag turn examined {len(calls)} rows"
    assert table.columnWidth(6) < old_width, "the drag did not change Message width"
    # More turns than the entire backlog needs; no wall-clock deadline is involved.
    for _ in range(table.rowCount() // 8 + 2):
        calls.clear()
        owner._refit_timer.stop()
        owner._refit_timer.timeout.emit()
        assert len(calls) <= 32, f"a backlog turn examined {len(calls)} rows"
    for row in owner._step_rows.values():
        needed = _qt_wrap_height(owner.cell(row, 6), table.item(row, 6).font(),
                                 _cell_text_width(table, row, 6))
        assert table.rowHeight(row) >= needed, (
            f"Message row {row} is {table.rowHeight(row)} px but needs {needed} at changed width"
        )


def test_scrolling_fits_newly_visible_rows_before_paint(qapp, tmp_path):
    screen = _many_long_rows(qapp, tmp_path)
    owner, table = screen.table, screen.table.table
    table.setColumnWidth(1, 350)
    owner._refit_timer.stop()  # leave the offscreen backlog undrained
    row = list(owner._step_rows.values())[-1]
    table.scrollTo(table.model().index(row, 6))
    table.viewport().repaint()
    from PySide6.QtCore import QPoint
    row = table.indexAt(QPoint(1, 0)).row()
    assert row > 300, "the scroll did not expose distant rows"
    needed = _qt_wrap_height(owner.cell(row, 6), table.item(row, 6).font(),
                             _cell_text_width(table, row, 6))
    assert table.rowHeight(row) >= needed, "scroll painted a stale, clipped Message row"


# -- the staged step list ----------------------------------------------------------


def test_staged_rows_with_multiline_detail_show_every_line(qapp, tmp_path):
    """The issue's second half: the stage layout showed only the first line of the detail."""
    screen = _staged_screen(qapp, tmp_path, width=900)
    view = screen.run_view
    nodes, attempts = _long_state()
    view.refresh_rows(nodes, attempts)
    qapp.processEvents()

    list_widget = view.step_list
    tall = list_widget.item(view._step_rows[0])
    short = list_widget.item(view._step_rows[1])
    assert tall.sizeHint().height() > short.sizeHint().height(), "the staged row did not grow"

    delegate = list_widget.itemDelegate()
    index = list_widget.model().index(view._step_rows[0])
    width = list_widget.viewport().width()
    shown = _ink(_paint(delegate, index, width, tall.sizeHint().height(), list_widget))
    roomier = _ink(_paint(delegate, index, width, 2000, list_widget))
    assert shown >= 0.95 * roomier, (
        f"the {tall.sizeHint().height()} px row clipped {roomier - shown} ink pixels of detail"
    )
    detail = tall.data(STEP_DETAIL_ROLE)
    assert tall.sizeHint().height() >= _qt_wrap_height(
        detail, StepRowDelegate()._detail_font(),
        width - StepRowDelegate.BADGE_MIN_WIDTH - 10
    ) - 4, "the row is shorter than its wrapped detail needs"


def test_a_staged_row_wraps_an_unbroken_token(qapp, tmp_path):
    """The staged detail is mono text: a 300-character run with no spaces still has to read."""
    screen = _staged_screen(qapp, tmp_path, width=900)
    view = screen.run_view
    view.refresh_rows(
        [{"node_seq": 0, "kind": "step", "result": "FAIL"}],
        [{"node_seq": 0, "attempt_no": 1, "actual": UNBREAKABLE, "expected": UNBREAKABLE, "msg": "x"}],
    )
    qapp.processEvents()
    row = view._step_rows[0]
    item = view.step_list.item(row)
    item.setText("")  # isolate detail ink from the name, whose descenders may reach it
    delegate = view.step_list.itemDelegate()
    index = view.step_list.model().index(row)
    width = view.step_list.viewport().width()
    shown = _ink(_paint(delegate, index, width, item.sizeHint().height(), view.step_list))
    roomier = _ink(_paint(delegate, index, width, 2000, view.step_list))
    assert shown == roomier, f"the {item.sizeHint().height()} px row clipped {roomier - shown} ink pixels"
    # Compare detail alone: the delegate uses muted ink, not the reference's default
    # black. Thresholded ink counts differ substantially between those on Windows.
    from ate.ui.run_widgets import STEP_EMPHASIS_ROLE, STEP_RESULT_ROLE
    from ate.ui.run_widgets import _detail_pen, _row_fill
    emphasis = item.data(STEP_EMPHASIS_ROLE)
    left = 5 if emphasis else 4
    badge = delegate._badge_width(item.data(STEP_RESULT_ROLE), QRect(0, 0, width, 2000))
    text_width = width - left - badge - 6
    font = delegate._detail_font()
    fill = TOKENS["failed_tint"] if emphasis == "failed" else _row_fill(index).color()
    reference = _paint_text(font, "\n".join(_reference_lines(item.data(STEP_DETAIL_ROLE), font, text_width)),
                            text_width, 2000, _detail_pen(fill), wrap=False)
    cell = _paint(delegate, index, width, item.sizeHint().height(), view.step_list)
    rect = QRect(left, 22, text_width, cell.height() - 22)
    painted = _ink_positions(cell, rect)
    expected = {(x + left, y + 22) for x, y in _ink_positions(
        reference, QRect(0, 0, text_width, reference.height()))}
    assert painted == expected, "the staged detail differs from the complete wrapped value"


def test_staged_rows_are_measured_at_the_width_the_list_actually_has(qapp, tmp_path):
    """Rows are refitted against the viewport's width, not the width the list had before.

    The filter has to sit on the viewport: the list widget is resized before Qt works out the
    viewport's new width, so a refit on the list measures against the old one (40b review)."""
    screen = _staged_screen(qapp, tmp_path, width=1200)
    view = screen.run_view
    nodes, attempts = _long_state()
    view.refresh_rows(nodes, attempts)
    qapp.processEvents()

    row = view._step_rows[0]
    item = view.step_list.item(row)
    delegate = view.step_list.itemDelegate()
    index = view.step_list.model().index(row)

    for width in (700, 500, 900, 1200):
        screen.resize(width, 800)
        qapp.processEvents()
        viewport = view.step_list.viewport().width()
        shown = _ink(_paint(delegate, index, viewport, item.sizeHint().height(), view.step_list))
        roomier = _ink(_paint(delegate, index, viewport, 2000, view.step_list))
        assert shown >= 0.95 * roomier, (
            f"at a {viewport} px list the row stayed {item.sizeHint().height()} px and lost "
            f"{roomier - shown} ink pixels of detail"
        )
        assert item.sizeHint().height() >= _qt_wrap_height(
            item.data(STEP_DETAIL_ROLE), StepRowDelegate()._detail_font(),
            viewport - StepRowDelegate.BADGE_MIN_WIDTH - 10
        ) - 4, f"a {item.sizeHint().height()} px row at {viewport} px wide is too short for its detail"


def test_short_staged_rows_keep_todays_height(qapp, tmp_path):
    """No detail line stays 30 px; a one-line detail stays 38 px — exactly as before 40b."""
    screen = _staged_screen(qapp, tmp_path)
    view = screen.run_view
    view.refresh_rows(
        [{"node_seq": 0, "kind": "step", "result": "PASS"}, {"node_seq": 1, "kind": "step"}],
        [{"node_seq": 0, "attempt_no": 1, "actual": "Green", "expected": "Green", "msg": "ok"}],
    )
    with_detail = view.step_list.item(view._step_rows[0]).sizeHint().height()
    without = view.step_list.item(view._step_rows[1]).sizeHint().height()
    assert (without, with_detail) == (30, 38), "short staged rows changed height"


def test_the_result_badge_still_paints_in_a_wrapped_row(qapp, tmp_path):
    """R4 alignment: badge and detail share the taller row, the badge stays at the right edge."""
    screen = _staged_screen(qapp, tmp_path, width=900)
    view = screen.run_view
    nodes, attempts = _long_state()
    view.refresh_rows(nodes, attempts)
    qapp.processEvents()

    row = view._step_rows[0]
    image = _paint(view.step_list.itemDelegate(), view.step_list.model().index(row),
                   view.step_list.viewport().width(), view.step_list.item(row).sizeHint().height(),
                   view.step_list)

    # The FAIL badge's own red, painted in the 70 px at the right edge, proves the badge is
    # still at the edge of a row that grew for its wrapped detail.
    target = QColor(TOKENS["failed"]).rgb()
    right_band = sum(
        1 for y in range(image.height())
        for x in range(image.width() - 70, image.width())
        if image.pixel(x, y) == target
    )
    assert right_band > 40, "no FAIL badge was painted at the right edge of the wrapped row"


# -- the failure prompt -------------------------------------------------------------


def test_the_failure_prompt_bar_grows_for_its_wrapped_message(qapp):
    """A wrapping label in a bar sized for the buttons clips the message (40b review)."""
    bar = FailurePromptBar()
    bar.setStyleSheet(STYLESHEET + bar.styleSheet())
    bar.show_prompt("1.", "FAIL", "the rail did not settle", actual=LONG_ACTUAL, expected=EXPECTED_LINES)
    assert bar.message_label.wordWrap(), "the prompt message cannot wrap"
    assert LONG_ACTUAL in bar.message_label.text(), "the prompt clipped the measurements"
    assert bar.hasHeightForWidth(), "the prompt bar reports a height that ignores its width"

    holder = QWidget()
    QVBoxLayout(holder).addWidget(bar)
    holder.setFixedWidth(640)
    holder.adjustSize()
    holder.show()
    qapp.processEvents()
    # Qt's own measure of the label at the width the layout gives it, plus the bar's padding.
    needed = bar.message_label.heightForWidth(bar.message_label.width())
    assert bar.minimumHeight() >= needed, (
        f"the bar is {bar.minimumHeight()} px tall while its message needs {needed} px "
        f"in the {bar.width()} px it has"
    )
    assert bar.heightForWidth(bar.width()) >= needed, (
        f"heightForWidth({bar.width()}) said {bar.heightForWidth(bar.width())}, short of {needed}"
    )
    # The ink of the message alone, against the same message in a twin bar that is given the
    # height it asks for. Counting every dark pixel of the bar counts its red background, so a
    # clipped message still passed, and rendering the text by hand uses the label's unstyled
    # 9 pt font, not the 15 px the stylesheet gives it (40b review, second round).
    shown = _prompt_text_ink(bar)
    room = _prompt_text_ink(_room_for_prompt(bar, qapp, bar.width()))
    assert shown >= 0.95 * room, (
        f"the bar's label paints {shown} of the {room} ink pixels the same message paints in "
        f"an identical {bar.width()} px bar: {len(bar.message_label.text())} characters in a "
        f"{bar.message_label.width()} x {bar.message_label.height()} px label in a "
        f"{bar.height()} px bar"
    )


def _prompt_ink_in_row(table, row: int, bar) -> int:
    """The message as it is actually painted inside the row that holds it.

    A bar taller than its row is clipped at the row's bottom edge, so grabbing the bar itself
    cannot see the cut: the last line of a long Actual fell below the row boundary and still
    counted as ink (40b review, second round). The viewport's own paint is what the Operator
    sees."""
    span = table.visualRect(table.model().index(row, 0))
    label = bar.message_label.geometry()
    painted = QRect(bar.geometry().left() + label.left(), bar.geometry().top() + label.top(),
                   label.width(), label.height())
    row_rect = QRect(span.left(), span.top(), span.width(), table.rowHeight(row))
    return _modal_ink(table.viewport().grab().toImage(), painted & row_rect)


def test_the_three_pane_inline_prompt_row_shows_the_whole_prompt(qapp, tmp_path):
    """The row holding the inline prompt is as tall as the bar laid out in *that row* needs.

    The prompt spans every column and Qt puts the bar inside the span less the style's item
    padding: at 1300 px the row was sized for the 619 px the span is while the bar laid its
    message out in the 607 px the padding leaves it, and the row came out 384 px against a bar
    that needs 398 — the last lines of a long Actual fell below the row's bottom (40b review,
    second round). The row is now measured from the bar's own geometry, so the two agree."""
    screen = _three_pane_screen(qapp, tmp_path, width=1300)
    screen.resize(1300, 1400)  # tall enough that the whole prompt row is painted
    # The Station runs the theme, and the theme's item padding is what makes the bar narrower
    # than the row it spans: without it the row and the bar agree by accident (40b review,
    # second round).
    screen.setStyleSheet(STYLESHEET)
    qapp.processEvents()
    nodes, attempts = _long_state()
    screen.table.show_state(nodes, attempts, paused_seq=0, paused_result="FAIL",
                            paused_message="the rail did not settle",
                            paused_actual=LONG_ACTUAL, paused_expected=EXPECTED_LINES)
    _settle_refit(screen, qapp)
    table = screen.table.table
    row = screen.table._prompt_rows[0]
    bar = screen.table._prompt_bars[0]
    height = table.rowHeight(row)
    span = table.visualRect(table.model().index(row, 0))
    # The height the bar needs at the width it actually has, measured by the bar itself.
    needed = bar.heightForWidth(bar.width())
    assert height >= needed, (
        f"the prompt row is {height} px while the bar in it needs {needed} px to show its "
        f"{len(bar.message_label.text())} character message in the {bar.width()} px it has"
    )
    assert bar.height() <= height, (
        f"the prompt bar is {bar.height()} px tall in a {height} px row, so the bottom "
        f"{bar.height() - height} px of the message is painted outside its row"
    )
    assert bar.geometry().left() >= span.left() and bar.geometry().right() <= span.right(), (
        f"the prompt bar spans {bar.geometry().left()}..{bar.geometry().right()} inside a row "
        f"that spans {span.left()}..{span.right()}"
    )
    row_bottom = span.top() + height - 1
    assert bar.geometry().bottom() <= row_bottom, (
        f"the prompt bar runs to {bar.geometry().bottom()} inside a row that ends at "
        f"{row_bottom}: the bottom {bar.geometry().bottom() - row_bottom} px of the prompt is "
        f"painted below the row it is in"
    )
    assert height > SHORT_STEP_ROW_HEIGHT, "the prompt row collapsed to a single line"
    shown = _prompt_ink_in_row(table, row, bar)
    room = _prompt_text_ink(_room_for_prompt(bar, qapp, bar.width(), screen.styleSheet()))
    assert shown >= 0.95 * room, (
        f"the inline prompt paints {shown} of the {room} ink pixels its "
        f"{len(bar.message_label.text())} character message paints in an identical "
        f"{bar.width()} px bar: the {bar.message_label.height()} px the label needs do not fit "
        f"in the {height} px row"
    )


# -- the other places the issue asked about --------------------------------------


def test_a_destroyed_step_table_refits_nothing(qapp, tmp_path):
    """A refit scheduled from the header must not fire after the table is gone.

    The header raises `sectionResized` while Qt is tearing the pane down, and by then the
    table's own attributes are cleared: measuring rows then raises inside the Qt event loop
    and every later test that pumps events pays for it (packet 40b review)."""
    screen = _three_pane_screen(qapp, tmp_path, width=900)
    nodes, attempts = _long_state()
    screen.table.show_state(nodes, attempts)
    qapp.processEvents()
    screen.resize(1100, 700)
    qapp.processEvents()
    from PySide6.QtCore import QTimer
    owner = screen.table
    timers = [owner._refit_timer, *owner.findChildren(QTimer)]
    delay = max(timer.interval() for timer in timers) + 150
    passes = owner.refit_passes
    screen.close()
    shiboken6.delete(screen)
    QTest.qWait(delay)
    qapp.processEvents()
    assert all(not shiboken6.isValid(timer) for timer in timers), "a refit timer outlived its table"
    assert owner.refit_passes == passes, "a destroyed table ran another refit callback"


def test_history_summary_labels_wrap_long_values(qapp, tmp_path):
    """History's detail panel: the summary, counts and mismatch lists wrap inside the panel."""
    store = RunStore(tmp_path / "ate.db")
    screen = HistoryScreen(HistorySession(store))
    screen.resize(800, 600)
    screen.show()
    qapp.processEvents()
    for label in (screen._summary_fields, screen._result_counts, screen._mismatches):
        assert label.wordWrap(), "a History summary label clips instead of wrapping"
    long_mismatches = ", ".join(f"checksum mismatch: part-{i}.tslua" for i in range(12))
    screen._mismatches.setText(long_mismatches)
    qapp.processEvents()
    assert screen._mismatches.heightForWidth(250) >= _qt_wrap_height(
        long_mismatches, screen._mismatches.font(), 250
    ), "the mismatch list is shorter than its wrapped lines"


def test_the_debug_window_wraps_long_log_lines(qapp):
    """Checked, no change needed: the Debug window's mono pane wraps at its width by default."""
    window = DebugWindow()
    text = window.text_widget()
    assert text.lineWrapMode() == QPlainTextEdit.LineWrapMode.WidgetWidth, (
        "the Debug window would clip long log lines at the window edge"
    )


def test_40b_review_screenshots(qapp, tmp_path):
    """Write the review set to ``ATE_40B_SHOTS`` (e.g. /tmp/40b-shots) for a human to look at.

    Both layouts, both showing the ~300-character Actual and the 4-line Expected, so the
    wrapping and the row growth can be seen rather than only asserted."""
    shots_env = os.environ.get("ATE_40B_SHOTS", "")
    if not shots_env:
        pytest.skip("review screenshots are opt-in: set ATE_40B_SHOTS to a directory")
    shots = Path(shots_env)
    shots.mkdir(parents=True, exist_ok=True)

    def shot(widget, name: str) -> None:
        for _ in range(30):
            qapp.processEvents()
        widget.grab().save(str(shots / name))
        assert (shots / name).stat().st_size > 0, name

    nodes, attempts = _long_state()

    screen = _three_pane_screen(qapp, tmp_path, width=1280)
    screen.resize(1280, 900)
    screen.table.show_state(nodes, attempts)
    shot(screen, "three-pane-long-values.png")

    staged = _staged_screen(qapp, tmp_path, width=1280)
    staged.resize(1280, 900)
    staged.run_view.refresh_rows(nodes, attempts)
    shot(staged.run_view.step_list, "staged-long-values.png")


def test_40b_uses_the_production_theme(qapp):
    assert qapp.styleSheet() == STYLESHEET, "40b did not install the production stylesheet"


@pytest.fixture(scope="module", autouse=True)
def production_theme(qapp):
    from ate.ui.theme import _APPLIED, apply_theme

    stylesheet, palette, font = qapp.styleSheet(), qapp.palette(), qapp.font()
    applied = qapp in _APPLIED
    _APPLIED.discard(qapp)
    apply_theme(qapp)
    yield
    qapp.setStyleSheet(stylesheet)
    qapp.setPalette(palette)
    qapp.setFont(font)
    if not applied:
        _APPLIED.discard(qapp)


def _many_long_rows(qapp, tmp_path, count=200):
    screen = _three_pane_screen(qapp, tmp_path, width=1600)
    steps = list(screen.table._rows)
    screen.table.load_steps((steps * (count // len(steps) + 1))[:count])
    seqs = list(screen.table._step_rows)
    screen.table.show_state(
        [{"node_seq": seq, "kind": "step", "result": "FAIL"} for seq in seqs],
        [{"node_seq": seq, "attempt_no": 1, "msg": LONG_ACTUAL * 2} for seq in seqs],
    )
    qapp.processEvents()
    return screen


def test_width_refit_callback_is_bounded(qapp, tmp_path, monkeypatch):
    screen = _many_long_rows(qapp, tmp_path)
    owner = screen.table
    table = owner.table
    calls = []
    original = owner._row_signature
    monkeypatch.setattr(owner, "_row_signature", lambda row: (calls.append(row), original(row))[1])
    table.setColumnWidth(1, table.columnWidth(1) + 100)
    owner._refit_timer.stop()
    owner._refit_now()  # one GUI timer callback, independent of elapsed wall time
    assert len(calls) <= 32, f"one width callback examined {len(calls)} rows (budget 32)"


def test_staged_width_callback_is_bounded(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QListWidgetItem
    from ate.ui import run_widgets

    screen = _staged_screen(qapp, tmp_path, width=1600)
    widget = screen.run_view.step_list
    widget.clear()
    for _ in range(200):
        item = QListWidgetItem("long step", widget)
        item.setData(STEP_DETAIL_ROLE, LONG_ACTUAL * 2)
    qapp.processEvents()
    calls = []
    original = run_widgets._step_row_height
    monkeypatch.setattr(run_widgets, "_step_row_height", lambda *args: (calls.append(args), original(*args))[1])
    screen.resize(1300, 700)
    fitter = widget.viewport().findChild(run_widgets._RefitStepRows)
    if fitter is None:
        fitter = widget.findChild(run_widgets._RefitStepRows)
    fitter._timer.stop()
    if hasattr(fitter, "tick"):
        fitter.restart()
        fitter._timer.stop()
        fitter.tick()
    else:
        fitter._refit()
    assert 0 < len(calls) <= 32, f"one staged width callback measured {len(calls)} rows (budget 32)"


@pytest.mark.parametrize("viewport_height", [398, 560, 697])
@pytest.mark.parametrize("window_width", [600, 650, 700, 750, 800, 1000, 1280])
def test_inline_prompt_first_show_reaches_buttons(qapp, tmp_path, viewport_height, window_width):
    screen = _three_pane_screen(qapp, tmp_path, width=window_width)
    owner = screen.table
    owner.table.setFixedHeight(viewport_height + owner.table.height() - owner.table.viewport().height())
    qapp.processEvents()
    assert owner.table.viewport().height() == viewport_height
    owner._refit_timer.stop()
    nodes, attempts = _long_state()
    owner.show_state(nodes, attempts, paused_seq=0, paused_result="FAIL",
                     paused_message=LONG_ACTUAL, paused_actual=LONG_ACTUAL,
                     paused_expected=EXPECTED_LINES)
    # First layout only: expressly prevent the width backlog from repairing the row.
    for _ in range(5):
        owner._refit_timer.stop()
        qapp.processEvents()
    owner._refit_timer.stop()
    row, bar = owner._prompt_rows[0], owner._prompt_bars[0]
    span = owner.table.visualRect(owner.table.model().index(row, 0))
    assert bar.geometry().bottom() < span.top() + owner.table.rowHeight(row), (
        f"first-show prompt needs {bar.height()} px in a {owner.table.rowHeight(row)} px row"
    )
    viewport = owner.table.viewport()
    scroll = owner.table.verticalScrollBar()
    buttons = (bar.continue_button, bar.retry_button, bar.abort_button)
    assert len({button.width() for button in buttons}) == 1
    for button in buttons:
        option = QStyleOptionButton()
        button.initStyleOption(option)
        contents = button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
        advance = button.fontMetrics().horizontalAdvance(button.text())
        assert contents.width() >= advance, f"{button.text()} content {contents.width()} < text {advance}"
        assert not button.isDefault() and not button.autoDefault()
    if bar.abort_button.y() > bar.retry_button.y():
        gap = bar.abort_button.y() - bar.retry_button.geometry().bottom() - 1
    else:
        gap = bar.abort_button.x() - bar.retry_button.geometry().right() - 1
    assert gap >= 28, f"Abort gap is only {gap} px"
    reachable = [False] * len(buttons)
    for position in range(scroll.minimum(), scroll.maximum() + 1):
        scroll.setValue(position)
        rects = [QRect(button.mapTo(viewport, button.rect().topLeft()), button.size())
                 for button in buttons]
        reachable = [seen or viewport.rect().contains(rect) for seen, rect in zip(reachable, rects)]
        if all(reachable):
            break
    assert all(reachable), f"all three prompt buttons cannot be reached at {window_width} px in {viewport_height} px viewport"
    if bar._buttons.y() > bar.message_label.y():
        assert bar.message_label.width() >= 0.6 * bar.width(), "stacked message is cramped"
    shown = _prompt_text_ink(bar)
    twin = _room_for_prompt(bar, qapp, bar.width(), screen.styleSheet())
    twin.setMinimumHeight(twin.height() + 400)
    qapp.processEvents()
    assert shown == _prompt_text_ink(twin), "prompt message text is clipped"
    twin.close()
    shots = os.environ.get("ATE_40B_SHOTS")
    if shots and window_width in (700, 800, 1000) and viewport_height == 697:
        Path(shots).mkdir(parents=True, exist_ok=True)
        screen.grab().save(str(Path(shots) / f"three-pane-inline-prompt-{window_width}.png"))


def test_one_text_update_during_width_backlog_stays_immediate(qapp, tmp_path):
    screen = _many_long_rows(qapp, tmp_path)
    owner, table = screen.table, screen.table.table
    table.setColumnWidth(1, 250)
    owner._refit_timer.stop()
    seqs = list(owner._step_rows)
    owner.show_state(
        [{"node_seq": seq, "kind": "step", "result": "FAIL"} for seq in seqs],
        [{"node_seq": seq, "attempt_no": 1, "msg": LONG_ACTUAL * 2 + (" changed" if seq == seqs[5] else "")}
         for seq in seqs],
    )
    assert owner.measured_rows == 1, f"one text update synchronously measured {owner.measured_rows} rows"


def test_staged_list_scrolls_in_pixels_and_fits_before_paint(qapp, tmp_path):
    from ate.ui.run_widgets import _RefitStepRows
    screen = _staged_screen(qapp, tmp_path)
    widget = screen.run_view.step_list
    assert widget.verticalScrollMode() == widget.ScrollMode.ScrollPerPixel, (
        "oversized staged rows cannot be scrolled through in pixels"
    )
    from PySide6.QtCore import QSize
    from PySide6.QtWidgets import QListWidgetItem
    for _ in range(30):
        item = QListWidgetItem("long staged row", widget)
        item.setSizeHint(QSize(0, 800))
    widget.doItemsLayout()
    widget.verticalScrollBar().setValue(0)
    fitter = widget.findChild(_RefitStepRows)
    fitter.restart()
    fitter._timer.stop()
    calls = []
    fitter.fit_row = calls.append
    widget.verticalScrollBar().setValue(widget.verticalScrollBar().maximum())
    assert calls, "pixel scrolling did not synchronously fit staged rows"


def test_unlaid_prompt_height_includes_style_padding(qapp, tmp_path):
    screen = _three_pane_screen(qapp, tmp_path, width=700)
    owner = screen.table
    row = owner._prompt_rows[0]
    owner.table.setRowHidden(row, False)
    # A fresh bar has not been attached to a cell or laid out at its span width.
    bar = FailurePromptBar()
    bar.message_label.setText(LONG_ACTUAL)
    bar.resize(0, 0)
    assert bar.width() == 0
    owner._prompt_bars[0] = bar
    span = owner.table.visualRect(owner.table.model().index(row, 0))
    option = QStyleOptionViewItem()
    option.initFrom(owner.table)
    option.rect = span
    content = owner.table.style().subElementRect(
        QStyle.SubElement.SE_ItemViewItemText, option, owner.table
    )
    padding = content.top() - span.top()
    assert padding > 0, "production theme must exercise item padding"
    width = max(1, min(content.width(), owner.table.viewport().width() - _INLINE_PROMPT_VIEWPORT_INSET))
    expected = bar.heightForWidth(width) + 2 * padding
    assert owner._prompt_row_height(0) == expected, "unlaid prompt lost its style padding"
    assert bar.minimumHeight() == expected - 2 * padding
    bar.close()


def test_staged_refit_filter_is_installed_once(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QObject
    from ate.ui.run_widgets import _RefitStepRows
    installs = []
    original = QObject.installEventFilter

    def install(watched, event_filter):
        if isinstance(event_filter, _RefitStepRows):
            installs.append((watched, event_filter))
        return original(watched, event_filter)

    monkeypatch.setattr(QObject, "installEventFilter", install)
    screen = _staged_screen(qapp, tmp_path)
    installs = [pair for pair in installs if pair[0] is screen.run_view.step_list.viewport()]
    assert len(installs) == 1, f"staged refit filter installed {len(installs)} times"


@pytest.mark.parametrize("layout", ["three-pane", "staged"])
def test_resize_fits_visible_rows_at_every_paint(qapp, tmp_path, layout):
    from ate.ui.run_widgets import _RefitStepRows
    if layout == "three-pane":
        screen = _three_pane_screen(qapp, tmp_path, width=1600)
        owner = screen.table
        nodes, attempts = _long_state()
        attempts[0]["msg"] = LONG_ACTUAL * 2
        owner.show_state(nodes, attempts)
        view, fitter = owner.table, owner._width_fit

        def heights(row):
            needed = max(_qt_wrap_height(owner.cell(row, col), view.item(row, col).font(),
                                         _cell_text_width(view, row, col))
                         for col in (1, 4, 5, 6) if view.item(row, col))
            return view.rowHeight(row), needed
    else:
        screen = _staged_screen(qapp, tmp_path, width=1600)
        owner = screen.run_view
        owner.refresh_rows(*_long_state())
        view = owner.step_list
        fitter = view.findChild(_RefitStepRows)

        def heights(row):
            item = view.item(row)
            if not item.data(STEP_DETAIL_ROLE):
                return item.sizeHint().height(), 0
            font = StepRowDelegate()._detail_font()
            lines = _reference_lines(item.data(STEP_DETAIL_ROLE), font,
                                     view.viewport().width() - StepRowDelegate.BADGE_MIN_WIDTH - 10)
            needed = 22 + len(lines) * QFontMetrics(font).lineSpacing()
            return item.sizeHint().height(), needed
    _settle_refit(screen, qapp)
    paints, deficits = [], []

    class Observer(QObject):
        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.Paint:
                paints.append(watched.width())
                for row in range(view.model().rowCount()):
                    if view.visualRect(view.model().index(row, 0)).intersects(watched.rect()):
                        actual, needed = heights(row)
                        if actual < needed:
                            deficits.append((watched.width(), row, needed - actual))
            return False

    observer = Observer(view)
    # Qt invokes filters in reverse installation order: observe after the fitter,
    # immediately before the viewport paints, including its very first paint.
    view.viewport().removeEventFilter(fitter)
    view.viewport().installEventFilter(observer)
    view.viewport().installEventFilter(fitter)
    for width in (1000, 800, 1280, *range(1280, 1220, -1)):
        screen.resize(width, screen.height())
        qapp.processEvents()
        view.viewport().repaint()
    assert paints, "observer saw no viewport paints"
    assert not deficits, f"paint used stale row heights (width, row, deficit): {deficits}"


@pytest.mark.parametrize("width", [319, 450, 700, 900])
def test_staged_prompt_switches_arrangement_without_clipping(qapp, width):
    bar = FailurePromptBar()
    bar.setStyleSheet(STYLESHEET + bar.styleSheet())
    bar.show_prompt("1.", "FAIL", LONG_ACTUAL, actual=LONG_ACTUAL, expected=EXPECTED_LINES)
    bar.setFixedWidth(width)
    bar.show()
    qapp.processEvents()
    buttons = (bar.continue_button, bar.retry_button, bar.abort_button)
    assert len({button.width() for button in buttons}) == 1
    assert all(not button.isDefault() and not button.autoDefault() for button in buttons)
    vertical = bar.abort_button.y() > bar.retry_button.y()
    peer_gap = (bar.retry_button.y() - bar.continue_button.geometry().bottom() - 1 if vertical
                else bar.retry_button.x() - bar.continue_button.geometry().right() - 1)
    abort_gap = (bar.abort_button.y() - bar.retry_button.geometry().bottom() - 1 if vertical
                 else bar.abort_button.x() - bar.retry_button.geometry().right() - 1)
    assert abort_gap - peer_gap == 16
    for button in buttons:
        option = QStyleOptionButton()
        button.initStyleOption(option)
        content = button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
        assert content.width() >= button.fontMetrics().horizontalAdvance(button.text())
        rect = QRect(button.mapTo(bar, button.rect().topLeft()), button.size())
        assert bar.rect().contains(rect), "staged prompt controls are clipped"
    if width <= 700:
        assert bar.message_label.width() >= 0.6 * width
        assert bar._buttons.y() >= bar.message_label.geometry().bottom()
    else:
        assert bar._buttons.y() < bar.message_label.geometry().bottom()
    twin = _room_for_prompt(bar, qapp, width)
    twin.setMinimumHeight(twin.height() + 400)
    qapp.processEvents()
    assert _prompt_text_ink(bar) == _prompt_text_ink(twin), "staged message is clipped"
    twin.close()
    shots = os.environ.get("ATE_40B_SHOTS")
    if shots and width == 700:
        bar.grab().save(str(Path(shots) / "staged-prompt-700.png"))
    bar.close()
