# Packaging and installation

ATE's definition of how it is built, shipped and installed, settled in "Settle the remaining Not yet specified items". Vocabulary follows `GLOSSARY.md`.

## Artifacts

| Artifact | Platform | Support | Contents |
| --- | --- | --- | --- |
| Inno Setup installer | Windows (64-bit) | Supported | PyInstaller one-directory build; per-machine install under `Program Files`, needs admin, adds `ate` to `PATH` |
| `.tar.gz` with `install.sh` | Linux | Best effort | The same PyInstaller build |
| `.deb` | Debian stable (currently 13 "trixie") | Best effort | Self-contained: the PyInstaller build under `/opt/ate`, with `/usr/bin/ate` linking to it; no dependency on Debian's Python packages. No `.rpm` |
| Wheel | Any (pipx) | Best effort | ATE as a Python package; dependencies come from PyPI |

- The frozen builds (installer, `.tar.gz`, `.deb`) bundle CPython **3.14**. The wheel supports CPython 3.13 and 3.14, so it installs on Debian 13's system Python 3.13.
- Every build bundles **pyvisa-py**, so LAN (`TCPIP`) and USBTMC instruments work without NI-VISA. pyvisa still prefers NI-VISA when it is installed, and GPIB needs it. ATE never ships NI-VISA.
- No ATE artifact contains Media Releases; they have their own installers.
- Source build instructions are provided: setting up a development environment and building each artifact.

## Preparing a Station: `ate setup`

One CLI command, run as admin or root, prepares a Station for every format. The Windows installer, `install.sh` and the `.deb`'s `postinst` all call it; wheel users run it themselves after `pipx install`.

- Creates the data directories (`%PROGRAMDATA%\ATE`, `/var/lib/ate`) and the config directory.
- Gives ordinary users modify rights on the data directories (the database and its WAL files, `reports`, `logs`), while `config.json` stays admin-only. On Linux it creates an `ate` group for this; adding Operators to the group is a manual admin step.
- Creates a default `config.json` (with a new `station.uuid`) if none exists; it never overwrites an existing config.
- Runs `ate db migrate`: creates the database on a fresh install, or upgrades it after a backup.

So neither a first launch nor the first launch after an upgrade stops on a missing config, a missing database or an outdated database.

## Upgrades and uninstalling

- Upgrades install in place and keep the config and all data.
- Uninstalling (Windows, `apt remove`) removes only the program. `apt purge` also removes the config, but never the database, reports or logs: Test Runs are records.

## Versions

- ATE uses semver (`MAJOR.MINOR.PATCH`), taken from the git tag at build time. It is recorded on every Test Run and in report footers.
- Database schema changes are listed in the release notes; they don't force a major version.

## Code signing

Signing the Windows installer and executables is an optional build step that runs when a code-signing certificate is configured. There is no certificate yet, so builds are unsigned for now and may trigger SmartScreen warnings.

## CI

- Tests run on Windows and Linux with CPython 3.13 and 3.14.
- Every tagged release builds all four artifacts and smoke-tests each in a clean Windows runner or Debian stable container: install, run `ate setup`, then launch ATE headless far enough to scan an example Media Release and check the database version.
