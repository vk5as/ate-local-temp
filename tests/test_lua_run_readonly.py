"""Ticket 09 criterion c — ``ATE.run`` is a read-only metadata table.

The run engine publishes this run's identifying metadata (UUT serial, Operator, Station, Project,
Test Schedule name, Media Release media number, name and version) into ``ATE.run`` before steps
run, so a schedule can READ the current run's context. The table is read-only: writing any field —
whether an already-seeded one or a new one — raises ``ATE.run: read-only`` instead of mutating it.

Verified against a real lupa runtime and end-to-end through the QThread run engine — no mocks."""

import sqlite3
import tempfile
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication
from lupa import LuaError

from ate.engine.lua_host import ATE_RUN_READONLY, _RUN_FIELDS, LuaHost
from ate.engine.run_engine import RunEngine
from ate.storage.store import RunStore

from ._fixtures import READS_RUN, WRITES_RUN, _drive


def _make_host(source: str) -> LuaHost:
    """Construct a host for one schedule on disk and return it."""
    with tempfile.TemporaryDirectory() as td:
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8", newline="\n")
        return LuaHost(str(schedule_path))


def _run_schedule(app: QCoreApplication, source: str, **kwargs) -> tuple[RunStore, dict]:
    """Create a fresh store + engine for one schedule on disk; return (store, run_row)."""
    captured = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8", newline="\n")
        engine = RunEngine(store, str(schedule_path), **kwargs)
        engine.run_uuid_reported.connect(_capture)
        _drive(engine, app)

    assert captured.get("run_uuid") is not None, "engine never reported a run key"
    return store, store.get_test_run(captured["run_uuid"])


def test_ate_run_is_a_table_with_all_fields() -> None:
    """ATE.run is a table exposing exactly the eight run fields. Before any context is published
    every field reads back as nil; after publishing, each reads its value."""
    host = _make_host(READS_RUN)
    ate = host.ate

    # It is a real Lua table (lupa's table proxy), and all eight fields are present but nil before
    # publish. Reads through the proxy prove it behaves as a table, not a function.
    run = ate.run
    assert type(run).__name__ == "_LuaTable", "ATE.run must be a Lua table"
    for field in _RUN_FIELDS:
        assert host._runtime.execute(f"return ATE.run.{field}") is None

    # Publishing seeds the fields from run context; reads then return them.
    ate.publish_run(
        {
            "UUT_serial": "SN-123",
            "Operator": "Alice",
            "Station": "BENCH-03",
            "Project": "Demo",
            "Test_Schedule_Name": "Reads run schedule",
            "Media_Release_Media_Number": "REL-A",
            "name": "Reads run schedule",
            "version": "1.0.0",
        }
    )
    for field, expected in zip(_RUN_FIELDS, [
        "SN-123", "Alice", "BENCH-03", "Demo",
        "Reads run schedule", "REL-A", "Reads run schedule", "1.0.0",
    ]):
        assert host._runtime.execute(f"return ATE.run.{field}") == expected


def test_reading_ate_run_works_via_proxy_and_lua(app: QCoreApplication) -> None:
    """Reading works from both sides of the seam and reflects published context: through the
    Python proxy (``ate.run["Operator"]``) and through real Lua execution (``ATE.run.Operator``)."""
    host = _make_host(READS_RUN)
    ate = host.ate

    ate.publish_run({"Operator": "Alice", "Station": "BENCH-03"})

    # Python-side proxy read returns the published value.
    assert ate.run["Operator"] == "Alice"
    # Lua-side read (how a schedule actually reads it) returns the same value.
    assert host._runtime.execute("return ATE.run.Operator") == "Alice"
    assert host._runtime.execute("return ATE.run.Station") == "BENCH-03"

    # Re-publishing updates what is read; an end-to-end run records the new operator.
    ate.publish_run({"Operator": "Bob"})
    store, row = _run_schedule(app, READS_RUN, operator="Bob")
    conn = store._conn
    conn.row_factory = sqlite3.Row
    attempt = conn.execute(
        "SELECT msg FROM attempt WHERE run_uuid=? AND node_seq=0", (row["run_uuid"],)
    ).fetchone()
    assert "operator is Bob" in attempt["msg"]


