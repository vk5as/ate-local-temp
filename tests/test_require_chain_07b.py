"""Packet 07b — transitive dependency resolution and the `require` search chain.

A Media Release's resolved chain is its own release first, then each `depends` entry in
declaration order depth-first, with a release reached twice listed once. That chain is what
`module_map` walks for `require`, what the run records as `run_file` rows, and what the
pre-run mismatch check looks at. Everything here is Seam 1: real `.atedef` files and real Lua
schedules on disk, driven through :class:`HeadlessRunSession`.
"""

import hashlib
import json
import time
from pathlib import Path
from typing import Optional

from ate.catalog import Definition, FileEntry, find_definitions, module_map
from ate.run_session.headless import HeadlessRunSession, SessionState

WRONG_SHA = "0" * 64

SCHEDULE_SRC = (
    'local m = require("c_mod")\n'
    'return ScheduleParams("Chain schedule", "requires a module from a grandchild release") :setSchedule({\n'
    '  StepParams("check"):setStepNo("1.")\n'
    '    :setAction(function() ATE.report(Result.PASS, "chain module " .. m.name) end),\n'
    "})\n"
)
UTIL_SRC = 'return { name = "own util", source = "A" }\n'
B_UTIL_SRC = 'return { name = "dependency util", source = "B" }\n'
C_MOD_SRC = 'return { name = "deep module", source = "C" }\n'
E_SHARED_SRC = 'return { name = "shared via B", source = "E" }\n'
D_SHARED_SRC = 'return { name = "shared via D", source = "D" }\n'
D_MOD_SRC = 'return { name = "D module", source = "D" }\n'
C_MOD_MISMATCH_SRC = 'return { name = "deep module", source = "C" } -- bytes changed after the release was cut\n'


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


def _config(tmp_path: Path, root: Path, store_mismatched: str = "ask") -> Path:
    config = {
        "format": 1,
        "station": {"name": "BENCH-07", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c11"},
        "database": {"type": "sqlite", "path": str(tmp_path / "ate.db"), "storeMismatchedFiles": store_mismatched},
        "reports": {"directory": str(tmp_path / "reports")},
        "testDefinitions": {"directories": [str(root)]},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def _by_media(root: Path) -> dict[str, Definition]:
    """Scan the tree and index the definitions by media number."""
    return {d.media_number: d for d in find_definitions(root)}


def _media_chain(definition: Definition) -> tuple[str, ...]:
    """The resolved chain as media numbers, for readable assertions."""
    return tuple(d.media_number for d in definition.chain)


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


def _files(session: HeadlessRunSession) -> dict[str, dict]:
    return {row["path"]: row for row in session.store.run_files(session.run_uuid)}


# -- the chain itself ----------------------------------------------------------

def test_a_three_level_chain_is_own_release_then_depth_first(tmp_path):
    """A -> B -> C: A's chain is (A, B, C); B's own chain starts with B."""
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
                "files": [_entry("b_mod.lua", C_MOD_SRC, "b_mod")],
            },
            "c": {
                "format": 1, "mediaNumber": "MR-C", "name": "Release C", "version": "1.1.0",
                "files": [_entry("c_mod.lua", C_MOD_SRC, "c_mod")],
            },
        },
        {"main.tslua": SCHEDULE_SRC, "b_mod.lua": C_MOD_SRC, "c_mod.lua": C_MOD_SRC},
    )
    by_media = _by_media(root)
    assert all(d.broken is None for d in by_media.values()), {d.media_number: d.broken for d in by_media.values()}
    assert _media_chain(by_media["MR-A"]) == ("MR-A", "MR-B", "MR-C")
    assert _media_chain(by_media["MR-B"]) == ("MR-B", "MR-C")
    assert _media_chain(by_media["MR-C"]) == ("MR-C",)
    assert by_media["MR-A"].chain[0] is by_media["MR-A"]
    assert all(isinstance(member, Definition) for member in by_media["MR-A"].chain)


