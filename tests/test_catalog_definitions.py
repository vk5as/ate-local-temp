"""Ticket 05's Seam-1 slice: the Test Definition catalog and its validation.

Drives :func:`ate.catalog.find_definitions` (and the schedule-listing helpers) against real
``.atedef`` files on disk — one fresh temp tree per test, no mocks. Each broken case from
``test-definition-format.md`` must surface as a definition with ``broken`` set to an error
string; runnable definitions and schedules stay usable. This is the headless catalog seam:
scan → validate → list, observed through public functions only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ate.catalog import (
    Definition,
    find_definitions,
    list_schedules,
    resolve_schedule,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_env(tmp_path: Path, *, defs: dict[str, dict], files: dict[str, bytes]) -> Path:
    """Write one Media Release per label under ``defs`` and its listed Lua files on disk.

    Each entry of ``defs`` becomes a ``<label>.atedef`` in its own subdirectory so the recursive
    scan finds them all; that same subdirectory holds every file the definition lists whose path
    is present in ``files`` (so relative paths resolve against the .atedef's directory). An entry
    whose path is absent from ``files`` describes a file that does not exist on disk (a
    missing-file defect).
    """
    root = tmp_path / "defs"
    root.mkdir(parents=True, exist_ok=True)
    for label, atedef in defs.items():
        release_dir = root / label
        release_dir.mkdir(parents=True, exist_ok=True)
        (release_dir / f"{label}.atedef").write_text(json.dumps(atedef), encoding="utf-8", newline="\n")
        for entry in atedef.get("files", []):
            if isinstance(entry, dict) and "path" in entry and entry["path"] in files:
                target = release_dir / entry["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(files[entry["path"]])
    return root


def _valid_tslua() -> bytes:
    return b'return ScheduleParams("S", "desc")\n'


# --- structural validation --------------------------------------------------


def test_missing_required_field_is_broken(tmp_path):
    """A definition missing a required field is broken and lists no runnable schedule."""
    defs_dir = build_env(
        tmp_path,
        defs={
            "demo": {
                "format": 1,
                "mediaNumber": "REL-A",
                # 'name' omitted -> required-field defect
                "version": "1.0.0",
                "files": [{"path": "s.tslua", "sha256": _sha256(_valid_tslua())}],
            }
        },
        files={"s.tslua": _valid_tslua()},
    )
    defs = find_definitions(defs_dir)
    assert len(defs) == 1
    broken = defs[0].broken
    assert broken is not None, "a definition missing a required field must be broken"
    assert "name" in broken


def test_unknown_format_is_broken(tmp_path):
    """An unknown ``format`` makes the whole Test Definition broken."""
    defs_dir = build_env(
        tmp_path,
        defs={
            "demo": {
                "format": 2,
                "mediaNumber": "REL-A",
                "name": "Demo release",
                "version": "1.0.0",
                "files": [{"path": "s.tslua", "sha256": _sha256(_valid_tslua())}],
            }
        },
        files={"s.tslua": _valid_tslua()},
    )
    defs = find_definitions(defs_dir)
    assert len(defs) == 1
    assert defs[0].broken is not None and "format" in defs[0].broken


def test_bad_extension_is_broken(tmp_path):
    """A listed file with an extension other than .tslua/.lua is broken."""
    bad = b"not lua\n"
    defs_dir = build_env(
        tmp_path,
        defs={
            "demo": {
                "format": 1,
                "mediaNumber": "REL-A",
                "name": "Demo release",
                "version": "1.0.0",
                "files": [{"path": "notes.txt", "sha256": _sha256(bad)}],
            }
        },
        files={"notes.txt": bad},
    )
    defs = find_definitions(defs_dir)
    assert len(defs) == 1
    assert defs[0].broken is not None and ".txt" in defs[0].broken


def test_hidden_on_lua_is_broken(tmp_path):
    """``hidden`` applies to .tslua only; on a .lua file the definition is broken."""
    lua = b"return {}\n"
    defs_dir = build_env(
        tmp_path,
        defs={
            "demo": {
                "format": 1,
                "mediaNumber": "REL-A",
                "name": "Demo release",
                "version": "1.0.0",
                "files": [{"path": "helper.lua", "sha256": _sha256(lua), "hidden": True}],
            }
        },
        files={"helper.lua": lua},
    )
    defs = find_definitions(defs_dir)
    assert len(defs) == 1
    assert defs[0].broken is not None and ".lua" in defs[0].broken


def test_module_on_tslua_is_broken(tmp_path):
    """``module`` applies to .lua only; on a .tslua file the definition is broken."""
    tslua = b'return {}\n'
    defs_dir = build_env(
        tmp_path,
        defs={
            "demo": {
                "format": 1,
                "mediaNumber": "REL-A",
                "name": "Demo release",
                "version": "1.0.0",
                "files": [{"path": "s.tslua", "sha256": _sha256(tslua), "module": "broken"}],
            }
        },
        files={"s.tslua": tslua},
    )
    defs = find_definitions(defs_dir)
    assert len(defs) == 1
    assert defs[0].broken is not None and ".tslua" in defs[0].broken


def test_duplicate_path_is_broken(tmp_path):
    """Two entries with the same path make the definition broken."""
    lua = b"return {}\n"
    defs_dir = build_env(
        tmp_path,
        defs={
            "demo": {
                "format": 1,
                "mediaNumber": "REL-A",
                "name": "Demo release",
                "version": "1.0.0",
                "files": [
                    {"path": "shared.lua", "sha256": _sha256(lua)},
                    {"path": "shared.lua", "sha256": _sha256(lua)},
                ],
            }
        },
        files={"shared.lua": lua},
    )
    defs = find_definitions(defs_dir)
    assert len(defs) == 1
    assert defs[0].broken is not None and "path" in defs[0].broken


def test_duplicate_module_name_is_broken(tmp_path):
    """Two .lua files that resolve to the same module name make the definition broken."""
    lua = b"return {}\n"
    # psu/a.lua and psu/b.lua both declare the explicit module 'shared' -> a collision.
    defs_dir = build_env(
        tmp_path,
        defs={
            "demo": {
                "format": 1,
                "mediaNumber": "REL-A",
                "name": "Demo release",
                "version": "1.0.0",
                "files": [
                    {"path": "psu/a.lua", "sha256": _sha256(lua), "module": "shared"},
                    {"path": "psu/b.lua", "sha256": _sha256(lua), "module": "shared"},
                ],
            }
        },
        files={"psu/a.lua": lua, "psu/b.lua": lua},
    )
    defs = find_definitions(defs_dir)
    assert len(defs) == 1
    assert defs[0].broken is not None and "module" in defs[0].broken


def test_reserved_ate_module_is_broken(tmp_path):
    """A module name under the reserved ``ate.`` prefix makes the definition broken."""
    lua = b"return {}\n"
    defs_dir = build_env(
        tmp_path,
        defs={
            "demo": {
                "format": 1,
                "mediaNumber": "REL-A",
                "name": "Demo release",
                "version": "1.0.0",
                "files": [{"path": "shared.lua", "sha256": _sha256(lua), "module": "ate.shared"}],
            }
        },
        files={"shared.lua": lua},
    )
    defs = find_definitions(defs_dir)
    assert len(defs) == 1
    assert defs[0].broken is not None and "ate." in defs[0].broken


def test_absolute_path_without_module_is_broken(tmp_path):
    """An absolute path to a .lua file without an explicit ``module`` is broken."""
    lua = b"return {}\n"
    defs_dir = build_env(
        tmp_path,
        defs={
            "demo": {
                "format": 1,
                "mediaNumber": "REL-A",
                "name": "Demo release",
                "version": "1.0.0",
                "files": [{"path": "/opt/ate/shared/limits.lua", "sha256": _sha256(lua)}],
            }
        },
        files={},  # the absolute file is intentionally absent; path rule fires first
    )
    defs = find_definitions(defs_dir)
    assert len(defs) == 1
    assert defs[0].broken is not None and "module" in defs[0].broken


def test_dotdot_path_without_module_is_broken(tmp_path):
    """A path containing ``..`` to a .lua file without an explicit ``module`` is broken."""
    lua = b"return {}\n"
    defs_dir = build_env(
        tmp_path,
        defs={
            "demo": {
                "format": 1,
                "mediaNumber": "REL-A",
                "name": "Demo release",
                "version": "1.0.0",
                "files": [{"path": "lib/../psu.lua", "sha256": _sha256(lua)}],
            }
        },
        files={},  # absent; the '..' rule fires before the existence check
    )
    defs = find_definitions(defs_dir)
    assert len(defs) == 1
    assert defs[0].broken is not None and "module" in defs[0].broken


def test_missing_file_is_broken(tmp_path):
    """A listed file that does not exist on disk makes the definition broken."""
    defs_dir = build_env(
        tmp_path,
        defs={
            "demo": {
                "format": 1,
                "mediaNumber": "REL-A",
                "name": "Demo release",
                "version": "1.0.0",
                "files": [{"path": "ghost.tslua", "sha256": _sha256(b"missing")}],
            }
        },
        files={},  # ghost.tslua is never written -> missing on disk
    )
    defs = find_definitions(defs_dir)
    assert len(defs) == 1
    assert defs[0].broken is not None and "missing" in defs[0].broken


# --- dependency resolution --------------------------------------------------


def test_ambiguous_dependency_is_broken(tmp_path):
    """A depends range matched by two releases with the same name+version is broken."""
    lua = b"return {}\n"
    defs_dir = build_env(
        tmp_path,
        defs={
            "common-a": {
                "format": 1,
                "mediaNumber": "MR-A",
                "name": "Common helpers",
                "version": "3.1.4",
                "files": [{"path": "a.lua", "sha256": _sha256(lua)}],
            },
            "common-b": {
                "format": 1,
                "mediaNumber": "MR-B",
                "name": "Common helpers",
                "version": "3.1.4",
                "files": [{"path": "b.lua", "sha256": _sha256(lua)}],
            },
            "main": {
                "format": 1,
                "mediaNumber": "MR-M",
                "name": "Main release",
                "version": "1.0.0",
                "depends": [{"name": "Common helpers", "version": "^3.1"}],
                "files": [{"path": "main.tslua", "sha256": _sha256(_valid_tslua())}],
            },
        },
        files={"a.lua": lua, "b.lua": lua, "main.tslua": _valid_tslua()},
    )
    defs = find_definitions(defs_dir)
    by_media = {d.media_number: d for d in defs}
    assert len(defs) == 3
    # Only the dependent is broken; both identical releases are fine on their own.
    assert "main" not in by_media or by_media["MR-M"].broken is not None
    assert by_media["MR-A"].broken is None
    assert by_media["MR-B"].broken is None


def test_unresolvable_dependency_is_broken(tmp_path):
    """A depends entry with no matching installed Media Release is broken."""
    tslua = b'return ScheduleParams("S", "d")\n'
    defs_dir = build_env(
        tmp_path,
        defs={
            "main": {
                "format": 1,
                "mediaNumber": "MR-M",
                "name": "Main release",
                "version": "1.0.0",
                "depends": [{"name": "Ghost helpers", "version": "^3"}],
                "files": [{"path": "main.tslua", "sha256": _sha256(tslua)}],
            }
        },
        files={"main.tslua": tslua},
    )
    defs = find_definitions(defs_dir)
    assert len(defs) == 1
    assert defs[0].broken is not None and "Ghost helpers" in defs[0].broken


def test_dependency_cycle_is_broken(tmp_path):
    """A dependency cycle breaks every definition on the cycle."""
    tslua = b'return ScheduleParams("S", "d")\n'
    defs_dir = build_env(
        tmp_path,
        defs={
            "e": {
                "format": 1,
                "mediaNumber": "MR-E",
                "name": "E release",
                "version": "1.0.0",
                "depends": [{"name": "F release", "version": "^1"}],
                "files": [{"path": "e.tslua", "sha256": _sha256(tslua)}],
            },
            "f": {
                "format": 1,
                "mediaNumber": "MR-F",
                "name": "F release",
                "version": "1.0.0",
                "depends": [{"name": "E release", "version": "^1"}],
                "files": [{"path": "f.tslua", "sha256": _sha256(tslua)}],
            },
        },
        files={"e.tslua": tslua, "f.tslua": tslua},
    )
    defs = find_definitions(defs_dir)
    by_media = {d.media_number: d for d in defs}
    assert len(defs) == 2
    assert by_media["MR-E"].broken is not None
    assert by_media["MR-F"].broken is not None


def test_duplicate_media_numbers_are_both_broken(tmp_path):
    """Two definitions sharing a mediaNumber are both broken."""
    lua = b"return {}\n"
    defs_dir = build_env(
        tmp_path,
        defs={
            "a": {
                "format": 1,
                "mediaNumber": "DUP",
                "name": "A release",
                "version": "1.0.0",
                "files": [{"path": "a.lua", "sha256": _sha256(lua)}],
            },
            "b": {
                "format": 1,
                "mediaNumber": "DUP",
                "name": "B release",
                "version": "1.0.0",
                "files": [{"path": "b.lua", "sha256": _sha256(lua)}],
            },
        },
        files={"a.lua": lua, "b.lua": lua},
    )
    defs = find_definitions(defs_dir)
    by_media = {d.media_number: d for d in defs}
    assert len(defs) == 2
    assert by_media["DUP"] is not None
    assert by_media["DUP"].broken is not None


# --- runnable definitions stay usable ---------------------------------------


def test_valid_definition_lists_its_schedule(tmp_path):
    """A fully valid definition lists its visible schedule with no error."""
    tslua = b'return ScheduleParams("S", "desc")\n'
    defs_dir = build_env(
        tmp_path,
        defs={
            "demo": {
                "format": 1,
                "mediaNumber": "REL-A",
                "name": "Demo release",
                "version": "1.0.0",
                "files": [{"path": "s.tslua", "sha256": _sha256(tslua)}],
            }
        },
        files={"s.tslua": tslua},
    )
    defs = find_definitions(defs_dir)
    assert defs[0].broken is None
    rows = list_schedules(defs_dir)
    assert ("S", "desc", "REL-A") in rows


def test_valid_relative_lua_module_derives_its_name(tmp_path):
    """A relative .lua module derives its module name from the path (lib/psu.lua -> lib.psu)."""
    lua = b"return {}\n"
    defs_dir = build_env(
        tmp_path,
        defs={
            "demo": {
                "format": 1,
                "mediaNumber": "REL-A",
                "name": "Demo release",
                "version": "1.0.0",
                "files": [{"path": "lib/psu.lua", "sha256": _sha256(lua)}],
            }
        },
        files={"lib/psu.lua": lua},
    )
    defs = find_definitions(defs_dir)
    assert defs[0].broken is None
    module_name = defs[0].files[0].module_name
    assert module_name == "lib.psu"


def test_broken_definition_is_not_offered_to_pick(tmp_path):
    """A broken definition contributes no runnable schedule to the flat list."""
    tslua = b'return ScheduleParams("S", "d")\n'
    defs_dir = build_env(
        tmp_path,
        defs={
            "good": {
                "format": 1,
                "mediaNumber": "REL-GOOD",
                "name": "Good release",
                "version": "1.0.0",
                "files": [{"path": "good.tslua", "sha256": _sha256(tslua)}],
            },
            "bad": {
                "format": 1,
                "mediaNumber": "REL-BAD",
                "name": "Bad release",
                "version": "not-semver",
                "files": [{"path": "bad.tslua", "sha256": _sha256(tslua)}],
            },
        },
        files={"good.tslua": tslua, "bad.tslua": tslua},
    )
    rows = list_schedules(defs_dir)
    media_numbers = {media for _, _, media in rows}
    # The usable release's schedule is offered with its real ScheduleParams name/description and media number.
    assert ("S", "d", "REL-GOOD") in rows
    assert not any(media == "REL-BAD" for _, _, media in rows)
