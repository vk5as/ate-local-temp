# 32a: Switch the Qt binding from PyQt6 to PySide6

**Parent:** none. User request, 2026-10-31, drafted from the licence gap found after `v0.1.0-rc2`.

**Why:** ATE ships PyQt6 and PyQt6-WebEngine, which are GPL or commercial, while `pyproject.toml` says MIT. PySide6 is available under LGPL-3.0, which lets ATE keep its own licence as long as the LGPL terms for Qt and PySide6 are met. The installer and `.deb` already keep Qt as separate shared libraries in `_internal` (onedir).

**What to build:** Replace PyQt6 with PySide6 everywhere, with no change in behaviour. This packet changes the binding only. It does not upgrade Qt, refactor screens or add features.

**Blocked by:** none. Needs CI on Windows and Linux, so the repo must be able to run Actions when this is merged.

**Status:** ready-for-agent

**Size of the change (2026-10-31):** about 112 Qt import lines (63 `QtCore`, 25 `QtWidgets`, 12 `QtGui`, 5 `QtWebEngineCore`, 4 `QtTest`, 3 `from PyQt6 import`) and 31 `pyqtSignal` uses in `src`/`packaging`. No `pyqtSlot`, `pyqtProperty` or `QVariant`. 9 test lines use `sip`/`isdeleted`. Most of this is mechanical. The behaviour differences below are the real work.

## Dependencies

- [ ] `pyproject.toml` depends on `PySide6-Addons>=6.11,<6.12` (it pins the matching `PySide6-Essentials` and contains QtWebEngine in 6.11; confirm both on PyPI first, and if the split differs, use the smallest set that provides QtWebEngine). Stay on Qt 6.11, the version every test and release run so far used, so that a failure points at the binding and not at a new Qt.
- [ ] No PyQt6 package remains in any dependency list, and a fresh venv install has no `PyQt6*` distribution. Rebuild the venv; do not install over it.
- [ ] The Qt-band comment in `pyproject.toml` and the "Check Qt and QtWebEngine are the same Qt" step in `ci.yml` are rewritten for PySide6. The step still fails the job if QtWebEngine cannot be imported or comes from a different Qt.
- [ ] `tests/test_wheel_build_28a.py` floor checks pass against the new names.

## Code

- [ ] `PyQt6.` imports become `PySide6.`, `pyqtSignal` becomes `Signal`, in `src`, `packaging`, `tests`, `conftest.py` and `examples`.
- [ ] `sip.isdeleted` in tests becomes `shiboken6.isValid` (inverted).
- [ ] `tests/test_architecture_rules.py` forbids widget imports under the new names, and is checked to still fail on a planted `PySide6.QtWidgets` import. Its patterns must not silently match nothing.
- [ ] pytest-qt uses `qt_api = pyside6`, set in the pytest config.
- [ ] The final tree has no `PyQt6`, `pyqt6`, `pyqtSignal` or `sip` outside `.scratch` and historical comments that name the old behaviour on purpose.

## Behaviour differences to check, not assume

Each of these gets an answer in the rework notes at the bottom of this file: what was checked and what was found.

