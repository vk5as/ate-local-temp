"""Test Definition (.atedef) reading and schedule listing for ticket 02.

Ticket 02's slice of the Catalog. It reads a minimal ``.atedef`` (format, mediaNumber,
name, version, files), computes each file's checksum, finds the visible Test Schedules
in the configured directories, and — using one fresh :class:`~ate.engine.lua_host.LuaHost`
per load — returns their names and descriptions for listing.

Full validation (dependency resolution, module mapping, cycle detection) is deferred;
this reader checks only what a run needs so ticket 02 can build against a stable shape.
Sub-schedule expansion is likewise deferred: include/header steps are recognised but not
expanded here, so the runner skips any step that has no action.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .errors import ATEError
from .engine.lua_host import HostError, LuaHost


@dataclass(frozen=True)
class FileEntry:
    """One file listed in a Test Definition."""

    rel_path: str  # path as written in the .atedef (relative to the .atedef dir)
    expected_sha256: Optional[str] = None
    computed_sha256: Optional[str] = None
    is_schedule: bool = False  # True for .tslua files
    hidden: bool = False


@dataclass(frozen=True)
class Definition:
    """A parsed Test Definition."""

    path: Path  # directory holding the .atedef (its parent)
    media_number: str
    name: str
    version: str
    format_version: int = 1
    files: tuple[FileEntry, ...] = field(default_factory=tuple)
    broken: Optional[str] = None  # error text if the definition is unusable


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_definition(atedef_path: str | Path) -> Definition:
    """Read and minimally validate one ``.atedef`` file, computing file checksums.

    Raises :class:`~ate.errors.ATEError` if the definition is broken (unknown format,
    missing required field, a listed file missing on disk, or an unknown extension).
    """
    atedef_path = Path(atedef_path)
    try:
        raw = json.loads(atedef_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ATEError(f".atedef not found: {atedef_path}") from exc
    except json.JSONDecodeError as exc:
        raise ATEError(f"invalid JSON in .atedef {atedef_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ATEError(f"{atedef_path} must be a JSON object")

    fmt = raw.get("format")
    if fmt != 1:
        raise ATEError(f"unknown .atedef format: {fmt!r}")

    media_number = raw.get("mediaNumber")
    name = raw.get("name")
    version = raw.get("version")
    for field_name, value in (("mediaNumber", media_number), ("name", name), ("version", version)):
        if not isinstance(value, str) or value == "":
            raise ATEError(f"{atedef_path}: required '{field_name}' is missing or empty")

    files_raw = raw.get("files", [])
    if not isinstance(files_raw, list):
        raise ATEError(f"{atedef_path}: 'files' must be a list")

    def_dir = atedef_path.parent
    files: list[FileEntry] = []
    for index, entry in enumerate(files_raw):
        if not isinstance(entry, dict) or "path" not in entry or "sha256" not in entry:
            raise ATEError(f"{atedef_path}: files[{index}] needs 'path' and 'sha256'")
        rel = entry["path"]
        expected = entry["sha256"]
        full = def_dir / rel
        if not full.exists():
            raise ATEError(f"{atedef_path}: listed file is missing: {rel}")
        data = full.read_bytes()
        computed = _sha256_bytes(data)
        # A mismatch warns but does not break the definition (see test-definition-format.md).
        if expected != computed:
            print(f"WARNING: checksum mismatch for {rel} (expected {expected}, got {computed})")
        suffix = full.suffix
        is_schedule = suffix == ".tslua"
        files.append(
            FileEntry(
                rel_path=rel,
                expected_sha256=expected,
                computed_sha256=computed,
                is_schedule=is_schedule,
                hidden=bool(entry.get("hidden", False)),
            )
        )

    return Definition(
        path=def_dir,
        media_number=media_number,
        name=name,
        version=version,
        format_version=int(fmt),
        files=tuple(files),
    )


def find_definitions(*directories: str | Path) -> list[Definition]:
    """Recursively scan directories for ``.atedef`` files and read each one."""
    definitions: list[Definition] = []
    for directory in directories:
        root = Path(directory).resolve()
        if not root.exists():
            print(f"WARNING: Test Definition directory does not exist: {root}")
            continue
        for atedef_path in sorted(root.rglob("*.atedef")):
            try:
                definitions.append(read_definition(atedef_path))
            except ATEError as exc:
                # A broken definition is reported but doesn't stop the scan.
                print(f"WARNING: {atedef_path}: {exc}")
    return definitions


def _schedule_summaries(definition: Definition) -> list[tuple[str, str, str]]:
    """Load each visible Test Schedule and read its name/description/media number.

    One fresh LuaHost per load (see the host module). A schedule that fails to load is
    skipped; a missing name is reported as broken so it isn't offered to pick from.
    """
    summaries: list[tuple[str, str, str]] = []
    for file_entry in definition.files:
        if not file_entry.is_schedule or file_entry.hidden:
            continue
        full = definition.path / file_entry.rel_path
        try:
            host = LuaHost(full)
            schedule_name = host.schedule.name
            description = host.schedule.description
        except HostError as exc:
            print(f"WARNING: {file_entry.rel_path}: {exc}")
            continue
        summaries.append((schedule_name, description, definition.media_number))
    return summaries


def list_schedules(*directories: str | Path) -> list[tuple[str, str, str]]:
    """Return ``(name, description, mediaNumber)`` for every visible Test Schedule.

    The flat list a layout shows the Operator to pick from — one entry per visible
    schedule across all Media Releases in the configured directories.
    """
    definitions = find_definitions(*directories)
    summaries: list[tuple[str, str, str]] = []
    for definition in definitions:
        summaries.extend(_schedule_summaries(definition))
    return summaries


def resolve_schedule_path(definition: Definition, schedule_name: str) -> Path | None:
    """Find the ``.tslua`` file whose loaded ScheduleParams name matches ``schedule_name``.

    Returns ``None`` if no visible schedule with that name is listed in this release.
    """
    for file_entry in definition.files:
        if not file_entry.is_schedule or file_entry.hidden:
            continue
        full = definition.path / file_entry.rel_path
        try:
            host = LuaHost(full)
        except HostError:
            continue
        if host.schedule.name == schedule_name:
            return full
    return None


def resolve_schedule(*directories: str | Path, schedule_name: str) -> Path | None:
    """Return the ``.tslua`` path whose loaded name matches ``schedule_name``.

    Searches each directory in declaration order and returns the first match; ``None``
    if no visible schedule with that name is listed anywhere. Used by the run session to
    turn an Operator's selection into a file to load.
    """
    for definition in find_definitions(*directories):
        path = resolve_schedule_path(definition, schedule_name)
        if path is not None:
            return path
    return None
