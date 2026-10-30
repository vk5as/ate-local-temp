# 27c: Windows release CI

**Parent:** 27 (Windows installer and release CI) — `27-windows-installer-and-release-ci.md`

**What to build:** Every tagged release builds and smoke-tests the installer.

**Blocked by:** 27b (Inno Setup installer), 30c (First run after `ate setup`: its smoke checks the database version)

Release CI must build the frozen bundle with CPython 3.14 using `packaging/build_pyinstaller.py --smoke` (27a).

**Status:** ready-for-agent

- [ ] Tagged releases build the installer and smoke-test it on a clean Windows runner: install, `ate setup`, headless launch that scans a sample Media Release and checks the database version

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
