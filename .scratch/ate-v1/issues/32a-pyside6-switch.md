# 32a: Switch the Qt binding from PyQt6 to PySide6

**Parent:** none. User request, 2026-10-31, drafted from the licence gap found after `v0.1.0-rc2`.

**Why:** ATE ships PyQt6 and PyQt6-WebEngine, which are GPL or commercial, while `pyproject.toml` says MIT. PySide6 is available under LGPL-3.0, which lets ATE keep its own licence as long as the LGPL terms for Qt and PySide6 are met. The installer and `.deb` already keep Qt as separate shared libraries in `_internal` (onedir).

**What to build:** Replace PyQt6 with PySide6 everywhere, with no change in behaviour. This packet changes the binding only. It does not upgrade Qt, refactor screens or add features.

**Blocked by:** none. Needs CI on Windows and Linux, so the repo must be able to run Actions when this is merged.

**Status:** ready-for-agent

**Size of the change (2026-10-31):** about 112 Qt import lines (63 `QtCore`, 25 `QtWidgets`, 12 `QtGui`, 5 `QtWebEngineCore`, 4 `QtTest`, 3 `from PyQt6 import`) and 31 `pyqtSignal` uses in `src`/`packaging`. No `pyqtSlot`, `pyqtProperty` or `QVariant`. 9 test lines use `sip`/`isdeleted`. Most of this is mechanical. The behaviour differences below are the real work.

## Dependencies

- [x] `pyproject.toml` depends on `PySide6-Addons>=6.11,<6.12` (it pins the matching `PySide6-Essentials` and contains QtWebEngine in 6.11; confirm both on PyPI first, and if the split differs, use the smallest set that provides QtWebEngine). Stay on Qt 6.11, the version every test and release run so far used, so that a failure points at the binding and not at a new Qt.
- [x] No PyQt6 package remains in any dependency list, and a fresh venv install has no `PyQt6*` distribution. Rebuild the venv; do not install over it.
- [x] The Qt-band comment in `pyproject.toml` and the "Check Qt and QtWebEngine are the same Qt" step in `ci.yml` are rewritten for PySide6. The step still fails the job if QtWebEngine cannot be imported or comes from a different Qt.
- [x] `tests/test_wheel_build_28a.py` floor checks pass against the new names.

## Code

- [x] `PyQt6.` imports become `PySide6.`, `pyqtSignal` becomes `Signal`, in `src`, `packaging`, `tests`, `conftest.py` and `examples`.
- [x] `sip.isdeleted` in tests becomes `shiboken6.isValid` (inverted).
- [x] `tests/test_architecture_rules.py` forbids widget imports under the new names, and is checked to still fail on a planted `PySide6.QtWidgets` import. Its patterns must not silently match nothing.
- [x] pytest-qt uses `qt_api = pyside6`, set in the pytest config.
- [x] The final tree has no `PyQt6`, `pyqt6`, `pyqtSignal` or `sip` outside `.scratch` and historical comments that name the old behaviour on purpose.

## Behaviour differences to check, not assume

Each of these gets an answer in the rework notes at the bottom of this file: what was checked and what was found.

- [x] **Which thread a slot runs on.** The engine's signals are emitted on the worker `QThread`. Audit every `.connect` on a worker signal whose target is a lambda, a closure or a method of a non-`QObject`, because the bindings can differ on which thread runs them. A test asserts that the session and screen slots run on the main thread. Anything that touches a widget or the store from the worker is a bug to fix.
- [x] **Exception in a worker or slot.** The comment on `RunEngine.run` says an escaping exception aborts the process under PyQt. Keep the guard, update the comment to what PySide6 does, and confirm `tests/test_unexpected_run_end.py` and `test_an_exception_leaving_the_worker_does_not_abort_the_process` still prove something. An exception in a slot may now only print, so check that no test or screen relied on the abort.
- [x] **Blocking waits and the GIL.** `QThread.wait()`, the blocking Operator input and the abort path must not deadlock with a worker that needs the GIL while it runs Lua. Run the abort and close-during-run tests under load.
- [x] **Object lifetime.** `report_qt_fatal`, the teardown and `QProcess` lifetime fixes, and `_RUNNING` worker tracking in `run_engine.py` were all shaped by PyQt behaviour. PySide6 raises different errors for deleted objects, and owns objects differently. Re-check `pdf_job.py`, `stop_pdf_jobs()`, `HeadlessRunSession.close`/`__del__` and every catch of `RuntimeError` for a deleted object.
- [x] **Signal argument types.** A `Signal(int)` or `Signal(str)` given `None` or a large value may behave differently. Check `run_engine.py`'s signals with the run IDs, sequence numbers and timings they really carry; use `object` where the value can be `None`.
- [x] **Dialogs and printing.** `dismiss_if_headless`, `clickedButton()` returning `None`, the mismatch confirmation and `printToPdf` must behave as before. `tests/test_modals_never_block_headless.py` and the PDF tests must pass unchanged, apart from imports.
- [x] **Interpreter shutdown.** The Qt 6.11 shutdown segfault fix (one `QApplication` in the suite) and exit codes: a clean run exits 0 on both platforms, with no exit 139 or 134.

