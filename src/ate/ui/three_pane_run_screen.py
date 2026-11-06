"""The three-pane Run screen (packet 25a): left setup pane, centre step table, right log.

Spec line 46-50: the same run session, arranged differently. The left pane carries the Operator's
fields, the filterable schedule list with errors and mismatches inline, the Start/Abort controls and
the live `Result` counts; the centre pane is the steps as a table with Result, Attempts, Actual,
Expected and Message, the Step Inputs under the step that declared them, and — on a Stop-on-Fail
pause — the Continue/Retry/Abort prompt inline under the failed step with a sticky banner above the
table; the right pane is the Log band (ticket 39, R3; packet 42 removed the Summary tab it used to
share), and Debug is its own window opened from the shared run header above the panes (R2, D2). The
status bar (packet 14d) runs along the bottom of all three.

Like the staged screen, this layout holds no run logic: every control is bound to the run session,
and everything drawn is a row the store already wrote. The widgets themselves are the shared ones in
``run_widgets``.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from PySide6.QtCore import Qt, QRect, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QRadioButton,
    QSplitter,
    QStyle,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ate.engine.run_engine import RunEngine

from ..run_session.headless import HeadlessRunSession, SessionError, SessionState
from .run_widgets import (
    _IncrementalRowFit,
    DebugWindow,
    FailurePromptBar,
    LogPane,
    MismatchConfirmation,
    ResultBadgeDelegate,
    ResultCounts,
    RunFieldsForm,
    RunHeader,
    RunScreenMixin,
    SchedulePicker,
    StatusBar,
    StepEnabler,
    StepInputWidget,
    WrappedTextDelegate,
    _BROKEN_ERR,
    _CURRENT_BG,
    _FAILED_BG,
    _HEADER_BG,
    _INLINE_PROMPT_STYLE,
    _ROW_BG,
    _STICKY_STYLE,
    _no_auto_default,
    _rollup_text as rollup_text,
    _row_label as row_label,
    load_schedule_steps,
)
from .theme import badge_width_for

#: The centre pane's columns. `Enable` carries the step's box (packet 15d); the rest are the mockup's
#: `.b-thead` cells (prototype line 100).
COLUMNS = ("Enable", "Step", "Result", "Attempts", "Actual", "Expected", "Message")
#: Widths that fit the 640 px centre pane of a 1280x800 window: every header word fits, and the
#: Message column takes whatever is left. Ticket 39 review: the Result column starts at 78 px,
#: enough for a "PASS"/"FAIL"/"ERROR" badge beside its glyph, and `StepTable.show_state` widens
#: it from the font metrics when a longer Result word ("NOREPORT", "FORCED_NOTRUN") appears, so
#: the word is never dropped in favour of the glyph alone.
_RESULT_COLUMN_MIN = 78
_COLUMN_WIDTHS = (46, 150, _RESULT_COLUMN_MIN, 60, 80, 80, 130)
#: Height of a step row holding only single-line text: the table's own default row height.
#: Wrapping only makes a row taller than this when a cell's wrapped lines need it (packet 40b);
#: a row is never made shorter.
SHORT_STEP_ROW_HEIGHT = 30
#: Reserve the inline cell's horizontal padding and frame inside the viewport.
_INLINE_PROMPT_VIEWPORT_INSET = 20
#: The columns that carry wrapping text. Enable holds a widget and Result is painted as a
#: badge, so neither needs a text measurement.
_TEXT_COLUMNS = (1, 4, 5, 6)


class StepTable(QWidget):
    """The centre pane's step table: one row per expanded step, its inputs and its prompt under it.

    Rows are built once, when a schedule is picked: a step row, then one row per declared Step Input
    (packet 10a), then a hidden prompt row for that step. Nothing is added or removed while the run
    is up — pausing shows the step's own prompt row and resuming hides it again — so no row carrying
    a widget is ever destroyed mid-run (the Qt 6 lesson from 10a: an item that holds a widget deletes
    it when the item goes, taking the Operator's answers with it).
    """

    #: A Stop-on-Fail prompt answered from one of the inline bars: CONTINUE, RETRY or ABORT.
    prompt_choice = Signal(str)

    def __init__(self, session: Optional[HeadlessRunSession] = None, screen: Optional["ThreePaneRunScreen"] = None) -> None:
        super().__init__()
        self.session = session
        # The layout this table sits in (packet 25b): its inputs and prompts answer only while this
        # layout is the visible one.
        self.screen = screen
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.schedule_label = QLabel("Choose a Test Schedule on the left.")
        self.schedule_label.setWordWrap(True)
        # The mockup's `.b-sticky`: the pause banner sits above the scrolling table so it stays
        # readable whatever the Operator scrolls to.
        self.banner = QLabel("")
        self.banner.setObjectName("pause_banner")
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet(_STICKY_STYLE)
        self.banner.hide()

        self.table = QTableWidget(0, len(COLUMNS))
        # A wrapped prompt can exceed the viewport: every part must be scrollable.
        self.table.setVerticalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
        self.table.setHorizontalHeaderLabels(COLUMNS)
        # R4: the Result column is painted as a badge. Ticket 39 review: the delegate is set
        # on that column alone rather than on the whole table, so every other cell (Step,
        # Actual, Message) keeps Fusion's ordinary drawing without the delegate having to
        # recognise text that is not a result.
        self.table.setItemDelegateForColumn(2, ResultBadgeDelegate())
        for index, width in enumerate(_COLUMN_WIDTHS):
            self.table.setColumnWidth(index, width)
        # The Message column takes the remaining pane width. The fixed columns still
        # require horizontal scrolling in windows narrower than 1280 px.
        self.table.horizontalHeader().setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        # Rows are named by their Step cell, not by a row number: a visible vertical header
        # would print a number beside every step, input and prompt row (packet 25a review).
        self.table.verticalHeader().setVisible(False)
        # Packet 40b (issue #3): the Actual, Expected, Message and step-name cells word-wrap
        # and the rows grow to fit them (``_fit_rows``), and the table is read-only: it mirrors
        # what the engine wrote, and an Operator must never be able to rewrite a result in it.
        # The Enable column is a cell widget, so ``NoEditTriggers`` leaves its boxes clickable.
        # The text columns take :class:`WrappedTextDelegate`: the default delegate wraps only at
        # spaces, so a 300-character device id in an 80 px column ran out of the cell.
        self.table.setWordWrap(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for column in _TEXT_COLUMNS:
            self.table.setItemDelegateForColumn(column, WrappedTextDelegate())
        self.refit_passes = 0
        self._width_fit = _IncrementalRowFit(self.table, self._fit_width_row)
        self._refit_timer = self._width_fit._timer
        self._refit_timer.timeout.disconnect()
        self._refit_timer.timeout.connect(self._refit_now)
        self.table.horizontalHeader().sectionResized.connect(self.refit_all)

        layout.addWidget(self.schedule_label)
        layout.addWidget(self.banner)
        self._table_index = layout.addWidget(self.table, stretch=1)

        #: Every declared Step Input of the loaded schedule, under the step that declared it.
        self.input_widgets: dict[int, list[StepInputWidget]] = {}
        #: Each step row's enable box (packet 15d: headers derive theirs from their children).
        self.checks: dict[int, QCheckBox] = {}
        self.enabler: Optional[StepEnabler] = None
        self._rows: list[object] = []
        self._step_rows: dict[int, int] = {}
        self._prompt_rows: dict[int, int] = {}
        self._prompt_bars: dict[int, FailurePromptBar] = {}
        #: Per step row, the cell texts and column widths the last measurement saw: a row is
        #: measured only when one of them changes (packet 40b performance fix).
        self._row_sizes: dict[int, tuple] = {}
        #: How many rows the last ``_fit_rows`` pass actually measured, kept so a test can
        #: check that an update of one row does not re-measure the whole table.
        self.measured_rows = 0
        #: The step whose Attempt is running (R4 emphasis) and the step the pause is on, as
        #: last drawn — enough to repaint a row's tint without reading the store again.
        self._current_seq: Optional[int] = None
        self._paused_in_table: Optional[int] = None

    # -- building the rows --------------------------------------------------

    def load_steps(self, steps: Iterable[object]) -> None:
        """Build the table for ``steps``: a row per step, its inputs, then its hidden prompt row."""
        self._rows = list(steps)
        self.enabler = StepEnabler(self._rows, self.session)
        self.input_widgets = {}
        self.checks = {}
        self._step_rows = {}
        self._prompt_rows = {}
        self._prompt_bars = {}
        table = self.table
        # The previous schedule's rows are gone for good, so their widgets may be destroyed here;
        # nothing mid-run ever reaches this path (packet 10a review).
        for row in range(table.rowCount()):
            for column in range(table.columnCount()):
                widget = table.cellWidget(row, column)
                if widget is None:
                    continue
                # Qt leaves a removed cell widget parented to the viewport, so a rebuild would leave
                # the old enable boxes painted over the new rows, exactly as a QListWidget.clear()
                # leaves orphan row widgets behind (packet 05b review).
                table.removeCellWidget(row, column)
                widget.setParent(None)
                widget.deleteLater()
        table.clearSpans()
        table.setRowCount(0)

        row = 0
        for seq, step in enumerate(self._rows):
            is_header = bool(getattr(step, "is_header", False))
            table.insertRow(row)
            box = QCheckBox()
            box.setToolTip("Include this step in the Test Run" if not is_header else "Set every step under this header")
            box.setCheckState(self.enabler.box_state(seq))
            box.setEnabled(True)
            # Packet 15d: a header box is derived, and clicking it sets every step beneath it.
            box.toggled.connect(lambda state, seq=seq: self._toggle_step(seq, state))
            table.setCellWidget(row, 0, box)
            self.checks[seq] = box

            name = QTableWidgetItem(row_label(getattr(step, "step_no", ""), getattr(step, "name", ""), int(getattr(step, "depth", 0))))
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if is_header:
                name.setBackground(QColor(_HEADER_BG))
                font = QFont(name.font())
                font.setBold(True)
                name.setFont(font)
            table.setItem(row, 1, name)
            self._step_rows[seq] = row
            row += 1

            for declaration in getattr(step, "_validated_inputs", None) or []:
                if self.session is None:
                    continue
                table.insertRow(row)
                table.setSpan(row, 0, 1, len(COLUMNS))
                widget = StepInputWidget(declaration, self.session, seq, owner=self.screen)
                table.setCellWidget(row, 0, widget)
                table.setRowHeight(row, widget.sizeHint().height())
                self.input_widgets.setdefault(seq, []).append(widget)
                row += 1

            if not is_header:
                # The prompt row exists from the start so a pause never has to insert a row and
                # shift the steps below it out from under their input widgets.
                table.insertRow(row)
                table.setSpan(row, 0, 1, len(COLUMNS))
                table.setRowHeight(row, 0)
                table.setRowHidden(row, True)
                self._prompt_rows[seq] = row
                row += 1

        table.setRowCount(row)
        self._row_sizes.clear()  # a new schedule: every row is measured again
        self._fit_rows()
        self.schedule_label.setText(
            f"{self.session.selected_schedule or ''}" if self.session is not None else ""
        )

    # -- painting the run ----------------------------------------------------

    def cell(self, row: int, column: int) -> str:
        """The text of one table cell, "" when it holds no item."""
        item = self.table.item(row, column)
        return item.text() if item is not None else ""

    def step_row(self, node_seq: int) -> Optional[int]:
        """The table row standing for the step at ``node_seq``."""
        return self._step_rows.get(node_seq)

    def input_widget(self, node_seq: int, index: int) -> Optional[StepInputWidget]:
        """The widget for the n-th (1-based) Step Input of the step at ``node_seq``, if any."""
        for widget in self.input_widgets.get(node_seq, ()):
            if widget.index == index:
                return widget
        return None

    def show_state(
        self,
        nodes: list[dict],
        attempts: list[dict],
        paused_seq: Optional[int] = None,
        paused_result: str = "",
        paused_message: str = "",
        paused_actual: str = "",
        paused_expected: str = "",
    ) -> None:
        """Write the recorded Results, Attempts, Actual, Expected and Message into the table.

        Everything here is read from the rows the engine wrote: a step with no node row yet simply
        has no Result. ``paused_seq`` marks the step a Stop-on-Fail pause is waiting on: its row is
        highlighted and its inline prompt shown; every other prompt row is hidden."""
        if self.enabler is None:
            return
        self._paused_in_table = paused_seq
        by_seq = {node["node_seq"]: node for node in nodes if node.get("node_seq") is not None}
        by_step: dict[int, list[dict]] = {}
        for attempt in attempts:
            by_step.setdefault(attempt["node_seq"], []).append(attempt)

        shown_results: list[str] = []
        for seq, row in self._step_rows.items():
            if self.enabler.is_header(seq):
                rollup = rollup_text(seq, self.enabler.children, self.enabler.is_header,
                                     lambda other: by_seq.get(other, {}).get("result"))
                self._set_cell(row, 2, rollup)
                continue
            node = by_seq.get(seq)
            tried = sorted(by_step.get(seq, ()), key=lambda a: a.get("attempt_no", 0))
            last = tried[-1] if tried else None
            result = paused_result if seq == paused_seq else (node.get("result") if node else "")
            attempts_count = (node.get("attempt_count") if node else 0) or len(tried)
            self._set_cell(row, 2, str(result or ""))
            shown_results.append(str(result or ""))
            # An unrun step has no Attempt to count: its cell stays blank rather than "0".
            self._set_cell(row, 3, str(attempts_count) if attempts_count else "")
            self._set_cell(row, 4, str(last.get("actual") or "") if last else "")
            self._set_cell(row, 5, str(last.get("expected") or "") if last else "")
            self._set_cell(row, 6, str(last.get("msg") or "") if last else "")
            self._highlight(row, seq == paused_seq, current=seq == self._current_seq)
        # Ticket 39 review: the Result column is widened to fit the widest badge the run
        # actually shows, measured from the badge's own font metrics, so "NOREPORT" and
        # "FORCED_NOTRUN" keep their words instead of being painted as a glyph alone. The
        # Message column is the stretch column, so a wide Result column costs it, not the
        # table's fit in the pane.
        widest = max((badge_width_for(value) for value in shown_results if value), default=_RESULT_COLUMN_MIN)
        self.table.setColumnWidth(2, max(_RESULT_COLUMN_MIN, widest + 8))
        self._fit_rows(text_only=True)
        self._paint_prompt(paused_seq, paused_result, paused_message, paused_actual, paused_expected)

    def refit_all(self) -> None:
        """Cancel stale width work and schedule visible rows plus a bounded chunk."""
        if getattr(self, "_width_fit", None) is not None:
            self._width_fit.restart()

    def _fit_width_row(self, row):
        if row in self._row_sizes:
            self._fit_rows((row,))

    def _refit_now(self) -> None:
        self.refit_passes += 1
        self._width_fit.tick()

    def _cell_width(self, row: int, column: int) -> int:
        """How wide Qt paints this cell — the section less the grid line drawn inside it.

        `columnWidth()` is the section's width, but the delegate is handed
        `visualRect(index)`, which is one grid line narrower. Measuring the wrap against the
        wider number under-counts the lines: a 60 px column measured at 60 px sized the row
        for 244 px while the text painted in the 59 px cell needed 252 (40b review, second round)."""
        return max(1, self.table.visualRect(self.table.model().index(row, column)).width())

    def _row_signature(self, row: int) -> tuple:
        """What the row's text columns are measured against: their text, font and width.

        A row is re-measured when any of them changes, which is what makes an update of one
        step measure one row instead of the table (packet 40b performance fix).

        The font is the whole font, not its bold flag and point size: a pixel size (13 px on a
        9 pt font) or a family change leaves the point size alone, and the row then keeps the
        height measured for the old type — 198 px of row for text that needs 781 (40b review,
        second round)."""
        table = self.table
        parts = []
        for column in _TEXT_COLUMNS:
            item = table.item(row, column)
            if item is None:
                continue
            font = item.font()
            parts.append((self._cell_width(row, column), item.text(), font.key(), font.pixelSize()))
        return tuple(parts)

    def _fit_rows(self, rows=None, *, text_only=False) -> int:
        """Size each step row to the wrapped text its cells hold (packet 40b).

        The height comes from the cell delegate's own `sizeHint` at the width the cell is
        painted in, which is the same measurement the delegate uses to draw. A row with
        only short lines keeps ``SHORT_STEP_ROW_HEIGHT``, and a row that had grown shrinks
        back when its column widens or its text is replaced.

        Rows whose text, font and cell widths are unchanged since the last pass are not
        measured at all: ``show_state`` runs after every Attempt, and measuring 1,000 rows
        each time cost a second. Returns how many rows were measured, also kept in
        ``measured_rows``."""
        table = self.table
        measured = 0
        for row in (self._step_rows.values() if rows is None else rows):
            signature = self._row_signature(row)
            previous = self._row_sizes.get(row)
            if previous == signature:
                continue
            # Attempts must not synchronously drain unrelated width work mid-drag.
            if text_only and previous is not None and tuple(part[1:] for part in previous) == tuple(part[1:] for part in signature):
                continue
            self._row_sizes[row] = signature
            measured += 1
            height = SHORT_STEP_ROW_HEIGHT
            for column in _TEXT_COLUMNS:
                item = table.item(row, column)
                if item is None:
                    continue
                delegate = table.itemDelegateForColumn(column) or table.itemDelegate()
                option = QStyleOptionViewItem()
                option.rect = QRect(0, 0, self._cell_width(row, column), SHORT_STEP_ROW_HEIGHT)
                option.text = item.text()
                option.font = item.font()
                hint = delegate.sizeHint(option, table.model().index(row, column))
                height = max(height, hint.height())
            table.setRowHeight(row, height)
        self.measured_rows = measured
        return measured

    def _prompt_row_height(self, seq: int) -> int:
        """How tall the prompt row has to be for the bar it holds (40b fix).

        The style's content rectangle supplies the cell padding. Clamp that width to
        the viewport before measuring, so a span wider than the visible columns cannot
        place controls offscreen. This also works before Qt lays out the cell widget.
        """
        bar = self._prompt_bars.get(seq)
        if bar is None:
            return SHORT_STEP_ROW_HEIGHT
        # Column spans can exceed the viewport in a narrow window. Keep the prompt's
        # controls accessible without horizontal scrolling.
        bar.setMaximumWidth(max(1, self.table.viewport().width() - _INLINE_PROMPT_VIEWPORT_INSET))
        span = self.table.visualRect(self.table.model().index(self._prompt_rows[seq], 0))
        option = QStyleOptionViewItem()
        option.initFrom(self.table)
        option.rect = span
        content = self.table.style().subElementRect(QStyle.SubElement.SE_ItemViewItemText, option, self.table)
        pad_y = max(0, content.top() - span.top())
        width = max(1, min(content.width(), bar.maximumWidth()))
        bar.sync_height_at(width)
        return max(SHORT_STEP_ROW_HEIGHT, bar.heightForWidth(width) + 2 * pad_y)

    def _fit_prompt_rows(self) -> None:
        """Give a shown inline prompt the height its wrapped message needs (packet 40b fix)."""
        if getattr(self, "_fitting_prompt", False):
            return
        self._fitting_prompt = True
        try:
            for seq, row in self._prompt_rows.items():
                if not self.table.isRowHidden(row):
                    self.table.setRowHeight(row, self._prompt_row_height(seq))
        finally:
            self._fitting_prompt = False

    def _set_cell(self, row: int, column: int, text: str) -> None:
        """Write one cell, reusing the item that is already there.

        A fresh item is created without `ItemIsEditable`: the table mirrors what the store
        wrote and the Operator can never rewrite a result in it (packet 40b, issue #3)."""
        item = self.table.item(row, column)
        if item is None:
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, column, item)
        else:
            item.setText(text)

    def _highlight(self, row: int, paused: bool, current: bool = False) -> None:
        """Tint a step row: the failed red for the step a pause is waiting on (packet 12d),
        the primary tint for the step whose Attempt is running (ticket 39, R4).

        An unset `QColor` paints the cell black rather than leaving it alone, so clearing means
        putting the plain row colour back."""
        fill = QColor(_FAILED_BG) if paused else (QColor(_CURRENT_BG) if current else QColor(_ROW_BG))
        for column in range(self.table.columnCount()):
            item = self.table.item(row, column)
            if item is None:
                continue
            item.setBackground(fill)
        # The emphasized step's name is bold as well, so the tint is not the only cue.
        name = self.table.item(row, 1)
        if name is not None:
            font = QFont(name.font())
            font.setBold(bool(paused or current))
            name.setFont(font)

    def mark_current(self, node_seq: Optional[int]) -> None:
        """Ticket 39 (R4): the step whose Attempt just started takes the primary tint and a
        bold name; every other step row goes back to plain (a paused one keeps its red)."""
        if self.enabler is None:
            return
        self._current_seq = node_seq
        for seq, row in self._step_rows.items():
            if self.enabler.is_header(seq):
                continue
            self._highlight(row, seq == self._paused_in_table, current=seq == node_seq)

    def _paint_prompt(self, seq: Optional[int], result: str, message: str,
                      actual: str = "", expected: str = "") -> None:
        """Show the inline prompt under the paused step, and the sticky banner above the table."""
        for other, row in self._prompt_rows.items():
            if other == seq:
                continue
            self.table.setRowHidden(row, True)
        if seq is None:
            self.banner.hide()
            return
        row = self._prompt_rows.get(seq)
        if row is None:
            return
        bar = self._prompt_bars.get(seq)
        if bar is None:
            bar = FailurePromptBar(_INLINE_PROMPT_STYLE)
            bar.choice_made.connect(self.prompt_choice.emit)
            bar.geometry_changed.connect(self._fit_prompt_rows)
            self._prompt_bars[seq] = bar
            self.table.setCellWidget(row, 0, bar)
        number = getattr(self._rows[seq], "step_no", "") if 0 <= seq < len(self._rows) else ""
        # Ticket 39 (D1): the prompt states the measurements as well as the message.
        bar.show_prompt(str(number or ""), result, message, actual=actual, expected=expected)
        # Packet 40b review: the row must be as tall as the bar's *wrapped* message. A
        # word-wrapping label grows its own hint, but `sizeHint()` is the bar laid out at its
        # preferred width — the spanned row is narrower than that, and the last lines of a
        # long Actual were cut off by the row boundary. `_prompt_row_height` measures the row
        # against the bar as Qt actually lays it out in the row.
        self.table.setRowHeight(row, self._prompt_row_height(seq))
        self.table.setRowHidden(row, False)
        self.banner.setText(f"Step {number} {result}: choose Continue, Retry or Abort below")
        self.banner.show()

    def clear_pause(self) -> None:
        """Drop the inline prompt and the banner once the Operator has answered."""
        self._paused_in_table = None
        self._paint_prompt(None, "", "")
        for seq, row in self._step_rows.items():
            if self.enabler is not None and self.enabler.is_header(seq):
                continue
            self._highlight(row, False, current=seq == self._current_seq)

    def close_inputs(self) -> None:
        """Close every Step Input: nothing is being asked for, so nothing can be answered."""
        for widgets in self.input_widgets.values():
            for widget in widgets:
                widget.deactivate()

    def _toggle_step(self, node_seq: int, state: bool) -> None:
        """One box was ticked: the enabler applies it (packet 15d) and the derived boxes follow.

        `QCheckBox.toggled` reports a plain bool, not a `Qt.CheckState`: comparing it against
        `CheckState.Checked` is always False, so every click read as un-ticking and a box could
        never be turned back on (packet 25a review).

        Re-syncing the headers around a toggle repaints their boxes, and a repaint must not read as
        another Operator toggle, so the enabler's recorded state is compared first."""
        if self.screen is not None and not self.screen.is_active():
            return  # the layout that is not showing cannot change what the next run runs (packet 25b)
        if self.enabler is None or not self.checks[node_seq].isEnabled():
            return
        checked = bool(state)
        if self.enabler.states.get(node_seq) == checked:
            return
        self.enabler.states[node_seq] = checked
        for other in self.enabler.toggle(node_seq, checked):
            box = self.checks.get(other)
            if box is not None:
                box.setCheckState(self.enabler.box_state(other))

    def set_checkable(self, checkable: bool) -> None:
        """Lock the step boxes while a run is in progress (packet 11b)."""
        for box in self.checks.values():
            box.setEnabled(checkable)


class ThreePaneRunScreen(RunScreenMixin, QWidget):
    """A three-pane Run screen bound to one :class:`HeadlessRunSession`.

    The same session, the same actions: Start and Abort live in the left pane, a Stop-on-Fail pause
    is answered by the inline prompt in the centre pane, and the status bar at the bottom carries the
    run's state, its tallies and its timers. The run-side handlers themselves are the shared
    :class:`~ate.ui.run_widgets.RunScreenMixin` (packet 25a review); this class only arranges panes.
    """

    def __init__(
        self,
        session: HeadlessRunSession,
        confirm_choice: Optional[Callable[[MismatchConfirmation], None]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        debug_window: Optional[DebugWindow] = None,
    ) -> None:
        """``debug_window`` (ticket 39 review) is the window's one shared Debug window: both
        layouts use it, so switching layouts never leaves a second one open and closing the
        main window closes it with the rest of the application."""
        super().__init__()
        self.session = session
        self.state = SessionState.IDLE
        # Injectable wall clock (packet 14d): the status bar's timers read it, so a test can advance
        # time on its own terms instead of waiting for real seconds.
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # Injected decision-maker for the run-start confirmation (ticket 06c).
        self.confirm_choice = confirm_choice
        self._confirm_dialog: Optional[MismatchConfirmation] = None
        # Captured when STARTED fires; ``None`` before the first run or after FINISHED.
        self._started_at: Optional[datetime] = None
        self._item_started_at: Optional[datetime] = None
        self._phase_text = "Ready"
        self._pre_counts: tuple[int, int] = (0, 0)
        self._post_counts: tuple[int, int] = (0, 0)
        self._started_steps: set[int] = set()
        self._step_total = 0
        self._engine: Optional[RunEngine] = None
        # The step a Stop-on-Fail pause is waiting on, and what it reported.
        self._paused_seq: Optional[int] = None
        self._paused_result = ""
        self._paused_message = ""
        self._paused_actual = ""
        self._paused_expected = ""
        self._steps: list[object] = []
        # Ticket 39 (D4): state the header chip needs — which blocking Step Inputs are waiting,
        # whether the Post-test phase has begun, and which step's Attempt is running.
        self._waiting_inputs: set[tuple[int, int]] = set()
        self._post_test_started = False
        self._current_seq: Optional[int] = None

        # -- the three panes -----------------------------------------------
        self.picker = SchedulePicker(session, inline_mismatches=True)
        self.fields = RunFieldsForm()
        self.stop_on_fail_radio = QRadioButton("Stop on first failure")
        self.continue_all_radio = QRadioButton("Run every step regardless of failures")
        self.stop_on_fail_radio.setChecked(True)
        failure_group = QGroupBox("On failure")
        failure_layout = QVBoxLayout(failure_group)
        failure_layout.addWidget(self.stop_on_fail_radio)
        failure_layout.addWidget(self.continue_all_radio)
        self.error_label = QLabel("")
        self.error_label.setObjectName("run_error")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {_BROKEN_ERR}; font-size: 12px;")
        self.start_button = QPushButton("Start")
        self.start_button.setObjectName("primary")
        self.abort_button = QPushButton("Abort")
        self.abort_button.setObjectName("danger")
        self.abort_button.setEnabled(False)
        # Packet 30b review: variant B's finished header offers "New run". The three-pane layout has
        # no run header of its own — its Start and Abort sit in the setup pane — so the next run is
        # offered there, with the same semantics as the staged button.
        self.new_run_button = QPushButton("New run")
        # Packet 41 review: like the staged layout's New run, this is a run control, so the theme's
        # `#primary` rule (and its `:disabled` fade) has to reach it by object name.
        self.new_run_button.setObjectName("primary")
        self.new_run_button.setEnabled(False)
        # Ticket 39 review: Abort and New run must not be the window's Enter-key default. A stray
        # Enter from a scanner, or an Operator pressing Enter to leave a field, would end a run or
        # start one they did not mean.
        _no_auto_default(self.abort_button, self.new_run_button)
        self.counts = ResultCounts()
        self.counts.hide()

        # Ticket 39 (R2, D2): the shared run header above the panes — state chip, schedule,
        # UUT/Operator, run timer — and Debug as its own window opened from the header.
        self.run_header = RunHeader()
        #: One Debug window for the window (ticket 39 review): MainWindow passes the same one to
        # both layouts. Left as a separate instance per layout, a layout switch opened a second
        # window and closing ATE left the layouts' parentless windows behind.
        self.debug_window = debug_window if debug_window is not None else DebugWindow()

        left = QWidget()
        # A low floor, not a demand: at 1280x800 the pane gets 320 px, and a bigger minimum would
        # make the splitter take width from the step table, which then needs a sideways scrollbar
        # (packet 25a review).
        left.setMinimumWidth(240)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.addWidget(self.picker, stretch=1)
        left_layout.addWidget(self.fields)
        left_layout.addWidget(failure_group)
        left_layout.addWidget(self.error_label)
        run_row = QHBoxLayout()
        run_row.addWidget(self.start_button, stretch=1)
        run_row.addWidget(self.abort_button)
        run_row.addWidget(self.new_run_button)
        left_layout.addLayout(run_row)
        left_layout.addWidget(self.counts)

        self.table = StepTable(session, screen=self)
        # R3: the right pane is the Log band — live from the start. Packet 42 (issue #3): the
        # Summary tab repeated the centre table's verdict and per-step results, so it is gone, and
        # a pane with no tab bar holds the log directly and says what it is. Debug is not a pane
        # any more (D2): it is its own window, opened from the header's button or Ctrl+D.
        self.log_pane = LogPane("Log", "INFO and above")
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 8, 8, 8)
        right_layout.addWidget(self.log_pane, stretch=1)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(left)
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(right)
        # 25/50/25 of a 1280 px window: the setup pane and the log each take a quarter.
        self.splitter.setSizes([320, 640, 320])
        self.left_pane, self.centre_pane, self.right_pane = left, self.table, right

        self.status_bar = StatusBar()
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.addWidget(self.run_header)
        root.addWidget(self.splitter, stretch=1)
        root.addWidget(self.status_bar)

        self.run_header.debug_button.clicked.connect(self.toggle_debug)
        QShortcut(QKeySequence("Ctrl+D"), self, self.toggle_debug)

        # Prefill Project from config's `defaultProject` (spec 08). The Station name is not a
        # Project and never lands in this field.
        if self.session.config is not None:
            self.fields.project_edit.setText(self.session.config.default_project or "")
        # A Test Run cannot start with an empty Project, so the field gates Start (packet 30a r3).
        # Packet 41: the other three required fields gate it as well. Packet 41 fix2: typing in any of
        # them is also the input change that drops a refusal the Start path wrote.
        for edit in (self.fields.project_edit, self.fields.operator_edit, self.fields.uut_type_edit, self.fields.serial_edit):
            edit.textChanged.connect(self._start_inputs_changed)

        # The picker owns the selection: it records it on the session and this screen follows it.
        self.picker.selection_changed.connect(self._on_selection)
        self.start_button.clicked.connect(self.begin_run)
        self.abort_button.clicked.connect(self._request_abort)
        self.new_run_button.clicked.connect(self.start_new_run)
        self.table.prompt_choice.connect(self._on_prompt_choice)

        self.session.state_changed.connect(self._on_state_changed)
        self.session.failure_prompt.connect(self._on_failure_prompt)
        # The pause ends when the session says it ends (packet 25b): a prompt answered in the other
        # layout has to clear this one's banner, prompt and "Paused" as well.
        self.session.prompt_answered.connect(self._on_prompt_answered)
        self.session.action_started.connect(self._on_action_started)
        self.session.step_started.connect(self._on_step_started)
        self.session.input_asked.connect(self._on_input_asked)
        self.session.input_answered.connect(self._on_input_answered)
        self.session.step_finished.connect(self._on_step_finished)
        # Packet 40a: the worker announces every recorded log line on this signal; the pane reads
        # the new rows from the store the moment they land, not when the step ends.
        self.session.log_appended.connect(self._on_log_appended)

        self.picker.refresh()
        self._update_start_button()

        # A one-second timer keeps the status bar's elapsed times live without polling the worker.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_header)
        self._timer.start(1000)

        # A screen built without a window is the layout that acts: MainWindow names which of the two
        # layouts acts when it builds both for one session (packet 25b).
        self.set_active(True)

    # -- setup ------------------------------------------------------------

    def _on_selection(self, name: str) -> None:
        """The picker picked a schedule: show its steps in the centre pane, boxes still editable.

        While a run is up the centre pane belongs to the schedule that started it: rebuilding it here
        would throw away the rows the worker is writing and the Step Input widgets sitting on them
        (packet 25a review). A pick made mid-run is recorded on the session for the next run and
        ignored by this layout."""
        if self.state == SessionState.STARTED:
            return
        if self._carrying_over:
            # MainWindow re-rendered this picker as part of a layout switch (packet 25b): the table
            # keeps whatever the run in view put in it, and the setup preview is carried over by hand.
            self._start_inputs_changed()
            return
        if not name:
            self.table.load_steps([])
            self._steps = []
        else:
            self._steps = load_schedule_steps(self.session, name)
            self.table.load_steps(self._steps)
            self.table.set_checkable(self.session.state in (SessionState.IDLE, SessionState.SELECTING))
            self._apply_stop_on_fail(self.session.stop_on_fail)
        self._start_inputs_changed()

    def start_new_run(self) -> None:
        """The three-pane "New run" (packet 30b review): unlock the session and show the setup.

        The setup pane is always on screen here, so the layout change is the schedule's step list:
        the centre pane goes back to the picked schedule's editable boxes instead of the finished
        run's results, and the tallies and log start from nothing. Nothing the Operator typed is
        cleared (spec 37).

        Packet 41 review: with nothing picked there is no schedule to prepare, so the run on screen
        stays as it is. A direct call to this method must not empty the table, the counts and the
        log for a schedule nobody chose. Packet 42 review, item 4: "stays as it is" includes the
        Enable boxes — the rows on screen belong to a run that is over and there is no schedule to
        apply them to, so they are locked until one is picked."""
        if not self.new_run():
            return
        schedule = self.session.selected_schedule
        if not schedule:
            self.table.set_checkable(False)
            return
        self._steps = load_schedule_steps(self.session, schedule)
        self.table.load_steps(self._steps)
        self.table.set_checkable(True)
        self.counts.set_counts({})
        self.counts.hide()
        self._stop_log_timer()  # a read queued by the run just finished must not refill the panes
        self.log_pane.clear()
        self.debug_window.clear()
        self.table.banner.hide()

    def _apply_stop_on_fail(self, value: bool) -> None:
        """Set the "On failure" pair to the picked schedule's :setStopOnFail (packet 12d)."""
        self.stop_on_fail_radio.setChecked(value)
        self.continue_all_radio.setChecked(not value)

    def _update_start_button(self) -> None:
        """Start is available only while :meth:`can_start` says a run can begin.

        A broken row carries no name, so it can never be the selection a run starts from (packet
        05b); a blank Project refuses the run too (spec 08); and packet 41 adds the other three
        required fields and the rule that no run may already be going. The reason goes on the
        error line."""
        can_start = self.can_start()
        self.start_button.setEnabled(can_start)
        # New run returns to setup so missing fields can be repaired.
        self.new_run_button.setEnabled(self.state == SessionState.FINISHED)
        self._show_error(self._start_problem(), "start")

    def _refresh_start_state(self) -> None:
        """This layout's Start refresh, under the mixin's one name (packet 41 fix2)."""
        self._update_start_button()

    def _request_abort(self) -> None:
        """Abort the run: answer a pending prompt, or stop at the next step boundary."""
        if not self.is_active():
            return  # the layout that is not showing cannot abort the run (packet 25b)
        if self.session.pause_pending():
            # The run is paused on a prompt (packet 25b): the session says so, not this pane's banner,
            # which can be stale if the choice was made in the other layout.
            self._on_prompt_choice("ABORT")
            return
        self.session.request_abort()

    @property
    def engine(self) -> Optional[RunEngine]:
        """The RunEngine worker for the current run, or ``None`` before it starts / after FINISHED."""
        return self._engine

    # -- state ------------------------------------------------------------

    def _on_state_changed(self, state: SessionState) -> None:
        """React to a session state change — the only place run truth reaches the layout."""
        self.state = state
        if state == SessionState.STARTED:
            steps = load_schedule_steps(self.session, self.session.selected_schedule or "")
            if steps:
                self._steps = steps
                self.table.load_steps(steps)
            self._started_at = self._clock()
            self._item_started_at = None
            self._phase_text = "Running"
            # Ticket 39 review: the header reads the run row, so a new run starts from a fresh one.
            # Ticket 39 review, second pass: a deferred read of the run that finished before this one
            # goes with it, so the layout that was hidden through it shows the run that is running.
            self.invalidate_run_row()
            self._pre_counts = (0, 0)
            self._post_counts = (0, 0)
            self._started_steps = set()
            self._reset_live_log_state()  # the Action the last run ended in is not this run's (packet 40a fix 2)
            self._step_total = self._enabled_step_count(steps)
            # Ticket 39 (D4): a fresh run starts with nothing waiting and no Post-test yet.
            self._waiting_inputs = set()
            self._post_test_started = False
            self._current_seq = None
            self._timer.start(1000)
            # A new run tallies from nothing: the previous run's numbers stay on the grid otherwise
            # (packet 25a review), and the log panes start empty too.
            self.counts.set_counts({})
            # Ticket 39 review: the live panes append rather than rewrite, so a new run starts by
            # emptying them — the log the Operator sees is the run they are looking at (30b). The
            # coalesced log read still pending (packet 40a review, item 3) is cancelled with them:
            # it was started for the run just finished, and firing later would refill these panes
            # with that run's lines.
            self._stop_log_timer()
            self.log_pane.clear()
            self.debug_window.clear()
            self.counts.show()
        elif state == SessionState.FINISHED:
            self._engine = None
            # Ticket 39 review: the finished verdict and End Reason are read from the run row, so
            # the row cached while the run was up is dropped before it is read again.
            self.invalidate_run_row()
            self._paused_seq = None
            self._paused_result = ""
            self._paused_message = ""
            # Ticket 39 review: the pause's Actual/Expected pair is part of the pause state, so
            # it is emptied with the rest of it. Left behind, a later deferred read would paint
            # a finished run's row with the measurements of a pause that was answered long ago.
            self._paused_actual = ""
            self._paused_expected = ""
            self.table.clear_pause()
            # Ticket 39 review: the run is over, so no step is "the one running" any more. Without
            # this the last step's row kept the primary tint after FINISHED, which reads as though
            # the run were still on it (R4 tints only the running and paused rows).
            self.table.mark_current(None)
            # Nothing can be asked for any more, so every Step Input closes: a widget left live
            # after FINISHED answers against a released engine (packet 25a review).
            self.table.close_inputs()
            run_uuid = self.session.run_uuid
            if self.is_active():
                nodes = self.session.store.nodes(run_uuid) if run_uuid else []
                attempts = self.session.store.attempts(run_uuid) if run_uuid else []
                self.table.show_state(nodes, attempts)
                self._reset_live_log_state()  # the run is over: no Action is running any more (packet 40a fix 2)
                self._refresh_log()
                self._update_counts(nodes)
            else:
                # The layout that is not showing defers the read and draws it when it comes back
                # (packet 25b review); its inputs are already closed above, so it answers nothing.
                self.defer_finished_run(run_uuid)
            self._phase_text = "Finished"
        elif state in (SessionState.IDLE, SessionState.SELECTING):
            self.table.clear_pause()
            self._paused_seq = None

        # Step boxes: editable in setup, locked while a run is up (packet 11b). Packet 42 review,
        # item 4: they are also locked when nothing is picked — a SELECTING session with no schedule
        # still has the finished run's rows in the table, and ticking those boxes edits a run that
        # is over rather than the next one.
        self.table.set_checkable(
            state in (SessionState.IDLE, SessionState.SELECTING) and bool(self.session.selected_schedule)
        )
        self.abort_button.setEnabled(state == SessionState.STARTED)
        # Packet 30b review: the next run is offered once this one is over. Packet 41: a run in
        # progress is the only thing that holds New run back; an incomplete setup is a message on
        # the error line, not a lock on the button.
        self.new_run_button.setEnabled(state == SessionState.FINISHED)
        # Packet 41 fix2: the session moving state is an input change too, so a refusal written
        # against the old state is dropped before Start is re-checked.
        self._start_inputs_changed()
        self._update_header(state)
        if state == SessionState.FINISHED:
            self._timer.stop()

    def _update_header(self, state: Optional[SessionState] = None, now: Optional[datetime] = None) -> None:
        """Refresh the status bar's timers (packet 14d) and the shared header's chip (ticket 39, R2)."""
        if now is None:
            now = self._clock()
        self.status_bar.set_progress(
            "Paused" if self._paused_seq is not None else self._phase_text,
            self._pre_counts,
            len(self._started_steps),
            self._step_total,
            self._post_counts,
            self._started_at,
            self._item_started_at,
            now,
        )
        self._refresh_run_header(now)

    def _sync_from_store(self) -> None:
        """Redraw the table, the counts and the log from whatever the store has recorded."""
        self._deferred_run_uuid = None
        run_uuid = self.session.run_uuid
        if not run_uuid or not self._store_open():
            return
        nodes = self.session.store.nodes(run_uuid)
        attempts = self.session.store.attempts(run_uuid)
        self.table.show_state(nodes, attempts, self._paused_seq, self._paused_result,
                              self._paused_message, self._paused_actual, self._paused_expected)
        self._update_counts(nodes)
        self._refresh_log()

    def _update_counts(self, nodes: list[dict]) -> None:
        """The live Result tallies: every step Result the store has recorded, by value.

        A step paused on a Stop-on-Fail failure has no node row yet (the engine writes it after the
        Operator's choice), so its pending Result is counted from the prompt the screen is showing."""
        recorded = {node.get("node_seq") for node in nodes}
        tallies = Counter(
            node.get("result") for node in nodes
            if node.get("kind") == "step" and node.get("result")
        )
        if self._paused_seq is not None and self._paused_seq not in recorded and self._paused_result:
            tallies[self._paused_result] += 1
        self.counts.set_counts(dict(tallies))

    # -- Stop-on-Fail prompt (packet 12d) ---------------------------------

    def _on_failure_prompt(self, node_seq: int, attempt_no: int) -> None:
        """A failure is awaiting the Operator's choice: put the prompt under the step it failed on."""
        attempt = self._attempt_row(node_seq, attempt_no)
        result = str(attempt.get("pass_fail") or "FAIL") if attempt else "FAIL"
        message = str(attempt.get("msg") or "") if attempt else ""
        # Ticket 39 (D1): the inline prompt carries the Attempt's Actual/Expected too.
        self._paused_actual = str(attempt.get("actual") or "") if attempt else ""
        self._paused_expected = str(attempt.get("expected") or "") if attempt else ""
        self._paused_seq = node_seq
        self._paused_result = result
        self._paused_message = message
        if not self.is_active():
            self.mark_store_dirty()  # the hidden pane does not read the store for a pause it cannot show
        else:
            self._sync_from_store()
        self._update_header()

    def _on_prompt_answered(self, node_seq: int, attempt_no: int, choice: str) -> None:
        """The session says the pause is over: clear this layout's prompt, banner and "Paused".

        Packet 25b: the prompt may have been answered in the layout the Operator was looking at, and
        the layout that was hidden must not keep a stale prompt that its Abort then answers again."""
        self._paused_seq = None
        self._paused_result = ""
        self._paused_message = ""
        self._paused_actual = ""
        self._paused_expected = ""
        self.table.clear_pause()
        self._phase_text = "Running"
        if self.is_active():
            self._sync_from_store()
        else:
            self.mark_store_dirty()
        self._update_header()

    def _paused_prompt_widget(self):
        """The inline prompt's Continue button while this pane has a prompt up."""
        bar = self.table._prompt_bars.get(self._paused_seq) if self._paused_seq is not None else None
        return bar.continue_button if bar is not None and bar.isVisible() else None

    def _on_prompt_choice(self, choice: str) -> None:
        """A prompt was answered: clear the pause's display, then tell the session."""
        if not self.is_active():
            return  # the hidden layout's inline prompt never answers the pause (packet 25b)
        self._paused_seq = None
        self._paused_result = ""
        self._paused_message = ""
        self._paused_actual = ""
        self._paused_expected = ""
        self.table.clear_pause()
        self._phase_text = "Running"
        self._sync_from_store()
        self._update_header()
        self.session.choose(choice)

    # -- Step Inputs (ticket 10a) ------------------------------------------

    def step_input_widgets(self) -> dict[int, list[StepInputWidget]]:
        """The centre pane's Step Input widgets, keyed by the seq of the step that declared them."""
        return self.table.input_widgets

    def _mark_current_step(self, node_seq: int) -> None:
        """Ticket 39 (R4): tint the row of the step whose Attempt just started and bold its
        name. The layout mirrors the session even while hidden, so this runs either way."""
        self.table.mark_current(node_seq)

    def _loaded_schedule_steps(self) -> list[object]:
        """The steps this layout loaded for the run: the rows the centre table was built from."""
        return list(self._steps)

    def _on_input_asked(self, node_seq: int, attempt_no: int, index: int) -> None:
        """The worker is waiting on this step's n-th input, so its widget becomes live.

        The ask is also when the log pane is most worth reading: whatever the step logged before it
        blocked is already in the store, and the Operator needs it to decide what to type
        (packet 25a review). Ticket 39 (D4): a blocking input waiting on the Operator is what the
        header chip counts as INPUT NEEDED."""
        if self.is_active():
            self._refresh_log()
        else:
            self.mark_store_dirty()  # the hidden pane reads the log when it becomes the visible one
        widget = self.input_widget(node_seq, index)
        if widget is not None:
            widget.activate(attempt_no)
            if widget.declaration.type != "POLL":
                self._waiting_inputs.add((node_seq, index))
                self._refresh_run_header()

    def _on_step_finished(self, node_seq: int) -> None:
        """One step's Attempts are done: close its POLL widgets and redraw the run so far."""
        for widget in self.table.input_widgets.get(node_seq, ()):
            if widget.declaration.type == "POLL":
                widget.deactivate()
        self._waiting_inputs = {waiting for waiting in self._waiting_inputs if waiting[0] != node_seq}
        # The pane that is not showing does not read the store for rows nobody is looking at: it
        # marks itself dirty and syncs once when the Operator switches to it (packet 25b review).
        if not self.is_active():
            self.mark_store_dirty()
        else:
            self._sync_from_store()
