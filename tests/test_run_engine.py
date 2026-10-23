"""Drive the run engine through an offscreen Qt loop and assert recorded rows.

Ticket 02's flow-control slice: a schedule runs on one QThread worker, each step records
one Attempt per ATE.report call (last-call-wins), ERROR for a raised action, and the run
result is FAIL on any FAIL/ERROR — PASS only when every step passed. Verified against a
real SQLite file with real .tslua schedules; no mocks.
"""

import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QCoreApplication

from ate.engine.run_engine import RunEngine, RunStatus
from ate.engine.operator_input import OperatorInput
from ate.storage._uuid import new_uuid7
from ate.storage.store import RunStore

from ._fixtures import ABORTABLE, ERRORING, FAILING, NEVER_REPORTS, PASSING, _drive


def _run_schedule(app: QCoreApplication, source: str) -> tuple[RunStore, dict]:
    """Create a fresh store + engine for one schedule on disk; return (store, run_row)."""
    captured = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8")
        engine = RunEngine(store, str(schedule_path))
        engine.run_uuid_reported.connect(_capture)
        _drive(engine, app)

    assert captured.get("run_uuid") is not None, "engine never reported a run key"
    return store, store.get_test_run(captured["run_uuid"])


def test_passing_schedule_records_pass(app: QCoreApplication) -> None:
    """A step that reports PASS yields a PASS run with one PASS attempt."""
    store, row = _run_schedule(app, PASSING)

    assert row is not None
    assert row["result"] == "PASS"
    assert row["end_reason"] == "COMPLETED"
    assert row["started_at"] is not None and row["ended_at"] is not None

    conn = store._conn
    conn.row_factory = sqlite3.Row
    node = conn.execute(
        "SELECT step_name, result, attempt_count FROM node WHERE run_uuid=?", (row["run_uuid"],)
    ).fetchone()
    assert node["step_name"] == "LED colour"
    assert node["result"] == "PASS"
    assert node["attempt_count"] == 1

    attempt = conn.execute(
        "SELECT pass_fail, msg FROM attempt WHERE run_uuid=?", (row["run_uuid"],)
    ).fetchone()
    assert attempt["pass_fail"] == "PASS"


def test_failing_schedule_records_fail_and_fails_run(app: QCoreApplication) -> None:
    """A step that reports FAIL fails the whole run — not just an exception."""
    store, row = _run_schedule(app, FAILING)

    assert row is not None
    # The reported FAIL must fail the run; this was a real bug before (reported FAIL -> PASS).
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "COMPLETED"

    conn = store._conn
    conn.row_factory = sqlite3.Row
    node = conn.execute(
        "SELECT result FROM node WHERE run_uuid=?", (row["run_uuid"],)
    ).fetchone()
    assert node["result"] == "FAIL"


def test_raised_action_becomes_error_attempt_and_fails_run(app: QCoreApplication) -> None:
    """A step that raises records an ERROR attempt and fails the run; later steps still run."""
    store, row = _run_schedule(app, ERRORING)

    assert row is not None
    assert row["result"] == "FAIL"  # FAIL on any FAIL/ERROR

    conn = store._conn
    conn.row_factory = sqlite3.Row
    nodes = conn.execute(
        "SELECT step_number, result FROM node WHERE run_uuid=? ORDER BY node_seq", (row["run_uuid"],)
    ).fetchall()
    by_step = {n["step_number"]: n for n in nodes}
    assert set(by_step) == {"1.", "2."}  # both steps expanded despite the first raising

    boom = by_step["1."]
    assert boom["result"] == "ERROR"
    # The erroring step ("boom") is the first expanded step, so it sits at node_seq 0.
    err_attempt = conn.execute(
        "SELECT pass_fail, msg FROM attempt WHERE run_uuid=? AND node_seq=0", (row["run_uuid"],)
    ).fetchone()
    assert err_attempt["pass_fail"] == "ERROR"
    assert "kaboom from Lua" in err_attempt["msg"]

    never = by_step["2."]
    assert never["result"] == "PASS"  # execution continued past the error


def test_raised_action_logs_full_traceback_at_error(app: QCoreApplication) -> None:
    """Criterion 02: a raised action records ERROR with its first line as the message and logs
    the full traceback (with the schedule's file:line in its stack portion) at ERROR."""
    store, row = _run_schedule(app, ERRORING)

    conn = store._conn
    conn.row_factory = sqlite3.Row
    err_attempt = conn.execute(
        "SELECT pass_fail, msg FROM attempt WHERE run_uuid=? AND node_seq=0", (row["run_uuid"],)
    ).fetchone()
    assert err_attempt["pass_fail"] == "ERROR"
    # The first line of the traceback is recorded verbatim as the message — for a Lua error
    # that already carries "<file>:<line>: <msg>", matching the ATE error format.
    assert err_attempt["msg"].startswith("schedule.tslua:")
    assert "kaboom from Lua" in err_attempt["msg"]

    # The full traceback (with its stack portion) is logged at ERROR, not just the first line.
    error_lines = [ln for ln in store.log_lines(row["run_uuid"]) if ln["level"] == "ERROR"]
    assert len(error_lines) >= 1
    tb = "\n".join(ln["text"] for ln in error_lines)
    assert "kaboom from Lua" in tb
    assert "stack traceback" in tb


