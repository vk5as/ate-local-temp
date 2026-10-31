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
    assert isinstance(minutes, int) and 10 <= minutes <= 60, f"the pytest job has no timeout: {minutes!r}"


def test_the_test_step_names_each_test_and_dumps_a_stuck_stack() -> None:
    """A hang must be readable: which test was running, and where its thread was parked."""
    command = _pytest_command()
    assert "-v" in command, f"pytest is silent, so a killed job shows no last test: {command!r}"
    assert "--durations" in command, "the slowest tests are never reported"
    assert "faulthandler_timeout" in command, "a stuck test is killed without a stack trace"
    timeout = int(command.split("faulthandler_timeout=")[1].split()[0])
    assert 60 <= timeout <= 600, f"faulthandler_timeout={timeout} is either too twitchy or too late"


def test_pytest_timeout_is_still_not_a_dependency() -> None:
    """The suite bounds a hang with faulthandler, not with a second plugin.

    If someone adds `pytest-timeout`, this test makes them say why: the point of the fix is the
    stack dump, and a timeout plugin kills the test before the dump is useful."""
    text = PYPROJECT.read_text(encoding="utf-8")
    assert "pytest-timeout" not in text, "pytest-timeout was added without replacing the faulthandler approach"


def test_every_release_job_is_bounded() -> None:
    """`release.yml` has no timeouts either, and its Windows job is the one that hangs."""
    for name, job in yaml.safe_load(RELEASE_FILE.read_text(encoding="utf-8"))["jobs"].items():
        minutes = job.get("timeout-minutes")
        assert isinstance(minutes, int) and minutes >= 10, f"release job {name} has no timeout-minutes"
