"""Packet 07d — every Media Release in the resolved chain is recorded with the run.

`media_release` gets one row per member of the run's search chain, in chain order: the release that
owns the run at seq 0, then its dependencies depth-first in declaration order, a release reached
twice listed once. Each row carries that release's own media number, name, version and the SHA-256
of ITS OWN `.atedef`; a `.atedef` that cannot be read at run start still gets a row, with a null
checksum and one WARNING, and the run continues. Seam 1 throughout: real `.atedef` trees and real
Lua schedules on disk, real SQLite, rows read back with `store.media_releases(run_uuid)`.
"""

import hashlib
import json
import time
from pathlib import Path
from typing import Optional

from ate.catalog import module_map, resolve_schedule_definition
from ate.engine.run_engine import RunEngine
from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.storage import RunStore

from ._fixtures import _drive

SCHEDULE_SRC = (
    'local m = require("c_mod")\n'
    'return ScheduleParams("Chain schedule", "requires a module from a grandchild release") :setSchedule({\n'
    '  StepParams("check"):setStepNo("1.")\n'
    '    :setAction(function() ATE.report(Result.PASS, "chain module " .. m.name) end),\n'
    "})\n"
)
PLAIN_SRC = (
    'return ScheduleParams("Plain schedule", "requires nothing") :setSchedule({\n'
    '  StepParams("check"):setStepNo("1.")\n'
    '    :setAction(function() ATE.report(Result.PASS, "no dependencies") end),\n'
    "})\n"
)
B_MOD_SRC = 'return { name = "B module", source = "B" }\n'
C_MOD_SRC = 'return { name = "deep module", source = "C" }\n'
D_MOD_SRC = 'return { name = "D module", source = "D" }\n'


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entry(path: str, src: str, module: Optional[str] = None) -> dict:
    entry = {"path": path, "sha256": _sha(src)}
    if module:
        entry["module"] = module
    return entry


