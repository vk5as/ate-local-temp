"""Make the src-layout importable when running pytest without an editable install.

Under CI the package is installed with ``pip install -e .`` (see
``.github/workflows/ci.yml``), which puts ``src/`` on ``sys.path`` itself. This
file only exists so tests can run straight from a checkout; it is harmless once
the package is installed, where ``import ate`` resolves through the editable
install regardless of this path tweak.

It also provides one offscreen application for the whole session, so the QThread
run engine and headless session (ticket 02) and the widget Run screen (ticket 03) can be
driven without a display. This Qt 6.11 / PySide6 build segfaults at interpreter shutdown when a
live ``QCoreApplication`` and a live ``QApplication`` are both present; sharing one ``QApplication``
(for which ``QCoreApplication`` is also true, so queued cross-thread signals still deliver) means
that never happens — only a lone application is ever finalized.
"""

import os
import subprocess
import sys
import warnings
import weakref
from pathlib import Path

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
_ROOT = Path(_SRC).parent  # the repository root; needed so ``tests`` is importable as a package
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# Qt's offscreen platform on Windows looks for fonts in Qt's own (empty) library directory rather
# than asking Windows, so every character draws as the missing-glyph box and every width is wrong.
# The real `windows` platform an Operator uses has no such problem. Set before any Qt import, and
# inherited by every subprocess a test starts.
if os.name == "nt" and os.environ.get("QT_QPA_PLATFORM") == "offscreen":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

# Ticket 02 drives QThread workers headless (QT_QPA_PLATFORM=offscreen). One shared application
# for the session avoids creating a second QApplication mid-session. This PySide6 build's
# ``QApplication`` binding does not accept a ``platform=`` kwarg, so we construct it with an empty
# argument list and rely on that env var instead.
import pytest  # noqa: E402

from PySide6.QtCore import QObject, QThread, qInstallMessageHandler  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from ate.engine.run_engine import _live_engines, stop_live_engines  # noqa: E402
from ate.log import AppLog  # noqa: E402
from ate.run_session.headless import HeadlessRunSession  # noqa: E402
from ate.storage.store import RunStore  # noqa: E402
from tests import _fixtures  # noqa: E402


def pytest_configure(config) -> None:
    _fixtures.open_fatal_stream()


# -- everything a test opens is released before its files are deleted ------------------------------

#: Sessions, stores and application logs in the order they were opened. Tests drop these freely: on Linux an
#: unreferenced one does no harm, and on Windows it keeps its file undeletable (WinError 32).
_opened: list = []


def _remember_opened(cls: type) -> None:
    original = cls.__init__

    def __init__(self, *args, **kwargs):
        original(self, *args, **kwargs)
        _opened.append(self)

    cls.__init__ = __init__


_remember_opened(HeadlessRunSession)
_remember_opened(RunStore)
_remember_opened(AppLog)
_fixtures.watch_store_writes()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """A test that failed with a worker still running reports it, whatever its own wait helper said.

    Taken as the call ends, before teardown aborts the worker: a wait for FINISHED that expired is
    described as it stood (packet 32b)."""
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.failed:
        return
    try:  # the failure being reported is the test's own: nothing here may replace it
        if any(_still_running(engine) for engine in list(_live_engines)):
            report.sections.append(("RunEngine worker still running", _fixtures.describe_stuck_runs(_opened)))
    except Exception as exc:  # noqa: BLE001
        report.sections.append(("RunEngine worker still running", f"could not describe the stuck run: {exc!r}"))


def _still_running(engine) -> bool:
    try:
        return engine.isRunning()
    except RuntimeError:  # its C++ side is already gone
        return False


def _release_since(opened_mark: int, scratch_mark: int) -> None:
    """End every run, close what was opened after the marks, then delete the scratch directories
    made after them. The order is the point: the deletion is strict, so it proves the closes."""
    stuck = stop_live_engines()
    if stuck:
        raise RuntimeError(f"{len(stuck)} RunEngine worker(s) did not stop\n{_fixtures.describe_stuck_runs(_opened)}")
    for item in reversed(_opened[opened_mark:]):
        item.close()
    del _opened[opened_mark:]
    directories = _fixtures.scratch_directories[scratch_mark:]
    del _fixtures.scratch_directories[scratch_mark:]
    for path in directories:
        _fixtures.remove_tree(path)


@pytest.fixture(scope="session", autouse=True)
def _release_files_session(tmp_path_factory):
    """Point scratch directories at pytest's basetemp and release what session fixtures opened."""
    _fixtures.scratch_root = tmp_path_factory.getbasetemp()
    marks = len(_opened), len(_fixtures.scratch_directories)
    yield
    _release_since(*marks)


@pytest.fixture(scope="module", autouse=True)
def _release_files_module(_release_files_session):
    """Release what a module-scoped fixture opened, once the tests sharing it are done."""
    marks = len(_opened), len(_fixtures.scratch_directories)
    yield
    _release_since(*marks)


