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
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
CI_FILE = REPO / ".github" / "workflows" / "ci.yml"
RELEASE_FILE = REPO / ".github" / "workflows" / "release.yml"
PYPROJECT = REPO / "pyproject.toml"


def _ci_doc() -> dict:
    return yaml.safe_load(CI_FILE.read_text(encoding="utf-8"))


def _matrix_os_values() -> list[str]:
    """The `os` entries the test job runs, exactly as the matrix lists them."""
    return [str(value) for value in _ci_doc()["jobs"]["test"]["strategy"]["matrix"]["os"]]


def _pytest_command() -> str:
    for step in _ci_doc()["jobs"]["test"]["steps"]:
        if "pytest" in step.get("run", ""):
            return step["run"]
    return ""


def _run_block_for(os_value: str) -> str:
    """The `Run tests` script as pwsh sees it: Actions substitutes `${{ matrix.os }}` from the matrix
    before the step reaches the shell, so anything that executes the block has to do the same."""
    return _pytest_command().replace("${{ matrix.os }}", os_value)


def _pytest_argv() -> list[str]:
    """The argv pytest is handed in the `Run tests` step, as pwsh builds it.

    The step splats an array, so the arguments are the quoted elements of `$pytestArgs` plus
    whatever the Linux branch appends to it. An empty list means the step does not build the
    command that way — which is the point of the test that uses it."""
    run = _pytest_command()
    args: list[str] = []
    for array in re.findall(r"@\(([^)]*)\)", run):
        args += re.findall(r"'([^']*)'", array)
    return args


#: The one line that adds the parallel flag: an `if` whose body is the append, nothing else.
_OS_GUARD = re.compile(
    r"if\s*\(\s*(?P<condition>.+?)\s*\)\s*\{\s*\$pytestArgs\s*\+=\s*@\('-n',\s*'auto'\s*\)\s*\}"
)


