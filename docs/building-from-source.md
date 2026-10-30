# Building ATE from source

Packet 28a: the wheel and the source build, plus the Linux artifacts (28b) and the Windows
installer (27b). This page covers running ATE from a checkout, building the wheel, the
frozen PyInstaller build that every artifact starts from, the Windows installer, the Linux
packages, and the release CI that builds and smoke-tests them on a tag (27c).

ATE targets CPython **3.13** (Debian stable's system Python) and **3.14** (the
version the frozen builds bundle). Anything older is refused by
`requires-python = ">=3.13"`.

## From a checkout

```bash
git clone <this repository> && cd ate-ornith
python3.13 -m venv .venv            # or python3.14
source .venv/bin/activate
pip install -e ".[test]"            # pytest, pytest-qt, and the build tooling (setuptools, build)
pytest                              # needs no display: QT_QPA_PLATFORM=offscreen helps on CI
```

Run it:

```bash
ate setup --root /tmp/ate-station   # creates the config, database and data directories
ate --version                       # the version baked from the git tag: the install check
```

`ate --version` is how you verify the install. Bare `ate` opens the GUI: it reads the config,
refuses a missing or mismatched database, and opens the main window with the Run screen.
`ate --config <path>` points the launch at a specific config file; `ate --help` and
`ate <subcommand> --help` print usage without running anything.

`ate setup` is best run as admin or root: elevated, it creates the `ate` group and sets
the ownership and permissions of the station directories. Run unprivileged it warns and
skips those steps (or force the skip with `--no-permissions`); the directories themselves
are still created. See the spec's Packaging section.

### Where the Station's files live

The config's path defaults are written with the `<data>` placeholder (`<data>/ate.db`,
`<data>/logs`, …). `<data>` is the platform **data directory**: `%PROGRAMDATA%\ATE` on
Windows, `/var/lib/ate` on Linux (`assets/app-config-format.md`). The reader resolves it at
launch, so a config that keeps the placeholders stays portable between machines.

`ate setup --root DIR` writes the config with those paths **already resolved** against
`DIR/data` (its `definitions`, `reports` and `logs` subdirectories are created too), so a
rooted Station reads and writes inside `DIR` and never touches the platform data directory.
`ate setup` without `--root` uses the platform directories from the table above.

On Windows the venv paths are `.venv\Scripts\activate` instead.

## Running ATE as root or as an administrator (packet 30d)

Chromium's renderer sandbox does not start under root or an elevated Windows
administrator, so a Test Report PDF rendered there can only be printed with the
sandbox off. ATE never does that silently:

* an ordinary user is never asked and never loses the sandbox, with or without the flag;
* an elevated user is asked before **each** PDF — at run end and on History's
  *Regenerate* — with a dialog whose buttons are "Generate PDF without sandbox" and
  "Skip PDF", the latter being the default. Skipping writes no PDF: the HTML Test
  Report is already on disk, the skip is logged in the application log, and the run-end
  flow still completes;
* the start-up flag `--allow-unsandboxed-pdf` presets Yes for a privileged run with no
  dialog, and logs that it did:
  `ate --allow-unsandboxed-pdf _selftest --run`;
* with no flag and no way to ask (headless, `ate _selftest --run`, a dialog dismissed by
  `ATE_AUTO_DISMISS_DIALOG`) the answer is No and no PDF is written. `ate _selftest --run`
  then reports the PDF as skipped and fails, naming the flag.

The decision is passed to the hidden `ate _pdf` child as an argument (`--unsandboxed`) and
through the child's environment; nothing in `ate.report.pdf`, the run session or the frozen
entry point decides it from who started ATE. Run `ate _pdf` directly as root without the
argument and it refuses with a non-zero exit. Handed to an unprivileged process, the
argument is ignored with a warning: the sandbox is kept.

The release smoke scripts pass the flag, because their containers and runners are
privileged.


## Building the wheel

```bash
python -m build --wheel             # or: pip wheel --no-deps -w dist .
```

The wheel is `dist/ate-<version>-py3-none-any.whl`, where `<version>` comes from
`git describe --tags` (`0.0.0-dev` when there is no tag). It carries the report
template, the bundled Lua modules and every other non-`.py` file under `src/ate`;
`tests/test_wheel_build_28a.py` fails if a resource is ever left out.

Install it the way a Station operator would:

```bash
pipx install dist/ate-0.0.0.dev0-py3-none-any.whl
ate setup
ate --version
```

