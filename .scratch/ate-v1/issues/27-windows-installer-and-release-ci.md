# 27: Windows installer and release CI

**What to build:** A supported Windows installer is built and smoke-tested on every tagged release.

**Blocked by:** 22 (PDF via the helper process), 26 (`ate setup`)

**Status:** ready-for-agent

- [ ] A PyInstaller one-directory build bundling CPython 3.14, QWebEngine and pyvisa-py
- [ ] An Inno Setup installer: per-machine under `Program Files`, admin, `ate` on `PATH`, in-place upgrades keeping config and data; runs `ate setup`; uninstall removes only the program
- [ ] Optional code signing when a certificate is configured (none yet)
- [ ] Tagged releases build the installer and smoke-test it on a clean Windows runner: install, `ate setup`, headless launch that scans a sample Media Release and checks the database version

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
