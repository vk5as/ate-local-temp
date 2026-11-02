# ADR 0001: the Qt binding is PySide6, not PyQt6

**Status:** accepted, 2026-11-01 (packet 32a)

## Context

ATE's GUI and its Test Report renderer are Qt, reached through a Python binding. The Windows
installer, the `.deb` and the tarball carry that binding and the Qt libraries it links against. The
wheel carries neither: it declares a dependency, and pip fetches the binding from PyPI.

PyQt6's wheels are licensed GPL-3.0 (or commercially, from Riverbank Computing). ATE declares MIT
in `pyproject.toml`. Shipping a GPL binding under an MIT label is a licence gap, and it was found
after `v0.1.0-rc1` and `v0.1.0-rc2`.

The options were:

* **Keep PyQt6 and relicense ATE GPL.** Rejected: a Station's own Test Schedules, config and
  database would inherit the GPL, and the user's deployment is not GPL.
* **Buy a PyQt commercial licence.** Rejected: ATE is redistributed to Stations that install it
  themselves, so the licence would have to cover every copy.
* **Switch to PySide6**, the official Qt for Python binding. Its wheels declare
  `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`. The LGPL is the option that does not ask ATE
  itself to change licence.

## Decision

Every Qt import in ATE is `PySide6`, and the dependency is a single package:

```toml
"PySide6-Addons>=6.11,<6.12"
```

`PySide6-Addons` is the wheel that carries `QtWebEngine`, and it pins `PySide6-Essentials` and
`shiboken6` to the *exact* same version, so one Qt serves the whole app. The band keeps ATE on
Qt 6.11, the release every test and release run so far used: a failure after the switch is the
binding's doing, not a new Qt's. The bundle stays one-directory, with Qt's libraries as separate
files copied unmodified from the wheels, so they can be replaced with another build of the same Qt
version. That replacement has not been tried.

`THIRD_PARTY_NOTICES.txt`, `NOTICE-Qt-PySide6.txt` and the LGPL-3.0 and GPL-3.0 texts are generated
by `packaging/licence/notices.py` at build time and put in the bundle; `verify_bundle()` refuses to
ship a bundle that does not carry them. They say which versions are shipped, where the source is and
what the build does with Qt. This record and those files state what the build does. They are not a
finding that the LGPL's terms are met: nobody has reviewed that, and the user decides whether a
release needs a legal review.

## Consequences

What the switch found, checked rather than assumed (packet 32a's rework notes carry the detail):

* An exception escaping `QThread.run` or a slot no longer aborts the process. PyQt6 aborted with
  SIGABRT (exit 134). PySide6 calls `sys.excepthook` and carries on. The guard in `RunEngine.run` is
  therefore still needed, and for a different reason: without it the run row would stay open and the
  screen would wait for a `FINISHED` that never arrives. For slots, a hook installed at launch
  (`ate.exception_log`) writes the traceback to the application log and shows a line in the main
  window's status bar. It does not stop the run and it passes the exception on to the previous hook.
* A child-process test can no longer rely on its exit code to see a slot exception. It has to read
  stderr as well (`assert_no_swallowed_exception`).
* `findChildren` takes one type, not a tuple of them.
* `QWidget.cursor()` no longer compares equal to a `Qt.CursorShape` enum; read `.shape()`.
* `QWIDGETSIZE_MAX` is not exposed as a name.
* Creating a second `QCoreApplication` while a `QApplication` exists raises `RuntimeError` instead
  of segfaulting at shutdown. The suite's single session-scoped `QApplication` is still the right
  shape; it now prevents an error rather than a crash.
* `Signal(int)` is a C `int`. With a slot connected, `emit(2**31)` returns normally and the slot gets
  -2147483648, with only a `RuntimeWarning`; `OverflowError` is raised only when nothing is connected.
  `Signal(str)` turns a non-string into `''`, with one line on stderr, where PyQt6 raised `TypeError`.
  A slot therefore cannot see a bad value. The engine's and session's signals carry node sequence,
  Attempt and input numbers bounded by the schedule, and strings that are always `str`, so none needs
  `object` for this; `tests/test_signal_threads_32a.py` pins them on the sending side. Anything that
  can pass 2**31 - 1 has to be declared `object`.
* Slots connected to worker signals still run on the main thread: the same test emits all eight on
  the worker and records where each shape of slot ran.

The licence notices are now part of the definition of a valid bundle, and every distributed
artifact has to carry them.
