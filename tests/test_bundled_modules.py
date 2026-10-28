"""Packet 06d — bundled ``ate.`` modules: resolution, recording, and the ATE version.

``require("ate.devices")`` resolves to a module shipped inside the ``ate`` package (``ate/lua/``), never
to the Test Definition, and an ``ate.`` name ATE does not ship raises like any other unknown module.
Each bundled module used is recorded as a ``run_file`` row keyed by the module NAME with no owning media
release and no expected checksum — there is nothing to expect, the bytes are this build — so it can never
mismatch. The run also records the ATE version. Seam 1: real files, a real lupa runtime, offscreen Qt."""

import hashlib
import importlib.resources
import json
import os
import tempfile
import time
import tomllib
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, Qt

from ate import _version
from ate.catalog import module_map, read_definition
from ate.engine.run_engine import RunEngine
from ate.run_session.headless import HeadlessRunSession
from ate.storage.store import RunStore

from ._fixtures import (
    _drive,
    fake_pdf_helper as _fake_helper,
    read_run_row,
    WRITES_PDF,
    write_ate_env,
)


def _bundled_bytes(name: str) -> bytes:
    """The bytes ATE ships for a bundled module, read the same way the host reads them."""
    return (importlib.resources.files("ate") / "lua" / f"{name}.lua").read_bytes()


