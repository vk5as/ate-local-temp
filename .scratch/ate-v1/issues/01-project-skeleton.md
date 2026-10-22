# 01: Project skeleton, `ate` CLI and CI

**What to build:** An installable Python package for ATE with an `ate` command-line entry point that prints its version, a pytest setup, and CI that runs the tests on Windows and Linux with CPython 3.13 and 3.14. This is the base every other ticket builds on.

**Blocked by:** None (can start immediately)

**Status:** resolved

- [ ] `ate --version` prints a semver version taken from the git tag at build time (a sensible dev version when untagged)
- [ ] Dependencies are declared: PyQt6, lupa (`>=2.8`), SQLAlchemy, Jinja2, pyserial, pyvisa, pyvisa-py
- [ ] pytest runs with a trivial passing test, and pytest-qt is available for later GUI smoke tests
- [ ] CI runs the test suite on Windows and Linux × CPython 3.13 and 3.14 (standard GIL builds) on every push
- [ ] The package layout separates engine, run session and UI so the rule "engine and run session never import widgets" can be enforced (e.g. by an import-lint test)

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.

## Answer

Built on branch `ticket-01-project-skeleton` (commits `31f95ca`–`f4e4c33`). No Lua runtime was implemented — that is deferred.

**`ate --version` from the git tag at build time.** Version lives in one place,
[`src/ate/_version.py`](../src/ate/_version.py): it runs `git describe --tags`,
parses a clean tag (`v1.2.3` → `1.2.3`) or commits-since-tag into a dev
prerelease (`1.2.3-4-gabc…` → `1.2.3.dev4+gabc…`), and falls back to the safe
dev version `0.0.0-dev` when untagged, git is absent, or parsing fails — so the
build never crashes. [`pyproject.toml`](../pyproject.toml) reads it with setuptools
dynamic versioning (`[tool.setuptools.dynamic] version = {attr = "ate._version.__version__"}`), and `cli.py` prints the same value at runtime, so a wheel built from a tag and `ate --version` always agree. Validated locally: tagged checkout → `9.9.9`; untagged/no-git → `0.0.0-dev`.

**Dependencies.** Declared exactly as specified in `pyproject.toml`: PyQt6,
`lupa>=2.8`, SQLAlchemy, Jinja2, pyserial, pyvisa, pyvisa-py; pytest + pytest-qt
under the `[test]` optional extra (pytest-qt is present for later GUI smoke tests).

**Tests.** `tests/test_cli_version.py` covers the CLI version output (`--version`/`-V` print a semver string and exit 0, including untagged), and
`tests/test_architecture_rules.py` enforces "engine and run session never import
widgets" by scanning every `.py` under `src/ate/engine` and `src/ate/run_session`
for widget imports (`PyQt6.QtWidgets`, `PyQt6.uic`, any `widgets` submodule) and
failing loudly if found. The suite is green (4 tests). A root `conftest.py` lets
pytest import the src-layout without an editable install; CI installs it instead.

**CI.** `.github/workflows/ci.yml` runs on every push across a matrix of Windows
and Linux × CPython 3.13 and 3.14: checkout, `pip install -e .[test]`, `pytest`.

**Package layout.** src-layout with top-level `src/ate/{__init__,cli,errors,_version}.py` and the three subpackages from the spec's architecture rule —
[`engine/`](../src/ate/engine/__init__.py) (flow control + Lua host),
[`run_session/`](../src/ate/run_session/__init__.py) (UI-free state + commands a layout binds to), and [`ui/`](../src/ate/ui/__init__.py) (QtWidgets layouts/screens). Each `__init__.py` documents its role; the engine and run session import no widgets.

