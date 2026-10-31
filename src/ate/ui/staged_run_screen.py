"""The staged Run screen: ticket 03's Operator-facing layout.

This is the only place that binds a :class:`~ate.run_session.headless.HeadlessRunSession`
to widgets. It renders one short schedule through three ordered setup stages and then a
Run view, but it holds no run logic of its own — every action it takes is a call into an
existing interface:

* the Operator's fields go to ``session.set_operator/project/uut_type/serial``;
* picking a schedule goes to ``session.select_schedule``;
* starting goes to ``session.start()`` which returns a :class:`~ate.engine.run_engine.RunEngine`;
* results come back through ``session.store.nodes(run_uuid)`` and
  ``session.store.attempts(run_uuid)`` (ticket 03's two child-read methods).

The layout never imports the engine or run session, so the architectural guard in
``tests/test_architecture_rules.py`` keeps passing; it only ever *reads* those layers.
Because every update rides a Qt signal (or a timer), the worker thread is never blocked
and the GUI stays responsive while a schedule runs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ate.engine.run_engine import RunEngine

from ..run_session.headless import HeadlessRunSession, SessionState
from .run_widgets import (
    FailurePromptBar,
    LogPane,
    MismatchConfirmation,
    RunFieldsForm,
    RunScreenMixin,
    SchedulePicker,
    StatusBar,
    StepEnabler,
    StepInputWidget,
    BROKEN_MARKER,
    load_schedule_steps,
    _BROKEN_BG,
    _BROKEN_ERR,
    _FORBIDDEN_CURSOR,
    _FLAG_STYLE,
    _HEADER_BG,
    _MUTED_STYLE,
    _ROW_MARGIN_X,
    _ROW_MARGIN_Y,
    _ResizeBrokenRows,
    _broken_row_size_hint,
    _broken_row_widget,
    _children_of_rows,
    _format_elapsed,
    _format_log_line,
    _mismatch_flag_text,
    _node_depths,
    _rollup_text,
    _row_filter_text,
    _row_label,
    _short_error,
    _short_sha,
    _wrapped_height,
)

def _append_input_rows(list_widget: QListWidget, widgets: list["StepInputWidget"], depth: int) -> list[QListWidgetItem]:
    """Add one row per Step Input widget, indented under the step that declared it.

    A list item can carry only one widget, so every Step Input gets its own row — the mockup's
    `.inputs` block, one level deeper than its step. The item text stays empty: any text would be
    drawn *under* the widget, which already paints the input's description, and the row would read
    twice (packet 10a review). The indent comes from the widget's own left margin, the mockup's
    `.inputs { margin-left: 26px }`.
    """
    items: list[QListWidgetItem] = []
    for widget in widgets:
        item = QListWidgetItem("")
        # Not selectable: clicking the row itself means nothing; the controls inside it are the point.
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setSizeHint(widget.sizeHint())
        list_widget.addItem(item)
        list_widget.setItemWidget(item, widget)
        widget.input_item = item  # so the screen can scroll to the row an input is asked for
        items.append(item)
    return items


class UutOperatorStage(RunFieldsForm):
    """Stage one: the Operator and UUT fields, plus the button that advances to stage two.

    The four labelled inputs are the shared :class:`~ate.ui.run_widgets.RunFieldsForm`; this stage
    adds only the mockup's Next button. Nothing here calls into the session until a run starts."""

    def __init__(self) -> None:
        super().__init__()
        self.next_button = QPushButton("Next")
        self.layout().addWidget(self.next_button, 4, 1)
        self.layout().setAlignment(Qt.AlignmentFlag.AlignTop)


class TestScheduleStage(SchedulePicker):
    """Stage two: the schedule picker and the button that moves on to Steps & options.

    The picker (filter, Rescan, list, notes, mismatch flag) is shared with the three-pane layout;
    this stage adds only its "Run this schedule" button, which the screen wires up."""

    def __init__(self, session: HeadlessRunSession) -> None:
        super().__init__(session)
        self.run_button = QPushButton("Run this schedule")
        self.layout().addWidget(self.run_button)


