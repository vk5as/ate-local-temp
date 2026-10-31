"""A CI job that cannot finish has to say so, and stop by itself.

The first real run of `.github/workflows/ci.yml` hung in "Run tests" on `windows-latest` with no
output for over an hour: GitHub's Windows runners are administrators, so packet 30d's unsandboxed-
PDF question opened a `QMessageBox`, and offscreen `exec()` never returns. A job that waits forever
is useless — the workflow has to bound the wait and print enough to find the stuck test.

`faulthandler_timeout` is pytest's own ini option (faulthandler is standard library and pytest
enables it): when one test runs longer than that, the stack of every thread is dumped into the log,
which is exactly what identifies a modal dialog nobody can answer. `pytest-timeout` is *not* a
dependency, and is not added: it would need its own install and floor, and it kills the stuck test
instead of showing where it is stuck.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
CI_FILE = REPO / ".github" / "workflows" / "ci.yml"
RELEASE_FILE = REPO / ".github" / "workflows" / "release.yml"
PYPROJECT = REPO / "pyproject.toml"


def _ci_doc() -> dict:
    return yaml.safe_load(CI_FILE.read_text(encoding="utf-8"))


def _pytest_command() -> str:
    for step in _ci_doc()["jobs"]["test"]["steps"]:
        if "pytest" in step.get("run", ""):
            return step["run"]
    return ""


def test_the_test_job_stops_by_itself() -> None:
    """No matrix entry can run for an hour: the job is bounded in minutes."""
    minutes = _ci_doc()["jobs"]["test"].get("timeout-minutes")
    values = re.findall(r"\d+", str(minutes))
    assert values, f"the pytest job has no timeout: {minutes!r}"
    assert all(10 <= int(m) <= 60 for m in values), f"the pytest job's bound is unreasonable: {minutes!r}"


def test_the_windows_entry_gets_the_room_it_needs() -> None:
    """Windows is the slow matrix entry, and it is the one that hangs, so it is bounded on its own.

    A bound tighter than the job needs turns a readable hang into a GitHub timeout with no output;
    the Windows entry is allowed more minutes than Linux precisely so faulthandler's dump lands
    before the runner is killed."""
    minutes = str(_ci_doc()["jobs"]["test"].get("timeout-minutes"))
    values = [int(m) for m in re.findall(r"\d+", minutes)]
    assert len(values) >= 2, f"the bound is not per matrix entry: {minutes!r}"
    assert max(values) >= 35, f"the Windows bound is {max(values)} minutes: too tight to be honest"
    assert "windows" in minutes.lower(), f"the bound does not single Windows out: {minutes!r}"


def test_the_test_step_names_each_test_and_dumps_a_stuck_stack() -> None:
    """A hang must be readable: which test was running, and where its thread was parked."""
    command = _pytest_command()
    assert "-v" in command, f"pytest is silent, so a killed job shows no last test: {command!r}"
    assert "--durations" in command, "the slowest tests are never reported"
    assert "faulthandler_timeout" in command, "a stuck test is killed without a stack trace"
    timeout = int(command.split("faulthandler_timeout=")[1].split()[0])
    assert 60 <= timeout <= 600, f"faulthandler_timeout={timeout} is either too twitchy or too late"
    assert "faulthandler_exit_on_timeout=true" in command, (
        "the dump is printed and pytest carries on into the same stuck test, so the job still "
        "runs to GitHub's wall clock"
    )
    assert "-rfEs" in command, "a long run reaches its end with no summary of what failed or was skipped"
    assert "--tb=short" in command, "the summary is a wall of full tracebacks nobody reads"


def test_pytest_timeout_is_still_not_a_dependency() -> None:
    """The suite bounds a hang with faulthandler, not with a second plugin.

    If someone adds `pytest-timeout`, this test makes them say why: the point of the fix is the
    stack dump, and a timeout plugin kills the test before the dump is useful."""
    text = PYPROJECT.read_text(encoding="utf-8")
    assert "pytest-timeout" not in text, "pytest-timeout was added without replacing the faulthandler approach"


def test_the_workflow_checks_the_qt_pair_before_running_tests() -> None:
    """The job says out loud whether QtWebEngine can be imported, before the suite starts.

    PyQt6 6.12 installs Qt 6.12, and PyQt6-WebEngine has no release for it: it pulls in
    PyQt6-WebEngine-Qt6 6.11.2, and two Qt builds in one process abort the import on Windows with
    0xc0000139 (STATUS_ENTRYPOINT_NOT_FOUND). In the first Windows run that crash was three lines
    of stack in the middle of collection, and the Test Report tests simply never ran. The check
    prints the versions and stops the job on a mismatch, so the cause is one line rather than a
    mystery skip."""
    steps = _ci_doc()["jobs"]["test"]["steps"]
    check = next((s for s in steps if "QtWebEngineCore" in s.get("run", "")), None)
    assert check, "no step checks that QtWebEngine imports on the runner"
    run = check["run"]
    assert "raise SystemExit" in run, "the check prints the versions and carries on regardless"
    pytest_step = next(i for i, s in enumerate(steps) if "pytest" in s.get("run", ""))
    assert steps.index(check) < pytest_step, "the pairing is checked after the suite has already run"


def test_every_release_job_is_bounded() -> None:
    """`release.yml` has no timeouts either, and its Windows job is the one that hangs."""
    for name, job in yaml.safe_load(RELEASE_FILE.read_text(encoding="utf-8"))["jobs"].items():
        minutes = job.get("timeout-minutes")
        assert isinstance(minutes, int) and minutes >= 10, f"release job {name} has no timeout-minutes"
