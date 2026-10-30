#!/usr/bin/env python3
r"""Smoke-test ATE's Windows installer on a clean runner (packet 27c).

Run by `.github/workflows/release.yml` on a tagged release, after
`packaging/build_pyinstaller.py --smoke` built the bundle and
`packaging/windows/build_installer.py` compiled `ate.iss`:

    python packaging/windows/smoke_installer.py

It installs silently, checks the machine PATH and the installer's exit code and `/LOG` file,
runs the Station setup the installer did, scans the example Media Release headless, installs a
second time over the first (upgrade) and then uninstalls — asserting at every step that the PATH
has exactly the entry it should have, that the other entries are untouched, and that the value is
still `REG_EXPAND_SZ`.

**NEVER RUN.** No machine that has ever written or read this file has had Windows, Inno Setup, or
a `%PROGRAMDATA%\ATE` to install into, and no tag of this repository has been cut, so the release
workflow has never started. `tests/test_release_ci_27c.py` runs the assertions below against a
fake registry and a fake installer on Linux: it proves the script notices a broken install, not
that an install works. The first real install, upgrade and uninstall is this script's first run.

Code signing is not touched here: the CI passes `ATE_SIGN_CERT`/`ATE_SIGN_PASSWORD` to the build
only when the repository has them, and an empty value means an unsigned artifact. Nothing in this
script uploads or publishes anything; the installer goes to `actions/upload-artifact`.

The PATH logic and every assertion are plain functions over values, so they can be exercised
without Windows; only `run_smoke` and the two classes at the bottom touch the registry or spawn
processes, and both are injected in the tests.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
IS_WINDOWS = os.name == "nt"

# Imported only where they exist: this file is read and its assertions run on Linux.
if IS_WINDOWS:
    import subprocess
    import winreg

# The registry value type numbers. PATH is REG_EXPAND_SZ because it holds %VAR% entries; if
# something writes it back as REG_SZ those entries stop expanding for every user.
REG_SZ = 1
REG_EXPAND_SZ = 2
PATH_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"

INSTALL_FLAGS = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS")
UNINSTALL_FLAGS = ("/VERYSILENT", "/SUPPRESSMSGBOXES")
MARKER = "27c-smoke-marker.txt"
STALE = Path("_internal") / "stale-from-the-previous-build.py"  # a file the previous build shipped

# What a failed install looks like in Inno's /LOG. ate.iss records `ate setup`'s exit code with
# "exited with code", and Inno prefixes every error line with "Error".
LOG_FAILURE_MARKERS = ("error", "exited with code", "could not", "failed")
# ate.iss logs this before running setup; tests pin the line in the script so this is not a guess.
SETUP_LOG_LINE = "Preparing the Station"


class SmokeError(Exception):
    """The first assertion that failed. The run stops there."""


def fail(message: str) -> None:
    raise SmokeError(message)


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    def output(self) -> str:
        return (self.stdout + self.stderr).strip()


# --- PATH: plain functions over values ----------------------------------------------------------


def normalise_entry(entry: str) -> str:
    """A PATH entry as a directory: {app} never ends in a backslash, and paths are not names."""
    return entry.strip().rstrip("\\").lower()


def path_entries(value: str) -> list[str]:
    """The `;`-separated entries of a PATH value, blanks dropped."""
    return [entry for entry in value.split(";") if entry.strip()]


def app_path_count(value: str, app_dir: str) -> int:
    return sum(1 for entry in path_entries(value) if normalise_entry(entry) == normalise_entry(app_dir))


def other_path_entries(value: str, app_dir: str) -> list[str]:
    return [entry for entry in path_entries(value) if normalise_entry(entry) != normalise_entry(app_dir)]


def check_registry_type(stage: str, value_type: int) -> None:
    if value_type != REG_EXPAND_SZ:
        fail(
            f"{stage}: the machine PATH is stored as type {value_type} (REG_SZ is {REG_SZ}), not "
            f"REG_EXPAND_SZ ({REG_EXPAND_SZ}): every %VAR% in it stops expanding for every user"
        )


def check_machine_path(stage: str, before: str, after: str, before_type: int, after_type: int, app_dir: str, expected: int) -> None:
    """One PATH entry for {app} (or none), everything else exactly as it was, still REG_EXPAND_SZ."""
    check_registry_type(stage, after_type)
    found = app_path_count(after, app_dir)
    if found != expected:
        if expected == 0:
            fail(f"{stage}: {app_dir} is still on the machine PATH ({found} entries) after uninstalling")
        if found > expected:
            fail(
                f"{stage}: {app_dir} appears {found} times in the machine PATH. NeedsAddPath is there "
                f"to stop an upgrade adding the directory again"
            )
        fail(f"{stage}: {app_dir} is not on the machine PATH at all")
    kept = other_path_entries(after, app_dir)
    previous = other_path_entries(before, app_dir)
    if kept != previous:
        fail(
            f"{stage}: the installer changed the machine PATH entries other than {app_dir}: "
            f"before {previous}, after {kept}"
        )
    return True


def check_install_log(result: CommandResult, log_text: str, stage: str) -> None:
    """The exit code and the /LOG file are the only signs of a silent install's failure."""
    if result.returncode != 0:
        fail(
            f"{stage}: the installer exited {result.returncode}. GetCustomSetupExitCode returns "
            f"non-zero when 'ate setup' failed, so a silent install that prepares no Station is "
            f"not a green run. Output: {result.output()[-400:]}"
        )
    if not log_text.strip():
        fail(f"{stage}: /LOG produced no log file, so there is nothing to prove the install did")
    hits = [line for line in log_text.splitlines() if any(marker in line.lower() for marker in LOG_FAILURE_MARKERS)]
    if hits:
        fail(f"{stage}: the install log reports a failure: {hits[0].strip()}")
    if SETUP_LOG_LINE.lower() not in log_text.lower():
        fail(
            f"{stage}: the log never shows the installer running 'ate setup' (it logs "
            f"{SETUP_LOG_LINE!r}), so the Station was not prepared"
        )
    return True


