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
from typing import Iterable, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ate.engine.run_engine import RunEngine

from ..catalog import list_schedules, module_map, resolve_schedule_definition, resolve_schedule_path
from ..engine.lua_host import LuaHost
from ..run_session.headless import HeadlessRunSession, SessionState


def _format_elapsed(delta: datetime) -> str:
    """Format a ``timedelta`` as ``MM:SS`` or ``HH:MM:SS`` for the run header."""
    total = int(delta.total_seconds())
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _format_log_line(line: dict) -> str:
    """Render one recorded log line as ``LEVEL  SOURCE  text`` for a pane."""
    level = str(line.get("level", ""))
    source = str(line.get("source", ""))
    return f"{level:<7} {source:<8} {line.get('text', '')}"


class _LogPane(QWidget):
    """A scrollable pane that renders recorded log lines under a header label.

    Both the Run view's Log pane (ticket 08) and its separate Debug window are instances of
    this: one is fed ``INFO``-and-above lines, the other every level. It never decides what
    to show — :meth:`add_lines` only renders whatever store rows it is handed, in seq order.
    """

    def __init__(self, title: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._title = QLabel(title)
        self._title.setStyleSheet("font-weight: bold;")
        self._list = QListWidget()
        self._area = QScrollArea()
        self._area.setWidget(self._list)
        self._area.setWidgetResizable(True)
        layout.addWidget(self._title)
        layout.addWidget(self._area, stretch=1)

    def clear(self) -> None:
        """Remove every rendered line."""
        self._list.clear()

    def add_lines(self, lines: Iterable[dict]) -> None:
        """Render ``lines`` (each a log_line dict) as level/source/text rows in seq order."""
        for line in sorted(lines, key=lambda ln: ln.get("seq", 0)):
            self._list.addItem(QListWidgetItem(_format_log_line(line)))

    def rendered_lines(self) -> list[str]:
        """Return the pane's lines in order (for inspection and tests)."""
        return [self._list.item(i).text() for i in range(self._list.count())]


class UutOperatorStage(QWidget):
    """Stage one: the Operator and UUT fields.

    Holds no run logic — it only owns four text inputs (Operator, Project, UUT type,
    UUT serial) plus a button that advances to the Test Schedule stage. The screen wires
    the button up; nothing here calls into the session until a run actually starts.
    """

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.operator_edit = QLineEdit()
        self.project_edit = QLineEdit()
        self.uut_type_edit = QLineEdit()
        self.serial_edit = QLineEdit()

        for widget in (self.operator_edit, self.project_edit, self.uut_type_edit, self.serial_edit):
            layout.addWidget(widget)

        self.next_button = QPushButton("Next")
        layout.addWidget(self.next_button)


class TestScheduleStage(QWidget):
    """Stage two: the list of available schedules and their notes.

    ``schedule_list`` is populated from the catalog; ``rescan_button`` reloads it; and
    ``notes_label`` shows the description of whatever row is selected. Selecting a row
    calls back into the session (``session.select_schedule``) — that binding lives here,
    which is where picking a schedule belongs.
    """

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.schedule_list = QListWidget()
        self.notes_label = QLabel()
        self.rescan_button = QPushButton("Rescan schedules")
        self.run_button = QPushButton("Run this schedule")

        layout.addWidget(self.schedule_list)
        layout.addWidget(self.notes_label)
        layout.addWidget(self.rescan_button)
        layout.addWidget(self.run_button)


class StepsOptionsStage(QWidget):
    """Stage three: the run's steps and the stop-on-fail option.

    ``load_steps`` fills ``step_list`` with the selected schedule's expanded steps so the
    Operator can confirm what will run; ``stop_on_fail_radio`` is prefilled checked (True)
    as the default policy, paired with a "run all" alternative. The stage's Run button
    starts the run through the screen.
    """

    def __init__(self, session: Optional["HeadlessRunSession"] = None) -> None:
        super().__init__()
        self.session = session
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.step_list = QListWidget()
        # Checkboxes are per step (packet 11b): each row is checkable and a toggle is one call
        # into session.toggle_node — the widget holds no logic of its own.
        self._checkable = True  # setup state: the boxes stay checkable until a run starts
        self._loading = False  # True while load_steps re-syncs check states, so itemChanged
        # is not mistaken for an Operator toggle.
        self._states: dict[int, bool] = {}  # each row's last-seen checked state
        self.step_list.itemChanged.connect(self._on_check_toggled)
        self.stop_on_fail_radio = QRadioButton("Stop on first failure")
        self.continue_all_radio = QRadioButton("Run every step regardless of failures")
        # Default policy: stop on the first failure; :meth:`load_steps` re-applies it from the
        # selected schedule's Stop on Fail default once a schedule has been chosen.
        self.stop_on_fail_radio.setChecked(True)
        self.run_button = QPushButton("Start run")

        layout.addWidget(self.step_list)
        layout.addWidget(self.stop_on_fail_radio)
        layout.addWidget(self.continue_all_radio)
        layout.addWidget(self.run_button)

    def load_steps(self, steps: Iterable[object]) -> None:
        """Show ``steps`` (each with ``step_no``, ``name``, ``enabled``) as checkable rows.

        Every row carries its step's default ``enabled`` state, or — when the session carries
        step-enable overrides for the selected schedule — the override that a next run would
        record, so the boxes show what the run will record (spec 37: options carry over). A
        toggle is one call into ``session.toggle_node(row, checked)`` (packet 11b), with the row's
        position in the expanded step list as the runner seq. Also re-applies the selected
        schedule's Stop on Fail default to the radio pair so it is not stuck at construction-
        time; the Operator can still toggle either choice."""
        self._loading = True
        self.step_list.clear()
        self._states = {}
        overrides = self.session.node_enables if self.session is not None else None
        if overrides is not None:
            overrides = dict(overrides)
        for step in steps:
            label = getattr(step, "name", "") or ""
            number = getattr(step, "step_no", "") or ""
            text = f"{number}  {label}" if number else str(label)
            item = QListWidgetItem(text)
            # Always keep the row enabled and selectable and the box drawn; only
            # ItemIsUserCheckable toggles with the run lock (a locked box still shows its
            # check state). NoItemFlags would disable the row even in setup.
            checkable = Qt.ItemFlag.ItemIsUserCheckable if self._checkable else Qt.ItemFlag.NoItemFlags
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | checkable)
            self.step_list.addItem(item)
            row = self.step_list.row(item)
            default = bool(getattr(step, "enabled", True))
            checked = overrides.get(row, default) if overrides else default
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self._states[row] = checked
        self._loading = False
        if self.session is not None:
            self._apply_stop_on_fail(self.session.selected_stop_on_fail)

    def _on_check_toggled(self, item: QListWidgetItem, *_args: int) -> None:
        """A checkbox toggle reaches the session as ``toggle_node(row, checked)`` — no logic here.

        PyQt6's ``itemChanged`` signal passes only the row, so the handler reads the row's new
        check state and compares it against the state it last saw; flag re-syncs (set_checkable),
        same-state events and the load_steps re-sync are all ignored, so only a real toggle
        reaches the session. A locked box (a run in progress) has no toggle: the handler
        returns before touching the session."""
        if self._loading or self.session is None or not self._checkable:
            return
        row = self.step_list.row(item)
        checked = item.checkState() == Qt.CheckState.Checked
        if self._states.get(row) == checked:
            return
        self._states[row] = checked
        self.session.toggle_node(row, checked)

    def set_checkable(self, checkable: bool) -> None:
        """Lock or unlock the step checkboxes: toggle only ItemIsUserCheckable so the
        boxes stay drawn with their check state and the row stays enabled and selectable
        (the screen calls this from session state changes)."""
        self._checkable = checkable
        # Re-sync the last-seen state before the flag pass, so the flag events the pass emits
        # are read back as the same state and never reach the session as phantom toggles.
        for i in range(self.step_list.count()):
            item = self.step_list.item(i)
            self._states[i] = item.checkState() == Qt.CheckState.Checked
        keep = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        flag = keep | (Qt.ItemFlag.ItemIsUserCheckable if checkable else Qt.ItemFlag.NoItemFlags)
        for i in range(self.step_list.count()):
            item = self.step_list.item(i)
            item.setFlags(item.flags() & keep | flag)

    def _apply_stop_on_fail(self, value: bool) -> None:
        """Set the radio pair to ``value`` (True = stop on first failure)."""
        self.stop_on_fail_radio.setChecked(value)
        self.continue_all_radio.setChecked(not value)


