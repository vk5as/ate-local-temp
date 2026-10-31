"""Every modal ATE opens, and what it answers when nobody is there to answer it.

A modal `exec()` blocks until someone clicks. On a CI runner, in a container, or in any process
started with `QT_QPA_PLATFORM=offscreen` there is no window server, so no one ever clicks: the
second real CI run hung for 30 minutes on the run-start mismatch confirmation, and the first hung
on the unsandboxed-PDF question. A dialog that cannot be answered has to answer itself, and this
module is the only place that decides both *when* a dialog has nobody to answer it and *how* it is
armed.

Two ways in:

* `QT_QPA_PLATFORM=offscreen` or `minimal` — no display at all. CI and every headless test run.
* `ATE_AUTO_DISMISS_DIALOG` — the explicit seam for subprocess tests (packet 30c). `""`, `"0"`,
  `"false"`, `"no"` and `"off"` mean unset.

`dismiss_if_headless(dialog, answer)` arms the dialog to close itself as soon as its own event loop
runs, so `exec()` returns. With no `answer`, a `QMessageBox` closes through `accept()`, which clicks
no button: `clickedButton()` is None and every prompt therefore takes its *negative* branch — the
missing-config prompt aborts, the close confirmation keeps the run going, the PDF question is
answered "Skip PDF". A dialog with a decision of its own (the mismatch confirmation) is handed an
explicit answer function, and the safe one is always the one that writes nothing.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from PyQt6.QtCore import QTimer

#: Qt platforms with no window server: nothing is ever drawn, so a dialog on them can never be
#: answered. `offscreen` is what CI and every headless test run uses.
_HEADLESS_PLATFORMS = ("offscreen", "minimal")


def has_no_display_to_answer() -> bool:
    """True when Qt has no display to show a box on, so `exec()` would wait for no one."""
    return os.environ.get("QT_QPA_PLATFORM", "").strip().lower() in _HEADLESS_PLATFORMS


def auto_dismiss_requested() -> bool:
    """True when `ATE_AUTO_DISMISS_DIALOG` is set to something that means "yes"."""
    value = os.environ.get("ATE_AUTO_DISMISS_DIALOG", "").strip().lower()
    return bool(value) and value not in ("0", "false", "no", "off")


def dismiss_if_headless(dialog, answer: Optional[Callable] = None) -> bool:
    """Arm `dialog` to close itself instead of waiting for a click, and say whether it was armed.

    `answer(dialog)` is what closes it: without one the box is `accept()`ed, which clicks no button
    and so answers every prompt with its negative branch. The timer runs inside the dialog's own
    `exec()` loop, so it fires the moment that loop starts."""
    if not (auto_dismiss_requested() or has_no_display_to_answer()):
        return False
    if answer is None:
        QTimer.singleShot(0, dialog.accept)
    else:
        QTimer.singleShot(0, lambda: answer(dialog))
    return True
