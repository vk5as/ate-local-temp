"""Packet 27c: Windows release CI.

Two things are checked here, and neither of them is the thing that ultimately matters:

* `packaging/windows/smoke_installer.py` — the script that runs on the Windows runner. Its PATH
  logic and every assertion are plain functions over values, and the install/upgrade/uninstall
  sequence runs against a fake registry and a fake installer. **The installer itself is never
  executed**: there is no Windows and no `iscc` on this machine, and this workflow has never run.
  The fake models what `ate.iss` promises (one PATH entry, `_internal` cleared before an upgrade,
  the data directory untouched), so these tests prove the smoke script notices when those promises
  are broken — not that the promises are kept.
* `.github/workflows/release.yml` — read as text (and as YAML when PyYAML is available), checked
  for the trigger shape, the Python version, `fetch-depth: 0`, the smoke step and the absence of
  a branch-push trigger or an echoed secret.

`ate _selftest --scan <dir>` is the headless launch 27c needs: it reads the Station config,
refuses a database that is not at the schema version this ATE expects, and scans a Test Definition
directory. That part really does run here, on Linux, against the shipped example Media Release.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "release.yml"
SMOKE = REPO / "packaging" / "windows" / "smoke_installer.py"
ISS = REPO / "packaging" / "windows" / "ate.iss"
EXAMPLE = REPO / "examples" / "media-release"
SRC = str(REPO / "src")


# --- the headless scan: `ate _selftest --scan <dir>` -------------------------------------------


def _station(root: Path, schema: int = 5) -> Path:
    """A Station in `root`: a config with absolute paths and a database at `schema`."""
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    config = root / "config.json"
    config.write_text(
        json.dumps(
            {
                "format": 1,
                "station": {"name": "ci", "uuid": "00000000-0000-7000-8000-000000000002"},
                "testDefinitions": {"directories": [str(EXAMPLE)]},
                "database": {"type": "sqlite", "path": str(data / "ate.db")},
                "reports": {"directory": str(data / "reports")},
                "log": {"directory": str(data / "logs")},
            }
        ),
        encoding="utf-8",
    )
    from ate.storage import RunStore

    RunStore(data / "ate.db").close()
    import sqlite3

    conn = sqlite3.connect(data / "ate.db")
    conn.execute(f"PRAGMA user_version={schema}")
    conn.commit()
    conn.close()
    return config


def _run_cli(args: list[str], env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """`python -m ate ...` in a real subprocess, offscreen — the way the CI invokes it."""
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    env["QT_QPA_PLATFORM"] = "offscreen"
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "ate", *args], capture_output=True, text=True, env=env, timeout=120)


def test_scan_reports_the_shipped_example_release(tmp_path):
    config = _station(tmp_path)
    proc = _run_cli(["_selftest", "--scan", str(EXAMPLE), "--config", str(config)])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "6 schedules" in out, out
    assert "0 broken" in out, out
    assert "0 mismatched" in out, out
    assert "schema 5 == 5" in out, out  # the database this ATE expects is the one it found


def test_scan_fails_on_a_database_at_the_wrong_version(tmp_path):
    config = _station(tmp_path, schema=4)
    proc = _run_cli(["_selftest", "--scan", str(EXAMPLE), "--config", str(config)])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "scan: MISSING" in out, out
    assert "migrate" in out, out


def test_scan_fails_when_the_directory_has_no_test_definitions(tmp_path):
    config = _station(tmp_path)
    empty = tmp_path / "nothing-here"
    empty.mkdir()
    proc = _run_cli(["_selftest", "--scan", str(empty), "--config", str(config)])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "no Test Definitions" in out, out


def test_scan_fails_on_a_broken_media_release(tmp_path):
    config = _station(tmp_path)
    release = tmp_path / "broken-release"
    release.mkdir()
    schedule = release / "oops.tslua"
    schedule.write_text("return ScheduleParams('broken', 'no params')\n", encoding="utf-8")
    (release / "broken.atedef").write_text(
        json.dumps({"format": 1, "mediaNumber": "MR-BROKEN", "name": "Broken", "version": "not semver", "files": []}),
        encoding="utf-8",
    )
    proc = _run_cli(["_selftest", "--scan", str(release), "--config", str(config)])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "1 broken" in out, out


def test_scan_fails_on_a_checksum_mismatch_in_the_release(tmp_path):
    config = _station(tmp_path)
    release = tmp_path / "tampered"
    shutil.copytree(EXAMPLE, release)
    (release / "schedule_basics.tslua").write_text(
        (release / "schedule_basics.tslua").read_text(encoding="utf-8") + "\n-- touched by the smoke test\n", encoding="utf-8"
    )
    proc = _run_cli(["_selftest", "--scan", str(release), "--config", str(config)])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "1 mismatched" in out, out


def test_scan_is_hidden_from_help():
    proc = _run_cli(["--help"])
    assert "_selftest" not in proc.stdout, proc.stdout


# --- release.yml -------------------------------------------------------------------------------


def test_release_workflow_exists():
    assert WORKFLOW.is_file(), "release.yml is missing"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _workflow_code() -> str:
    """The workflow without comment lines: a fact stated in a comment is not a fact the runner obeys."""
    return "\n".join(line for line in _workflow_text().splitlines() if not line.strip().startswith("#"))


def test_release_workflow_triggers_on_tags_only():
    text = _workflow_code()
    assert "tags" in text
    assert "workflow_dispatch" in text
    assert re.search(r"^\s*push:\s*$", text, re.MULTILINE), "push must be narrowed to tags"
    # A push to a branch would start a release build on every merge.
    assert not re.search(r"branches\s*:", text), "release CI must not trigger on branch pushes"


def test_release_workflow_builds_with_python_3_14_and_full_tag_history():
    code = _workflow_code()
    assert 'python-version: "3.14"' in code, "the frozen bundles are required to carry CPython 3.14"
    assert "fetch-depth: 0" in code, "the version comes from the git tag: tags must be fetched"
    assert "smoke_installer.py" in code, "the workflow must run the smoke script"
    assert "upload-artifact" in code, "the installer goes to an artifact"
    assert "build_pyinstaller.py --smoke" in code, "27a's smoke build runs before the installer"
    assert "build_installer.py" in code, "the .iss is compiled by 27b's wrapper"
    assert "runs-on: windows-latest" in code


def test_release_workflow_passes_the_signing_secrets_and_nothing_else():
    code = _workflow_code()
    # The names build_installer.py reads; an empty secret there means an unsigned build.
    assert re.search(r"ATE_SIGN_CERT:\s*\$\{\{\s*secrets\.ATE_SIGN_CERT\s*\}\}", code)
    assert re.search(r"ATE_SIGN_PASSWORD:\s*\$\{\{\s*secrets\.ATE_SIGN_PASSWORD\s*\}\}", code)
    for line in _workflow_text().splitlines():
        if "secrets." not in line:
            continue
        assert re.match(r"^\s*[A-Z_]+:\s*\$\{\{\s*secrets\.[A-Z_]+\s*\}\}\s*$", line), f"secret used outside an env assignment: {line!r}"


def test_release_workflow_leaves_room_for_the_linux_jobs():
    assert "28c" in _workflow_text(), "the Linux artifacts (28c) need a named place in this workflow"


# --- the smoke script: its assertions against a fake registry and a fake installer ------------


def _load_smoke():
    spec = importlib.util.spec_from_file_location("smoke_installer", SMOKE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["smoke_installer"] = module  # @dataclass in the script needs its module to exist
    spec.loader.exec_module(module)
    return module


smoke = _load_smoke()

BASE_PATH = r"C:\Windows\system32;C:\Windows;C:\Users\Operator\bin"


class FakeRegistry:
    """HKLM's PATH value and its type, as a pair of plain values."""

    def __init__(self, value=BASE_PATH, type_=None):
        self.value = value
        self.type = smoke.REG_EXPAND_SZ if type_ is None else type_

    def read(self):
        return self.value, self.type

    def write(self, value, type_):
        self.value, self.type = value, type_


