# 27c: Windows release CI

**Parent:** 27 (Windows installer and release CI) — `27-windows-installer-and-release-ci.md`

**What to build:** Every tagged release builds and smoke-tests the installer.

**Blocked by:** 27b (Inno Setup installer), 30c (First run after `ate setup`: its smoke checks the database version)

Release CI must build the frozen bundle with CPython 3.14 using `packaging/build_pyinstaller.py --smoke` (27a).

27b's `.iss` has never been compiled: the first CI run must compile it, install silently (`/VERYSILENT /SUPPRESSMSGBOXES /CLOSEAPPLICATIONS /LOG=install.log`), check `ate` on PATH, check upgrades keep `%PROGRAMDATA%\ATE`, and check uninstall leaves it. `/CLOSEAPPLICATIONS` is the switch behind 27b's `CloseApplications=yes`: under `/VERYSILENT` Setup closes a running ATE without asking.

The run must assert PATH before/after install, upgrade and uninstall: one entry after an upgrade, none after uninstall, other entries unchanged, and the value type still `REG_EXPAND_SZ`. It must also check the installer's exit code and the `/LOG` file — 27b's `GetCustomSetupExitCode` returns non-zero when `ate setup` failed, and under `/VERYSILENT` the exit code and the log are the only signs of it.

**Status:** resolved

- [x] Tagged releases build the installer and smoke-test it on a clean Windows runner: install, `ate setup`, headless launch that scans a sample Media Release and checks the database version — written; never run: first run happens on the first tag
- [x] The smoke test asserts the machine PATH before install, after install, after an upgrade and after uninstall, and checks the installer's exit code and its `/LOG` file — written; never run: first run happens on the first tag

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.

## What was written, and what has never run

Written on a Linux machine with no Windows, no Inno Setup and no way to run a workflow:

- `.github/workflows/release.yml` — **never run.** It only triggers on `push: tags: ["v*"]` and
  `workflow_dispatch`, so an ordinary push cannot start it; no tag of this repository has been cut.
  The Linux release jobs (28c) have a commented placeholder in the same file and are not written.
  No `actionlint` is installed here, and none was installed; the workflow is checked by
  `tests/test_release_ci_27c.py` reading it.
- `packaging/windows/smoke_installer.py` — **never run.** Its install, upgrade and uninstall steps
  need `setup.exe` and the Windows registry. Its PATH helpers and assertions are plain functions,
  and the tests drive the whole sequence through a fake registry and a fake installer on Linux.
- `ate _selftest --scan <dir>` (src/ate/cli.py) — this one does run on Linux, and is tested here
  against the shipped example Media Release.

The first time any of the Windows pieces execute is the first tag's release job.
