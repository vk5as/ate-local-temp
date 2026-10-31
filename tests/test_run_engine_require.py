"""Packet 06a — the replacement ``require`` end to end through a run.

A schedule that requires its helper at load time (and inside steps) must still load and run when the
run engine forwards the Test Definition's module map to the Lua host; modules are cached once per run,
so every step in one run sees the same value even if it re-requires. Verified against real files with
a real lupa runtime under offscreen Qt — no mocks."""

import hashlib
import json
import tempfile
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication

from ate.catalog import module_map, read_definition, resolve_schedule_definition
from ate.engine.run_engine import RunEngine
from ate.storage.store import RunStore

from ._fixtures import _drive


# A helper that counts how many times it really ran and exposes the count to steps.
COUNTER_MODULE = """\
-- packet 06a: a require'd module counts how many times it actually runs, then reports it
COUNTER = (COUNTER or 0) + 1
return { name = "counter", count = COUNTER }
"""


def _write_release(tmp_path: Path) -> tuple[Path, dict]:
    """Write one Media Release with a helper module and two schedules that require it.

    Returns ``(defs_dir, definition)`` where ``definition`` is the parsed :class:`Definition`."""
    defs_dir = tmp_path / "defs"
    defs_dir.mkdir(parents=True, exist_ok=True)

    # Sources first so their bytes (and hashes) are exact; written once each.
    load_src = (
        'local helper = require("helper")\n'
        "_G.H_NAME = helper.name\n"
        'return ScheduleParams("Load require", "requires its helper at load time") :setSchedule({})\n'
    )
    cache_src = (
        'local first = require("helper")\n'
        'return ScheduleParams("Cache require", "two steps that re-require the helper") :setSchedule({\n'
        '  StepParams("first step"):setStepNo("1.")\n'
        '    :setAction(function() ATE.report(Result.PASS, "count is "..first.count) end),\n'
        '  StepParams("second step"):setStepNo("2.")\n'
        '    :setAction(function() local again=require("helper"); ATE.report(Result.PASS, "still "..again.count) end),\n'
        "})\n"
    )

    (defs_dir / "helper.lua").write_bytes(COUNTER_MODULE.encode("utf-8"))
    (defs_dir / "load.tslua").write_text(load_src, encoding="utf-8", newline="\n")
    (defs_dir / "cache.tslua").write_text(cache_src, encoding="utf-8", newline="\n")

    atedef = {
        "format": 1,
        "mediaNumber": "REL-06A",
        "name": "Require release",
        "version": "1.0.0",
        "files": [
            {"path": "helper.lua", "sha256": hashlib.sha256(COUNTER_MODULE.encode("utf-8")).hexdigest()},
            {"path": "load.tslua", "sha256": hashlib.sha256(load_src.encode("utf-8")).hexdigest()},
            {"path": "cache.tslua", "sha256": hashlib.sha256(cache_src.encode("utf-8")).hexdigest()},
        ],
    }
    (defs_dir / "demo.atedef").write_text(json.dumps(atedef), encoding="utf-8", newline="\n")

    definition = read_definition(defs_dir / "demo.atedef")
    return defs_dir, definition


def _modules(definition: dict) -> dict:
    return module_map(definition)


def test_load_time_require_runs_and_step_uses_it(app: QCoreApplication) -> None:
    """A schedule that requires at load time loads and runs through the engine when modules are passed."""
    captured = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with tempfile.TemporaryDirectory() as td:
        defs_dir, definition = _write_release(Path(td))
        store = RunStore(Path(td) / "ate.db")
        engine = RunEngine(store, str(defs_dir / "load.tslua"), modules=_modules(definition))
        engine.run_uuid_reported.connect(_capture)
        _drive(engine, app)

    assert captured.get("run_uuid") is not None, "engine never reported a run key"
    row = store.get_test_run(captured["run_uuid"])
    assert row is not None and row["result"] == "PASS"


def test_module_is_cached_once_per_run(app: QCoreApplication) -> None:
    """Two steps re-requiring the same module both see count 1 — it ran once for the whole run."""
    captured = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with tempfile.TemporaryDirectory() as td:
        defs_dir, definition = _write_release(Path(td))
        store = RunStore(Path(td) / "ate.db")
        engine = RunEngine(store, str(defs_dir / "cache.tslua"), modules=_modules(definition))
        engine.run_uuid_reported.connect(_capture)
        _drive(engine, app)

    assert captured.get("run_uuid") is not None, "engine never reported a run key"
    row = store.get_test_run(captured["run_uuid"])
    assert row is not None and row["result"] == "PASS"

    conn = store._conn
    messages = [r[0] for r in conn.execute("SELECT msg FROM attempt WHERE run_uuid=?", (row["run_uuid"],))]
    # Both steps saw the cached count of 1; if it re-ran per step the second would be 2.
    assert "count is 1" in messages
    assert "still 1" in messages


def test_resolve_schedule_definition_returns_the_release(tmp_path: Path) -> None:
    """The catalog helper returns the Media Release that owns a schedule (so its module map is reachable)."""
    defs_dir, definition = _write_release(tmp_path)

    resolved = resolve_schedule_definition(defs_dir, schedule_name="Load require")
    assert resolved is not None and resolved.media_number == "REL-06A"
    # Without the helper's map the schedule can't load its helper at load time.
    assert module_map(resolved) == {"helper": (defs_dir / "helper.lua").resolve()}
