"""What the first real Windows run of the suite found, pinned where Linux can see it.

CI on `windows-latest` is the only Windows machine this project has, so each behaviour here is
checked by faking the part of the OS that differs: a socket that raises what Winsock raises, a
process abort a dropped QThread causes on every platform, and a file left open, which Linux will
delete without complaint but can still list under ``/proc``.
"""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest
from PyQt6.QtCore import QCoreApplication

from ate import catalog
from ate.devices.stream import SEND_CHUNK, _StreamDevice, _TransportClosed, transport_error_text
from ate.devices.tcp import _TcpDevice
from ate.devices.udp import REFUSED_TEXT, _UdpDevice
from ate.devices import visa
from ate.engine.run_engine import _live_engines
from ate.log import AppLog
from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.storage.store import RunStore
from ate.ui import history_screen
from tests._fixtures import files_held_open_under, files_refusing_a_rename_under, write_ate_env

REPO = Path(__file__).resolve().parents[1]

needs_proc = pytest.mark.skipif(
    sys.platform != "win32" and not Path("/proc/self/fd").is_dir(), reason="the OS cannot list a process's open files"
)

#: One step that passes, as a Test Schedule file.
PASSING_SCHEDULE = (
    'return ScheduleParams("x"):setSchedule({StepParams("a"):setStepNo("1."):'
    'setAction(function() ATE.report(Result.PASS, "ok") end)})\n'
)
#: One step blocked on an answer nobody gives.
BLOCKED_SCHEDULE = (
    'return ScheduleParams("x"):setSchedule({StepParams("a"):setStepNo("1.")'
    ':setInputs({[1] = {desc = "n", type = "INTEGER"}})'
    ':setAction(function() ATE.input(1) ATE.report(Result.PASS, "ok") end)})\n'
)