- [ ] **Which thread a slot runs on.** The engine's signals are emitted on the worker `QThread`. Audit every `.connect` on a worker signal whose target is a lambda, a closure or a method of a non-`QObject`, because the bindings can differ on which thread runs them. A test asserts that the session and screen slots run on the main thread. Anything that touches a widget or the store from the worker is a bug to fix.
- [ ] **Exception in a worker or slot.** The comment on `RunEngine.run` says an escaping exception aborts the process under PyQt. Keep the guard, update the comment to what PySide6 does, and confirm `tests/test_unexpected_run_end.py` and `test_an_exception_leaving_the_worker_does_not_abort_the_process` still prove something. An exception in a slot may now only print, so check that no test or screen relied on the abort.
- [ ] **Blocking waits and the GIL.** `QThread.wait()`, the blocking Operator input and the abort path must not deadlock with a worker that needs the GIL while it runs Lua. Run the abort and close-during-run tests under load.
- [ ] **Object lifetime.** `report_qt_fatal`, the teardown and `QProcess` lifetime fixes, and `_RUNNING` worker tracking in `run_engine.py` were all shaped by PyQt behaviour. PySide6 raises different errors for deleted objects, and owns objects differently. Re-check `pdf_job.py`, `stop_pdf_jobs()`, `HeadlessRunSession.close`/`__del__` and every catch of `RuntimeError` for a deleted object.
- [ ] **Signal argument types.** A `Signal(int)` or `Signal(str)` given `None` or a large value may behave differently. Check `run_engine.py`'s signals with the run IDs, sequence numbers and timings they really carry; use `object` where the value can be `None`.
- [ ] **Dialogs and printing.** `dismiss_if_headless`, `clickedButton()` returning `None`, the mismatch confirmation and `printToPdf` must behave as before. `tests/test_modals_never_block_headless.py` and the PDF tests must pass unchanged, apart from imports.
- [ ] **Interpreter shutdown.** The Qt 6.11 shutdown segfault fix (one `QApplication` in the suite) and exit codes: a clean run exits 0 on both platforms, with no exit 139 or 134.

## Packaging

- [ ] `packaging/pyinstaller/ate.spec`: hidden imports, and the excludes list (`PyQt6.Qt3D*` and the rest) under PySide6 names. PySide6 needs `shiboken6` in the bundle.
- [ ] `build_pyinstaller.py`'s `verify_bundle()` still fails on a bundle with no Qt platform plugin, no `QtWebEngineProcess` or no `.pak` files. PySide6 lays these out under different paths, so update the paths in `verify_bundle()`, `ate_entry.py` (helper location, Linux and Windows) and the 27a/28a/28b/28c tests, and prove the check still fails on a stripped bundle.
- [ ] `packaging/linux/build_linux.py` `Depends` and `WHEEL_SYSTEM_LIBS`, `smoke_linux.py` and `docker_smoke.py`: re-derive the system libraries from what the PySide6 wheel and bundle need, in the bare Debian container. PySide6's Linux wheels need a recent glibc (`manylinux_2_34` at 6.12), so check the wheel installs on Debian stable.
- [ ] Bundle sizes are reported before and after. Anything over 20% larger gets more excludes or an explanation.

## Licence

These are the engineering steps for LGPL compliance. They are not legal advice, and the user decides whether the release needs a legal review.

- [ ] Every distributed artifact (installer, `.deb`, tarball) carries the LGPL-3.0 and GPL-3.0 texts, a Qt/PySide6 notice stating the version and where to get the source, and a statement that Qt can be replaced. Keep the bundle onedir, with Qt as separate, unmodified shared libraries. A test fails if the notices are missing from the bundle.
- [ ] A `THIRD_PARTY_NOTICES` file lists every runtime dependency and its licence, generated from the installed distributions, and including lupa/Lua, SQLAlchemy, Jinja2, pyserial, pyvisa and pyvisa-py. Any dependency whose licence is not compatible with MIT distribution is reported, not guessed at.
- [ ] The repo has no `LICENSE` file although `pyproject.toml` says MIT. Add none in this packet; report it in the rework notes for the user to decide.

## Docs

- [ ] `README.md`, `docs/building-from-source.md`, `.scratch/ate-v1/spec.md` (line 20) and `test-report-format.md` name PySide6. Other wayfinding documents are history and stay as they are.
- [ ] A new ADR under `docs/adr/` records the switch and why. See `docs/agents/domain.md` for the format.

## Acceptance

- [ ] Full suite passes three times in a row on Linux, with no new skips. The skip list is compared before and after; a third of the suite once skipped silently when WebEngine would not import.
- [ ] A real PDF renders with text in it, from source and from the frozen bundle.
- [ ] CI is green on Linux and Windows, and a manual `release.yml` run is green on all 5 jobs, including the Windows installer checks and the `.deb`, tarball and wheel smokes.
- [ ] One GUI run on Xvfb with screenshots of the Run and History screens, compared with the same screens on PyQt6: same layout, fonts and theme. PySide6 is the same Qt, so any visible difference is a bug to explain.

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes.