def _write_release(tmp_path: Path, main_src: str, extra: dict[str, str] | None = None) -> tuple[Path, object]:
    """Write one Media Release whose schedule is ``main_src``; return ``(defs_dir, definition)``.

    ``extra`` adds further ``.lua`` files (and their checksums) so a run can also require an ordinary
    Test Definition module alongside a bundled one."""
    defs_dir = tmp_path / "defs"
    defs_dir.mkdir(parents=True, exist_ok=True)
    (defs_dir / "main.tslua").write_text(main_src, encoding="utf-8")

    files = [{"path": "main.tslua", "sha256": hashlib.sha256(main_src.encode("utf-8")).hexdigest()}]
    for rel_path, source in (extra or {}).items():
        (defs_dir / rel_path).write_text(source, encoding="utf-8")
        files.append({"path": rel_path, "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest()})

    (defs_dir / "demo.atedef").write_text(
        json.dumps({"format": 1, "mediaNumber": "REL-06D", "name": "Bundled release", "version": "1.0.0", "files": files}),
        encoding="utf-8",
    )
    return defs_dir, read_definition(defs_dir / "demo.atedef")


def _run(app: QCoreApplication, defs_dir: Path, definition):
    """Run ``main.tslua`` and return ``(run row, run_file rows)``."""
    captured = {}
    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        engine = RunEngine(
            store, str(defs_dir / "main.tslua"),
            modules=module_map(definition), definition=definition,
        )
        engine.run_uuid_reported.connect(lambda u: captured.__setitem__("run_uuid", u))
        _drive(engine, app)
        run_uuid = captured["run_uuid"]
        return store.get_test_run(run_uuid), store.run_files(run_uuid), store.log_lines(run_uuid)


BUNDLED_SCHEDULE = (
    'local devices = require("ate.devices")\n'
    'return ScheduleParams("Bundled schedule", "requires a module bundled with ATE") :setSchedule({\n'
    '  StepParams("require it again"):setStepNo("1.")\n'
    '    :setAction(function() require("ate.devices"); ATE.report(Result.PASS, "devices loaded") end),\n'
    "})\n"
)


def test_bundled_module_runs_and_is_recorded_as_a_run_file_row(app: QCoreApplication) -> None:
    """``require("ate.devices")`` runs, and the run records its checksum under the module name."""
    with tempfile.TemporaryDirectory() as td:
        defs_dir, definition = _write_release(Path(td), BUNDLED_SCHEDULE)
        row, files, _ = _run(app, defs_dir, definition)

    assert row is not None and row["result"] == "PASS", "a schedule requiring a bundled module still runs"

    bundled = [f for f in files if f["kind"] == "ate-module"]
    assert len(bundled) == 1, "one row for the one bundled module used"
    f = bundled[0]
    assert f["path"] == "ate.devices"  # the module name, not a filesystem path
    assert f["media_number"] is None  # bundled modules belong to no Media Release
    assert f["expected_sha256"] is None  # nothing in the Test Definition expects these bytes
    assert f["mismatch"] == 0 or f["mismatch"] is False
    assert f["computed_sha256"] == hashlib.sha256(_bundled_bytes("devices")).hexdigest()
    # the schedule is used first, the required module second
    assert [x["path"] for x in files] == ["main.tslua", "ate.devices"]


def test_unknown_bundled_module_fails_naming_the_module(app: QCoreApplication) -> None:
    """An ``ate.`` name ATE does not ship is an error that names the module."""
    src = (
        'return ScheduleParams("Missing bundled module", "requires a module ATE does not ship") :setSchedule({\n'
        '  StepParams("bad require"):setStepNo("1.")\n'
        '    :setAction(function() require("ate.nope") end),\n'
        "})\n"
    )
    with tempfile.TemporaryDirectory() as td:
        defs_dir, definition = _write_release(Path(td), src)
        row, _, logs = _run(app, defs_dir, definition)

    assert row["result"] != "PASS"
    assert any("require: unknown module 'ate.nope'" in ln["text"] for ln in logs)


def test_malformed_ate_names_are_rejected(app: QCoreApplication) -> None:
    """``ate.`` is the reserved namespace: anything else after it is not a module name."""
    for bad in ("ate.", "ate.../x", "ate.sub.module"):
        src = (
            'return ScheduleParams("Malformed require", "requires a name outside the bundled namespace") :setSchedule({\n'
            '  StepParams("bad require"):setStepNo("1.")\n'
            f'    :setAction(function() require("{bad}") end),\n'
            "})\n"
        )
        with tempfile.TemporaryDirectory() as td:
            defs_dir, definition = _write_release(Path(td), src)
            row, _, logs = _run(app, defs_dir, definition)

        assert row["result"] != "PASS", f"require('{bad}') is rejected"
        assert any(f"require: unknown module '{bad}'" in ln["text"] for ln in logs)


def test_a_definition_module_with_an_ordinary_name_still_resolves(app: QCoreApplication) -> None:
    """Names that are not ``ate.`` keep resolving against the Test Definition, as before (packet 06a)."""
    helper = "return { name = 'helper', value = 42 }\n"
    src = (
        'local h = require("helper")\n'
        'return ScheduleParams("Ordinary module schedule", "requires a definition module") :setSchedule({\n'
        '  StepParams("use it"):setStepNo("1.")\n'
        '    :setAction(function() ATE.report(Result.PASS, "helper is " .. h.value) end),\n'
        "})\n"
    )
    with tempfile.TemporaryDirectory() as td:
        defs_dir, definition = _write_release(Path(td), src, {"helper.lua": helper})
        row, files, _ = _run(app, defs_dir, definition)

    assert row is not None and row["result"] == "PASS"
    assert [(f["path"], f["kind"]) for f in files] == [("main.tslua", "tslua"), ("helper.lua", "lua")]
    assert all(f["media_number"] == "REL-06D" for f in files)


def test_requiring_a_bundled_module_twice_records_one_row(app: QCoreApplication) -> None:
    """A bundled module loads once per run; the cached second require adds no row."""
    src = (
        'local first = require("ate.devices")\n'
        'return ScheduleParams("Twice bundled", "requires the same bundled module in two steps") :setSchedule({\n'
        '  StepParams("first"):setStepNo("1.")\n'
        '    :setAction(function() require("ate.devices"); ATE.report(Result.PASS, "first") end),\n'
        '  StepParams("second"):setStepNo("2.")\n'
        '    :setAction(function() require("ate.devices"); ATE.report(Result.PASS, "second") end),\n'
        "})\n"
    )
    with tempfile.TemporaryDirectory() as td:
        defs_dir, definition = _write_release(Path(td), src)
        row, files, _ = _run(app, defs_dir, definition)

    assert row is not None and row["result"] == "PASS"
    assert [f for f in files if f["kind"] == "ate-module"] and len([f for f in files if f["kind"] == "ate-module"]) == 1


def test_headless_run_records_the_ate_version(app: QCoreApplication, tmp_path: Path, monkeypatch) -> None:
    """The run row carries this build's version, from ``ate._version`` (packet 06d fills ticket 02's gap)."""
    env = write_ate_env(tmp_path)
    # The real PDF helper needs QtWebEngine; stub it so the QProcess finishes before the session dies.
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", _fake_helper(WRITES_PDF))
    session = HeadlessRunSession(env["config"])
    session.set_operator("Ada")
    session.set_project("Power board")
    session.set_uut_type("PSU")
    session.set_uut_serial("PSU-001")
    session.select_schedule("Passing schedule")

    events: list[tuple[str, str]] = []
    session.report_written.connect(lambda html, pdf: events.append((html, pdf)), Qt.ConnectionType.QueuedConnection)
    session.start()

    # Pump until the PDF helper's process has ended: a QProcess destroyed while still running aborts Qt.
    deadline = time.time() + 20
    while not events and time.time() < deadline:
        app.processEvents()
        time.sleep(0.002)

    row = read_run_row(session.store, session.run_uuid)
    assert row is not None
    assert row["ate_version"] == _version.__version__
    assert row["ate_version"]  # never empty: every run says which ATE ran it


def test_bundled_modules_ship_inside_the_package() -> None:
    """``ate/lua/*.lua`` is package data. The tests run from the source tree, so this checks the
    config a wheel is built from: the keys must be quoted, because a bare dotted key (``ate.lua =``)
    is a nested TOML table that setuptools rejects, and then no wheel builds at all. Packet 28a's
    wildcard key ``"*"`` covers every package, so a new resource needs no pyproject edit."""
    assert _bundled_bytes("devices"), "ATE ships a bundled devices module"
    lua_dir = importlib.resources.files("ate") / "lua"
    assert (lua_dir / "devices.lua").is_file()
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    package_data = tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["setuptools"]["package-data"]
    patterns = [pattern for entry in package_data.values() for pattern in entry]
    assert "*.lua" in patterns, f"package-data must ship ate/lua/*.lua: {package_data}"
    assert any(pattern.endswith(".j2") for pattern in patterns), f"and the report template: {package_data}"
    assert all(isinstance(v, list) for v in package_data.values()), f"no nested tables: {package_data}"


def test_an_ate_name_cannot_walk_out_of_the_bundled_directory(app: QCoreApplication) -> None:
    """A planted .lua outside ``ate/lua`` reached by a ``..`` path is never loaded: only ``ate.<word>``
    is a bundled name, whatever exists on disk."""
    lua_dir = Path(str(importlib.resources.files("ate") / "lua"))
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "escape.lua").write_text('ESCAPED = true\nreturn true\n', encoding="utf-8")
        bad = "ate." + os.path.relpath(Path(td) / "escape", lua_dir)
        src = (
            'return ScheduleParams("Escape", "requires a file outside ate/lua") :setSchedule({\n'
            '  StepParams("escape"):setStepNo("1.")\n'
            f'    :setAction(function() require("{bad}") ATE.report(Result.PASS, "loaded") end),\n'
            "})\n"
        )
        defs_dir, definition = _write_release(Path(td), src)
        row, files, logs = _run(app, defs_dir, definition)

    assert row["result"] != "PASS", f"require('{bad}') must not load a file outside ate/lua"
    assert any(f"require: unknown module '{bad}'" in ln["text"] for ln in logs)
    assert not [f for f in files if f["kind"] == "ate-module"], f"nothing recorded as bundled: {files}"
