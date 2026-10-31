"""The suite never lets the runner's privilege decide what a test does.

The first real CI run hung in "Run tests" on `windows-latest`. GitHub's Windows runners are
administrators, and packet 30d makes a privileged ATE ask the Operator before every Test Report
PDF: `QMessageBox.exec()` in an offscreen Qt application returns nothing, so any test that runs a
Test Run through `launch()` without injecting a `pdf_dialog` waits forever. A container runs as
root and has the same problem.

`conftest.py` therefore pins elevation to False for every test with `ATE_ELEVATED`
(:data:`ate.report.pdf.ELEVATION_ENV`); a test that wants to be privileged sets it to 1, exactly
as it used to fake `os.geteuid`. The variable is read by the process and by every helper it
spawns, so a spawned `ate _pdf` gets the same answer the parent had.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ate.app import _pdf_sandbox_dialog
from ate.report import pdf
from ate.report.pdf import PDF_SANDBOXED, pdf_sandbox_choice

SRC = Path(__file__).resolve().parents[1] / "src"


def test_the_runner_is_never_privileged_for_a_test(monkeypatch) -> None:
    """A root runner is pinned to ordinary user, so the 30d question is never asked."""
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)  # what a root runner would report
    assert pdf._elevation() is False, "conftest did not pin the runner's privilege"
    assert pdf._is_elevated() is False


def test_a_test_can_still_ask_for_privilege(monkeypatch) -> None:
    """The pin is a default, not a wall: setting the variable answers the other way."""
    monkeypatch.setenv(pdf.ELEVATION_ENV, "1")
    assert pdf._elevation() is True, "a test cannot simulate an elevated ATE any more"
    monkeypatch.setenv(pdf.ELEVATION_ENV, "0")
    assert pdf._elevation() is False


def test_the_pin_reaches_a_spawned_helper(monkeypatch) -> None:
    """A child process has its own euid, so the seam has to travel through the environment.

    The child is told it is root by replacing `os.geteuid` in its own interpreter: without the pin
    it would report itself elevated, which is exactly the case the Windows runner is in."""
    code = "import os; os.geteuid = lambda: 0; from ate.report.pdf import _elevation; print(_elevation())"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=dict(os.environ, PYTHONPATH=str(SRC)),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False", f"the helper saw the runner's real privilege: {proc.stdout}"


def test_the_pin_is_in_the_environment_of_the_whole_session() -> None:
    """The variable, not a monkeypatch: a module-scoped fixture that runs a Test Run, and every
    subprocess it spawns, must see the same answer as the test body.

    The example harness builds its session before any per-test fixture runs, so a pin that only
    existed per test would let a root or admin runner skip the Test Report PDF there."""
    assert os.environ.get(pdf.ELEVATION_ENV) == "0", "conftest pins privilege too late, or not at all"


def test_a_pinned_launch_writes_the_pdf_instead_of_skipping_it(monkeypatch) -> None:
    """The default `launch()` answer is sandboxed, so no modal box is ever opened in a test.

    `pdf_sandbox_choice(ask=_pdf_sandbox_dialog)` is what `launch` builds when the caller injects
    nothing. Offscreen, `exec()` on that box never returns: an elevated runner blocks here, which
    is the hang the Windows job showed."""
    plan = pdf_sandbox_choice(ask=_pdf_sandbox_dialog)()
    assert plan == PDF_SANDBOXED, f"the pinned run opened the 30d dialog instead of rendering ({plan})"


def test_elevation_is_simulated_by_the_variable_not_by_geteuid() -> None:
    """The 30d tests must be able to say "pretend this is root" on Windows too.

    `os.geteuid` does not exist on a Windows runner, so a suite that fakes privilege through it
    cannot express the elevated branch there at all; the environment variable can."""
    text = (SRC.parent / "tests" / "test_pdf_sandbox_choice_30d.py").read_text(encoding="utf-8")
    assert 'monkeypatch.setenv(pdf.ELEVATION_ENV, "1")' in text, "no test asks for privilege through the seam"
    assert 'monkeypatch.setattr(os, "geteuid", lambda: 0)' not in text, "a test still fakes root through os.geteuid"


def test_linux_packaging_tests_do_not_run_on_windows() -> None:
    """`install.sh`, `dpkg` and `/bin/sh` do not exist on a Windows runner."""
    text = (SRC.parent / "tests" / "test_linux_packages_28b.py").read_text(encoding="utf-8")
    assert "allow_module_level" in text, "the POSIX-only packaging module runs on Windows anyway"


def test_an_elevated_offscreen_dialog_returns_in_a_fresh_process() -> None:
    """Production code never blocks on a question nobody is there to answer, in a new process.

    A monkeypatch cannot reach a subprocess, so this is the case the seams are really for: a
    separate interpreter that *is* elevated (the variable says so, as it does on a Windows runner
    or in a root container), has no window server, and is not given `ATE_AUTO_DISMISS_DIALOG`.
    Every modal ATE opens goes through `_dismiss_if_headless`; if that guard is removed, `exec()`
    waits for a click that can never happen and this test times out instead of passing."""
    env = dict(os.environ, PYTHONPATH=str(SRC), QT_QPA_PLATFORM="offscreen")
    env[pdf.ELEVATION_ENV] = "1"
    code = (
        "from ate.app import _pdf_sandbox_dialog, _close_confirm_dialog; "
        "print(_pdf_sandbox_dialog(), _close_confirm_dialog('Close ATE and stop the run?'))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False False", (
        f"a headless ATE answered {proc.stdout.strip()!r} where it should answer its negative branch"
    )