`pipx` puts `ate` on the PATH in its own virtual environment; `pip install` into
an existing venv works the same way. Dependencies come from PyPI, so no wheel
step is needed for them.

## Frozen build (PyInstaller)

Every frozen artifact — the Inno Setup installer, the `.tar.gz`, the `.deb` — starts from the
same one-directory PyInstaller build:

```bash
python packaging/build_pyinstaller.py --smoke     # builds dist/ate/ and runs the smoke checks
```

The recipe is CPython-version-agnostic: it builds with whichever interpreter runs the script.
Release CI builds the bundles with **CPython 3.14** (the version the artifacts are required to
bundle); building on a machine with only 3.13 proves the recipe and produces a working bundle.

What the script does:

1. creates a throwaway venv in `build/pyinstaller-venv` with the current interpreter
   (`--no-venv` builds with the one you are already in);
2. installs `.` and `pyinstaller` into it — the network is needed on a fresh venv;
3. runs `packaging/pyinstaller/ate.spec` with PyInstaller, one-directory, into
   `dist/ate/`: the `ate` executable next to its `_internal` directory;
4. with `--smoke`, runs the checks the release CI runs: `dist/ate/ate --version`,
   `dist/ate/ate _pdf report.html report.pdf` (QtWebEngine rendering, with
   `QT_QPA_PLATFORM=offscreen`), `dist/ate/ate _selftest --run`, and a check that the WebEngine
   helper and the template/Lua files are inside `_internal`.

The bundle carries CPython and every dependency, plus the things no import reaches: the
report template and the bundled `ate.` Lua modules, lupa's native Lua runtime, pyvisa-py and
its protocols (pyvisa picks a backend by name at runtime), and QtWebEngine's
`QtWebEngineProcess` helper with its `.pak`, ICU and V8 data. NI-VISA is never bundled;
pyvisa falls back to pyvisa-py for TCPIP and USBTMC. The Qt bindings ATE never import
(QML/Quick, Qt3D, QtMultimedia, QtBluetooth and the rest) are excluded from the bundle:
the shared libraries QtWebEngine is linked against are still collected, so the exclusion
only removes bindings and their data.

