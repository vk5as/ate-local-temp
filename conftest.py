"""Make the src-layout importable when running pytest without an editable install.

Under CI the package is installed with ``pip install -e .`` (see
``.github/workflows/ci.yml``), which puts ``src/`` on ``sys.path`` itself. This
file only exists so tests can run straight from a checkout; it is harmless once
the package is installed, where ``import ate`` resolves through the editable
install regardless of this path tweak.

It also provides one offscreen QApplication for the whole session, so the QThread
run engine and headless session (ticket 02) can be driven without a display.
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


# Ticket 02 drives QThread workers headless (QT_QPA_PLATFORM=offscreen). One shared
# application for the session avoids creating a second QApplication mid-session.
import pytest  # noqa: E402

from PyQt6.QtCore import QCoreApplication  # noqa: E402


# The single offscreen application shared for the whole session. Ticket 02 drives QThread
# workers headless, and they can only be polled through one QApplication's event loop; a
# second instance would raise "a QApplication instance already exists". Created lazily on
# first request so importing conftest needs no display.
_app = None  # type: QCoreApplication | None


@pytest.fixture(scope="session", autouse=True)
def _qt_app() -> None:
    """Guarantee one offscreen application exists for the session's QThread tests."""
    global _app
    if _app is None:
        _app = QCoreApplication(sys.argv)
    yield


@pytest.fixture(scope="session")
def app() -> QCoreApplication:
    """The shared offscreen application, exposed to tests as ``app``.

    Tests request this (a pytest-qt-style name) rather than the internal ``_qt_app``; it
    returns the same instance so every test drives the worker through one event loop.
    """
    assert _app is not None
    return _app