def test_a_diamond_release_appears_once_in_the_chain(tmp_path):
    """A -> B, A -> C, B -> D, C -> D: D is listed once, at its first position."""
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
                "format": 1, "mediaNumber": "MR-C", "name": "Release C", "version": "1.0.0",
                "depends": [{"name": "Release D", "version": "^1.0"}],
                "files": [],
            },
            "d": {
                "format": 1, "mediaNumber": "MR-D", "name": "Release D", "version": "1.0.0",
                "files": [_entry("d_mod.lua", D_MOD_SRC, "d_mod")],
            },
        },
        {"main.tslua": SCHEDULE_SRC, "d_mod.lua": D_MOD_SRC},
    )
    chain = _media_chain(_by_media(root)["MR-A"])
    assert chain.count("MR-D") == 1
    assert chain == ("MR-A", "MR-B", "MR-D", "MR-C")


def test_a_broken_definition_keeps_an_empty_chain(tmp_path):
    """An unresolved dependency breaks the release and leaves its chain empty."""
    root = _write_releases(
        tmp_path,
        {
            "a": {
                "format": 1, "mediaNumber": "MR-A", "name": "Release A", "version": "1.0.0",
                "depends": [{"name": "Nowhere", "version": "^1.0"}],
                "files": [_entry("main.tslua", SCHEDULE_SRC)],
            }
        },
        {"main.tslua": SCHEDULE_SRC},
    )
    definition = _by_media(root)["MR-A"]
    assert definition.broken is not None and "unresolved" in definition.broken
    assert definition.chain == ()


def test_a_definition_with_no_chain_still_maps_its_own_modules():
    """Hand-built definitions (no dependency resolution ran) keep working."""
    definition = Definition(
        path=Path("/tmp/ate-chain-hand"),
        media_number="MR-HAND",
        name="Hand built",
        version="1.0.0",
        files=(FileEntry(rel_path="lib/psu.lua", module_name="lib.psu"),),
    )
    assert module_map(definition) == {"lib.psu": Path("/tmp/ate-chain-hand/lib/psu.lua")}
    assert definition.chain == ()


# -- the require search order -----------------------------------------------

def test_a_grandchild_module_is_requireable_and_recorded(qapp, tmp_path):
    """A -> B -> C: A's schedule requires C's module, the run passes, and C owns the run_file row."""
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
                "files": [_entry("b_mod.lua", D_MOD_SRC, "b_mod")],
            },
            "c": {
                "format": 1, "mediaNumber": "MR-C", "name": "Release C", "version": "1.1.0",
                "files": [_entry("c_mod.lua", C_MOD_SRC, "c_mod")],
            },
        },
        {"main.tslua": SCHEDULE_SRC, "b_mod.lua": D_MOD_SRC, "c_mod.lua": C_MOD_SRC},
    )
    session = HeadlessRunSession(_config(tmp_path, root))
    session.select_schedule("Chain schedule")
    session.start()
    assert _finish(qapp, session)
    assert session.store.get_test_run(session.run_uuid)["result"] == "PASS"
    files = _files(session)
    assert "c_mod.lua" in files, sorted(files)
    row = files["c_mod.lua"]
    assert row["media_number"] == "MR-C" and row["kind"] == "lua" and row["mismatch"] == 0
    assert row["computed_sha256"] == _sha(C_MOD_SRC) == row["expected_sha256"]
    assert session.store.get_test_run(session.run_uuid)["result"] == "PASS"


def test_a_release_own_module_wins_over_the_same_name_in_a_dependency(tmp_path):
    """Own files are searched first: A's `util` beats the `util` reached through B and C."""
    root = _write_releases(
        tmp_path,
        {
            "a": {
                "format": 1, "mediaNumber": "MR-A", "name": "Release A", "version": "1.0.0",
                "depends": [{"name": "Release B", "version": "^1.0"}],
                "files": [_entry("main.tslua", SCHEDULE_SRC), _entry("util.lua", UTIL_SRC, "util")],
            },
            "b": {
                "format": 1, "mediaNumber": "MR-B", "name": "Release B", "version": "1.0.0",
                "depends": [{"name": "Release C", "version": "^1.0"}],
                "files": [_entry("b_mod.lua", D_MOD_SRC, "b_mod")],
            },
            "c": {
                "format": 1, "mediaNumber": "MR-C", "name": "Release C", "version": "1.1.0",
                "files": [_entry("util.lua", B_UTIL_SRC, "util")],
            },
        },
        {"main.tslua": SCHEDULE_SRC, "b_mod.lua": D_MOD_SRC, "util.lua": ""},
    )
    # Each release's own directory holds its own copy of util.lua.
    (root / "a" / "util.lua").write_text(UTIL_SRC, encoding="utf-8")
    (root / "c" / "util.lua").write_text(B_UTIL_SRC, encoding="utf-8")

    by_media = _by_media(root)
    assert all(d.broken is None for d in by_media.values()), {d.media_number: d.broken for d in by_media.values()}
    modules = module_map(by_media["MR-A"])
    assert modules["util"] == root / "a" / "util.lua", "the release's own file wins"
    assert modules["b_mod"] == root / "b" / "b_mod.lua", "dependency modules are in the map too"
    assert len(modules) == 2, "the shared name reached twice is listed once"
    assert _media_chain(by_media["MR-A"]) == ("MR-A", "MR-B", "MR-C")