def test_writing_an_existing_field_raises_read_only() -> None:
    """Criterion c — writing an already-seeded field raises ``ATE.run: read-only``. A schedule
    mutates the table with a Lua assignment, so the write is exercised through one; it must raise
    cleanly (not corrupt state)."""
    host = _make_host(READS_RUN)
    ate = host.ate

    ate.publish_run({"Operator": "Alice"})

    raised: Exception | None = None
    try:
        # Exactly how a schedule would attempt to mutate it — through the ATE table in Lua.
        host._runtime.execute("ATE.run.Operator = 'injected'", name="@write")
    except LuaError as exc:
        raised = exc

    assert raised is not None, "writing an existing field must raise"
    # The message carries the settled "<file>:<line>: ATE.run:" form (see criterion d).
    assert ATE_RUN_READONLY in str(raised)


def test_writing_a_new_field_raises_read_only() -> None:
    """Criterion c — writing a brand-new field also raises ``ATE.run: read-only`` (not just the
    seeded ones); the table cannot gain fields either."""
    host = _make_host(READS_RUN)
    ate = host.ate

    ate.publish_run({"Operator": "Alice"})

    raised: Exception | None = None
    try:
        host._runtime.execute("ATE.run.Project = 'injected'", name="@write")
    except LuaError as exc:
        raised = exc

    assert raised is not None, "writing a new field must raise"
    # A brand-new field still trips the read-only guard.
    assert ATE_RUN_READONLY in str(raised)


def test_ate_run_write_does_not_corrupt_state(app: QCoreApplication) -> None:
    """A failed write leaves the table untouched and leaks no globals. Reads still return the
    original values after an attempted mutation."""
    host = _make_host(READS_RUN)
    ate = host.ate

    ate.publish_run({"Operator": "Alice"})
    try:
        # An attempted write must raise; we only need to know it did.
        host._runtime.execute("ATE.run.Operator = 'injected'", name="@write")
    except LuaError:
        pass

    assert host._runtime.execute("return ATE.run.Operator") == "Alice"
    # The private values table must not leak into the schedule's global scope.
    assert host._runtime.execute("return _G._ATE_RUN_VALS") is None


def test_step_reading_ate_run_operator_is_recorded(app: QCoreApplication) -> None:
    """End-to-end — a step that reads ATE.run.Operator during a real run records the operator in
    its report. The run passes."""
    store, row = _run_schedule(app, READS_RUN, operator="Alice", station_name="BENCH-03")

    assert row is not None and row["result"] == "PASS"

    conn = store._conn
    conn.row_factory = sqlite3.Row
    attempt = conn.execute(
        "SELECT pass_fail, msg FROM attempt WHERE run_uuid=? AND node_seq=0", (row["run_uuid"],)
    ).fetchone()
    assert attempt["pass_fail"] == "PASS"
    # The value came from ATE.run, populated by the run engine from this run's operator.
    assert "operator is Alice" in attempt["msg"]


def test_step_writing_ate_run_raises_error_and_fails_run(app: QCoreApplication) -> None:
    """End-to-end — a step that writes to ATE.run raises ``ATE.run: read-only``, which the run
    engine records as an ERROR attempt and reflects on the node; the run fails."""
    store, row = _run_schedule(app, WRITES_RUN)

    assert row is not None and row["result"] == "FAIL"  # FAIL on any ERROR

    conn = store._conn
    conn.row_factory = sqlite3.Row
    node = conn.execute(
        "SELECT result FROM node WHERE run_uuid=? AND step_number='1.'", (row["run_uuid"],)
    ).fetchone()
    assert node is not None and node["result"] == "ERROR"

    error_attempt = conn.execute(
        "SELECT pass_fail, msg FROM attempt WHERE run_uuid=?", (row["run_uuid"],)
    ).fetchone()
    assert error_attempt is not None and error_attempt["pass_fail"] == "ERROR"
    # The message carries the settled "<file>:<line>: ATE.run:" form (see criterion d).
    assert ATE_RUN_READONLY in error_attempt["msg"]
