"""CLI behaviour tests: ``ate --version`` output."""

import re

from ate.cli import main


def test_version_flag_returns_zero(capsys):
    """``--version`` is a recognised command and exits cleanly (0)."""
    assert main(["--version"]) == 0


def test_version_is_semver_when_untagged(capsys):
    """With no tag present, ``ate --version`` still prints a semver string."""
    assert main(["--version"]) == 0
    out = capsys.readouterr().out.strip()
    assert re.match(r"\d+\.\d+\.\d+", out), f"expected a version core, got {out!r}"


def test_short_version_flag(capsys):
    """The ``-V`` short flag behaves like ``--version``."""
    assert main(["-V"]) == 0
    out = capsys.readouterr().out.strip()
    assert re.match(r"\d+\.\d+\.\d+", out)