def _child(tmp_path: Path, source: str) -> subprocess.CompletedProcess:
    """Run ``source`` in a fresh Python: an abort there is an exit code here, not the end of pytest."""
    script = tmp_path / "child.py"
    script.write_text(source, encoding="utf-8", newline="\n")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=f"{REPO / 'src'}{os.pathsep}{REPO}")
    return subprocess.run(
        [sys.executable, str(script), str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=90, cwd=str(REPO),
    )


def _pump_until(predicate, timeout: float = 10.0) -> bool:
    import time

    app = QCoreApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


# -- a closed thing holds no file open (WinError 32) -----------------------------------------------


@needs_proc
def test_a_closed_store_holds_nothing_open_even_after_a_worker_thread_used_it(app, tmp_path):
    """The database, its -wal and its mapped -shm: Windows refuses to delete any of them while open."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    session.select_schedule("Passing schedule")
    session.set_uut_type("U")
    session.set_uut_serial("S")
    session.set_operator("O")
    session.set_project("P")
    session.start()
    assert _pump_until(lambda: session.state == SessionState.FINISHED)
    assert any(path.endswith("ate.db") for path in files_held_open_under(tmp_path)), "the check sees nothing"

    session.close()
    assert files_held_open_under(tmp_path) == set()


@needs_proc
def test_the_check_sees_a_mapped_shm_and_a_plain_open_file(tmp_path):
    """The conftest leak check only means something if it can see each kind of handle."""
    store = RunStore(tmp_path / "a.db")
    store.close_interrupted_runs()
    assert str(tmp_path / "a.db") in files_held_open_under(tmp_path)
    store.close()
    assert files_held_open_under(tmp_path) == set()
    with open(tmp_path / "plain.txt", "w", encoding="utf-8"):
        assert str(tmp_path / "plain.txt") in files_held_open_under(tmp_path)


def test_a_closed_store_refuses_rather_than_reopening_its_file(tmp_path):
    """SQLAlchemy reconnects a disposed Engine on demand, which is the leak coming straight back."""
    store = RunStore(tmp_path / "a.db")
    store.close()
    with pytest.raises(Exception, match="closed"):
        store.close_interrupted_runs()
    assert not (tmp_path / "a.db-wal").exists()
    store.close()  # still idempotent


@needs_proc
def test_db_migrate_leaves_the_database_closed(tmp_path, capsys):
    """`ate db migrate` and `ate setup` run in-process in tests and hold the file on Windows."""
    from ate.cli import main

    db = tmp_path / "ate.db"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "format": 1, "station": {"name": "S", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c10"},
        "database": {"type": "sqlite", "path": str(db)},
    }), encoding="utf-8", newline="\n")
    assert main(["--config", str(config), "db", "migrate", "--yes"]) == 0, capsys.readouterr().err
    assert files_held_open_under(tmp_path) == set()


@needs_proc
def test_setup_leaves_the_database_closed(tmp_path, capsys):
    from ate.cli import main

    assert main(["setup", "--root", str(tmp_path / "station")]) == 0, capsys.readouterr().err
    assert files_held_open_under(tmp_path) == set()


@needs_proc
def test_a_closed_application_log_holds_nothing_open(tmp_path):
    log = AppLog(tmp_path / "logs")
    assert files_held_open_under(tmp_path) == {str(log.path)}
    log.close()
    assert files_held_open_under(tmp_path) == set()


def test_a_sharing_violation_is_what_windows_calls_held_open(tmp_path, monkeypatch):
    """The rename check counts only WinError 32, and leaves every file it probes where it was."""
    (tmp_path / "held.db").write_text("x", encoding="utf-8")
    (tmp_path / "acl.db").write_text("x", encoding="utf-8")
    (tmp_path / "free.db").write_text("x", encoding="utf-8")
    real_rename = os.rename

    def rename(source, target):
        if source.endswith("held.db"):
            error = PermissionError(13, "in use")
            error.winerror = 32
            raise error
        if source.endswith("acl.db"):
            raise PermissionError(13, "access denied")
        real_rename(source, target)

    monkeypatch.setattr(os, "rename", rename)
    assert files_refusing_a_rename_under(tmp_path) == {str(tmp_path / "held.db")}
    assert sorted(path.name for path in tmp_path.iterdir()) == ["acl.db", "free.db", "held.db"]


def test_no_test_deletes_a_directory_its_store_may_still_hold():
    """`tempfile.TemporaryDirectory` is what produced 194 WinError 32 failures: see scratch_directory."""
    allowed = {Path(__file__).name, "_fixtures.py"}  # this check, and the note on why not to
    offenders = [
        path.name
        for path in (REPO / "tests").glob("*.py")
        if path.name not in allowed and "tempfile.TemporaryDirectory" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


# -- the worker's lifetime ("QThread: Destroyed while thread is still running") --------------------

_ENGINE_CHILD_PREAMBLE = f"""
import sys, time
from pathlib import Path
from PyQt6.QtWidgets import QApplication
from ate.engine.run_engine import RunEngine, RunStatus
from ate.storage.store import RunStore

work = Path(sys.argv[1])
app = QApplication([])
store = RunStore(work / "a.db")
schedule = work / "s.tslua"
"""


def test_an_engine_nobody_holds_survives_the_end_of_its_own_thread(tmp_path):
    """The abort behind the 1-in-9 local crash, the Linux 3.14 failures and Windows' 0xC0000409.

    The session drops its engine when the queued FINISHED arrives, which can be before the worker
    has left ``run()``. The sleep makes that order certain: without the engine owning itself this
    aborts every time, where the real race needs luck."""
    source = _ENGINE_CHILD_PREAMBLE + f"""
schedule.write_text({PASSING_SCHEDULE!r}, encoding="utf-8", newline="\\n")
original = RunEngine.run
def slow_to_leave(self):
    original(self)
    time.sleep(0.3)
RunEngine.run = slow_to_leave
done = []
def start():
    engine = RunEngine(store, str(schedule), uut_type="U", uut_serial="S", operator="O")
    engine.state_changed.connect(lambda status: done.append(status) if status == RunStatus.FINISHED else None)
    engine.start()
start()
deadline = time.time() + 20
while not done and time.time() < deadline:
    app.processEvents()
end = time.time() + 1
while time.time() < end:
    app.processEvents()
    time.sleep(0.01)
from ate.engine.run_engine import _live_engines
print("FINISHED" if done else "no finish", len(_live_engines))
"""
    result = _child(tmp_path, source)
    assert result.returncode == 0, f"exit {result.returncode}\n{result.stderr[-1500:]}"
    assert "FINISHED 0" in result.stdout, "the ended worker was not let go: " + result.stdout


def test_an_exception_leaving_the_worker_does_not_abort_the_process(tmp_path):
    """PyQt aborts on an exception escaping ``QThread.run``. Here a schedule no host can load."""
    source = _ENGINE_CHILD_PREAMBLE + """
schedule.write_text('this is not lua', encoding="utf-8", newline="\\n")
done = []
engine = RunEngine(store, str(schedule), uut_type="U", uut_serial="S", operator="O")
engine.state_changed.connect(lambda status: done.append(status) if status == RunStatus.FINISHED else None)
engine.start()
deadline = time.time() + 20
while not done and time.time() < deadline:
    app.processEvents()
engine.wait()
print("FINISHED" if done else "no finish")
"""
    result = _child(tmp_path, source)
    assert result.returncode == 0, f"exit {result.returncode}\n{result.stderr[-1500:]}"
    assert "FINISHED" in result.stdout, result.stdout


def test_exiting_with_a_run_in_progress_stops_the_worker_first(tmp_path):
    """An interpreter ending with a live worker destroys it running. The answer never comes here."""
    source = _ENGINE_CHILD_PREAMBLE + f"""
schedule.write_text({BLOCKED_SCHEDULE!r}, encoding="utf-8", newline="\\n")
engine = RunEngine(store, str(schedule), uut_type="U", uut_serial="S", operator="O")
engine.start()
deadline = time.time() + 5
while time.time() < deadline and not engine.isRunning():
    app.processEvents()
time.sleep(0.3)
print("leaving", engine.isRunning())
"""
    result = _child(tmp_path, source)
    assert result.returncode == 0, f"exit {result.returncode}\n{result.stderr[-1500:]}"
    assert "leaving True" in result.stdout, result.stdout
    conn = sqlite3.connect(tmp_path / "a.db")
    try:
        rows = conn.execute("SELECT end_reason FROM test_run").fetchall()
    finally:
        conn.close()
    assert rows == [("ABORTED",)], "the worker was killed mid-run rather than stopped: the run row is open"


def test_closing_a_session_mid_run_ends_the_worker_before_the_store(app, tmp_path):
    """A worker must not record into a closed store, and the report is still written."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    session.select_schedule("Blocking input schedule")
    session.set_uut_type("U")
    session.set_uut_serial("S")
    session.set_operator("O")
    session.set_project("P")
    engine = session.start()
    assert _pump_until(lambda: session.run_uuid is not None and engine.isRunning())
    run_uuid = session.run_uuid

    session.close()

    assert not engine.isRunning(), "the worker outlived the session that owned it"
    state = session.state
    _pump_until(lambda: False, timeout=0.1)  # the FINISHED still queued from that worker
    assert session.state == state, "a closed session moved on"
    reopened = RunStore(env["db"])
    row = reopened.get_test_run(run_uuid)
    assert row["ended_at"] is not None and row["end_reason"] == "ABORTED", row


def test_a_qt_fatal_message_reaches_the_log_with_every_stack(tmp_path):
    """Qt ends a Windows process with __fastfail and pytest-qt keeps its messages: say why first."""
    source = """
import time
from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QThread, qInstallMessageHandler
from tests._fixtures import report_qt_fatal

app = QCoreApplication([])
qInstallMessageHandler(report_qt_fatal())
class Sleeper(QThread):
    def run(self):
        time.sleep(30)
thread = Sleeper()
thread.start()
time.sleep(0.2)
sip.delete(thread)  # destroyed while it runs
"""
    result = _child(tmp_path, source)
    assert result.returncode != 0, "the scenario no longer aborts, so this proves nothing"
    assert "Qt fatal error: QThread: Destroyed" in result.stderr, result.stderr[-1500:]
    assert "most recent call first" in result.stderr, "no Python stack: " + result.stderr[-1500:]


def test_a_qt_fatal_inside_a_real_pytest_run_is_not_swallowed(tmp_path):
    """Inside pytest two things eat that message: pytest-qt's handler and the capture of stderr."""
    test_file = tmp_path / "test_dies.py"
    test_file.write_text(
        "import time\n"
        "from PyQt6 import sip\n"
        "from PyQt6.QtCore import QThread\n"
        "class Sleeper(QThread):\n"
        "    def run(self):\n"
        "        time.sleep(30)\n"
        "def test_dies():\n"
        "    thread = Sleeper()\n"
        "    thread.start()\n"
        "    time.sleep(0.2)\n"
        "    sip.delete(thread)\n",
        encoding="utf-8",
        newline="\n",
    )
    env = dict(os.environ, PYTHONPATH=f"{REPO / 'src'}{os.pathsep}{REPO}")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "conftest", "-p", "no:cacheprovider", "-q", "--rootdir", str(tmp_path), str(test_file)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=90, cwd=str(tmp_path),
    )
    assert result.returncode != 0
    assert "Qt fatal error: QThread: Destroyed" in result.stderr, result.stdout[-800:] + result.stderr[-800:]


