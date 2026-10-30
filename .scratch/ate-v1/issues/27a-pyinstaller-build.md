# 27a: PyInstaller build

**Parent:** 27 (Windows installer and release CI) — `27-windows-installer-and-release-ci.md`

**What to build:** A one-directory build of ATE.

**Blocked by:** 22b (PDF at run end), 26b (`ate setup`: permissions)

**Status:** ready-for-agent

- [ ] A PyInstaller one-directory build bundling CPython 3.14, QWebEngine and pyvisa-py; the built `ate --version` runs

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.

**Decision (user, 2026-10-30):** the local build here uses CPython 3.13, the only Python on this machine; it proves the recipe. CI builds the 3.14 bundle. Keep the build script and spec Python-version-agnostic, so CI's 3.14 runner uses the same recipe.