@pytest.fixture(autouse=True)
def _release_files(_release_files_module, tmp_path_factory):
    """Release what this test opened, and fail it if a file under pytest's temp directory stays open.

    Linux will happily delete an open file, so without this check a handle that only Windows would
    refuse to give up is invisible until CI runs there. Files of stores still open for an enclosing
    scope are that scope's, and are excluded."""
    root = tmp_path_factory.getbasetemp()
    before = _fixtures.open_files_under(root)
    marks = len(_opened), len(_fixtures.scratch_directories)
    yield
    outer = {
        str(item.path) + suffix
        for item in _opened[: marks[0]]
        if isinstance(item, RunStore)
        for suffix in ("", "-wal", "-shm")
    }
    _release_since(*marks)
    leaked = _fixtures.open_files_under(root) - before - outer
    assert not leaked, f"still open after the test closed everything it opened: {sorted(leaked)}"


@pytest.fixture
def deny_writes():
    """Make a directory unwritable for this process, on either OS, until the test ends."""
    undo = []

    def deny(path: Path) -> None:
        if os.name == "nt":
            subprocess.run(_fixtures.deny_writes_command(path), check=True, capture_output=True)
            undo.append(lambda: subprocess.run(_fixtures.allow_writes_command(path), check=True, capture_output=True))
            return
        if not _fixtures.chmod_can_deny_access():
            pytest.skip("chmod 0o555 denies nothing to root")
        path.chmod(0o555)
        undo.append(lambda: path.chmod(0o755))

    yield deny
    for step in undo:
        step()


@pytest.fixture(autouse=True)
def _report_qt_fatal():
    """Keep Qt's fatal message from dying inside pytest-qt's capture (see report_qt_fatal)."""
    previous = qInstallMessageHandler(None)
    qInstallMessageHandler(_fixtures.report_qt_fatal(previous))
    yield
    qInstallMessageHandler(previous)


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
    platform is supplied by the ``QT_QPA_PLATFORM=offscreen`` env var; this PySide6 build's
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
def _stop_pdf_jobs_after_test(_release_files) -> None:
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
    #: The machine's own Station directories, taken from the product's own reading of the
    #: platform before this fixture pins the data directory: `_platform_data_dir` and
    #: `_platform_config_dir` know the documented fallback for a missing `PROGRAMDATA`
    #: (`config.PROGRAMDATA_FALLBACK`), so the guard sees `C:\ProgramData\ATE` on a Windows
    #: runner that has no `PROGRAMDATA` at all (packet 35b, CI 41407051552).
    _REAL_STATION_DIRS[:] = sorted(
        {d for d in (ate.config._platform_data_dir(), ate.config._platform_config_dir()) if d.is_absolute()}
    )
    original = ate.config._platform_data_dir
    ate.config._platform_data_dir = lambda: data_dir
    # A test that starts ATE in a fresh interpreter inherits this, so its application log lands
    # here too and not in the runner's real `C:\ProgramData\ATE` (packet 35b, CI 41445252353).
    previous_env = os.environ.get("ATE_DATA_DIR")
    os.environ["ATE_DATA_DIR"] = str(data_dir)
    yield
    if previous_env is None:
        os.environ.pop("ATE_DATA_DIR", None)
    else:
        os.environ["ATE_DATA_DIR"] = previous_env
    ate.config._platform_data_dir = original


#: Where a real Station keeps its files, filled by the fixture above: `/etc/ate` and
#: `/var/lib/ate` on Linux, `%PROGRAMDATA%\ATE` on Windows, with `C:\ProgramData\ATE` when
#: `PROGRAMDATA` is unset.
_REAL_STATION_DIRS: list[Path] = []

#: Paths under those directories that the suite itself made, so a later test that changes one of
#: them is still caught, while a real Station's own files are never flagged.
_suite_station_files: set[str] = set()
_unreadable_reported = False


def _station_snapshot() -> tuple[dict[str, float], list[str]]:
    """``{path: mtime}`` for every entry under the machine's real Station directories that can be
    read, plus the directories pytest could not read.

    `rglob` walks past a directory it cannot open without saying so, so the walk is written here:
    what cannot be read is not snapshotted, and is named as unreadable instead of reported as
    empty. The guard never claims a test left nothing behind in a place it was never able to look.
    """
    snapshot: dict[str, float] = {}
    unreadable: list[str] = []
    for directory in _REAL_STATION_DIRS:
        if not directory.exists():
            continue  # nothing there yet: a test that makes one is caught as a new entry
        try:
            snapshot[str(directory)] = directory.stat().st_mtime
        except OSError as exc:
            unreadable.append(f"{directory}: {exc}")
            continue
        stack = [directory]
        while stack:
            current = stack.pop()
            try:
                children = list(current.iterdir())
            except OSError as exc:
                unreadable.append(f"{current}: {exc}")
                continue
            for child in children:
                try:
                    snapshot[str(child)] = child.stat().st_mtime
                except OSError:
                    continue  # vanished between the walk and the stat: nothing to compare
                if child.is_dir():
                    stack.append(child)
    return snapshot, unreadable


