"""The run engine: one QThread worker that runs a Test Schedule and records it.

Ticket 02's flow-control slice. On ``run()`` (executed on the worker thread) the engine:

1. inserts a fresh :class:`~ate.storage.store.RunStore` test_run row, stamping the
   Station UTC offset once for the run;
2. steps through the schedule's expanded steps in order — skipping header/include rows
   that have no action — invoking each step's Lua action closure and recording one
   Attempt per ``ATE.report`` call it makes (last call wins); a step whose action raises
   records an ERROR Attempt;
3. sets the run result to FAIL if any step failed or errored, otherwise PASS, with End
   Reason COMPLETED, then closes the run row.

Real Lua execution is stubbed in :mod:`ate.engine.lua_host`; this module only drives it
and writes rows through the store. It reaches the GUI only through queued Qt signals and
imports no widgets.

Because a schedule must not use coroutines, each step's action runs synchronously on
the worker thread; ``ATE.report`` calls are therefore attributed to that step by taking
a snapshot of the host's report log before invoking it and diffing afterwards.
"""

from __future__ import annotations

import enum
import json
import queue
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from PyQt6.QtCore import QThread, pyqtSignal

from ..errors import ATEError
from ..storage._uuid import new_uuid7
from ..storage.store import RunStore, _now_iso
from .lua_host import _AbortFlag, _sha256_hex, LuaHost
from .operator_input import OperatorInput


class RunStatus(enum.Enum):
    """Observable run states, emitted on the main thread via a queued signal."""

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"


# Announced before each step's action so the GUI can show that step's inputs under it and the
# operator can supply a blocking value from another thread while the worker blocks on
# :meth:`OperatorInput.get_raw`. Delivered as a queued signal; the emitting worker is free to block
# because delivery rides the receiver's own event loop.


def _to_text(value: Any) -> Optional[str]:
    """Store an ATE.report value as text (``nil`` becomes blank)."""
    if value is None:
        return ""
    return str(value)


# The phase a Pre-test/Post-test Action runs in, stored on its action row ("pre"/"post"), mapped to
# the internal execution context string the Lua host uses for lockdown and error location. A step
# keeps "step"; loading/idle is None.
PHASE_CONTEXT = {"pre": "pretest", "post": "posttest"}


