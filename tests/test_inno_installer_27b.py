"""Packet 27b: the Inno Setup installer.

There is no `iscc` (and no Wine) on this machine, so the `.iss` is **never compiled or run**
by these tests — they read it the way a compiler would read its sections and check the
things that decide whether an install is per-machine, upgradeable, and reversible:

* the `[Setup]` directives: fixed `AppId` (in-place upgrades key off it), admin,
  `{autopf}`, 64-bit mode, `ChangesEnvironment`, `CloseApplications`, the artifact name;
* `[Files]`: the whole bundle, recursively, `ignoreversion`, nothing dropped;
* `[InstallDelete]`: `_internal` is cleared before an upgrade copies the new bundle;
* `[Registry]`: the machine PATH entry is guarded by
  `NeedsAddPath(ExpandConstant('{app}'))` and carries no `uninsdelete*` flag, which would
  wipe the whole PATH at uninstall;
* `[Code]`: `NeedsAddPath`, the PATH entry removal at uninstall, the exit code handed back
  by `GetCustomSetupExitCode`, and the `ate setup` run — `[Run]` entries ignore exit codes,
  so setup is run with `Exec`;
* signing: only compiled in when `build_installer.py` defines `SignEnabled`.

All section checks read the script with comments removed, so commented-out code cannot
satisfy them.

The PATH handling in `[Code]` is Pascal, and Pascal cannot be executed here. `NeedsAddPath`
and `PathWithout` are mirrored in Python below and tested against real PATH shapes. The
mirror **does not run the installer's Pascal**: on its own it proves nothing about
`ate.iss`. What makes it worth having is `test_the_pascal_the_mirror_copies_is_unchanged`,
which pins the exact lines the mirror was copied from — flip `<>` in the `.iss` and that
test fails, while the mirror happily keeps passing. The actual proof that PATH handling
works is 27c's Windows run.

`build_installer.py` is exercised on Linux with `--dry-run`, which prints the exact `iscc`
command without running it, and with a stub bundle so the version lookup and the
Windows-safe `VersionInfoVersion` mapping are real. The password from `ATE_SIGN_PASSWORD`
is never printed, not even when `iscc` fails.

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


def _closing_brace(text: str, start: int) -> int:
    """Index of the '}' that closes the '{' at `start`, ignoring braces inside quoted text."""
    i = start + 1
    depth = 1
    while i < len(text):
        char = text[i]
        if char in ("'", '"'):
            end = text.find(char, i + 1)
            if end == -1:
                return -1
            i = end + 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1

# Inno sections that hold Pascal, where a '{' starts a comment. Everywhere else a '{'
# opens a constant such as {app}.
CODE_SECTIONS = {"code", "run", "init"}

APP_DIR = "C:\\Program Files\\ATE"

# Every line below is copied out of [Code] verbatim. The Python mirror in this file only
# mirrors those lines: if the Pascal changes, the mirror is a lie and this test fails.
PASCAL_LINES_MIRRORED = (
    "while Remaining <> '' do",
    "if not SamePathEntry(Entry, Path) then",
    "if SamePathEntry(Entry, Param) then",
    "Result := TrimTrailingBackslash(Lowercase(Entry)) = TrimTrailingBackslash(Lowercase(Path));",
    "while (Length(Value) > 0) and (Copy(Value, Length(Value), 1) = '\\') do",
    "Cleaned := PathWithout(Existing, AppDir);",
    "if Cleaned = Existing then",
    "Result := Kept;",
    "Result := False;",
    "Result := True;",
)


def _iss_text() -> str:
    assert ISS.is_file(), "packaging/windows/ate.iss is missing"
    return ISS.read_text(encoding="utf-8")


def _strip_pascal_comments(text: str) -> str:
    """Drop Pascal '{ ... }' block comments from script code, leaving quoted text alone.

    In Inno's script language a quoted constant such as `'{app}'` is data, so only a '{'
    that is not inside quotes opens a comment.
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char in ("'", '"'):
            end = text.find(char, i + 1)
            if end == -1:
                out.append(text[i:])
                break
            out.append(text[i:end + 1])
            i = end + 1
            continue
        if char == "{":
            end = _closing_brace(text, i)
            if end == -1:
                out.append(char)
                i += 1
            else:
                i = end + 1
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _sections(text: str) -> dict[str, list[str]]:
    """The .iss as INI-like sections: section name -> lines, comments removed.

    A comment in a section is a line whose first character is ';' (the ';' inside
    `Root: HKLM; Subkey: "..."` separates parameters, it is not a comment). In the Pascal
    sections, '{ ... }' block comments are dropped too, so commented-out code cannot
    satisfy the checks below.
    """
    raw: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        match = re.match(r"^\s*\[([A-Za-z]+)\]\s*$", line)
        if match:
            current = match.group(1)
            raw.setdefault(current, [])
            continue
        if current is not None:
            raw[current].append(line)

    sections: dict[str, list[str]] = {}
    for name, lines in raw.items():
        body = "\n".join(lines)
        if name.lower() in CODE_SECTIONS:
            body = _strip_pascal_comments(body)
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith(";"):
                continue
            sections.setdefault(name, []).append(line)
    return sections


def _code_lines() -> list[str]:
    return _sections(_iss_text()).get("Code", [])


def _code_text() -> str:
    return "\n".join(_code_lines())


def _clean_text() -> str:
    """The whole script with every comment removed: what the compiler actually sees."""
    return "\n".join(line for lines in _sections(_iss_text()).values() for line in lines)


def _registry_lines() -> list[str]:
    return _sections(_iss_text()).get("Registry", [])


def _pascal_lines() -> list[str]:
    """The [Code] section with comments stripped, as stripped lines."""
    return _code_lines()


def _needs_add_path(existing: str | None, param: str) -> bool:
    """Python mirror of `NeedsAddPath` — see the module docstring: this is a copy, not the Pascal.

    `existing` is None when the registry has no Path value at all.
    """
    if existing is None:
        return True
    return not any(_same_path_entry(entry, param) for entry in _path_entries(existing))


def _path_without(original: str, path: str) -> str:
    """Python mirror of `PathWithout`."""
    kept = ""
    for entry in _path_entries(original):
        if not _same_path_entry(entry, path):
            if kept != "":
                kept = kept + ";"
            kept = kept + entry
    return kept


def _path_entries(original: str) -> list[str]:
    """Split on ';' the way the Pascal loop does: empty trailing pieces are not entries."""
    entries: list[str] = []
    remaining = original
    while remaining != "":
        cut = remaining.find(";")
        if cut == -1:
            entries.append(remaining)
            remaining = ""
        else:
            entries.append(remaining[:cut])
            remaining = remaining[cut + 1:]
    return entries


def _trim_trailing_backslash(value: str) -> str:
    while value.endswith("\\"):
        value = value[:-1]
    return value


def _same_path_entry(entry: str, path: str) -> bool:
    return _trim_trailing_backslash(entry.lower()) == _trim_trailing_backslash(path.lower())


def _remove_path_from(existing: str, app_dir: str) -> str | None:
    """Mirror of `RemoveOwnPathFromMachinePath`: what gets written back, or None if untouched."""
    cleaned = _path_without(existing, app_dir)
    if cleaned == existing:
        return None
    return cleaned


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


def _dry_run(bundle: Path, env: dict[str, str] | None = None, cwd: str | None = None, extra: list[str] | None = None) -> str:
    """Run the wrapper with --dry-run and return everything it printed."""
    if extra is None:
        extra = ["--dist", str(bundle.parent), "--out", str(bundle.parent / "release")]
    proc = subprocess.run(
        [sys.executable, str(BUILD), "--dry-run", *extra],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
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
    entries = _registry_lines()
    path_entries = [e for e in entries if "Session Manager\\Environment" in e]
    assert len(path_entries) == 1, "exactly one entry may touch the machine PATH"
    entry = path_entries[0]
    lowered = entry.lower()
    assert "needsaddpath" in lowered, "the PATH entry must not be added twice on an upgrade"
    assert "expandsz" in lowered, "PATH is a REG_EXPAND_SZ value; writing it as REG_SZ breaks %VAR% entries"
    assert "{app}" in entry
    assert not any(flag in e.lower() for e in entries for flag in ("uninsdeletevalue", "uninsdeletekey")), \
        "uninsdelete* on the PATH value would delete the machine's whole PATH at uninstall"


def test_the_path_check_expands_app_before_comparing() -> None:
    """Inno does not expand constants inside a Check parameter.

    `NeedsAddPath('{app}')` passes the six literal characters `{app}` to the function, so
    NeedsAddPath never finds it, and every upgrade appends another copy of the install
    directory to the machine PATH.
    """
    entry = [e for e in _registry_lines() if "Session Manager\\Environment" in e][0]
    assert re.search(r"Check:\s*NeedsAddPath\(ExpandConstant\('\{app\}'\)\)", entry), \
        f"the Check must expand the constant, not pass it as a literal: {entry}"
    assert re.search(r"NeedsAddPath\('\{app\}'\)", entry) is None, \
        f"the unexpanded literal is the bug this guards against: {entry}"


def test_a_running_ate_is_closed_before_an_upgrade() -> None:
    """Upgrading while ATE is running would fail on the files the bundle has open."""
    directives = _setup_directives(_iss_text())
    closes = directives.get("closeapplications", "") == "yes"
    mutex = "appmutex" in directives
    assert closes or mutex, "the installer must handle a running ATE: CloseApplications=yes or an AppMutex"
    if closes:
        assert directives.get("restartapplications", "") == "no", "Setup should not relaunch ATE behind the user's back"


def test_uninstall_touches_no_station_data() -> None:
    """Config and data live in %PROGRAMDATA%\\ATE; uninstall removes the program only.

    There is no [UninstallDelete] or [Dirs] section at all, which is the point: a loop over
    an absent section would pass vacuously. If one is ever added, no entry in it may name
    the Station's data directory.
    """
    sections = _sections(_iss_text())
    delete_sections = [name for name in sections if "delete" in name.lower()]
    assert delete_sections == ["InstallDelete"], \
        f"the only section that may delete anything is InstallDelete; found {delete_sections}"
    for name in delete_sections:
        for entry in sections[name]:
            lowered = entry.lower()
            for token in ("programdata", "commonappdata", "users"):
                assert token not in lowered, f"[{name}] entry deletes Station data: {entry}"
            assert "{app}" in entry, f"[{name}] must only delete inside the install directory: {entry}"
    clean = _clean_text().lower()
    assert "uninsdelete" not in clean, "deleting a registry value or key is never acceptable"


def test_code_defines_needs_add_path() -> None:
    code = _code_text()
    assert re.search(r"function NeedsAddPath\(Param: String\): Boolean", code), \
        "NeedsAddPath must be defined in [Code] for the [Registry] Check to work"
    assert "RegQueryStringValue(HKEY_LOCAL_MACHINE, EnvironmentKey, 'Path', Existing)" in code, \
        "NeedsAddPath has to read the existing PATH"
    assert "SamePathEntry(Entry, Param)" in code, \
        "NeedsAddPath must compare whole ';' entries, case-insensitively"


def test_the_pascal_the_mirror_copies_is_unchanged() -> None:
    """Pin the exact Pascal lines the Python mirror mirrors.

    Without this, the mirror tests would only ever test the mirror: inverting the comparison
    in `PathWithout` leaves every mirror test green.
    """
    lines = _pascal_lines()
    for pinned in PASCAL_LINES_MIRRORED:
        assert pinned in lines, f"[Code] changed a line the mirror depends on: {pinned!r}"
    # Both PATH functions walk entries with the same loop shape the mirror copies.
    assert lines.count("while Remaining <> '' do") == 2, "NeedsAddPath and PathWithout both walk entries"


def test_needs_add_path_mirror_cases() -> None:
    """Whole entries only, case-insensitive, and a trailing backslash is the same directory."""
    cases = [
        (None, True, "no Path value at all"),
        ("", True, "an empty PATH still needs the entry"),
        ("C:\\Windows;C:\\Program Files\\ATE", False, "already there"),
        ("C:\\Windows;c:\\program files\\ate", False, "the same entry in a different case"),
        ("C:\\Program Files\\ATE\\;C:\\Windows", False, "the entry with a trailing backslash"),
        ("C:\\Windows;C:\\Program Files\\ATE\\", False, "trailing backslash at the end of PATH"),
        ("C:\\Windows", True, "not present"),
        ("C:\\Program Files\\ATE2", True, "a prefix is not the same entry"),
    ]
    for existing, expected, why in cases:
        assert _needs_add_path(existing, APP_DIR) is expected, f"{why}: {existing!r}"


def test_path_without_mirror_cases() -> None:
    """The uninstaller's PATH rewrite, mirrored."""
    cases = [
        ("C:\\A;C:\\ATE;C:\\B", "C:\\ATE", "C:\\A;C:\\B", "middle entry"),
        ("C:\\ATE", "C:\\ATE", "", "PATH is only our entry"),
        ("C:\\ATE;", "C:\\ATE", "", "trailing separator"),
        ("C:\\ATE\\;C:\\B", "C:\\ATE", "C:\\B", "entry with a trailing backslash"),
        ("C:\\A;C:\\B", "C:\\ATE", "C:\\A;C:\\B", "absent: nothing removed"),
        ("C:\\A;c:\\ate;C:\\B", "C:\\ATE", "C:\\A;C:\\B", "differently cased entry"),
        ("C:\\Windows;C:\\Program Files\\ATE;C:\\Windows\\System32", APP_DIR,
         "C:\\Windows;C:\\Windows\\System32", "everybody else's entries survive"),
    ]
    for original, path, expected, why in cases:
        assert _path_without(original, path) == expected, f"{why}: {original!r}"


def test_only_the_own_entry_is_written_back() -> None:
    """Mirror of RemoveOwnPathFromMachinePath: if it was not ours, the PATH is left alone."""
    assert _remove_path_from("C:\\A;C:\\B", APP_DIR) is None, "an entry this installer never added is not touched"
    assert _remove_path_from("C:\\A;" + APP_DIR, APP_DIR) == "C:\\A"


def test_the_uninstall_write_keeps_the_expanded_string_type() -> None:
    """PATH is REG_EXPAND_SZ; write it back with the expand writer, not the plain one."""
    code = _code_text()
    assert re.search(r"RegWriteExpandStringValue\(HKEY_LOCAL_MACHINE, EnvironmentKey, 'Path', Cleaned\)", code), \
        "the cleaned PATH must be written as REG_EXPAND_SZ explicitly"
    assert re.search(r"RegWriteStringValue\(", code) is None, \
        "RegWriteStringValue writes a plain REG_SZ; reviewers disagree on whether it preserves the type"
    assert "%USERPROFILE%" in _iss_text(), "the comment must say why the type matters"


def test_uninstall_cleanup_is_defined_and_called() -> None:
    code = _code_text()
    assert "procedure RemoveOwnPathFromMachinePath;" in code, "the cleanup procedure must exist"
    assert re.search(r"if CurUninstallStep = usPostUninstall then\s*\n\s*RemoveOwnPathFromMachinePath;", code), \
        "the cleanup must actually run at the post-uninstall step"
    assert "ExpandConstant('{app}')" in code, "the entry removed must be the one this installer added"
    assert re.search(r"while Remaining <> '' do", code), "the PATH is rebuilt from its remaining entries"


def test_code_runs_ate_setup_and_checks_the_exit_code() -> None:
    code = _code_text()
    lowered = code.lower()
    assert "ssPostInstall" in code, "setup must run after the files are in place"
    assert "curstepchanged" in lowered
    assert "exec(" in lowered and "ewWaitUntilTerminated" in code, "Exec + wait is how the exit code is seen"
    assert "resultcode" in lowered and re.search(r"ResultCode <> 0", code), "the exit code must be tested"
    assert "ate.exe" in lowered and "setup" in lowered
    # [Run] ignores exit codes, so setup must not be a [Run] entry.
    assert not _sections(_iss_text()).get("Run"), "setup in [Run] would ignore its exit code"
    # The program stays installed: we report, we do not abort.
    assert "raiseexception" not in lowered and "abort" not in lowered


def test_a_failed_ate_setups_changes_the_installer_exit_code() -> None:
    """Under /VERYSILENT there is no message box, so the exit code is the only signal.

    GetCustomSetupExitCode is Inno's event function for exactly this: it is called when
    Setup would otherwise exit 0.
    """
    code = _code_text()
    assert re.search(r"function GetCustomSetupExitCode: Integer;", code), \
        "a silent install cannot report a failed `ate setup` without GetCustomSetupExitCode"
    assert re.search(r"Result := SetupExitCode;", code), "the failure flag must be returned"
    assert code.count("SetupExitCode := 1;") == 2, \
        "both `ate setup` failure paths (could not start, non-zero exit) must set it"
    assert "Log('" in code, "the failure is still logged"
    assert re.search(r"SuppressibleMsgBox\(", code), "and shown, when there is a wizard to show it in"
    assert not re.search(r"(?<!Suppressible)MsgBox\(", code), "a bare MsgBox blocks a silent install"


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


def test_comments_are_stripped_before_the_section_checks() -> None:
    """The parser all the checks above use drops comments, so commented-out code fails."""
    body = ("[Code]\n; if CurStep = ssPostInstall then RunStationSetup;\n"
            "{ Result := Kept }\n"
            "Exit;  { not a comment: this runs }\n"
            "procedure Alive; begin end;\n")
    code = "\n".join(_sections(body)["Code"])
    assert "ssPostInstall" not in code, "a commented-out ssPostInstall is not a real step check"
    assert "procedure Gone; begin end;" not in code, "a ';' line comment is not code"
    assert "Result := Kept" not in code, "a Pascal block comment is not code"
    assert "Exit;" in code and "procedure Alive; begin end;" in code, "real code survives"


def test_dry_run_prints_the_command_without_signing(tmp_path: Path) -> None:
    bundle = _stub_bundle(tmp_path, "0.0.0-dev")
    out = tmp_path / "release"
    output = _dry_run(bundle, env=dict(os.environ, ISCC="", PATH=tempfile.mkdtemp()),
                      extra=["--dist", str(bundle.parent), "--out", str(out)])
    assert "iscc" in output.lower()
    assert "/DAppVersion=0.0.0-dev" in output
    assert "/DVersionInfoVersion=0.0.0.0" in output, "0.0.0-dev is not a Windows version; it must map to 0.0.0.0"
    assert "/DSourceDir=" in output and "_internal" not in output.split("/DSourceDir=", 1)[1].split()[0]
    assert "ate.iss" in output
    assert "SignEnabled" not in output and "signtool" not in output.lower()
    assert f"/O{out}" in output, "ISCC's output-directory switch is /O<path>, not --output-dir=<path>"
    assert "--output-dir" not in output, "--output-dir is not the spelling the wrapper documents"
    assert "/O=" not in output, "/O takes the path directly: /Odist, not /O=dist"
    assert "/D" in output and "/S" not in output, "no sign tool is defined without a certificate"


def test_dry_run_paths_are_absolute(tmp_path: Path) -> None:
    """Inno resolves a relative SourceDir against the script's folder, not the cwd.

    `--dist dist` from some other directory must reach the absolute dist path, or the
    compiler would look for `packaging\\windows\\dist\\ate`.
    """
    bundle = _stub_bundle(tmp_path, "0.0.0-dev")
    output = _dry_run(bundle, env=dict(os.environ, ISCC="", PATH=tempfile.mkdtemp()),
                      cwd=str(tmp_path), extra=["--dist", "dist"])
    source = re.search(r"/DSourceDir=(\S+)", output).group(1)
    out_dir = re.search(r"/O(\S+)", output).group(1)
    assert source == str(Path(tmp_path, "dist", "ate").resolve()), f"SourceDir must be absolute: {source}"
    assert out_dir == str(Path(tmp_path, "dist").resolve()), f"the output path must be absolute: {out_dir}"
    assert not source.startswith("dist") and not out_dir.startswith("dist"), "relative paths reach the wrong folder"


def test_dry_run_with_a_certificate_configures_signing(tmp_path: Path) -> None:
    bundle = _stub_bundle(tmp_path, "1.2.3-rc1")
    secret = "super-secret-signing-password"
    env = dict(os.environ, ISCC="", PATH=tempfile.mkdtemp(),
               ATE_SIGN_CERT="C:\\certs\\ate.pfx", ATE_SIGN_PASSWORD=secret)
    output = _dry_run(bundle, env=env)
    assert "/DSignEnabled" in output, "a configured certificate must turn signing on"
    assert "/Ssigntool=" in output, "the Sign Tool the script names is defined on the command line"
    assert "signtool" in output.lower()
    assert "/DVersionInfoVersion=1.2.3.0" in output
    assert secret not in output, "the signing password must never be echoed"
    assert "ATE_SIGN_PASSWORD" in output, "the redacted command should say where the password came from"


def test_a_failing_iscc_reports_redacted_output(tmp_path: Path, monkeypatch, capsys) -> None:
    """iscc echoes the Sign Tool command it ran, password and all: nothing printed raw."""
    secret = "super-secret-signing-password"
    fake = tmp_path / "iscc.exe"
    fake.write_text(
        f"#!{sys.executable}\n"
        f"import sys\n"
        f"sys.stdout.write('signing: /p {secret}\\n')\n"
        f"sys.stderr.write('signtool failed for {secret}\\n')\n"
        f"sys.exit(2)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    bundle = _stub_bundle(tmp_path, "1.2.3")
    module = _build_module()
    original = dict(os.environ)
    try:
        os.environ.update(ATE_SIGN_CERT="C:\\certs\\ate.pfx", ATE_SIGN_PASSWORD=secret)
        monkeypatch.setattr(module, "_is_windows", lambda: True)  # pretend to be on Windows to reach iscc
        with pytest.raises(SystemExit) as exc:
            module.main(["--dist", str(bundle.parent), "--iscc", str(fake), "--version", "1.2.3"])
        printed = capsys.readouterr().out
        combined = printed + str(exc.value)
        assert "iscc failed" in combined.lower(), "a failed compile must be reported as a failure"
        assert secret not in combined, f"a failed iscc's output leaked the password: {combined}"
        assert "ATE_SIGN_PASSWORD" in combined, "the password is replaced by its source"
        assert "/O" in printed, "the echoed command still shows the real switches"
    finally:
        os.environ.clear()
        os.environ.update(original)


def test_half_a_certificate_is_an_error(tmp_path: Path) -> None:
    """Configuring only one of the two variables is a mistake, not "unsigned is fine"."""
    module = _build_module()
    original = dict(os.environ)
    try:
        os.environ.pop("ATE_SIGN_CERT", None)
        os.environ["ATE_SIGN_PASSWORD"] = "hunter2"
        with pytest.raises(SystemExit) as exc:
            module._signing()
        assert "ATE_SIGN_CERT" in str(exc.value)
        os.environ.pop("ATE_SIGN_PASSWORD", None)
        assert module._signing() is None, "no certificate at all means an unsigned build, not an error"
    finally:
        os.environ.clear()
        os.environ.update(original)


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
    combined = proc.stdout + proc.stderr
    assert "iscc" in combined.lower() or "windows" in combined.lower()
    assert "--dry-run" in combined, "the error should point at --dry-run for machines without Inno Setup"
    assert "Traceback" not in combined, "refusing to build is a clean exit, not a crash"


def test_iscc_is_found_from_the_environment(tmp_path: Path) -> None:
    """A build machine points at its compiler with ISCC; otherwise PATH, then the default location."""
    module = _build_module()
    fake = tmp_path / "ISCC.exe"
    fake.write_text("", encoding="utf-8")
    original = dict(os.environ)
    try:
        os.environ["ISCC"] = str(fake)
        assert module._find_iscc() == str(fake), "ISCC must be honoured"
        os.environ["PATH"] = ""
        os.environ.pop("ISCC", None)
        assert module._find_iscc() is None, "with no ISCC and no PATH there is no compiler to find"
    finally:
        os.environ.clear()
        os.environ.update(original)


def test_the_iss_header_shows_absolute_paths_and_both_signing_switches() -> None:
    """The hand-written example in the header has to be a command that actually works."""
    header = re.split(r"^\[Setup\]\s*$", _iss_text(), maxsplit=1, flags=re.M)[0].lower()
    assert "/dsignenabled" in header and "/ssigntool=" in header, "signing by hand needs both switches"
    assert "--output-dir" not in header, "ISCC's output switch is /O<path>"
    assert re.search(r"/o[a-z]:\\", header), "the example output path is absolute: /OC:\\work\\dist"
    assert re.search(r"/d[a-z]*dir=[a-z]:\\", header), "the example source dir is absolute too"
    assert "process" in header and "log" in header, "the header must say the password is on the command line"


def test_output_dir_in_the_script_is_relative_to_the_script_folder() -> None:
    """The wrapper passes /O, so the script's own OutputDir only matters in the Compiler IDE."""
    directives = _setup_directives(_iss_text())
    out = directives.get("outputdir", "")
    assert out.startswith("..") or Path(out).is_absolute(), f"OutputDir={out!r} resolves against the script folder"
    assert "dist" in out