def test_the_chain_is_walked_depth_first_in_declaration_order(tmp_path):
    """A depends [B, D] and B depends E: E (B's subtree) is searched before D."""
    root = _write_releases(
        tmp_path,
        {
            "a": {
                "format": 1, "mediaNumber": "MR-A", "name": "Release A", "version": "1.0.0",
                "depends": [{"name": "Release B", "version": "^1.0"}, {"name": "Release D", "version": "^1.0"}],
                "files": [_entry("main.tslua", SCHEDULE_SRC)],
            },
            "b": {
                "format": 1, "mediaNumber": "MR-B", "name": "Release B", "version": "1.0.0",
                "depends": [{"name": "Release E", "version": "^1.0"}],
                "files": [],
            },
            "d": {
                "format": 1, "mediaNumber": "MR-D", "name": "Release D", "version": "1.0.0",
                "files": [_entry("shared.lua", D_SHARED_SRC, "shared")],
            },
            "e": {
                "format": 1, "mediaNumber": "MR-E", "name": "Release E", "version": "1.0.0",
                "files": [_entry("shared.lua", E_SHARED_SRC, "shared")],
            },
        },
        {"main.tslua": SCHEDULE_SRC, "shared.lua": ""},
    )
    (root / "d" / "shared.lua").write_text(D_SHARED_SRC, encoding="utf-8")
    (root / "e" / "shared.lua").write_text(E_SHARED_SRC, encoding="utf-8")

    by_media = _by_media(root)
    assert all(d.broken is None for d in by_media.values()), {d.media_number: d.broken for d in by_media.values()}
    assert _media_chain(by_media["MR-A"]) == ("MR-A", "MR-B", "MR-E", "MR-D")
    assert module_map(by_media["MR-A"])["shared"] == root / "e" / "shared.lua", "B's subtree before D"
    # Schedules in a dependency are never modules.
    assert not any(name.endswith(".tslua") for name in module_map(by_media["MR-A"]))


# -- mismatches across the chain --------------------------------------------

def test_a_mismatched_module_in_a_grandchild_is_flagged_before_the_run(qapp, tmp_path):
    """`pending_mismatches` covers the whole chain and names the owning release."""
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
                "files": [_entry("b_mod.lua", D_MOD_SRC, "b_mod")],
            },
            "c": {
                "format": 1, "mediaNumber": "MR-C", "name": "Release C", "version": "1.1.0",
                "files": [{"path": "c_mod.lua", "sha256": WRONG_SHA}],
            },
        },
        {"main.tslua": SCHEDULE_SRC, "b_mod.lua": D_MOD_SRC, "c_mod.lua": C_MOD_MISMATCH_SRC},
    )
    session = HeadlessRunSession(_config(tmp_path, root))
    session.select_schedule("Chain schedule")
    mismatches = session.pending_mismatches()
    assert mismatches == [("c_mod.lua", WRONG_SHA, _sha(C_MOD_MISMATCH_SRC), "MR-C")], mismatches

    session.start(store_mismatched=True)
    assert _finish(qapp, session)
    row = _files(session)["c_mod.lua"]
    assert row["media_number"] == "MR-C" and row["mismatch"] == 1
    assert row["content"] == C_MOD_MISMATCH_SRC.encode("utf-8")
    warnings = [line["text"] for line in session.store.log_lines(session.run_uuid) if "checksum mismatch" in line["text"]]
    assert any("c_mod.lua" in text for text in warnings), warnings