class StepsOptionsStage(QWidget):
    """Stage three: the run's steps and the stop-on-fail option.

    ``load_steps`` fills ``step_list`` with the selected schedule's expanded steps so the
    Operator can confirm what will run; ``stop_on_fail_radio`` is prefilled checked (True)
    as the default policy, paired with a "run all" alternative. The stage's Run button
    starts the run through the screen.
    """

    def __init__(self, session: Optional["HeadlessRunSession"] = None, screen: Optional["StagedRunScreen"] = None) -> None:
        super().__init__()
        self.session = session
        # The layout this stage sits in (packet 25b): its Step Inputs answer only when the layout
        # is the one showing.
        self.screen = screen
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.step_list = QListWidget()
        # Checkboxes are per step (packet 11b): each row is checkable and a toggle is one call
        # into session.toggle_node — the widget holds no logic of its own. Packet 15d: a header row
        # has no toggle of its own; clicking it sets every step beneath it, one call per step.
        self._checkable = True  # setup state: the boxes stay checkable until a run starts
        self._loading = False  # True while load_steps re-syncs check states, so itemChanged
        # is not mistaken for an Operator toggle.
        self._states: dict[int, bool] = {}  # each step's last-seen checked state
        self._rows: list[object] = []  # the expanded tree rows, in runner seq order
        self._step_items: dict[int, QListWidgetItem] = {}  # each step index -> its list row
        self._children: dict[int, list[int]] = {}  # each row's direct children in the tree
        self._overrides: dict[int, bool] = {}  # the Operator's per-step toggles seen so far
        # Every declared Step Input of the selected schedule, under its step row (ticket 10a). They
        # are always shown here, but inert until the run asks for them.
        self.input_widgets: dict[int, list[StepInputWidget]] = {}
        self.step_list.itemChanged.connect(self._on_check_toggled)
        self.stop_on_fail_radio = QRadioButton("Stop on first failure")
        self.continue_all_radio = QRadioButton("Run every step regardless of failures")
        # Default policy: stop on the first failure; :meth:`load_steps` re-applies it from the
        # selected schedule's Stop on Fail default once a schedule has been chosen.
        self.stop_on_fail_radio.setChecked(True)
        self.run_button = QPushButton("Start run")
        # The mockup's `.err` line for a run that cannot start (packet 05b review): the screen fills
        # it when `begin_run` is refused, so the Operator sees why instead of a traceback.
        self.error_label = QLabel("")
        self.error_label.setObjectName("run_error")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {_BROKEN_ERR}; font-size: 12px;")

        layout.addWidget(self.step_list)
        layout.addWidget(self.stop_on_fail_radio)
        layout.addWidget(self.continue_all_radio)
        layout.addWidget(self.error_label)
        layout.addWidget(self.run_button)

    def load_steps(self, steps: Iterable[object]) -> None:
        """Show ``steps`` (each with ``step_no``, ``name``, ``default_enabled``) as checkable rows.

        Every row carries its step's enabled default — after any enclosing header's ``setEnable``
        forced it (packet 15d) — or, when the session carries step-enable overrides for the
        selected schedule, the override that a next run would record, so the boxes show what the
        run will record (spec 37: options carry over). A toggle is one call into
        ``session.toggle_node(row, checked)`` (packet 11b), with the row's position in the expanded
        step list as the runner seq. A header row's box is DERIVED (packet 15d): Checked when every
        step beneath it is on, Unchecked when none is, partial when mixed, and clicking it sets
        them all. Header rows also get the mockup's look: ``#eef1f5`` background and a bold name.
        Each step's declared Step Inputs sit under it as their own rows (ticket 10a), disabled until
        the run asks for them.
        Also re-applies the selected schedule's Stop on Fail default to the radio pair so it is not
        stuck at construction-time; the Operator can still toggle either choice."""
        self._loading = True
        self._clear_input_widgets()
        self.step_list.clear()
        self._rows = list(steps)
        self._step_items = {}
        self._states = {}
        # Packet 25a: the enable rules (defaults, overrides, header tri-state) are the shared
        # StepEnabler's; this stage only paints the boxes and forwards the toggles.
        self._enabler = StepEnabler(self._rows, self.session)
        for seq, step in enumerate(self._rows):
            label = getattr(step, "name", "") or ""
            number = getattr(step, "step_no", "") or ""
            item = QListWidgetItem(_row_label(number, label, int(getattr(step, "depth", 0))))
            # Always keep the row enabled and selectable and the box drawn; only
            # ItemIsUserCheckable toggles with the run lock (a locked box still shows its
            # check state). NoItemFlags would disable the row even in setup.
            checkable = Qt.ItemFlag.ItemIsUserCheckable if self._checkable else Qt.ItemFlag.NoItemFlags
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | checkable)
            self.step_list.addItem(item)
            self._step_items[seq] = item
            if getattr(step, "is_header", False):
                item.setBackground(QColor(_HEADER_BG))
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            state = self._enabler.box_state(seq)
            self._states[seq] = state == Qt.CheckState.Checked
            item.setCheckState(state)
            declarations = getattr(step, "_validated_inputs", None) or []
            if declarations and self.session is not None:
                self.input_widgets[seq] = [
                    StepInputWidget(decl, self.session, seq, owner=self.screen) for decl in declarations
                ]
                _append_input_rows(
                    self.step_list, self.input_widgets[seq], int(getattr(step, "depth", 0)) + 1
                )
        self._loading = False
        if self.session is not None:
            # The session's effective choice, not the schedule's raw default: after a run the
            # Operator's own On failure selection carries into the next one (spec 37, packet 30b).
            self._apply_stop_on_fail(self.session.stop_on_fail)

    def _effective(self, row: int) -> bool:
        """Whether row ``row``'s box is on: the Operator's override if there is one, else the default."""
        return self._enabler.effective(row)

    def _subtree(self, row: int) -> list[int]:
        """Every row beneath the header at ``row``, depth first — headers and Test Steps alike."""
        return self._enabler.subtree(row)

    def _header_state(self, row: int) -> Qt.CheckState:
        """A header's box, derived from the steps beneath it (packet 15d, the mockup's enableBox)."""
        return self._enabler.header_state(row)

    def _header_rows_above(self, row: int) -> list[int]:
        """The header rows enclosing ``row``, innermost first, whose box must follow its children."""
        return self._enabler.headers_above(row)

    def _set_box(self, seq: int, state: Qt.CheckState) -> None:
        """Set one step's box without it reading as a new Operator toggle (the state is recorded first)."""
        self._states[seq] = state == Qt.CheckState.Checked
        self._step_items[seq].setCheckState(state)

    def _on_check_toggled(self, item: QListWidgetItem, *_args: int) -> None:
        """A checkbox toggle reaches the session as ``toggle_node(row, checked)`` — no logic here.

        PyQt6's ``itemChanged`` signal passes only the row, so the handler reads the row's new
        check state and compares it against the state it last saw; flag re-syncs (set_checkable),
        same-state events and the load_steps re-sync are all ignored, so only a real toggle
        reaches the session. A locked box (a run in progress) has no toggle: the handler
        returns before touching the session. Packet 15d: clicking a header is not an override of
        its own row — the enabler sets every Test Step beneath it, and the header's box (and every
        header above it) then reads back from those steps. Ticket 10a: the input rows under a step
        have no box, so their itemChanged events are ignored."""
        if self._loading or self.session is None or not self._checkable:
            return
        if self.screen is not None and not self.screen.is_active():
            return  # the layout that is not showing cannot change what the next run runs (packet 25b)
        seq = next((step for step, step_item in self._step_items.items() if step_item is item), None)
        if seq is None:
            return  # an input row (ticket 10a), not a step with a box
        checked = item.checkState() == Qt.CheckState.Checked
        if self._states.get(seq) == checked:
            return
        self._states[seq] = checked
        for row in self._enabler.toggle(seq, checked):
            self._set_box(row, self._enabler.box_state(row))

    def set_checkable(self, checkable: bool) -> None:
        """Lock or unlock the step checkboxes: toggle only ItemIsUserCheckable so the
        boxes stay drawn with their check state and the row stays enabled and selectable
        (the screen calls this from session state changes)."""
        self._checkable = checkable
        # Re-sync the last-seen state before the flag pass, so the flag events the pass emits
        # are read back as the same state and never reach the session as phantom toggles.
        for seq, item in self._step_items.items():
            self._states[seq] = item.checkState() == Qt.CheckState.Checked
        keep = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        flag = keep | (Qt.ItemFlag.ItemIsUserCheckable if checkable else Qt.ItemFlag.NoItemFlags)
        for item in self._step_items.values():
            item.setFlags(item.flags() & keep | flag)

    def _clear_input_widgets(self) -> None:
        """Delete the previous schedule's input widgets before the stage is rebuilt.

        ``clear()`` leaves every `setItemWidget` widget parented to the viewport (packet 05b
        review), so each reload would otherwise orphan one row per Step Input."""
        for widgets in self.input_widgets.values():
            for widget in widgets:
                widget.setParent(None)
                widget.deleteLater()
        self.input_widgets = {}

    def _apply_stop_on_fail(self, value: bool) -> None:
        """Set the radio pair to ``value`` (True = stop on first failure)."""
        self.stop_on_fail_radio.setChecked(value)
        self.continue_all_radio.setChecked(not value)