def _pwsh_argv_for(os_value: str, script_path: Path) -> list[str]:
    """Run the `Run tests` step in pwsh with `pytest` replaced by a stub that prints its argv.

    `${{ matrix.os }}` is filled in first, because Actions substitutes it before the step reaches the
    shell. The stub prints one `<arg>` per argument, so an empty argument shows up as `<>` and the
    argv can be read back exactly. pwsh parses the whole script before it runs any of it, so a bad
    operator in the guard is caught here as a ParserError with exit 1, which is how the job fails on
    a runner (packet 44 review)."""
    block = _run_block_for(os_value)
    assert block.count("pytest @pytestArgs") == 1, f"the step no longer calls `pytest @pytestArgs` once: {block!r}"
    script = block.replace("pytest @pytestArgs", "argv-dump @pytestArgs")
    script_path.write_text("function argv-dump { foreach ($a in $args) { '<' + $a + '>' } }\n" + script,
                           encoding="utf-8", newline="\n")
    proc = subprocess.run(
        [shutil.which("pwsh"), "-NoProfile", "-File", str(script_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"pwsh cannot run the Run tests step for {os_value} (exit {proc.returncode}): {proc.stderr.strip()}"
    )
    return re.findall(r"^<(.*)>$", proc.stdout, re.M)


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
    timeout = int(re.search(r"faulthandler_timeout=(\d+)", command).group(1))
    assert 60 <= timeout <= 600, f"faulthandler_timeout={timeout} is either too twitchy or too late"
    assert "faulthandler_exit_on_timeout=true" in command, (
        "the dump is printed and pytest carries on into the same stuck test, so the job still "
        "runs to GitHub's wall clock"
    )
    assert "-rfEs" in command, "a long run reaches its end with no summary of what failed or was skipped"
    assert "--tb=short" in command, "the summary is a wall of full tracebacks nobody reads"


def test_the_parallel_flag_is_two_arguments_and_no_empty_one() -> None:
    """`-n auto` must reach pytest-xdist as two argv elements, and Windows must pass nothing extra.

    pwsh hands a variable to a native command as a single argument, so `$workers = "-n auto"`
    plus `pytest ... $workers` gives pytest one argument, `-n auto`. xdist then reads the tail of
    it as the worker count and the job dies with `invalid parse_numprocesses value: ' auto'` and
    exit 4 on every Linux runner; on Windows the same shape put an empty `""` in the argv
    (packet 44 review). The step builds an array and splats it with `@`, which is what this
    checks."""
    run = _pytest_command()
    argv = _pytest_argv()
    assert argv, f"pytest is not run from an argument array: {run!r}"
    assert "pytest @pytestArgs" in run, f"pytest is called with the flags in one string: {run!r}"
    assert "-n auto" not in run, "the parallel flag is a single quoted string, not two arguments"
    assert "-n" in argv and "auto" in argv, f"xdist never gets its worker count: {argv}"
    assert argv[argv.index("-n") + 1] == "auto", f"-n is not its own argument: {argv}"
    assert "" not in argv, f"an empty argument reaches pytest on Windows: {argv}"
    for flag in ("-v", "-rfEs", "--tb=short", "--durations=20", "log_cli=false",
                "faulthandler_timeout=300", "faulthandler_exit_on_timeout=true"):
        assert flag in argv, f"{flag} is no longer an argument pytest is given: {argv}"
    guard = _OS_GUARD.search(run)
    assert guard, (
        "the -n append is not sitting alone inside an `if (...) { $pytestArgs += @('-n', 'auto') }` "
        f"guard: {run!r}"
    )
    condition = guard.group("condition")
    assert "-eq" in condition and "ubuntu-latest" in condition and "matrix.os" in condition, (
        f"the guard does not test the matrix OS against ubuntu-latest with pwsh's -eq: {condition!r}"
    )
    assert guard.start() < run.index("pytest @pytestArgs"), (
        "the -n append comes after pytest is called, so it never reaches pytest"
    )


def test_the_run_step_has_no_powertools_equals_operator() -> None:
    """PowerShell has no `==` or `!=`: the step is parsed before any of it runs, and a bad operator
    kills the job before pytest starts.

    GPT-6.1-sol ran the step in pwsh for both matrix entries with the guard written
    `if ('ubuntu-latest' == ...)` and got exit 1 and `The assignment expression is not valid` before
    pytest was reached, on every job in the matrix (packet 44 review). `-eq` and `-ne` are the
    operators pwsh has, so nothing in the block may use the other spelling."""
    run = _pytest_command()
    offenders = [m.group(0) for m in re.finditer(r"==|!=", run)]
    assert not offenders, f"pwsh has no {offenders} operator, so the Run tests step will not parse: {run!r}"


def test_pwsh_builds_the_argv_for_every_matrix_entry(tmp_path: Path) -> None:
    """The `Run tests` step is run in pwsh, with pytest replaced by a stub that prints its argv.

    Text assertions cannot tell a working guard from an inverted one: an `if` that tests `-ne`, or
    `-n auto` appended outside the guard, still reads as the right words in the right order (packet
    44 review). So the step is executed, per matrix entry, with `${{ matrix.os }}` substituted the way
    Actions does, and the argv each entry actually produces is checked: Linux ends with `-n` `auto`,
    Windows passes no worker flag and no empty argument. `pwsh` is on every GitHub runner; this
    machine has none, so the test skips there rather than asserting nothing."""
    if shutil.which("pwsh") is None:
        pytest.skip("pwsh is not installed here; every GitHub runner has it")
    for os_value in _matrix_os_values():
        argv = _pwsh_argv_for(os_value, tmp_path / "run-tests.ps1")
        assert argv, f"the stub printed no argv for {os_value}"
        assert "" not in argv, f"{os_value} passes an empty argument to pytest: {argv}"
        assert "-n auto" not in argv, f"-n auto reached pytest as one argument on {os_value}: {argv}"
        assert "-v" in argv, f"the step's flags never reached pytest on {os_value}: {argv}"
        if os_value == "ubuntu-latest":
            assert argv[-2:] == ["-n", "auto"], f"Linux did not add the worker count: {argv}"
        else:
            assert "-n" not in argv and "auto" not in argv, (
                f"{os_value} runs the suite in parallel, which has not been shown clean there: {argv}"
            )


def test_pytest_timeout_is_still_not_a_dependency() -> None:
    """The suite bounds a hang with faulthandler, not with a second plugin.

    If someone adds `pytest-timeout`, this test makes them say why: the point of the fix is the
    stack dump, and a timeout plugin kills the test before the dump is useful."""
    text = PYPROJECT.read_text(encoding="utf-8")
    assert "pytest-timeout" not in text, "pytest-timeout was added without replacing the faulthandler approach"


def test_the_workflow_checks_the_qt_pair_before_running_tests() -> None:
    """The job says out loud whether QtWebEngine can be imported, before the suite starts.

    Under PyQt6, 6.12 installed Qt 6.12 while PyQt6-WebEngine had no release for it and pulled in
    PyQt6-WebEngine-Qt6 6.11.2: two Qt builds in one process abort the import on Windows with
    0xc0000139 (STATUS_ENTRYPOINT_NOT_FOUND). `PySide6-Addons` pins `PySide6-Essentials` and
    `shiboken6` to its own version, so that resolve cannot happen now; the check still catches a
    mixed install. In the first Windows run that crash was three lines
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