## Packaging

- [x] `packaging/pyinstaller/ate.spec`: hidden imports, and the excludes list (`PyQt6.Qt3D*` and the rest) under PySide6 names. PySide6 needs `shiboken6` in the bundle.
- [x] `build_pyinstaller.py`'s `verify_bundle()` still fails on a bundle with no Qt platform plugin, no `QtWebEngineProcess` or no `.pak` files. PySide6 lays these out under different paths, so update the paths in `verify_bundle()`, `ate_entry.py` (helper location, Linux and Windows) and the 27a/28a/28b/28c tests, and prove the check still fails on a stripped bundle.
- [x] `packaging/linux/build_linux.py` `Depends` and `WHEEL_SYSTEM_LIBS`, `smoke_linux.py` and `docker_smoke.py`: re-derive the system libraries from what the PySide6 wheel and bundle need, in the bare Debian container. PySide6's Linux wheels need a recent glibc (`manylinux_2_34` at 6.12), so check the wheel installs on Debian stable.
- [x] Bundle sizes are reported before and after. Anything over 20% larger gets more excludes or an explanation.

## Licence

These are the engineering steps for LGPL compliance. They are not legal advice, and the user decides whether the release needs a legal review.

- [x] Every distributed artifact (installer, `.deb`, tarball) carries the LGPL-3.0 and GPL-3.0 texts, a Qt/PySide6 notice stating the version and where to get the source, and a statement that Qt can be replaced. Keep the bundle onedir, with Qt as separate, unmodified shared libraries. A test fails if the notices are missing from the bundle.
- [x] A `THIRD_PARTY_NOTICES` file lists every runtime dependency and its licence, generated from the installed distributions, and including lupa/Lua, SQLAlchemy, Jinja2, pyserial, pyvisa and pyvisa-py. Any dependency whose licence is not compatible with MIT distribution is reported, not guessed at.
- [x] The repo has no `LICENSE` file although `pyproject.toml` says MIT. Add none in this packet; report it in the rework notes for the user to decide.

## Docs

- [x] `README.md`, `docs/building-from-source.md`, `.scratch/ate-v1/spec.md` (line 20) and `test-report-format.md` name PySide6. Other wayfinding documents are history and stay as they are.
- [x] A new ADR under `docs/adr/` records the switch and why. See `docs/agents/domain.md` for the format.

## Acceptance

- [x] Full suite passes three times in a row on Linux, with no new skips. The skip list is compared before and after; a third of the suite once skipped silently when WebEngine would not import.
- [x] A real PDF renders with text in it, from source and from the frozen bundle.
- [ ] CI is green on Linux and Windows, and a manual `release.yml` run is green on all 5 jobs, including the Windows installer checks and the `.deb`, tarball and wheel smokes.
- [x] One GUI run on Xvfb with screenshots of the Run and History screens, compared with the same screens on PyQt6: same layout, fonts and theme. PySide6 is the same Qt, so any visible difference is a bug to explain.

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes.

## Rework notes

Done on branch `packet-32a-pyside6`. Everything below was run on this Linux box. Nothing was run on
Windows, in GitHub Actions, or in the release workflow: those results are marked "from documentation"
or "not run" where they appear, and the CI acceptance box above is deliberately left unticked.

### What ran, and the numbers

