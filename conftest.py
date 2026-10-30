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
import weakref
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

from PyQt6.QtCore import QObject, QThread  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402


def _stop_workers(app: QApplication) -> None:
    """Stop every live QThread worker before the tree is torn down.

    Ticket 02's RunEngine runs on a QThread and emits cross-thread signals onto this shared
    application's event loop. If a worker is still alive when one test ends, its queued signals
    can fire into the next test's construction and leave widgets in a stale visibility state —
    that is what makes ``test_run_view_is_hidden_until_a_run_starts`` flake across a full run.
    Stopping workers here guarantees each test starts with no lingering worker feeding events."""
    def _halt(obj: object) -> None:
        try:
            children = list(getattr(obj, "children", lambda: [])())
        except RuntimeError:  # deleted on the C++ side mid-walk (e.g. a gesture object); nothing to stop
            return
        for child in children:
            if isinstance(child, QThread) and child.isRunning():
                child.stop()
            _halt(child)

    _halt(app)


def _destroy_widget_tree(app: QApplication) -> None:
    """Recursively delete every descendant widget while Python is still alive.

    A quit ``QApplication`` leaves its QObject descendants orphaned; if any of those are still
    referenced by Python they get finalized at interpreter shutdown and, in this Qt 6.11 build,
    that finalization segfaults. Deleting them here (before the app quits) means nothing Qt is
    left for ``Py_Exit`` to touch."""
    def _drop(obj: object) -> None:
        try:
            children = list(getattr(obj, "children", lambda: [])())
        except RuntimeError:  # already deleted on the C++ side
            return
        for child in children:
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
    _stop_workers(app)
    _destroy_widget_tree(app)
    app.processEvents()
    app.quit()


@pytest.fixture(scope="session")
def app(qapp: QApplication) -> QApplication:
    """The shared offscreen application, exposed to tests as ``app``.

    Ticket 02's QThread-driven tests request this name; it returns the same instance so every test
    drives the worker through one event loop."""
    return qapp


@pytest.fixture(autouse=True)
def _stop_pdf_jobs_after_test() -> None:
    """End every Test Report PDF helper before the next test starts (packet 25b review).

    The helper is a ``QProcess`` parented to the session that started it. A test that finishes with
    the render still in flight drops its session, Qt destroys the process underneath it, and the
    job's ``finished`` callback then runs against a session whose Qt side is gone: PyQt aborts the
    interpreter. Stopping the jobs at the end of each test keeps a helper from outliving its session.
    """
    from ate.report.pdf_job import stop_pdf_jobs

    yield
    stop_pdf_jobs()


@pytest.fixture(autouse=True)
def _clean_qt_state(qapp: QApplication) -> None:
    """Give every test a clean Qt slate, regardless of which module runs.

    Ticket 03's StagedRunScreen keeps a live QThread worker and a one-second timer for its whole
    lifetime; without per-test cleanup those linger across tests and keep firing cross-thread
    signals onto the shared application's event loop, intermittently corrupting the visibility
    state of the next test's widgets (the ``run_view`` flake). Stopping workers and dropping every
    widget tree here — before the next test constructs anything — removes that leak. This runs for
    all tests too, so a QThread-driven test never leaves a worker feeding events either."""
    _stop_workers(qapp)
    _destroy_widget_tree(qapp)
    qapp.processEvents()


@pytest.fixture(scope="session", autouse=True)
def _platform_data_dir_under_basetemp(tmp_path_factory):
    """Keep the ``<data>`` platform data directory inside pytest's basetemp for the session.

    The config format's defaults are written as ``<data>/...`` and the reader resolves
    ``<data>`` to the real platform data directory (``/var/lib/ate`` on Linux,
    ``%PROGRAMDATA%\\ATE`` on Windows) — packet 30c. No test may create files under
    ``/var/lib`` or ``C:\\ProgramData``. Session scope so module-scoped fixtures that open
    a session are covered too; tests wanting a specific directory patch it again, and
    subprocess tests are unaffected because a real Station's config carries the paths
    setup wrote resolved."""
    import ate.config

    data_dir = tmp_path_factory.mktemp("platform-data")
    original = ate.config._platform_data_dir
    ate.config._platform_data_dir = lambda: data_dir
    yield
    ate.config._platform_data_dir = original


@pytest.fixture(autouse=True)
def _fake_pdf_helper(request, monkeypatch):
    """Every finished run with a config starts the PDF helper; outside the one real end-to-end test,
    run a trivial stand-in process instead of QWebEngine. Tests wanting other helper behaviour patch
    ``ate.report.pdf_job.pdf_helper_command`` again themselves."""
    if request.node.name.endswith("test_run_end_writes_real_pdf_report"):
        return
    from tests._fixtures import WRITES_PDF, fake_pdf_helper

    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", fake_pdf_helper(WRITES_PDF))


_started_engines: "weakref.WeakSet" = weakref.WeakSet()


@pytest.fixture(autouse=True)
def _stop_running_engines(monkeypatch):
    """After each test, abort and join any RunEngine worker still running.

    RunEngines have no Qt parent, so :func:`_stop_workers` never sees them. A test that ends with
    its run unfinished drops its session; the garbage collector then deletes the session's Qt
    children (its OperatorInput) under the live worker, which aborts the process in a later test.
    Every started engine is remembered weakly so this check stays cheap."""
    from ate.engine.run_engine import RunEngine

    original_start = RunEngine.start

    def start(self, *args):
        _started_engines.add(self)
        return original_start(self, *args)

    monkeypatch.setattr(RunEngine, "start", start)
    yield
    for engine in list(_started_engines):
        try:
            running = engine.isRunning()
        except RuntimeError:  # its C++ side is already gone
            continue
        if not running:
            continue
        try:
            if engine.pause_pending():
                # A worker parked in a Stop-on-Fail pause only wakes on a choice, so an abort
                # requested from outside would sit here for the whole join timeout.
                engine.choose("ABORT")
            engine.request_abort()
            joined = engine.wait(5000)
        except RuntimeError:  # its C++ side is already gone
            continue
        if not joined:
            raise RuntimeError("a RunEngine worker outlived its test: abort did not stop it")
    _started_engines.clear()