class FailurePromptBar(QWidget):
    """The Stop-on-Fail prompt bar shown while a failed step awaits the Operator's choice.

    Rendered only while a prompt is pending — the screen shows it with :meth:`show_prompt` and
    it hides itself once a choice is made — and it holds no run logic of its own: each button
    only hides the bar and emits ``choice_made`` with CONTINUE, RETRY or ABORT, which the
    screen sends on to the session. The layout follows the run-screen mockup (variant C): a
    bold ``Step <no> <result>`` label, the step's message growing across the bar, then the
    three buttons in order.
    """

    choice_made = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        # A plain QWidget never paints its stylesheet background, so the bar showed up grey
        # (packet 12d review): opt it into styled backgrounds, and give the labels their own
        # colour rule because a stylesheet `color` does not cascade to child QLabels.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(12)

        self.step_label = QLabel()
        bold = self.step_label.font()
        bold.setBold(True)
        self.step_label.setFont(bold)
        self.message_label = QLabel()
        # The message grows to fill whatever width the labels leave, like the mockup's .grow.
        self.message_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self.continue_button = QPushButton("Continue")
        self.continue_button.setToolTip("Keep the result and move on")
        self.retry_button = QPushButton("Retry")
        self.retry_button.setToolTip("Run the step's action again")
        self.abort_button = QPushButton("Abort")
        self.abort_button.setToolTip("Run the Post-test Actions and end the run")
        # Red bar, 15px type, and the mockup's button classes: plain Continue, primary Retry,
        # danger Abort.
        self.setStyleSheet(
            "FailurePromptBar { background: #c62828; color: white; font-size: 15px; }"
            "FailurePromptBar QLabel { color: white; }"
            "FailurePromptBar QPushButton { background: #f5f5f5; color: black; font-size: 15px; "
            "padding: 6px 18px; border: 1px solid #a9a9a9; border-radius: 3px; }"
            "FailurePromptBar QPushButton:hover { background: #e6e6e6; }"
            "FailurePromptBar QPushButton#retry { background: #0a64d6; color: white; border-color: #0850ad; }"
            "FailurePromptBar QPushButton#abort { background: #c62828; color: white; border-color: #8e1c1c; }"
        )
        self.retry_button.setObjectName("retry")
        self.abort_button.setObjectName("abort")

        self.continue_button.clicked.connect(lambda: self._on_choice("CONTINUE"))
        self.retry_button.clicked.connect(lambda: self._on_choice("RETRY"))
        self.abort_button.clicked.connect(lambda: self._on_choice("ABORT"))

        layout.addWidget(self.step_label)
        layout.addWidget(self.message_label)
        layout.addWidget(self.continue_button)
        layout.addWidget(self.retry_button)
        layout.addWidget(self.abort_button)

        # Only shown while a prompt is pending; the screen shows it via show_prompt.
        self.hide()

    def show_prompt(self, step_number: str, result: str, message: str) -> None:
        """Show the bar naming the failed step and its message, for the Operator to answer."""
        self.step_label.setText(f"Step {step_number} {result}")
        self.message_label.setText(message)
        self.show()

    def _on_choice(self, choice: str) -> None:
        """A button was clicked: hide the bar first, then hand the choice to the screen."""
        if not self.isVisible():
            return  # a second click after the bar already hid itself must not answer twice
        self.hide()
        self.choice_made.emit(choice)


