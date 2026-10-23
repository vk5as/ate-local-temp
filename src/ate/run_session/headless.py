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

from ..catalog import resolve_schedule
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

    def __init__(self, config: Config | None = None) -> None:
        super().__init__()
        self._config = read_config(config) if config is not None else None
        self._store = RunStore(self._config.database_path) if self._config is not None else None
        self._selected_name: Optional[str] = None
        self._stop_on_fail: bool = False
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

    def select_schedule(self, name: str) -> None:
        """Select a schedule by its loaded name (for listing/validating later)."""
        if not name or name == "":
            raise SessionError("a schedule name is required to select one")
        self._selected_name = name
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
        """Set whether the next run stops at the first failure.

        The session default is False; call this to opt in before starting a run."""
        if not isinstance(value, bool):
            raise SessionError("stop_on_fail must be a boolean")
        self._pending_stop_on_fail = value

    def request_abort(self) -> None:
        """Request a whole-run abort of the current run (the Run screen's Abort button calls this).

        Only meaningful while a run is in progress; delegate to the worker so it stops at its next
        step boundary and records ERROR "Aborted by Operator", remaining steps NOTRUN, End Reason
        ABORTED and result FAIL. Raises if there is no run to abort."""
        if self._engine is None:
            raise SessionError("cannot abort when no run is in progress")
        self._engine.request_abort()

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
        path = resolve_schedule(*config.test_definition_dirs, schedule_name=self._selected_name)
        if path is None:
            raise SessionError(f"schedule '{self._selected_name}' is not available")

        pending = {
            "operator": getattr(self, "_pending_operator", "") or "",
            "project": getattr(self, "_pending_project", "") or "",
            "uut_type": getattr(self, "_pending_uut_type", "") or "",
            "uut_serial": getattr(self, "_pending_uut_serial", "") or "",
            "stop_on_fail": bool(getattr(self, "_pending_stop_on_fail", False)),
        }

        self._engine = RunEngine(
            self._store,
            path,
            operator=pending["operator"],
            project=pending["project"],
            uut_type=pending["uut_type"],
            uut_serial=pending["uut_serial"],
            stop_on_fail=pending["stop_on_fail"],
            station_name=self._config.station_name or "",
            station_uuid=self._config.station_uuid or "",
            ate_version="",  # ticket 02 leaves the ATE version empty; a later ticket fills it
            media_number="",
            schedule_name=self._selected_name,
            utc_offset="+00:00",
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

        self._engine.start()
        return self._engine

    def _on_run_uuid(self, run_uuid: str) -> None:
        """Capture the freshly generated run key on the main thread."""
        self._run_uuid = run_uuid

    def _on_input_asked(self, node_seq: int, attempt_no: int, index: int) -> None:
        """Forward the worker's ``input_asked`` so an operator (or test) can answer it through this session.

        Emitted on the owning thread; delivery rides the event loop like every other queued signal."""
        self.input_asked.emit(node_seq, attempt_no, index)