`ate _selftest` is a hidden subcommand (like `ate _pdf`) that prints one line per thing the
bundle must carry and exits 1 if any is missing. With `--run` it adds one end-to-end check: a
built-in one-step Test Schedule is run headless through `HeadlessRunSession` in a temporary
directory with its own config and database, and the check passes only if the run wrote its HTML
report and the `_pdf` child (the bundle's own executable when frozen) wrote the PDF. With
`--scan <dir>` (27c) it reads the Station config, refuses a database that is not at this ATE's
schema version, and lists the Test Schedules in a Test Definition directory, reporting broken
entries and checksum mismatches. Apart from that temporary directory it touches no Station,
database or Test Run.

`build/` and `dist/` are gitignored: build output is never committed, and the build is
reproducible from a clean checkout. The bundle is roughly 490 MB on disk (579 MB before the
Qt exclusions) — Qt's libraries and QtWebEngine are the bulk of it.

## Windows installer

The installer is `packaging/windows/ate.iss`, compiled by `packaging/windows/build_installer.py`.
It needs **Windows** and **Inno Setup 6.3 or newer** (`x64compatible` is the 6.3-era directive);
`iscc` is looked for in `$ISCC`, then on `PATH`, then in
`C:\Program Files\Inno Setup 6\ISCC.exe`.

```powershell
python packaging\windows\build_installer.py --dist dist --build   # builds dist\ate first, then the installer
python packaging\windows\build_installer.py --dist dist           # dist\ate already built
python packaging\windows\build_installer.py --dry-run             # print the iscc command, compile nothing
```

`--dry-run` works on any platform, which is how this script is exercised on Linux. `--dist`,
`--out` and `--iss` are resolved to absolute paths before they reach `iscc`: Inno resolves a
relative `SourceDir`, `Source` or `OutputDir` against the folder holding `ate.iss`
(`packaging\windows`), not the directory the compiler was started in, so handing it `dist`
would make it look in `packaging\windows\dist`.

The artifact is `dist\ate-<version>-windows-x64-setup.exe`. What it does:

- Per-machine install under `Program Files\ATE`; `PrivilegesRequired=admin`, and it refuses
  32-bit Windows because the bundle is 64-bit.
- Adds `{app}` to the **machine** PATH (`HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment`,
  written as `expandsz` so `%VAR%` entries survive) and tells Explorer the environment changed,
  so `ate` is on the PATH straight after the install. `NeedsAddPath` in the script compares
  whole `;`-separated entries case-insensitively, ignoring a trailing backslash, so an upgrade
  never adds the directory twice. It is called as `NeedsAddPath(ExpandConstant('{app}'))`:
  a `Check:` parameter is plain Pascal, and Inno does not expand constants inside it, so
  `NeedsAddPath('{app}')` would search for the literal text `{app}` and append another entry
  on every upgrade.
- Runs `ate setup` at the end. It is run with `Exec` and its exit code checked rather than as a
  `[Run]` entry, because `[Run]` entries ignore exit codes: if setup fails, the installer names
  the fix (`ate setup` from an elevated command prompt) and, through `GetCustomSetupExitCode`,
  exits non-zero. Under `/VERYSILENT` there is no message box to read, so the exit code and the
  `/LOG` file are what a build or CI run has to go on. The program itself stays installed.
- Upgrades install in place. `[InstallDelete]` clears `{app}\_internal` before the new bundle is
  copied, so files the new build no longer ships cannot linger. A running ATE is closed first
  (`CloseApplications=yes`, `RestartApplications=no`): Setup finds the processes holding the
  bundle's files and shuts them down — silently under `/VERYSILENT`, which is the
  `/CLOSEAPPLICATIONS` behaviour. No `AppMutex` is used; the app would have to create one at
  GUI launch, and the file handles already tell Setup what to close. The Station's config and data
  are in `%PROGRAMDATA%\ATE`, outside `{app}`, so they survive; the uninstaller removes the
  program and its PATH entry and nothing else, and there is deliberately no `uninsdeletevalue`
  on the PATH entry, which would wipe the machine's entire PATH.

The AppId GUID in the script is what makes an upgrade an upgrade. Changing it would install a
second ATE beside the first and leave the old uninstall entry behind.

### Signing

Optional, as the spec's packaging notes say. When **both** `ATE_SIGN_CERT` and
`ATE_SIGN_PASSWORD` are set, the wrapper compiles with `/DSignEnabled` (the switch that turns
the script's signing guard on) and `/Ssigntool=signtool.exe sign /fd SHA256 /f $q<cert>$q
/p $q<password>$q ... $f` (the switch that defines the Sign Tool the guard names): signing
needs both. With neither variable set the build is unsigned and nothing complains; setting
only one is an error rather than a silent unsigned build.

The password comes from the environment only, but it is passed to `iscc` on its command line,
so anyone who can list processes on the build machine can see it. Nothing here logs it:
`--dry-run` shows the command with it replaced by `<from ATE_SIGN_PASSWORD>`, and a failed
compile's stdout and stderr are redacted before they are printed. Do not pipe `iscc` output
into a CI log that is stored or displayed raw.

> **Never compiled, never run.** Every machine that has written this script is Linux, with no
> Inno Setup and no Wine. `--dry-run` and `tests/test_inno_installer_27b.py` (which reads the
> script's sections and runs the wrapper against a stub bundle) are the only checks so far.
> The first real compile, install, upgrade and uninstall is release CI's Windows runner (27c).

## Linux packages

Both Linux artifacts come out of that same `dist/ate` bundle:

```bash
python packaging/linux/build_linux.py --dist dist --build   # builds dist/ate first if needed
```

They land in `--out` (default the `--dist` directory):

| Artifact | Name |
| --- | --- |
| tarball | `ate-<version>-linux-x86_64.tar.gz` |
| Debian package | `ate_<version>_amd64.deb` |

### The tarball

Inside it are the bundle as `ate/` and `install.sh` next to it:

```bash
tar -xzf ate-0.0.0-dev-linux-x86_64.tar.gz
./install.sh                          # /opt/ate, /usr/bin/ate, then "ate setup"
./install.sh --prefix /opt/ate        # or PREFIX=...; --bin puts the link somewhere else
./install.sh --uninstall              # removes the program only
```

`install.sh` is idempotent: re-running it clears the bundle's own contents (`ate` and `_internal`)
in the prefix and writes the new copy, so nothing from the previous version survives — a plain
`cp -a` merge would leave the old `_internal` files behind. `--prefix` or `--bin` with no value is
a usage error. As root the link goes to `/usr/bin/ate`; unprivileged it goes to `<prefix>/bin/ate`
instead, and `ate setup` then cannot create the `ate` group or set the ownership of `/etc/ate` and
`/var/lib/ate`: the program is installed, and the Station is finished with `sudo ate setup`. If
setup fails, install.sh says so and exits 1 rather than pretending the install is complete.
Nothing under `/etc/ate` or `/var/lib/ate` is ever touched, so an upgrade keeps the config, the
database, the reports and the logs. `--uninstall` refuses `/`, an empty prefix, and any prefix
that has no `ate` executable in it; otherwise it removes the bundle and the link, and the prefix
only if it is empty afterwards — the same as `apt remove`.

### The .deb

Built with `dpkg-deb --root-owner-group --build` from a staging directory, so nothing on the
build machine's ownership leaks into the package:

- `/opt/ate/…` is the whole bundle, and `/usr/bin/ate` is a symlink to `/opt/ate/ate`.
- Upgrading a `.deb` needs no cleanup code: dpkg removes every file the package installed before
  `postinst` runs, so the new bundle replaces `/opt/ate` completely. Only what `ate setup`
  creates is outside dpkg's file list, which is why `postrm` decides what happens to it.
- `postinst` runs `/opt/ate/ate setup` on `configure` — the same command the installer and
  `install.sh` run (see the spec's Packaging section). `dpkg-reconfigure ate` does nothing:
  re-running setup is not a package operation.
- `postrm`: on `remove` it does nothing, so the Station's config and data survive; on `purge` it
  deletes `/etc/ate/config.json` and the config directory if it is empty, and **never** touches
  `/var/lib/ate` — the database, its WAL files, the reports and the logs are records.
- `/etc/ate/config.json` is **not** a conffile. `ate setup` creates it, so it is not in the
  package's file list and dpkg would leave it behind even on purge; `postrm` removes it instead.

The version in `Version` is ATE's version as dpkg will accept it. Every prerelease suffix becomes
`~` — `0.0.0-dev` → `0.0.0~dev`, `1.2.3-rc1` → `1.2.3~rc1`, `1.2.3-beta2` → `1.2.3~beta2` — because
`~` sorts before everything while a bare `-` sorts after the digits before it, so a prerelease
precedes the release it is a prerelease of (`dpkg --compare-versions 1.2.3~rc1 lt 1.2.3`). Anything
outside `[A-Za-z0-9.+~:-]` is rewritten as `-`, and a bundle whose `--version` prints nothing is
refused rather than packaged as `ate__amd64.deb`.

The bundle carries its own CPython, Qt, QtWebEngine, lupa, pyvisa-py, ICU, NSS, freetype, fontconfig
and libX11, so the package needs none of Debian's Python or Qt packages. What it cannot carry is the
system glibc, GL/EGL and a font. The list is not derived from a manifest: `packaging/linux/docker_smoke.py`
builds the bundle in a `debian:stable` container, installs the `.deb` there with `apt-get`, and runs
`ate _selftest --run`. With these five packages the run passes. Remove `libgl1` or `libegl1` and the
selftest dies with `libGL.so.1: cannot open shared object file`; remove the two font packages and the
report renders as a 1.7 KB PDF with no text in it, which `ate.report.pdf.pdf_has_text` catches.

| Depends | Why |
| --- | --- |
| `libc6` | libc, libm, libdl, libpthread, libresolv: the bundled CPython and Qt are linked against the system glibc |
| `libgl1`, `libegl1` | libGL/libGLX/libGLdispatch and libEGL: every Qt6 Gui-linked library wants them |
| `fontconfig`, `fonts-dejavu-core` | a stock Debian image ships no fonts at all, and QtWebEngine then writes a report PDF with no glyphs |

`libdrm2`, `libxcb1`, `libxcb-dri3-0` and `libwayland-client0` are not listed because apt installs them
as dependencies of `libgl1`/`libegl1` anyway. The Wayland and extra xcb platform plugins, GTK and CUPS
are linked against but never opened headless, so they are not depended on either.

### Testing seams

Only one flag is a seam in the shipped scripts, and it is a real installer flag:

| Variable / flag | What it does |
| --- | --- |
| `install.sh --prefix` / `--bin` | Aim the install at a temporary directory instead of `/opt/ate` and `/usr/bin/ate` |

`postinst` and `postrm` have **no** test seam: they name `/opt/ate`, `/etc/ate` and `/var/lib/ate`
directly. The test runs a copy of each script with those three paths rewritten to a temporary
root, so a variable exported on a Station can never change what the installed package does.

`packaging/linux/smoke_linux.py` (28c) never runs on a Station, so its seams are plain flags:
`--root` and `--home` root `/opt/ate`, `/usr/bin/ate`, `/etc/ate`, `/var/lib/ate` and pipx's
`~/.local/bin`, and every command the script runs goes through an injected `Runner`. The CI passes
no `--root`, so in the container the paths are the real ones.

`tests/test_linux_packages_28b.py` runs the scripts against a stub bundle in seconds: it checks
the tarball and the deb layout, that the maintainer scripts are executable and pass `sh -n`, that
`purge` never deletes the data directory, and that `install.sh` copies the bundle, links it and
runs `setup`.

The packaged-artifact test is marked `build` like the frozen-build test: with
`ATE_BUILD_TESTS=1` it builds the bundle, packages it both ways, extracts the `.deb` with
`dpkg-deb -x`, runs the extracted `/opt/ate/ate --version` and `_selftest`, and runs `ldd` over
every `.so` in the bundle so that each library that does not resolve inside it is named in
`Depends`.

## Release CI

`.github/workflows/release.yml` runs on a tag (`v*`) and on a manual run — never on an ordinary
push, because an artifact is only ever made for a release. The per-push test matrix is
`.github/workflows/ci.yml`.

```bash
git tag v1.2.3 && git push --tags     # that is what starts the release job; nothing else does
```

The `windows-installer` job, on `windows-latest`:

1. checks out with `fetch-depth: 0` — ATE's version is the tag (`src/ate/_version.py` runs
   `git describe --tags`), and a shallow clone would stamp `0.0.0-dev`;
2. sets up **CPython 3.14**, the version the frozen artifacts are required to bundle;
3. installs Inno Setup (pinned to 6.7.1; the runner's own copy is used if it is already there);
4. `python packaging/build_pyinstaller.py --smoke` — the bundle and 27a's smoke checks;
5. `python packaging/windows/build_installer.py` — signed only when `ATE_SIGN_CERT` and
   `ATE_SIGN_PASSWORD` are set in the repository; an unset secret arrives as an empty string and
   the build is unsigned;
6. `python packaging/windows/smoke_installer.py` — installs, upgrades and uninstalls;
7. uploads `dist/ate-*-windows-x64-setup.exe` to `actions/upload-artifact`. Publishing a GitHub
   Release is not part of this packet.

`smoke_installer.py` is the part that proves the installer is worth shipping. It asserts, at
install, upgrade and uninstall:

- the installer's exit code is 0 and its `/LOG=install.log` contains no failure line. Under
  `/VERYSILENT` those are the only signs that `ate setup` inside the installer failed
  (`GetCustomSetupExitCode` in `ate.iss` is what makes the exit code non-zero);
- the machine PATH has exactly one `{app}` entry after an install and after an upgrade, none after
  an uninstall, every other entry unchanged, and the value is still `REG_EXPAND_SZ` so `%VAR%`
  entries in it keep expanding;
- `ate --version` runs in a new process started with that refreshed PATH;
- the installer left a Station: `%PROGRAMDATA%\ATE\config.json` and the database are there,
  `ate db status` prints a schema version, and `ate _selftest --run` runs a Test Schedule;
- a headless launch scans the example Media Release: `ate _selftest --scan <dir>` (a hidden
  subcommand like `_pdf`) reads the config, refuses a database that is not at this ATE's schema
  version, and reports the schedules it found, the broken entries and the checksum mismatches;
- an upgrade keeps `%PROGRAMDATA%\ATE` (a marker file written before the upgrade is still there)
  and clears `{app}\_internal` (a stale file planted before the upgrade is gone);
- uninstalling removes the program and its PATH entry and nothing else — the uninstaller re-spawns
  itself, so the script polls until `{app}` is gone and fails loudly if it never is.

The PATH comparisons are plain functions over values (`packaging/windows/smoke_installer.py`), and
`tests/test_release_ci_27c.py` runs the whole sequence against a fake registry and a fake
installer on Linux. That fake models what `ate.iss` promises, so those tests prove the smoke script
catches a duplicate PATH entry, a PATH entry left behind, a `REG_SZ` write, a wiped data
directory, a stale `_internal`, and a non-zero installer exit — not that the installer does any of
those things correctly.

> **Never run.** No Windows machine has ever executed `release.yml`, `ate.iss` or
> `smoke_installer.py`: every machine that wrote them is Linux, and no tag has been cut. The
> workflow is checked by reading it (no `actionlint` here, and none was installed to check it).
> Its first run is the first tag.

The Linux artifacts (the `.tar.gz`, the `.deb` and the wheel, 28c) are built and smoke-tested in
the same workflow.

### The Linux jobs

`linux-build` runs on `ubuntu-24.04`, and `linux-smoke` runs three times after it — once for each
of `deb`, `tar` and `wheel` — each in its own `debian:stable` container (`fail-fast: false`, so a
broken `.deb` does not cancel the wheel's run):

1. `ldd --version | tee dist/glibc-build.txt` — the glibc the bundle will be linked against,
   recorded so the smoke run can compare it with the container's;
2. `python packaging/build_pyinstaller.py --smoke` — the bundle and 27a's smoke checks;
3. `python packaging/linux/build_linux.py --dist dist` — the tarball and the `.deb`;
4. `python -m build --wheel` — `dist/ate-<version>-py3-none-any.whl`;
5. one upload of `dist/ate-*-linux-x86_64.tar.gz`, `dist/ate_*_amd64.deb`, `dist/ate-*.whl` and
   `dist/glibc-build.txt` as `ate-linux-artifacts`, with `if-no-files-found: error`.

Each smoke job then runs `python3 packaging/linux/smoke_linux.py --kind deb|tar|wheel --dist
artifacts --expect-version "$tag"` in a container that has nothing installed, which is the point
(`--expect-version` is the tag: the bundle carries it in `ate/version.txt` and the wheel in its
metadata, so `ate --version` has to print it):

- **`.deb`** — `apt-get install -y ./ate_*.deb`. apt resolving the package's `Depends` on Debian
  stable is the check, and `postinst` has already run `ate setup`;
- **`.tar.gz`** — the runtime libraries are installed from the `DEPENDS` list in
  `packaging/linux/build_linux.py` (read from the script, never copied into the smoke script), then
  `tar -xzf` and `./install.sh`;
- **wheel** — `python3`, `python3-venv`, `pipx` and `WHEEL_SYSTEM_LIBS` from the same script: Debian
  stable's Python is 3.13, and the wheel pulls PyQt6 and QtWebEngine from PyPI, so it needs the
  libraries PyPI's Qt is linked against rather than the bundle's own. `pipx install ./ate-*.whl`,
  then `ate setup`, because a wheel has no postinst:

  ```bash
  apt-get install -y --no-install-recommends python3 python3-venv pipx \
    libgl1 libegl1 libglib2.0-0t64 libdbus-1-3 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libxtst6 libxkbcommon0 libxkbfile1 libasound2t64 libfontconfig1 fontconfig fonts-dejavu-core
  pipx install dist/ate-1.2.3-py3-none-any.whl
  ```

  Each of those was found by taking the list apart one package at a time in a fresh container: every
  removal makes `ate _selftest --run` fail with "cannot open shared object file".

Every kind has to print a version, leave `/etc/ate/config.json` and the database that config names,
answer `ate db status` with a schema version, run `ate _selftest --run` headless, and scan the
shipped example Media Release copied into the configured definitions directory with nothing broken
and nothing mismatched. The `.deb` run then checks that `apt remove` keeps the config and the data
and that `apt purge` removes the config while leaving the database, reports and logs; the tarball
run runs `install.sh` again (an upgrade: the data survives, `_internal` is cleared) and then
`--uninstall`, which removes the program and nothing else. The container runs as root, so
`ate setup` really creates the `ate` group and sets the ownership of `/etc/ate` and `/var/lib/ate`;
`QT_QPA_PLATFORM=offscreen` is set at job level for QtWebEngine.

**Why `ubuntu-24.04`, not `ubuntu-latest`.** A PyInstaller bundle is linked against the glibc of the
machine that built it, and glibc is only forward compatible: a binary built against 2.39 runs on
2.41, never the other way round. Ubuntu 24.04 ships glibc 2.39 and Debian stable ships a newer one,
so artifacts built there start in the smoke container; `ubuntu-latest` will move to a newer Ubuntu,
and a bundle linked against that glibc would fail to start in Debian. The build writes its own
version into `dist/glibc-build.txt` and every smoke run reads it back and refuses a container with
an older glibc — the baseline is checked, not trusted. Move the pin only with a Debian that can run
it.

`smoke_linux.py` is one script shared by the three runs; it stops at the first failure and names it.
Every command it runs goes through an injected `Runner`, and `Station(root=…)` roots `/opt/ate`,
`/usr/bin/ate`, `/etc/ate` and `/var/lib/ate` in a directory, so `tests/test_release_ci_28c.py`
drives the whole sequence against a fake Debian container on Linux: `apt-get` and `dpkg` are
modelled, `install.sh` really runs through its `--prefix`/`--bin` seam, and every `ate` call is the
real source CLI against that container's config and database. Those tests prove the script catches a
postinst that never ran setup, a database at the wrong schema, a Media Release with broken entries,
a purge that keeps the config or deletes the records, and an uninstall that loses data — not that a
real Debian install does any of them.

The real thing is `packaging/linux/docker_smoke.py`, which needs Docker and network:

```bash
python packaging/linux/docker_smoke.py                                   # build, then deb, tar, wheel
python packaging/linux/docker_smoke.py --kinds deb --skip-build --out /tmp/ate-docker
ATE_DOCKER_TESTS=1 pytest -m build -q                                    # the same run as a test
```

It tags a copy of the checkout `v1.2.3`, builds the bundle with the container's own `/usr/bin/python3`
(3.13 on Debian stable), packages the tarball, the `.deb` and the wheel, and then runs the smoke
script — the exact command the release job runs — in three *fresh* `debian:stable` containers. Each
one also renders a page with the installed `ate` and reads the PDF back with `pdftotext`, because a
valid PDF with no glyphs is exactly the failure the release CI is meant to catch.

> **Never run.** `linux-build` and `linux-smoke` in `release.yml` have still never executed: no tag of
> this repository has been cut, so GitHub Actions has never started them, and the workflow is checked
> by reading it (no `actionlint` here, and none was installed). What *has* run is the same artifact
> trio in the same `debian:stable` image, through `packaging/linux/docker_smoke.py`: `dpkg` has
> installed the `.deb`, `install.sh` has installed the tarball, and `pipx` has installed the wheel.
> The Windows jobs, `ate.iss` and `smoke_installer.py` have still never run anywhere.

## Tests

```bash
PYTHONPATH=src QT_QPA_PLATFORM=offscreen pytest -q
```

The wheel test builds a wheel from a clean copy into a temporary directory and takes a few
seconds. The only test marked `slow` is the one that installs the wheel into a fresh venv
from PyPI: `pytest -m "not slow"` deselects just that one.

The Windows installer test reads `packaging/windows/ate.iss` as text and runs
`build_installer.py --dry-run` against a stub bundle, so it checks the script's sections and
the wrapper's command on Linux, without Windows or Inno Setup.

The frozen-build test is marked `build` as well and is skipped unless you ask for it,
because a PyInstaller build takes minutes:

```bash
ATE_BUILD_TESTS=1 pytest -m build -q
```

### Testing seams: three environment variables

Tests that run `ate` in a real subprocess need to aim the platform paths somewhere writable
and to close Qt dialogs that would otherwise block offscreen. These are test seams, not
Station features — nothing else reads them, and a config's own paths still get no
environment expansion.

| Variable | What it does | Read by |
| --- | --- | --- |
| `ATE_DEFAULT_CONFIG` | Replaces the platform default **config file** path (`/etc/ate/config.json`, `%PROGRAMDATA%\ATE\config.json`), so a test can prove "no config at the default location" without touching `/etc` | `ate.config._platform_default` only |
| `ATE_DATA_DIR` | Replaces the platform **data directory** (`/var/lib/ate`, `%PROGRAMDATA%\ATE`) that `<data>` resolves to | `ate.config._platform_data_dir` only |
| `ATE_AUTO_DISMISS_DIALOG` | Closes every launch dialog by itself, so a refusal exits instead of blocking in `exec()`. It closes with `accept()`, which clicks no button: the missing-config prompt therefore answers **Abort** and the "abort the run and quit?" confirmation answers **keep running**. `""`, `"0"`, `"false"`, `"no"` and `"off"` mean unset | `ate.app._dismiss_if_headless` |

`conftest.py` also points `<data>` at pytest's `basetemp` for the whole session, so no test
ever writes under `/var/lib`.