class RunView(QWidget):
    """The view shown while and after a run: state header, live steps, Log/Summary.

    ``state_header`` shows the current :class:`SessionState` plus a live elapsed timer;
    ``step_list`` lists each step with its per-step Result (the latest step enlarged);
    ``failure_bar`` is a full-width bar that surfaces any failing or errored step; and
    ``tabs`` holds the attempt Log and a run Summary. All content is refreshed from the
    store by :meth:`refresh`, never computed here.
    """

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.state_header = QLabel()
        # Whole-run Abort control (packets 13a): asks the session to abort the current run so it
        # stops between steps and records ERROR / NOTRUN / ABORTED. Enabled only while a run is up;
        # the screen toggles it in _on_state_changed.
        self.abort_button = QPushButton("Abort")
        self.step_list = QListWidget()
        # Full-width failure bar: a fixed-height label that spans the view's width in the layout.
        self.failure_bar = QLabel()
        self.failure_bar.setFixedHeight(24)
        # The Stop-on-Fail prompt bar (packet 12d): hidden until the session pauses on a failure.
        self.failure_prompt_bar = FailurePromptBar()

        self.tabs = QTabWidget()
        log_tab = QLabel()
        summary_tab = QLabel()
        self.tabs.addTab(log_tab, "Log")
        self.tabs.addTab(summary_tab, "Summary")

        # Ticket 08's two log views: the Log pane shows INFO and above; the Debug window is a
        # separate pane showing every level. Both are filled from session.store.log_lines() in
        # refresh(), never computed here.
        self.log_pane = _LogPane("Log — INFO and above")
        self.debug_window = _LogPane("Debug — every level")

        layout.addWidget(self.state_header)
        layout.addWidget(self.abort_button)
        layout.addWidget(self.failure_bar)
        layout.addWidget(self.step_list, stretch=1)
        layout.addWidget(self.failure_prompt_bar)
        layout.addWidget(self.tabs)
        layout.addWidget(self.log_pane)
        layout.addWidget(self.debug_window)

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

        self.step_list.clear()
        failed_steps: list[str] = []
        for node in nodes:
            if node.get("kind") != "step":
                continue
            result = str(node.get("result", ""))
            item = QListWidgetItem(f"{node.get('step_number', '')}  {node.get('step_name', '')}  →  {result}")
            self.step_list.addItem(item)
        # The latest step is the current one; enlarge it so the eye lands on where we are.
        if self.step_list.count() > 1:
            last = self.step_list.item(self.step_list.count() - 1)
            font = last.font()
            font.setPointSize(font.pointSize() + 2)
            font.setBold(True)
            last.setFont(font)

        failed = any(node.get("result") in ("FAIL", "ERROR") for node in nodes)
        if failed:
            names = [node.get("step_name", "") for node in nodes if node.get("result") in ("FAIL", "ERROR")]
            self.failure_bar.setText(f"Run FAILED — {len(names)} step(s): {'; '.join(names)}")
            self._apply_failure_style()
        else:
            self.failure_bar.setText("")

        for attempt in attempts:
            message = attempt.get("msg") or ""
            log_tab.setText(log_tab.text() + f"{attempt.get('pass_fail', '')}  {message}\n")

        results = [node.get("result", "") for node in nodes if node.get("kind") == "step"]
        verdict = "FAIL" if failed else ("PASS" if results else "NOTRUN")
        lines = [f"Result: {verdict}"]
        for node in nodes:
            if node.get("kind") != "step":
                continue
            lines.append(f"{node.get('step_number', '')}  {node.get('step_name', '')}  →  {node.get('result', '')}")
        summary_tab.setText("\n".join(lines))

        # The Log pane shows INFO and above; the Debug window keeps every level.
        info_and_above = [ln for ln in log_lines if str(ln.get("level")) != "DEBUG"]
        self.log_pane.clear()
        self.log_pane.add_lines(info_and_above)
        self.debug_window.clear()
        self.debug_window.add_lines(log_lines)

    def _apply_failure_style(self) -> None:
        """Give the failure bar its red look via a stylesheet."""
        self.failure_bar.setStyleSheet("background-color: #b71c1c; color: white;")