@pytest.fixture(autouse=True)
def _no_test_writes_to_a_real_station():
    """Fail a test that leaves anything in the machine's own Station directories.

    conftest pins the platform data directory and a test that needs a particular config pins
    ``ATE_DEFAULT_CONFIG``, but a path those pins do not reach still lands in ``/etc/ate``,
    ``/var/lib/ate`` or ``%PROGRAMDATA%\\ATE``: a bare ``ate setup`` on the Windows runner created
    ``C:\\ProgramData\\ATE\\config.json`` (packet 35b, CI 41407051552).

    On a machine that has a real Station, only entries that were not there when the test started
    are flagged: the Station's own ATE writes into its data directory while the suite runs, and an
    mtime moving on a file the suite never made is not a test's doing. A file the suite made is a
    suite's file, so changing it in a later test is flagged too. What could not be read is said
    once, as a warning, rather than passed off as an empty directory."""
    global _unreadable_reported
    before, unreadable = _station_snapshot()
    yield
    after, unreadable = _station_snapshot()
    if unreadable and not _unreadable_reported:
        _unreadable_reported = True
        warnings.warn(f"the Station-directory guard cannot read: {', '.join(unreadable)}", stacklevel=1)
    new = sorted(path for path in after if path not in before)
    changed = sorted(path for path, when in after.items() if before.get(path) != when and path in _suite_station_files)
    _suite_station_files.update(new)
    if new or changed:
        raise AssertionError(f"a test wrote into the machine's own Station: {new + changed}")


@pytest.fixture(scope="session", autouse=True)
def _runner_privilege_is_not_a_test_variable() -> None:
    """Pin "is ATE elevated?" to False for the whole session, on every runner.

    Session scope on purpose: a module-scoped fixture that starts a Test Run (the example
    harness) runs before any per-test fixture, so a function-scoped pin would arrive too late.

    A GitHub Windows runner is an administrator and a container runs as root, so the runner's own
    privilege decides which PDF path a test takes (packet 30d): a root or admin process asks the
    Operator before every Test Report PDF, and `QMessageBox.exec()` blocks forever offscreen. Every
    test therefore starts as an ordinary user; one that wants to be privileged sets
    ``ATE_ELEVATED=1`` (``ate.report.pdf.ELEVATION_ENV`), which also reaches a spawned `ate _pdf`
    child through the inherited environment."""
    from ate.report.pdf import ELEVATION_ENV

    previous = os.environ.get(ELEVATION_ENV)
    os.environ[ELEVATION_ENV] = "0"
    yield
    if previous is None:
        os.environ.pop(ELEVATION_ENV, None)
    else:
        os.environ[ELEVATION_ENV] = previous


@pytest.fixture(autouse=True)
def _fake_pdf_helper(request, monkeypatch):
    """Every finished run with a config starts the PDF helper; outside the real end-to-end tests, run
    a trivial stand-in process instead of QWebEngine. Tests wanting other helper behaviour patch
    ``ate.report.pdf_job.pdf_helper_command`` again themselves.

    The stand-in writes a 13-byte stub, which is fine for the signal plumbing but not for a test that
    claims the report was rendered: packet 27a's `ate _selftest --run` and 22a's real-report test use
    the real helper, because "the PDF has text in it" is only provable against a PDF that was printed.
    Packet 30d's privileged `--run` is on that list too: it has to render for real to prove the
    unsandboxed choice reaches the child, and so does the ordinary-user case, which has to show that
    the flag changes nothing for someone who keeps the sandbox.
    """
    if request.node.name in (
        "test_run_end_writes_real_pdf_report",
        "test_selftest_run_executes_a_schedule_and_writes_both_reports",
        "test_selftest_run_as_root_with_the_flag_before_the_subcommand_renders_a_pdf",
        "test_an_ordinary_user_with_the_flag_is_told_it_has_no_effect",
    ):
        return
    from tests._fixtures import WRITES_PDF, fake_pdf_helper

    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", fake_pdf_helper(WRITES_PDF))


_started_engines: "weakref.WeakSet" = weakref.WeakSet()


@pytest.fixture(autouse=True)
def _stop_running_engines(monkeypatch, _release_files):
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
            joined = engine.stop_and_wait()
        except RuntimeError:  # its C++ side is already gone
            continue
        if not joined:
            raise RuntimeError(
                f"a RunEngine worker outlived its test: abort did not stop it\n{_fixtures.describe_stuck_runs(_opened)}"
            )
    _started_engines.clear()
