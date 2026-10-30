; -- ate.iss --
; ATE's Windows installer (packet 27b): the PyInstaller one-directory bundle from
; packaging/build_pyinstaller.py, installed per-machine under Program Files, with
; `ate` on the machine PATH, `ate setup` run at the end, and an uninstall that removes
; the program and nothing else.
;
; Compile it with packaging/windows/build_installer.py, which is also what release CI
; (27c) runs. Every path on the command line is absolute, because Inno resolves a
; relative SourceDir, Source or OutputDir against the folder holding THIS script, not
; the directory the compiler was started in:
;
;   iscc /DAppVersion=1.2.3 /DVersionInfoVersion=1.2.3.0 /DSourceDir=C:\work\dist\ate
;        /OC:\work\dist C:\work\packaging\windows\ate.iss
;
; Signing needs two switches, not one: /DSignEnabled turns the [Setup] guard on, and
; /Ssigntool=<command> defines the Sign Tool the guard names. The <command> carries the
; certificate password, so it is visible in any process listing; keep it out of logs.
;
; THIS SCRIPT HAS NEVER BEEN COMPILED OR RUN. It was written on Linux machines that
; have neither Inno Setup nor Wine; the first real compile and install is 27c's
; Windows runner.

; Defaults so the script also compiles straight from the Compiler IDE. They are relative
; to this script's own folder, which is where Inno looks for relative paths.
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#ifndef VersionInfoVersion
  #define VersionInfoVersion "0.0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\ate"
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
; build_installer.py always passes /O, which overrides this; it only matters when the
; script is compiled straight from the Compiler IDE, where it means the repo's dist\.
OutputDir=..\..\dist
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

; Upgrading while ATE is running would fail on the files the running bundle has open.
; CloseApplications=yes (the Inno default) is the smallest correct answer: Setup detects
; the processes holding the bundle's files and closes them. No AppMutex: that would need
; the app to create a named mutex at GUI launch, and file handles already tell Setup what
; to close. RestartApplications=no because the user, not the installer, starts ATE.
; Under /VERYSILENT the close happens without asking (the setup command-line switch is
; /CLOSEAPPLICATIONS).
CloseApplications=yes
RestartApplications=no

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
;
; ExpandConstant, not '{app}': a Check parameter is a Pascal call, and Inno does not
; expand constants inside it. NeedsAddPath('{app}') would look for the literal string
; {app} in PATH, never find it, and append another entry on every upgrade.
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Check: NeedsAddPath(ExpandConstant('{app}'))

[Code]
; The machine PATH is one registry value. Setup appends {app} to it above; the
; uninstaller below removes exactly that one entry and leaves everything else alone.
;
; PATH is compared entry by entry, never as a substring, and a trailing backslash on an
; entry means the same directory: {app} never ends in one, but somebody's PATH can.

var
  SetupExitCode: Integer;

function GetCustomSetupExitCode: Integer;
begin
  { Called only when Setup would otherwise exit 0. Without it a /VERYSILENT install that
    could not prepare the Station exits 0 and release CI (27c) sees a green run. }
  Result := SetupExitCode;
end;

function EnvironmentKey: String;
begin
  Result := 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';
end;

function TrimTrailingBackslash(Value: String): String;
begin
  while (Length(Value) > 0) and (Copy(Value, Length(Value), 1) = '\') do
    Value := Copy(Value, 1, Length(Value) - 1);
  Result := Value;
end;

function SamePathEntry(Entry, Path: String): Boolean;
begin
  { A path is not a name: 'C:\ATE', 'c:\ate\' and 'C:\ATE\' are one directory. }
  Result := TrimTrailingBackslash(Lowercase(Entry)) = TrimTrailingBackslash(Lowercase(Path));
end;

function NeedsAddPath(Param: String): Boolean;
var
  Existing, Remaining, Entry: String;
  Cut: Integer;
begin
  if not RegQueryStringValue(HKEY_LOCAL_MACHINE, EnvironmentKey, 'Path', Existing) then
  begin
    Result := True;
    Exit;
  end;
  Remaining := Existing;
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
    if SamePathEntry(Entry, Param) then
    begin
      Result := False;
      Exit;
    end;
  end;
  Result := True;
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
    if not SamePathEntry(Entry, Path) then
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
  { Write it back as REG_EXPAND_SZ on purpose. Whether RegWriteStringValue preserves the
    existing value's type is not something this script wants to bet on: PATH holds
    %USERPROFILE%-style entries, and a plain REG_SZ would stop them expanding. }
  if not RegWriteExpandStringValue(HKEY_LOCAL_MACHINE, EnvironmentKey, 'Path', Cleaned) then
    Log('Could not remove ' + AppDir + ' from the machine PATH; delete it by hand.');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveOwnPathFromMachinePath;
end;

; [Run] entries ignore exit codes, so `ate setup` is run with Exec and its result is
; checked: an installer that says "installed" while the Station is not prepared is worse
; than one that says what is missing. The failure is also recorded in SetupExitCode so a
; silent install exits non-zero, not 0.
procedure RunStationSetup;
var
  ResultCode: Integer;
begin
  Log('Preparing the Station: ate.exe setup');
  if not Exec(ExpandConstant('{app}\ate.exe'), 'setup', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    SetupExitCode := 1;
    SuppressibleMsgBox('ATE is installed, but "ate setup" could not be started (error ' + IntToStr(ResultCode) + '). ' +
      'Run "ate setup" from an elevated command prompt to create the config, the database and the data directories.',
      mbError, MB_OK, IDOK);
    Exit;
  end;
  if ResultCode <> 0 then
  begin
    SetupExitCode := 1;
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