class FakeInstaller:
    """What ate.iss promises, modelled on a filesystem: one PATH entry, `_internal` cleared before
    an upgrade copies the bundle, `%PROGRAMDATA%\\ATE` untouched by both install and uninstall.

    Every keyword below is a way the installer could break that promise; each one is a test."""

    def __init__(
        self,
        registry: FakeRegistry,
        app_dir: Path,
        data_dir: Path,
        *,
        install_exit=0,
        add_path=True,
        duplicate=False,
        path_type=None,
        clobber_others=False,
        keep_stale=False,
        wipe_data=False,
        leave_path_on_uninstall=False,
        uninstall_polls=2,
        uninstall_never_finishes=False,
        remove_data_on_uninstall=False,
        no_config=False,
        no_db=False,
        no_selftest_line=False,
        log_error=None,
        schedules=6,
        broken=0,
        mismatches=0,
        schema=5,
        version="1.2.3",
    ):
        self.registry = registry
        self.app_dir = Path(app_dir)
        self.data_dir = Path(data_dir)
        self.flags = dict(
            install_exit=install_exit,
            add_path=add_path,
            duplicate=duplicate,
            path_type=path_type,
            clobber_others=clobber_others,
            keep_stale=keep_stale,
            wipe_data=wipe_data,
            leave_path_on_uninstall=leave_path_on_uninstall,
            uninstall_polls=uninstall_polls,
            uninstall_never_finishes=uninstall_never_finishes,
            remove_data_on_uninstall=remove_data_on_uninstall,
            no_config=no_config,
            no_db=no_db,
            no_selftest_line=no_selftest_line,
            log_error=log_error,
            schedules=schedules,
            broken=broken,
            mismatches=mismatches,
            schema=schema,
            version=version,
        )
        self.pending_uninstall: int | None = None
        self.installs = 0

    def run(self, argv: list[str], env: dict[str, str] | None = None) -> smoke.CommandResult:
        exe = Path(argv[0])
        if exe.name == "unins000.exe":
            self.installs -= 0  # uninstall is not an install
            self.pending_uninstall = None if self.flags["uninstall_never_finishes"] else self.flags["uninstall_polls"]
            return smoke.CommandResult(0, "", "")
        if exe.name in ("setup.exe", "ate-1.2.3-windows-x64-setup.exe"):
            return self._install(argv)
        # Everything else is `ate`: it has to be reachable on the PATH we handed the process.
        path = (env or {}).get("PATH", "")
        if exe.name not in ("ate", "ate.exe"):
            return smoke.CommandResult(1, "", f"not found: {exe}")
        if smoke.normalise_entry(str(self.app_dir)) not in [smoke.normalise_entry(e) for e in smoke.path_entries(path)]:
            return smoke.CommandResult(1, "", f"'ate' is not on the PATH the process was given: {path}")
        command = argv[1:]
        if command == ["--version"]:
            return smoke.CommandResult(0, self.flags["version"] + "\n", "")
        if command[:2] == ["db", "status"]:
            if not self.data_dir.joinpath("ate.db").is_file():
                return smoke.CommandResult(1, "", "database does not exist")
            return smoke.CommandResult(0, f"{self.flags['schema']}\n", "")
        if command[:2] == ["_selftest", "--run"]:
            return smoke.CommandResult(0, "run end-to-end: ok - selftest ran\n", "")
        if command[:2] == ["_selftest", "--scan"]:
            out = (
                f"scan: {'ok' if self.flags['broken'] == 0 and self.flags['mismatches'] == 0 else 'MISSING'} - "
                f"{command[2]}: {self.flags['schedules']} schedules, {self.flags['broken']} broken, "
                f"{self.flags['mismatches']} mismatched (schema {self.flags['schema']} == {self.flags['schema']})"
            )
            code = 0 if self.flags["broken"] == 0 and self.flags["mismatches"] == 0 else 1
            return smoke.CommandResult(code, out + "\n", "")
        return smoke.CommandResult(2, "", f"unknown ate command: {command}")

    def _install(self, argv: list[str]) -> smoke.CommandResult:
        self.installs += 1
        for flag in smoke.INSTALL_FLAGS:
            if flag not in argv:
                raise AssertionError(f"run_smoke did not pass {flag} to setup.exe")
        log_arg = next((a for a in argv if a.startswith("/LOG=")), None) or "/LOG=install.log"
        log_path = Path(log_arg[len("/LOG=") :])
        log_lines = ["- Setup logging started.", "- Installing file ate.exe"]
        if not self.flags["no_selftest_line"]:
            log_lines.append("- " + smoke.SETUP_LOG_LINE + ": ate.exe setup")
        if self.flags["log_error"]:
            log_lines.append("- Error: " + self.flags["log_error"])

        # [InstallDelete] clears {app}\_internal, then the bundle is copied in.
        self.app_dir.mkdir(parents=True, exist_ok=True)
        internal = self.app_dir / "_internal"
        if not self.flags["keep_stale"]:
            shutil.rmtree(internal, ignore_errors=True)
        internal.mkdir(parents=True, exist_ok=True)
        (self.app_dir / "ate.exe").write_text("the bundle", encoding="utf-8")
        (self.app_dir / "unins000.exe").write_text("the uninstaller", encoding="utf-8")

        others = smoke.other_path_entries(self.registry.value, str(self.app_dir))
        if self.flags["clobber_others"]:
            others = others[:-1] + [r"C:\Program Files\SomethingElse"]
        if self.flags["add_path"]:
            entries = others + [str(self.app_dir), str(self.app_dir)] if self.flags["duplicate"] else others + [str(self.app_dir)]
            self.registry.write(";".join(entries), self.flags["path_type"] or smoke.REG_EXPAND_SZ)
        else:
            self.registry.write(";".join(others), self.flags["path_type"] or smoke.REG_EXPAND_SZ)

        # `ate setup` at the end of the install: the Station's config and database.
        if not self.flags["no_config"]:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            (self.data_dir / "config.json").write_text(
                json.dumps(
                    {
                        "format": 1,
                        "testDefinitions": {"directories": [str(self.data_dir / "definitions")]},
                        "database": {"type": "sqlite", "path": str(self.data_dir / "ate.db")},
                    }
                ),
                encoding="utf-8",
            )
        if not self.flags["no_db"]:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            (self.data_dir / "ate.db").write_text("sqlite bytes", encoding="utf-8")
        if self.flags["wipe_data"] and self.installs > 1:
            shutil.rmtree(self.data_dir, ignore_errors=True)

        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return smoke.CommandResult(self.flags["install_exit"], "", "")

    def clock(self, _seconds: float) -> None:
        """The fake `sleep`: the uninstaller re-spawns itself, and this is when it finally runs."""
        if self.pending_uninstall is None:
            return
        self.pending_uninstall -= 1
        if self.pending_uninstall > 0:
            return
        self.pending_uninstall = None
        shutil.rmtree(self.app_dir, ignore_errors=True)
        if self.flags["remove_data_on_uninstall"]:
            shutil.rmtree(self.data_dir, ignore_errors=True)
        if not self.flags["leave_path_on_uninstall"]:
            cleaned = smoke.other_path_entries(self.registry.value, str(self.app_dir))
            self.registry.write(";".join(cleaned), self.registry.type)