Three full suite runs in a row, in a fresh venv (`/tmp/venv-32a`, built with nothing in it but this
packet's dependencies), `QT_QPA_PLATFORM=offscreen`:

```
1431 passed, 4 skipped in 335.47s (0:05:35)
1431 passed, 4 skipped in 340.46s (0:05:40)
1431 passed, 4 skipped in 332.98s (0:05:32)
```

After the review rework, three more runs in the same venv:

```
1462 passed, 4 skipped in 272.36s (0:04:32)
1462 passed, 4 skipped in 270.70s (0:04:30)
1462 passed, 4 skipped in 267.10s (0:04:27)
```

The same four tests skip. After the rework, `ATE_BUILD_TESTS=1 pytest -m build` was re-run on
`tests/test_pyinstaller_build_27a.py` only (`1 passed`), so the new generator ran in a build venv. The
`.deb` and container gates below predate the rework and were not re-run.

Those wall-clock times are from a loaded box and do not measure the binding. A reviewer ran both
suites on the same box and got 265 s (PySide6) against 246 s (PyQt6), about 7% slower. The reviewer
puts that at per-Qt-call overhead, which was not measured separately here.

The same four tests skip in all three runs, and they are the same four that skipped before the switch
(the baseline, from the pre-switch checkout in the shared venv, was `1424 passed, 4 skipped in
255.53s`): the opt-in 30b screenshots, the two `ATE_BUILD_TESTS=1` frozen-build tests, and the
`ATE_DOCKER_TESTS=1` container test. No test went from running to skipped, and no skip was added.

The two slow gates were each run once, separately, and both passed:

- `ATE_BUILD_TESTS=1 pytest -m build` on `tests/test_pyinstaller_build_27a.py` and
  `tests/test_linux_packages_28b.py`: the real PyInstaller bundle is built, packaged as a `.deb`,
  unpacked and run (`--version`, `_selftest`), and the `ldd` sweep over the bundle finds every
  library it cannot resolve mapped either to a `Depends` entry or to `NEVER_OPENED`.
- `ATE_DOCKER_TESTS=1 pytest tests/test_release_ci_28c.py -m build`: `packaging/linux/docker_smoke.py`
  built the artifacts in a `debian:stable` container and smoke-tested the `.deb`, the tarball and the
  wheel in three fresh containers: `1 passed, 66 deselected in 597.19s (0:09:57)`.

Sizes, measured on bundles built on this machine:

| | PyQt6 (before) | PySide6 (after) |
|---|---|---|
| onedir bundle | 491,687,228 B, 454 files | 488,781,911 B, 444 files |
| `.tar.gz` artifact | 198,491,010 B | 199,766,770 B |
| `.deb` artifact | 146,857,180 B | 148,071,984 B |

The bundle is 2.9 MB smaller (0.6%), not larger, so nothing needs more excludes. The two artifacts are
about 1.2 MB bigger while the bundle is smaller: the four licence files are new (`_internal/licences`,
46,847 B), and the PySide6/shiboken6 binaries compress slightly worse than the PyQt6 ones they
replace (`_internal/PySide6` 402,424,704 B against `_internal/PyQt6` 405,862,577 B). Both are well
inside the 20% the packet cares about.

The dependency installed is `PySide6-Addons 6.11.3`, which pins `PySide6-Essentials 6.11.3` and
`shiboken6 6.11.3`; the Linux wheel is `cp310-abi3-manylinux_2_34_x86_64`, which Debian stable
(glibc 2.41) accepts — the container build and the three smokes above are the proof. Qt itself is
6.11.3 here and 6.11.2 in the PyQt6 venv: the same Qt 6.11 line, one patch apart. No `PyQt6*`,
`PyQt6-WebEngine*` or `sip` distribution is in the new venv.

A real PDF with text in it came out of both paths: from source, `ate _selftest --run` wrote a 65,764
byte report PDF that `pdftotext` reads; from the frozen bundle the smoke ran the same selftest and got
65,656 bytes, "with text", plus a 13,695 byte `ate _pdf` render of a report page.

The GUI comparison ran on a real X display: `xvfb-run` with the xcb platform, the Run screen at idle,
with a schedule picked, and finished, plus the History screen with one recorded run, in both bindings.
The four widget grabs are byte-identical (same md5), and the four whole-window `xwd` captures have zero
differing pixels (`compare -metric AE` = 0). The geometry dump — every widget's object name and
rectangle — is identical apart from the first line, which names the binding and the Qt version. That
is the whole of the comparison: these four screens, in this one run, on Qt 6.11.3 against Qt 6.11.2.
This host has no `libxcb-cursor.so.0`, so the run used a `dpkg -x`-extracted copy of Debian's
`libxcb-cursor0` on `LD_LIBRARY_PATH`; nothing was installed on the machine.

### Behaviour differences, one line each

- **Thread a slot runs on.** The first pass read the 69 `.connect(` calls in `src/ate/ui` and
  `src/ate/run_session` and claimed that was all of them. It was not: the connects at
  `engine/run_engine.py:220,226`, `report/pdf_job.py:65,103,104` and `report/pdf.py:390,391` were
  missed. Read now. The two in `run_engine.py` are what a worker reaches (`_op.asked`, `finished`) and
  the test covers them; the `pdf_job.py` ones live on the main thread with their `QProcess`, and
  `report_written` is checked landing there; the `pdf.py` ones run inside the helper process, on its
  main thread, with no worker in it, so they have no test here. `tests/test_signal_threads_32a.py` now drives a real run with a
  Stop-on-Fail pause, a POLL and a blocking input. All eight engine signals are recorded being emitted
  on the worker, and each goes to a lambda, a closure, a `QObject` method and a plain method, plus the
  session's own slots: all ran on the main thread. A control test connects `DirectConnection` and sees
  the worker's thread, and planting `DirectConnection` in the main test fails it (`a worker signal ran
  its slot outside the main thread`). Nothing had to be fixed. Only PySide6 was run; "both bindings"
  was not something this test could show.
- **An exception escaping a slot or `QThread.run`.** PyQt6 aborts the process (exit 134) before the
  probe's next line prints. PySide6 prints `Error calling Python override of QThread::run():` (or the
  traceback for a slot) and the process keeps going, which is why the guard in `RunEngine.run` matters
  more now, not less: without it the run would be left open and the screen would wait for a FINISHED
  that never arrives. The guard is unchanged, the docstring now says what PySide6 does, and
  `tests/test_unexpected_run_end.py` and `test_an_exception_leaving_the_worker_does_not_abort_the_process`
  still prove the run is recorded INTERRUPTED and FINISHED is still announced.
  An exception in a slot was first left at "may only print". That made it invisible, so it now has a
  route. PySide6 calls `sys.excepthook` for a direct slot, a queued slot, a timer callback and a
  `QThread.run` override (probed; the last runs on the worker), so `launch()` installs
  `ate.exception_log.UnhandledExceptionLog` there. It writes the traceback to the per-launch
  application log at ERROR and passes the exception to the previous hook, so stderr and pytest-qt's
  capture still get it. The Operator sees one red line in the main window's status bar: "Internal error: this window may have
  stopped updating. A Test Run in progress keeps recording. Details: <log path>". No dialog, because
  a modal box would sit in front of a run and nobody has to answer it for the run to continue; no
  abort, because the worker's record is still good. The status bar exists only from the first error
  on, so a healthy window keeps the mockup's layout. `qInstallMessageHandler` was not used: it sees
  Qt's messages, not Python exceptions. It covers the GUI launch only: `ate _selftest` and other
  headless paths have no application log and still print. Child-process tests no longer trust exit 0:
  `assert_no_swallowed_exception` also reads stderr, in 10a, the `test_windows_ci_fixes.py` children,
  the two in `test_modals_never_block_headless.py` and the one in `test_gui_launch_30a.py`. The other
  `returncode == 0` tests run things with no Qt event loop (`--version`, `ate setup`, `sh -n`, dpkg) or are
  frozen-build smokes, which were not changed.
- **Blocking waits and the GIL.** `QThread.wait()`, the blocking Operator input and the abort path ran
  with eight busy-loop processes competing for three cores: `73 passed in 29.90s`
  (`tests/test_abort_blocking.py`, `tests/test_lua_seam13c_abort.py`, `tests/test_gui_launch_30a.py`,
  `tests/test_headless_session.py`). A worker running Lua finished, and `wait(5000)` returned True.
  Repeated after the rework with the signal and exception-log tests added: `82 passed in 34.61s`.
- **Object lifetime.** The message differs: PyQt6 raises `RuntimeError: wrapped C/C++ object of type
  Thing has been deleted`; PySide6 raises `RuntimeError: libshiboken: Internal C++ object (Thing)
  already deleted.` Every catch in `pdf_job.py`, `stop_pdf_jobs()`, `HeadlessRunSession.close`/`__del__`
  and the teardown is `except RuntimeError`, so they behave the same; `sip.isdeleted(x)` became
  `not shiboken6.isValid(x)` and the tests that relied on the deleted-object path still see it. One
  real difference found: constructing a second `QCoreApplication` while a `QApplication` exists raises
  in PySide6 and is tolerated by PyQt6, which is why the suite still keeps exactly one application, and
  why `conftest.py` destroys the widget tree before quitting (an orphaned `QWidget` finalized at
  interpreter shutdown segfaults in this Qt 6.11 build either way).
- **Signal argument types.** `Signal(int)` in PySide6 is a C `int`. With a slot connected,
  `emit(2**31)` returns normally and the slot receives -2147483648, with a `RuntimeWarning`; the
  `OverflowError` stays pending behind the slot, so a slot's next C call can raise `SystemError`. With
  nothing connected, `emit` raises `OverflowError`. PyQt6 wraps as well (a reviewer's probe, not re-run
  here): the earlier line here saying it accepts 9223372036854775807 was false. `Signal(str)` turns `5`
  and `None` into `''`, with one stderr line for `5`, where PyQt6 raised `TypeError` for `5`. So a slot
  cannot see a bad value, and the earlier test, which looked from a slot, could not fail. The test now
  wraps `emit` on the sending side, and a planted `seq + 2**31` or a non-`str` phase fails it. What is
  really sent: node sequence, Attempt number and input index (bounded by the schedule; `LuaHost.inputs`
  refuses an undeclared index before it reaches a signal), run UUID (`str` from the store), `"pre"`/`"post"`,
  the choice (`CONTINUE`/`RETRY`/`ABORT`, from buttons), and report paths (`str()` or `""`). None
  needs `object`. `session.choose()` does not validate a non-`str` argument, which would emit `''`, but
  every caller passes a `Signal(str)` value that started as a button's constant. `input_answered` can carry any
  type and already declares `object`.
- **Dialogs and printing.** `dismiss_if_headless`, `clickedButton()` returning `None` headless, the
  mismatch confirmation and the `printToPdf` path are unchanged: `tests/test_modals_never_block_headless.py`
  and the PDF tests pass with imports renamed and nothing else touched.
- **Interpreter shutdown.** Clean runs exit 0; the child-process tests in `tests/test_windows_ci_fixes.py`
  assert exit 0 and no `Fatal Python error`, and the three full suite runs ended with no aborted or
  segfaulting process. On Windows this is asserted by the same tests but not executed here.

### Packaging finding worth recording

PySide6's `PySide6.QtWebEngineWidgets` imports `PySide6.QtWebChannel` on its own; PyQt6's
`QtWebEngineWidgets` never touched that binding. Keeping `QtWebChannel` in the spec's excludes list
therefore produced a bundle that looked fine — the helper binary and all 57 `.pak` files were present —
but died in `ate _pdf` with "could not import module 'PySide6.QtWebChannel'". It is out of the list,
with the reason written above it, and `test_the_spec_does_not_exclude_what_webengine_imports_itself`
now runs the WebEngine import and checks the excludes list against what it actually pulls in.

`WHEEL_SYSTEM_LIBS` was re-derived in a bare `debian:stable` container with `PySide6-Addons 6.11.3`:
`ldd` over every `.so` in the wheel, install the resulting list, then run the selftest (65,961 byte
PDF, with text). Six xcb libraries were added to the list (cursor, icccm, keysyms, shape, xkb,
xkbcommon-x11): with only the GL and WebEngine packages installed, `QApplication()` under `xvfb-run`
aborts with "Could not load the Qt platform plugin \"xcb\"" and starts once those six are present.
`DEPENDS` did not need to change — the bundle already carries libX11, libglib, libdbus, ICU, freetype
and fontconfig from the build machine, and apt brings libxcb, libdrm and libxkbcommon in as
libgl1/libegl1's own dependencies — and the 28b `ldd` sweep over the real bundle confirms nothing is
uncovered.

### Test wording changes, and why

No test was deleted, skipped, xfailed or made to accept a failure it used to reject. Four assertions
were reworded because the two bindings do not expose the same attribute:

1. `tests/test_finished_run_layout_30b.py` — `QWIDGETSIZE_MAX` is not exported by PySide6's `QtWidgets`,
   so the literal `(1 << 24) - 1` is used. Same value, same test.
2. `tests/test_broken_rows_05b.py` — the cursor check compares `cursor().shape()` with the enum instead
   of comparing the `QCursor` object with the enum, which only worked because PyQt6 overloads `==` for
   it. Same thing is being asserted.
3. `tests/test_three_pane_run_screen_25a.py` — the pause assertion became `phases and set(phases) ==
   {"Running"}`, and the end-state check is back in a weaker form: the header must read `Running` or
   `Finished`, never `Paused`. Exactly `Running`, as before, is a race: the worker is already free and
   in 8 local runs read `Finished` once.
4. `tests/test_windows_ci_fixes.py` and `tests/test_headless_session.py` — docstring and one variable
   name (`PYQT_WEBENGINE_IMPORT_ERROR` → `WEBENGINE_IMPORT_ERROR`) updated for the new binding; no
   assertion changed.

Tests added, all proving something rather than relaxing it: `tests/test_signal_threads_32a.py`
(thread of every slot, a control that sees a real worker delivery, the values sent, and what PySide6
does to bad values), `tests/test_unhandled_exception_log_32a.py`, the child-check test in
`tests/test_windows_ci_fixes.py`, the excludes-vs-WebEngine-import check (which now fails rather than
skips when the import fails) and the licence classifier, closure, notice and Inno Setup tests in 27a,
the "every artifact carries the notices" test in 28b, and a guard in `tests/test_architecture_rules.py`
that fails if the forbidden-import patterns match nothing.

### Licence

PySide6's wheels declare `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`, so the bundle carries the
LGPL-3.0 and GPL-3.0 texts, and a notice saying which versions are shipped, where the source is, and
that Qt's libraries are separate, unmodified files that can be replaced. The notice describes the
build and makes no claim of compliance. Nobody has checked compliance, and replacing Qt has not been
tried. `strip=False` and `upx=False` in `ate.spec` are what "unmodified" rests on; no test compares
the bundled libraries with the wheel's.

Source locations: `https://code.qt.io/cgit/pyside/pyside-setup.git/` is read from the wheels'
`Project-URL: Repository`. The wheels name no Qt source, so `https://code.qt.io/cgit/qt/qt5.git/` is a
constant in `notices.py`, chosen from the same host. On 2026-11-01 both URLs answered HTTP 200 and
`git ls-remote` found tag `v6.11.3` on `pyside/pyside-setup`, `qt/qt5`, `qt/qtbase` and `qt/qtwebengine`. The
earlier GitHub and `qt.io/download-open-source` links were not from any metadata and are gone.

`THIRD_PARTY_NOTICES.txt` is generated from the installed wheels at build time, so no copy is checked
in. It now walks the whole requirement closure, honouring markers and extras, so `markupsafe` and
`typing-extensions` are listed. It reads classifiers too: the earlier claim that jinja2's wheel states
no licence was wrong, because it has `License :: OSI Approved :: BSD License`. A term counts only if
the whole term is a known licence name, which goes further than word boundaries: `Not MIT` has the
word and means the opposite. Generated from this venv: jinja2, markupsafe, pyserial, pyvisa, pyvisa-py,
sqlalchemy and typing-extensions are permissive; the three Qt wheels are copyleft (LGPL).

**Reported for you to decide:** lupa's wheel declares only `MIT style`, with no classifier, so it is
listed as "check upstream". The Lupa section of its `LICENSE.txt` is the MIT text, and the file points
to lua.org for Lua. The earlier note that the Lua source is inside lupa's wheel was false, since the
wheel has only compiled modules, and is corrected. Separately, not done here: MIT and BSD ask for their copyright notice
to travel with copies, and this inventory names licences rather than reproducing them. Whether the
dependencies' licence files reach the bundle has not been checked.

The repo still has no `LICENSE` file even though `pyproject.toml` says MIT. Per the packet, none was
added here — that is your call, and it is worth deciding before the next release, because an artifact
that carries third-party LGPL text and no licence for ATE's own source is a strange thing to ship.

### Not proven here

- CI on Linux and Windows, and a manual `release.yml` run with all 5 jobs: never executed from this
  machine. The workflow files were edited to match PySide6, and the same Python that CI runs was run
  locally where it could be, but the Actions result is unknown.
- Windows paths in `ate_entry.py` and `verify_bundle()` (`PySide6/QtWebEngineProcess.exe` in
  `_internal/PySide6`, the Qt DLLs next to it): a reviewer checked them against the real Windows
  wheels and they match, so they are unchanged. They have still never run: no Windows build,
  machine or emulator was available.
- The Windows installer checks in `release.yml` are untested for the same reason.
