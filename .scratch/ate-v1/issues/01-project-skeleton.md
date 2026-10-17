# 01: Project skeleton, `ate` CLI and CI

**What to build:** An installable Python package for ATE with an `ate` command-line entry point that prints its version, a pytest setup, and CI that runs the tests on Windows and Linux with CPython 3.13 and 3.14. This is the base every other ticket builds on.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] `ate --version` prints a semver version taken from the git tag at build time (a sensible dev version when untagged)
- [ ] Dependencies are declared: PyQt6, lupa (`>=2.8`), SQLAlchemy, Jinja2, pyserial, pyvisa, pyvisa-py
- [ ] pytest runs with a trivial passing test, and pytest-qt is available for later GUI smoke tests
- [ ] CI runs the test suite on Windows and Linux × CPython 3.13 and 3.14 (standard GIL builds) on every push
- [ ] The package layout separates engine, run session and UI so the rule "engine and run session never import widgets" can be enforced (e.g. by an import-lint test)

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