def _smoke(tmp_path: Path, **flags):
    """Run the smoke sequence against a fake registry/installer; returns (checks, registry, runner)."""
    registry = FakeRegistry()
    app = tmp_path / "ATE"
    data = tmp_path / "ProgramData" / "ATE"
    installer = tmp_path / "dist" / "ate-1.2.3-windows-x64-setup.exe"
    installer.parent.mkdir(parents=True, exist_ok=True)
    installer.write_text("setup", encoding="utf-8")
    runner = FakeInstaller(registry, app, data, **flags)
    checks = smoke.run_smoke(
        installer=installer,
        app_dir=app,
        data_dir=data,
        registry=registry,
        runner=runner,
        sample_dir=EXAMPLE,
        install_log=tmp_path / "install.log",
        sleep=runner.clock,
        poll_limit=10,
    )
    return checks, registry, runner


def test_smoke_happy_path_installs_upgrades_and_uninstalls(tmp_path):
    checks, registry, runner = _smoke(tmp_path)
    expected = [
        "installer exit code 0 and a clean /LOG",
        "one {app} entry on the machine PATH, others unchanged, REG_EXPAND_SZ",
        "ate --version runs from the refreshed PATH",
        "reports schema version 5",
        "the frozen bundle runs a Test Schedule end to end",
        "headless launch scanned the sample Media Release",
        "upgrade exit code 0 and a clean /LOG",
        "still exactly one PATH entry after the upgrade",
        "the marker in %PROGRAMDATA%\\ATE survived and the stale _internal file is gone",
        "unins000.exe finished and {app} is gone",
        "no {app} PATH entry left, other entries unchanged, REG_EXPAND_SZ",
        "%PROGRAMDATA%\\ATE and its marker survived the uninstall",
    ]
    for name in expected:
        assert any(name in check for check in checks), f"missing check: {name}"
    assert runner.installs == 2, "the smoke run installs, upgrades, then uninstalls"
    assert not (tmp_path / "ATE").exists(), "the uninstall removed the program"
    assert registry.value == BASE_PATH, f"the machine PATH ended up as {registry.value!r}"
    assert registry.type == smoke.REG_EXPAND_SZ
    assert (tmp_path / "ProgramData" / "ATE" / smoke.MARKER).is_file()


