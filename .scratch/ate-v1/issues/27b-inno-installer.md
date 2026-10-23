# 27b: Inno Setup installer

**Parent:** 27 (Windows installer and release CI) — `27-windows-installer-and-release-ci.md`

**What to build:** A supported Windows installer.

**Blocked by:** 27a (PyInstaller build)

**Status:** ready-for-agent

- [ ] An Inno Setup installer: per-machine under `Program Files`, admin, `ate` on `PATH`, in-place upgrades keeping config and data; runs `ate setup`; uninstall removes only the program
- [ ] Optional code signing when a certificate is configured (none yet)

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