def check_ate_runs(config_path: Path, ate_result: CommandResult, app_dir: str) -> None:
    """`ate --version` works in a new process with the refreshed PATH, and `ate db status` prints a
    schema version for a database that exists."""
    if ate_result.returncode != 0:
        fail(f"ate is not runnable from the machine PATH ({app_dir}): exit {ate_result.returncode}: {ate_result.output()[-300:]}")
    if not ate_result.stdout.strip():
        fail("ate --version printed nothing")
    if not config_path.is_file():
        fail(f"the installer did not leave a config at {config_path}: 'ate setup' never ran")
    return True


def check_scan(result: CommandResult, definitions: Path) -> None:
    """The headless launch scanned the sample release: schedules found, nothing broken, no mismatch."""
    out = result.output()
    if "0 broken" not in out:
        fail(f"the headless scan of {definitions} reported broken Test Definitions: {out[-400:]}")
    if "0 mismatched" not in out:
        fail(f"the headless scan of {definitions} reported checksum mismatches: {out[-400:]}")
    if "0 schedules" in out:
        fail(f"the headless scan of {definitions} found no Test Schedule: copying the example release failed")
    if result.returncode != 0:
        fail(f"the headless scan of {definitions} exited {result.returncode}: {out[-400:]}")
    return True


# --- the sequence -----------------------------------------------------------------------------


