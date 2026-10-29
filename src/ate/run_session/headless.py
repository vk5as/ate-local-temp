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
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, pyqtSignal

from ..catalog import definition_mismatches, include_map, module_map, resolve_schedule, resolve_schedule_definition, resolve_schedule_path
from .._version import __version__  # packet 06d: the version written to test_run.ate_version
from ..engine.lua_host import LuaHost
from ..config import Config, read_config
from ..engine.run_engine import RunEngine, RunStatus
from ..engine.step_inputs import value_error
from ..log import AppLog, LogError, open_app_log
from ..report.html import write_report as _write_run_report
from ..report.pdf_job import start_pdf_job
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
    # (node_seq, attempt_no, index, value): an asked input has been answered, through this session.
    input_answered = pyqtSignal(int, int, int, object)
    step_finished = pyqtSignal(int)  # node_seq: that step's Attempts are done, so its POLL widgets close
    failure_prompt = pyqtSignal(int, int)  # (node_seq, attempt_no): a Stop-on-Fail failure is pending an operator choice
    action_started = pyqtSignal(str, int, int)  # (phase "pre"/"post", completed, total): a Pre-/Post-test Action is starting (packet 14d)
    step_started = pyqtSignal(int)  # node_seq: a Test Step Attempt is starting (packet 14d)
    report_written = pyqtSignal(str, str)  # (html_path, pdf_path): the run-end PDF worker finished; pdf_path is "" on failure

    def __init__(self, config: Config | str | Path | None = None, app_log: Optional[AppLog] = None) -> None:
        super().__init__()
        # Accept an already-read Config (the GUI launch reads the file once, spec "read once at
        # launch") or the path form used by existing callers (30a review).
        self._config = config if isinstance(config, Config) else (read_config(config) if config is not None else None)
        self._store = RunStore(self._config.database_path) if self._config is not None else None
        self._selected_name: Optional[str] = None
        # The selected schedule's declared Stop on Fail default, captured when it is chosen;
        # ``None`` until a schedule has been selected. Prefills the Steps stage radio (True when
        # unset) and is what a run records unless the Operator overrides it via set_stop_on_fail.
        self._selected_stop_on_fail: Optional[bool] = None
        # The current observable state; updated on the owning thread by signal handlers.
        self._state = SessionState.IDLE
        # ATE's logger seam (ticket 08); interrupted-run recovery logs through it. The launch's
        # AppLog is passed in by the GUI (one application log per launch, spec 17); without one,
        # a session given a config opens its own so ATE-level events still land in a file. A
        # broken log directory must not block launch, so a failure leaves on_log as None.
        self.on_log: Optional[Callable[[str, str, str], None]] = None
        self._app_log: Optional[AppLog] = None
        self._owns_app_log = False
        if app_log is not None:
            self._app_log = app_log
            self.on_log = app_log.log
        elif self._config is not None:
            try:
                self._app_log = open_app_log(self._config.log_directory, level=self._config.log_level)
                self.on_log = self._app_log.log
                self._owns_app_log = True
            except LogError:
                self._app_log = None
        self._run_uuid: Optional[str] = None
        self._engine: Optional[RunEngine] = None
        # Spec 08: Project is prefilled from config's `defaultProject` — the same value the Run
        # screen shows — and the Operator's typing replaces it. A run cannot start without one.
        self._pending_project: str = self._config.default_project if self._config is not None else ""
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
    def state(self) -> SessionState:
        """The session's current observable state."""
        return self._state

    def close(self) -> None:
        """Release this session's resources: close the store, and the application log if the
        session opened its own (a log handed in by the launch belongs to the launch)."""
        if self._store is not None:
            self._store.close()
        if self._app_log is not None and self._owns_app_log:
            self._app_log.close()

    @property
    def selected_schedule(self) -> Optional[str]:
        """The schedule name the Operator last selected, if any."""
        return self._selected_name

    def pending_mismatches(self) -> list[tuple[str, Optional[str], str, str]]:
        """Every checksum mismatch a run of the selected schedule can load (its own ``.tslua`` and its
        release's modules; not sibling schedules' files).

        Each entry is ``(rel_path, expected_sha256, computed_sha256, media_number)`` for a file the
        run would load whose bytes on disk differ from what its Test Definition recorded — the set
        the Run screen flags with ⚠ and the confirmation lists before the run starts (ticket 06c).
        Empty when nothing is selected or everything matches."""
        config = self._config
        if config is None or self._selected_name is None:
            return []
        definition = resolve_schedule_definition(*config.test_definition_dirs, schedule_name=self._selected_name)
        if definition is None:
            return []
        return definition_mismatches(definition, self._selected_name)

    def store_mismatched_files(self, choice: Optional[bool], mismatches: Optional[list[tuple[str, Optional[str], str, str]]] = None) -> bool:
        """Decide whether this run stores the bytes of its mismatched files.

        `always` stores and `never` does not, whatever the Operator chose; `ask` takes the
        confirmation's checkbox, and with mismatches pending and no choice given there is nothing to
        start — :class:`ValueError`. With no mismatch there is nothing to store, so the choice is
        ignored (spec: the confirmation only appears on a mismatch). ``mismatches`` is the pending
        list; it is resolved from the selected schedule when omitted."""
        setting = self._config.store_mismatched_files if self._config is not None else "ask"
        if mismatches is None:
            mismatches = self.pending_mismatches()
        if not mismatches:
            return False
        if setting == "always":
            return True
        if setting == "never":
            return False
        if choice is None:
            raise ValueError(
                "database.storeMismatchedFiles is 'ask' and this schedule has checksum mismatches: "
                "start() needs store_mismatched=True or False"
            )
        return bool(choice)

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
                # The run row is already closed by then; write its HTML report now, then render the PDF
                # on a background thread (never blocking FINISHED). A failure is logged and never raised
                # so it can't crash the session or a live run (spec 22b).
                self._write_run_report()
                self._engine = None
            self.state_changed.emit(state)

    def _write_run_report(self) -> None:
        """Write this run's HTML report at run end, then render its PDF without blocking the run.

        The HTML is written synchronously (spec 21a); the PDF follows in the helper process (see
        :meth:`_start_pdf`), so FINISHED never waits for it. A failure here never raises."""
        config = self._config
        if config is None or self._run_uuid is None or self._store is None:
            return
        try:
            html_path = _write_run_report(self._store, self._run_uuid, config)
        except Exception as exc:  # noqa: BLE001 - a report failure must not crash the run
            self._log_error(f"Failed to write Test Report for {self._run_uuid}: {exc}")
            return

        self._start_pdf(Path(html_path), config.reports_paper_size)

    def _log_error(self, msg: str) -> None:
        if self.on_log is not None:
            self.on_log("ERROR", "ate", msg)
        else:
            print(msg)

    def _start_pdf(self, html_path: Path, paper: str) -> None:
        """Render ``html_path`` to a PDF beside it in the helper process, without waiting for it.

        The QProcess logic lives in :func:`ate.report.pdf_job.start_pdf_job` (shared with any other
        caller); here we only translate its completion into this session's ``report_written`` signal —
        both paths on success, an empty pdf path plus an ERROR log line when the helper fails (spec 22b).
        Nothing here raises into the run."""
        pdf_path = html_path.with_suffix(".pdf")
        run_uuid = self._run_uuid

        def on_done(error: Optional[str]) -> None:
            if error is None:
                self.report_written.emit(str(html_path), str(pdf_path))
            else:
                self._log_error(f"Failed to write Test Report PDF for {run_uuid}: {error}")
                self.report_written.emit(str(html_path), "")

        start_pdf_job(self, html_path, pdf_path, paper, on_done=on_done)


    @property
    def node_enables(self) -> Optional[dict[int, bool]]:
        """The Operator's step-enable overrides for the selected schedule, keyed by the
        runner seq each step is assigned.

        The Steps stage seeds its boxes from these so they show what a next run will record
        (spec 37: options carry over). Returns a copy, so callers can read the state without
        mutating the session's; ``None`` until a toggle has been made."""
        return dict(self._pending_node_enables) if self._pending_node_enables is not None else None

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
        its radio from the declared default; a broken or unreadable schedule falls back to True.
        Re-selecting from FINISHED returns to SELECTING so the step boxes unlock again (spec 37);
        while a run is in progress the selection is remembered but not announced."""
        if not name or name == "":
            raise SessionError("a schedule name is required to select one")
        # Switching schedules drops the previous schedule's step overrides; selecting
        # the same one keeps them (spec 37: options carry over within a schedule).
        if self._selected_name != name:
            self._pending_node_enables = None
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
                        host = LuaHost(path, modules=modules, includes=include_map(definition))
                        stop_on_fail = bool(host.schedule.stop_on_fail)
            except Exception:  # noqa: BLE001 - any load failure keeps the run startable at True
                print(f"WARNING: could not read Stop on Fail default from '{name}', using True")
        self._selected_stop_on_fail = stop_on_fail
        # Announce selection while idle or just finished; the FINISHED release (engine cleared in
        # _set_state) lets a re-select return to SELECTING, which unlocks the step boxes.
        # While a run is in progress the run owns the session and no selection is announced.
        if self._state in (SessionState.IDLE, SessionState.FINISHED):
            self._set_state(SessionState.SELECTING)

    def clear_selection(self) -> None:
        """Forget the selected schedule, so no run can start from it (packet 05b review).

        The Run screen calls this when the picked row is no longer the current one — a broken row
        made current, or the filter hiding the picked row — so the session cannot carry a choice the
        Operator no longer sees. Nothing is announced: the state stays as it is, and ``start()``
        then reports that no schedule is selected."""
        self._selected_name = None
        self._selected_stop_on_fail = None
        self._pending_node_enables = None

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

    def supply_input(self, node_seq: int, attempt_no: int, index: int, value: Any) -> None:
        """Answer one blocking Step Input, exactly as the Operator's widget does (ticket 10a).

        ``index`` is the 1-based ``n`` of ``ATE.input(n)`` — the same number the ``input_asked``
        signal carries — while :class:`~ate.engine.operator_input.OperatorInput` keys its blocking
        channels from 0, so this is where the two meet. The worker is blocked on
        :meth:`OperatorInput.get_raw` and wakes as soon as the value arrives.

        Packet 10a review: nothing reaches the worker unless this session's own check says the answer
        belongs where it is sent — the step and Attempt must be the ones running, the index must be
        declared by that step, and that input's call must be waiting right now. A value the
        declaration rejects (:func:`step_inputs.value_error`) raises :class:`ValueError` too, so a bad
        value can never be recorded. Emits :attr:`input_answered` so the layout marks it answered.
        """
        if self._engine is None:
            raise SessionError("cannot supply an input when no run is in progress")
        engine = self._engine
        decl = engine.input_declaration(node_seq, attempt_no, index)
        if decl is None:
            raise ValueError(f"step {node_seq} attempt {attempt_no} declares no input {index}")
        if decl.type == "POLL":
            raise ValueError(f"input {index} of step {node_seq} is a POLL: click it instead of supplying a value")
        if not engine.is_asking(node_seq, attempt_no, index):
            raise ValueError(f"step {node_seq} attempt {attempt_no} is not waiting for input {index}")
        error = value_error(decl, value)
        if error:
            raise ValueError(error)
        engine.operator_input.supply_input(index - 1, value)
        self.input_answered.emit(node_seq, attempt_no, index, value)

    def click_poll(self, node_seq: int, attempt_no: int, index: int, text: str) -> None:
        """Click a POLL button while its step runs (ticket 10a).

        ``index`` is again the 1-based ``n`` in ``ATE.input(n)``. The click is sticky state for that
        step's Attempt and is recorded as an ``input_value`` row when the Attempt's inputs are
        snapshotted; this forwards it to the worker's controller and announces the answer.

        Packet 10a review: the session refuses a click for a step that is not running, an Attempt
        that is not the current one, an index that is not this step's POLL, or a text the POLL never
        offered — each raises :class:`ValueError` rather than quietly recording a phantom answer."""
        if self._engine is None:
            raise SessionError("cannot click a POLL when no run is in progress")
        engine = self._engine
        decl = engine.input_declaration(node_seq, attempt_no, index)
        if decl is None or decl.type != "POLL":
            raise ValueError(f"step {node_seq} attempt {attempt_no} has no POLL input {index}")
        if not engine.is_poll_open(node_seq, attempt_no, index):
            raise ValueError(f"step {node_seq} attempt {attempt_no} is no longer running, so its POLL is closed")
        if text not in decl.prompt:
            raise ValueError(f"input {index} of step {node_seq} offers no prompt {text!r}")
        engine.operator_input.click_poll(node_seq, attempt_no, index - 1, text)
        self.input_answered.emit(node_seq, attempt_no, index, text)

    # -- start ---------------------------------------------------------------

    def start(self, store_mismatched: Optional[bool] = None) -> RunEngine:
        """Recover interrupted runs, resolve the selected schedule, and launch it.

        Returns the :class:`RunEngine` worker; call ``start()`` from the owning thread
        (typically after ``qapp.processEvents()`` delivers queued signals). Raises
        :class:`SessionError` if no config or an unresolvable schedule selection.

        ``store_mismatched`` answers the run-start confirmation for a schedule whose files no longer
        match their recorded checksums (ticket 06c): with `database.storeMismatchedFiles` = `ask` it
        is required when there is a mismatch, and `always`/`never` ignore it. A clean schedule needs
        no answer at all.
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

        # Spec 08/11: a Test Run never has an empty Project. The Run screen keeps Start disabled,
        # but the session is the authority, so a blank Project refuses the run here too.
        if not (getattr(self, "_pending_project", "") or "").strip():
            raise SessionError("a Test Run cannot start with an empty Project")

        config = self._config
        assert config is not None, "session was constructed without a config"
        definition = resolve_schedule_definition(*config.test_definition_dirs, schedule_name=self._selected_name)
        if definition is None:
            raise SessionError(f"schedule '{self._selected_name}' is not available")
        path = resolve_schedule_path(definition, self._selected_name)
        assert path is not None, "resolved schedule has no matching file"
        modules = module_map(definition)  # packet 06a: forward the release's require'd module map to the run
        # Packet 06c: the Operator's answer to the run-start confirmation decides whether the bytes of
        # a file whose checksum differs are recorded on its run_file row.
        store = self.store_mismatched_files(store_mismatched, definition_mismatches(definition, self._selected_name))

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
            ate_version=__version__,  # packet 06d: every run records the ATE build that ran it
            media_number=definition.media_number or "",  # the Media Release this schedule came from
            modules=modules,  # packet 06a: forward the release's require'd module map to this run
            definition=definition,  # packet 06b: record the owning Media Release and files used with checksums
            store_mismatched_files=store,  # packet 06c: keep the bytes of a mismatched file when asked to
            schedule_name=self._selected_name,
            utc_offset="+00:00",
            overrides={int(seq): bool(en) for seq, en in (self._pending_node_enables or {}).items()},
            receive_buffer_bytes=self._config.receive_buffer_bytes,  # packet 17a: the per-device receive cap
            device_trace_enabled=self._config.device_trace_enabled,  # packet 17c: deviceTrace.enabled
            device_trace_max_bytes=self._config.device_trace_max_bytes,  # packet 17c: deviceTrace.maxBytes
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
        # A step's Attempt loop ending closes its POLL widgets: the layout watches this signal.
        self._engine.step_finished.connect(self.step_finished.emit)
        # Forward the worker's Stop-on-Fail failure prompt so an operator (or a staged-run test) can
        # answer it through this session once it fires — same connection timing as input asks above.
        self._engine.failure_prompt.connect(self._on_failure_prompt)
        # Forward the worker's phase announcements (packet 14d) so the Run screen's status bar can
        # count Pre-/Post-test Actions and restart its current-item timer without polling the worker.
        self._engine.action_started.connect(self._on_action_started)
        self._engine.step_started.connect(self._on_step_started)

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

    def _on_action_started(self, phase: str, done: int, total: int) -> None:
        """Forward the worker's Pre-/Post-test Action announcement for the status bar (packet 14d).

        ``phase`` is "pre" or "post"; ``done`` is the 1-based position of the Action that has just
        begun within its phase and ``total`` how many Actions that phase has."""
        self.action_started.emit(phase, done, total)

    def _on_step_started(self, node_seq: int) -> None:
        """Forward the worker's Test Step start so the status bar restarts its current-item timer.

        Emitted on the owning thread; delivery rides the event loop like every other queued signal."""
        self.step_started.emit(node_seq)
