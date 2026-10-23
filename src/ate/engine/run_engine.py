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
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from PyQt6.QtCore import QThread, pyqtSignal

from ..errors import ATEError
from ..storage._uuid import new_uuid7
from ..storage.store import RunStore, _now_iso
from .lua_host import LuaHost
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


class RunEngine(QThread):
    """Runs one Test Schedule on a worker thread and records it through the store."""

    run_uuid_reported = pyqtSignal(str)  # the freshly generated run key, on start
    state_changed = pyqtSignal(RunStatus)  # IDLE -> RUNNING -> FINISHED, main thread
    input_asked = pyqtSignal(int, int, int)  # (node_seq, attempt_no, index): a step is asking for its n-th input

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
        # Optional pre-registered ATE functions (a testability seam; see criterion 03). When
        # provided they are registered on the host before it runs, so a failing one can be
        # exercised through real Lua execution without touching device drivers.
        self._ate_fns = dict(ate_fns or {})
        # The OperatorInput controller this run's ATE.input calls read/write through; injected by
        # tests (so they own supply/click) or made fresh when running for real. See ticket 10.
        self._op = operator_input or OperatorInput()

    @property
    def operator_input(self) -> OperatorInput:
        """The :class:`OperatorInput` controller this run's ``ATE.input`` calls read/write through.

        Owned by the engine (a fresh one per real run); tests and the GUI drive it directly so a
        blocking supply or POLL click reaches the worker while it waits, without importing widgets."""
        return self._op

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

    def run(self) -> None:
        """Execute the schedule and record every step's Attempt through the store."""
        # A fresh LuaRuntime per load (see lua_host); real execution is stubbed there. The
        # OperatorInput controller this run owns is injected so its ATE.input calls read/write the
        # same blocking waits and POLL state the GUI drives through supply_input/click_poll.
        host = LuaHost(self._schedule_path, operator_input=self._op)
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
        # Report the run key and mark RUNNING on the main thread, both queued signals
        # delivered after ``start()`` returns; emitted from the worker before any other
        # row is written beyond this one.
        self.run_uuid_reported.emit(created["run_uuid"])
        self.state_changed.emit(RunStatus.RUNNING)

        failed = False
        for seq, step in enumerate(steps):
            # Header/include rows have no action; only ordinary steps run.
            if step.action is None:
                continue

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

            self._store.insert_node(
                run_uuid,
                seq,
                kind="step",
                step_number=step.step_no or "",
                step_name=step.name,
                default_enabled=int(bool(step.enabled)),
                enabled=int(bool(step.enabled)),
                result=last_result,
                attempt_count=attempts_recorded,
                inputs=[d.to_dict() for d in validated] if validated else None,
            )

        # FAIL on any FAIL/ERROR; otherwise PASS. End Reason COMPLETED for this ticket.
        run_result = "FAIL" if failed else "PASS"
        self._store.update_run_end(run_uuid, result=run_result, end_reason="COMPLETED")
        # FINISHED is queued to the main thread; QThread's own ``finished`` signal also
        # fires when run() returns. Observers use state_changed for the observable states.
        self.state_changed.emit(RunStatus.FINISHED)