class TextPane(QPlainTextEdit):
    """A scrollable text pane with the ``text``/``setText`` shape the screens and tests use.

    Plain text scrolls, so the Log and Summary tabs keep a small fixed minimum (packet 30b); the
    QLabel they replaced grew their minimumSizeHint with every line the run wrote. The Operator reads
    them; the run writes them, so they are read-only (packet 30b review)."""

    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)

    def text(self) -> str:
        return self.toPlainText()

    def setText(self, value: str) -> None:
        self.setPlainText(value)

    def clear(self) -> None:
        """Empty the pane, so a new run's Log and Summary hold only their own lines (packet 30b)."""
        self.setPlainText("")


class RunView(QWidget):
    """The view shown while and after a run: state header, live steps, Log/Summary, status bar.

    ``state_header`` shows the current :class:`SessionState` plus a live elapsed timer;
    ``step_list`` lists each step with its per-step Result (the latest step enlarged), with every
    declared Step Input on its own row under the step that asked for it (ticket 10a);
    ``failure_bar`` is a full-width bar that surfaces any failing or errored step;
    ``tabs`` holds the attempt Log and a run Summary; and ``status_bar`` (packet 14d) is the last
    widget, the mockup's ``statusBar()``: the run's state, the Pre-test / Steps / Post-test tallies
    and the run and current-item timers. All content is refreshed from the
    store by :meth:`refresh`, never computed here.
    """

    def __init__(self, session: Optional[HeadlessRunSession] = None, screen: Optional["StagedRunScreen"] = None) -> None:
        super().__init__()
        self.session = session
        # The layout this view belongs to (packet 25b): a Step Input here answers only while this
        # layout is the one the Operator is looking at.
        self.screen = screen
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.state_header = QLabel()
        # Status bar (packet 14d): the shared StatusBar, whose labels the screen reads back.
        self.status_bar = StatusBar()
        self.status_label = self.status_bar.status_label
        self.status_pre = self.status_bar.status_pre
        self.status_steps = self.status_bar.status_steps
        self.status_post = self.status_bar.status_post
        self.status_total = self.status_bar.status_total
        self.status_current = self.status_bar.status_current
        # Whole-run Abort control (packets 13a): asks the session to abort the current run so it
        # stops between steps and records ERROR / NOTRUN / ABORTED. Enabled only while a run is up;
        # the screen toggles it in _on_state_changed.
        self.abort_button = QPushButton("Abort")
        # Packet 30b: the mockup's finished state puts "New run" in the run header (variant C);
        # the screen wires it back to the setup stages, which carry their fields over untouched.
        self.new_run_button = QPushButton("New run")
        self.new_run_button.setEnabled(False)
        self.step_list = QListWidget()
        # Ticket 10a: each step's Step Input widgets, keyed by the runner seq of the step they
        # belong to, in declaration order. They are built once per run and re-attached whenever the
        # step list is rebuilt, so the answers the Operator gave survive every refresh.
        self.input_widgets: dict[int, list[StepInputWidget]] = {}
        # Full-width failure bar: a fixed-height label that spans the view's width in the layout.
        self.failure_bar = QLabel()
        self.failure_bar.setFixedHeight(24)
        # The Stop-on-Fail prompt bar (packet 12d): hidden until the session pauses on a failure.
        self.failure_prompt_bar = FailurePromptBar()

        self.tabs = QTabWidget()
        # Packet 30b: a QLabel's minimumSizeHint grows with its text, so a finished 20-step run made
        # this view's minimum taller than the window and the window grew to fit it. The mockup's Log
        # and Summary panes scroll (`.pane` with `data-scroll`), so the tabs hold scrollable text
        # panes with a fixed small minimum instead of unbounded labels.
        log_tab = TextPane()
        summary_tab = TextPane()
        self.tabs.addTab(log_tab, "Log")
        self.tabs.addTab(summary_tab, "Summary")
        #: The two panes of the Log/Summary tab: read-only, and emptied when a run starts, so the
        # text on screen is always the run the Operator is looking at (packets 30b, 30b review).
        self.log_tab = log_tab
        self.summary_tab = summary_tab

        # Ticket 08's two log views: the Log pane shows INFO and above; the Debug window is a
        # separate pane showing every level. Both are filled from session.store.log_lines() in
        # refresh(), never computed here.
        self.log_pane = LogPane("Log — INFO and above")
        self.debug_window = LogPane("Debug — every level")

        # Packet 30b: the mockup's run view is dominated by the step list; the Log/Summary tabs and
        # the two log panes are the bottom band. Their default preferred sizes each ran close to
        # 200 px, so together they ate the whole view and the step list collapsed to 68 px. A floor
        # for the list keeps the steps readable at 1280x800 without the window having to grow.
        # Packet 30b review: the band's absolute ceilings (tabs 220, log 150, debug 110) capped it at
        # any window size, so a 1920x1200 window wasted the space below it. The panes now have a
        # readable floor and share what is left by stretch: bigger window, bigger panes, same floor.
        self.step_list.setMinimumHeight(150)
        self.tabs.setMinimumHeight(60)
        self.log_pane.setMinimumHeight(60)
        self.debug_window.setMinimumHeight(40)

        # The band's share of the view: steps 3, Log/Summary 2, Log 2, Debug 1.
        _STEPS_STRETCH, _TABS_STRETCH, _LOG_STRETCH, _DEBUG_STRETCH = 3, 2, 2, 1

        # Header row (mockup variant C): the state label grows, the buttons sit at its right —
        # Abort while the run is up, New run once it is finished (packet 30b).
        header_row = QHBoxLayout()
        header_row.addWidget(self.state_header, stretch=1)
        header_row.addWidget(self.abort_button)
        header_row.addWidget(self.new_run_button)
        layout.addLayout(header_row)
        layout.addWidget(self.failure_bar)
        layout.addWidget(self.step_list, stretch=_STEPS_STRETCH)
        layout.addWidget(self.failure_prompt_bar)
        layout.addWidget(self.tabs, stretch=_TABS_STRETCH)
        layout.addWidget(self.log_pane, stretch=_LOG_STRETCH)
        layout.addWidget(self.debug_window, stretch=_DEBUG_STRETCH)
        # Packet 14d review: the mockup's statusBar() closes the run view, so the bar is the last
        # widget — under the step list and the Log/Summary tabs — not a header.
        layout.addWidget(self.status_bar)

    def clear_run_text(self) -> None:
        """Empty the Log and Summary panes: a run's view holds only its own lines (packet 30b review).

        ``refresh`` appends every Attempt line to the Log pane, so a second run in the same window
        would otherwise read as one long log."""
        self.log_tab.clear()
        self.summary_tab.clear()

    def load_run_steps(self, steps: Iterable[object]) -> None:
        """Build the run's step rows, with each step's declared Step Inputs under it.

        Called when a run starts (ticket 10a) so every input is on screen, inert, before the worker
        ever asks for one: the spec says every step is listed with its inputs always visible. The
        widgets are made from the schedule's validated declarations, exactly as the mockup's
        ``inputsHTML`` renders them."""
        for widgets in self.input_widgets.values():
            for widget in widgets:
                widget.setParent(None)
                widget.deleteLater()
        self.input_widgets = {}
        # Nothing carries a widget yet (they were deleted just above), so clearing is safe: an item
        # takes its widget with it only when it is destroyed still holding one (packet 10a review).
        self.step_list.clear()
        rows: list[tuple] = []
        for seq, step in enumerate(steps):
            is_header = bool(getattr(step, "is_header", False))
            rows.append((
                _row_label(getattr(step, "step_no", ""), getattr(step, "name", ""), int(getattr(step, "depth", 0))),
                None,
                is_header,
                QColor(_HEADER_BG) if is_header else None,
                0,
            ))
            declarations = getattr(step, "_validated_inputs", None) or []
            if declarations and self.session is not None:
                self.input_widgets[seq] = [
                    StepInputWidget(decl, self.session, seq, owner=self.screen) for decl in declarations
                ]
                for widget in self.input_widgets[seq]:
                    rows.append(("", widget, False, None, 0))
        self._render_rows(rows)

    def _render_rows(self, rows: list[tuple]) -> list[QListWidgetItem]:
        """Draw ``(text, input widget or None, is_header, background, font bump)`` into the step list.

        Rows are updated in place rather than cleared (packet 10a review): a QListWidgetItem that
        carries a Step Input widget deletes that widget when the item is destroyed, so rebuilding the
        list with ``clear()`` wiped out every answer the Operator had given and left the view painting
        a deleted widget. Every rebuild of a run has the same rows — one per step plus one per
        declared input — so each row is reused and only its text, background and font change."""
        list_widget = self.step_list
        while list_widget.count() > len(rows):
            extra = list_widget.takeItem(list_widget.count() - 1)
            widget = list_widget.itemWidget(extra)  # a schedule with fewer rows: drop the orphan too
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        items: list[QListWidgetItem] = []
        for index, (text, widget, is_header, background, bump) in enumerate(rows):
            if index >= list_widget.count():
                item = QListWidgetItem(text)
                list_widget.addItem(item)
                item.setData(Qt.ItemDataRole.UserRole, item.font().pointSize())  # the un-bumped size
            else:
                item = list_widget.item(index)
                item.setText(text)
            item.setBackground(background if background is not None else QBrush())
            font = item.font()
            base = item.data(Qt.ItemDataRole.UserRole)
            font.setPointSize(int(base if base is not None else font.pointSize()) + bump)
            font.setBold(is_header or bump > 0)
            item.setFont(font)
            if widget is not None:
                # Not selectable: clicking the row itself means nothing; the controls inside it are
                # the point. The indent is the widget's own left margin (the mockup's `.inputs`).
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                item.setSizeHint(widget.sizeHint())
                if list_widget.itemWidget(item) is not widget:
                    list_widget.setItemWidget(item, widget)
                widget.input_item = item
            items.append(item)
        return items

    def input_widget(self, node_seq: int, index: int) -> Optional[StepInputWidget]:
        """The widget for the n-th (1-based) Step Input of the step at ``node_seq``, if any."""
        for widget in self.input_widgets.get(node_seq, ()):
            if widget.index == index:
                return widget
        return None

    def refresh(self, nodes: list[dict], attempts: list[dict], log_lines: list[dict]) -> None:
        """Render a finished run's steps, failure bar, and Log/Summary from the store.

        Reads only what the store recorded; it never decides pass/fail itself — a step is
        treated as failed purely when its stored result is ``FAIL`` or ``ERROR``, so this
        stays a view of data, not a run engine. The two tabs are added in order (Log, then
        Summary), so they are widgets 0 and 1 here.

        ``log_lines`` feeds the Log pane (INFO and above) and the separate Debug window
        (every level); DEBUG is filtered out of the Log pane but kept in the debug view.
        """
        log_tab = self.tabs.widget(0)
        summary_tab = self.tabs.widget(1)

        # The run is over: nothing can be asked for any more, so every input closes.
        for widgets in self.input_widgets.values():
            for widget in widgets:
                widget.deactivate()
        # Packet 15b: header rows show their Step Number and name with no result, and every row is
        # indented by its depth in the expanded tree, as on the Steps & options stage. Packet 15d:
        # header rows get the mockup's `#eef1f5` background and bold name, and a rollup of the
        # steps beneath them — `N/M done`, plus `, K failed` when some failed. Ticket 10a: each step
        # keeps its Step Input rows under it, showing the answers the Operator gave.
        depths = _node_depths(nodes)
        children = _children_of_rows(nodes, lambda node: node.get("parent_node_seq"))
        by_seq = {node["node_seq"]: node for node in nodes}
        # Attempts per step, from the recorded Attempt rows: a step retried after a pause has more
        # than one, and the spec's "Retried steps show their Attempts" (packet 25b review) means the
        # staged list has to say so too, not only the three-pane table.
        attempt_count: dict[int, int] = {}
        for attempt in attempts:
            attempt_count[attempt["node_seq"]] = attempt_count.get(attempt["node_seq"], 0) + 1
        rows: list[tuple] = []
        last_step_row = None
        for node in nodes:
            text = _row_label(node.get("step_number"), node.get("step_name"), depths[node.get("node_seq")])
            is_header = node.get("kind") != "step"
            if not is_header:
                text += f"  →  {node.get('result', '')}"
                tries = attempt_count.get(node["node_seq"], 0)
                if tries > 1:
                    text += f"  ({tries} Attempts)"
            else:
                rollup = _rollup_text(node["node_seq"], children,
                                     lambda seq: by_seq[seq].get("kind") != "step",
                                     lambda seq: by_seq[seq].get("result"))
                if rollup:
                    text += f"  {rollup}"
            rows.append((text, None, is_header, QColor(_HEADER_BG) if is_header else None, 0))
            if not is_header:
                last_step_row = len(rows) - 1
                for widget in self.input_widgets.get(node["node_seq"], ()):
                    rows.append(("", widget, False, None, 0))
        # The latest step is the current one; enlarge it so the eye lands on where we are.
        if last_step_row is not None and len(rows) > 1:
            text, widget, is_header, background, _ = rows[last_step_row]
            rows[last_step_row] = (text, widget, is_header, background, 2)
        self._render_rows(rows)

        failed = any(node.get("result") in ("FAIL", "ERROR") for node in nodes)
        if failed:
            names = [node.get("step_name") or "" for node in nodes if node.get("result") in ("FAIL", "ERROR")]
            self.failure_bar.setText(f"Run FAILED — {len(names)} step(s): {'; '.join(names)}")
            self._apply_failure_style()
        else:
            self.failure_bar.setText("")

        for attempt in attempts:
            message = attempt.get("msg") or ""
            log_tab.setPlainText(log_tab.text() + f"{attempt.get('pass_fail', '')}  {message}\n")

        results = [node.get("result", "") for node in nodes if node.get("kind") == "step"]
        verdict = "FAIL" if failed else ("PASS" if results else "NOTRUN")
        lines = [f"Result: {verdict}"]
        for node in nodes:
            if node.get("kind") != "step":
                continue
            lines.append(f"{node.get('step_number', '')}  {node.get('step_name') or ''}  →  {node.get('result', '')}")
        summary_tab.setPlainText("\n".join(lines))

        # The Log pane shows INFO and above; the Debug window keeps every level.
        info_and_above = [ln for ln in log_lines if str(ln.get("level")) != "DEBUG"]
        self.log_pane.clear()
        self.log_pane.add_lines(info_and_above)
        self.debug_window.clear()
        self.debug_window.add_lines(log_lines)

    def _apply_failure_style(self) -> None:
        """Give the failure bar its red look via a stylesheet."""
        self.failure_bar.setStyleSheet("background-color: #b71c1c; color: white;")


