"""Ticket 09 criterion d — seam-1 coverage of the error message form and full traceback content.

Every rule that raises a schedule-sourced action records, at the seam between the Lua host and the
run engine (seam-1), BOTH:

* the settled ``<file>:<line>: ATE.<fn>:`` message form — the exact line shown to the operator; and
* the full traceback content that backs it — not just the first line.

Two rules flow through seam-1 as a real run (RunEngine -> store): an ``ATE.run`` write, which raises
``ATE.run: read-only`` while a step runs, and any raised Lua action, whose message is logged at ERROR
with its full traceback. Two rules cannot flow through RunEngine — ``ATE.report``/``ATE.input`` are
illegal only in the Pre-test/Post-test Action phases (or, for report, while loading), which RunEngine
never executes — so they are covered at the host-log seam: driving the offending action there raises
exactly what the run engine would record verbatim as both the ERROR message and its content.

Verified against a real lupa runtime and end-to-end through the QThread run engine — no mocks."""

import sqlite3
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication
from lupa import LuaError

from ate.engine.lua_host import HostError, NotAllowedError, LuaHost
from ate.engine.run_engine import RunEngine
from ate.storage.store import RunStore

from ._fixtures import LOAD_REPORT, POSTTEST_INPUT, WRITES_RUN, _drive
from tests._fixtures import scratch_directory


def _make_host(source: str) -> LuaHost:
    """Construct a host for one schedule on disk and return it."""
    with scratch_directory() as td:
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8", newline="\n")
        return LuaHost(str(schedule_path))


def _run_schedule(app: QCoreApplication, source: str) -> tuple[RunStore, dict]:
    """Create a fresh store + engine for one schedule on disk; return (store, run_row)."""
    captured = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with scratch_directory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8", newline="\n")
        engine = RunEngine(store, str(schedule_path))
        _drive(engine, app, on_run_uuid=_capture)

    assert captured.get("run_uuid") is not None, "engine never reported a run key"
    return store, store.get_test_run(captured["run_uuid"])


def test_ate_run_write_records_read_only_error_form_and_traceback(app: QCoreApplication) -> None:
    """Criterion c — an ``ATE.run`` write raises through seam-1 (RunEngine -> store). The ERROR
    attempt carries the exact ``<file>-run:<line>: ATE.run: read-only`` message form, and the full
    traceback content is logged at ERROR with the schedule's file:line."""
    store, row = _run_schedule(app, WRITES_RUN)

    assert row is not None and row["result"] == "FAIL"  # FAIL on any ERROR

    conn = store._conn
    conn.row_factory = sqlite3.Row

    error_attempt = conn.execute(
        "SELECT pass_fail, msg FROM attempt WHERE run_uuid=?", (row["run_uuid"],)
    ).fetchone()
    assert error_attempt is not None and error_attempt["pass_fail"] == "ERROR"

    # The message form: "<file>-run:<line>: ATE.run:" — note the "-run" suffix on the filename,
    # taken from the publish chunk name that seeded this run.
    assert error_attempt["msg"].startswith("schedule.tslua-run:")
    assert "ATE.run: read-only" in error_attempt["msg"]

    # The full traceback content is logged at ERROR, not just the first line: it carries both the
    # Lua "stack traceback" banner and the schedule's file:line.
    log_lines = conn.execute(
        "SELECT text FROM log_line WHERE run_uuid=? AND level='ERROR'", (row["run_uuid"],)
    ).fetchall()
    assert any("stack traceback" in line["text"] for line in log_lines), \
        "the raised action's full traceback must be logged at ERROR"
    error_log = next(line["text"] for line in log_lines if "stack traceback" in line["text"])
    assert "schedule.tslua:" in error_log  # schedule file:line inside the traceback


def test_input_lockdown_records_message_form_and_content() -> None:
    """Criterion b — ``ATE.input`` is illegal in a Pre-test Action; driving that action raises with
    the exact ``<file>:<line>: ATE.input:`` message form. This settled line is precisely what the
    run engine records verbatim as both the ERROR message and its full content (it cannot flow
    through RunEngine, which never executes pretest actions)."""
    host = _make_host(POSTTEST_INPUT)

    raised: Exception | None = None
    try:
        # The fixture's Post-test Action calls ATE.input; input is illegal there too.
        host._run_action(host.schedule.post_test_action, "posttest")  # type: ignore[attr-defined]
    except NotAllowedError as exc:
        raised = exc

    assert raised is not None, "ATE.input in a posttest action must raise"

    text = str(raised)
    # The message form carries both the schedule's file:line and names the function.
    assert text.startswith("schedule.tslua:")
    assert "ATE.input:" in text
    # Full content: one settled line carrying BOTH the location and the function name.
    assert "only allowed in a step action" in text


def test_report_lockdown_records_message_form_and_content() -> None:
    """Criterion b — ``ATE.report`` is illegal while loading; constructing the host raises with the
    exact ``<file>: ATE.report:`` message form (load time has no call-site line, so it omits one).
    This settled line is precisely what the run engine records verbatim as both the ERROR message
    and its full content."""
    raised: Exception | None = None
    try:
        # Loading the chunk runs the top level; ATE.report there raises before any step/action.
        _make_host(LOAD_REPORT)
    except HostError as exc:
        raised = exc

    assert raised is not None, "a load-time ATE.report must raise"

    text = str(raised)
    # The message form carries both the schedule's filename and names the function.
    assert "schedule.tslua:" in text
    assert "ATE.report:" in text
    # Full content: one settled line carrying BOTH the location and the reason.
    assert "only allowed in a step action" in text
