"""Ticket 07a: npm-style semver ranges in a Media Release ``depends`` entry.

Item 1 and 2 are covered headlessly by direct unit tests of :func:`ate.catalog.matches_range`:
every range form the spec names (exact, ``^``, ``~``, comparison bounds and combined bounds)
plus the edge cases it calls out — npm zero-major caret rules (``^0.x``, ``^0.0.x``),
``~1.2``, build metadata ignored, and whitespace — and the pre-release rule: a pre-release
candidate matches only when the range names a pre-release on the same major.minor.patch
tuple (the npm reading this codebase implements).

Item 3 is the Seam-1 slice: real ``.atedef`` files in a temp directory, one fresh scan per
test, each range form resolving to the expected highest installed release, a pre-release
excluded by a plain range and included when the range names one, and a transitive chain
still resolving. Each test fails if its rule breaks; the proof that they do is recorded in
/tmp/07a-checklist.md (one or two cases deliberately broken while running).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from ate.catalog import find_definitions, matches_range

from .test_catalog_definitions import _valid_tslua, build_env


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- item 1: every range form the spec lists -------------------------------


def test_exact_range_matches_only_that_version():
    """The bare version ``3.1.4`` matches exactly 3.1.4 and nothing else."""
    assert matches_range("3.1.4", "3.1.4") is True
    assert matches_range("3.1.5", "3.1.4") is False
    assert matches_range("3.1.3", "3.1.4") is False
    assert matches_range("3.1.4.0", "3.1.4") is False  # not valid semver


def test_caret_range_means_at_least_and_below_next_major():
    """``^3.1`` is ``>=3.1.0 <4.0.0``; ``^3.1.2`` keeps the patch floor."""
    for version in ("3.1.0", "3.1.4", "3.9.9"):
        assert matches_range(version, "^3.1") is True, f"{version} must satisfy ^3.1"
    for version in ("4.0.0", "4.0.1", "2.9.0", "3.0.9"):
        assert matches_range(version, "^3.1") is False, f"{version} must not satisfy ^3.1"

    for version in ("3.1.2", "3.1.9", "3.9.9"):
        assert matches_range(version, "^3.1.2") is True, f"{version} must satisfy ^3.1.2"
    for version in ("3.1.1", "3.1.0", "4.0.0"):
        assert matches_range(version, "^3.1.2") is False, f"{version} must not satisfy ^3.1.2"


def test_zero_major_caret_rules_are_special():
    """npm: ^0.x keeps the minor (``^0.2`` <0.3.0) and ^0.0.x keeps the patch (``^0.0.3`` <0.0.4)."""
    # ^0.2 := >=0.2.0 <0.3.0
    for version in ("0.2.0", "0.2.5", "0.2.99"):
        assert matches_range(version, "^0.2") is True, f"{version} must satisfy ^0.2"
    for version in ("0.3.0", "0.1.9", "1.0.0"):
        assert matches_range(version, "^0.2") is False, f"{version} must not satisfy ^0.2"

    # ^0.0.3 := >=0.0.3 <0.0.4 (31 > 4, so nothing above 0.0.3 qualifies)
    assert matches_range("0.0.3", "^0.0.3") is True
    for version in ("0.0.4", "0.0.41", "0.1.0", "0.0.2"):
        assert matches_range(version, "^0.0.3") is False, f"{version} must not satisfy ^0.0.3"

    # ^0 := >=0.0.0 <1.0.0 (and ^0.0 agrees)
    for version in ("0.0.1", "0.5.0", "0.9.9"):
        assert matches_range(version, "^0") is True, f"{version} must satisfy ^0"
    for version in ("1.0.0", "1.2.3"):
        assert matches_range(version, "^0") is False, f"{version} must not satisfy ^0"
        assert matches_range(version, "^0.0") is False, f"{version} must not satisfy ^0.0"


def test_tilde_range_keeps_the_minor():
    """``~3.1.2`` is ``>=3.1.2 <3.2.0``; ``~1.2`` is ``>=1.2.0 <1.3.0``; ``~1`` is ``>=1.0.0 <2.0.0``."""
    for version in ("3.1.2", "3.1.5", "3.1.9"):
        assert matches_range(version, "~3.1.2") is True, f"{version} must satisfy ~3.1.2"
    for version in ("3.1.1", "3.2.0", "3.2.1", "4.0.0"):
        assert matches_range(version, "~3.1.2") is False, f"{version} must not satisfy ~3.1.2"

    # ~1.2 (a two-component tilde): same rule, padded to 1.2.0.
    for version in ("1.2.0", "1.2.9"):
        assert matches_range(version, "~1.2") is True, f"{version} must satisfy ~1.2"
    for version in ("1.1.9", "1.3.0"):
        assert matches_range(version, "~1.2") is False, f"{version} must not satisfy ~1.2"

    for version in ("1.0.0", "1.9.9"):
        assert matches_range(version, "~1") is True, f"{version} must satisfy ~1"
    for version in ("2.0.0", "0.9.0"):
        assert matches_range(version, "~1") is False, f"{version} must not satisfy ~1"


def test_combined_comparison_bounds_match_the_stated_bounds():
    """``>=3.0 <4`` matches the stated bounds, partial bounds included."""
    for version in ("3.0.0", "3.0.1", "3.9.9"):
        assert matches_range(version, ">=3.0 <4") is True, f"{version} must satisfy >=3.0 <4"
    for version in ("2.9.9", "4.0.0", "4.0.1"):
        assert matches_range(version, ">=3.0 <4") is False, f"{version} must not satisfy >=3.0 <4"

    assert matches_range("3.2.0", ">=3.0.1 <=3.2") is True
    for version in ("3.2.1", "3.0.0"):
        assert matches_range(version, ">=3.0.1 <=3.2") is False, f"{version} must not satisfy >=3.0.1 <=3.2"


def test_whitespace_around_the_range_is_ignored():
    """Leading/trailing spaces and spaces inside a combined range do not change it."""
    for version in ("3.1.4", "3.9.0"):
        assert matches_range(version, "  ^3.1  ") is True, f"{version} must satisfy '  ^3.1  '"
        assert matches_range(version, ">= 3.0   < 4 ") is True, f"{version} must satisfy '>= 3.0   < 4 '"
    assert matches_range("4.0.0", "  ^3.1  ") is False
    assert matches_range("2.9.0", ">= 3.0   < 4 ") is False


def test_build_metadata_is_ignored_everywhere():
    """Build metadata in a candidate or a range bound is ignored, per the packet."""
    assert matches_range("1.0.0+build.5", "1.0.0") is True
    assert matches_range("1.0.0+build.5", "^1.0.0") is True
    assert matches_range("1.0.0", "1.0.0+build.5") is True  # bound's build is ignored
    assert matches_range("1.0.1", "1.0.0+build.5") is False
    assert matches_range("1.0.0+b.2", "1.0.0+b.9") is True  # both reduce to 1.0.0


# --- item 2: the pre-release rule --------------------------------------------


def test_prerelease_excluded_from_a_plain_range():
    """A pre-release candidate never matches a range that names no pre-release."""
    for range_text in ("^1", "^1.0", "^1.0.0", ">=1.0.0", "~1.0.1", "1.0.1", "^1.0.0-alpha"):
        assert matches_range("1.0.1-alpha", range_text) is False, (
            f"a pre-release must not satisfy a plain range like {range_text!r}"
        )


def test_prerelease_included_when_the_range_names_one_on_the_same_tuple():
    """npm reading implemented: the range needs a pre-release on the same major.minor.patch
    (its identifiers may differ from the candidate's); the usual comparators still apply."""
    for version in ("1.0.1-beta", "1.0.1-beta.2", "1.0.1-gamma"):
        # ^1.0.1-beta := >=1.0.1-beta <2.0.0; pre-releases >= beta satisfy the lower bound.
        assert matches_range(version, "^1.0.1-beta") is True, f"{version} must satisfy ^1.0.1-beta"
        assert matches_range(version, ">=1.0.0-beta") is False, (
            f"different minor: {version} must not satisfy >=1.0.0-beta"
        )

    # Same major.minor.patch, different identifiers: allowed (gate passes, comparators decide).
    assert matches_range("1.0.1-gamma.2", ">=1.0.1-beta") is True
    # alpha < beta, so the lower bound rejects it even though the gate passes.
    assert matches_range("1.0.1-alpha.7", ">=1.0.1-beta") is False
    assert matches_range("1.0.1-alpha", "1.0.1-beta") is False     # exact needs full equality
    assert matches_range("1.0.1", ">=1.0.1-beta") is True          # a release is not gated


# --- item 3: Seam-1 — the catalog scan over a temp directory -----------------


def _release(media_number: str, version: str) -> dict:
    """One Media Release definition; a structural-only definition (its .lua is never listed)."""
    return {
        "format": 1,
        "mediaNumber": media_number,
        "name": "Common helpers",
        "version": version,
        "files": [],
    }


def _scan_with_main_range(tmp_path: Path, range_text: str, installed: list[tuple[str, str]]) -> dict:
    """Scan a temp tree of the installed releases plus a Main release with that ``depends`` range.

    ``installed`` is a list of (media_number, version); the Main release depends on
    ``Common helpers`` with ``range_text``. Returns ``{media_number: Definition}``.
    """
    defs: dict[str, dict] = {}
    for media_number, version in installed:
        defs[f"rel-{media_number}"] = _release(media_number, version)
    defs["main"] = {
        "format": 1,
        "mediaNumber": "MR-MAIN",
        "name": "Main release",
        "version": "1.0.0",
        "depends": [{"name": "Common helpers", "version": range_text}],
        "files": [{"path": "main.tslua", "sha256": _sha256(_valid_tslua())}],
    }
    root = build_env(tmp_path, defs=defs, files={"main.tslua": _valid_tslua()})
    return {d.media_number: d for d in find_definitions(root)}


def test_seam_exact_range_picks_that_installed_release(tmp_path):
    """Exact ``3.1.4`` resolves to 3.1.4 even when 3.1.5 and 3.2.0 are also installed."""
    installed = [("MR-314", "3.1.4"), ("MR-315", "3.1.5"), ("MR-320", "3.2.0")]
    by_media = _scan_with_main_range(tmp_path, "3.1.4", installed)
    main = by_media["MR-MAIN"]
    assert main.broken is None, f"exact range 3.1.4 must resolve: {main.broken}"
    chain = getattr(main, "_chain", ())
    assert "MR-314" in chain
    for media_number in ("MR-315", "MR-320"):
        assert media_number not in chain, (
            f"exact range 3.1.4 must not pick {media_number}; only that version satisfies it"
        )


def test_seam_caret_range_picks_highest_below_next_major(tmp_path):
    """``^3.1`` picks 3.9.0 from {3.1.0, 3.1.4, 3.9.0, 4.0.0} — the highest satisfying release."""
    installed = [("MR-310", "3.1.0"), ("MR-314", "3.1.4"), ("MR-390", "3.9.0"), ("MR-400", "4.0.0")]
    by_media = _scan_with_main_range(tmp_path, "^3.1", installed)
    main = by_media["MR-MAIN"]
    assert main.broken is None, f"^3.1 must resolve: {main.broken}"
    assert "MR-390" in getattr(main, "_chain", ())
    for media_number in ("MR-310", "MR-314", "MR-400"):
        assert media_number not in getattr(main, "_chain", ())


def test_seam_tilde_range_picks_highest_in_the_minor(tmp_path):
    """``~3.1.2`` picks 3.1.9 from {3.1.2, 3.1.9, 3.2.0, 4.0.0}."""
    installed = [("MR-312", "3.1.2"), ("MR-319", "3.1.9"), ("MR-320", "3.2.0"), ("MR-400", "4.0.0")]
    by_media = _scan_with_main_range(tmp_path, "~3.1.2", installed)
    main = by_media["MR-MAIN"]
    assert main.broken is None, f"~3.1.2 must resolve: {main.broken}"
    assert "MR-319" in getattr(main, "_chain", ())
    for media_number in ("MR-312", "MR-320", "MR-400"):
        assert media_number not in getattr(main, "_chain", ())


def test_seam_zero_major_caret_picks_highest_below_the_minor(tmp_path):
    """``^0.2`` picks 0.2.7 from {0.2.0, 0.2.7, 0.3.0, 1.0.0} (npm zero-major keeps the minor)."""
    installed = [("MR-020", "0.2.0"), ("MR-027", "0.2.7"), ("MR-030", "0.3.0"), ("MR-100", "1.0.0")]
    by_media = _scan_with_main_range(tmp_path, "^0.2", installed)
    main = by_media["MR-MAIN"]
    assert main.broken is None, f"^0.2 must resolve: {main.broken}"
    assert "MR-027" in getattr(main, "_chain", ())
    for media_number in ("MR-020", "MR-030", "MR-100"):
        assert media_number not in getattr(main, "_chain", ())


def test_seam_zero_patch_caret_picks_highest_below_the_patch(tmp_path):
    """``^0.0.3`` keeps the patch: of {0.0.3, 0.0.4, 0.1.0} only 0.0.3 qualifies, and it wins."""
    installed = [("MR-003", "0.0.3"), ("MR-004", "0.0.4"), ("MR-010", "0.1.0")]
    by_media = _scan_with_main_range(tmp_path, "^0.0.3", installed)
    main = by_media["MR-MAIN"]
    assert main.broken is None, f"^0.0.3 must resolve: {main.broken}"
    assert "MR-003" in getattr(main, "_chain", ())
    for media_number in ("MR-004", "MR-010"):
        assert media_number not in getattr(main, "_chain", ())


def test_seam_two_component_tilde_picks_highest_in_the_minor(tmp_path):
    """``~1.2`` picks 1.2.9 from {1.2.0, 1.2.9, 1.3.0} (padded to >=1.2.0 <1.3.0)."""
    installed = [("MR-120", "1.2.0"), ("MR-129", "1.2.9"), ("MR-130", "1.3.0")]
    by_media = _scan_with_main_range(tmp_path, "~1.2", installed)
    main = by_media["MR-MAIN"]
    assert main.broken is None, f"~1.2 must resolve: {main.broken}"
    assert "MR-129" in getattr(main, "_chain", ())
    for media_number in ("MR-120", "MR-130"):
        assert media_number not in getattr(main, "_chain", ())


def test_seam_combined_bounds_picks_highest_in_the_window(tmp_path):
    """``>=3.0 <4`` picks 3.9.9 from {3.0.1, 3.9.9, 4.0.0}."""
    installed = [("MR-301", "3.0.1"), ("MR-399", "3.9.9"), ("MR-400", "4.0.0")]
    by_media = _scan_with_main_range(tmp_path, ">=3.0 <4", installed)
    main = by_media["MR-MAIN"]
    assert main.broken is None, f">=3.0 <4 must resolve: {main.broken}"
    assert "MR-399" in getattr(main, "_chain", ())
    for media_number in ("MR-301", "MR-400"):
        assert media_number not in getattr(main, "_chain", ())


def test_seam_prerelease_excluded_from_a_plain_range(tmp_path):
    """A plain ``^1.0`` range picks the release 1.0.0, not the installed 1.0.1-alpha."""
    installed = [("MR-100", "1.0.0"), ("MR-101A", "1.0.1-alpha")]
    by_media = _scan_with_main_range(tmp_path, "^1.0", installed)
    main = by_media["MR-MAIN"]
    assert main.broken is None, f"^1.0 must resolve without picking a pre-release: {main.broken}"
    assert "MR-100" in getattr(main, "_chain", ())
    assert "MR-101A" not in getattr(main, "_chain", ())


def test_seam_prerelease_included_when_the_range_names_one(tmp_path):
    """``>=1.0.1-beta`` names a pre-release on 1.0.1, so the pre-releases are candidates: the
    highest satisfying one (1.0.1-beta.2) is picked over the lower 1.0.1-beta, and the
    out-of-range release 1.0.0 stays out."""
    installed = [("MR-100", "1.0.0"), ("MR-101B", "1.0.1-beta"), ("MR-101B2", "1.0.1-beta.2")]
    by_media = _scan_with_main_range(tmp_path, ">=1.0.1-beta", installed)
    main = by_media["MR-MAIN"]
    assert main.broken is None, f">=1.0.1-beta must resolve: {main.broken}"
    assert "MR-101B2" in getattr(main, "_chain", ())
    for media_number in ("MR-100", "MR-101B"):
        assert media_number not in getattr(main, "_chain", ())


def test_seam_transitive_dependency_chain_resolves(tmp_path):
    """A dependency's own ``depends`` is resolved too: A -> B (^2.1) -> C (~2.1) -> 2.1.7."""
    tslua = _valid_tslua()
    defs = {
        "a": {
            "format": 1,
            "mediaNumber": "MR-A",
            "name": "Release A",
            "version": "1.0.0",
            "depends": [{"name": "Release B", "version": "^2.1"}],
            "files": [{"path": "a.tslua", "sha256": _sha256(tslua)}],
        },
        "b-215": {
            "format": 1,
            "mediaNumber": "MR-B215",
            "name": "Release B",
            "version": "2.1.5",
            "depends": [{"name": "Release C", "version": "~2.1"}],
            "files": [{"path": "b.lua", "sha256": _sha256(b"return {}\n")}],
        },
        "b-219": {
            "format": 1,
            "mediaNumber": "MR-B219",
            "name": "Release B",
            "version": "2.1.9",
            "depends": [{"name": "Release C", "version": "~2.1"}],
            "files": [{"path": "b.lua", "sha256": _sha256(b"return {}\n")}],
        },
        "c-217": {
            "format": 1,
            "mediaNumber": "MR-C217",
            "name": "Release C",
            "version": "2.1.7",
            "files": [{"path": "c.lua", "sha256": _sha256(b"return {}\n")}],
        },
        "c-218": {
            "format": 1,
            "mediaNumber": "MR-C218",
            "name": "Release C",
            "version": "2.1.8",
            "files": [{"path": "c.lua", "sha256": _sha256(b"return {}\n")}],
        },
    }
    files = {"a.tslua": tslua, "b.lua": b"return {}\n", "c.lua": b"return {}\n"}
    with tempfile.TemporaryDirectory() as tmp:
        root = build_env(Path(tmp), defs=defs, files=files)
        by_media = {d.media_number: d for d in find_definitions(root)}

    a = by_media["MR-A"]
    assert a.broken is None, f"transitive chain must resolve: {a.broken}"
    # A resolves B to the highest ^2.1 release (2.1.9); each definition's _chain holds the
    # Media Releases it resolved, so B219's own chain must then hold the ~2.1 pick (2.1.8),
    # not 2.1.7 or 2.1.9 (no ~2.1 release at 2.1.9).
    a_chain = getattr(a, "_chain", ())
    b219_chain = getattr(by_media["MR-B219"], "_chain", ())
    assert "MR-B219" in a_chain, f"A's dependency must resolve to B=2.1.9, chain={a_chain}"
    assert "MR-C218" in b219_chain, f"B219's ~2.1 dependency must resolve to C=2.1.8, chain={b219_chain}"
    assert "MR-B215" not in a_chain and "MR-C217" not in b219_chain
    assert by_media["MR-C218"].broken is None