class StagedRunScreen(QWidget):
    """A staged Run screen bound to one :class:`HeadlessRunSession`.

    Three setup stages (UUT & Operator, Test Schedule, Steps & options) live in a
    :class:`QStackedWidget`; the Run view sits below it and is shown once a run starts.
    The session's ``state_changed`` signal drives every update, so starting a run never
    blocks this thread — the worker reports progress through queued signals that land here.
    """

    def __init__(self, session: HeadlessRunSession) -> None:
        super().__init__()
        self.session = session
        self.state = SessionState.IDLE
        # Captured when STARTED fires; ``None`` before the first run or after FINISHED.
        self._started_at: Optional[datetime] = None
        # The RunEngine worker for the current run, set by :meth:`_begin_run_from_selection` and
        # cleared on FINISHED so a new start() captures fresh fields; ``None`` otherwise. Exposed so
        # tests can drive inputs (supply/click) through the engine reached via the session.
        self._engine: Optional[RunEngine] = None
        # The step-list row carrying the pause highlight (packet 12d); ``None`` when no prompt is
        # pending. Dropped before refresh() clears the list, which would delete the item.
        self._failed_row: Optional[QListWidgetItem] = None

        self.uut_operator_stage = UutOperatorStage()
        self.test_schedule_stage = TestScheduleStage()
        self.steps_options_stage = StepsOptionsStage(self.session)
        self.run_view = RunView()
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

        # Prefill the Project field from the station name in config (ticket 03). This is
        # a display convenience only; it does not capture into the session.
        if self.session.config is not None:
            self.uut_operator_stage.project_edit.setText(self.session.config.station_name or "")

        # Bind to the session's observable state — the single source of run truth.
        self.session.state_changed.connect(self._on_state_changed)

        # A Stop-on-Fail pause names its failing step in the prompt bar (packet 12d); the bar's
        # buttons answer it. Both are display-only — the session's choose() is the only call out.
        self.session.failure_prompt.connect(self._on_failure_prompt)
        self.run_view.failure_prompt_bar.choice_made.connect(self._on_prompt_choice)

        # Selecting a row records that selection on the session, so starting later resolves
        # exactly what the Operator picked; the notes label mirrors it for confirmation.
        self.test_schedule_stage.schedule_list.itemSelectionChanged.connect(self._on_schedule_selected)

        # Wire navigation and start; each button only advances or begins a run, never decides results.
        self.uut_operator_stage.next_button.clicked.connect(lambda: self._advance(1))
        self.test_schedule_stage.run_button.clicked.connect(self._begin_run_from_selection)
        self.steps_options_stage.run_button.clicked.connect(self.begin_run)

        # The Abort button asks the session to abort the current run; only meaningful while it is up.
        self.run_view.abort_button.clicked.connect(self._request_abort)

        # Populate the schedule list once at construction; rescan reloads it.
        self._refresh_schedule_list()

        # A one-second timer keeps the header's elapsed time live without polling the worker.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_header)
        self._timer.start(1000)

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
        if not name:
            return
        steps = self._load_schedule_steps(name)
        self.steps_options_stage.load_steps(steps)
        self.steps_stack.setCurrentIndex(2)

    def _on_schedule_selected(self) -> None:
        """Record the newly selected schedule on the session and mirror it in the notes label.

        The exact name is read back from the row's user data (set when the list was built),
        so this hands the session precisely what the Operator picked; ``select_schedule`` then
        moves the session to SELECTING while idle."""
        item = self.test_schedule_stage.schedule_list.currentItem()
        if item is not None:
            name = item.data(Qt.ItemDataRole.UserRole) or ""
            if name:
                self.session.select_schedule(name)
            self.test_schedule_stage.notes_label.setText(item.text())

    def _load_schedule_steps(self, schedule_name: str) -> list[object]:
        """Load the expanded steps of ``schedule_name`` for preview (no execution)."""
        config = self.session.config
        if config is None:
            return []
        definition = resolve_schedule_definition(*config.test_definition_dirs, schedule_name=schedule_name)
        if definition is None:
            return []
        modules = module_map(definition)  # packet 06a: forward the release's require'd module map to the preview host
        path = resolve_schedule_path(definition, schedule_name)
        if path is None:
            return []
        host = LuaHost(path, modules=modules)
        return list(host.schedule.steps)

    # -- running ------------------------------------------------------------

    def _reveal_run_view(self) -> None:
        """Show the Run view for an in-progress run and hide the setup stages.

        This screen is itself a top-level widget, so ``run_view.show()`` alone does not make
        it visible offscreen (a child only reports visible once its ancestor chain is too);
        showing this widget as well cascades visibility down to every descendant."""
        self.steps_stack.hide()
        self.run_view.show()
        self.show()

    def begin_run(self) -> None:
        """Capture the Operator's fields and start a run. Clears nothing on purpose.

        Fields are only pushed to the session when a widget is non-empty, so a new run keeps
        every field it did not change; blanking one drops that value for this run. The run
        then starts through ``session.start()`` and the Run view is shown. This never blocks:
        ``start()`` queues its signals and returns immediately.
        """
        operator = self.uut_operator_stage.operator_edit.text().strip()
        project = self.uut_operator_stage.project_edit.text().strip()
        uut_type = self.uut_operator_stage.uut_type_edit.text().strip()
        serial = self.uut_operator_stage.serial_edit.text().strip()
        # The stop-on-fail choice is a boolean toggle, so it always applies (default True).
        self.session.set_stop_on_fail(self.steps_options_stage.stop_on_fail_radio.isChecked())

        if operator:
            self.session.set_operator(operator)
        if project:
            self.session.set_project(project)
        if uut_type:
            self.session.set_uut_type(uut_type)
        if serial:
            self.session.set_uut_serial(serial)

        engine = self.session.start()  # returns the RunEngine; start is already queued
        self._engine = engine
        self._started_at = datetime.now(timezone.utc)
        # Switch to the Run view and hide the setup stages for the duration of the run.
        self._reveal_run_view()

    def _request_abort(self) -> None:
        """The Abort button ends the current run — answering a pending prompt or stopping between steps.

        With a Stop-on-Fail prompt up the worker is blocked waiting for a choice, so the
        step-boundary flag ``request_abort`` would set is never observed and the run hangs: answer
        the pending prompt with ABORT instead (the bar's own Abort path). Otherwise the worker is
        free to stop at its next step boundary (records ERROR / NOTRUN / ABORTED); ``request_abort``
        raises if there is no run in progress (the button is disabled whenever one is not)."""
        if self.run_view.failure_prompt_bar.isVisible():
            self._on_prompt_choice("ABORT")
            return
        self.session.request_abort()

    @property
    def engine(self) -> Optional[RunEngine]:
        """The RunEngine worker for the current run, or ``None`` before it starts / after FINISHED.

        Exposed so tests can drive inputs (supply/click) through the engine reached via the session;
        the GUI never needs it — input widgets are a later ticket."""
        return self._engine

    def _on_state_changed(self, state: SessionState) -> None:
        """React to a session state change — the only place run truth reaches the layout."""
        self.state = state
        if state == SessionState.STARTED:
            # Begin the run and reveal it.
            self._started_at = datetime.now(timezone.utc)
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
            nodes = self.session.store.nodes(run_uuid) if run_uuid else []
            attempts = self.session.store.attempts(run_uuid) if run_uuid else []
            log_lines = self.session.store.log_lines(run_uuid) if run_uuid else []
            self.run_view.refresh(nodes, attempts, log_lines)
            # Return to the first setup stage so a new run can be started.
            self.steps_stack.show()
            self.steps_stack.setCurrentIndex(0)
        elif state in (SessionState.IDLE, SessionState.SELECTING):
            # Back on the setup stages; show them again and hide the Run view.
            self.run_view.hide()
            self.steps_stack.show()

        # Step checkboxes (packet 11b): checkable in setup, locked once a run starts and kept
        # locked until it ends; checkable again only for the next run's setup.
        self.steps_options_stage.set_checkable(state in (SessionState.IDLE, SessionState.SELECTING))

        # The Abort button is only meaningful while a run is in progress.
        self.run_view.abort_button.setEnabled(state == SessionState.STARTED)

        self._update_header(state)

    def _update_header(self, state: Optional[SessionState] = None) -> None:
        """Refresh the header's state label and live elapsed timer."""
        if self.run_view.failure_prompt_bar.isVisible():
            # A failure prompt is pending: the header waits on the Operator, whatever the state
            # (this also guards the one-second timer while paused) — packet 12d.
            self.run_view.state_header.setText("WAITING")
            return
        if state is None:
            state = self.state
        text = f"{state.value}"
        if self._started_at is not None and state in (SessionState.STARTED, SessionState.FINISHED):
            delta = datetime.now(timezone.utc) - self._started_at
            text += f"  ·  {_format_elapsed(delta)}"
        self.run_view.state_header.setText(text)

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

    def _attempt_row(self, node_seq: int, attempt_no: int) -> Optional[dict]:
        """The Attempt row a pause was raised on — its result and message, read from the store."""
        run_uuid = self.session.run_uuid
        if run_uuid is None:
            return None
        for row in self.session.store.attempts(run_uuid):
            if row.get("node_seq") == node_seq and row.get("attempt_no") == attempt_no:
                return row
        return None

    def _render_paused_steps(self, node_seq: int, result: str) -> None:
        """Rebuild the step list mid-run: done steps from the store, the failing one marked, the rest pending.

        Rows normally only exist after FINISHED (refresh), so a pause renders its own list; the
        failing step's row gets the pause highlight and is remembered so the choice can clear it."""
        run_uuid = self.session.run_uuid
        nodes = self.session.store.nodes(run_uuid) if run_uuid else []
        done = {node.get("node_seq"): node for node in nodes if node.get("kind") == "step"}
        steps = self._load_schedule_steps(self.session.selected_schedule or "")
        self.run_view.step_list.clear()
        self._failed_row = None
        for seq, step in enumerate(steps):
            number = str(getattr(step, "step_no", "") or "")
            name = str(getattr(step, "name", "") or "")
            if seq == node_seq:
                state = result
            elif seq in done:
                state = str(done[seq].get("result", ""))
            else:
                state = "…"
            item = QListWidgetItem(f"{number}  {name}  →  {state}")
            if seq == node_seq:
                # The pause highlight: a pale red row, cleared once the Operator chooses.
                item.setBackground(QColor("#fff0f0"))
                self._failed_row = item
            self.run_view.step_list.addItem(item)

    def _set_paused_header(self) -> None:
        """While a prompt is pending the header goes red and reads WAITING (see _update_header)."""
        self.run_view.state_header.setStyleSheet("background-color: #c62828; color: white;")
        self._update_header()

    def _on_prompt_choice(self, choice: str) -> None:
        """A prompt was answered (a bar button, or the header Abort while one is up): clear the
        pause's display, then answer the session.

        Clearing first means a run that finishes straight away can refresh() without a highlight
        still held on an item refresh() would delete; the session's worker unblocks on choose().
        The bar hides itself in _on_choice, but the header-Abort path answers without the bar, so
        hide it here too (a no-op when the bar already hid itself)."""
        self.run_view.failure_prompt_bar.hide()
        self._clear_paused_display()
        self.session.choose(choice)

    def _clear_paused_display(self) -> None:
        """Drop the pause highlight and red header once the Operator has chosen."""
        if self._failed_row is not None:
            self._failed_row.setBackground(QBrush())
            self._failed_row = None
        self.run_view.state_header.setStyleSheet("")
        self._update_header()

    # -- schedule list ------------------------------------------------------

    def _refresh_schedule_list(self) -> None:
        """Populate the schedule list from the catalog and clear the notes label."""
        config = self.session.config
        if config is None:
            return
        summaries = [
            (name, description, media_number)
            for name, description, media_number in list_schedules(*config.test_definition_dirs)
        ]
        self.test_schedule_stage.schedule_list.clear()
        for name, description, media_number in summaries:
            item = QListWidgetItem(f"{name} — {description}")
            # Carry the exact selected name so a later selection can hand it to the session.
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setToolTip(media_number or "")
            self.test_schedule_stage.schedule_list.addItem(item)
