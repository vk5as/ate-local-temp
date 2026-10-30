"""Packet 27b: the Inno Setup installer.

There is no `iscc` (and no Wine) on this machine, so the `.iss` is **never compiled or run**
by these tests — they read it the way a compiler would read its sections and check the
things that decide whether an install is per-machine, upgradeable, and reversible:

* the `[Setup]` directives: fixed `AppId` (in-place upgrades key off it), admin,
  `{autopf}`, 64-bit mode, `ChangesEnvironment`, the artifact name;
* `[Files]`: the whole bundle, recursively, `ignoreversion`, nothing dropped;
* `[InstallDelete]`: `_internal` is cleared before an upgrade copies the new bundle;
* `[Registry]`: the machine PATH entry is guarded by `NeedsAddPath` and carries no
  `uninsdelete*` flag, which would wipe the whole PATH at uninstall;
* `[Code]`: `NeedsAddPath`, the PATH entry removal at uninstall, and the exit-code check
  around `ate setup` — `[Run]` entries ignore exit codes, so setup is run with `Exec`;
* signing: only compiled in when `build_installer.py` defines `SignEnabled`.

`build_installer.py` is exercised on Linux with `--dry-run`, which prints the exact `iscc`
command without running it, and with a stub bundle so the version lookup and the
Windows-safe `VersionInfoVersion` mapping are real. The password from `ATE_SIGN_PASSWORD`
is never printed.

Everything here runs in well under a second; nothing needs Windows.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ISS = REPO / "packaging" / "windows" / "ate.iss"
BUILD = REPO / "packaging" / "windows" / "build_installer.py"

# Directives a per-machine, upgradeable, PATH-adding installer cannot do without.
REQUIRED_SETUP_DIRECTIVES = (
    "AppId",
    "AppName",
    "AppVersion",
    "DefaultDirName",
    "PrivilegesRequired",
    "ArchitecturesAllowed",
    "ArchitecturesInstallIn64BitMode",
    "ChangesEnvironment",
    "UninstallDisplayIcon",
    "OutputBaseFilename",
    "VersionInfoVersion",
)

GUID = re.compile(r"^\{\{[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\}$")


def _iss_text() -> str:
    assert ISS.is_file(), "packaging/windows/ate.iss is missing"
    return ISS.read_text(encoding="utf-8")


def _sections(text: str) -> dict[str, list[str]]:
    """The .iss as INI-like sections: section name -> non-empty, non-comment lines."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        match = re.match(r"^\[([A-Za-z]+)\]$", line)
        if match:
            current = match.group(1)
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def _setup_directives(text: str) -> dict[str, str]:
    """`[Setup]` directive name -> value, names lower-cased for lookup."""
    out: dict[str, str] = {}
    for line in _sections(text).get("Setup", []):
        if line.startswith("#"):
            continue
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        out[name.strip().lower()] = value.strip()
    return out


def _build_module():
    """Load packaging/windows/build_installer.py without importing it as a package."""
    spec = importlib.util.spec_from_file_location("build_installer", BUILD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_bundle(directory: Path, version: str) -> Path:
    """A fake PyInstaller bundle whose `ate` executable answers `--version`."""
    bundle = directory / "dist" / "ate"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "_internal").mkdir(exist_ok=True)
    (bundle / "_internal" / "stuff.dat").write_text("stub", encoding="utf-8")
    exe = bundle / ("ate.exe" if os.name == "nt" else "ate")
    exe.write_text(f"#!{sys.executable}\nprint({version!r})\n", encoding="utf-8")
    exe.chmod(0o755)
    return bundle