# -- devices speak the same on both platforms ------------------------------------------------------


class _FakeSocket:
    """Raises what Winsock raises; records what it was asked to send."""

    def __init__(self, recv_raises=None, send_raises=None, send_limit=None):
        self.recv_raises, self.send_raises, self.send_limit = recv_raises, send_raises, send_limit
        self.sends: list[int] = []
        self._timeout = None

    def recv(self, _size):
        raise self.recv_raises

    def send(self, data):
        if self.send_raises is not None:
            raise self.send_raises
        self.sends.append(len(data))
        return len(data)

    def gettimeout(self):
        return self._timeout

    def settimeout(self, value):
        self._timeout = value

    def close(self):
        pass


WINSOCK_RESET = ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")


@pytest.mark.parametrize(
    "exc, text",
    [
        (WINSOCK_RESET, "connection reset by peer"),
        (ConnectionResetError(104, "Connection reset by peer"), "connection reset by peer"),
        (ConnectionAbortedError(10053, "An established connection was aborted"), "connection aborted"),
        (BrokenPipeError(32, "Broken pipe"), "broken pipe"),
        (OSError(5, "I/O error"), "[Errno 5] I/O error"),
    ],
)
def test_a_broken_connection_has_one_wording(exc, text):
    assert transport_error_text(exc) == text


