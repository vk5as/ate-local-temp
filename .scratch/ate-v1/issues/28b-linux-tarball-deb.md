# 28b: Linux tarball and `.deb`

**Parent:** 28 (Linux packages, wheel and source build instructions) — `28-linux-packages.md`

**What to build:** Best-effort Linux packages.

**Blocked by:** 27a (PyInstaller build)

**Status:** ready-for-agent

- [ ] A `.tar.gz` of the PyInstaller build with `install.sh` calling `ate setup`
- [ ] A self-contained `.deb` for Debian stable (`/opt/ate`, `/usr/bin/ate` link, `postinst` calls `ate setup`); `apt remove` keeps config and data, `apt purge` removes config but never the database, reports or logs

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
