# Building ATE from source

Packet 28a: the wheel and the source build, plus the Linux artifacts (28b). The Windows
installer is a separate packet; this page covers running ATE from a checkout, building the
wheel, and packaging the frozen build for Linux.

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
report and the `_pdf` child (the bundle's own executable when frozen) wrote the PDF. Apart from
that temporary directory it touches no Station, database or Test Run.

`build/` and `dist/` are gitignored: build output is never committed, and the build is
reproducible from a clean checkout. The bundle is roughly 490 MB on disk (579 MB before the
Qt exclusions) — Qt's libraries and QtWebEngine are the bulk of it.

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

The bundle carries its own CPython, Qt, QtWebEngine, lupa, pyvisa-py, ICU, NSS, fontconfig and
libX11, so the package needs none of Debian's Python or Qt packages. What it cannot carry is what
it links against at runtime, which is the entire `Depends` list — established by running `ldd` over
a built bundle's `_internal` and keeping only the libraries that resolve to the system:

| Depends | Why |
| --- | --- |
| `libc6` | libc, libm, libdl, libpthread, libresolv: the bundled CPython and Qt are linked against the system glibc |
| `libgl1`, `libegl1` | libGL/libGLX/libGLdispatch and libEGL: every Qt6 Gui-linked library wants them |
| `libdrm2`, `libxcb-dri3-0` | QtWebEngine's GPU/direct-rendering path |
| `libxcb1`, `libxcb-cursor0` | libxcb and its cursor-shape extension: Qt's xcb platform plugin needs both, and without `libxcb-cursor0` it aborts, so the GUI never opens on a clean Debian box |
| `libwayland-client0`, `libwayland-cursor0`, `libwayland-egl1` | only reached when the desktop is Wayland rather than X |

### Testing seams

Only one flag is a seam in the shipped scripts, and it is a real installer flag:

| Variable / flag | What it does |
| --- | --- |
| `install.sh --prefix` / `--bin` | Aim the install at a temporary directory instead of `/opt/ate` and `/usr/bin/ate` |

`postinst` and `postrm` have **no** test seam: they name `/opt/ate`, `/etc/ate` and `/var/lib/ate`
directly. The test runs a copy of each script with those three paths rewritten to a temporary
root, so a variable exported on a Station can never change what the installed package does.

`tests/test_linux_packages_28b.py` runs the scripts against a stub bundle in seconds: it checks
the tarball and the deb layout, that the maintainer scripts are executable and pass `sh -n`, that
`purge` never deletes the data directory, and that `install.sh` copies the bundle, links it and
runs `setup`.

The packaged-artifact test is marked `build` like the frozen-build test: with
`ATE_BUILD_TESTS=1` it builds the bundle, packages it both ways, extracts the `.deb` with
`dpkg-deb -x`, runs the extracted `/opt/ate/ate --version` and `_selftest`, and runs `ldd` over
every `.so` in the bundle so that each library that does not resolve inside it is named in
`Depends`.

## Tests

```bash
PYTHONPATH=src QT_QPA_PLATFORM=offscreen pytest -q
```

The wheel test builds a wheel from a clean copy into a temporary directory and takes a few
seconds. The only test marked `slow` is the one that installs the wheel into a fresh venv
from PyPI: `pytest -m "not slow"` deselects just that one.

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
