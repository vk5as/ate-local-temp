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

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QPushButton,
    QRadioButton,
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
        """Show ``steps`` (each with ``step_no``, ``name``, ``enabled``) in the list.

        Also re-applies the selected schedule's Stop on Fail default to the radio pair so it is not
        stuck at construction-time; the Operator can still toggle either choice."""
        self.step_list.clear()
        for step in steps:
            label = getattr(step, "name", "") or ""
            number = getattr(step, "step_no", "") or ""
            text = f"{number}  {label}" if number else str(label)
            item = QListWidgetItem(text)
            self.step_list.addItem(item)
        if self.session is not None:
            self._apply_stop_on_fail(self.session.selected_stop_on_fail)

    def _apply_stop_on_fail(self, value: bool) -> None:
        """Set the radio pair to ``value`` (True = stop on first failure)."""
        self.stop_on_fail_radio.setChecked(value)
        self.continue_all_radio.setChecked(not value)


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
        """The Abort button asks the session to abort the current run.

        ``session.request_abort`` delegates to the worker, which stops at its next step boundary and
        records ERROR / NOTRUN / ABORTED; it raises if there is no run in progress (the button is
        disabled whenever one is not)."""
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

        # The Abort button is only meaningful while a run is in progress.
        self.run_view.abort_button.setEnabled(state == SessionState.STARTED)

        self._update_header(state)

    def _update_header(self, state: Optional[SessionState] = None) -> None:
        """Refresh the header's state label and live elapsed timer."""
        if state is None:
            state = self.state
        text = f"{state.value}"
        if self._started_at is not None and state in (SessionState.STARTED, SessionState.FINISHED):
            delta = datetime.now(timezone.utc) - self._started_at
            text += f"  ·  {_format_elapsed(delta)}"
        self.run_view.state_header.setText(text)

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
