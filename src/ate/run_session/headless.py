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

    def __init__(self, config: Config | None = None) -> None:
        super().__init__()
        self._config = read_config(config) if config is not None else None
        self._store = RunStore(self._config.database_path) if self._config is not None else None
        self._selected_name: Optional[str] = None
        # The current observable state; updated on the owning thread by signal handlers.
        self._state = SessionState.IDLE
        # ATE's logger plugs in later (ticket 08); interrupted-run recovery logs via it.
        self.on_log: Optional[Callable[[str, str, str], None]] = None
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
        """Update the tracked state and announce it; safe to call on the owning thread."""
        if state != self._state:
            self._state = state
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
        }

        self._engine = RunEngine(
            self._store,
            path,
            operator=pending["operator"],
            project=pending["project"],
            uut_type=pending["uut_type"],
            uut_serial=pending["uut_serial"],
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

        self._engine.start()
        return self._engine

    def _on_run_uuid(self, run_uuid: str) -> None:
        """Capture the freshly generated run key on the main thread."""
        self._run_uuid = run_uuid
