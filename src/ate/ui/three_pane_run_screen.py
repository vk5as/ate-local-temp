"""The three-pane Run screen (packet 25a): left setup pane, centre step table, right log.

Spec line 46-50: the same run session, arranged differently. The left pane carries the Operator's
fields, the filterable schedule list with errors and mismatches inline, the Start/Abort controls and
the live `Result` counts; the centre pane is the steps as a table with Result, Attempts, Actual,
Expected and Message, the Step Inputs under the step that declared them, and — on a Stop-on-Fail
pause — the Continue/Retry/Abort prompt inline under the failed step with a sticky banner above the
table; the right pane is the log. The status bar (packet 14d) runs along the bottom of all three.

Like the staged screen, this layout holds no run logic: every control is bound to the run session,
and everything drawn is a row the store already wrote. The widgets themselves are the shared ones in
``run_widgets``.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ate.engine.run_engine import RunEngine

from ..run_session.headless import HeadlessRunSession, SessionError, SessionState
from .run_widgets import (
    FailurePromptBar,
    LogPane,
    MismatchConfirmation,
    ResultCounts,
    RunFieldsForm,
    RunScreenMixin,
    SchedulePicker,
    StatusBar,
    StepEnabler,
    StepInputWidget,
    _BROKEN_ERR,
    _FAILED_BG,
    _HEADER_BG,
    _INLINE_PROMPT_STYLE,
    _ROW_BG,
    _STICKY_STYLE,
    _rollup_text as rollup_text,
    _row_label as row_label,
    load_schedule_steps,
)

#: The centre pane's columns. `Enable` carries the step's box (packet 15d); the rest are the mockup's
#: `.b-thead` cells (prototype line 100).
COLUMNS = ("Enable", "Step", "Result", "Attempts", "Actual", "Expected", "Message")
#: Widths that fit the 640 px centre pane of a 1280x800 window: every header word fits, and the
#: Message column takes whatever is left.
_COLUMN_WIDTHS = (46, 165, 62, 60, 80, 80, 130)


class StepTable(QWidget):
    """The centre pane's step table: one row per expanded step, its inputs and its prompt under it.

    Rows are built once, when a schedule is picked: a step row, then one row per declared Step Input
    (packet 10a), then a hidden prompt row for that step. Nothing is added or removed while the run
    is up — pausing shows the step's own prompt row and resuming hides it again — so no row carrying
    a widget is ever destroyed mid-run (the Qt 6 lesson from 10a: an item that holds a widget deletes
    it when the item goes, taking the Operator's answers with it).
    """

    #: A Stop-on-Fail prompt answered from one of the inline bars: CONTINUE, RETRY or ABORT.
    prompt_choice = pyqtSignal(str)

    def __init__(self, session: Optional[HeadlessRunSession] = None) -> None:
        super().__init__()
        self.session = session
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
        self.table.setHorizontalHeaderLabels(COLUMNS)
        for index, width in enumerate(_COLUMN_WIDTHS):
            self.table.setColumnWidth(index, width)
        # The Message column takes whatever the pane leaves, so the table never has to scroll
        # sideways and a spanned row (an input, a prompt) always has the full width.
        self.table.horizontalHeader().setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        # Rows are named by their Step cell, not by a row number: a visible vertical header would
        # print a number beside every step, input and prompt row (packet 25a review).
        self.table.verticalHeader().setVisible(False)
        # Text is clipped by its own cell rather than running into the neighbour (`.cell { ellipsis }`).
        self.table.setWordWrap(False)

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
                widget = StepInputWidget(declaration, self.session, seq)
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
    ) -> None:
        """Write the recorded Results, Attempts, Actual, Expected and Message into the table.

        Everything here is read from the rows the engine wrote: a step with no node row yet simply
        has no Result. ``paused_seq`` marks the step a Stop-on-Fail pause is waiting on: its row is
        highlighted and its inline prompt shown; every other prompt row is hidden."""
        if self.enabler is None:
            return
        by_seq = {node["node_seq"]: node for node in nodes if node.get("node_seq") is not None}
        by_step: dict[int, list[dict]] = {}
        for attempt in attempts:
            by_step.setdefault(attempt["node_seq"], []).append(attempt)

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
            self._set_cell(row, 3, str(attempts_count))
            self._set_cell(row, 4, str(last.get("actual") or "") if last else "")
            self._set_cell(row, 5, str(last.get("expected") or "") if last else "")
            self._set_cell(row, 6, str(last.get("msg") or "") if last else "")
            self._highlight(row, seq == paused_seq)
        self._paint_prompt(paused_seq, paused_result, paused_message)

    def _set_cell(self, row: int, column: int, text: str) -> None:
        """Write one cell, reusing the item that is already there."""
        item = self.table.item(row, column)
        if item is None:
            item = QTableWidgetItem(text)
            self.table.setItem(row, column, item)
        else:
            item.setText(text)

    def _highlight(self, row: int, paused: bool) -> None:
        """Give a paused step's row the mockup's `.b-row.failed-hl` tint; clear it otherwise.

        An unset `QColor` paints the cell black rather than leaving it alone, so clearing means
        putting the plain row colour back."""
        for column in range(self.table.columnCount()):
            item = self.table.item(row, column)
            if item is None:
                continue
            item.setBackground(QColor(_FAILED_BG) if paused else QColor(_ROW_BG))

    def _paint_prompt(self, seq: Optional[int], result: str, message: str) -> None:
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
            self._prompt_bars[seq] = bar
            self.table.setCellWidget(row, 0, bar)
        number = getattr(self._rows[seq], "step_no", "") if 0 <= seq < len(self._rows) else ""
        bar.show_prompt(str(number or ""), result, message)
        self.table.setRowHeight(row, bar.sizeHint().height())
        self.table.setRowHidden(row, False)
        self.banner.setText(f"Step {number} {result}: choose Continue, Retry or Abort below")
        self.banner.show()

    def clear_pause(self) -> None:
        """Drop the inline prompt and the banner once the Operator has answered."""
        self._paint_prompt(None, "", "")
        for row in self._step_rows.values():
            self._highlight(row, False)

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
    ) -> None:
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
        self._steps: list[object] = []

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
        self.abort_button = QPushButton("Abort")
        self.abort_button.setEnabled(False)
        self.counts = ResultCounts()
        self.counts.hide()

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
        left_layout.addLayout(run_row)
        left_layout.addWidget(self.counts)

        self.table = StepTable(session)
        self.log_pane = LogPane("Log — INFO and above")
        self.debug_window = LogPane("Debug — every level")
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 8, 8, 8)
        right_layout.addWidget(self.log_pane, stretch=2)
        right_layout.addWidget(self.debug_window, stretch=1)

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
        root.addWidget(self.splitter, stretch=1)
        root.addWidget(self.status_bar)

        # Prefill Project from config's `defaultProject` (spec 08). The Station name is not a
        # Project and never lands in this field.
        if self.session.config is not None:
            self.fields.project_edit.setText(self.session.config.default_project or "")
        # A Test Run cannot start with an empty Project, so the field gates Start (packet 30a r3).
        self.fields.project_edit.textChanged.connect(self._update_start_button)

        # The picker owns the selection: it records it on the session and this screen follows it.
        self.picker.selection_changed.connect(self._on_selection)
        self.start_button.clicked.connect(self.begin_run)
        self.abort_button.clicked.connect(self._request_abort)
        self.table.prompt_choice.connect(self._on_prompt_choice)

        self.session.state_changed.connect(self._on_state_changed)
        self.session.failure_prompt.connect(self._on_failure_prompt)
        self.session.action_started.connect(self._on_action_started)
        self.session.step_started.connect(self._on_step_started)
        self.session.input_asked.connect(self._on_input_asked)
        self.session.input_answered.connect(self._on_input_answered)
        self.session.step_finished.connect(self._on_step_finished)

        self.picker.refresh()
        self._update_start_button()

        # A one-second timer keeps the status bar's elapsed times live without polling the worker.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_header)
        self._timer.start(1000)

    # -- setup ------------------------------------------------------------

    def _on_selection(self, name: str) -> None:
        """The picker picked a schedule: show its steps in the centre pane, boxes still editable.

        While a run is up the centre pane belongs to the schedule that started it: rebuilding it here
        would throw away the rows the worker is writing and the Step Input widgets sitting on them
        (packet 25a review). A pick made mid-run is recorded on the session for the next run and
        ignored by this layout."""
        if self.state == SessionState.STARTED:
            return
        if not name:
            self.table.load_steps([])
            self._steps = []
        else:
            self._steps = load_schedule_steps(self.session, name)
            self.table.load_steps(self._steps)
            self.table.set_checkable(self.session.state in (SessionState.IDLE, SessionState.SELECTING))
            self._apply_stop_on_fail(self.session.selected_stop_on_fail)
        self._update_start_button()

    def _apply_stop_on_fail(self, value: bool) -> None:
        """Set the "On failure" pair to the picked schedule's :setStopOnFail (packet 12d)."""
        self.stop_on_fail_radio.setChecked(value)
        self.continue_all_radio.setChecked(not value)

    def _update_start_button(self) -> None:
        """Start is available only for a runnable schedule and a non-empty Project.

        A broken row carries no name, so it can never be the selection a run starts from (packet
        05b); a blank Project refuses the run too (spec 08). The reason goes on the error line."""
        runnable = self.picker.selected_name() is not None
        project = self.fields.project_edit.text().strip()
        self.start_button.setEnabled(runnable and bool(project) and self.state != SessionState.STARTED)
        if runnable:
            self.error_label.setText(
                "" if project else "⛔ Project is required before a Test Run can start"
            )
        else:
            self.error_label.setText("")

    def _request_abort(self) -> None:
        """Abort the run: answer a pending prompt, or stop at the next step boundary."""
        if self._paused_seq is not None:
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
            self._pre_counts = (0, 0)
            self._post_counts = (0, 0)
            self._started_steps = set()
            self._step_total = self._enabled_step_count(steps)
            self._timer.start(1000)
            # A new run tallies from nothing: the previous run's numbers stay on the grid otherwise
            # (packet 25a review), and the log panes start empty too.
            self.counts.set_counts({})
            self._show_log([])
            self.counts.show()
        elif state == SessionState.FINISHED:
            self._engine = None
            self._paused_seq = None
            self._paused_result = ""
            self._paused_message = ""
            self.table.clear_pause()
            # Nothing can be asked for any more, so every Step Input closes: a widget left live
            # after FINISHED answers against a released engine (packet 25a review).
            self.table.close_inputs()
            run_uuid = self.session.run_uuid
            nodes = self.session.store.nodes(run_uuid) if run_uuid else []
            attempts = self.session.store.attempts(run_uuid) if run_uuid else []
            log_lines = self.session.store.log_lines(run_uuid) if run_uuid else []
            self.table.show_state(nodes, attempts)
            self._show_log(log_lines)
            self._update_counts(nodes)
            self._phase_text = "Finished"
        elif state in (SessionState.IDLE, SessionState.SELECTING):
            self.table.clear_pause()
            self._paused_seq = None

        # Step boxes: editable in setup, locked while a run is up (packet 11b).
        self.table.set_checkable(state in (SessionState.IDLE, SessionState.SELECTING))
        self.abort_button.setEnabled(state == SessionState.STARTED)
        self._update_start_button()
        self._update_header(state)
        if state == SessionState.FINISHED:
            self._timer.stop()

    def _update_header(self, state: Optional[SessionState] = None, now: Optional[datetime] = None) -> None:
        """Refresh the status bar's timers (packet 14d)."""
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

    def _sync_from_store(self) -> None:
        """Redraw the table, the counts and the log from whatever the store has recorded so far."""
        run_uuid = self.session.run_uuid
        if not run_uuid:
            return
        nodes = self.session.store.nodes(run_uuid)
        attempts = self.session.store.attempts(run_uuid)
        self.table.show_state(nodes, attempts, self._paused_seq, self._paused_result, self._paused_message)
        self._update_counts(nodes)
        self._show_log(self.session.store.log_lines(run_uuid))

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
        self._paused_seq = node_seq
        self._paused_result = result
        self._paused_message = message
        self._sync_from_store()
        self._update_header()

    def _on_prompt_choice(self, choice: str) -> None:
        """A prompt was answered: clear the pause's display, then tell the session."""
        self._paused_seq = None
        self._paused_result = ""
        self._paused_message = ""
        self.table.clear_pause()
        self._phase_text = "Running"
        self._sync_from_store()
        self._update_header()
        self.session.choose(choice)

    # -- Step Inputs (ticket 10a) ------------------------------------------

    def step_input_widgets(self) -> dict[int, list[StepInputWidget]]:
        """The centre pane's Step Input widgets, keyed by the seq of the step that declared them."""
        return self.table.input_widgets

    def _on_input_asked(self, node_seq: int, attempt_no: int, index: int) -> None:
        """The worker is waiting on this step's n-th input, so its widget becomes live.

        The ask is also when the log pane is most worth reading: whatever the step logged before it
        blocked is already in the store, and the Operator needs it to decide what to type
        (packet 25a review)."""
        self._refresh_log()
        widget = self.input_widget(node_seq, index)
        if widget is not None:
            widget.activate(attempt_no)

    def _on_step_finished(self, node_seq: int) -> None:
        """One step's Attempts are done: close its POLL widgets and redraw the run so far."""
        for widget in self.table.input_widgets.get(node_seq, ()):
            if widget.declaration.type == "POLL":
                widget.deactivate()
        self._sync_from_store()