#: The mockup's ``.flag`` box (amber), used for the mismatch flag line and the confirmation's file list.
_FLAG_STYLE = "background: #fff3cd; border: 1px solid #e0b100; color: #5c4300; padding: 4px 10px;"


class StagedRunScreen(RunScreenMixin, QWidget):
    """A staged Run screen bound to one :class:`HeadlessRunSession`.

    Three setup stages (UUT & Operator, Test Schedule, Steps & options) live in a
    :class:`QStackedWidget`; the Run view sits below it and is shown once a run starts.
    The session's `state_changed` signal drives every update, so starting a run never
    blocks this thread — the worker reports progress through queued signals that land here.
    The run-side handlers are the shared :class:`~ate.ui.run_widgets.RunScreenMixin`; this
    class only arranges the stages (packet 25a review).
    """

    def __init__(self, session: HeadlessRunSession, confirm_choice: Optional[Callable[[MismatchConfirmation], None]] = None, clock: Optional[Callable[[], datetime]] = None) -> None:
        super().__init__()
        self.session = session
        self.state = SessionState.IDLE
        # Injectable wall clock (packet 14d): the status bar's timers read it, so tests can
        # advance time on their own terms instead of waiting for real seconds.
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # Injected decision-maker for the run-start confirmation (ticket 06c). Tests supply one that
        # clicks OK/Cancel on the dialog instead of a real Operator; without it the dialog is exec()'d
        # as usual. The screen never blocks on it in a test.
        self.confirm_choice = confirm_choice
        # The mismatch confirmation built by the most recent Start, or None when none was needed.
        self._confirm_dialog: Optional[MismatchConfirmation] = None
        # The picker (packet 05b, 06c) owns the catalog rows, the ⚠ flags and the release labels.
        # Captured when STARTED fires; ``None`` before the first run or after FINISHED.
        self._started_at: Optional[datetime] = None
        # When the current action or step began (packet 14d); ``None`` between run start and the
        # first Action/Step. The status bar's "Current" timer reads this.
        self._item_started_at: Optional[datetime] = None
        #: The status bar's left label (packet 14d): "Ready" before a run, "Running" while it is up,
        # "Paused" during a Stop-on-Fail pause, "Finished" when it ends. The three tallies beside it
        # are what the mockup's statusBar() shows; they never overwrite each other.
        self._phase_text = "Ready"
        # Pre-/Post-test Action progress as (done, total), read from the engine's action_started.
        self._pre_counts: tuple[int, int] = (0, 0)
        self._post_counts: tuple[int, int] = (0, 0)
        # Steps that have started (a Retry counts once) and the enabled steps in the run.
        self._started_steps: set[int] = set()
        self._step_total = 0
        # The RunEngine worker for the current run, set by :meth:`_begin_run_from_selection` and
        # cleared on FINISHED so a new start() captures fresh fields; ``None`` otherwise. Exposed so
        # tests can drive inputs (supply/click) through the engine reached via the session.
        self._engine: Optional[RunEngine] = None
        # The step-list row carrying the pause highlight (packet 12d); ``None`` when no prompt is
        # pending. Dropped before refresh() clears the list, which would delete the item.
        self._failed_row: Optional[QListWidgetItem] = None

        self.uut_operator_stage = UutOperatorStage()
        self.test_schedule_stage = TestScheduleStage(session)
        self.steps_options_stage = StepsOptionsStage(self.session, screen=self)
        self.run_view = RunView(self.session, screen=self)
        # The shared run handlers (packet 25a) read these names on either layout: the Operator's
        # form, the picker, the stop-on-fail pair, the line a refused Start writes on, the Start
        # button and the two log panes. They are the stage widgets themselves, not copies.
        self.fields = self.uut_operator_stage
        self.picker = self.test_schedule_stage
        self.stop_on_fail_radio = self.steps_options_stage.stop_on_fail_radio
        self.continue_all_radio = self.steps_options_stage.continue_all_radio
        self.error_label = self.steps_options_stage.error_label
        self.start_button = self.steps_options_stage.run_button
        self.log_pane = self.run_view.log_pane
        self.debug_window = self.run_view.debug_window
        # Disabled until a run starts; _on_state_changed enables it only while STARTED. The session's
        # initial IDLE emit precedes this connection, so set the default here explicitly.
        self.run_view.abort_button.setEnabled(False)
        self.steps_stack = QStackedWidget()
        self.steps_stack.addWidget(self.uut_operator_stage)
        self.steps_stack.addWidget(self.test_schedule_stage)
        self.steps_stack.addWidget(self.steps_options_stage)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.addWidget(self.steps_stack, stretch=1)
        root.addWidget(self.run_view)
        self.run_view.hide()

        # Prefill the Project field from config's `defaultProject` (spec 08). The Station name is
        # not a Project: it is recorded on the run row separately, and never lands in this field.
        if self.session.config is not None:
            self.uut_operator_stage.project_edit.setText(self.session.config.default_project or "")
        # A Test Run cannot start with an empty Project, so the field gates Start (packet 30a r3).
        self.uut_operator_stage.project_edit.textChanged.connect(self._update_run_button)

        # Bind to the session's observable state — the single source of run truth.
        self.session.state_changed.connect(self._on_state_changed)

        # A Stop-on-Fail pause names its failing step in the prompt bar (packet 12d); the bar's
        # buttons answer it. Both are display-only — the session's choose() is the only call out.
        self.session.failure_prompt.connect(self._on_failure_prompt)
        # The pause ends when the session says it ends, not when this layout's own button is clicked
        # (packet 25b): a prompt answered in the other layout has to clear here too.
        self.session.prompt_answered.connect(self._on_prompt_answered)
        self.run_view.failure_prompt_bar.choice_made.connect(self._on_prompt_choice)

        # Status bar (packet 14d): every new Action or Step names the phase and restarts the
        # current-item timer.
        self.session.action_started.connect(self._on_action_started)
        self.session.step_started.connect(self._on_step_started)
        # Ticket 10a: Step Input widgets are driven by these two signals and answer through the
        # session's supply_input/click_poll. The layout decides nothing: asked → live, answered →
        # closed with the value showing.
        self.session.input_asked.connect(self._on_input_asked)
        self.session.input_answered.connect(self._on_input_answered)
        # A step's Attempt loop ending closes its POLL widgets: they are live only while it runs.
        self.session.step_finished.connect(self._on_step_finished)

        # The picker records the selection on the session itself (packets 05b, 06c); this screen
        # only follows it to decide whether Start is available.
        self.test_schedule_stage.selection_changed.connect(lambda *_seen: self._update_run_button())

        # Wire navigation and start; each button only advances or begins a run, never decides results.
        self.uut_operator_stage.next_button.clicked.connect(lambda: self._advance(1))
        self.test_schedule_stage.run_button.clicked.connect(self._begin_run_from_selection)
        self.steps_options_stage.run_button.clicked.connect(self.begin_run)

        # The Abort button asks the session to abort the current run; only meaningful while it is up.
        self.run_view.abort_button.clicked.connect(self._request_abort)
        # The New run button (packet 30b) goes back to the setup stages for the next run.
        self.run_view.new_run_button.clicked.connect(self.start_new_run)

        # Populate the schedule list once at construction; rescan reloads it.
        self._refresh_schedule_list()

        # A one-second timer keeps the header's elapsed time live without polling the worker.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_header)
        self._timer.start(1000)

        # A screen built without a window is the layout that acts: MainWindow names which of the two
        # layouts acts when it builds both for one session (packet 25b).
        self.set_active(True)

    def _sync_from_store(self) -> None:
        """Draw what this layout skipped while it was hidden (packet 25b).

        A finished run whose read was deferred is drawn here from the store, rows and log together;
        a run still in progress needs only the log lines it did not read, since its step rows were
        built when it started and its pause is painted by the prompt handler."""
        run_uuid = self._deferred_run_uuid
        self._deferred_run_uuid = None
        if run_uuid:
            self.run_view.refresh(
                self.session.store.nodes(run_uuid),
                self.session.store.attempts(run_uuid),
                self.session.store.log_lines(run_uuid),
            )
        else:
            self._refresh_log()

    # -- navigation ---------------------------------------------------------

    def _advance(self, index: int) -> None:
        """Move to another setup stage without starting a run."""
        self.steps_stack.setCurrentIndex(index)

    def _begin_run_from_selection(self) -> None:
        """Advance from the schedule stage to the steps stage for the selected schedule.

        Loads that schedule's expanded steps into the Steps & options stage so the Operator
        can see what will run, then shows the next stage. Selection itself was already sent
        to the session when the row was picked; this only previews its steps.
        """
        name = self.session.selected_schedule or ""
        # A broken row made current names no schedule, so there is nothing to preview and no run to
        # begin: the selection must still be a runnable row (packet 05b).
        if not name or self._selected_schedule_name() != name:
            return
        steps = self._load_schedule_steps(name)
        self.steps_options_stage.load_steps(steps)
        self.steps_stack.setCurrentIndex(2)

    def _load_schedule_steps(self, schedule_name: str) -> list[object]:
        """Load the expanded steps of ``schedule_name`` for preview (no execution)."""
        return load_schedule_steps(self.session, schedule_name)

    # -- running ------------------------------------------------------------

    def _reveal_run_view(self) -> None:
        """Show the Run view for an in-progress run and hide the setup stages.

        This screen is itself a top-level widget, so ``run_view.show()`` alone does not make
        it visible offscreen (a child only reports visible once its ancestor chain is too);
        showing this widget as well cascades visibility down to every descendant — but only
        when this layout is the page the window is showing (packet 30b review)."""
        self.steps_stack.hide()
        self.run_view.show()
        self._show_if_on_screen()

    def start_new_run(self) -> None:
        """The New run button (packet 30b): back to the first setup stage, clearing nothing.

        The spec's "New run clears nothing" is satisfied by construction: the stage widgets still
        hold Project, Operator, UUT Type, serial and the Stop-on-Fail choice, and the session keeps
        the selected schedule; Start then captures them again (``begin_run`` never clears fields).
        The session is put back to SELECTING first (packet 30b review): a finished session leaves the
        step boxes locked and the options shut, and re-selecting the same schedule is the Operator
        action that unlocks them for the next run."""
        self.new_run()
        self.steps_stack.setCurrentIndex(0)
        self.steps_stack.show()
        self.run_view.hide()
        self._show_if_on_screen()


    def _request_abort(self) -> None:
        """The Abort button ends the current run — answering a pending prompt or stopping between steps.

        With a Stop-on-Fail prompt up the worker is blocked waiting for a choice, so the
        step-boundary flag ``request_abort`` would set is never observed and the run hangs: answer
        the pending prompt with ABORT instead (the bar's own Abort path). Otherwise the worker is
        free to stop at its next step boundary (records ERROR / NOTRUN / ABORTED); ``request_abort``
        raises if there is no run in progress (the button is disabled whenever one is not)."""
        if not self.is_active():
            return  # the layout that is not showing cannot abort the run (packet 25b)
        if self.session.pause_pending():
            # The worker is blocked on a prompt: answer it with ABORT (packet 25b). The decision comes
            # from the session, not from this layout's bar — the pause may have been answered in the
            # other layout, and then this bar would be stale.
            self._on_prompt_choice("ABORT")
            return
        self.session.request_abort()

    @property
    def engine(self) -> Optional[RunEngine]:
        """The RunEngine worker for the current run, or ``None`` before it starts / after FINISHED.

        Exposed so tests can inspect the worker a run is going through; the Step Input widgets
        (ticket 10a) answer through the session's ``supply_input``/``click_poll``, never the engine."""
        return self._engine

    def _on_state_changed(self, state: SessionState) -> None:
        """React to a session state change — the only place run truth reaches the layout."""
        self.state = state
        if state == SessionState.STARTED:
            # Begin the run and reveal it. Ticket 10a: the step rows and their Step Inputs are built
            # as the run starts, so every declared input is on screen, inert, before the worker asks.
            steps = self._load_schedule_steps(self.session.selected_schedule or "")
            self.run_view.load_run_steps(steps)
            # Packet 30b review: the Log tab appends, so it must be emptied when a run starts or the
            # second run's log reads as the first run's lines plus its own.
            self.run_view.clear_run_text()
            # Status bar (packet 14d): the run clock starts here and the one-second timer restarts,
            # because a previous run stopped it at FINISHED. No Action or Step has begun yet, so the
            # current-item timer is blank; the Steps tally knows its total from the rows just built.
            self._started_at = self._clock()
            self._item_started_at = None
            self._phase_text = "Running"
            self._pre_counts = (0, 0)
            self._post_counts = (0, 0)
            self._started_steps = set()
            self._step_total = self._enabled_step_count(steps)
            self._timer.start(1000)
            self._reveal_run_view()
        elif state == SessionState.FINISHED:
            # Release the finished worker so a new start() captures fresh fields; the next run owns
            # its own engine again.
            self._engine = None
            # refresh() clears the step list and deletes its items — drop the pause highlight's row
            # reference before that so we never touch a deleted item (packet 12d).
            self._failed_row = None
            # Read results from the store (never compute them here) and refresh the view.
            run_uuid = self.session.run_uuid
            if self.is_active():
                # Read results from the store (never compute them here) and refresh the view.
                nodes = self.session.store.nodes(run_uuid) if run_uuid else []
                attempts = self.session.store.attempts(run_uuid) if run_uuid else []
                log_lines = self.session.store.log_lines(run_uuid) if run_uuid else []
                self.run_view.refresh(nodes, attempts, log_lines)
            else:
                # The layout that is not showing defers the read and draws it when it comes back
                # (packet 25b review); meanwhile every input it holds is closed, since nothing can
                # be answered on a run that is over.
                for widgets in self.run_view.input_widgets.values():
                    for widget in widgets:
                        widget.deactivate()
                self.defer_finished_run(run_uuid)
            # The run is over: the status bar keeps these final timings (packet 14d).
            self._phase_text = "Finished"
            # Packet 30b: the finished run stays as the run view — the mockup's variant C finished
            # state has no setup stages beside it. Re-showing the stages here used to make the
            # layout's minimumSizeHint taller than the window, and the window grew to fit it,
            # shrinking the step list to ~68 px and pushing the status bar down. "New run" is what
            # takes the Operator back to the setup stages, fields kept.
            self.steps_stack.hide()
            self.run_view.show()
        elif state in (SessionState.IDLE, SessionState.SELECTING):
            # Back on the setup stages; show them again and hide the Run view.
            self.run_view.hide()
            self.steps_stack.show()

        # Step checkboxes (packet 11b): checkable in setup, locked once a run starts and kept
        # locked until it ends; checkable again only for the next run's setup.
        self.steps_options_stage.set_checkable(state in (SessionState.IDLE, SessionState.SELECTING))

        # The Abort button is only meaningful while a run is in progress.
        self.run_view.abort_button.setEnabled(state == SessionState.STARTED)
        # Packet 30b: the New run button offers the next run once this one is over.
        self.run_view.new_run_button.setEnabled(state == SessionState.FINISHED)

        self._update_header(state)
        if state == SessionState.FINISHED:
            # Final state reached: stop the one-second timer so the header and status bar keep
            # their final values on screen (packet 14d).
            self._timer.stop()

    def _update_header(self, state: Optional[SessionState] = None, now: Optional[datetime] = None) -> None:
        """Refresh the header's state label and the status bar's live timers.

        ``now`` is the injectable clock's reading (packet 14d): a test can emit the QTimer's
        ``timeout`` to tick the bar without waiting for real seconds. The status bar updates
        even while a failure prompt is pending."""
        if now is None:
            now = self._clock()
        self._update_status_bar(now)
        if self.run_view.failure_prompt_bar.isVisible():
            # A failure prompt is pending: the header waits on the Operator, whatever the state
            # (this also guards the one-second timer while paused) — packet 12d.
            self.run_view.state_header.setText("WAITING")
            return
        if state is None:
            state = self.state
        text = f"{state.value}"
        if self._started_at is not None and state in (SessionState.STARTED, SessionState.FINISHED):
            delta = now - self._started_at
            text += f"  ·  {_format_elapsed(delta)}"
        self.run_view.state_header.setText(text)

    def _update_status_bar(self, now: datetime) -> None:
        """Refresh the status bar (packet 14d): the state, the three tallies and both timers.

        Pre-test, Steps and Post-test progress are on the bar at the same time — starting an Action
        or a Step never overwrites another phase's tally. The Run and Current timers keep ticking
        through a Stop-on-Fail pause: the bar measures real elapsed time, which is how the Operator
        can tell that something has hung."""
        view = self.run_view
        view.status_label.setText(self._phase_text)
        view.status_pre.setText(f"Pre-test {self._pre_counts[0]}/{self._pre_counts[1]}")
        view.status_steps.setText(f"Steps {len(self._started_steps)}/{self._step_total}")
        view.status_post.setText(f"Post-test {self._post_counts[0]}/{self._post_counts[1]}")
        if self._started_at is None:
            view.status_total.setText("Run --:--")
            view.status_current.setText("Current --:--")
            return
        view.status_total.setText(f"Run {_format_elapsed(now - self._started_at)}")
        if self._item_started_at is None:
            view.status_current.setText("Current --:--")
        else:
            view.status_current.setText(f"Current {_format_elapsed(now - self._item_started_at)}")




    # -- Stop-on-Fail prompt (packet 12d) -------------------------------------

    def _on_failure_prompt(self, node_seq: int, attempt_no: int) -> None:
        """A failure is awaiting the Operator's choice: name it in the bar and pause the display.

        The failing step's node row is not written yet (the engine writes it after the last
        Attempt), so the step's number and name come from the schedule's expanded steps and its
        result and message from the Attempt row the pause was raised on."""
        step = self._step_identity(node_seq)
        attempt = self._attempt_row(node_seq, attempt_no)
        result = str(attempt.get("pass_fail") or "FAIL") if attempt else "FAIL"
        message = str(attempt.get("msg") or "") if attempt else ""
        self.run_view.failure_prompt_bar.show_prompt(str(getattr(step, "step_no", "") or ""), result, message)
        self._render_paused_steps(node_seq, result)
        self._set_paused_header()

    def _step_identity(self, node_seq: int):
        """The selected schedule's expanded step at ``node_seq`` (its number and name)."""
        steps = self._load_schedule_steps(self.session.selected_schedule or "")
        if 0 <= node_seq < len(steps):
            return steps[node_seq]
        return None

        for row in self.session.store.attempts(run_uuid):
            if row.get("node_seq") == node_seq and row.get("attempt_no") == attempt_no:
                return row
        return None

    def _render_paused_steps(self, node_seq: int, result: str) -> None:
        """Rebuild the step list mid-run: done steps from the store, the failing one marked, the rest pending.

        Rows normally only exist after FINISHED (refresh), so a pause renders its own list; the
        failing step's row gets the pause highlight and is remembered so the choice can clear it.
        Packet 10a review: the rows are updated in place (``RunView._render_rows``) rather than
        cleared and re-added — destroying an item that carries a Step Input widget deletes that
        widget, and the pause then painted a dead widget (SIGSEGV while the run is paused)."""
        run_uuid = self.session.run_uuid
        nodes = self.session.store.nodes(run_uuid) if run_uuid else []
        done = {node.get("node_seq"): node for node in nodes if node.get("kind") == "step"}
        steps = self._load_schedule_steps(self.session.selected_schedule or "")
        # Packet 15d: header rows are styled like the finished list and roll up the steps beneath
        # them, so a paused run still shows how far each Sub-schedule got.
        children = _children_of_rows(steps, lambda step: getattr(step, "parent", None))
        rows: list[tuple] = []
        failed_index = None
        for seq, step in enumerate(steps):
            text = _row_label(getattr(step, "step_no", ""), getattr(step, "name", ""), int(getattr(step, "depth", 0)))
            if getattr(step, "is_header", False):  # a header row has no result, as in the finished list
                rollup = _rollup_text(seq, children,
                                      lambda index: getattr(steps[index], "is_header", False),
                                      lambda index: done[index].get("result") if index in done else None)
                rows.append((text + (f"  {rollup}" if rollup else ""), None, True, QColor(_HEADER_BG), 0))
                continue
            if seq == node_seq:
                state = result
            elif seq in done:
                state = str(done[seq].get("result", ""))
            else:
                state = "…"
            # The pause highlight: a pale red row, cleared once the Operator chooses.
            if seq == node_seq:
                failed_index = len(rows)
            rows.append((f"{text}  →  {state}", None, False, QColor("#fff0f0") if seq == node_seq else None, 0))
            for widget in self.run_view.input_widgets.get(seq, ()):
                rows.append(("", widget, False, None, 0))
        items = self.run_view._render_rows(rows)
        self._failed_row = items[failed_index] if failed_index is not None else None

    def _set_paused_header(self) -> None:
        """While a prompt is pending the header goes red and reads WAITING (see _update_header), and
        the status bar says "Paused" (packet 14d) — its timers keep running, because the pause is
        real time the Operator is spending on the run."""
        self.run_view.state_header.setStyleSheet("background-color: #c62828; color: white;")
        self._phase_text = "Paused"
        self._update_header()

    def _on_prompt_choice(self, choice: str) -> None:
        """A prompt was answered (a bar button, or the header Abort while one is up): clear the
        pause's display, then answer the session.

        Clearing first means a run that finishes straight away can refresh() without a highlight
        still held on an item refresh() would delete; the session's worker unblocks on choose().
        The bar hides itself in _on_choice, but the header-Abort path answers without the bar, so
        hide it here too (a no-op when the bar already hid itself)."""
        if not self.is_active():
            return  # the hidden layout's prompt bar never answers the pause (packet 25b)
        self.run_view.failure_prompt_bar.hide()
        self._clear_paused_display()
        self.session.choose(choice)

    def _clear_paused_display(self) -> None:
        """Drop the pause highlight and red header once the Operator has chosen."""
        if self._failed_row is not None:
            self._failed_row.setBackground(QBrush())
            self._failed_row = None
        self.run_view.state_header.setStyleSheet("")
        self._phase_text = "Running"  # the bar is back on the run's state (packet 14d)
        self._update_header()

    def _on_prompt_answered(self, node_seq: int, attempt_no: int, choice: str) -> None:
        """The session says the pause is over: clear this layout's prompt UI, whoever answered it.

        Packet 25b: a prompt answered in the layout that was showing must not leave the other one
        still holding a bar, a red highlight and "Paused" — the run resumed, so both layouts say so.
        This is driven by the session's signal, never by this layout's own button."""
        self.run_view.failure_prompt_bar.hide()
        self._clear_paused_display()

    def _paused_prompt_widget(self):
        """The prompt bar's Continue button while this layout has a prompt up."""
        bar = self.run_view.failure_prompt_bar
        return bar.continue_button if bar.isVisible() else None

    # -- Step Inputs (ticket 10a) ---------------------------------------------

    def step_input_widgets(self) -> dict[int, list[StepInputWidget]]:
        """The Run view's Step Input widgets, keyed by the seq of the step that declared each one."""
        return self.run_view.input_widgets

    def _on_input_asked(self, node_seq: int, attempt_no: int, index: int) -> None:
        """The worker is waiting on this step's n-th input, so its widget becomes live.

        A blocking input is enabled only while its ``ATE.input(n)`` call waits; a POLL opens when
        its step's inputs are announced, which is while that step runs. The mockup's `focusActive`
        puts the cursor in the input that is currently asking, which `activate` does. Nothing here
        decides whether the input is answerable — the session announced it, so the layout renders it.
        """
        widget = self.input_widget(node_seq, index)
        if widget is None:
            return
        widget.activate(attempt_no)
        # The mockup scrolls the asking row into view so the Operator sees it (packet 10a review).
        if widget.input_item is not None:
            self.run_view.step_list.scrollToItem(widget.input_item)

    def _on_step_finished(self, node_seq: int) -> None:
        """One step's Attempts are done: nothing may answer its POLL any more, so close its widgets."""
        for widget in self.run_view.input_widgets.get(node_seq, ()):
            if widget.declaration.type == "POLL":
                widget.deactivate()


    # -- schedule list ------------------------------------------------------

    def _refresh_schedule_list(self) -> None:
        """Reload the picker from the catalog — the shared SchedulePicker does the rendering."""
        self.test_schedule_stage.refresh()

    def _render_schedule_rows(self) -> None:
        """Rebuild the picker, applying the filter box's text."""
        self.test_schedule_stage.render()

    def _selected_schedule_name(self) -> Optional[str]:
        """The schedule the picker's current row stands for, or ``None`` if it cannot run."""
        return self.test_schedule_stage.selected_name()

    def _update_run_button(self) -> None:
        """Start is enabled only while a runnable schedule is the current selection.

        The Steps stage's Start button follows the same rule (packet 05b review): the picked
        schedule can break between stages — a Rescan finds a bad file — and `begin_run` would then
        raise out of the button's handler. A blank Project keeps Start away too (spec 08: a Test
        Run cannot start with an empty Project), with the reason on the stage's error line."""
        runnable = self._selected_schedule_name() is not None
        project = self.uut_operator_stage.project_edit.text().strip()
        self.test_schedule_stage.run_button.setEnabled(runnable and bool(project))
        self.steps_options_stage.run_button.setEnabled(runnable and bool(project))
        if runnable:
            self.steps_options_stage.error_label.setText(
                "" if project else f"{BROKEN_MARKER} Project is required before a Test Run can start"
            )