def run_smoke(
    installer: Path,
    app_dir: Path,
    data_dir: Path,
    registry,
    runner,
    sample_dir: Path,
    ate: str = "ate",
    install_log: Path | None = None,
    sleep=time.sleep,
    poll_limit: int = 60,
) -> list[str]:
    """Install, smoke, upgrade, uninstall. Returns the checks that passed."""
    log = Path(install_log) if install_log else Path.cwd() / "install.log"
    passed: list[str] = []

    def note(name: str) -> None:
        passed.append(name)
        print(f"ok: {name}")

    def flags() -> list[str]:
        return [*INSTALL_FLAGS, f"/LOG={log}"]

    before_value, before_type = registry.read()
    print(f"machine PATH before install: {before_value}")

    result = runner.run([str(installer), *flags()])
    check_install_log(result, log.read_text(encoding="utf-8") if log.is_file() else "", "install")
    note("installer exit code 0 and a clean /LOG")

    after_value, after_type = registry.read()
    check_machine_path("install", before_value, after_value, before_type, after_type, str(app_dir), 1)
    note("one {app} entry on the machine PATH, others unchanged, REG_EXPAND_SZ")

    env = dict(os.environ, PATH=after_value)  # a NEW process sees the PATH Setup just wrote
    result = runner.run([ate, "--version"], env=env)
    check_ate_runs(data_dir / "config.json", result, str(app_dir))
    note("ate --version runs from the refreshed PATH")

    config = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
    database = Path(config.get("database", {}).get("path", ""))
    if not database.is_file():
        fail(f"'ate setup' created no database at {database}")
    result = runner.run([ate, "db", "status"], env=env)
    if result.returncode != 0 or not result.stdout.strip().isdigit():
        fail(f"ate db status did not print a schema version: exit {result.returncode}: {result.output()[-300:]}")
    note(f"database at {database} reports schema version {result.stdout.strip()}")

    result = runner.run([ate, "_selftest", "--run"], env=env)
    if result.returncode != 0:
        fail(f"ate _selftest --run failed: {result.output()[-600:]}")
    note("the frozen bundle runs a Test Schedule end to end")

    definitions = Path((config.get("testDefinitions", {}).get("directories") or [str(data_dir / "definitions")])[0])
    shutil.copytree(sample_dir, definitions, dirs_exist_ok=True)
    result = runner.run([ate, "_selftest", "--scan", str(definitions)], env=env)
    check_scan(result, definitions)
    note("headless launch scanned the sample Media Release")

    # Upgrade: a marker outside {app} and a stale file inside it. Both are what 27b promises an
    # in-place upgrade handles: data survives, _internal does not.
    marker = data_dir / MARKER
    marker.write_text("written before the upgrade", encoding="utf-8")
    stale = app_dir / STALE
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("a file the previous build shipped and this one does not", encoding="utf-8")
    before_upgrade_value, before_upgrade_type = registry.read()
    result = runner.run([str(installer), *flags()])
    check_install_log(result, log.read_text(encoding="utf-8") if log.is_file() else "", "upgrade")
    note("upgrade exit code 0 and a clean /LOG")
    after_upgrade_value, after_upgrade_type = registry.read()
    check_machine_path("upgrade", before_upgrade_value, after_upgrade_value, before_upgrade_type, after_upgrade_type, str(app_dir), 1)
    note("still exactly one PATH entry after the upgrade")
    if not marker.is_file():
        fail(f"the upgrade removed {marker}: an in-place upgrade must keep %PROGRAMDATA%\\ATE")
    if marker.read_text(encoding="utf-8") != "written before the upgrade":
        fail(f"the upgrade rewrote {marker}")
    if stale.exists():
        fail(
            f"{stale} survived the upgrade: [InstallDelete] is meant to clear {app_dir}\\_internal "
            f"before the new bundle is copied, so nothing stale is left behind"
        )
    note("the marker in %PROGRAMDATA%\\ATE survived and the stale _internal file is gone")

    # Uninstall: unins000 re-spawns itself, so wait for {app} to disappear before checking.
    result = runner.run([str(app_dir / "unins000.exe"), *UNINSTALL_FLAGS])
    if result.returncode != 0:
        fail(f"unins000.exe exited {result.returncode}: {result.output()[-300:]}")
    for _ in range(poll_limit):
        if not app_dir.exists():
            break
        sleep(1)
    else:
        fail(f"the uninstaller never removed {app_dir} within {poll_limit} polls — it re-spawns itself, and this wait gave up")
    note("unins000.exe finished and {app} is gone")
    after_uninstall_value, after_uninstall_type = registry.read()
    check_machine_path("uninstall", before_value, after_uninstall_value, before_type, after_uninstall_type, str(app_dir), 0)
    note("no {app} PATH entry left, other entries unchanged, REG_EXPAND_SZ")
    if not data_dir.is_dir():
        fail(f"uninstalling removed {data_dir}: uninstall removes the program, never the Station's data")
    if not marker.is_file():
        fail(f"uninstalling removed the marker in {data_dir}: Test Runs and config are records")
    note("%PROGRAMDATA%\\ATE and its marker survived the uninstall")

    return passed