def _dry_run(bundle: Path, env: dict[str, str] | None = None) -> str:
    """Run the wrapper with --dry-run and return everything it printed."""
    proc = subprocess.run(
        [sys.executable, str(BUILD), "--dry-run", "--dist", str(bundle.parent), "--out", str(bundle.parent / "release")],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, f"--dry-run failed: {proc.stdout}\n{proc.stderr}"
    return proc.stdout + proc.stderr


def test_iss_has_required_setup_directives() -> None:
    directives = _setup_directives(_iss_text())
    missing = [name for name in REQUIRED_SETUP_DIRECTIVES if name.lower() not in directives]
    assert not missing, f"[Setup] is missing directives: {', '.join(missing)}"

    assert directives["privilegesrequired"] == "admin", "an installer that writes to Program Files and the machine PATH must be admin"
    assert "{autopf}" in directives["defaultdirname"], "the install must be per-machine under Program Files"
    assert directives["architecturesallowed"] == "x64compatible"
    assert directives["architecturesinstallin64bitmode"] == "x64compatible"
    assert directives["changesenvironment"] == "yes", "Explorer needs to be told the PATH changed"
    assert "ate.exe" in directives["uninstalldisplayicon"]
    assert directives["outputbasefilename"] == "ate-{#AppVersion}-windows-x64-setup"


def test_app_id_is_a_fixed_guid() -> None:
    """Inno matches an upgrade to an existing install by AppId: a GUID, not a name."""
    app_id = _setup_directives(_iss_text()).get("appid", "")
    assert GUID.match(app_id), f"AppId is not a GUID: {app_id!r}"
    text = _iss_text()
    assert "never change" in text.lower(), "the script must warn that AppId is load-bearing"


def test_files_section_copies_the_whole_bundle_recursively() -> None:
    entries = _sections(_iss_text()).get("Files", [])
    bundle_entry = [e for e in entries if "{#SourceDir}\\*" in e]
    assert bundle_entry, "[Files] has no recursive entry for the PyInstaller bundle"
    entry = bundle_entry[0].lower()
    assert "recursesubdirs" in entry, "the bundle's _internal directory needs recursesubdirs"
    assert "ignoreversion" in entry, "an upgrade must overwrite files it already installed"
    assert "_internal" not in entry, "_internal must not be excluded from the install"
    assert "external" not in entry, "the bundle is inside the installer, not downloaded at install time"


def test_upgrade_clears_internal_before_installing() -> None:
    """An in-place upgrade must not leave files the new bundle no longer ships."""
    entries = _sections(_iss_text()).get("InstallDelete", [])
    assert any("_internal" in e.lower() and "filesandordirs" in e.lower() for e in entries), \
        "[InstallDelete] must clear {app}\\_internal before the new bundle is copied"


def test_path_registry_entry_is_guarded_and_never_deleted() -> None:
    entries = _sections(_iss_text()).get("Registry", [])
    path_entries = [e for e in entries if "Session Manager\\Environment" in e]
    assert len(path_entries) == 1, "exactly one entry may touch the machine PATH"
    entry = path_entries[0]
    lowered = entry.lower()
    assert "needsaddpath" in lowered, "the PATH entry must not be added twice on an upgrade"
    assert "expandsz" in lowered, "PATH is a REG_EXPAND_SZ value; writing it as REG_SZ breaks %VAR% entries"
    assert "{app}" in entry
    assert not any(flag in e.lower() for e in entries for flag in ("uninsdeletevalue", "uninsdeletekey")), \
        "uninsdelete* on the PATH value would delete the machine's whole PATH at uninstall"


def test_uninstall_touches_no_station_data() -> None:
    """Config and data live in %PROGRAMDATA%\\ATE; uninstall removes the program only."""
    sections = _sections(_iss_text())
    for name in ("UninstallDelete", "Dirs"):
        for entry in sections.get(name, []):
            lowered = entry.lower()
            for token in ("programdata", "commonappdata", "ate"):
                assert token not in lowered, f"[{name}] entry touches Station data: {entry}"


def test_code_defines_needs_add_path() -> None:
    code = "\n".join(_sections(_iss_text()).get("Code", []))
    assert re.search(r"function\s+NeedsAddPath\s*\(\s*Param\s*:\s*String\s*\)\s*:\s*Boolean", code), \
        "NeedsAddPath must be defined in [Code] for the [Registry] Check to work"
    assert "regquerystringvalue" in code.lower(), "NeedsAddPath has to read the existing PATH"
    # Case-insensitive, and matched against ';...;' so only whole entries count.
    assert re.search(r"Pos\(Lowercase\(';'\s*\+\s*Param\s*\+\s*';'", code), \
        "NeedsAddPath must compare whole ';' entries, case-insensitively"


def test_code_removes_only_its_own_path_entry_on_uninstall() -> None:
    code = "\n".join(_sections(_iss_text()).get("Code", []))
    lowered = code.lower()
    assert "usPostUninstall".lower() in lowered, "the PATH cleanup belongs in the post-uninstall step"
    assert "curuninstallstepchanged" in lowered, "the uninstaller needs CurUninstallStepChanged"
    assert "regwritestringvalue" in lowered, "the cleaned PATH has to be written back"
    assert "expandconstant('{app}')" in lowered, "the entry removed must be the one this installer added"
    assert "uninsdelete" not in lowered, "deleting the value is never acceptable"
    # Everything else in PATH is somebody else's.
    assert re.search(r"while\s+Remaining", code), "the PATH is rebuilt from its remaining entries"


def test_code_runs_ate_setup_and_checks_the_exit_code() -> None:
    code = "\n".join(_sections(_iss_text()).get("Code", []))
    lowered = code.lower()
    assert "ssPostInstall" in code, "setup must run after the files are in place"
    assert "curstepchanged" in lowered
    assert "exec(" in lowered and "ewWaitUntilTerminated" in code, "Exec + wait is how the exit code is seen"
    assert "resultcode" in lowered and re.search(r"ResultCode\s*<>", code), "the exit code must be tested"
    assert "msgbox" in lowered, "a failed setup has to be reported, not swallowed"
    assert "ate.exe" in lowered and "setup" in lowered
    # [Run] ignores exit codes, so setup must not be a [Run] entry.
    assert not _sections(_iss_text()).get("Run"), "setup in [Run] would ignore its exit code"
    # The program stays installed: we report, we do not abort.
    assert "raiseexception" not in lowered and "abort" not in lowered


def test_signing_is_guarded_by_a_define() -> None:
    text = _iss_text()
    assert "#ifdef SignEnabled" in text, "signing must be compiled in only when the wrapper defines SignEnabled"
    assert "#endif" in text
    block = text.split("#ifdef SignEnabled", 1)[1].split("#endif", 1)[0]
    assert "SignTool" in block, "the SignTool directive belongs inside the guard"
    assert "sign" in block.lower()
    # With no SignTool set the script must still compile: nothing outside the guard references signing.
    outside = text.replace(block, "")
    assert "SignTool=" not in outside


def test_dry_run_prints_the_command_without_signing(tmp_path: Path) -> None:
    bundle = _stub_bundle(tmp_path, "0.0.0-dev")
    output = _dry_run(bundle, env=dict(os.environ, ISCC="", PATH=tempfile.mkdtemp()))
    assert "iscc" in output.lower()
    assert "/DAppVersion=0.0.0-dev" in output
    assert "/DVersionInfoVersion=0.0.0.0" in output, "0.0.0-dev is not a Windows version; it must map to 0.0.0.0"
    assert "/DSourceDir=" in output and "_internal" not in output.split("/DSourceDir=", 1)[1].split()[0]
    assert "ate.iss" in output
    assert "SignEnabled" not in output and "signtool" not in output.lower()
    assert "--output-dir" in output


def test_dry_run_with_a_certificate_configures_signing(tmp_path: Path) -> None:
    bundle = _stub_bundle(tmp_path, "1.2.3-rc1")
    secret = "super-secret-signing-password"
    env = dict(os.environ, ISCC="", PATH=tempfile.mkdtemp(),
               ATE_SIGN_CERT="C:\\certs\\ate.pfx", ATE_SIGN_PASSWORD=secret)
    output = _dry_run(bundle, env=env)
    assert "/DSignEnabled" in output, "a configured certificate must turn signing on"
    assert "signtool" in output.lower()
    assert "/DVersionInfoVersion=1.2.3.0" in output
    assert secret not in output, "the signing password must never be echoed"
    assert "ATE_SIGN_PASSWORD" in output, "the redacted command should say where the password came from"


def test_windows_version_mapping() -> None:
    module = _build_module()
    cases = {
        "0.0.0-dev": "0.0.0.0",
        "1.2.3": "1.2.3.0",
        "1.2.3-rc1": "1.2.3.0",
        "1.2.3.dev4+gabc123de": "1.2.3.0",
        "": "0.0.0.0",
        "1.2": "1.2.0.0",
        "1.2.3.4.5": "1.2.3.4",
    }
    for version, expected in cases.items():
        assert module._windows_version(version) == expected, f"{version!r} should map to {expected}"


def test_version_is_read_from_the_built_bundle(tmp_path: Path) -> None:
    module = _build_module()
    bundle = _stub_bundle(tmp_path, "1.2.3")
    exe = bundle / ("ate.exe" if os.name == "nt" else "ate")
    assert module._bundle_version(exe) == "1.2.3"
    with pytest.raises(SystemExit):
        module._bundle_version(bundle / "nonexistent")


def test_missing_iscc_without_dry_run_fails_clearly(tmp_path: Path) -> None:
    bundle = _stub_bundle(tmp_path, "0.0.0-dev")
    proc = subprocess.run(
        [sys.executable, str(BUILD), "--dist", str(bundle.parent)],
        capture_output=True,
        text=True,
        env=dict(os.environ, ISCC="", PATH=tempfile.mkdtemp()),
    )
    assert proc.returncode != 0, "a missing iscc must not look like a successful build"
    combined = (proc.stdout + proc.stderr).lower()
    assert "iscc" in combined or "windows" in combined
    assert "--dry-run" in combined, "the error should point at --dry-run for machines without Inno Setup"
