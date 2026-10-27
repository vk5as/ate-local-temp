"""Packet 07c — bad dependency graphs break the Test Definition.

Seam 1: real ``.atedef`` files in a temp tree, driven through :func:`find_definitions`. Every
``depends``-related defect from ``test-definition-format.md``'s Broken Test Definitions list
surfaces as ``Definition.broken`` carrying a readable message:

- a ``depends`` entry with no matching installed Media Release;
- a dependency cycle (every release on the cycle, and everything above it, is broken);
- the same module name provided by more than one Media Release of the resolved chain;
- a range matched by two installed releases with the same name and version (ambiguous);
- a broken dependency, transitively (a broken release breaks everything above it).

A broken Test Definition lists no runnable schedule, so :func:`resolve_schedule_definition`
returns ``None`` for its schedules.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ate.catalog import Definition, find_definitions, resolve_schedule_definition

LUA = b"return {}\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _release(
    media_number: str,
    name: str,
    version: str,
    modules: tuple[str, ...] = (),
    depends: tuple[tuple[str, str], ...] = (),
    schedule: str | None = None,
) -> tuple[dict, dict[str, bytes]]:
    """One Media Release: ``modules`` are ``.lua`` helper files; ``schedule`` adds a visible
    ``.tslua`` whose loaded ScheduleParams name is that string. Returns the `.atedef` dict and
    the bytes of every file it lists."""
    files: dict[str, bytes] = {f"{module}.lua": LUA for module in modules}
    entries = [{"path": path, "sha256": _sha(data)} for path, data in files.items()]
    if schedule:
        src = b'return ScheduleParams("%s", "desc")\n' % schedule.encode()
        files["main.tslua"] = src
        entries.append({"path": "main.tslua", "sha256": _sha(src)})
    atedef = {
        "format": 1,
        "mediaNumber": media_number,
        "name": name,
        "version": version,
        "depends": [{"name": dep_name, "version": dep_range} for dep_name, dep_range in depends],
        "files": entries,
    }
    return atedef, files


def _scan(tmp_path: Path, releases: dict[str, dict]) -> dict[str, Definition]:
    """Write every release under ``defs/<label>/`` with its listed files on disk, then scan."""
    root = tmp_path / "defs"
    root.mkdir(parents=True, exist_ok=True)
    for label, (atedef, files) in releases.items():
        release_dir = root / label
        release_dir.mkdir(parents=True, exist_ok=True)
        (release_dir / f"{label}.atedef").write_text(json.dumps(atedef), encoding="utf-8")
        for path, content in files.items():
            (release_dir / path).write_bytes(content)
    return {d.media_number: d for d in find_definitions(root)}


def _chain_media(definition: Definition) -> tuple[str, ...]:
    return tuple(d.media_number for d in definition.chain)


# --- a depends entry that matches nothing ------------------------------------


def test_a_depends_entry_with_no_match_breaks_the_definition(tmp_path):
    """No installed release satisfies the range: the dependent is broken and names the entry."""
    by_media = _scan(
        tmp_path,
        {"main": _release("MR-M", "Main release", "1.0.0", depends=(("Ghost helpers", "^3"),), schedule="Main run")},
    )
    assert by_media["MR-M"].broken == "unresolved dependency 'Ghost helpers' (^3)"
    assert by_media["MR-M"].chain == ()
    assert resolve_schedule_definition(tmp_path / "defs", schedule_name="Main run") is None


# --- cycles ------------------------------------------------------------------


def test_a_cycle_breaks_every_release_on_it(tmp_path):
    """E -> F -> E: both cycle members are broken with the cycle message."""
    by_media = _scan(
        tmp_path,
        {
            "e": _release("MR-E", "E release", "1.0.0", depends=(("F release", "^1"),), schedule="E run"),
            "f": _release("MR-F", "F release", "1.0.0", depends=(("E release", "^1"),), schedule="F run"),
        },
    )
    for media_number in ("MR-E", "MR-F"):
        assert "dependency cycle" in by_media[media_number].broken, by_media[media_number].broken
    assert by_media["MR-E"].chain == ()
    assert resolve_schedule_definition(tmp_path / "defs", schedule_name="E run") is None


def test_a_release_above_a_cycle_is_broken_by_its_dependency(tmp_path):
    """D -> E where E is on a cycle: D is broken as a broken dependency, not as part of the cycle."""
    by_media = _scan(
        tmp_path,
        {
            "d": _release("MR-D", "D release", "1.0.0", depends=(("E release", "^1"),), schedule="D run"),
            "e": _release("MR-E", "E release", "1.0.0", depends=(("F release", "^1"),), schedule="E run"),
            "f": _release("MR-F", "F release", "1.0.0", depends=(("E release", "^1"),), schedule="F run"),
        },
    )
    assert "dependency cycle" in by_media["MR-E"].broken
    assert "dependency cycle" in by_media["MR-F"].broken
    assert by_media["MR-D"].broken == "broken dependency 'E release'"
    assert resolve_schedule_definition(tmp_path / "defs", schedule_name="D run") is None


# --- module names shared by more than one release of the chain ---------------


def test_a_release_colliding_with_its_own_dependency_is_broken(tmp_path):
    """MR-A provides `one` and its dependency MR-B provides `one` too: MR-A is broken by name."""
    by_media = _scan(
        tmp_path,
        {
            "a": _release(
                "MR-A", "A release", "1.0.0", modules=("one", "own"),
                depends=(("B release", "^1"),), schedule="A run",
            ),
            "b": _release("MR-B", "B release", "1.0.0", modules=("one",)),
        },
    )
    assert by_media["MR-A"].broken == "module name collision across Media Releases: one (MR-A, MR-B)"
    assert by_media["MR-B"].broken is None, "MR-B's own chain holds one release, so no collision"
    assert resolve_schedule_definition(tmp_path / "defs", schedule_name="A run") is None


def test_two_sibling_dependencies_with_the_same_module_name_collide(tmp_path):
    """A -> [B, C]: B and C are chain siblings both providing `one`, so A is broken by that name."""
    by_media = _scan(
        tmp_path,
        {
            "a": _release(
                "MR-A", "A release", "1.0.0",
                depends=(("B release", "^1"), ("C release", "^1")), schedule="A run",
            ),
            "b": _release("MR-B", "B release", "1.0.0", modules=("one",)),
            "c": _release("MR-C", "C release", "1.0.0", modules=("one",)),
        },
    )
    assert by_media["MR-A"].broken == "module name collision across Media Releases: one (MR-B, MR-C)"
    for media_number in ("MR-B", "MR-C"):
        assert by_media[media_number].broken is None
    assert resolve_schedule_definition(tmp_path / "defs", schedule_name="A run") is None


def test_several_colliding_names_are_listed_sorted(tmp_path):
    """Two colliding names in one chain are joined, sorted by module name."""
    by_media = _scan(
        tmp_path,
        {
            "a": _release(
                "MR-A", "A release", "1.0.0", modules=("two", "one"),
                depends=(("B release", "^1"),), schedule="A run",
            ),
            "b": _release("MR-B", "B release", "1.0.0", modules=("one", "two")),
        },
    )
    assert by_media["MR-A"].broken == (
        "module name collision across Media Releases: one (MR-A, MR-B); two (MR-A, MR-B)"
    )


def test_two_different_names_in_one_release_are_not_a_collision(tmp_path):
    """One release with `one` and `two` depending on a release with neither: no collision at all."""
    by_media = _scan(
        tmp_path,
        {
            "a": _release(
                "MR-A", "A release", "1.0.0", modules=("one", "two"),
                depends=(("B release", "^1"),), schedule="A run",
            ),
            "b": _release("MR-B", "B release", "1.0.0", modules=("three",)),
        },
    )
    for media_number in ("MR-A", "MR-B"):
        assert by_media[media_number].broken is None, by_media[media_number].broken
    assert _chain_media(by_media["MR-A"]) == ("MR-A", "MR-B")
    assert resolve_schedule_definition(tmp_path / "defs", schedule_name="A run") is not None


def test_a_diamond_chain_still_collides_when_two_releases_share_a_name(tmp_path):
    """A -> [B, C], B -> D, C -> D: D is listed once, and B/D both providing `shared` breaks
    every release whose chain holds both of them (A and B), not C."""
    by_media = _scan(
        tmp_path,
        {
            "a": _release(
                "MR-A", "A release", "1.0.0",
                depends=(("B release", "^1"), ("C release", "^1")), schedule="A run",
            ),
            "b": _release("MR-B", "B release", "1.0.0", modules=("shared",), depends=(("D release", "^1"),)),
            "c": _release("MR-C", "C release", "1.0.0", depends=(("D release", "^1"),)),
            "d": _release("MR-D", "D release", "1.0.0", modules=("shared",)),
        },
    )
    assert by_media["MR-A"].broken == "module name collision across Media Releases: shared (MR-B, MR-D)"
    assert by_media["MR-B"].broken == "module name collision across Media Releases: shared (MR-B, MR-D)"
    assert by_media["MR-C"].broken is None
    assert by_media["MR-D"].broken is None
    assert _chain_media(by_media["MR-C"]) == ("MR-C", "MR-D"), "D is listed once, even though A reaches it twice"


# --- ambiguous matches ---------------------------------------------------------


def test_an_ambiguous_match_breaks_the_dependent_only(tmp_path):
    """Two installed releases with the same name and version make the range ambiguous for A only."""
    by_media = _scan(
        tmp_path,
        {
            "common-a": _release("MR-A", "Common helpers", "3.1.4", modules=("a_mod",)),
            "common-b": _release("MR-B", "Common helpers", "3.1.4", modules=("b_mod",)),
            "main": _release(
                "MR-M", "Main release", "1.0.0", depends=(("Common helpers", "^3.1"),), schedule="Main run"
            ),
        },
    )
    assert by_media["MR-M"].broken == "ambiguous dependency 'Common helpers' matched by ^3.1"
    for media_number in ("MR-A", "MR-B"):
        assert by_media[media_number].broken is None


# --- broken dependencies, transitively ----------------------------------------


def test_a_broken_release_breaks_its_dependents_transitively(tmp_path):
    """X -> Y -> Z where Z matches nothing: Y and X are both broken as broken dependencies."""
    by_media = _scan(
        tmp_path,
        {
            "x": _release("MR-X", "X release", "1.0.0", depends=(("Y release", "^1"),), schedule="X run"),
            "y": _release("MR-Y", "Y release", "1.0.0", depends=(("Z release", "^1"),), schedule="Y run"),
            "z": _release("MR-Z", "Z release", "1.0.0", depends=(("Ghost release", "^9"),), schedule="Z run"),
        },
    )
    assert by_media["MR-Z"].broken == "unresolved dependency 'Ghost release' (^9)"
    assert by_media["MR-Y"].broken == "broken dependency 'Z release'"
    assert by_media["MR-X"].broken == "broken dependency 'Y release'", by_media["MR-X"].broken
    assert by_media["MR-X"].chain == ()
    assert resolve_schedule_definition(tmp_path / "defs", schedule_name="X run") is None


def test_a_collision_broken_release_breaks_the_release_above_it(tmp_path):
    """A -> B -> C with B and C both providing `one`: B is broken by the collision and A above it."""
    by_media = _scan(
        tmp_path,
        {
            "a": _release("MR-A", "A release", "1.0.0", depends=(("B release", "^1"),), schedule="A run"),
            "b": _release("MR-B", "B release", "1.0.0", modules=("one",), depends=(("C release", "^1"),)),
            "c": _release("MR-C", "C release", "1.0.0", modules=("one",)),
        },
    )
    assert by_media["MR-B"].broken == "module name collision across Media Releases: one (MR-B, MR-C)"
    assert by_media["MR-C"].broken is None
    assert by_media["MR-A"].broken is not None, by_media["MR-A"].broken
    assert by_media["MR-A"].chain == ()
    assert resolve_schedule_definition(tmp_path / "defs", schedule_name="A run") is None
