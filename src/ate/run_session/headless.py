"""The headless run session: ticket 02's Operator-facing slice, GUI-free.

It ties the pieces together so a one-step Run can start with no display attached:

1. read the minimal :class:`~ate.config.Config` (Station + database path + Test
   Definition directories);
2. open the single :class:`~ate.storage.store.RunStore`;
3. on ``start()`` recover any run left open by a crash (:meth:`RunStore.close_interrupted_runs`,
   spec rule 98), resolve the selected schedule across the configured directories, and
   launch a :class:`~ate.engine.run_engine.RunEngine` worker;
4. expose the Operator's actions — select schedule, set Operator/Project/UUT Type/serial —
   and observable run state through queued Qt signals so it can be driven under an
   offscreen event loop with no widgets imported here or in the engine.

The worker reaches this session only through those queued signals; nothing here imports
``QtWidgets``, so it stays testable headless (see ``tests/test_architecture_rules.py``).
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from ..catalog import module_map, resolve_schedule, resolve_schedule_definition, resolve_schedule_path
from ..engine.lua_host import LuaHost
from ..config import Config, read_config
from ..engine.run_engine import RunEngine, RunStatus
from ..log import AppLog, LogError, open_app_log
from ..storage.store import RunStore


class SessionState(enum.Enum):
    """Observable states of a headless run session."""

    IDLE = "IDLE"  # nothing running; fields not yet entered
    SELECTING = "SELECTING"  # a schedule is selected but the run has not started
    STARTED = "STARTED"  # the worker thread is running the schedule
    FINISHED = "FINISHED"  # the run finished


class SessionError(Exception):
    """Raised for an invalid selection or when starting fails."""


def _map_run_status(status: RunStatus) -> SessionState:
    """Map a :class:`RunEngine` status onto this session's states (RUNNING -> STARTED)."""
    return {
        RunStatus.IDLE.value: SessionState.IDLE,
        RunStatus.RUNNING.value: SessionState.STARTED,
        RunStatus.FINISHED.value: SessionState.FINISHED,
    }[status.value]


