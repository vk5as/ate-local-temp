"""Packet 06b — checksums recorded on every run through ``media_release``/``run_file``.

A schedule that requires a helper module is run end to end: the release and both files (the
schedule at load time, the module when required) are recorded with expected and computed SHA-256.
Then the helper's bytes are changed after its checksum was written into the Test Definition — the
run still completes, but that file's row is flagged mismatched and a single ``WARNING`` line (source
``ate``, naming the file) is recorded. Verified against real files with a real lupa runtime under
offscreen Qt — no mocks."""

import hashlib
import json
import tempfile
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication

from ate.catalog import module_map, read_definition
from ate.engine.run_engine import RunEngine
from ate.storage.store import RunStore

from ._fixtures import _drive


def _helper_source() -> str:
    return "-- packet 06b: a helper the schedule requires at load time and inside its step\n" \
           "return { name = 'helper', value = 42 }\n"


def _main_source() -> str:
    return (
        'local h = require("helper")\n'
        'return ScheduleParams("Checksum schedule", "requires a helper and reports its value") :setSchedule({\n'
        '  StepParams("check"):setStepNo("1.")\n'
        '    :setAction(function() ATE.report(Result.PASS, "helper is "..h.value) end),\n'
        "})\n"
    )


def _write_release(tmp_path: Path) -> tuple[Path, object]:
    """Write one Media Release (a helper + a requiring schedule) and return ``(defs_dir, definition)``.

    The ``.atedef`` records the correct checksums for both files; the caller can later edit a file on
    disk so its bytes no longer match what was recorded."""
    defs_dir = tmp_path / "defs"
    defs_dir.mkdir(parents=True, exist_ok=True)

    helper_src = _helper_source()
    main_src = _main_source()
    (defs_dir / "helper.lua").write_text(helper_src, encoding="utf-8")
    (defs_dir / "main.tslua").write_text(main_src, encoding="utf-8")

    atedef = {
        "format": 1,
        "mediaNumber": "REL-06B",
        "name": "Checksum release",
        "version": "1.0.0",
        "files": [
            {"path": "helper.lua", "sha256": hashlib.sha256(helper_src.encode("utf-8")).hexdigest()},
            {"path": "main.tslua", "sha256": hashlib.sha256(main_src.encode("utf-8")).hexdigest()},
        ],
    }
    (defs_dir / "demo.atedef").write_text(json.dumps(atedef), encoding="utf-8")

    definition = read_definition(defs_dir / "demo.atedef")
    return defs_dir, definition


def _drive_capture(app: QCoreApplication, engine: RunEngine, captured: dict) -> None:
    """Drive the worker to completion and capture its run key as a plain string."""
    captured["run_uuid"] = None
    engine.run_uuid_reported.connect(lambda u: captured.__setitem__("run_uuid", u))
    _drive(engine, app)


def test_clean_run_records_release_and_files_with_no_mismatch(app: QCoreApplication) -> None:
    """A clean run records the release (seq 0) and both files with matching checksums."""
    captured = {}
    with tempfile.TemporaryDirectory() as td:
        defs_dir, definition = _write_release(Path(td))
        store = RunStore(Path(td) / "ate.db")
        engine = RunEngine(
            store, str(defs_dir / "main.tslua"),
            modules=module_map(definition), definition=definition,
        )
        _drive_capture(app, engine, captured)

    assert captured.get("run_uuid") is not None, "engine never reported a run key"
    row = store.get_test_run(captured["run_uuid"])
    assert row is not None and row["result"] == "PASS"  # the run still completes cleanly

    releases = store.media_releases(captured["run_uuid"])
    assert len(releases) == 1
    rel = releases[0]
    assert rel["seq"] == 0
    assert (rel["media_number"], rel["name"], rel["version"]) == ("REL-06B", "Checksum release", "1.0.0")
    assert rel["atedef_sha256"] is not None and len(rel["atedef_sha256"]) == 64

    files = store.run_files(captured["run_uuid"])
    # The schedule (loaded first) then the required module, both mismatch-free.
    assert [(f["path"], f["kind"], f["expected_sha256"] == f["computed_sha256"], f["mismatch"]) for f in files] == [
        ("main.tslua", "tslua", True, False),
        ("helper.lua", "lua", True, False),
    ]
    assert all(f["media_number"] == "REL-06B" for f in files)


def test_mismatched_file_is_flagged_and_warns_but_still_runs(app: QCoreApplication) -> None:
    """Editing a file after its checksum was recorded flags that row and logs one WARNING."""
    captured = {}
    with tempfile.TemporaryDirectory() as td:
        defs_dir, definition = _write_release(Path(td))

        # Corrupt the helper on disk *after* the .atedef recorded its original bytes.
        (defs_dir / "helper.lua").write_text(_helper_source() + "\n-- tampered after recording\n", encoding="utf-8")

        store = RunStore(Path(td) / "ate.db")
        engine = RunEngine(
            store, str(defs_dir / "main.tslua"),
            modules=module_map(definition), definition=definition,
        )
        _drive_capture(app, engine, captured)

    assert captured.get("run_uuid") is not None, "engine never reported a run key"
    row = store.get_test_run(captured["run_uuid"])
    # The mismatched file still loads and the schedule still runs to completion.
    assert row is not None and row["result"] == "PASS"

    files = {f["path"]: f for f in store.run_files(captured["run_uuid"])}
    helper = files["helper.lua"]
    assert helper["kind"] == "lua"
    # expected (from the .atedef) differs from computed (the tampered bytes).
    assert helper["expected_sha256"] != helper["computed_sha256"]
    assert bool(helper["mismatch"]) is True  # stored as an int flag (0/1)

    warnings = [ln for ln in store.log_lines(captured["run_uuid"]) if ln["level"] == "WARNING" and ln["source"] == "ate"]
    assert len(warnings) == 1, "exactly one checksum-mismatch WARNING line"
    assert "helper.lua" in warnings[0]["text"]
