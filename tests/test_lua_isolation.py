"""Ticket 09 criterion 03 — an isolated ATE function never lets a Python exception reach Lua.

Two seams cover the contract:

* :func:`_AteTable._isolate` catches the exception, logs its full traceback to the app-log
  seam at ERROR, and returns graceful halves ``(false, "<file>:<line>: ATE.<fn>: <msg>")``;
* a schedule that calls such a function through real Lua execution sees no error surface —
  run_engine's own ``except`` never fires (no ERROR Attempt) — because the wrapper already
  returned. Verified against a real lupa runtime and a real SQLite file with an injected
  failing ATE function via RunEngine's ``ate_fns`` seam; no mocks."""

import sqlite3
from pathlib import Path

from PyQt6.QtCore import QCoreApplication

from ate.engine.lua_host import LuaHost
from ate.engine.run_engine import RunEngine
from ate.storage.store import RunStore

from ._fixtures import ISO_ISOLATED, _drive
from tests._fixtures import scratch_directory


def _make_host(source: str) -> LuaHost:
    """Construct a host for one schedule on disk and return it (no Qt event loop needed)."""
    with scratch_directory() as td:
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8", newline="\n")
        return LuaHost(str(schedule_path))


def _boom(fn_name: str = "boom", msg: str = "ATE.boom: cannot read nil"):
    """A Python function that raises when called — the failure criterion 03 isolates."""
    def boom(_x):
        raise ValueError(msg)

    return boom


# --- Unit: _isolate returns halves and logs the Python traceback -------------------------


def test_isolate_returns_halves_and_logs_python_traceback():
    """A failing ATE function wrapped by __getattr__ is caught, logged at ERROR with its full
    traceback, and returns graceful halves instead of raising — even outside a step run."""
    host = _make_host("return ScheduleParams('p') :setSchedule({})")
    ate = host.ate

    def boom(_x):
        raise ValueError("ATE.boom: cannot read nil")

    # Registering the failing function puts it in _extra; __getattr__ then routes access to the
    # _isolate wrapper (the same path a Lua call ATE.boom takes through lupa).
    ate.register("boom", boom)

    wrapped = ate.__getattr__("boom")
    assert callable(wrapped)

    # Outside a run, only the filename is known; the message still carries file + function.
    ok, msg = wrapped("bad value")  # must NOT raise
    assert ok is False
    assert msg.startswith("schedule.tslua:")
    assert "ATE.boom:" in msg
    assert "ATE.boom: cannot read nil" in msg

    # The full Python traceback was logged at ERROR, not swallowed or re-raised. host.logs is a
    # list of ``(level, source, text)`` tuples (the run engine batches these per step).
    error_logs = [ln for ln in host.logs if ln[0] == "ERROR"]
    assert len(error_logs) == 1
    tb = "\n".join(ln[2] for ln in error_logs)
    assert "ValueError" in tb
    assert "ATE.boom: cannot read nil" in tb


def test_isolate_attributes_error_to_the_calling_schedule_line():
    """With a current step set, the isolated message carries that step's schedule line."""
    host = _make_host(ISO_ISOLATED)
    ate = host.ate

    def boom(_x):
        raise ValueError("ATE.boom: cannot read nil")

    ate.register("boom", boom)  # same path a Lua call ATE.boom takes through lupa -> __getattr__
    host._current_step = 0  # run_engine sets this before each action
    ok, msg = ate.__getattr__("boom")("bad value")

    assert ok is False
    # The message names the schedule file and a concrete line (where :setAction began).
    assert msg.startswith("schedule.tslua:")
    assert ": ATE.boom: " in msg


# --- Seam-1 integration through RunEngine ------------------------------------------------


def _boom_fn():
    def boom(_x):
        raise ValueError("ATE.boom: cannot read nil")

    return boom


def test_ate_exception_isolated_through_run_engine(app: QCoreApplication) -> None:
    """A schedule step that calls an isolated failing ATE function records no ERROR Attempt from
    run_engine (the wrapper already handled it), yet the Python traceback is logged at ERROR with
    the schedule's file:line and the function name — so criterion 03 is testable without device ops."""
    captured = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with scratch_directory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(ISO_ISOLATED, encoding="utf-8", newline="\n")
        engine = RunEngine(store, str(schedule_path), ate_fns={"boom": _boom_fn()})
        engine.run_uuid_reported.connect(_capture)
        _drive(engine, app)

    assert captured.get("run_uuid") is not None, "engine never reported a run key"
    ru = captured["run_uuid"]

    conn = store._conn
    conn.row_factory = sqlite3.Row
    # The boom step must NOT be an ERROR Attempt: _isolate caught it before run_engine's except.
    node = conn.execute(
        "SELECT result, attempt_count FROM node WHERE run_uuid=? AND step_number='1.'", (ru,)
    ).fetchone()
    assert node is not None
    assert node["result"] != "ERROR"

    # But the Python traceback was logged at ERROR with file:line + function name.
    error_logs = [ln for ln in store.log_lines(ru) if ln["level"] == "ERROR"]
    assert len(error_logs) >= 1
    tb = "\n".join(ln["text"] for ln in error_logs)
    assert "schedule.tslua:" in tb
    assert "ATE.boom: cannot read nil" in tb