class HeadlessRunSession(QObject):
    """Drives a one-step Test Schedule headlessly and records it through the store."""

    state_changed = pyqtSignal(SessionState)  # IDLE -> SELECTING -> STARTED -> FINISHED
    input_asked = pyqtSignal(int, int, int)  # (node_seq, attempt_no, index): a step is asking for its n-th input
    failure_prompt = pyqtSignal(int, int)  # (node_seq, attempt_no): a Stop-on-Fail failure is pending an operator choice

    def __init__(self, config: Config | None = None) -> None:
        super().__init__()
        self._config = read_config(config) if config is not None else None
        self._store = RunStore(self._config.database_path) if self._config is not None else None
        self._selected_name: Optional[str] = None
        # The selected schedule's declared Stop on Fail default, captured when it is chosen;
        # ``None`` until a schedule has been selected. Prefills the Steps stage radio (True when
        # unset) and is what a run records unless the Operator overrides it via set_stop_on_fail.
        self._selected_stop_on_fail: Optional[bool] = None
        # The current observable state; updated on the owning thread by signal handlers.
        self._state = SessionState.IDLE
        # ATE's logger seam (ticket 08); interrupted-run recovery logs through it. Defaults to
        # the launch application log when a config is present (see below), so ATE-level events —
        # config load and interrupted-run recovery — are recorded in that one file per launch; a
        # broken log directory must not block launch, so a failure leaves on_log as None.
        self.on_log: Optional[Callable[[str, str, str], None]] = None
        if self._config is not None:
            try:
                self._app_log = open_app_log(self._config.log_directory, level=self._config.log_level)
                self.on_log = self._app_log.log
            except LogError:
                self._app_log = None
        self._run_uuid: Optional[str] = None
        self._engine: Optional[RunEngine] = None
        # Packet 11a: the Operator's enable/disable toggles for expanded-tree positions, keyed by
        # the runner seq each step is assigned. Applied at start() as engine overrides; absent here
        # every step keeps its :setEnable() default (see RunEngine._enabled). ``None`` until set.
        self._pending_node_enables: Optional[dict[int, bool]] = None
        # Emit the initial state on the thread that owns this session.
        self.state_changed.emit(SessionState.IDLE)

    # -- read-only views ----------------------------------------------------

    @property
    def config(self) -> Optional[Config]:
        """The loaded :class:`~ate.config.Config`, or ``None`` if not configured."""
        return self._config

    @property
    def store(self) -> RunStore:
        """The run store this session records through."""
        assert self._store is not None, "session was constructed without a config"
        return self._store

    @property
    def selected_schedule(self) -> Optional[str]:
        """The schedule name the Operator last selected, if any."""
        return self._selected_name

    @property
    def run_uuid(self) -> Optional[str]:
        """The UUID of the most recent run, captured on the main thread when reported."""
        return self._run_uuid

    # -- selection and fields ------------------------------------------------

    def _set_state(self, state: SessionState) -> None:
        """Update the tracked state and announce it; safe to call on the owning thread.

        Releasing ``_engine`` when a run reaches FINISHED lets the next start() capture
        fresh field values (spec "new run keeps every field") without racing the old
        worker, which is no longer running."""
        if state != self._state:
            self._state = state
            if state == SessionState.FINISHED:
                self._engine = None
            self.state_changed.emit(state)

    @property
    def selected_stop_on_fail(self) -> bool:
        """The selected schedule's Stop on Fail default — ``True`` when it is unset or unknown.

        Captured from the loaded :class:`ScheduleParams` at selection time; a broken or unreadable
        schedule falls back to the spec default (True), so the Steps stage radio always has a value
        to prefill and a run always records something meaningful."""
        return self._selected_stop_on_fail if self._selected_stop_on_fail is not None else True

    def select_schedule(self, name: str) -> None:
        """Select a schedule by its loaded name and remember its Stop on Fail default.

        Loads the schedule once (the same host that runs it later) so the Steps stage can prefill
        its radio from the declared default; a broken or unreadable schedule falls back to True."""
        if not name or name == "":
            raise SessionError("a schedule name is required to select one")
        self._selected_name = name
        stop_on_fail = True  # spec default when unset, unknown, or the schedule fails to load
        config = self._config
        if config is not None:
            try:
                definition = resolve_schedule_definition(*config.test_definition_dirs, schedule_name=name)
                if definition is not None:
                    modules = module_map(definition)
                    path = resolve_schedule_path(definition, name)
                    if path is not None:
                        host = LuaHost(path, modules=modules)
                        stop_on_fail = bool(host.schedule.stop_on_fail)
            except Exception:  # noqa: BLE001 - any load failure keeps the run startable at True
                print(f"WARNING: could not read Stop on Fail default from '{name}', using True")
        self._selected_stop_on_fail = stop_on_fail
        # Only announce selection while idle; once running, the run owns the session.
        if self._state in (SessionState.IDLE,):
            self._set_state(SessionState.SELECTING)

    def set_operator(self, operator: str) -> None:
        """Set the Operator's name for the next run."""
        if not isinstance(operator, str):
            raise SessionError("operator must be a string")
        if self._engine is not None:
            raise SessionError("cannot change fields while a run is in progress")
        # Fields are captured at start(); store them on the engine's constructor args.
        self._pending_operator = operator

    def set_project(self, project: str) -> None:
        """Set the Project for the next run."""
        if not isinstance(project, str):
            raise SessionError("project must be a string")
        if self._engine is not None:
            raise SessionError("cannot change fields while a run is in progress")
        self._pending_project = project

    def set_uut_type(self, uut_type: str) -> None:
        """Set the UUT type for the next run."""
        if not isinstance(uut_type, str):
            raise SessionError("uut_type must be a string")
        if self._engine is not None:
            raise SessionError("cannot change fields while a run is in progress")
        self._pending_uut_type = uut_type

    def set_uut_serial(self, uut_serial: str) -> None:
        """Set the UUT serial for the next run."""
        if not isinstance(uut_serial, str):
            raise SessionError("uut_serial must be a string")
        if self._engine is not None:
            raise SessionError("cannot change fields while a run is in progress")
        self._pending_uut_serial = uut_serial

    def set_stop_on_fail(self, value: bool) -> None:
        """Override the selected schedule's Stop on Fail default for this run.

        Called by the Steps stage with the radio's checked state; leaving it at the prefilled
        default still records that default (the radio is always a definite choice). When nothing
        has been set, :meth:`start` falls back to the schedule's own default."""
        if not isinstance(value, bool):
            raise SessionError("stop_on_fail must be a boolean")
        self._pending_stop_on_fail = value

    def toggle_node(self, seq: int, enable: bool) -> None:
        """Override one step's enable/disable default for this run at its runner ``seq``.

        Called by the Steps stage when an Operator toggles an expanded-tree position; applied at
        start() as engine overrides (see RunEngine._enabled). Raises if called with a bad type or
        while a run is in progress."""
        if not isinstance(seq, int):
            raise SessionError("node seq must be an integer")
        if not isinstance(enable, bool):
            raise SessionError("enable must be a boolean")
        if self._engine is not None:
            raise SessionError("cannot toggle nodes while a run is in progress")
        self._pending_node_enables = {**(self._pending_node_enables or {}), seq: enable}

    def request_abort(self) -> None:
        """Request a whole-run abort of the current run (the Run screen's Abort button calls this).

        Only meaningful while a run is in progress; delegate to the worker so it stops at its next
        step boundary and records ERROR "Aborted by Operator", remaining steps NOTRUN, End Reason
        ABORTED and result FAIL. Raises if there is no run to abort."""
        if self._engine is None:
            raise SessionError("cannot abort when no run is in progress")
        self._engine.request_abort()

    def choose(self, choice: str) -> None:
        """Answer a Stop-on-Fail failure prompt (CONTINUE / RETRY / ABORT).

        Only meaningful while a run is paused on a failure; delegate to the worker so it records the
        choice and unblocks. Raises if there is no run to answer."""
        if self._engine is None:
            raise SessionError("cannot choose when no run is in progress")
        self._engine.choose(choice)

    # -- start ---------------------------------------------------------------

    def start(self) -> RunEngine:
        """Recover interrupted runs, resolve the selected schedule, and launch it.

        Returns the :class:`RunEngine` worker; call ``start()`` from the owning thread
        (typically after ``qapp.processEvents()`` delivers queued signals). Raises
        :class:`SessionError` if no config or an unresolvable schedule selection.
        """
        # Rule 98: close any run left open by a crash before starting a new one.
        assert self._store is not None, "session was constructed without a config"
        closed = self._store.close_interrupted_runs(on_log=self.on_log)
        if closed:
            msg = f"Closed {len(closed)} interrupted run(s) at launch."
            if self.on_log is not None:
                self.on_log("ERROR", "ate", msg)

        if self._selected_name is None:
            raise SessionError("no schedule selected before start()")

        config = self._config
        assert config is not None, "session was constructed without a config"
        definition = resolve_schedule_definition(*config.test_definition_dirs, schedule_name=self._selected_name)
        if definition is None:
            raise SessionError(f"schedule '{self._selected_name}' is not available")
        path = resolve_schedule_path(definition, self._selected_name)
        assert path is not None, "resolved schedule has no matching file"
        modules = module_map(definition)  # packet 06a: forward the release's require'd module map to the run

        pending = {
            "operator": getattr(self, "_pending_operator", "") or "",
            "project": getattr(self, "_pending_project", "") or "",
            "uut_type": getattr(self, "_pending_uut_type", "") or "",
            "uut_serial": getattr(self, "_pending_uut_serial", "") or "",
        }

        # Stop on Fail: the Operator's explicit choice overrides the schedule's default; otherwise
        # use the schedule's own default (captured at selection, True when unset). Whatever this is
        # is what gets recorded on the run row, so it always matches what was in effect.
        if getattr(self, "_pending_stop_on_fail", None) is not None:
            stop_on_fail = bool(self._pending_stop_on_fail)
        else:
            stop_on_fail = self.selected_stop_on_fail

        self._engine = RunEngine(
            self._store,
            path,
            operator=pending["operator"],
            project=pending["project"],
            uut_type=pending["uut_type"],
            uut_serial=pending["uut_serial"],
            stop_on_fail=stop_on_fail,
            station_name=self._config.station_name or "",
            station_uuid=self._config.station_uuid or "",
            ate_version="",  # ticket 02 leaves the ATE version empty; a later ticket fills it
            media_number="",
            modules=modules,  # packet 06a: forward the release's require'd module map to this run
            schedule_name=self._selected_name,
            utc_offset="+00:00",
            overrides={int(seq): bool(en) for seq, en in (self._pending_node_enables or {}).items()},
        )

        # Announce STARTED on this thread before the worker runs. The worker's queued
        # signals (RUNNING -> STARTED, FINISHED) then update state on delivery; RUNNING
        # is a no-op here since we are already STARTED. All delivered via Qt's queued
        # connections, so no display is required. Route them through _set_state so the
        # tracked state and the emitted signal stay in step.
        self._set_state(SessionState.STARTED)
        self._engine.run_uuid_reported.connect(self._on_run_uuid)
        self._engine.state_changed.connect(lambda st: self._set_state(_map_run_status(st)))
        # Forward the worker's input asks so an operator (or a staged-run test) can answer them
        # through this session before it starts — connecting here avoids the single-shot race of
        # grabbing the engine after start() has already begun.
        self._engine.input_asked.connect(self._on_input_asked)
        # Forward the worker's Stop-on-Fail failure prompt so an operator (or a staged-run test) can
        # answer it through this session once it fires — same connection timing as input asks above.
        self._engine.failure_prompt.connect(self._on_failure_prompt)

        self._engine.start()
        return self._engine

    def _on_run_uuid(self, run_uuid: str) -> None:
        """Capture the freshly generated run key on the main thread."""
        self._run_uuid = run_uuid

    def _on_input_asked(self, node_seq: int, attempt_no: int, index: int) -> None:
        """Forward the worker's ``input_asked`` so an operator (or test) can answer it through this session.

        Emitted on the owning thread; delivery rides the event loop like every other queued signal."""
        self.input_asked.emit(node_seq, attempt_no, index)

    def _on_failure_prompt(self, node_seq: int, attempt_no: int) -> None:
        """Forward the worker's Stop-on-Fail failure prompt so an operator (or test) can answer it.

        Emitted on the owning thread; delivery rides the event loop like every other queued signal."""
        self.failure_prompt.emit(node_seq, attempt_no)