class RunEngine(QThread):
    """Runs one Test Schedule on a worker thread and records it through the store."""

    run_uuid_reported = pyqtSignal(str)  # the freshly generated run key, on start
    state_changed = pyqtSignal(RunStatus)  # IDLE -> RUNNING -> FINISHED, main thread
    input_asked = pyqtSignal(int, int, int)  # (node_seq, attempt_no, index): a step is asking for its n-th input
    failure_prompt = pyqtSignal(int, int)  # (node_seq, attempt_no): a Stop-on-Fail failure is pending an operator choice

    def __init__(
        self,
        store: RunStore,
        schedule_path: str | Path,
        *,
        operator: str = "",
        project: str = "",
        uut_type: str = "",
        uut_serial: str = "",
        stop_on_fail: bool = False,
        station_name: str = "",
        station_uuid: str = "",
        ate_version: str = "",
        media_number: str = "",
        schedule_name: str = "",
        utc_offset: str = "+00:00",
        overrides: Optional[Mapping[int, bool]] = None,  # packet 11a: per-step enable/disable Operator toggles
        modules: Mapping[str, Path] | None = None,  # packet 06a: the require'd module map for this run
        definition: Optional[Any] = None,  # packet 06b: the resolved Media Release that owns this run (records media_release/run_file rows)
        parent: Optional[object] = None,
        ate_fns: Mapping[str, Callable[..., Any]] | None = None,
        operator_input: Optional[OperatorInput] = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._schedule_path = Path(schedule_path)
        self.operator = operator
        self.project = project
        self.uut_type = uut_type
        self.uut_serial = uut_serial
        self.stop_on_fail = stop_on_fail
        self.station_name = station_name
        self.station_uuid = station_uuid
        self.ate_version = ate_version
        self.media_number = media_number
        self.schedule_name = schedule_name
        self.utc_offset = utc_offset
        # Packet 11a: the Operator's enable/disable toggles for expanded-tree positions, keyed by
        # the same seq the runner assigns to each step. Absent here every step keeps its own
        # :setEnable() default; an override wins over that default when present (see _enabled).
        self._overrides: dict[int, bool] = {int(seq): bool(en) for seq, en in (overrides or {}).items()}
        # Optional pre-registered ATE functions (a testability seam; see criterion 03). When
        # provided they are registered on the host before it runs, so a failing one can be
        # exercised through real Lua execution without touching device drivers.
        self._ate_fns = dict(ate_fns or {})
        # The OperatorInput controller this run's ATE.input calls read/write through; injected by
        # tests (so they own supply/click) or made fresh when running for real. See ticket 10.
        # Packet 06a: the require'd module map for this run, forwarded to the Lua host so a schedule's
        # load-time and step-time requires resolve against it (one host per Test Run). ``None`` means
        # no replacement require is installed.
        self._modules = dict(modules) if modules is not None else None
        # Packet 06b: the resolved Media Release that owns this run, forwarded from the session so we
        # can record its media_release row (seq 0) and every file used with expected/computed checksums.
        # ``None`` means a bare schedule test — nothing extra is recorded then.
        self._definition = definition
        self._op = operator_input or OperatorInput()
        # Whole-run abort, requested by the Operator (the staged Run screen's Abort button calls
        # :meth:`request_abort` through the session). It is checked between steps so a clean stop
        # needs no busy-loop or blocking interruption — those come in packets 13b/13c. This flag is
        # also read by the Lua count hook (:meth:`_LuaHost._install_abort_hook`) to interrupt a busy
        # loop, and passed to that host so both threads see one value; see :meth:`aborted`.
        self._abort_flag = _AbortFlag()
        # Stop-on-Fail failure pause (packet 12b). When set and a step fails, the worker blocks in
        # :meth:`_pause_for_operator` until choose() records a choice; every failure pauses. Pending pauses
        # are keyed by (node_seq, attempt_no) and delivered through plain queues — the same Qt 6.11 /
        # PyQt6 6.11 offscreen deadlock that makes OperatorInput avoid a Condition means no
        # threading.Condition here either; a queue blocks-and-wakes correctly under the fixture's
        # event-loop pump.
        self._pause_lock = threading.Lock()
        self._pending: dict[tuple[int, int], queue.Queue] = {}
        # Set when a Stop-on-Fail pause is aborted (packet 12b); ends the run STOPPED_ON_FAIL.
        self._stopped_on_fail = False

    @property
    def aborted(self) -> bool:
        """Whether a whole-run abort has been requested for this run."""
        return self._abort_flag.aborted

    def request_abort(self) -> None:
        """Request a whole-run abort (settable from the operator/main thread).

        The running worker observes it at its next step boundary and stops between steps, recording
        the step about to run as an ERROR "Aborted by Operator" attempt, marking every remaining
        step NOTRUN, and closing with End Reason ABORTED / result FAIL (spec rule 83). A busy loop
        or blocking call in flight is interrupted too (packets 13b/13c) via the shared flag."""
        self._abort_flag.request()

    @property
    def operator_input(self) -> OperatorInput:
        """The :class:`OperatorInput` controller this run's ``ATE.input`` calls read/write through.

        Owned by the engine (a fresh one per real run); tests and the GUI drive it directly so a
        blocking supply or POLL click reaches the worker while it waits, without importing widgets."""
        return self._op

    def _pause_for_operator(self, run_uuid: str, node_seq: int) -> str:
        """Stop-on-Fail pause for a failed step: announce the pending choice and block until answered.

        The failing Attempt is already recorded; once :meth:`choose` returns a value we stamp its
        ``prompt_choice`` column (packet 12b checkbox 4). Blocks with no timeout — the worker waits
        here while the operator/main thread supplies CONTINUE, RETRY or ABORT through choose()."""
        ch = queue.Queue()
        with self._pause_lock:
            self._pending[(node_seq, 1)] = ch
        # Mark this Attempt as ending at a prompt; resolved to its choice once choose() returns.
        self._store.update_attempt(run_uuid, node_seq, 1, prompt_choice=None)
        # Announce the pending choice on the owning thread so an operator/test can answer it there.
        self.failure_prompt.emit(node_seq, 1)
        choice = ch.get()  # blocks until choose() puts a value and wakes us
        # Stamp the chosen CONTINUE/RETRY/ABORT onto this Attempt now that we hold it (packet 12b checkbox 4).
        self._store.update_attempt(run_uuid, node_seq, 1, prompt_choice=choice)
        with self._pause_lock:
            self._pending.pop((node_seq, 1), None)
        return choice

    def choose(self, choice: str) -> None:
        """Answer a Stop-on-Fail failure prompt from the operator/main thread (packet 12b).

        Records ``choice`` (CONTINUE / RETRY / ABORT) onto the pending pause channel; the blocked
        worker wakes and records it as that Attempt's prompt_choice. Raises if there is no run to
        answer."""
        with self._pause_lock:
            entry = next(iter(self._pending), None)
            ch = self._pending.pop(entry) if entry is not None else None
        if ch is not None:
            ch.put_nowait(choice)

    def _record_inputs(self, node_seq: int, validated: list[Any]) -> list[dict[str, Any]]:
        """Build this step's input_value rows from what OperatorInput recorded during its action.

        Blocking supplies and POLL clicks are typed from the step's declarations (CHECKBOX stored as
        JSON); a supplied index with no declaration is recorded verbatim so nothing asked for is lost."""
        records: list[dict[str, Any]] = []
        # ``key`` is the 0-based get_raw position (n-1) under which each blocking value was stored.
        for key, raw in self._op.supplied_inputs():
            decl = validated[key] if 0 <= key < len(validated) else None
            records.append(self._value_text(node_seq, 1, decl.index if decl is not None else key + 1, decl, raw))
        # POLL clicks never block the worker; record each one as its own row keyed by context.
        for _node_seq_ctx, attempt_no, key, at, text in self._op.poll_clicks():
            records.append(
                {
                    "node_seq": node_seq,
                    "attempt_no": attempt_no,
                    "input_index": key + 1,
                    "type": "POLL",
                    "value_text": text,
                    "at": at,
                }
            )
        return records

    def _value_text(self, node_seq: int, attempt_no: int, input_index: int, decl: Any, raw: Any) -> dict[str, Any]:
        """One input_value row for a blocking supply, typed from its declaration."""
        if decl is not None:
            return {
                "node_seq": node_seq,
                "attempt_no": attempt_no,
                "input_index": input_index,  # the 1-based n in ATE.input(n)
                "type": decl.type,
                "value_text": self._serialise(decl, raw),
                "at": _now_iso(),
            }
        return {
            "node_seq": node_seq,
            "attempt_no": attempt_no,
            "input_index": input_index,  # no declaration for this index (defensive)
            "type": "",
            "value_text": str(raw),
            "at": _now_iso(),
        }

    def _serialise(self, decl: Any, raw: Any) -> str:
        """Serialise a supplied raw value to its stored text form for one declaration."""
        if decl.type == "ENUM" and decl.widget == "CHECKBOX":
            labels = list(raw) if isinstance(raw, (list, tuple)) else [raw]
            return json.dumps([str(label) for label in labels])  # CHECKBOX is recorded as JSON
        typed = self._host._map_input(decl, raw)
        return str(typed)

    def _record_abort(self, run_uuid: str, steps: list[Any], aborted_seq: int, *, record_attempt: bool = True) -> None:
        """Record a whole-run abort that took effect at step ``aborted_seq`` (0-based).

        The step about to run records one ERROR attempt "Aborted by Operator" and its node as
        ERROR; every later actionable step is recorded NOTRUN with no Attempt. Nothing after the
        abort point ran, yet the aborted step still has an Attempt explaining why it stopped.

        ``record_attempt`` is False when the abort came from a Stop-on-Fail pause (packet 12b): that
        failing step already has its FAIL/ERROR Attempt and prompt_choice recorded, so only the later
        steps are marked NOTRUN and its result is kept."""
        if record_attempt:
            step = steps[aborted_seq]
            self._store.insert_attempt(run_uuid, aborted_seq, 1, pass_fail="ERROR", msg="Aborted by Operator")
            default_enabled, enabled = self._enabled(aborted_seq, step)
            self._store.insert_node(
                run_uuid,
                aborted_seq,
                kind="step",
                step_number=step.step_no or "",
                step_name=step.name,
                default_enabled=default_enabled,
                enabled=enabled,
                result="ERROR",
                attempt_count=1,
            )
        for seq in range(aborted_seq + 1, len(steps)):
            later = steps[seq]
            if later.action is None:
                continue
            default_enabled, enabled = self._enabled(seq, later)
            self._store.insert_node(
                run_uuid,
                seq,
                kind="step",
                step_number=later.step_no or "",
                step_name=later.name,
                default_enabled=default_enabled,
                enabled=enabled,
                result="NOTRUN",
                attempt_count=0,
            )

    @staticmethod
    def _phase_actions(schedule: Any, phase: str) -> list[Callable[..., Any]]:
        """Return the action closures for one Pre-test/Post-test phase, in run order.

        ``schedule.pre_test_action`` / ``post_test_action`` are each a single closure under the
        current builder model; wrap it so both phases and any future multi-action schedules share
        one path (Post-test callers reverse this list to honour "run in reverse")."""
        actions = getattr(schedule, f"{phase}_test_action", None)
        if actions is None:
            return []
        if callable(actions) and not isinstance(actions, list):
            return [actions]
        return [a for a in actions if a is not None]

    def _record_phase_action(
        self, host: "_LuaHost", run_uuid: str, *, instance_seq: int, phase: str, fn: Callable[..., Any], pass_fail: str
    ) -> None:
        """Run one Pre-test/Post-test action closure and record its row (and owned log lines).

        ``phase`` is "pre" or "post"; the internal context string ("pretest"/"posttest") keeps the
        host's lockdown/location logic intact. On success the outcome is "ok"; a raised Lua error
        becomes an "error" row carrying its first line, with the traceback logged at ERROR and both
        attributed to this action via ``action_seq`` so later packets can surface it. Returns
        ``(outcome, error)`` so the caller can abort on a Pre-test failure (packet 14b)."""
        started_at = _now_iso()
        outcome = "ok"
        error = None
        log_start = len(host.logs)
        try:
            host._run_action(fn, PHASE_CONTEXT[phase], pass_fail=pass_fail)
        except Exception as exc:  # noqa: BLE001 - a Lua error becomes an ERROR action row
            # A second abort during a Post-test Action ends only that action (packet 14c): the count
            # hook raises exactly "aborted", so record outcome "aborted" and don't log it as an
            # ERROR — it is not a run error, and the caller keeps running any remaining actions.
            if str(exc).splitlines()[0].strip() == "aborted":
                outcome = "aborted"
            else:
                outcome = "error"
                error = str(exc).splitlines()[0] if str(exc) else ""
                host.record_log("ERROR", str(exc))
        finally:
            ended_at = _now_iso()
        new_logs = host.logs[log_start:]
        if new_logs:
            self._store.insert_log_lines(
                run_uuid, 0,
                [{"level": level, "source": source, "text": text} for (level, source, text) in new_logs],
                action_seq=instance_seq,
            )
        self._store.insert_action(
            run_uuid, instance_seq=instance_seq, phase=phase, run_order=0,
            outcome=outcome, error=(error or None), started_at=started_at, ended_at=ended_at,
        )
        return outcome, error

    def _record_pretest_failure(self, run_uuid: str, steps: list[Any], *, failed_seq: int, error_text: str) -> None:
        """Record that a failing Pre-test Action (which started at pre-test ``failed_seq``) aborted the run.

        No step ran yet, so every actionable step is recorded NOTRUN with no Attempt; the failing
        Pre-test's own ERROR row and traceback already exist from ``_record_phase_action``. A single
        log line names the failure and explains why every step is NOTRUN."""
        for seq in range(len(steps)):
            step = steps[seq]
            if step.action is None:  # header/include rows have no node recorded (see run)
                continue
            default_enabled, enabled = self._enabled(seq, step)
            self._store.insert_node(
                run_uuid,
                seq,
                kind="step",
                step_number=step.step_no or "",
                step_name=step.name,
                default_enabled=default_enabled,
                enabled=enabled,
                result="NOTRUN",
                attempt_count=0,
            )
        self._store.insert_log_lines(
            run_uuid, 0,
            [{"level": "ERROR", "source": "ate", "text": f"Pre-test Action failed ({error_text}); the run aborted so every step is NOTRUN."}],
            action_seq=failed_seq,
        )

    def _enabled(self, seq: int, step: Any) -> tuple[int, int]:
        """Return ``(default_enabled, enabled)`` for a runner step at ``seq``.

        The default comes from the author's :setEnable(); an Operator override wins when one is set."""
        default_enabled = bool(step.enabled)
        effective_enabled = self._overrides.get(seq, default_enabled)
        return int(default_enabled), int(effective_enabled)

    def run(self) -> None:
        """Execute the schedule and record every step's Attempt through the store."""
        # A fresh LuaRuntime per load (see lua_host); real execution is stubbed there. The
        # OperatorInput controller this run owns is injected so its ATE.input calls read/write the
        # same blocking waits and POLL state the GUI drives through supply_input/click_poll.
        host = LuaHost(
            self._schedule_path,
            operator_input=self._op,
            aborted_flag=self._abort_flag,  # the count hook reads this; see request_abort/aborted
            modules=self._modules,  # packet 06a: forward the require'd module map to the host
        )
        self._host = host  # kept on the engine so per-step input values can be typed from declarations
        for name, fn in self._ate_fns.items():
            host.ate.register(name, fn)  # criterion 03: isolate a failing ATE call through Lua
        schedule = host.schedule
        # Criterion c: publish this run's identifying metadata into the read-only ATE.run table so a
        # step can READ it (e.g. which UUT/operator/project it is running under). Empty fields seed
        # as nil; writes from Lua are rejected by the table's read-only guard.
        host.ate.publish_run(
            {
                "UUT_serial": self.uut_serial or None,
                "Operator": self.operator or None,
                "Station": self.station_name or None,
                "Project": self.project or None,
                "Test_Schedule_Name": schedule.name if schedule else None,
                "Media_Release_Media_Number": self.media_number or None,
                "name": schedule.name if schedule else None,
                "version": self.ate_version or None,
            }
        )
        steps = schedule.steps

        run_uuid = new_uuid7()
        created = self._store.insert_test_run(
            {
                "run_uuid": run_uuid,
                "station_uuid": self.station_uuid or None,
                "station_name": self.station_name or None,
                "operator": self.operator,
                "project": self.project,
                "uut_type": self.uut_type,
                "uut_serial": self.uut_serial,
                "ate_version": self.ate_version or None,
                "schedule_name": schedule.name if schedule else None,
                "media_number": self.media_number or None,
                "stop_on_fail": bool(self.stop_on_fail),
                "utc_offset": self.utc_offset,
            }
        )
        # Packet 06b: record the Media Release that owns this run (seq 0) straight after the test_run
        # row. Without a resolved definition there is nothing to record — bare schedule tests behave as before.
        if self._definition is not None:
            atedef_path = getattr(self._definition, "atedef_path", None)
            self._store.insert_media_release(
                run_uuid,
                media_number=self._definition.media_number or None,
                name=self._definition.name or None,
                version=self._definition.version or None,
                atedef_sha256=_sha256_hex(atedef_path.read_bytes()) if atedef_path is not None else None,
            )
        # Report the run key and mark RUNNING on the main thread, both queued signals
        # delivered after ``start()`` returns; emitted from the worker before any other
        # row is written beyond this one.
        self.run_uuid_reported.emit(created["run_uuid"])
        self.state_changed.emit(RunStatus.RUNNING)

        failed = False  # the run's standing result; a step flips pass_fail to FAIL on any FAIL/ERROR
        pass_fail = "PASS"  # the run's standing result; a step flips it to FAIL on any FAIL/ERROR
        aborted = False  # a whole-run abort requested between steps (see request_abort)

        # Pre-test Actions run up front, in order, each called with the current pass_fail. Nothing
        # has failed yet so this is "PASS"; 13b owns what happens if one fails (aborting). Each runs
        # before any step and records an action row plus its own log lines first.
        pretest_failed = False  # a Pre-test Action that raised aborts the whole run (packet 14b)
        for seq, action_fn in enumerate(self._phase_actions(schedule, "pre")):
            outcome, error = self._record_phase_action(host, run_uuid, instance_seq=seq, phase="pre", fn=action_fn, pass_fail=pass_fail)
            if outcome == "error":
                # A failing Pre-test Action aborts automatically before any step runs.
                pretest_failed = True
                self._record_pretest_failure(run_uuid, steps, failed_seq=seq, error_text=error or "")
                break

        for seq, step in enumerate(steps):
            if pretest_failed:
                # Already recorded every step NOTRUN above; stop before running any.
                break

            # Header/include rows have no action; only ordinary steps run.
            if step.action is None:
                continue
            # A step disabled at run start (its :setEnable() default or an Operator override) isn't
            # executed; record it NOTRUN with no Attempt and move on. See :meth:`_enabled`.
            default_enabled, enabled = self._enabled(seq, step)
            if not enabled:
                self._store.insert_node(
                    run_uuid,
                    seq,
                    kind="step",
                    step_number=step.step_no or "",
                    step_name=step.name,
                    default_enabled=default_enabled,
                    enabled=enabled,
                    result="NOTRUN",
                    attempt_count=0,
                )
                continue
            # Whole-run abort checked at the step boundary — this packet covers stopping cleanly
            # between steps; interrupting a running (blocking or busy) step comes in 13b/13c. If
            # set, record the step about to run as ERROR and mark every later step NOTRUN, then stop.
            if self.aborted:
                aborted = True
                self._record_abort(run_uuid, steps, seq)
                break

            # Tell the host which step is running so an isolated ATE error can be attributed to
            # the schedule line where that step's action began (criterion 03), and put the host in
            # the "step" phase so a step may legally call any ATE function — including ATE.report
            # and ATE.input, which are rejected elsewhere (criterion b). _run_action sets and then
            # restores that context around the action.
            host._current_step = seq
            # Ticket 10: put this step's OperatorInput context in place before the action runs so
            # ATE.input(n) reads POLL sticky state under the right key and range-checks against THIS
            # step's declarations, install those declarations on the host, and announce each asked
            # input. The input_asked signals are queued here (before _run_action blocks), so a
            # blocking supply from another thread wakes get_raw while the worker waits; op.reset()
            # starts this step's recording window clean so its values attach to it.
            self._op.reset()
            validated = getattr(step, "_validated_inputs", None) or []
            host._node_seq = seq
            host._attempt_no = 1
            host._step_inputs = list(validated)
            for index in range(1, len(validated) + 1):
                # Announce this input so the OperatorInput controller counts it as outstanding; a
                # blocking supply or POLL click decrements that count when delivered.
                self._op.note_asked()
                self.input_asked.emit(seq, 1, index)
            start = len(host.reports)
            log_start = len(host.logs)
            error_text: Optional[str] = None
            try:
                host._run_action(step.action, "step")  # runs the closure in Lua; may call ATE.report several times
            except Exception as exc:  # noqa: BLE001 - a Lua error becomes an ERROR Attempt
                # The first line is the human-readable message (for a Lua-originated error it
                # is just the message; file:line lives in the traceback portion). The full
                # traceback — which carries the schedule's file:line — is logged at ERROR so
                # criterion 03 can rebuild "<file>:<line>: ATE.<func>:" from it.
                error_text = str(exc).splitlines()[0] if str(exc) else ""
                # Packet 13c: an abort raised mid-step (the count hook, or a re-raised pcall/xpcall)
                # ends the run here — record this running step as ERROR and mark every later step
                # NOTRUN instead of treating it as an ordinary failure. The sentinel is exactly the
                # string "aborted" that the hook raises; a genuine error whose first line merely
                # contains it is left to fail normally below.
                if error_text.strip() == "aborted":
                    aborted = True
                    self._record_abort(run_uuid, steps, seq)
                    break
                host.record_log("ERROR", str(exc))

            new_reports = host.reports[start:]
            last_result = "NOREPORT"
            attempts_recorded = 0
            if error_text is not None:
                self._store.insert_attempt(
                    run_uuid, seq, 1, pass_fail="ERROR", msg=error_text
                )
                attempts_recorded = 1
                last_result = "ERROR"  # the step has an ERROR attempt; reflect it on the node
            else:
                for report in new_reports:
                    pass_fail, msg, actual, expected = report
                    self._store.insert_attempt(
                        run_uuid,
                        seq,
                        1,
                        pass_fail=pass_fail,
                        msg=msg,
                        actual=_to_text(actual),
                        expected=_to_text(expected),
                    )
                    attempts_recorded += 1
                    last_result = pass_fail
                if not new_reports:
                    # Ran but never reported -> NOREPORT (logged as a warning later).
                    self._store.insert_attempt(
                        run_uuid, seq, 1, pass_fail="NOREPORT", msg=None
                    )
                    attempts_recorded = 1

            # Every schedule-sourced log line emitted during this step's action belongs to
            # that Attempt (source "schedule"); a step that never reported adds its own
            # warning (source "ate"). Both are batched into one insert keyed by the step.
            new_logs = host.logs[log_start:]
            if new_logs:
                self._store.insert_log_lines(
                    run_uuid,
                    seq,
                    [
                        {"level": level, "source": source, "text": text}
                        for (level, source, text) in new_logs
                    ],
                    node_seq=seq,
                    attempt_no=1,
                )
            if not new_reports:
                self._store.insert_log_lines(
                    run_uuid,
                    seq,
                    [{"level": "WARNING", "source": "ate", "text": f"step '{step.name}' never reported"}],
                    node_seq=seq,
                    attempt_no=1,
                )

            # A step fails the whole run on FAIL or ERROR — whether that came from an
            # ATE.report call above or from a raised action (recorded as an ERROR attempt).
            if last_result == "FAIL" or error_text is not None:
                failed = True
                pass_fail = "FAIL"  # flip the standing result so Post-test Actions see it

            # Ticket 10: wait for every announced input to be answered before recording this step's
            # values. Blocking supplies already woke the worker via get_raw; POLL clicks never block —
            # so without this wait a worker could record (and drop an unanswered click) before the
            # operator's answer was delivered on another thread. A blocking supply or POLL click
            # decrements the outstanding count when it arrives, waking this wait.
            self._op.wait_for_asks()
            # Ticket 10: record every input value this step asked for. Supplies and POLL clicks the
            # OperatorInput controller recorded during the action are typed from this step's
            # declarations and written as input_value rows; the declarations themselves are stored
            # on the node (as JSON, in schedule format via StepInput.to_dict) so the GUI can render
            # each input under its step at all times.
            records = self._record_inputs(seq, validated)
            if records:
                self._store.insert_input_values(run_uuid, records)

            default_enabled, enabled = self._enabled(seq, step)
            self._store.insert_node(
                run_uuid,
                seq,
                kind="step",
                step_number=step.step_no or "",
                step_name=step.name,
                default_enabled=default_enabled,
                enabled=enabled,
                result=last_result,
                attempt_count=attempts_recorded,
                inputs=[d.to_dict() for d in validated] if validated else None,
            )

            # Stop on Fail: a failed step pauses until the operator chooses (packet 12b). The failing
            # Attempt is already recorded above; once choose() returns we stamp its prompt_choice and
            # either keep going (CONTINUE/RETRY) or stop cleanly (ABORT ->
            # remaining steps NOTRUN, End Reason STOPPED_ON_FAIL / result FAIL). Each later failure
            # pauses again; only Continue on fail (stop_on_fail False) never pauses. RETRY's rerun semantics are 12c; here it keeps going like
            # Continue so the seam exists and nothing blocks on an unanswered choice.
            if self.stop_on_fail:
                if last_result == "FAIL" or error_text is not None:
                    failed = True
                    pass_fail = "FAIL"
                    choice = self._pause_for_operator(run_uuid, seq)
                    # _pause_for_operator blocked until choose(); it recorded prompt_choice and returned.
                    if choice == "ABORT":
                        # Stop cleanly between steps; keep the failed step's result (already recorded).
                        self._stopped_on_fail = True
                        self._record_abort(run_uuid, steps, seq, record_attempt=False)
                        break

        # Post-test Actions always run after the last step, in reverse, each called with the final
        # pass_fail. Before they start we clear the abort flag (packet 14c) so a previously-aborted
        # run can still tear down; but if it had already been aborted we remember that here so its
        # End Reason stays ABORTED even though the flag is now cleared. A Post-test Action that
        # raised fails the run without changing this reason (packet 14c); a second abort during one
        # ends only that action and leaves the rest to run.
        posttest_aborted = self.aborted
        for seq, action_fn in enumerate(reversed(self._phase_actions(schedule, "post"))):
            # Clear before every action, not just the first: a second abort ends only the action it
            # lands in, so the flag must not carry over and kill the next one on its first hook tick.
            self._abort_flag.clear()
            outcome, error = self._record_phase_action(host, run_uuid, instance_seq=seq, phase="post", fn=action_fn, pass_fail=pass_fail)
            if outcome == "aborted":
                posttest_aborted = True
            elif outcome == "error":
                # A Post-test Action that raised fails the run (result FAIL) but keeps its End Reason.
                failed = True
                pass_fail = "FAIL"

        # A Pre-test failure aborts before any step runs (packet 14b); a Stop-on-Fail pause that was
        # aborted ends STOPPED_ON_FAIL; a whole-run operator abort — whether between steps or already
        # set when the Post-test Actions began — ends ABORTED; a normal finish is FAIL on any FAIL/
        # ERROR, otherwise PASS.
        if pretest_failed:
            run_result = "FAIL"
            end_reason = "PRETEST_FAILED"
        elif self._stopped_on_fail:
            run_result = "FAIL"
            end_reason = "STOPPED_ON_FAIL"
        elif aborted or posttest_aborted:
            run_result = "FAIL"
            end_reason = "ABORTED"
        else:
            run_result = "FAIL" if failed else "PASS"
            end_reason = "COMPLETED"
        # Packet 06b: record every file this host loaded (the schedule plus each module it required,
        # in load order) with its expected and computed checksums; a mismatch adds one WARNING log line.
        self._record_files(run_uuid, host)
        self._store.update_run_end(run_uuid, result=run_result, end_reason=end_reason)
        # FINISHED is queued to the main thread; QThread's own ``finished`` signal also
        # fires when run() returns. Observers use state_changed for the observable states.
        self.state_changed.emit(RunStatus.FINISHED)

    def _record_files(self, run_uuid: str, host: "_LuaHost") -> None:
        """Record one ``run_file`` row per file used this run and warn on mismatches (packet 06b).

        Iterates the host's load-order list of ``(path, kind, sha256)`` entries; each is matched back
        to its Test Definition entry by absolute path so we can carry the definition-listed ``rel_path``,
        owning media number and expected checksum. A file that loaded but isn't listed has no expected
        value to compare against, so it is skipped rather than recorded with a null expectation."""
        if self._definition is None:
            return
        by_path = {self._definition.path / entry.rel_path: entry for entry in self._definition.files}
        mismatches: list[tuple[str, Optional[str], str]] = []
        for path, kind, computed in getattr(host, "_files_used", []):
            entry = by_path.get(Path(path))
            if entry is None:
                continue
            mismatch = (entry.expected_sha256 or None) != computed
            self._store.insert_run_file(
                run_uuid,
                path=entry.rel_path,
                media_number=self._definition.media_number or None,
                kind=kind,
                expected_sha256=entry.expected_sha256 or None,
                computed_sha256=computed,
                mismatch=mismatch,
            )
            if mismatch:
                mismatches.append((entry.rel_path, entry.expected_sha256, computed))
        for rel_path, expected, computed in mismatches:
            self._store.insert_log_lines(
                run_uuid, 0,
                [{"level": "WARNING", "source": "ate", "text": f"checksum mismatch for {rel_path} (expected {expected}, got {computed})"}],
            )
