# 27b: Inno Setup installer

**Parent:** 27 (Windows installer and release CI) — `27-windows-installer-and-release-ci.md`

**What to build:** A supported Windows installer.

**Blocked by:** 27a (PyInstaller build)

**Status:** resolved

- [x] An Inno Setup installer: per-machine under `Program Files`, admin, `ate` on `PATH`, in-place upgrades keeping config and data; runs `ate setup`; uninstall removes only the program — **written, not compiled or run**: there is no Inno Setup (and no Wine) on any machine that has touched `packaging/windows/ate.iss`. Its first real compile, install, upgrade and uninstall is 27c's Windows runner. What has been run here is the wrapper (`--dry-run`) and `tests/test_inno_installer_27b.py`, which reads the script's sections and the wrapper's command.
- [x] Optional code signing when a certificate is configured (none yet) — **written, not compiled**: `build_installer.py` adds `/DSignEnabled` and `/Ssigntool=...` only when `ATE_SIGN_CERT` and `ATE_SIGN_PASSWORD` are both set, and the `.iss` guards `SignTool` with `#ifdef SignEnabled`. Verified only by the dry-run output and the section-parsing tests.

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
