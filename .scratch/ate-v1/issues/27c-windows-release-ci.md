# 27c: Windows release CI

**Parent:** 27 (Windows installer and release CI) — `27-windows-installer-and-release-ci.md`

**What to build:** Every tagged release builds and smoke-tests the installer.

**Blocked by:** 27b (Inno Setup installer), 30c (First run after `ate setup`: its smoke checks the database version)

Release CI must build the frozen bundle with CPython 3.14 using `packaging/build_pyinstaller.py --smoke` (27a).

27b's `.iss` has never been compiled: the first CI run must compile it, install silently (`/VERYSILENT /SUPPRESSMSGBOXES /CLOSEAPPLICATIONS /LOG=install.log`), check `ate` on PATH, check upgrades keep `%PROGRAMDATA%\ATE`, and check uninstall leaves it. `/CLOSEAPPLICATIONS` is the switch behind 27b's `CloseApplications=yes`: under `/VERYSILENT` Setup closes a running ATE without asking.

The run must assert PATH before/after install, upgrade and uninstall: one entry after an upgrade, none after uninstall, other entries unchanged, and the value type still `REG_EXPAND_SZ`. It must also check the installer's exit code and the `/LOG` file — 27b's `GetCustomSetupExitCode` returns non-zero when `ate setup` failed, and under `/VERYSILENT` the exit code and the log are the only signs of it.

**Status:** ready-for-agent

- [ ] Tagged releases build the installer and smoke-test it on a clean Windows runner: install, `ate setup`, headless launch that scans a sample Media Release and checks the database version
- [ ] The smoke test asserts the machine PATH before install, after install, after an upgrade and after uninstall, and checks the installer's exit code and its `/LOG` file

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
