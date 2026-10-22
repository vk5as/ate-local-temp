"""Compute ATE's version from the git tag — once, so build time and runtime agree.

This module is the single source of truth for ATE's version:

* ``pyproject.toml`` reads it with setuptools dynamic versioning
  (``[tool.setuptools.dynamic] version = {attr = "ate._version:__version__"}``),
  so a wheel built from a tagged checkout carries the tag's semver.
* The CLI and tests read :data:`__version__` at runtime, so ``ate --version``
  prints exactly what was baked into the build.

The value comes from ``git describe --tags`` when a tag is reachable; otherwise
it falls back to a deterministic dev version so an untagged checkout — or one on
a machine without git — never fails to build.
"""

from __future__ import annotations

import os
import re
import subprocess

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


def _compute_version() -> str:
    """The ATE version string for this checkout."""
    describe = _describe()
    if not describe:
        return FALLBACK
    parsed = _parse(describe)
    return parsed if parsed is not None else FALLBACK


__version__ = _compute_version()


def version() -> str:
    """Return the computed version string (identical to :data:`__version__`)."""
    return __version__
