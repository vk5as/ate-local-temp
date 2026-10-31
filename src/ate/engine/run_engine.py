"""The run engine: one QThread worker that runs a Test Schedule and records it.

Ticket 02's flow-control slice. On ``run()`` (executed on the worker thread) the engine:

1. inserts a fresh :class:`~ate.storage.store.RunStore` test_run row, stamping the
   Station UTC offset once for the run;
2. steps through the schedule's expanded steps in order — skipping header/include rows
   that have no action — invoking each step's Lua action closure once per Attempt and
   recording one ``attempt`` row per execution, with its last ``ATE.report`` (last call wins); when the
   operator chooses RETRY the action runs again as a new Attempt, and a step whose action
   raises records an ERROR Attempt;
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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from PyQt6.QtCore import QThread, pyqtSignal

from ..catalog import _search_chain, include_map
from ..errors import ATEError
from ..devices.registry import DEFAULT_RECEIVE_BUFFER_BYTES
from ..devices.tracing import DEFAULT_TRACE_MAX_BYTES  # packet 17c: the default deviceTrace.maxBytes
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


# Reported values are stored as text exactly as Lua prints them; the Lua host owns that conversion
# (see :meth:`_LuaHost.to_text`), so nothing here re-serialises a value that came out of Lua.

# The phase a Pre-test/Post-test Action runs in, stored on its action row ("pre"/"post"), mapped to
# the internal execution context string the Lua host uses for lockdown and error location. A step
# keeps "step"; loading/idle is None.
PHASE_CONTEXT = {"pre": "pretest", "post": "posttest"}


class RunEngine(QThread):
    """Runs one Test Schedule on a worker thread and records it through the store."""

    run_uuid_reported = pyqtSignal(str)  # the freshly generated run key, on start
    state_changed = pyqtSignal(RunStatus)  # IDLE -> RUNNING -> FINISHED, main thread
    input_asked = pyqtSignal(int, int, int)  # (node_seq, attempt_no, index): a step is asking for its n-th input
    step_finished = pyqtSignal(int)  # node_seq: this step's Attempts are done, so its POLL inputs close
    failure_prompt = pyqtSignal(int, int)  # (node_seq, attempt_no): a Stop-on-Fail failure is pending an operator choice
    # (node_seq, attempt_no, choice): the pause above was answered and the worker is running again.
    prompt_answered = pyqtSignal(int, int, str)
    action_started = pyqtSignal(str, int, int)  # (phase "pre"/"post", completed, total): a Pre-/Post-test Action is about to run (packet 14d)
    step_started = pyqtSignal(int)  # node_seq: a Test Step Attempt is about to run, again on each Retry (packet 14d)

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
        store_mismatched_files: bool = False,  # packet 06c: store the raw bytes of a file whose checksum differs
        parent: Optional[object] = None,
        ate_fns: Mapping[str, Callable[..., Any]] | None = None,
        operator_input: Optional[OperatorInput] = None,
        receive_buffer_bytes: int = DEFAULT_RECEIVE_BUFFER_BYTES,  # packet 17a: the per-device receive cap
        device_trace_enabled: bool = False,  # packet 17c: deviceTrace.enabled — one DEBUG line per operation
        device_trace_max_bytes: int = DEFAULT_TRACE_MAX_BYTES,  # packet 17c: deviceTrace.maxBytes truncation
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
        # Packet 15d: the expanded tree's parent links, built once per run by _tree_children, so a
        # header row can ask whether any Test Step beneath it is enabled.
        self._children: Optional[dict[int, list[int]]] = None
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
        # Packet 17a: the per-device receive buffer cap, forwarded to the host (and thus to each
        # device's open) so a chatty device can't exhaust memory; the config sets it at the session.
        self._receive_buffer_bytes = receive_buffer_bytes
        # Packet 17c: device tracing (deviceTrace.enabled/maxBytes) forwarded to the host's registry,
        # which wraps each handle it opens and logs every operation at DEBUG.
        self._device_trace_enabled = bool(device_trace_enabled)
        self._device_trace_max_bytes = int(device_trace_max_bytes)
        # Packet 06b: the resolved Media Release that owns this run, forwarded from the session so we
        # can record its media_release row (seq 0) and every file used with expected/computed checksums.
        # ``None`` means a bare schedule test — nothing extra is recorded then.
        self._definition = definition
        # Packet 15b: the Sub-schedule resolver for the owning release and its chain, forwarded to
        # the host so each `setInclude` resolves from the release that owns the including file, own
        # files first and then its dependencies. None for a bare schedule test.
        self._includes = include_map(definition) if definition is not None else None
        # Packet 06c: whether this run stores the raw bytes of a file whose checksum differs from the
        # Test Definition's. The Run screen's confirmation (or `database.storeMismatchedFiles`) decides
        # it at the session; a run started without one stores nothing.
        self._store_mismatched_files = bool(store_mismatched_files)
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
        # Packet 17b: who owns a device failure drained right now — the running step Attempt or
        # Pre-/Post-test Action as (window start, ownership kwargs), or None for the run — and the
        # run's drain callable (also the host's device_warning_sink), set once the run row exists.
        self._device_owner: Optional[tuple[float, dict[str, int]]] = None
        self._device_sink: Optional[Callable[[], None]] = None
        # The first index into the host's ``logs`` not yet written to the store; set to the log
        # start whenever an action begins (its owner is set), advanced by every drain.
        self._log_cursor = 0
        # Packet 20: the first index into the host's ``equipment`` not yet written to the store;
        # advanced by every drain, so each `ATE.addEquipment` call is recorded in call order.
        self._equipment_cursor = 0
        # Set when a Stop-on-Fail pause is aborted (packet 12b); ends the run STOPPED_ON_FAIL.
        self._stopped_on_fail = False
        # Packet 10a review: which step/Attempt is running right now, and which of its POLL inputs
        # are open. An answered blocking input, or a POLL whose step has ended, is no longer live,
        # and the session rejects answers sent for anything else.
        self._ask_step: Optional[int] = None
        self._ask_attempt: int = 0
        self._ask_inputs: list[Any] = []
        self._poll_open: set[tuple[int, int, int]] = set()
        # A blocking ask announced by the worker (get_raw) reaches the GUI through input_asked.
        self._op.asked.connect(self._on_asked)

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

    def _on_asked(self, node_seq: int, attempt_no: int, index: int) -> None:
        """A blocking ``ATE.input(index)`` has just started waiting: announce it as an input ask.

        Qt queues this cross-thread signal to the engine's own thread, so the GUI and the session see
        the ask while the worker sits in :meth:`OperatorInput.get_raw` (packet 10a review)."""
        self.input_asked.emit(node_seq, attempt_no, index)

    def input_declaration(self, node_seq: int, attempt_no: int, index: int) -> Optional[Any]:
        """The running step's n-th Step Input declaration, or None if that step/Attempt/index isn't running.

        The session uses it to reject an answer sent for a step that isn't asking, an old Attempt, or
        an index the step never declared (packet 10a review)."""
        if node_seq != self._ask_step or attempt_no != self._ask_attempt:
            return None
        if 1 <= index <= len(self._ask_inputs):
            return self._ask_inputs[index - 1]
        return None

    def is_asking(self, node_seq: int, attempt_no: int, index: int) -> bool:
        """True while this step's blocking ``ATE.input(index)`` call is actually waiting."""
        decl = self.input_declaration(node_seq, attempt_no, index)
        return decl is not None and decl.type != "POLL" and self._op.is_waiting(index - 1)

    def is_poll_open(self, node_seq: int, attempt_no: int, index: int) -> bool:
        """True while this step's POLL button is live — that is, while its Attempt runs."""
        return (node_seq, attempt_no, index) in self._poll_open

    def _pause_for_operator(self, run_uuid: str, node_seq: int, attempt_no: int) -> str:
        """Stop-on-Fail pause for a failed Attempt: announce the pending choice and block until answered.

        The failing Attempt is already recorded; once :meth:`choose` returns a value we stamp its
        ``prompt_choice`` column (packet 12b checkbox 4). Blocks with no timeout — the worker waits
        here while the operator/main thread supplies CONTINUE, RETRY or ABORT through choose()."""
        ch = queue.Queue()
        with self._pause_lock:
            self._pending[(node_seq, attempt_no)] = ch
        # Mark this Attempt as ending at a prompt; resolved to its choice once choose() returns.
        self._store.update_attempt(run_uuid, node_seq, attempt_no, prompt_choice=None)
        # Announce the pending choice on the owning thread so an operator/test can answer it there,
        # carrying this Attempt's number so a retried failure is announced as Attempt 2, 3, ...
        self.failure_prompt.emit(node_seq, attempt_no)
        # Blocks until choose() puts a value and wakes us; meanwhile a device failure (run-level: the
        # Attempt's action has ended) is still logged at once (packet 17b).
        while True:
            self._drain_device_warnings()
            try:
                choice = ch.get(timeout=0.05)
                break
            except queue.Empty:
                continue
        self._drain_device_warnings()
        # Stamp the chosen CONTINUE/RETRY/ABORT onto this Attempt now that we hold it (packet 12b checkbox 4).
        self._store.update_attempt(run_uuid, node_seq, attempt_no, prompt_choice=choice)
        with self._pause_lock:
            self._pending.pop((node_seq, attempt_no), None)
        # The pause is over: every layout clears its prompt UI from this, not from its own button.
        self.prompt_answered.emit(node_seq, attempt_no, choice)
        return choice

    def pause_pending(self) -> bool:
        """True while the worker is blocked waiting for a Stop-on-Fail choice (packet 25b).

        The layouts ask this instead of looking at their own prompt bar: the bar of a layout that is
        not on screen is not the run's truth, and a pause answered in one layout is over for both."""
        with self._pause_lock:
            return bool(self._pending)

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

    def _record_inputs(self, node_seq: int, validated: list[Any], attempt_no: int) -> list[dict[str, Any]]:
        """Build this Attempt's input_value rows from what OperatorInput recorded during its action.

        Blocking supplies and POLL clicks are typed from the step's declarations (CHECKBOX stored as
        JSON); a supplied index with no declaration is recorded verbatim so nothing asked for is lost.
        Every row carries ``attempt_no`` so a retried Attempt's values land on its own rows (12c)."""
        records: list[dict[str, Any]] = []
        # ``key`` is the 0-based get_raw position (n-1) under which each blocking value was stored.
        for key, raw in self._op.supplied_inputs():
            decl = validated[key] if 0 <= key < len(validated) else None
            records.append(self._value_text(node_seq, attempt_no, decl.index if decl is not None else key + 1, decl, raw))
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
            "value_text": self._host.to_text(raw) or "",
            "at": _now_iso(),
        }

    def _serialise(self, decl: Any, raw: Any) -> str:
        """Serialise a supplied raw value to its stored text form for one declaration.

        The typed value is what the schedule reads back from ``ATE.input(n)``, so its text is Lua's
        ``tostring`` of it, not Python's ``str`` (see :meth:`_LuaHost.to_text`)."""
        if decl.type == "ENUM" and decl.widget == "CHECKBOX":
            labels = list(raw) if isinstance(raw, (list, tuple)) else [raw]
            return json.dumps([self._host.to_text(label) or "" for label in labels])  # CHECKBOX is recorded as JSON
        typed = self._host._map_input(decl, raw)
        return self._host.to_text(typed) or ""

    def _insert_node(self, run_uuid: str, seq: int, node: Any, **fields: Any) -> None:
        """Write one node row, stamped with its place in the expanded tree (packet 15b).

        `node` is the tree entry, so `parent_node_seq` and `instance_seq` come from the tree walk
        the host did at load; the caller supplies the kind, number, name, enable flags, result and
        Attempt count. `seq` is the node's index in the tree, which is its `node_seq`."""
        self._store.insert_node(
            run_uuid,
            seq,
            parent_node_seq=node.parent,
            instance_seq=node.instance,
            **fields,
        )

    def _record_abort(
        self, run_uuid: str, steps: list[Any], aborted_seq: int, *, record_attempt: bool = True, attempt_no: int = 1
    ) -> None:
        """Record a whole-run abort that took effect at step ``aborted_seq`` (0-based).

        The step about to run records one ERROR attempt "Aborted by Operator" and its node as
        ERROR; every later actionable step is recorded NOTRUN with no Attempt. Nothing after the
        abort point ran, yet the aborted step still has an Attempt explaining why it stopped.

        ``record_attempt`` is False when the abort came from a Stop-on-Fail pause (packet 12b): that
        failing step already has its FAIL/ERROR Attempt and prompt_choice recorded, so only the later
        steps are marked NOTRUN and its result is kept. ``attempt_no`` names the Attempt the abort
        landed on — Attempt 1 for a step boundary, the in-flight Attempt's number when a retry's
        rerun was interrupted (packet 12c), so the ERROR row never collides with Attempt 1's."""
        if record_attempt:
            step = steps[aborted_seq]
            self._store.insert_attempt(
                run_uuid, aborted_seq, attempt_no, pass_fail="ERROR", msg="Aborted by Operator"
            )
            default_enabled, enabled = self._enabled(aborted_seq, step)
            self._insert_node(
                run_uuid,
                aborted_seq,
                step,
                kind="step",
                step_number=step.step_no or "",
                step_name=step.name,
                default_enabled=default_enabled,
                enabled=enabled,
                result="ERROR",
                attempt_count=attempt_no,
            )
        for seq in range(aborted_seq + 1, len(steps)):
            later = steps[seq]
            if later.is_header:  # a header never ran, so it records no Attempt and no Result
                self._record_header(run_uuid, seq, later)
                continue
            default_enabled, enabled = self._enabled(seq, later)
            self._insert_node(
                run_uuid,
                seq,
                later,
                kind="step",
                step_number=later.step_no or "",
                step_name=later.name,
                default_enabled=default_enabled,
                enabled=enabled,
                result="NOTRUN",
                attempt_count=0,
            )

    def _record_header(self, run_uuid: str, seq: int, node: Any) -> None:
        """Record one header row of the expanded tree: a row that never runs (packets 15a, 15b).

        A header (an include step, packet 15a) has no Attempt and no Result — `result` stays null
        and `attempt_count` is 0 — so it is left out of the summary counts and can never make the
        run PASS or FAIL. Its own Step Number is optional, so it is stored null when absent.
        Packet 15d: `default_enabled` is the header's own default after any enclosing header's
        :setEnable() forced it, and `enabled` is whether any Test Step beneath it is enabled —
        the header row itself is never toggled as a whole."""
        default_enabled, enabled = self._enabled(seq, node)
        self._insert_node(
            run_uuid,
            seq,
            node,
            kind="header",
            step_number=node.step_no or None,
            step_name=node.name,
            default_enabled=default_enabled,
            enabled=enabled,
            result=None,
            attempt_count=0,
        )

    @staticmethod
    def _phase_actions(schedule: Any, phase: str) -> list[Callable[..., Any]]:
        """Return the action closures for one schedule instance's Pre-test/Post-test phase, in run order.

        ``schedule.pre_test_action`` / ``post_test_action`` are each a single closure under the
        current builder model; wrap it so both phases and any future multi-action schedules share
        one path. Packet 15e calls this once per schedule instance, walking the instances in tree
        order for Pre-test and in exactly that order reversed for Post-test."""
        actions = getattr(schedule, f"{phase}_test_action", None)
        if actions is None:
            return []
        if callable(actions) and not isinstance(actions, list):
            return [actions]
        return [a for a in actions if a is not None]

    def _flush_device_warnings(self, host: "LuaHost", run_uuid: str, *, in_window: tuple[float, float] | None = None,
                               log_from: int | None = None, node_seq: int | None = None,
                               attempt_no: int | None = None, action_seq: int | None = None) -> None:
        """Drain the devices' failure warnings — and, with ``log_from``, the running action's own log
        lines — and record them on one timeline, in the order they happened.

        Packet 17b: the reader (or a failed read/write) enqueues exactly one (time, message)
        warning per failure, promptly, and the engine drains it at once (see
        :meth:`_drain_device_warnings`). A warning whose
        timestamp falls inside ``in_window`` (the action's own start..end) belongs to that action: a
        step gets ``node_seq``/``attempt_no``, a Pre-/Post-test Action its ``action_seq``. A warning
        from outside the window — the device failed in the gap between actions, or the drain is the
        run-end one — attaches to the run as a whole (all three None).

        ``log_from`` is the first unwritten index into ``host.logs``: every line from there on was
        emitted during the running action, so it is owned by it whatever its time (the slice the
        action used to batch at its end). Lines and warnings are merged by their monotonic times
        (a stable sort, so lines logged in one instant keep their order) and inserted in that order,
        so ``seq`` is the order they happened; each line's ``at`` is its own time mapped to wall-clock,
        not the drain's. The cursor (:attr:`_log_cursor`) moves past the lines written."""
        pending = host.take_device_warnings()
        logs: list[tuple[float, dict[str, Any], bool]] = []
        if log_from is not None:
            end = len(host.logs)
            for (level, source, text), t in zip(host.logs[log_from:end], host.log_times[log_from:end]):
                logs.append((t, {"level": level, "source": source, "text": text}, True))
            self._log_cursor = end
        if not pending and not logs:
            return
        wall_offset = time.time() - time.monotonic()  # monotonic -> wall-clock, measured once per drain

        def owned(t: float) -> bool:
            return in_window is not None and in_window[0] <= t <= in_window[1]

        warnings = [(t, {"level": "WARNING", "source": "device", "text": text}, owned(t)) for t, text in pending]
        ownership = {"action_seq": action_seq, "node_seq": node_seq, "attempt_no": attempt_no}
        # One insert per run of consecutive lines with the same owner, so ``seq`` keeps the merged order.
        batch: list[dict[str, Any]] = []
        batch_owned = False
        for t, line, is_owned in sorted(logs + warnings, key=lambda entry: entry[0]):
            if batch and is_owned != batch_owned:
                self._store.insert_log_lines(run_uuid, 0, batch, **(ownership if batch_owned else {}))
                batch = []
            batch_owned = is_owned
            at = datetime.fromtimestamp(t + wall_offset, timezone.utc).isoformat(timespec="milliseconds")
            batch.append({**line, "at": at})
        self._store.insert_log_lines(run_uuid, 0, batch, **(ownership if batch_owned else {}))

    def _flush_equipment(self, host: "_LuaHost", run_uuid: str, *, node_seq: int | None = None,
                         attempt_no: int | None = None, action_seq: int | None = None) -> None:
        """Record every `ATE.addEquipment` call made since the last drain, in the order it happened (packet 20).

        The host keeps the Equipment its schedule declared; this writes the rows the engine has not
        seen yet, each stamped with the time the call was made (the host's monotonic clock mapped to
        wall-clock, exactly like a log line's ``at``) and owned by the running step Attempt or
        Pre-/Post-test Action, the same way that action's own log lines are. A no-op when nothing is
        pending, so the drain that runs on every ATE call stays cheap."""
        if self._equipment_cursor >= len(host.equipment):
            return
        wall_offset = time.time() - time.monotonic()  # monotonic -> wall-clock, measured once per drain
        for item in host.equipment[self._equipment_cursor:]:
            at = datetime.fromtimestamp(item["at"] + wall_offset, timezone.utc).isoformat(timespec="milliseconds")
            self._store.insert_equipment(
                run_uuid,
                name=item["name"],
                serial=item["serial"],
                connection=item["connection"],
                cal_due=item["cal_due"],
                notes=item["notes"],
                at=at,
                action_seq=action_seq,
                node_seq=node_seq,
                attempt_no=attempt_no,
            )
        self._equipment_cursor = len(host.equipment)

    def _drain_device_warnings(self) -> None:
        """Record pending device warnings now, owned by whatever is running (see ``_device_owner``),
        together with the running action's log lines and Equipment declarations emitted so far, in the
        order they happened.

        A no-op before the run row exists. Called at every ATE call (through the host's sink), through
        a Stop-on-Fail pause, at action end and at run end, so a failure is logged at once."""
        if self._device_sink is not None:
            self._device_sink()

    def _make_device_sink(self, host: "LuaHost", run_uuid: str) -> Callable[[], None]:
        """The drain callable for this run: a failure since the running action's start is owned by
        it, as is every log line it has emitted; anything else (between actions, during a pause)
        attaches to the run."""
        def sink() -> None:
            owner = self._device_owner
            if owner is None:
                # Nothing is running (the gap between actions, or the run-end teardown): lines emitted
                # since the last drain belong to the run as a whole — 17c's trace of the closes this
                # engine performs on devices the schedule left open, among them.
                self._flush_device_warnings(host, run_uuid, log_from=self._log_cursor)
                self._flush_equipment(host, run_uuid)
            else:
                start, ownership = owner
                self._flush_device_warnings(
                    host, run_uuid, in_window=(start, time.monotonic()), log_from=self._log_cursor, **ownership
                )
                self._flush_equipment(host, run_uuid, **ownership)
        return sink

    def _record_phase_action(
        self, host: "_LuaHost", run_uuid: str, *, instance_seq: int, phase: str, fn: Callable[..., Any], pass_fail: str,
        run_order: int = 0
    ) -> None:
        """Run one Pre-test/Post-test action closure and record its row (and owned log lines).

        ``phase`` is "pre" or "post"; ``instance_seq`` is the schedule instance the Action belongs to
        (packet 15e — the same value the `action` row and the Action's log lines carry), and
        ``run_order`` its position within that phase. The internal context string ("pretest"/"posttest")
        keeps the host's lockdown/location logic intact. On success the outcome is "ok"; a raised Lua
        error becomes an "error" row carrying its first line, with the traceback logged at ERROR and
        both attributed to this action via ``action_seq`` so later packets can surface it. Returns
        ``(outcome, error)`` so the caller can abort on a Pre-test failure (packet 14b)."""
        started_at = _now_iso()
        # Packet 17b: a device failure from now until the action ends is owned by this action.
        # Its log lines (from here on in host.logs) are written with those failures as they drain.
        self._log_cursor = len(host.logs)
        self._device_owner = (time.monotonic(), {"action_seq": instance_seq})
        outcome = "ok"
        error = None
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
                if phase == "pre":
                    # A Pre-test failure aborts the run (packet 14b). Say so inside this action's
                    # window, so the line is owned by and recorded with the Action that failed.
                    host.record_log("ERROR", f"Pre-test Action failed ({error}); the run aborted so every step is NOTRUN.", source="ate")
        finally:
            ended_at = _now_iso()
            # Packet 17b: the last drain inside this action's window — it also writes the action's
            # remaining log lines (the ERROR above included) — then failures go to the run.
            self._drain_device_warnings()
            self._device_owner = None
        self._store.insert_action(
            run_uuid, instance_seq=instance_seq, phase=phase, run_order=run_order,
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
            if step.is_header:
                self._record_header(run_uuid, seq, step)
                continue
            default_enabled, enabled = self._enabled(seq, step)
            self._insert_node(
                run_uuid,
                seq,
                step,
                kind="step",
                step_number=step.step_no or "",
                step_name=step.name,
                default_enabled=default_enabled,
                enabled=enabled,
                result="NOTRUN",
                attempt_count=0,
            )
        # The explanation line itself is written inside the failed Action's window
        # (_record_phase_action), so every reader groups it with that Pre-test Action.

    def _tree_children(self) -> dict[int, list[int]]:
        """Each expanded-tree row's direct children, keyed by its node seq (packet 15d)."""
        if self._children is None:
            children: dict[int, list[int]] = {}
            for seq, node in enumerate(self._host.tree):
                if node.parent is not None:
                    children.setdefault(node.parent, []).append(seq)
            self._children = children
        return self._children

    def _enabled(self, seq: int, step: Any) -> tuple[int, int]:
        """Return ``(default_enabled, enabled)`` for a tree row at ``seq``.

        The default is the row's own :setEnable() after any enclosing header's :setEnable() has
        forced it (packet 15d, :attr:`_TreeNode.default_enabled`); an Operator override wins over
        that default for a Test Step. A header row is never toggled as a whole (the Run screen sets
        every step beneath it one by one), so its own `enabled` is whether any Test Step in its
        subtree is enabled."""
        default_enabled = bool(getattr(step, "default_enabled", getattr(step, "enabled", True)))
        if getattr(step, "is_header", False):
            children = self._tree_children()
            stack = list(children.get(seq, ()))
            saw_step = False
            while stack:
                child = stack.pop()
                node = self._host.tree[child]
                if node.is_header:
                    stack.extend(children.get(child, ()))
                    continue
                saw_step = True
                if self._enabled(child, node)[1]:
                    return default_enabled, 1
            # No Test Step beneath: nothing the Operator could switch, so it stays at its default
            # (the Run screen draws such a header checked).
            return default_enabled, (0 if saw_step else int(default_enabled))
        effective_enabled = self._overrides.get(seq, default_enabled)
        return int(default_enabled), int(effective_enabled)

    def _action_instances(self, host: "LuaHost") -> list[int]:
        """The schedule instance seqs whose Pre-/Post-test Actions run, in Pre-test order (packet 15e).

        :attr:`_LuaHost.instances` is already depth first — the top level, then each include in step
        order, each before its own includes — so this is that list minus every instance whose subtree
        holds Test Steps and every one of them is disabled: a Sub-schedule that runs no step has
        nothing to set up or tear down, so both of its Actions are skipped. Nested instances count, so
        an instance is skipped only when every step beneath it, down to the deepest include, is
        disabled. A schedule with no Test Steps at all keeps its Actions — an empty schedule is not a
        disabled one — and Operator enable overrides (packet 11a) count as much as the author's
        :setEnable()."""
        children: dict[int, list[int]] = {}
        for seq, instance in enumerate(host.instances):
            if instance.parent_instance is not None:
                children.setdefault(instance.parent_instance, []).append(seq)
        steps: dict[int, int] = {}
        live: dict[int, int] = {}
        for seq, node in enumerate(host.tree):
            if node.is_header:
                continue
            steps[node.instance] = steps.get(node.instance, 0) + 1
            if self._enabled(seq, node)[1]:
                live[node.instance] = live.get(node.instance, 0) + 1

        def subtree_steps(seq: int) -> int:
            return steps.get(seq, 0) + sum(subtree_steps(child) for child in children.get(seq, ()))

        def subtree_enabled(seq: int) -> int:
            return live.get(seq, 0) + sum(subtree_enabled(child) for child in children.get(seq, ()))

        return [seq for seq in range(len(host.instances)) if subtree_enabled(seq) or not subtree_steps(seq)]

    def _close_open_devices(self, host: "LuaHost", run_uuid: str) -> None:
        """Close every Device still open at run end and log one WARNING per device (packet 16a).

        Each leftover handle is closed defensively (its own teardown swallows errors), then a single
        ``WARNING`` line naming the device is recorded with source "ate" so it attaches to the run as
        a whole rather than to any step. An empty registry is a no-op, so runs that close their
        devices normally are unaffected."""
        registry = getattr(host, "_devices", None)
        if registry is None:
            return
        warnings: list[str] = []
        for device in registry.snapshot():
            try:
                registry.close_device(device)  # the registry's close, so 17c traces it; safe to repeat
            except Exception:  # noqa: BLE001 - teardown at run end must never abort the run
                pass
            if hasattr(device, "describe"):
                warnings.append(f"device {device.describe()} left open at end of run")
        self._store.insert_log_lines(
            run_uuid,
            0,
            [{"level": "WARNING", "source": "ate", "text": text} for text in warnings],
        )

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
            includes=self._includes,  # packet 15b: the release's Sub-schedule resolver
            receive_buffer_bytes=self._receive_buffer_bytes,  # packet 17a: the cap, per Device
            device_trace_enabled=self._device_trace_enabled,  # packet 17c: deviceTrace.enabled
            device_trace_max_bytes=self._device_trace_max_bytes,  # packet 17c: deviceTrace.maxBytes
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
        # Packet 15b: the expanded tree, in node order — every ordinary step, and every include
        # header immediately followed by the subtree of the Sub-schedule it includes. Each entry
        # reads like its step, so the runner below is unchanged apart from where it gets the list.
        steps = host.tree

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
                "schedule_description": schedule.description if schedule else None,
                "doc_title": schedule.doc_title if schedule else None,
                "doc_id": schedule.doc_id if schedule else None,
                "doc_rev": schedule.doc_rev if schedule else None,
                "media_number": self.media_number or None,
                "stop_on_fail": bool(self.stop_on_fail),
                "utc_offset": self.utc_offset,
            }
        )
        # Packet 06b, 07d: record every Media Release in this run's resolved chain, straight after the
        # test_run row — the owning release at seq 0, then its dependencies depth-first in declaration
        # order, a release reached twice listed once. Without a resolved definition there is nothing to
        # record, so bare schedule tests behave as before. Each row checksums ITS OWN .atedef; one that
        # has vanished or cannot be read since the scan still gets a row with a null checksum, one
        # WARNING, and the run carries on.
        if self._definition is not None:
            for member in _search_chain(self._definition):
                atedef_path = getattr(member, "atedef_path", None)
                atedef_sha = None
                if atedef_path is not None:
                    try:
                        atedef_sha = _sha256_hex(atedef_path.read_bytes())
                    except Exception as exc:  # any read failure: the row is still recorded, the run carries on
                        self._store.insert_log_lines(
                            run_uuid, 0,
                            [{"level": "WARNING", "source": "ate", "text": f"could not read .atedef for {member.media_number or member.name} ({atedef_path}): {exc}"}],
                        )
                self._store.insert_media_release(
                    run_uuid,
                    media_number=member.media_number or None,
                    name=member.name or None,
                    version=member.version or None,
                    atedef_sha256=atedef_sha,
                )
        # Packet 15b: one row for the top-level schedule and one per Sub-schedule instance, in tree
        # order (instance 0 is the top level), each naming the instance that included it and the
        # header node that introduced it. `keyvals` is stored as JSON text.
        for seq, instance in enumerate(host.instances):
            self._store.insert_schedule_instance(
                run_uuid,
                instance_seq=seq,
                parent_instance_seq=instance.parent_instance,
                header_node_seq=instance.header,
                schedule_name=instance.name,
                file=instance.file,
                media_number=instance.media_number,
                prefix=instance.prefix,
                keyvals=json.dumps(instance.keyvals),
            )
        # Report the run key and mark RUNNING on the main thread, both queued signals
        # delivered after ``start()`` returns; emitted from the worker before any other
        # row is written beyond this one.
        self.run_uuid_reported.emit(created["run_uuid"])
        # Packet 17b: from here on every ATE call drains device failures into the store at once.
        self._device_sink = host.device_warning_sink = self._make_device_sink(host, run_uuid)
        self.state_changed.emit(RunStatus.RUNNING)

        failed = False  # the run's standing result; a step flips pass_fail to FAIL on any FAIL/ERROR
        pass_fail = "PASS"  # the run's standing result; a step flips it to FAIL on any FAIL/ERROR
        aborted = False  # a whole-run abort requested between steps (see request_abort)

        # Pre-test Actions run up front, in order, each called with the current pass_fail. Nothing
        # has failed yet so this is "PASS"; 13b owns what happens if one fails (aborting). Each runs
        # before any step and records an action row plus its own log lines first.
        # Packet 15e: every schedule instance's Pre-test Action runs before the first Test Step, depth
        # first through the Sub-schedule tree (parent first, each include in step order) — that is
        # exactly :attr:`_LuaHost.instances` order, minus the instances whose whole subtree of Test
        # Steps is disabled (see :meth:`_action_instances`). `pre_started` keeps the instances whose
        # Pre-test phase was reached, so a Pre-test failure can skip the Post-test Actions of the
        # ones that never got there.
        pretest_failed = False  # a Pre-test Action that raised aborts the whole run (packet 14b)
        pre_started: list[int] = []
        run_order = 0
        # Packet 14d: the status bar shows "Pre-test n/total" as each Action starts, so announce
        # every one with its phase, position and the phase's total before it runs.
        pre_total = sum(len(self._phase_actions(host.instances[s].schedule, "pre")) for s in self._action_instances(host))
        for instance_seq in self._action_instances(host):
            pre_started.append(instance_seq)
            for action_fn in self._phase_actions(host.instances[instance_seq].schedule, "pre"):
                self.action_started.emit("pre", run_order + 1, pre_total)
                outcome, error = self._record_phase_action(host, run_uuid, instance_seq=instance_seq, phase="pre", fn=action_fn, pass_fail=pass_fail, run_order=run_order)
                run_order += 1
                if outcome == "error":
                    # A failing Pre-test Action aborts automatically before any step runs.
                    pretest_failed = True
                    self._record_pretest_failure(run_uuid, steps, failed_seq=instance_seq, error_text=error or "")
                    break
            if pretest_failed:
                break

        for seq, step in enumerate(steps):
            if pretest_failed:
                # Already recorded every step NOTRUN above; stop before running any.
                break

            # An include step is a header row: it records its place in the tree and never runs. Its
            # Sub-schedule was expanded at load (packet 15b), so its steps are separate nodes that
            # follow it in this same list.
            if step.is_header:
                self._record_header(run_uuid, seq, step)
                continue
            # A step disabled at run start (its :setEnable() default or an Operator override) isn't
            # executed; record it NOTRUN with no Attempt and move on. See :meth:`_enabled`.
            default_enabled, enabled = self._enabled(seq, step)
            if not enabled:
                self._insert_node(
                    run_uuid,
                    seq,
                    step,
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
            validated = getattr(step, "_validated_inputs", None) or []
            host._node_seq = seq
            host._step_inputs = list(validated)
            # Packet 12c: run the step's action, and whenever a Stop-on-Fail pause is answered RETRY
            # rerun it from the start as a new Attempt (no limit). Each Attempt runs against a fresh
            # OperatorInput window — op.reset() clears its supplied values and POLL clicks, and every
            # asked input is re-announced under the new Attempt's number — while Devices stay open
            # (the registry lives on the host, untouched by reset). One attempt row is recorded per
            # executed Attempt, carrying that Attempt's final report's values; the step's node row is
            # written once, after its last Attempt, with that Attempt's result.
            last_result = "NOREPORT"
            attempt_count = 0
            # 14d review: POLL is the only non-blocking type, so these are the indices whose
            # ``ATE.input(n)`` can wait. reset() below keeps any answer already given for them.
            blocking = [index - 1 for index, decl in enumerate(validated, start=1) if decl.type != "POLL"]
            while True:
                attempt_count += 1
                # Packet 14d: announce the Attempt so the status bar restarts its current-item timer
                # (a Retry is a new start of the same step, and gets its own timing).
                self.step_started.emit(seq)
                host._attempt_no = attempt_count  # ATE.input POLL reads its sticky state under this key
                # Ticket 10: put this Attempt's OperatorInput context in place before the action runs
                # so ATE.input(n) range-checks against THIS step's declarations, and announce each
                # asked input. The input_asked signals are queued here (before _run_action blocks), so
                # a blocking supply from another thread wakes get_raw while the worker waits; the
                # op.reset() below starts this Attempt's recording window clean so its values attach
                # to it (a retried Attempt re-asks under its own number, packet 12c). The `blocking`
                # argument carries in any answer the operator gave before this step's first
                # ATE.input(n) ran — without it that answer is wiped, the ask waits forever, and the
                # run hangs (14d review: the status-bar test's flake was this race).
                self._op.reset(blocking)
                # Packet 10a review: a blocking input opens only when its ``ATE.input(n)`` call
                # actually blocks — the OperatorInput's ``asked`` signal, wired to ``input_asked`` in
                # __init__ — while a POLL is live for the whole Attempt, so it is announced here,
                # before the action runs. A step that declares inputs its action never reads therefore
                # opens nothing and never keeps the run waiting. op.reset() above starts this Attempt's
                # recording window clean, so a retried Attempt re-asks under its own number (12c).
                self._ask_step = seq
                self._ask_attempt = attempt_count
                self._ask_inputs = list(validated)
                self._poll_open = {
                    (seq, attempt_count, index)
                    for index, decl in enumerate(validated, start=1)
                    if decl.type == "POLL"
                }
                for index, decl in enumerate(validated, start=1):
                    if decl.type == "POLL":
                        self.input_asked.emit(seq, attempt_count, index)
                start = len(host.reports)
                error_text: Optional[str] = None
                # Packet 17b: a device failure from now until the Attempt's action ends is owned by it,
                # and its log lines (from here on in host.logs) are written with them as they drain.
                self._log_cursor = len(host.logs)
                self._device_owner = (time.monotonic(), {"node_seq": seq, "attempt_no": attempt_count})
                try:
                    host._run_action(step.action, "step")  # runs the closure in Lua; may call ATE.report several times
                except Exception as exc:  # noqa: BLE001 - a Lua error becomes an ERROR Attempt
                    # The first line is the human-readable message (for a Lua-originated error it
                    # is just the message; file:line lives in the traceback portion). The full
                    # traceback — which carries the schedule's file:line — is logged at ERROR so
                    # criterion 03 can rebuild "<file>:<line>: ATE.<func>:" from it.
                    error_text = str(exc).splitlines()[0] if str(exc) else ""
                    # Packet 13c: an abort raised mid-step (the count hook, or a re-raised pcall/xpcall)
                    # ends the run here — record this running Attempt as ERROR and mark every later
                    # step NOTRUN instead of treating it as an ordinary failure. The sentinel is
                    # exactly the string "aborted" that the hook raises; a genuine error whose first
                    # line merely contains it is left to fail normally below. A retry in flight
                    # records its ERROR at the in-flight Attempt's number (packet 12c).
                    if error_text.strip() == "aborted":
                        aborted = True
                        self._record_abort(run_uuid, steps, seq, attempt_no=attempt_count)
                        break
                    host.record_log("ERROR", str(exc))
                finally:
                    # Packet 17b: the last drain inside this Attempt's window (node_seq/attempt_no),
                    # which also writes the Attempt's remaining log lines (the ERROR above included,
                    # in emission order with any device warning); after it, failures go to the run level. The finally also covers a mid-step
                    # abort, so a failure during a raised action stays with the running Attempt.
                    self._drain_device_warnings()
                    self._device_owner = None

                # One attempt row per executed Attempt, carrying its final report's values — every
                # Attempt is recorded (spec rule), and the step's result will be its last Attempt's.
                new_reports = host.reports[start:]
                if error_text is not None:
                    self._store.insert_attempt(
                        run_uuid, seq, attempt_count, pass_fail="ERROR", msg=error_text
                    )
                    last_result = "ERROR"  # the Attempt raised; reflect it on the node
                elif new_reports:
                    report_pass_fail, report_msg, report_actual, report_expected = new_reports[-1]
                    self._store.insert_attempt(
                        run_uuid,
                        seq,
                        attempt_count,
                        pass_fail=report_pass_fail,
                        msg=report_msg,
                        actual=report_actual,
                        expected=report_expected,
                    )
                    last_result = report_pass_fail
                else:
                    # Ran but never reported -> NOREPORT (logged as a warning below).
                    self._store.insert_attempt(
                        run_uuid, seq, attempt_count, pass_fail="NOREPORT", msg=None
                    )
                    last_result = "NOREPORT"

                # The Attempt's own log lines are already written (drained above, in emission order);
                # an Attempt that never reported adds its own warning (source "ate") after them.
                if not new_reports:
                    self._store.insert_log_lines(
                        run_uuid,
                        seq,
                        [{"level": "WARNING", "source": "ate", "text": f"step '{step.name}' never reported"}],
                        node_seq=seq,
                        attempt_no=attempt_count,
                    )

                # Ticket 10: wait for every announced input to be answered before recording this
                # Attempt's values. Blocking supplies already woke the worker via get_raw; POLL clicks
                # never block — so without this wait a worker could record (and drop an unanswered
                # click) before the operator's answer was delivered on another thread. A blocking
                # supply or POLL click decrements the outstanding count when it arrives, waking this
                # wait. Each Attempt waits on its own re-announced inputs, so a retry's supplies land
                # on its own rows (packet 12c).
                self._op.wait_for_asks()
                # Ticket 10: record every input value this Attempt asked for. Supplies and POLL clicks
                # the OperatorInput controller recorded during the action are typed from this step's
                # declarations and written as input_value rows; the declarations themselves are stored
                # on the node (as JSON, in schedule format via StepInput.to_dict) so the GUI can render
                # each input under its step at all times.
                records = self._record_inputs(seq, validated, attempt_count)
                if records:
                    self._store.insert_input_values(run_uuid, records)

                # Stop on Fail: an Attempt that ends FAIL or ERROR pauses until the operator chooses
                # (packet 12b). Packet 29a review: "FAIL or ERROR" includes a step that REPORTS
                # Result.ERROR — an Attempt recorded as ERROR pauses the same way a raised Action does,
                # which the old `error_text is not None` test only covered for Lua errors. The
                # Attempt is already recorded above; once choose() returns, _pause_for_operator has
                # stamped its prompt_choice. RETRY reruns the action from the start as the next
                # Attempt, with no limit (packet 12c); CONTINUE keeps this Attempt's result and moves
                # on; ABORT stops cleanly (remaining steps NOTRUN, End Reason STOPPED_ON_FAIL / result
                # FAIL). Continue on fail (stop_on_fail False) never pauses.
                if last_result in ("FAIL", "ERROR") and self.stop_on_fail:
                    choice = self._pause_for_operator(run_uuid, seq, attempt_count)
                    if choice == "RETRY":
                        continue  # rerun this step's action as a new Attempt from the start
                    if choice == "ABORT":
                        self._stopped_on_fail = True
                        self._record_abort(run_uuid, steps, seq, record_attempt=False)
                        break
                break

            # Packet 10a review: this step ran its last Attempt, so nothing may answer its POLL any
            # more — the layout closes its widget on this signal.
            self._poll_open = {key for key in self._poll_open if key[0] != seq}
            self.step_finished.emit(seq)

            # The step's node row is written once, after its last Attempt, carrying that Attempt's
            # result and the number of Attempts executed (packet 12c). The mid-step abort path above
            # already recorded its own ERROR node and attempt, so it is skipped here.
            if not aborted:
                default_enabled, enabled = self._enabled(seq, step)
                self._insert_node(
                    run_uuid,
                    seq,
                    step,
                    kind="step",
                    step_number=step.step_no or "",
                    step_name=step.name,
                    default_enabled=default_enabled,
                    enabled=enabled,
                    result=last_result,
                    attempt_count=attempt_count,
                    inputs=[d.to_dict() for d in validated] if validated else None,
                )
                # The step's result is its last Attempt's (packet 12c): a step that passed on retry
                # no longer fails the run, so the standing result flips only when the last Attempt
                # was FAIL or ERROR (a raised action recorded as an ERROR attempt).
                if last_result == "FAIL" or last_result == "ERROR":
                    failed = True
                    pass_fail = "FAIL"  # flip the standing result so Post-test Actions see it
            if aborted or self._stopped_on_fail:
                break

        # Post-test Actions always run after the last step, in reverse, each called with the final
        # pass_fail. Before they start we clear the abort flag (packet 14c) so a previously-aborted
        # run can still tear down; but if it had already been aborted we remember that here so its
        # End Reason stays ABORTED even though the flag is now cleared. A Post-test Action that
        # raised fails the run without changing this reason (packet 14c); a second abort during one
        # ends only that action and leaves the rest to run.
        posttest_aborted = self.aborted
        # The Post-test Actions get the run's overall result (spec: `fn(passFail)`): a Pre-test
        # failure, a stop or an abort has already made it FAIL, whatever the steps did.
        if pretest_failed or self._stopped_on_fail or posttest_aborted or aborted:
            pass_fail = "FAIL"
        # Packet 15e: one Post-test Action per schedule instance, in exactly the reverse of the
        # Pre-test order above — an instance reached in the Pre-test pass gets its teardown even
        # when its own Pre-test Action failed, and one the pass never got to gets none.
        run_order = 0
        post_total = sum(len(self._phase_actions(host.instances[s].schedule, "post")) for s in pre_started)
        for instance_seq in reversed(pre_started):
            for action_fn in self._phase_actions(host.instances[instance_seq].schedule, "post"):
                # Clear before every action, not just the first: a second abort ends only the action it
                # lands in, so the flag must not carry over and kill the next one on its first hook tick.
                self._abort_flag.clear()
                self.action_started.emit("post", run_order + 1, post_total)
                outcome, error = self._record_phase_action(host, run_uuid, instance_seq=instance_seq, phase="post", fn=action_fn, pass_fail=pass_fail, run_order=run_order)
                run_order += 1
                if outcome == "aborted":
                    posttest_aborted = True
                elif outcome == "error":
                    # A Post-test Action that raised fails the run (result FAIL) but keeps its End Reason.
                    failed = True
                    pass_fail = "FAIL"

        # Packet 17b: drain any device failure warning no action's window claimed (a failure in the
        # gap between the last action and run end) — it attaches to the run as a whole.
        self._drain_device_warnings()
        # Packet 16a: close every Device still open at run end (including an aborted or errored run)
        # and log one WARNING per device naming it — source "ate", so the run engine records them as
        # run-level lines with no owning step. Done before the result is computed so a leftover
        # device's socket is released even when the run fails; sockets are closed defensively in case
        # teardown runs after an abort.
        self._close_open_devices(host, run_uuid)
        # Packet 17b: once more after the close, so a failure that raced the teardown isn't lost.
        self._drain_device_warnings()
        host.device_warning_sink = self._device_sink = None

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
        """Record one ``run_file`` row per file used this run and warn on mismatches (packets 06b, 06d, 07b).

        Iterates the host's load-order list of ``(path, kind, sha256)`` entries; each is matched back
        to its Test Definition entry by absolute path so we can carry the definition-listed ``rel_path``,
        owning media number and expected checksum. The match walks the run's whole resolved chain, so a
        module loaded from a dependency release is recorded against that release: its own media number,
        its rel path in that release, and that release's expected checksum. A file that loaded but isn't
        listed has no expected value to compare against, so it is skipped rather than recorded with a
        null expectation — except a bundled ``ate.`` module, which is never in the definition by design:
        it is recorded under its module name, with no media number and no expectation, and can therefore
        never mismatch."""
        if self._definition is None:
            return
        by_path = {}
        for member in _search_chain(self._definition):
            for entry in member.files:
                by_path.setdefault(member.path / entry.rel_path, (entry, member))
        mismatches: list[tuple[str, Optional[str], str]] = []
        for path, kind, computed in getattr(host, "_files_used", []):
            if kind == "ate-module":
                self._store.insert_run_file(
                    run_uuid,
                    path=path,  # the `ate.` module name, not a path on disk
                    media_number=None,
                    kind=kind,
                    expected_sha256=None,
                    computed_sha256=computed,
                    mismatch=False,
                )
                continue
            found = by_path.get(Path(path))
            if found is None:
                continue
            entry, owner = found
            mismatch = (entry.expected_sha256 or None) != computed
            # Packet 06c: only a file that mismatched, and only when this run was told to store them,
            # carries the bytes it was checksummed from — never a re-read of the file.
            content = None
            if mismatch and self._store_mismatched_files:
                content = getattr(host, "_file_bytes", {}).get(str(Path(path)))
            self._store.insert_run_file(
                run_uuid,
                path=entry.rel_path,
                media_number=owner.media_number or None,
                kind=kind,
                expected_sha256=entry.expected_sha256 or None,
                computed_sha256=computed,
                mismatch=mismatch,
                content=content,
            )
            if mismatch:
                mismatches.append((entry.rel_path, entry.expected_sha256, computed, content is not None))
        for rel_path, expected, computed, stored in mismatches:
            note = " (file stored with this Test Run)" if stored else ""
            self._store.insert_log_lines(
                run_uuid, 0,
                [{"level": "WARNING", "source": "ate", "text": f"checksum mismatch for {rel_path} (expected {expected}, got {computed}){note}"}],
            )
