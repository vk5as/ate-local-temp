# 27a: PyInstaller build

**Parent:** 27 (Windows installer and release CI) — `27-windows-installer-and-release-ci.md`

**What to build:** A one-directory build of ATE.

**Blocked by:** 22b (PDF at run end), 26b (`ate setup`: permissions)

**Status:** resolved

- [x] A PyInstaller one-directory build recipe that bundles QWebEngine and pyvisa-py; the built `ate --version` runs
  Proven on CPython 3.13 here (the only Python on this machine) with
  `packaging/build_pyinstaller.py --smoke` and `packaging/pyinstaller/ate.spec`. Nothing in this
  packet reads a CPython version, so the same recipe freezes a 3.14 bundle — but no job here
  builds one or runs it: the 3.14 frozen build and its smoke run are release CI's job (27c).

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.

**Decision (user, 2026-10-30):** the local build here uses CPython 3.13, the only Python on this machine; it proves the recipe. CI builds the 3.14 bundle. Keep the build script and spec Python-version-agnostic, so CI's 3.14 runner uses the same recipe.