def _tcp(sock) -> _TcpDevice:
    return _TcpDevice(sock, {"host": "h", "port": 1, "timeout": 1, "readTerm": "", "writeTerm": ""})


def test_a_tcp_reset_reads_the_same_as_on_linux(monkeypatch):
    monkeypatch.setattr("select.select", lambda r, w, x, t: (r, w, x))
    dev = _tcp(_FakeSocket(recv_raises=WINSOCK_RESET, send_raises=WINSOCK_RESET))
    with pytest.raises(_TransportClosed, match="^connection reset by peer$"):
        dev._recv()
    assert dev.write({"data": b"x"}) == (False, 0, "connection reset by peer")


def test_a_stream_write_never_hands_the_os_more_than_a_chunk(monkeypatch):
    """Winsock takes one send of any size if its buffer has room, so one big send cannot time out."""
    monkeypatch.setattr("select.select", lambda r, w, x, t: (r, w, x))
    sock = _FakeSocket()
    payload = b"x" * (SEND_CHUNK * 3 + 1)
    assert _tcp(sock).write({"data": payload})[:2] == (True, len(payload))
    assert sock.sends == [SEND_CHUNK, SEND_CHUNK, SEND_CHUNK, 1]


def test_a_visa_socket_write_never_hands_the_os_more_than_a_chunk():
    sock = _FakeSocket()
    payload = b"x" * (SEND_CHUNK + 1)
    assert visa._socket_write(sock, payload, 5) == (True, len(payload), None)
    assert sock.sends == [SEND_CHUNK, 1]


