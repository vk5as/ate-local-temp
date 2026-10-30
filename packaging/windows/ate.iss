; -- ate.iss --
; ATE's Windows installer (packet 27b): the PyInstaller one-directory bundle from
; packaging/build_pyinstaller.py, installed per-machine under Program Files, with
; `ate` on the machine PATH, `ate setup` run at the end, and an uninstall that removes
; the program and nothing else.
;
; Compile it with packaging/windows/build_installer.py, which is also what release CI
; (27c) runs:
;
;   iscc /DAppVersion=1.2.3 /DVersionInfoVersion=1.2.3.0 /DSourceDir=dist\ate
;        --output-dir=dist packaging\windows\ate.iss
;
; and, when a code-signing certificate is configured, /DSignEnabled plus
; /Ssigntool=<command> (see the signing block below).
;
; THIS SCRIPT HAS NEVER BEEN COMPILED OR RUN. It was written on Linux machines that
; have neither Inno Setup nor Wine; the first real compile and install is 27c's
; Windows runner.

; Defaults so the script also compiles straight from the Compiler IDE.
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#ifndef VersionInfoVersion
  #define VersionInfoVersion "0.0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "dist/ate"
#endif

[Setup]
; Signing is opt-in: build_installer.py defines SignEnabled and hands over a matching
; signtool command (with /Ssigntool=...) only when ATE_SIGN_CERT and ATE_SIGN_PASSWORD
; are set. Without them the installer builds unsigned, which is the state of the world
; until a certificate exists (assets/packaging.md, "Code signing").
#ifdef SignEnabled
SignTool=signtool
SignedUninstaller=yes
#endif
; AppId is how Setup recognises an existing installation of ATE. Change it and the next
; release installs a second copy beside the first instead of upgrading in place, and the
; old uninstall entry is left behind. NEVER CHANGE THIS GUID.
AppId={{B4E4A4B2-7F1E-4A5C-9D3F-2A6E8C1B5D7F}
AppName=ATE
AppVersion={#AppVersion}
AppPublisher=VK5AS
DefaultDirName={autopf}\ATE
DefaultGroupName=ATE
UninstallDisplayIcon={app}\ate.exe
OutputDir=dist
OutputBaseFilename=ate-{#AppVersion}-windows-x64-setup
VersionInfoVersion={#VersionInfoVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

; The bundle is 64-bit: refuse 32-bit Windows, and install in 64-bit mode so that
; {autopf} is the real Program Files and nothing is redirected to WOW6432Node.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Per-machine, and it writes to the machine PATH: both need admin.
PrivilegesRequired=admin

; Tell Explorer the PATH changed, so `ate` is on the PATH without logging out.
ChangesEnvironment=yes

[InstallDelete]
; An upgrade copies the new bundle over the old one file by file, so files that the new
; build no longer ships would stay in _internal forever. Clearing it first is what makes
; an in-place upgrade leave nothing stale behind. Config and data are not here: they live
; in %PROGRAMDATA%\ATE, outside {app}, and survive every upgrade.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
#ifdef SignEnabled
; ate.exe on its own so it can be signed; the wildcard entry then skips it so the
; unsigned copy does not overwrite the signed one.
Source: "{#SourceDir}\ate.exe"; DestDir: "{app}"; Flags: ignoreversion sign
#else
Source: "{#SourceDir}\ate.exe"; DestDir: "{app}"; Flags: ignoreversion
#endif
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "ate.exe"

[Icons]
Name: "{group}\ATE"; Filename: "{app}\ate.exe"

[Registry]
; Put {app} on the machine PATH for every user. NeedsAddPath (in [Code]) stops an
; upgrade from adding the same directory twice.
;
; Deliberately no uninsdelete* flag: Inno does not restore a value it modified, and
; uninsdeletevalue here would delete the machine's entire PATH at uninstall time. The
; entry is taken back out in [Code] instead.
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Check: NeedsAddPath('{app}')

[Code]
; The machine PATH is one registry value. Setup appends {app} to it above; the
; uninstaller below removes exactly that one entry and leaves everything else alone.

function EnvironmentKey: String;
begin
  Result := 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';
end;

function NeedsAddPath(Param: String): Boolean;
var
  Existing: String;
begin
  if not RegQueryStringValue(HKEY_LOCAL_MACHINE, EnvironmentKey, 'Path', Existing) then
  begin
    Result := True;
    Exit;
  end;
  { Wrapped in ';' so only whole entries match, and compared lower-case: a path is not
    a name, so the same directory spelled differently must not be added a second time. }
  Result := Pos(Lowercase(';' + Param + ';'), Lowercase(';' + Existing + ';')) = 0;
end;

function PathWithout(Original, Path: String): String;
var
  Remaining, Entry, Kept: String;
  Cut: Integer;
begin
  Kept := '';
  Remaining := Original;
  while Remaining <> '' do
  begin
    Cut := Pos(';', Remaining);
    if Cut = 0 then
    begin
      Entry := Remaining;
      Remaining := '';
    end
    else
    begin
      Entry := Copy(Remaining, 1, Cut - 1);
      Remaining := Copy(Remaining, Cut + 1, Length(Remaining) - Cut);
    end;
    if Lowercase(Entry) <> Lowercase(Path) then
    begin
      if Kept <> '' then
        Kept := Kept + ';';
      Kept := Kept + Entry;
    end;
  end;
  Result := Kept;
end;

procedure RemoveOwnPathFromMachinePath;
var
  AppDir, Existing, Cleaned: String;
begin
  AppDir := ExpandConstant('{app}');
  if not RegQueryStringValue(HKEY_LOCAL_MACHINE, EnvironmentKey, 'Path', Existing) then
    Exit;
  Cleaned := PathWithout(Existing, AppDir);
  if Cleaned = Existing then
    Exit;  { this installer did not put it there; leave the PATH as it is }
  { RegWriteStringValue keeps a REG_EXPAND_SZ value's type, so %USERPROFILE%-style
    entries in PATH still expand. }
  if not RegWriteStringValue(HKEY_LOCAL_MACHINE, EnvironmentKey, 'Path', Cleaned) then
    Log('Could not remove ' + AppDir + ' from the machine PATH; delete it by hand.');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveOwnPathFromMachinePath;
end;

; [Run] entries ignore exit codes, so `ate setup` is run with Exec and its result is
; checked: an installer that says "installed" while the Station is not prepared is worse
; than one that says what is missing.
procedure RunStationSetup;
var
  ResultCode: Integer;
begin
  Log('Preparing the Station: ate.exe setup');
  if not Exec(ExpandConstant('{app}\ate.exe'), 'setup', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    SuppressibleMsgBox('ATE is installed, but "ate setup" could not be started (error ' + IntToStr(ResultCode) + '). ' +
      'Run "ate setup" from an elevated command prompt to create the config, the database and the data directories.',
      mbError, MB_OK, IDOK);
    Exit;
  end;
  if ResultCode <> 0 then
  begin
    Log('"ate setup" exited with code ' + IntToStr(ResultCode));
    SuppressibleMsgBox('ATE is installed, but "ate setup" exited with code ' + IntToStr(ResultCode) + '. ' +
      'Run "ate setup" from an elevated command prompt: it creates %PROGRAMDATA%\ATE, config.json and the database. ' +
      'The program itself is installed and on the PATH.', mbError, MB_OK, IDOK);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    RunStationSetup;
end;
