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
    SchedulePicker,
    StatusBar,
    StepEnabler,
    StepInputWidget,
    _BROKEN_ERR,
    _HEADER_BG,
    _FAILED_BG,
    _INLINE_PROMPT_STYLE,
    _STICKY_STYLE,
    _rollup_text as rollup_text,
    _row_label as row_label,
    load_schedule_steps,
)

#: The centre pane's columns. `Enable` carries the step's box (packet 15d); the rest are the mockup's
#: `.b-thead` cells (prototype line 100).
COLUMNS = ("Enable", "Step", "Result", "Attempts", "Actual", "Expected", "Message")
#: Widths that fit the 640 px centre pane of a 1280x800 window without clipping a Result or an Attempt.
_COLUMN_WIDTHS = (44, 180, 62, 30, 80, 80, 140)


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
        """Give a paused step's row the mockup's `.b-row.failed-hl` tint; clear it otherwise."""
        for column in range(self.table.columnCount()):
            item = self.table.item(row, column)
            if item is None:
                continue
            item.setBackground(QColor(_FAILED_BG) if paused else QColor())

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

    def _toggle_step(self, node_seq: int, state: Qt.CheckState) -> None:
        """One box was ticked: the enabler applies it (packet 15d) and the derived boxes follow.

        Re-syncing the headers around a toggle repaints their boxes, and a repaint must not read as
        another Operator toggle, so the enabler's recorded state is compared first."""
        if self.enabler is None or not self.checks[node_seq].isEnabled():
            return
        checked = state == Qt.CheckState.Checked
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


