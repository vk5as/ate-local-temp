"""Packet 29e: CI sets the offscreen Qt platform the suite depends on.

`conftest.py` builds one `QApplication` for the whole session with an empty argument list — this
PyQt6 build's binding does not accept a `platform=` kwarg — so Qt gets its offscreen platform from
`QT_QPA_PLATFORM`. The workflow must set it for every matrix entry (Ubuntu and Windows), or every
test that needs `app` fails on a runner with no display.
"""

from __future__ import annotations

from pathlib import Path

CI_FILE = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def test_ci_sets_offscreen_for_every_matrix_entry() -> None:
    """The env is at job level, so both operating systems in the matrix get it."""
    text = CI_FILE.read_text(encoding="utf-8")
    job = text.split("jobs:", 1)[1]
    assert "QT_QPA_PLATFORM: offscreen" in job, "CI never sets the offscreen Qt platform"
    assert "os: [ubuntu-latest, windows-latest]" in job, "the matrix no longer covers both operating systems"
    env, steps = job.split("env:", 1)[1].split("steps:", 1)[0], job.split("steps:", 1)[1]
    assert "QT_QPA_PLATFORM" in env and "QT_QPA_PLATFORM" not in steps, "the env moved below the steps"
