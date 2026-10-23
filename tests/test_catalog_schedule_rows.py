"""Ticket 05's broken-schedule seam (list_schedule_rows / list_schedules).

A Test Schedule that raises an error while loading is listed as broken with that error, and the rest
of its Media Release stays usable — per ``test-definition-format.md`` "Finding and choosing Test
Schedules". Each load uses a fresh :class:`~ate.engine.lua_host.LuaHost`, so this exercises real Lua
execution (not widgets), which keeps it headless under offscreen Qt.
"""

import hashlib
import json
from pathlib import Path

from ate.catalog import list_schedules, list_schedule_rows


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_env(tmp_path: Path, *, defs: dict[str, dict], files: dict[str, bytes]) -> Path:
    """Write one Media Release per label; each lists its ``.tslua`` schedules on disk."""
    root = tmp_path / "defs"
    root.mkdir(parents=True, exist_ok=True)
    for label, atedef in defs.items():
        release_dir = root / label
        release_dir.mkdir(parents=True, exist_ok=True)
        (release_dir / f"{label}.atedef").write_text(json.dumps(atedef), encoding="utf-8")
        for entry in atedef.get("files", []):
            if isinstance(entry, dict) and "path" in entry and entry["path"] in files:
                target = release_dir / entry["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(files[entry["path"]])
    return root


def test_broken_schedule_is_listed_with_its_error_and_release_stays_usable(tmp_path):
    """One .tslua that raises while loading is a broken row; its sibling schedule in the same release runs."""
    good = b'return ScheduleParams("Good", "desc")\n'
    bad = b'return {\n'  # unclosed table -> a Lua syntax error raised while loading
    defs_dir = build_env(
        tmp_path,
        defs={
            "demo": {
                "format": 1,
                "mediaNumber": "REL-X",
                "name": "Demo release",
                "version": "1.0.0",
                "files": [
                    {"path": "good.tslua", "sha256": _sha256(good)},
                    {"path": "bad.tslua", "sha256": _sha256(bad)},
                ],
            }
        },
        files={"good.tslua": good, "bad.tslua": bad},
    )

    rows = list_schedule_rows(defs_dir)
    assert len(rows) == 2

    runnable = [r for r in rows if r.error is None]
    broken = [r for r in rows if r.error is not None]

    # The good schedule runs with its ScheduleParams name/description and media number.
    assert ("Good", "desc", "REL-X") in [(r.name, r.description, r.media_number) for r in runnable]
    # The raising schedule is listed broken with a non-empty error; the release still has a usable row.
    assert len(broken) == 1 and broken[0].media_number == "REL-X" and bool(broken[0].error)


def test_flat_list_excludes_the_raising_schedule(tmp_path):
    """The runnable flat list (list_schedules) lists only the schedule that loaded; the raising one is omitted."""
    good = b'return ScheduleParams("Good", "desc")\n'
    bad = b'return {\n'  # raises while loading -> not offered to pick from
    defs_dir = build_env(
        tmp_path,
        defs={
            "demo": {
                "format": 1,
                "mediaNumber": "REL-X",
                "name": "Demo release",
                "version": "1.0.0",
                "files": [
                    {"path": "good.tslua", "sha256": _sha256(good)},
                    {"path": "bad.tslua", "sha256": _sha256(bad)},
                ],
            }
        },
        files={"good.tslua": good, "bad.tslua": bad},
    )

    summaries = list_schedules(defs_dir)  # returns (name, description, mediaNumber) tuples
    assert ("Good", "desc", "REL-X") in summaries
    # Only the runnable schedule is listed; the one that raised while loading is excluded.
    assert len(summaries) == 1


def test_broken_media_release_is_a_single_error_row(tmp_path):
    """A structurally broken Media Release appears once as an error row and offers no runnable schedule."""
    defs_dir = build_env(
        tmp_path,
        defs={
            "bad": {
                "format": 1,
                "mediaNumber": "REL-BAD",
                # 'name' omitted -> required-field defect breaks the whole release.
                "version": "1.0.0",
                "files": [{"path": "s.tslua", "sha256": _sha256(b'return ScheduleParams("S","d")\n')}],
            }
        },
        files={"s.tslua": b'return ScheduleParams("S","d")\n'},
    )

    rows = list_schedule_rows(defs_dir)
    assert len(rows) == 1
    row = rows[0]
    assert row.media_number == "REL-BAD"
    assert row.error is not None and "name" in row.error