def test_path_helpers_compare_entries_as_directories():
    app = r"C:\Program Files\ATE"
    assert smoke.app_path_count(r"C:\Windows;C:\Program Files\ATE", app) == 1
    assert smoke.app_path_count(r"c:\program files\ate\;C:\Windows", app) == 1  # case and a trailing \ are the same directory
    assert smoke.app_path_count(r"C:\Windows;C:\Program Files\ATEX", app) == 0  # a prefix is not the entry
    assert smoke.other_path_entries(r"C:\Windows;%USERPROFILE%\bin;C:\Program Files\ATE", app) == ["C:\\Windows", "%USERPROFILE%\\bin"]
    assert smoke.path_entries("C:\\Windows;;C:\\Program Files\\ATE") == ["C:\\Windows", "C:\\Program Files\\ATE"]


def test_the_path_helpers_mirror_the_pascal_in_the_iss():
    iss = ISS.read_text(encoding="utf-8")
    # normalise_entry is SamePathEntry: lowercase, trailing backslash trimmed. Change the Pascal and
    # this fails, while the Python mirror keeps passing on its own.
    assert "TrimTrailingBackslash(Lowercase(Entry)) = TrimTrailingBackslash(Lowercase(Path))" in iss
    assert "RegWriteExpandStringValue(HKEY_LOCAL_MACHINE, EnvironmentKey, 'Path', Cleaned)" in iss
    assert f"Log('{smoke.SETUP_LOG_LINE}" in iss, "the smoke script looks for a line the script never writes"


