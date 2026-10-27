"""Test Definition (.atedef) reading, validation and schedule listing for ticket 05.

This is the Catalog layer: it recursively scans every configured Test Definition directory
for ``.atedef`` files, fully validates each one (fields, format, semver version, file roles,
paths, module names, duplicates, reserved names, missing files) and resolves their dependencies
(npm-style semver ranges, transitively, with cycle and ambiguity detection). It then loads every
visible Test Schedule in its own fresh :class:`~ate.engine.lua_host.LuaHost` to read the schedule's
name/description for one flat picker list — a schedule that raises while loading is surfaced as
broken with its error while the rest of its Media Release stays usable.

Validation and dependency resolution are deliberately UI-free so they can be exercised headless;
the run session and layouts observe them through these functions only. Real Lua execution is not
implemented here (ticket 02's stub host runs canned actions); this module never imports widgets or
any device driver.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .errors import ATEError
from .engine.lua_host import HostError, LuaHost


@dataclass
class FileEntry:
    """One file listed in a Test Definition."""

    rel_path: str  # path as written in the .atedef (relative to the .atedef dir)
    expected_sha256: Optional[str] = None
    computed_sha256: Optional[str] = None
    is_schedule: bool = False  # True for .tslua files
    hidden: bool = False
    module_name: Optional[str] = None  # derived or explicit name, meaningful only for .lua files


@dataclass
class Definition:
    """A parsed Test Definition.

    ``broken`` carries an error string when the definition is unusable (a structural defect or a
    problem in its resolved dependency chain); ``None`` means it is usable. ``dependencies`` holds
    each raw ``(name, range)`` entry so dependency resolution can walk them without re-reading the
    file; it is set by :func:`read_definition`. ``chain`` is this release followed by the releases it
    depends on, resolved transitively depth-first in declaration order, each listed once; it is empty
    until :func:`resolve_dependencies` runs, and stays empty for a broken release.
    """

    path: Path  # directory holding the .atedef (its parent)
    media_number: str
    name: str
    version: str
    format_version: int = 1
    files: tuple[FileEntry, ...] = field(default_factory=tuple)
    dependencies: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    broken: Optional[str] = None
    atedef_path: Optional[Path] = None  # the .atedef file itself (packet 06b records its SHA-256)
    chain: tuple[Definition, ...] = field(default_factory=tuple)  # this release, then its resolved dependencies depth-first (packet 07b)


# --- semver (RFC 2.0.0) helpers ---------------------------------------------


_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
_PARTIAL_SEMVER_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
_COMPARATOR_PART_RE = re.compile(
    r"((?:>=|<=|>|<|=|\^|~))?\s*(\d+(?:\.\d+)?(?:\.\d+)?(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)"
)


def parse_version(text: str) -> Optional[tuple[int, int, int, tuple]]:
    """Parse a semver 2.0.0 string into ``(major, minor, patch, prerelease-tuple)`` or ``None``."""
    if not isinstance(text, str):
        return None
    match = _SEMVER_RE.match(text.strip())
    if not match:
        return None
    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
    prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
    return (major, minor, patch, prerelease)


def _version_key(version: tuple[int, int, int, tuple]):
    """A sort key for versions so :func:`max` picks the highest, by the same precedence as
    :func:`_compare` (semver 2.0.0 §11): a release outranks its pre-releases, numeric identifiers
    compare numerically and rank below alphanumeric ones, and a shorter identifier list ranks
    lower when all preceding identifiers are equal."""
    return functools.cmp_to_key(_compare)(version)


def _compare(a: tuple[int, int, int, tuple], b: tuple[int, int, int, tuple]) -> int:
    """Return -1, 0 or 1 comparing two parsed versions by semver precedence."""
    for i in range(3):
        if a[i] != b[i]:
            return -1 if a[i] < b[i] else 1
    a_pre, b_pre = a[3], b[3]
    if not a_pre and not b_pre:
        return 0
    if not a_pre:
        return 1  # a release outranks the same version's pre-release
    if not b_pre:
        return -1
    for x, y in zip(a_pre, b_pre):
        xi = int(x) if x.isdigit() else None
        yi = int(y) if y.isdigit() else None
        if xi is None and yi is None:
            if x != y:
                return -1 if x < y else 1
        elif xi is not None and yi is not None:
            if xi != yi:
                return -1 if xi < yi else 1
        else:
            # A numeric identifier always ranks below a non-numeric one (§11.4.3).
            return 1 if xi is None else -1
    return 0 if len(a_pre) == len(b_pre) else (-1 if len(a_pre) < len(b_pre) else 1)


def _satisfies(op: str, comparison: int) -> bool:
    """Whether ``comparison`` (from :func:`_compare`) satisfies comparator ``op``."""
    if op == ">=":
        return comparison >= 0
    if op == "<=":
        return comparison <= 0
    if op == ">":
        return comparison > 0
    if op == "<":
        return comparison < 0
    return comparison == 0  # "="


def _parse_range_version(text: str) -> Optional[tuple[int, int, int, tuple]]:
    """Parse a range bound: a full semver string or a partial one (``3`` or ``3.1``).

    Missing minor/patch components become ``None`` (they pad to 0 at the lower bound); build
    metadata is ignored, matching the packet's rule. A string that is not semver at all returns
    ``None``.
    """
    match = _PARTIAL_SEMVER_RE.match(text)
    if not match:
        return None
    minor = int(match.group(2)) if match.group(2) is not None else None
    patch = int(match.group(3)) if match.group(3) is not None else None
    prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
    return (int(match.group(1)), minor, patch, prerelease)


def _expand_caret_tilde(op: str, bound: tuple[int, int, int, tuple]) -> list[tuple[str, tuple]]:
    """Expand a caret/tilde range into an inclusive lower and exclusive upper comparator.

    ``bound`` is a partial version (``None`` for a missing component), per npm:
    ^X.Y.Z / ^X.Y / ^X := >=X.Y.Z <(X+1).0.0 ; ^0.Y := >=0.Y.0 <0.(Y+1).0 ;
    ^0.0.Z := >=0.0.Z <0.0.(Z+1) ; ^0 / ^0.0 := >=0.0.0 <1.0.0 ;
    ~X.Y.Z / ~X.Y := >=X.Y.0 <X.(Y+1).0 ; ~X := >=X.0.0 <(X+1).0.0.
    """
    major, minor, patch, prerelease = bound
    lower = (major, minor or 0, patch or 0, prerelease)
    if op == "^":
        if major > 0:
            upper = (major + 1, 0, 0)
        elif (minor or 0) > 0:
            upper = (0, (minor or 0) + 1, 0)
        elif patch is not None:
            upper = (0, 0, patch + 1)  # ^0.0.Z keeps the patch
        else:
            upper = (1, 0, 0)  # ^0 and ^0.0
    else:  # "~"
        # ~X.Y.Z / ~X.Y keep the minor; ~X (major only) keeps the major.
        upper = (major, (minor or 0) + 1, 0) if minor is not None else (major + 1, 0, 0)
    upper += ((),)  # the upper bound names no pre-release
    return [(">=", lower), ("<", upper)]


def matches_range(version_text: str, range_text: str) -> bool:
    """Whether ``version_text`` satisfies the npm-style ``range_text``.

    Supports exact versions, comma-joined comparators (``>=3.0 <4``), and caret/tilde ranges
    (``^3.1``, ``~3.1.2``), including partial versions and the npm zero-major caret rules
    (``^0.x`` keeps the minor, ``^0.0.x`` keeps the patch). Build metadata is ignored on both
    sides. A pre-release candidate only matches if the range names a pre-release on the same
    major.minor.patch tuple; an unparseable range or comparator never matches anything.
    """
    version = parse_version(version_text)
    if version is None:
        return False

    # Comparators join with commas or whitespace (>=3.0 <4); parse one comparator at a time.
    comparators: list[tuple[str, tuple]] = []
    text = range_text.strip()
    pos = 0
    if not text:
        return False
    while pos < len(text):
        match = _COMPARATOR_PART_RE.match(text, pos)
        if not match:
            return False
        op = match.group(1) or "="
        bound = _parse_range_version(match.group(2))
        if bound is None:
            return False
        if op in ("^", "~"):
            comparators.extend(_expand_caret_tilde(op, bound))
        else:
            # A comparison bound pads missing components to 0 (>=3.0 means >=3.0.0).
            comparators.append((op, (bound[0], bound[1] or 0, bound[2] or 0, bound[3])))
        pos = match.end()
        while pos < len(text) and text[pos] in " ,\t\n":
            pos += 1

    for op, bound in comparators:
        if not _satisfies(op, _compare(version, bound)):
            return False

    # npm pre-release rule: a candidate with a pre-release only matches a range that names
    # a pre-release on the same major.minor.patch (the identifiers may differ).
    if version[3]:
        named = any(bound[3] and bound[:3] == version[:3] for _, bound in comparators)
        if not named:
            return False
    return True


# --- paths and module names -------------------------------------------------

_RESERVED_PREFIX = "ate."


def _needs_module(rel_path: str) -> bool:
    """Whether a ``.lua`` path needs an explicit ``module`` (absolute, or containing ``..``)."""
    if Path(rel_path).is_absolute():
        return True
    parts = rel_path.split("/")
    if ".." in parts:
        return True
    # Absolute forms not caught by pathlib on POSIX (a Windows drive letter or UNC path) still need a module.
    if re.match(r"^[A-Za-z]:[\\/]", rel_path):
        return True
    if rel_path.startswith("//"):
        return True
    return False


def _derive_module_name(rel_path: str) -> str:
    """The module name derived from a relative ``.lua`` path (``lib/psu.lua`` -> ``lib.psu``)."""
    stem = rel_path[: -len(".lua")] if rel_path.endswith(".lua") else rel_path
    return stem.replace("/", ".")


def _module_is_reserved(module_name: str) -> bool:
    """Names starting with ``ate.`` are reserved for ATE's own bundled modules."""
    return module_name.startswith(_RESERVED_PREFIX)


