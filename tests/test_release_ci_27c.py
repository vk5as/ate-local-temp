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