def test_step_that_never_reports_records_noreport_and_warns(app: QCoreApplication) -> None:
    """Criterion 04 — a step that runs its action but never calls ATE.report records a NOREPORT
    attempt and logs one warning, so the forgotten report is visible."""
    store, row = _run_schedule(app, NEVER_REPORTS)

    assert row is not None
    # The run still completes; a NOREPORT step does not fail it (spec rule 91: only FAIL/ERROR do).
    assert row["end_reason"] == "COMPLETED"

    conn = store._conn
    conn.row_factory = sqlite3.Row
    node = conn.execute(
        "SELECT result, attempt_count FROM node WHERE run_uuid=? AND step_number='1.'", (row["run_uuid"],)
    ).fetchone()
    assert node is not None
    # The only attempt recorded for the silent step is NOREPORT.
    assert node["result"] == "NOREPORT"
    assert node["attempt_count"] == 1

    err_attempt = conn.execute(
        "SELECT pass_fail, msg FROM attempt WHERE run_uuid=? AND node_seq=0", (row["run_uuid"],)
    ).fetchone()
    assert err_attempt is not None
    assert err_attempt["pass_fail"] == "NOREPORT"

    # Ate-sourced warning that the step never reported.
    warnings = [ln for ln in store.log_lines(row["run_uuid"]) if ln["level"] == "WARNING"]
    assert len(warnings) >= 1
    joined = "\n".join(ln["text"] for ln in warnings)
    assert "never reported" in joined


def test_run_key_is_uuidv7(app: QCoreApplication) -> None:
    """The generated run key is a canonical uppercase UUIDv7 (version nibble 7)."""
    store, row = _run_schedule(app, PASSING)
    assert len(row["run_uuid"]) == 36
    assert row["run_uuid"][14] == "7"


def _abort_between_steps(app: QCoreApplication, source: str) -> tuple[RunStore, dict]:
    """Drive ``source`` on a fresh engine and request a whole-run abort between steps.

    The ABORTABLE schedule's first step blocks on an INTEGER input; while it waits the driver
    supplies its value (so that step completes cleanly) and then requests an abort. The engine
    therefore stops at the next boundary — recording the blocked step PASS, the following step as
    ERROR "Aborted by Operator", later steps NOTRUN, and closing ABORTED / FAIL.

    Driving is a concurrent pump loop rather than a bare ``QThread.wait``: the worker's queued
    signals (input_asked -> supply/abort) only fire when the application event loop is pumped, and
    that pumping races with the worker's own step-2 boundary check under a plain wait. Pumping in
    lock-step with the worker — as :func:`_pump_until_finished` does for Ticket 10 — delivers every
    queued slot (including FINISHED) deterministically before we inspect rows."""
    captured = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8")
        op = OperatorInput()
        engine = RunEngine(store, str(schedule_path), operator_input=op)
        engine.run_uuid_reported.connect(_capture)

        finished = threading.Event()
        engine.state_changed.connect(lambda status: finished.set() if status == RunStatus.FINISHED else None)

        def _on_ask(seq: int, attempt_no: int, index: int) -> None:
            op.supply_input(index - 1, 5)  # answer step 1's input so it completes cleanly
            engine.request_abort()  # then stop between steps before step 2 runs

        engine.input_asked.connect(_on_ask)
        engine.start()
        deadline = time.time() + 10.0
        while not finished.is_set():
            app.processEvents()
            if time.time() > deadline:
                raise TimeoutError("engine did not reach FINISHED within 10 s")

    return store, store.get_test_run(captured["run_uuid"])


def test_whole_run_abort_between_steps_records_error_notrun_and_aborted(app: QCoreApplication) -> None:
    """Packet 13a checkbox 2: aborting between steps records the running step ERROR 'Aborted by
    Operator', remaining steps NOTRUN, End Reason ABORTED and result FAIL."""
    store, row = _abort_between_steps(app, ABORTABLE)

    assert row is not None
    # A whole-run abort fails the run with End Reason ABORTED.
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "ABORTED"

    conn = store._conn
    conn.row_factory = sqlite3.Row
    nodes = {n["step_number"]: dict(n) for n in conn.execute(
        "SELECT step_number, result, attempt_count FROM node WHERE run_uuid=? ORDER BY node_seq", (row["run_uuid"],)
    ).fetchall()}
    # Step 1 ran and passed; step 2 was the running step at abort time -> ERROR; step 3 never ran.
    assert nodes == {
        "1.": {"step_number": "1.", "result": "PASS", "attempt_count": 1},
        "2.": {"step_number": "2.", "result": "ERROR", "attempt_count": 1},
        "3.": {"step_number": "3.", "result": "NOTRUN", "attempt_count": 0},
    }

    # The running step's single attempt carries the operator-abort message.
    err = conn.execute(
        "SELECT pass_fail, msg FROM attempt WHERE run_uuid=? AND node_seq=1", (row["run_uuid"],)
    ).fetchone()
    assert err is not None and err["pass_fail"] == "ERROR"
    assert err["msg"] == "Aborted by Operator"
