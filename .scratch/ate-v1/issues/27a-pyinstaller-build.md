# 27a: PyInstaller build

**Parent:** 27 (Windows installer and release CI) — `27-windows-installer-and-release-ci.md`

**What to build:** A one-directory build of ATE.

**Blocked by:** 22b (PDF at run end), 26b (`ate setup`: permissions)

**Status:** ready-for-agent

- [ ] A PyInstaller one-directory build bundling CPython 3.14, QWebEngine and pyvisa-py; the built `ate --version` runs

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