def _write_releases(tmp_path: Path, releases: dict[str, dict], files: dict[str, str]) -> Path:
    """Write one `.atedef` per label under ``defs/<label>/`` plus the files it lists."""
    root = tmp_path / "defs"
    root.mkdir(parents=True, exist_ok=True)
    for label, atedef in releases.items():
        release_dir = root / label
        release_dir.mkdir(parents=True, exist_ok=True)
        (release_dir / f"{label}.atedef").write_text(json.dumps(atedef), encoding="utf-8")
        for entry in atedef["files"]:
            if entry["path"] in files:
                target = release_dir / entry["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(files[entry["path"]], encoding="utf-8")
    return root


def _config(tmp_path: Path, root: Path) -> Path:
    config = {
        "format": 1,
        "station": {"name": "BENCH-07D", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c11"},
        "defaultProject": "Power board",
        "database": {"type": "sqlite", "path": str(tmp_path / "ate.db"), "storeMismatchedFiles": "never"},
        "reports": {"directory": str(tmp_path / "reports")},
        "testDefinitions": {"directories": [str(root)]},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def _finish(qapp, session: HeadlessRunSession) -> bool:
    """Pump the event loop until the run is FINISHED and its report job is done."""
    written: list[tuple[str, str]] = []
    session.report_written.connect(lambda html, pdf: written.append((html, pdf)))
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if session._state == SessionState.FINISHED and written:
            return True
        time.sleep(0.005)
    raise TimeoutError("the run never reached FINISHED with its report written")


def _chain(session) -> list[tuple[int, str, str, str, Optional[str]]]:
    """This run's `media_release` rows as (seq, media number, name, version, atedef sha)."""
    return [
        (row["seq"], row["media_number"], row["name"], row["version"], row["atedef_sha256"])
        for row in session.store.media_releases(session.run_uuid)
    ]


def _atedef_sha(root: Path, label: str) -> str:
    return hashlib.sha256((root / label / f"{label}.atedef").read_bytes()).hexdigest()


# -- the chain recorded ------------------------------------------------------

def test_a_three_level_chain_records_every_release_in_chain_order(qapp, tmp_path):
    """A -> B -> C: three rows, seq 0/1/2, each with its own name, version and .atedef SHA-256."""
    root = _write_releases(
        tmp_path,
        {
            "a": {
                "format": 1, "mediaNumber": "MR-A", "name": "Release A", "version": "1.0.0",
                "depends": [{"name": "Release B", "version": "^1.0"}],
                "files": [_entry("main.tslua", SCHEDULE_SRC)],
            },
            "b": {
                "format": 1, "mediaNumber": "MR-B", "name": "Release B", "version": "1.2.0",
                "depends": [{"name": "Release C", "version": "^1.0"}],
                "files": [_entry("b_mod.lua", B_MOD_SRC, "b_mod")],
            },
            "c": {
                "format": 1, "mediaNumber": "MR-C", "name": "Release C", "version": "1.1.0",
                "files": [_entry("c_mod.lua", C_MOD_SRC, "c_mod")],
            },
        },
        {"main.tslua": SCHEDULE_SRC, "b_mod.lua": B_MOD_SRC, "c_mod.lua": C_MOD_SRC},
    )
    session = HeadlessRunSession(_config(tmp_path, root))
    session.select_schedule("Chain schedule")
    session.start()
    assert _finish(qapp, session)
    assert session.store.get_test_run(session.run_uuid)["result"] == "PASS"

    rows = _chain(session)
    assert [(seq, media, name, version) for seq, media, name, version, _ in rows] == [
        (0, "MR-A", "Release A", "1.0.0"),
        (1, "MR-B", "Release B", "1.2.0"),
        (2, "MR-C", "Release C", "1.1.0"),
    ]
    # each row's checksum is that release's OWN .atedef, not the top-level one
    for label, row in zip(("a", "b", "c"), rows):
        assert row[4] == _atedef_sha(root, label), f"{row[1]} recorded {row[4]}, not its own .atedef"
    assert len({row[4] for row in rows}) == 3, "the three .atedef files must have three distinct checksums"


def test_a_diamond_chain_records_the_shared_dependency_once_in_search_order(qapp, tmp_path):
    """A -> [B, C], B -> D, C -> D: four rows, A, B, D, C; D appears once."""
    root = _write_releases(
        tmp_path,
        {
            "a": {
                "format": 1, "mediaNumber": "MR-A", "name": "Release A", "version": "1.0.0",
                "depends": [{"name": "Release B", "version": "^1.0"}, {"name": "Release C", "version": "^1.0"}],
                "files": [_entry("main.tslua", SCHEDULE_SRC)],
            },
            "b": {
                "format": 1, "mediaNumber": "MR-B", "name": "Release B", "version": "1.0.0",
                "depends": [{"name": "Release D", "version": "^1.0"}],
                "files": [],
            },
            "c": {
                "format": 1, "mediaNumber": "MR-C", "name": "Release C", "version": "1.1.0",
                "files": [_entry("c_mod.lua", C_MOD_SRC, "c_mod")],
            },
            "d": {
                "format": 1, "mediaNumber": "MR-D", "name": "Release D", "version": "1.0.1",
                "files": [_entry("d_mod.lua", D_MOD_SRC, "d_mod")],
            },
        },
        {"main.tslua": SCHEDULE_SRC, "c_mod.lua": C_MOD_SRC, "d_mod.lua": D_MOD_SRC},
    )
    session = HeadlessRunSession(_config(tmp_path, root))
    session.select_schedule("Chain schedule")
    session.start()
    assert _finish(qapp, session)

    rows = _chain(session)
    assert [media for _, media, _, _, _ in rows] == ["MR-A", "MR-B", "MR-D", "MR-C"], rows
    assert [seq for seq, _, _, _, _ in rows] == [0, 1, 2, 3]
    for label, row in zip(("a", "b", "d", "c"), rows):
        assert row[4] == _atedef_sha(root, label), f"{row[1]} recorded {row[4]}, not its own .atedef"


def test_a_release_with_no_dependencies_records_exactly_one_row(qapp, tmp_path):
    """Unchanged behaviour for a release with nothing to resolve: one row, seq 0."""
    root = _write_releases(
        tmp_path,
        {
            "solo": {
                "format": 1, "mediaNumber": "MR-SOLO", "name": "Solo release", "version": "3.4.5",
                "files": [_entry("main.tslua", PLAIN_SRC)],
            },
        },
        {"main.tslua": PLAIN_SRC},
    )
    session = HeadlessRunSession(_config(tmp_path, root))
    session.select_schedule("Plain schedule")
    session.start()
    assert _finish(qapp, session)

    rows = _chain(session)
    assert rows == [(0, "MR-SOLO", "Solo release", "3.4.5", _atedef_sha(root, "solo"))]


def test_a_dependency_whose_atedef_is_deleted_is_recorded_with_no_checksum(qapp, tmp_path):
    """The chain is resolved at run start; a member whose .atedef is gone still gets a row.

    The row is there, its checksum is null, one WARNING (source ``ate``) names it, and the run
    finishes PASS. The definition is resolved from the tree on disk before the .atedef is removed,
    so the engine sees a chain it can no longer read from."""
    root = _write_releases(
        tmp_path,
        {
            "a": {
                "format": 1, "mediaNumber": "MR-A", "name": "Release A", "version": "1.0.0",
                "depends": [{"name": "Release B", "version": "^1.0"}],
                "files": [_entry("main.tslua", SCHEDULE_SRC)],
            },
            "b": {
                "format": 1, "mediaNumber": "MR-B", "name": "Release B", "version": "1.2.0",
                "depends": [{"name": "Release C", "version": "^1.0"}],
                "files": [_entry("b_mod.lua", B_MOD_SRC, "b_mod")],
            },
            "c": {
                "format": 1, "mediaNumber": "MR-C", "name": "Release C", "version": "1.1.0",
                "files": [_entry("c_mod.lua", C_MOD_SRC, "c_mod")],
            },
        },
        {"main.tslua": SCHEDULE_SRC, "b_mod.lua": B_MOD_SRC, "c_mod.lua": C_MOD_SRC},
    )
    definition = resolve_schedule_definition(root, schedule_name="Chain schedule")
    assert definition is not None and not definition.broken
    store = RunStore(tmp_path / "ate.db")
    engine = RunEngine(
        store, str(root / "a" / "main.tslua"),
        modules=module_map(definition), definition=definition,
    )
    (root / "b" / "b.atedef").unlink()  # the dependency's definition file vanished after the scan

    captured: dict[str, Optional[str]] = {}
    _drive(engine, qapp, lambda uuid: captured.__setitem__("run_uuid", uuid))
    uuid = captured["run_uuid"]
    assert uuid is not None, "engine never reported a run key"

    rows = store.media_releases(uuid)
    assert [(r["seq"], r["media_number"]) for r in rows] == [(0, "MR-A"), (1, "MR-B"), (2, "MR-C")]
    by_media = {r["media_number"]: r for r in rows}
    assert by_media["MR-A"]["atedef_sha256"] == _atedef_sha(root, "a")
    assert by_media["MR-C"]["atedef_sha256"] == _atedef_sha(root, "c")
    assert by_media["MR-B"]["atedef_sha256"] is None
    warnings = [ln for ln in store.log_lines(uuid) if ln["level"] == "WARNING" and "atedef" in ln["text"]]
    assert len(warnings) == 1, store.log_lines(uuid)
    assert "MR-B" in warnings[0]["text"]
    assert store.get_test_run(uuid)["result"] == "PASS", "the run must still complete"