class ThreePaneRunScreen(QWidget):
    """A three-pane Run screen bound to one :class:`HeadlessRunSession`.

    The same session, the same actions: Start and Abort live in the left pane, a Stop-on-Fail pause
    is answered by the inline prompt in the centre pane, and the status bar at the bottom carries the
    run's state, its tallies and its timers.
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
        """The picker picked a schedule: show its steps in the centre pane, boxes still editable."""
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

    def _confirm_store_choice(self, mismatches: list[tuple[str, Optional[str], str, str]]) -> Optional[bool]:
        """Ask the Operator what to do with mismatched files before the run starts (ticket 06c)."""
        if not mismatches:
            return None
        labels = self.picker.release_labels
        release = labels.get(mismatches[0][3], mismatches[0][3] or "")
        setting = self.session.config.store_mismatched_files if self.session.config is not None else "ask"
        details = ", ".join(part for part in (
            f"UUT {self.fields.serial_edit.text().strip()}" if self.fields.serial_edit.text().strip() else "",
            f"Project {self.fields.project_edit.text().strip()}" if self.fields.project_edit.text().strip() else "",
            f"Operator {self.fields.operator_edit.text().strip()}" if self.fields.operator_edit.text().strip() else "",
        ) if part)
        dialog = MismatchConfirmation(self.session.selected_schedule or "", release, mismatches, setting, details)
        self._confirm_dialog = dialog
        if self.confirm_choice is not None:
            self.confirm_choice(dialog)
        else:
            dialog.exec()
        return dialog.store_choice()

    def begin_run(self) -> None:
        """Capture the Operator's fields and start a run. Clears nothing on purpose."""
        operator = self.fields.operator_edit.text().strip()
        project = self.fields.project_edit.text().strip()
        uut_type = self.fields.uut_type_edit.text().strip()
        serial = self.fields.serial_edit.text().strip()
        self.session.set_stop_on_fail(self.stop_on_fail_radio.isChecked())
        if operator:
            self.session.set_operator(operator)
        # Project is pushed even when blank: a Test Run cannot start without one (spec 08).
        self.session.set_project(project)
        if uut_type:
            self.session.set_uut_type(uut_type)
        if serial:
            self.session.set_uut_serial(serial)

        mismatches = self.session.pending_mismatches()
        store = self._confirm_store_choice(mismatches)
        if mismatches and store is None:
            return  # the Operator cancelled the confirmation: nothing started, nothing cleared.
        try:
            engine = self.session.start(store_mismatched=store)
        except SessionError as exc:
            self.error_label.setText(f"⛔ {exc}")
            self.start_button.setEnabled(False)
            return
        self._engine = engine

    @property
    def engine(self) -> Optional[RunEngine]:
        """The RunEngine worker for the current run, or ``None`` before it starts / after FINISHED."""
        return self._engine

    def _request_abort(self) -> None:
        """Abort the run: answer a pending prompt, or stop at the next step boundary."""
        if self._paused_seq is not None:
            self._on_prompt_choice("ABORT")
            return
        self.session.request_abort()

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
            self.counts.show()
        elif state == SessionState.FINISHED:
            self._engine = None
            self._paused_seq = None
            self._paused_result = ""
            self._paused_message = ""
            self.table.clear_pause()
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

    def _on_action_started(self, phase: str, done: int, total: int) -> None:
        """A Pre-/Post-test Action has begun: move its tally and restart the current-item timer."""
        now = self._clock()
        if phase == "pre":
            self._pre_counts = (done, total)
        else:
            self._post_counts = (done, total)
        self._item_started_at = now
        self._update_header(now=now)

    def _on_step_started(self, node_seq: int) -> None:
        """A Test Step Attempt — or a Retry of one — has begun."""
        now = self._clock()
        self._started_steps.add(node_seq)
        self._item_started_at = now
        self._update_header(now=now)

    def _enabled_step_count(self, steps: list[object]) -> int:
        """How many expanded rows are Test Steps this run will execute (the Steps tally's total)."""
        overrides = self.session.node_enables or {}
        return sum(
            1
            for seq, step in enumerate(steps)
            if not getattr(step, "is_header", False)
            and overrides.get(seq, bool(getattr(step, "default_enabled", getattr(step, "enabled", True))))
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

    def _show_log(self, log_lines: list[dict]) -> None:
        """The log pane shows INFO and above; the debug window keeps every level (ticket 08)."""
        info_and_above = [ln for ln in log_lines if str(ln.get("level")) != "DEBUG"]
        self.log_pane.clear()
        self.log_pane.add_lines(info_and_above)
        self.debug_window.clear()
        self.debug_window.add_lines(log_lines)

    # -- Stop-on-Fail prompt (packet 12d) ---------------------------------

    def _on_failure_prompt(self, node_seq: int, attempt_no: int) -> None:
        """A failure is awaiting the Operator's choice: put the prompt under the step it failed on."""
        step = self._steps[node_seq] if 0 <= node_seq < len(self._steps) else None
        attempt = self._attempt_row(node_seq, attempt_no)
        result = str(attempt.get("pass_fail") or "FAIL") if attempt else "FAIL"
        message = str(attempt.get("msg") or "") if attempt else ""
        self._paused_seq = node_seq
        self._paused_result = result
        self._paused_message = message
        self._sync_from_store()
        self._update_header()

    def _attempt_row(self, node_seq: int, attempt_no: int) -> Optional[dict]:
        """The Attempt row a pause was raised on — its result and message, read from the store."""
        run_uuid = self.session.run_uuid
        if run_uuid is None:
            return None
        for row in self.session.store.attempts(run_uuid):
            if row.get("node_seq") == node_seq and row.get("attempt_no") == attempt_no:
                return row
        return None

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

    def _on_input_asked(self, node_seq: int, attempt_no: int, index: int) -> None:
        """The worker is waiting on this step's n-th input, so its widget becomes live."""
        widget = self.table.input_widget(node_seq, index)
        if widget is not None:
            widget.activate(attempt_no)

    def _on_input_answered(self, node_seq: int, attempt_no: int, index: int, value: object) -> None:
        """An answer arrived through the session: show it on the widget and close it."""
        widget = self.table.input_widget(node_seq, index)
        if widget is not None:
            widget.answer(value)

    def _on_step_finished(self, node_seq: int) -> None:
        """One step's Attempts are done: close its POLL widgets and redraw the run so far."""
        for widget in self.table.input_widgets.get(node_seq, ()):
            if widget.declaration.type == "POLL":
                widget.deactivate()
        self._sync_from_store()