def test_installer_flags_are_the_ones_the_packet_names():
    assert "/VERYSILENT" in smoke.INSTALL_FLAGS
    assert "/SUPPRESSMSGBOXES" in smoke.INSTALL_FLAGS
    assert "/CLOSEAPPLICATIONS" in smoke.INSTALL_FLAGS
    assert "/VERYSILENT" in smoke.UNINSTALL_FLAGS


def test_smoke_fails_when_the_installer_exits_non_zero(tmp_path):
    with pytest.raises(smoke.SmokeError) as exc:
        _smoke(tmp_path, install_exit=1)
    assert "exited 1" in str(exc.value)


def test_smoke_fails_when_the_log_reports_a_failure(tmp_path):
    with pytest.raises(smoke.SmokeError) as exc:
        _smoke(tmp_path, log_error='"ate setup" exited with code 1')
    assert "install log reports a failure" in str(exc.value)


def test_smoke_fails_when_the_log_never_shows_ate_setup(tmp_path):
    with pytest.raises(smoke.SmokeError) as exc:
        _smoke(tmp_path, no_selftest_line=True)
    assert "never shows the installer running 'ate setup'" in str(exc.value)


def test_smoke_fails_when_ate_is_not_on_the_path(tmp_path):
    with pytest.raises(smoke.SmokeError) as exc:
        _smoke(tmp_path, add_path=False)
    assert "not on the machine PATH" in str(exc.value)


def test_smoke_fails_on_a_duplicate_path_entry(tmp_path):
    with pytest.raises(smoke.SmokeError) as exc:
        _smoke(tmp_path, duplicate=True)
    assert "appears 2 times" in str(exc.value)


def test_smoke_fails_when_the_type_is_reg_sz(tmp_path):
    with pytest.raises(smoke.SmokeError) as exc:
        _smoke(tmp_path, path_type=smoke.REG_SZ)
    assert "REG_EXPAND_SZ" in str(exc.value)


def test_smoke_fails_when_other_entries_are_altered(tmp_path):
    with pytest.raises(smoke.SmokeError) as exc:
        _smoke(tmp_path, clobber_others=True)
    assert "entries other than" in str(exc.value)


