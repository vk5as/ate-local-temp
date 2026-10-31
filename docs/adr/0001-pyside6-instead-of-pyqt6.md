# ADR 0001: the Qt binding is PySide6, not PyQt6

**Status:** accepted, 2026-11-01 (packet 32a)

## Context

ATE's GUI and its Test Report renderer are Qt, reached through a Python binding. Every artifact
that ships ATE — the Windows installer, the `.deb`, the tarball, the wheel — ships that binding and
the Qt libraries it links against.

PyQt6's wheels are licensed GPL-3.0 (or commercially, from Riverbank Computing). ATE declares MIT
in `pyproject.toml`. Shipping a GPL binding under an MIT label is a licence gap, and it was found
after `v0.1.0-rc1` and `v0.1.0-rc2`.

The options were:

* **Keep PyQt6 and relicense ATE GPL.** Rejected: a Station's own Test Schedules, config and
  database would inherit the GPL, and the user's deployment is not GPL.
* **Buy a PyQt commercial licence.** Rejected: ATE is redistributed to Stations that install it
  themselves, so the licence would have to cover every copy.
* **Switch to PySide6**, the official Qt for Python binding, licensed LGPL-3.0 (also offered as
  GPL-2.0 or GPL-3.0 by the wheel metadata). LGPL allows linking with MIT code as long as the
  LGPL-covered libraries stay available in modifiable form.

## Decision

Every Qt import in ATE is `PySide6`, and the dependency is a single package:

```toml
"PySide6-Addons>=6.11,<6.12"
```

`PySide6-Addons` is the wheel that carries `QtWebEngine`, and it pins `PySide6-Essentials` and
`shiboken6` to the *exact* same version, so one Qt serves the whole app. The band keeps ATE on
Qt 6.11, the release every test and release run so far used: a failure after the switch is the
binding's doing, not a new Qt's. The bundle stays one-directory with Qt as separate, unmodified
shared libraries, which is what the LGPL's "link-time" form expects and what lets a Station swap
Qt for its own build.

`THIRD_PARTY_NOTICES.txt`, `NOTICE-Qt-PySide6.txt` and the LGPL-3.0 and GPL-3.0 texts are generated
by `packaging/licence/notices.py` at build time and put in the bundle; `verify_bundle()` refuses to
ship a bundle that does not carry them.

## Consequences

What the switch found, checked rather than assumed (packet 32a's rework notes carry the detail):

* An exception escaping `QThread.run` or a slot no longer aborts the process. PyQt6 aborted with
  SIGABRT (exit 134). PySide6 prints the traceback and the thread just ends. The guard in
  `RunEngine.run` is therefore still needed, and for a different reason: without it the run row
  would stay open and the screen would wait for a `FINISHED` that never arrives.
* `findChildren` takes one type, not a tuple of them.
* `QWidget.cursor()` no longer compares equal to a `Qt.CursorShape` enum; read `.shape()`.
* `QWIDGETSIZE_MAX` is not exposed as a name.
* Creating a second `QCoreApplication` while a `QApplication` exists raises `RuntimeError` instead
  of segfaulting at shutdown. The suite's single session-scoped `QApplication` is still the right
  shape; it now prevents an error rather than a crash.
* `Signal(int)` raises `OverflowError` for values above 32 bits. The engine's signals carry node
  sequence numbers, attempt numbers and short strings, all well inside the band; the ones that can
  be `None` are typed `object`.
* Slots connected to worker signals still run on the main thread, proved by
  `tests/test_signal_threads_32a.py`.

The licence notices are now part of the definition of a valid bundle, and every distributed
artifact has to carry them.