# --- checksums ---------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- reading and validating one .atedef -------------------------------------


def _empty_definition(atedef_path: Path, error: str) -> Definition:
    """A structurally unusable definition with no usable fields (e.g. unparseable JSON)."""
    return Definition(path=Path(atedef_path).parent, media_number="", name="", version="", atedef_path=atedef_path)


def read_definition(atedef_path: str | Path) -> Definition:
    """Read and fully validate one ``.atedef`` file, computing each file's checksum.

    A broken definition is never raised on for a structural defect: instead it is returned with
    :attr:`Definition.broken` set to an error string so the scan can list it with its error (see
    ``test-definition-format.md`` Broken Test Definitions). Only a truly unreadable file or one that
    is not JSON at all yields such an unusable definition. A checksum mismatch warns but does not break
    the definition (see ``test-definition-format.md`` Checksums).
    """
    atedef_path = Path(atedef_path)

    try:
        raw = json.loads(atedef_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ATEError(f".atedef not found: {atedef_path}") from exc
    except OSError as exc:
        raise ATEError(f"could not read .atedef {atedef_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        return _empty_definition(atedef_path, f"invalid JSON in .atedef {atedef_path}: {exc}")

    if not isinstance(raw, dict):
        return _empty_definition(atedef_path, f"{atedef_path} must be a JSON object")

    def_dir = atedef_path.parent
    fmt = raw.get("format")
    media_number = raw.get("mediaNumber")
    name = raw.get("name")
    version = raw.get("version")
    dependencies_raw = raw.get("depends", []) or []

    definition = Definition(
        path=def_dir,
        media_number=str(media_number),
        name=str(name),
        version=str(version),
        format_version=int(fmt) if isinstance(fmt, int) else 1,
        files=(),
        dependencies=tuple(),
        atedef_path=atedef_path,
    )

    def broken(message: str) -> Definition:
        """Mark this definition unusable with ``message`` and return it (a structural defect)."""
        definition.broken = message
        return definition

    # An unknown format makes the whole Test Definition broken.
    if fmt != 1:
        return broken(f"unknown .atedef format: {fmt!r}")

    for field_name, value in (("mediaNumber", media_number), ("name", name), ("version", version)):
        if not isinstance(value, str) or value == "":
            return broken(f"required '{field_name}' is missing or empty")

    parsed_version = parse_version(version)
    if parsed_version is None:
        return broken(f"'{version}' is not valid semver 2.0.0")

    files_raw = raw.get("files", [])
    if not isinstance(files_raw, list):
        return broken("'files' must be a list")

    dependencies: list[tuple[str, str]] = []
    for index, dep in enumerate(dependencies_raw):
        if not isinstance(dep, dict) or "name" not in dep or "version" not in dep:
            return broken(f"depends[{index}] needs 'name' and 'version'")
        dependencies.append((str(dep["name"]), str(dep["version"])))

    files: list[FileEntry] = []
    seen_paths: set[str] = set()
    seen_modules: set[str] = set()
    for index, entry in enumerate(files_raw):
        if not isinstance(entry, dict) or "path" not in entry or "sha256" not in entry:
            return broken(f"files[{index}] needs 'path' and 'sha256'")

        rel = entry["path"]
        expected = entry["sha256"]
        suffix = Path(rel).suffix

        # Extension must be .tslua (a schedule) or .lua (a module); anything else is broken.
        if suffix not in (".tslua", ".lua"):
            return broken(f"unknown file extension '{suffix}' for {rel}")

        is_schedule = suffix == ".tslua"

        # ``hidden`` applies to .tslua only; ``module`` applies to .lua only.
        if entry.get("hidden") and not is_schedule:
            return broken(f"'hidden' is only valid on .tslua files ({rel})")
        if entry.get("module") and is_schedule:
            return broken(f"'module' is only valid on .lua files ({rel})")

        module_name = None
        # An absolute or '..' path needs an explicit ``module``; otherwise the name is derived.
        if not is_schedule and _needs_module(rel):
            if entry.get("module"):
                module_name = str(entry["module"])
            else:
                return broken(f"{rel} has an absolute or '..' path but no 'module'")
        elif entry.get("module"):
            module_name = str(entry["module"])
        else:
            module_name = _derive_module_name(rel) if not is_schedule else None

        # Names under the reserved ``ate.`` prefix belong to ATE's own bundled modules.
        if module_name is not None and _module_is_reserved(module_name):
            return broken(f"'{module_name}' is a reserved module name")

        # Two entries with the same path, or two files deriving/explicitly to the same module name, are broken.
        if rel in seen_paths:
            return broken(f"duplicate path '{rel}' listed more than once")
        if module_name is not None and module_name in seen_modules:
            return broken(f"duplicate module name '{module_name}'")
        seen_paths.add(rel)
        if module_name is not None:
            seen_modules.add(module_name)

        full = rel if Path(rel).is_absolute() else def_dir / rel
        if not full.exists():
            return broken(f"listed file is missing: {rel}")
        data = full.read_bytes()
        computed = _sha256_bytes(data)
        # A mismatch warns but does not break the definition (see test-definition-format.md).
        if expected != computed:
            print(f"WARNING: checksum mismatch for {rel} (expected {expected}, got {computed})")

        files.append(
            FileEntry(
                rel_path=rel,
                expected_sha256=expected,
                computed_sha256=computed,
                is_schedule=is_schedule,
                hidden=bool(entry.get("hidden", False)),
                module_name=module_name,
            )
        )

    definition.files = tuple(files)
    definition.dependencies = tuple(dependencies)
    return definition


# --- dependency resolution --------------------------------------------------


def resolve_dependencies(definitions: list[Definition]) -> None:
    """Resolve each definition's ``depends`` chain and mark cross-definition defects in place.

    Builds a registry of Media Releases by name (for matching) and media number, flags duplicate
    media numbers, resolves every range to the highest matching release, and detects every intrinsic
    defect: ambiguous ranges (two releases with the same name+version), dependency cycles, and
    module names provided by more than one Media Release of a resolved chain. Only then does it
    propagate "broken dependency" up the chain, repeatedly, so a broken release breaks everything
    above it. Every defect sets ``Definition.broken``; runnable definitions are left untouched.
    """
    # Registry of installed Media Releases for matching depends entries. A broken release still
    # matches (with a readable name and version), so its dependents are told it is a broken
    # dependency rather than that nothing is installed.
    by_name: dict[str, list[tuple[str, Definition]]] = {}
    for definition in definitions:
        if definition.name and isinstance(definition.version, str) and parse_version(definition.version):
            by_name.setdefault(definition.name, []).append((definition.version, definition))

    # Registry of structurally-valid Media Releases (with a real media number) for matching depends entries and collision checks.
    by_media = {d.media_number: d for d in definitions if d.media_number}
    # Two definitions with the same mediaNumber are both broken (media number is a unique identity); only Media Releases with a real media number can be matched or flagged as duplicates.
    seen_media: dict[str, list[Definition]] = {}
    for definition in definitions:
        if not definition.media_number:
            continue
        seen_media.setdefault(definition.media_number, []).append(definition)
    for media_number, group in seen_media.items():
        if len(group) > 1:
            for definition in group:
                _set_broken(definition, f"duplicate mediaNumber '{media_number}'")

    def resolve(media_number: str, path: list[str]) -> None:
        """Resolve one Media Release's dependency chain; ``path`` is the DFS stack (for cycles).

        The chain is the release itself, then each ``depends`` entry in declaration order depth-first
        with that dependency's own resolved chain, and a release reached twice keeps its first
        position (packet 07b)."""
        definition = by_media.get(media_number)
        if not definition or definition.broken:
            return

        chain: list[Definition] = [definition]  # own release first, then dependencies depth-first
        seen = {media_number}
        for dep_name, range_text in definition.dependencies:
            candidates = by_name.get(dep_name, [])
            matches = [(v, other) for (v, other) in candidates if matches_range(v, range_text)]
            versions = [v for v, _ in matches]

            if not matches:
                _set_broken(definition, f"unresolved dependency '{dep_name}' ({range_text})")
                return
            if len(versions) != len(set(versions)):
                # Two releases with the same name and version both satisfy the range -> ambiguous.
                _set_broken(definition, f"ambiguous dependency '{dep_name}' matched by {range_text}")
                return

            chosen = max(matches, key=lambda item: _version_key(parse_version(item[0])))
            dep_media = chosen[1].media_number
            if dep_media in path:
                # The dependency is already an ancestor of this release -> a cycle.
                start = path.index(dep_media)
                cyclic = [by_media[m] for m in path[start:]] + [definition]
                for member in cyclic:
                    _set_broken(member, f"dependency cycle involving '{dep_name}'")
                return

            if dep_media not in seen:  # a diamond: keep the first position it was reached at
                seen.add(dep_media)
                chain.append(chosen[1])
            resolve(dep_media, path + [dep_media])
            for member in getattr(chosen[1], "chain", ()):  # transitive: the dependency's own chain
                if member.media_number in seen:
                    continue
                seen.add(member.media_number)
                chain.append(member)

        definition.chain = tuple(chain)
        definition._chain = tuple(d.media_number for d in chain)  # media numbers, for the module-collision check

    for definition in definitions:
        if not definition.broken:
            resolve(definition.media_number, [definition.media_number])

    # A module name provided by more than one Media Release of a resolved chain breaks the
    # definition. Intrinsic defects are all found first, so a collision never hides a broken
    # dependency further up the chain (packet 07c). A chain already holding a broken release is
    # left to propagation, so the message names that release, not a collision with it.
    broken_before = {id(d) for d in definitions if d.broken}
    for definition in definitions:
        if definition.broken or any(id(member) in broken_before for member in getattr(definition, "chain", ())):
            continue
        providers: dict[str, set[str]] = {}
        for member in getattr(definition, "chain", ()):
            for file_entry in member.files:
                if file_entry.is_schedule or not file_entry.module_name:
                    continue
                providers.setdefault(file_entry.module_name, set()).add(member.media_number)

        collisions = {name: medias for name, medias in providers.items() if len(medias) > 1}
        if collisions:
            detail = "; ".join(
                f"{name} ({', '.join(sorted(medias))})" for name, medias in sorted(collisions.items())
            )
            _set_broken(definition, f"module name collision across Media Releases: {detail}")

    # A broken release breaks every release whose chain contains it, and so on up the chain.
    # Shortest chains first so each definition is broken by the closest broken member of its
    # chain, and repeat until nothing new breaks. A definition keeps its own first error.
    by_depth = sorted(definitions, key=lambda definition: len(getattr(definition, "chain", ())))
    changed = True
    while changed:
        changed = False
        for definition in by_depth:
            if definition.broken:
                continue
            broken_member = next((member for member in definition.chain if member.broken), None)
            if broken_member:
                _set_broken(definition, f"broken dependency '{broken_member.name}'")
                changed = True

    # A broken release has no usable chain, however far its own resolve got before the break.
    for definition in definitions:
        if definition.broken:
            definition.chain = ()


def _set_broken(definition: Definition, error: str) -> None:
    """Mark ``definition`` broken; a definition keeps the first error found for it."""
    if definition.broken is None:
        definition.broken = error


# --- scanning ----------------------------------------------------------------


def find_definitions(*directories: str | Path) -> list[Definition]:
    """Recursively scan directories for ``.atedef`` files and read each one.

    A broken definition is reported (WARNING to stdout) but does not stop the scan; every returned
    :class:`Definition` carries its ``broken`` error string (or ``None`` if usable), including any
    defect found while resolving dependencies across Media Releases.
    """
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
    resolve_dependencies(definitions)
    return definitions


# --- schedule listing --------------------------------------------------------


@dataclass(frozen=True)
class ScheduleRow:
    """One visible Test Schedule in the flat picker list, or a broken entry with its error.

    ``error`` is ``None`` for a runnable schedule; otherwise it names why that row can't start
    (a definition-level defect, or a schedule that raised while loading).
    """

    media_number: str
    name: Optional[str] = None
    description: Optional[str] = None
    error: Optional[str] = None


def _schedule_summaries(definition: Definition) -> list[tuple[str, str, str]]:
    """Load each visible Test Schedule and read its name/description/media number.

    One fresh LuaHost per load (see the host module). A schedule that fails to load is skipped; a
    missing name is reported as broken so it isn't offered to pick from. Only usable definitions
    contribute here, so broken Media Releases never appear in the runnable list.
    """
    summaries: list[tuple[str, str, str]] = []
    for file_entry in definition.files:
        if not file_entry.is_schedule or file_entry.hidden:
            continue
        full = definition.path / file_entry.rel_path
        try:
            host = LuaHost(full, modules=module_map(definition))
            schedule_name = host.schedule.name
            description = host.schedule.description
        except HostError as exc:
            print(f"WARNING: {file_entry.rel_path}: {exc}")
            continue
        summaries.append((schedule_name, description, definition.media_number))
    return summaries


def list_schedules(*directories: str | Path) -> list[tuple[str, str, str]]:
    """Return ``(name, description, mediaNumber)`` for every visible Test Schedule.

    The flat runnable list a layout shows the Operator to pick from — one entry per visible
    schedule across all usable Media Releases in the configured directories. Broken definitions are
    excluded (their schedules can't run); see :func:`list_schedule_rows` for the full picker view.
    """
    definitions = find_definitions(*directories)
    summaries: list[tuple[str, str, str]] = []
    for definition in definitions:
        if definition.broken:
            continue
        summaries.extend(_schedule_summaries(definition))
    return summaries


def list_schedule_rows(*directories: str | Path) -> list[ScheduleRow]:
    """Return one row per visible schedule across all Media Releases, runnable and broken.

    The full picker view the Operator sees: usable schedules (``error=None``), schedules that raised
    while loading within an otherwise-usable release (``error=<load error>``), and each broken Media
    Release once with its defect. Broken definitions are never offered to start.
    """
    rows: list[ScheduleRow] = []
    for definition in find_definitions(*directories):
        if definition.broken:
            rows.append(ScheduleRow(media_number=definition.media_number, error=definition.broken))
            continue
        for file_entry in definition.files:
            if not file_entry.is_schedule or file_entry.hidden:
                continue
            full = definition.path / file_entry.rel_path
            try:
                host = LuaHost(full, modules=module_map(definition))
                rows.append(
                    ScheduleRow(
                        media_number=definition.media_number,
                        name=host.schedule.name,
                        description=host.schedule.description,
                    )
                )
            except HostError as exc:
                rows.append(ScheduleRow(media_number=definition.media_number, error=str(exc)))
    return rows


def resolve_schedule_path(definition: Definition, schedule_name: str) -> Path | None:
    """Find the ``.tslua`` file whose loaded ScheduleParams name matches ``schedule_name``.

    Returns ``None`` if no visible schedule with that name is listed in this release (broken
    definitions and schedules that fail to load are skipped).
    """
    for file_entry in definition.files:
        if not file_entry.is_schedule or file_entry.hidden:
            continue
        full = definition.path / file_entry.rel_path
        try:
            host = LuaHost(full, modules=module_map(definition))
        except HostError:
            continue
        if host.schedule.name == schedule_name:
            return full
    return None


def resolve_schedule(*directories: str | Path, schedule_name: str) -> Path | None:
    """Return the ``.tslua`` path whose loaded name matches ``schedule_name``.

    Searches each directory in declaration order and returns the first match; ``None`` if no visible
    runnable schedule with that name is listed anywhere. Used by the run session to turn an Operator's
    selection into a file to load — only usable schedules are ever returned.
    """
    for definition in find_definitions(*directories):
        if definition.broken:
            continue
        path = resolve_schedule_path(definition, schedule_name)
        if path is not None:
            return path
    return None


def _search_chain(definition: Definition) -> tuple[Definition, ...]:
    """The release itself followed by its resolved chain, each release listed once (packet 07b).

    A definition never resolved (or a hand-built one) has an empty chain, so the search is just its
    own files; ``resolve_dependencies`` always puts the release first in ``chain``."""
    if not definition.chain:
        return (definition,)
    members = list(definition.chain)
    if members[0] is not definition:
        members.insert(0, definition)
    return tuple(members)


def module_map(definition: Definition) -> dict[str, Path]:
    """Map each require-able helper module name to its absolute path (packets 06a, 07b).

    Walks the resolved chain — the release's own files first, then each dependency depth-first in
    declaration order — so a module only listed in a dependency is still require-able; a name found
    in more than one release resolves to the first one in that order. Schedules are never modules.
    Only non-schedule entries with a derived or explicit ``module_name`` are included. Paths resolve
    against each release's own directory so the Lua host can read them directly; a missing file is
    still listed here (its absence is a defect caught when the module is loaded), never filtered
    out."""
    modules: dict[str, Path] = {}
    for member in _search_chain(definition):
        for entry in member.files:
            if entry.is_schedule or entry.module_name is None:
                continue
            modules.setdefault(entry.module_name, member.path / entry.rel_path)
    return modules


def resolve_schedule_definition(*directories: str | Path, schedule_name: str) -> Definition | None:
    """Return the usable Media Release whose loaded schedules include ``schedule_name`` (or ``None``).

    Like :func:`resolve_schedule` but returns the whole :class:`Definition`, so a caller can read the
    release's module map alongside the resolved schedule. Broken releases and schedules that fail to
    load are skipped, matching :func:`resolve_schedule`.
    """
    for definition in find_definitions(*directories):
        if definition.broken:
            continue
        # Load each schedule with its own release's module map so a load-time require still resolves.
        modules = module_map(definition)
        for file_entry in definition.files:
            if not file_entry.is_schedule or file_entry.hidden:
                continue
            full = definition.path / file_entry.rel_path
            try:
                host = LuaHost(full, modules=modules)
            except HostError:
                continue
            if host.schedule.name == schedule_name:
                return definition
    return None


# --- checksum mismatches ------------------------------------------------------


def definition_mismatches(
    definition: Definition, schedule_name: Optional[str] = None
) -> list[tuple[str, Optional[str], str, str]]:
    """Return ``(rel_path, expected_sha256, computed_sha256, media_number)`` for every mismatch.

    With ``schedule_name``, only the files a run of that schedule can load are checked: its own
    ``.tslua`` and the ``.lua`` modules of the release and of every release in its resolved chain
    (any of which it may ``require``; which ones it actually loads is only known at run time). Other
    schedules' files are left out, so a schedule is never flagged for a sibling's mismatch. Without
    it, every listed file is checked. The media number is the one of the release that owns the file.
    (The catalog rejects a definition entry with no ``sha256``, so ``expected`` is always set here.)"""
    own = None
    if schedule_name is not None:
        path = resolve_schedule_path(definition, schedule_name)
        own = path.relative_to(definition.path).as_posix() if path is not None else None
    return [
        (entry.rel_path, entry.expected_sha256 or None, entry.computed_sha256, member.media_number)
        for member in _search_chain(definition)
        for entry in member.files
        if (entry.expected_sha256 or None) != entry.computed_sha256
        and (schedule_name is None or not entry.is_schedule or (member is definition and Path(entry.rel_path).as_posix() == own))
    ]


def list_schedule_mismatches(*directories: str | Path) -> dict[str, list[tuple[str, Optional[str], str, str]]]:
    """Map every visible schedule name to the checksum mismatches a run of it can load.

    Schedules with nothing to flag are absent from the map, so the Run screen only prefixes the rows
    it finds here with ⚠ (ticket 06c). Broken definitions contribute nothing: their schedules cannot
    run, so there is nothing to confirm."""
    flags: dict[str, list[tuple[str, Optional[str], str, str]]] = {}
    for definition in find_definitions(*directories):
        if definition.broken:
            continue
        if not definition_mismatches(definition):
            continue
        for name, _description, _media_number in _schedule_summaries(definition):
            mismatches = definition_mismatches(definition, name)
            if mismatches:
                flags[name] = mismatches
    return flags