# --- the Windows-only pieces the tests replace -----------------------------------------------------


class MachinePath:
    """Reads HKLM's PATH value and its type. Windows only, and never executed here."""

    def read(self) -> tuple[str, int]:
        if not IS_WINDOWS:
            raise SmokeError("the machine PATH is a Windows registry value; this test needs a fake")
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, PATH_KEY) as key:
            value, value_type = winreg.QueryValueEx(key, "Path")
        return str(value), int(value_type)


class SubprocessRunner:
    """Runs a command and hands back its exit code and output. Windows only in practice."""

    def run(self, argv: list[str], env: dict[str, str] | None = None) -> CommandResult:
        if not IS_WINDOWS:
            raise SmokeError("smoke_installer runs setup.exe and unins000.exe: it needs Windows")
        proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=900)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def find_installer(dist: Path) -> Path:
    candidates = sorted(dist.glob("ate-*-windows-x64-setup.exe"))
    if not candidates:
        raise SmokeError(
            f"no installer in {dist}: run packaging/windows/build_installer.py first "
            "(build_pyinstaller.py --smoke builds the bundle it compiles from)"
        )
    return candidates[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", default=str(REPO / "dist"), help="directory holding the installer")
    parser.add_argument("--installer", default=None, help="setup.exe to run (default: the newest in --dist)")
    parser.add_argument("--app-dir", default=None, help="where the installer puts the program (default: Program Files\\ATE)")
    parser.add_argument("--data-dir", default=None, help="the Station data directory (default: %PROGRAMDATA%\\ATE)")
    parser.add_argument("--sample", default=str(REPO / "examples" / "media-release"), help="Media Release to copy for the scan")
    parser.add_argument("--install-log", default="install.log", help="the /LOG file the installer writes")
    parser.add_argument("--poll-limit", type=int, default=60, help="how long to wait for the re-spawned uninstaller")
    args = parser.parse_args(argv)

    installer = Path(args.installer) if args.installer else find_installer(Path(args.dist))
    app_dir = Path(args.app_dir) if args.app_dir else Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "ATE"
    data_dir = Path(args.data_dir) if args.data_dir else Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "ATE"

    if not IS_WINDOWS:
        print(
            "smoke_installer.py installs setup.exe: it runs on the release CI's Windows runner, "
            "not here. Its PATH assertions are tested on Linux by tests/test_release_ci_27c.py.",
            file=sys.stderr,
        )
        return 1

    try:
        checks = run_smoke(
            installer=installer,
            app_dir=app_dir,
            data_dir=data_dir,
            registry=MachinePath(),
            runner=SubprocessRunner(),
            sample_dir=Path(args.sample),
            install_log=Path(args.install_log),
            poll_limit=args.poll_limit,
        )
    except SmokeError as exc:
        print(f"smoke test failed: {exc}", file=sys.stderr)
        log = Path(args.install_log)
        if log.is_file():
            print(f"--- {log} ---", file=sys.stderr)
            print(log.read_text(encoding="utf-8", errors="replace"), file=sys.stderr)
        return 1
    print(f"smoke test passed: {len(checks)} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
