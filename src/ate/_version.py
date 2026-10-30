"""Compute ATE's version from the git tag — once, so build time and runtime agree.

This module is the single source of truth for ATE's version:

* ``pyproject.toml`` reads it with setuptools dynamic versioning
  (``[tool.setuptools.dynamic] version = {attr = "ate._version:__version__"}``),
  so a wheel built from a tagged checkout carries the tag's semver.
* The CLI and tests read :data:`__version__` at runtime, so ``ate --version``
  prints exactly what was baked into the build.
* A frozen build (packet 27a) has no git, so the build script writes the tag into
  ``ate/version.txt`` and the bundle carries it as package data.

The value is looked up in the order a build records it:

1. frozen bundle: ``ate/version.txt`` written by ``packaging/build_pyinstaller.py``;
2. installed distribution: the version in the wheel's own metadata
   (``importlib.metadata``), which is the tag the wheel was built from;
3. a source checkout: ``git describe --tags``;
4. otherwise a deterministic dev version, so an untagged checkout — or a machine
   without git — never fails to build.

Step 2 is what makes a released wheel honest. An installed wheel has no repository
next to it, so ``git describe`` finds nothing and the version would print
``0.0.0-dev`` even for a tagged release — which the release CI checks against the
tag (packet 28c), and which every Test Run and report footer records.
"""

from __future__ import annotations

import importlib.metadata
import importlib.resources
import os
import re
import subprocess
import sys

# Fallback used when no tag is reachable, git is unavailable, or parsing fails.
FALLBACK = "0.0.0-dev"

_SEMVER_RE = re.compile(r"\d+\.\d+\.\d+")


def _repo_root() -> str:
    """Directory containing ``pyproject.toml`` (two levels up from this file)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _describe() -> str | None:
    """Return raw ``git describe --tags`` output, or ``None`` if unavailable."""
    try:
        out = subprocess.check_output(
            ["git", "describe", "--tags"],
            cwd=_repo_root(),
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.decode("utf-8").strip()
    return text or None


def _parse(text: str) -> str | None:
    """Turn ``git describe`` output into a PEP 440 version string.

    A clean tag such as ``v1.2.3`` yields the final release ``1.2.3``; commits
    since the tag yield a dev prerelease like ``1.2.3.dev4+gabc123de``. Returns
    ``None`` when no semver core can be found so the caller can fall back.
    """
    match = _SEMVER_RE.search(text)
    if not match:
        return None
    base = match.group(0)

    # Everything after the tag, e.g. "-4-gabc123de" for commits since the tag,
    # or "" for a clean checkout sitting exactly on the tag.
    rest = text[match.end():].lstrip("-")
    if not rest:
        return base  # clean tag with no commits since -> final release
    # Commits since the tag: ``<count>-g<sha>`` -> dev prerelease.
    dev = re.match(r"^(?P<count>\d+)-g(?P<sha>[0-9a-f]{7,40})$", rest)
    if dev:
        return f"{base}.dev{dev.group('count')}+g{dev.group('sha')}"
    # Unexpected shape (e.g. a non-semver tag): stay safe and deterministic.
    return None


def _frozen_version() -> str | None:
    """The version a build script recorded into the bundle (packet 27a).

    A PyInstaller bundle has no git and no installed distribution metadata, so
    ``packaging/build_pyinstaller.py`` writes the tag's version to ``ate/version.txt`` and the
    spec bundles it as package data. Returns ``None`` when the file is not there."""
    try:
        text = (importlib.resources.files("ate") / "version.txt").read_text(encoding="utf-8").strip()
    except (OSError, ModuleNotFoundError):
        return None
    return text or None


def _metadata_version() -> str | None:
    """The version this ATE was *built* with, from the installed distribution's metadata.

    A wheel records its version in its ``*.dist-info/METADATA`` at build time, and that is the tag
    the release was cut from. An installed wheel has no repository beside it, so without this the
    version would fall through to ``git describe`` (nothing) and print ``0.0.0-dev``. Returns
    ``None`` when ATE is not an installed distribution — a plain source checkout with
    ``PYTHONPATH=src``, which is how the suite runs.
    """
    try:
        text = importlib.metadata.version("ate").strip()
    except (importlib.metadata.PackageNotFoundError, ValueError):
        return None
    return text or None


def _compute_version() -> str:
    """The ATE version string for this checkout."""
    if getattr(sys, "frozen", False):
        # Frozen: there is no repository here, and ``git describe`` would read whatever
        # repository the operator's working directory happens to be in.
        return _frozen_version() or FALLBACK
    metadata = _metadata_version()
    if metadata:
        return metadata
    describe = _describe()
    if not describe:
        return FALLBACK
    parsed = _parse(describe)
    return parsed if parsed is not None else FALLBACK


__version__ = _compute_version()


def version() -> str:
    """Return the computed version string (identical to :data:`__version__`)."""
    return __version__
