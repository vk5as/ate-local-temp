"""Make the src-layout importable when running pytest without an editable install.

Under CI the package is installed with ``pip install -e .`` (see
``.github/workflows/ci.yml``), which puts ``src/`` on ``sys.path`` itself. This
file only exists so tests can run straight from a checkout; it is harmless once
the package is installed, where ``import ate`` resolves through the editable
install regardless of this path tweak.

It also provides one offscreen application for the whole session, so the QThread
run engine and headless session (ticket 02) and the widget Run screen (ticket 03) can be
driven without a display. This Qt 6.11 / PyQt6 build segfaults at interpreter shutdown when a
live ``QCoreApplication`` and a live ``QApplication`` are both present; sharing one ``QApplication``
(for which ``QCoreApplication`` is also true, so queued cross-thread signals still deliver) means
that never happens — only a lone application is ever finalized.
"""

import os
import sys
from pathlib import Path

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
_ROOT = Path(_SRC).parent  # the repository root; needed so ``tests`` is importable as a package
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# Ticket 02 drives QThread workers headless (QT_QPA_PLATFORM=offscreen). One shared application
# for the session avoids creating a second QApplication mid-session. This PyQt6 build's
# ``QApplication`` binding does not accept a ``platform=`` kwarg, so we construct it with an empty
# argument list and rely on that env var instead.
import pytest  # noqa: E402

from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402


def _destroy_widget_tree(app: QApplication) -> None:
    """Recursively delete every descendant widget while Python is still alive.

    A quit ``QApplication`` leaves its QObject descendants orphaned; if any of those are still
    referenced by Python they get finalized at interpreter shutdown and, in this Qt 6.11 build,
    that finalization segfaults. Deleting them here (before the app quits) means nothing Qt is
    left for ``Py_Exit`` to touch."""
    def _drop(obj: object) -> None:
        for child in list(getattr(obj, "children", lambda: [])()):
            if isinstance(child, QWidget):
                child.deleteLater()
            else:
                _drop(child)
        obj.deleteLater()

    for widget in list(app.children()):
        if isinstance(widget, QWidget):
            widget.deleteLater()
    app.processEvents()


@pytest.fixture(scope="session", autouse=True)
def qapp() -> QApplication:
    """The single offscreen application shared by every test.

    Autouse so tests that do not declare it still run with one application present (ticket 02's
    signal delivery needs an application to pump queued cross-thread events). The ``offscreen``
    platform is supplied by the ``QT_QPA_PLATFORM=offscreen`` env var; this PyQt6 build's
    ``QApplication`` binding does not accept a ``platform=`` kwarg, so we construct it with an empty
    argument list and rely on that env var.

    The widget tree is destroyed before quitting (see :func:`_destroy_widget_tree`) so the
    interpreter shutdown has nothing Qt to finalize — this Qt 6.11 build segfaults when a live
    ``QApplication`` is finalized alongside any ``QCoreApplication``."""
    app = QApplication([])
    yield app
    _destroy_widget_tree(app)
    app.processEvents()
    app.quit()


@pytest.fixture(scope="session")
def app(qapp: QApplication) -> QApplication:
    """The shared offscreen application, exposed to tests as ``app``.

    Ticket 02's QThread-driven tests request this name; it returns the same instance so every test
    drives the worker through one event loop."""
    return qapp