def _udp(sock) -> _UdpDevice:
    return _UdpDevice(sock, {"host": "h", "port": 1, "localPort": 0, "timeout": 1})


@pytest.mark.parametrize(
    "refusal",
    [ConnectionRefusedError(111, "Connection refused"), WINSOCK_RESET],
    ids=["linux-ECONNREFUSED", "windows-WSAECONNRESET"],
)
def test_a_udp_refusal_seen_by_the_reader_fails_the_next_write_only(monkeypatch, refusal):
    """Windows reports a port-unreachable only through ``recv``, as a *reset*. Nothing was reset, and
    a write still says so: once, in the same words, leaving the device usable."""
    monkeypatch.setattr("select.select", lambda r, w, x, t: (r, w, x))
    sock = _FakeSocket(recv_raises=refusal)
    dev = _udp(sock)
    assert dev._recv() is None, "a refusal is nothing this poll"
    assert dev._failed is None
    assert dev.write({"data": b"go"}) == (False, 0, REFUSED_TEXT)
    assert dev.write({"data": b"go"}) == (True, 2, None), "the next write goes out"


def test_a_udp_refusal_raised_by_the_send_reads_the_same(monkeypatch):
    monkeypatch.setattr("select.select", lambda r, w, x, t: (r, w, x))
    dev = _udp(_FakeSocket(send_raises=WINSOCK_RESET))
    assert dev.write({"data": b"go"}) == (False, 0, REFUSED_TEXT)
    assert dev._failed is None


# -- a path means the same on every Station --------------------------------------------------------


@pytest.mark.parametrize("path", ["/opt/ate/limits.lua", "\\opt\\limits.lua", "..\\up\\limits.lua", "C:\\x.lua", "//host/x.lua"])
def test_a_module_path_outside_the_release_needs_a_module_name_on_windows_too(monkeypatch, path):
    """On Windows ``PureWindowsPath("/opt/x").is_absolute()`` is False, so a rooted path slipped past."""
    monkeypatch.setattr(catalog, "Path", PureWindowsPath)
    assert catalog._needs_module(path) is True


# -- History opens a file on Windows -----------------------------------------------------------------


def test_opening_a_report_from_history_uses_the_windows_shell(monkeypatch, tmp_path):
    """There is no xdg-open on Windows: the button raised FileNotFoundError on every click."""
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF")
    opened = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)
    monkeypatch.setattr(history_screen.subprocess, "Popen", lambda *a, **k: pytest.fail("ran xdg-open"))
    history_screen._default_open_file(str(report))
    assert opened == [str(report)]


# -- text has one encoding, whatever the machine's code page -------------------------------------


def test_no_shipped_code_leaves_a_text_encoding_to_the_machine():
    """Windows decodes with its legacy code page by default, and strict decoding of a child's output
    raises. ATE must work with nothing set, so PYTHONUTF8 on the runner would only hide this."""
    offenders = []
    for root in ("src", "packaging"):
        for path in (REPO / root).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                keywords = {keyword.arg for keyword in node.keywords}
                binary = any(isinstance(a, ast.Constant) and "b" in str(a.value) for a in node.args[1:])
                needs = (
                    name in ("read_text", "write_text")
                    or (name == "open" and isinstance(node.func, ast.Name) and not binary)
                    or (name in ("run", "Popen", "check_output") and "text" in keywords)
                )
                if needs and "encoding" not in keywords:
                    offenders.append(f"{path.relative_to(REPO)}:{node.lineno} {name}")
    assert offenders == []
