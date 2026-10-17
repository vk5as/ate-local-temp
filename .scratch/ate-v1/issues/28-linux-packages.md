# 28: Linux packages, wheel and source build instructions

**What to build:** Best-effort Linux delivery and instructions for building from source.

**Blocked by:** 27 (Windows installer and release CI)

**Status:** ready-for-agent

- [ ] A `.tar.gz` of the PyInstaller build with `install.sh` calling `ate setup`
- [ ] A self-contained `.deb` for Debian stable (`/opt/ate`, `/usr/bin/ate` link, `postinst` calls `ate setup`); `apt remove` keeps config and data, `apt purge` removes config but never the database, reports or logs
- [ ] A wheel supporting CPython 3.13 and 3.14 (users run `ate setup` after `pipx install`)
- [ ] Source build instructions
- [ ] Tagged releases build all three and smoke-test each in a Debian stable container

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