def test_smoke_fails_when_the_upgrade_deletes_the_data_directory(tmp_path):
    with pytest.raises(smoke.SmokeError) as exc:
        _smoke(tmp_path, wipe_data=True)
    assert "in-place upgrade must keep" in str(exc.value)
    assert smoke.MARKER in str(exc.value)


def test_smoke_fails_when_uninstall_takes_the_data_directory(tmp_path):
    with pytest.raises(smoke.SmokeError) as exc:
        _smoke(tmp_path, remove_data_on_uninstall=True)
    assert "uninstall removes the program, never the Station's data" in str(exc.value)


def test_smoke_fails_when_the_stale_internal_file_survives(tmp_path):
    with pytest.raises(smoke.SmokeError) as exc:
        _smoke(tmp_path, keep_stale=True)
    assert "survived the upgrade" in str(exc.value)
    assert "_internal" in str(exc.value)


def test_smoke_fails_when_uninstall_leaves_the_path_entry(tmp_path):
    with pytest.raises(smoke.SmokeError) as exc:
        _smoke(tmp_path, leave_path_on_uninstall=True)
    assert "still on the machine PATH" in str(exc.value)


def test_smoke_fails_when_the_uninstaller_never_finishes(tmp_path):
    with pytest.raises(smoke.SmokeError) as exc:
        _smoke(tmp_path, uninstall_never_finishes=True)
    assert "never removed" in str(exc.value)
    assert "re-spawns" in str(exc.value)


def test_smoke_fails_when_the_scan_finds_broken_definitions(tmp_path):
    with pytest.raises(smoke.SmokeError) as exc:
        _smoke(tmp_path, broken=1)
    assert "broken Test Definitions" in str(exc.value)


def test_smoke_fails_when_no_database_is_created(tmp_path):
    with pytest.raises(smoke.SmokeError) as exc:
        _smoke(tmp_path, no_db=True)
    assert "created no database" in str(exc.value)


def test_smoke_fails_when_no_config_is_left_behind(tmp_path):
    with pytest.raises(smoke.SmokeError) as exc:
        _smoke(tmp_path, no_config=True)
    assert "config" in str(exc.value)


def test_smoke_prints_install_log_when_it_fails(tmp_path, capsys):
    """main()'s failure path: the message, then the log, then exit 1."""
    log = tmp_path / "install.log"
    log.write_text("- Setup logging started.\n- Error: \"ate setup\" exited with code 1\n", encoding="utf-8")
    installer = tmp_path / "ate-1.2.3-windows-x64-setup.exe"
    installer.write_text("setup", encoding="utf-8")

    def boom(**_kwargs):
        raise smoke.SmokeError("install: the installer exited 1")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(smoke, "IS_WINDOWS", True)
    monkeypatch.setattr(smoke, "run_smoke", boom)
    code = smoke.main(["--installer", str(installer), "--install-log", str(log)])
    monkeypatch.undo()
    captured = capsys.readouterr().err
    assert code == 1
    assert "smoke test failed: install: the installer exited 1" in captured
    assert '"ate setup" exited with code 1' in captured, "a failure without the log is not diagnosable"


def test_smoke_refuses_to_run_where_setup_exe_cannot_run(tmp_path, capsys):
    assert smoke.IS_WINDOWS is False, "these tests run on Linux"
    code = smoke.main(["--installer", str(tmp_path / "does-not-exist.exe")])
    assert code == 1
    assert "release CI" in capsys.readouterr().err


def test_release_workflow_parses_as_yaml_when_yaml_is_available():
    try:
        import yaml
    except ImportError:
        pytest.skip("PyYAML is not installed; the text checks above still ran")
    doc = yaml.safe_load(_workflow_text())
    on = doc[True] if True in doc else doc["on"]
    assert set(on) == {"push", "workflow_dispatch"}, on
    assert set(on["push"]) == {"tags"}, "push must be restricted to tags"
    job = doc["jobs"]["windows-installer"]
    assert job["runs-on"] == "windows-latest"
    steps = job["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0
    setup = next(s for s in steps if "setup-python" in str(s.get("uses", "")))
    assert setup["with"]["python-version"] == "3.14"
    assert not any("run" in s and "secrets." in s["run"] for s in steps), "a secret must not reach a run line"
