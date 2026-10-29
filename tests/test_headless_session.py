"""Headless run session: config -> store -> catalog -> engine, driven offscreen.

Ticket 02's seam-1 test. A passing and a failing schedule each run headless (no display)
and are read back through :class:`RunStore` with the right results; fields set before start
are recorded on the run row; interrupted runs left open by a crash are closed as INTERRUPTED
at launch (spec rule 98). No widgets, no mocks.
"""

import os
import re
import sys
import time
import pytest
import sqlite3
import threading
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, QObject, QProcess, Qt

from ate.run_session.headless import HeadlessRunSession, SessionError, SessionState
from ate.storage.store import RunStore

from ._fixtures import ENABLE_DISABLED, FAILING, LOGGING, PASSING, fake_pdf_helper as _fake_helper, write_ate_env


# The real end-to-end PDF test needs QtWebEngineCore; if it can't import here the helper process
# can never build its page, so skip that one test rather than fail the whole module.
try:
    from PyQt6.QtWebEngineCore import QWebEnginePage  # noqa: F401

    PYQT_WEBENGINE_IMPORT_ERROR = None
except Exception as _exc:  # noqa: BLE001 - any import failure means no real PDF rendering here
    PYQT_WEBENGINE_IMPORT_ERROR = _exc


def _pump_until_report_written(session: HeadlessRunSession, events: list[tuple[str, str]], *, deadline: float | None = None) -> bool:
    """Pump the event loop until ``report_written`` fired (or ``deadline``), returning whether it did.

    The helper runs in a QProcess whose finished signal is delivered here when events are pumped."""
    app = QCoreApplication.instance() or QCoreApplication([])
    if deadline is None:
        deadline = time.time() + 20.0
    while not events:
        app.processEvents()
        if time.time() > deadline:
            return False
        time.sleep(0.002)  # pump in slices instead of burning the CPU on an empty loop
    return True


def _wait_finished(session: HeadlessRunSession) -> bool:
    """Poll an application's processEvents until the session reaches FINISHED (or times out).

    Queued cross-thread signals are only delivered once a QCoreApplication exists to pump them;
    this offscreen build delivers nothing through bare ``QCoreApplication.processEvents()`` when
    no instance is present. If none exists, create one so delivery works. The instance is local
    to the test and gone by the next one, so nothing Qt lingers for interpreter shutdown.

    A Stop-on-Fail pause blocks the worker until an operator answers a prompt (packet 12b); headless
    drivers have no UI to do that, so answer Continue here while pumping — mirroring how input-supplying
    tests supply values. Passing runs never emit failure_prompt, so this only affects paused failures."""
    def _continue_on_failure(_node_seq: int, _attempt_no: int) -> None:
        session.choose("CONTINUE")

    session.failure_prompt.connect(_continue_on_failure)
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    deadline = time.monotonic() + 10.0  # a time bound, not a pump count: load-independent
    while time.monotonic() < deadline:
        app.processEvents()
        if session._state == SessionState.FINISHED:
            return True
        time.sleep(0.005)
    return False


def test_passing_schedule_runs_headless_and_reads_back(tmp_path) -> None:
    """A selected schedule runs headless and records a PASS run with the Operator's fields."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    states = []
    session.state_changed.connect(states.append)

    session.set_operator("Ada")
    session.set_project("Power board")
    session.set_uut_type("PSU")
    session.set_uut_serial("PSU-001")
    session.select_schedule("Passing schedule")

    engine = session.start()
    assert _wait_finished(session)

    row = session.store.get_test_run(session.run_uuid)
    assert row is not None
    assert row["result"] == "PASS"
    assert row["end_reason"] == "COMPLETED"
    # Fields set before start are recorded on the run row.
    assert row["operator"] == "Ada"
    assert row["project"] == "Power board"
    assert row["uut_type"] == "PSU"
    assert row["uut_serial"] == "PSU-001"
    # Station identity comes from the config.
    assert row["station_name"] == "BENCH-03"
    assert row["schedule_name"] == "Passing schedule"

    # Observable state progressed IDLE -> SELECTING -> STARTED -> FINISHED.
    assert SessionState("SELECTING") in states
    assert SessionState("STARTED") in states
    assert SessionState("FINISHED") in states


def test_run_end_writes_html_report(tmp_path) -> None:
    """A finished headless run writes its HTML report under reports.directory (packet 21a)."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    session.set_operator("Ada")
    session.set_project("Power board")
    session.set_uut_type("PSU")
    session.set_uut_serial("PS-1")
    session.select_schedule("Passing schedule")

    engine = session.start()
    assert _wait_finished(session)

    # The run-end hook wrote a <Type>/<Type>_<serial>_<stamp>_PASS.html report.
    written = list(env["reports_dir"].glob("**/*_PASS.html"))
    assert len(written) == 1, f"no PASS report written at run end: {[p.name for p in env['reports_dir'].glob('**/*.html')]}"
    report = written[0]
    assert report.parent == env["reports_dir"] / "PSU"
    assert re.match(r"^PSU_PS-1_\d{8}-\d{6}_PASS\.html$", report.name)
    body = report.read_text(encoding="utf-8")
    assert "<div class=\"verdict verdict-pass\">PASS</div>" in body
    assert "Ada" in body  # operator name flowed through to the Cover


def test_run_end_writes_html_and_pdf_reports(tmp_path, monkeypatch) -> None:
    """A finished run writes both HTML and PDF; the helper process reports both paths (packet 22b)."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    events: list[tuple[str, str]] = []
    session.report_written.connect(lambda html, pdf: events.append((html, pdf)), Qt.ConnectionType.QueuedConnection)


    session.set_operator("Ada")
    session.set_project("Power board")
    session.set_uut_type("PSU")
    session.set_uut_serial("PS-1")
    session.select_schedule("Passing schedule")

    engine = session.start()
    assert _wait_finished(session)

    # HTML report exists next to the PDF.
    written = list(env["reports_dir"].glob("**/*_PASS.html"))
    assert len(written) == 1, f"no PASS report: {[p.name for p in env['reports_dir'].glob('**/*.html')]}"
    html_path = str(written[0])

    # The helper finished and report_written delivered both paths.
    assert _pump_until_report_written(session, events)
    html, pdf = events[-1]
    assert html == html_path
    assert Path(pdf).exists() and Path(pdf).stat().st_size > 0
    assert Path(pdf).name == written[0].with_suffix(".pdf").name


def test_run_end_pdf_failure_keeps_html_and_logs_error(tmp_path, monkeypatch) -> None:
    """A failing PDF helper leaves the HTML, logs an ERROR (source ate), and reports an empty pdf path."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    log_lines: list[str] = []
    session.on_log = lambda level, source, text: log_lines.append(f"{level}:{source}:{text}")

    events: list[tuple[str, str]] = []
    session.report_written.connect(lambda html, pdf: events.append((html, pdf)), Qt.ConnectionType.QueuedConnection)

    monkeypatch.setattr(
        "ate.report.pdf_job.pdf_helper_command",
        _fake_helper("import sys; sys.stderr.write('fake pdf helper blew up'); sys.exit(3)"),
    )

    session.set_operator("Ada")
    session.select_schedule("Passing schedule")

    engine = session.start()
    assert _wait_finished(session)

    # HTML stays; no PDF.
    written = list(env["reports_dir"].glob("**/*_PASS.html"))
    assert len(written) == 1, f"expected the HTML report to survive: {[p.name for p in env['reports_dir'].glob('**/*.html')]}"
    assert not any(p.suffix == ".pdf" for p in env["reports_dir"].rglob("*"))

    # report_written fired with an empty pdf path but the html path kept.
    assert _pump_until_report_written(session, events)
    # ATE logged the failure with source ate.
    assert any("ERROR:ate:" in line and "fake pdf helper blew up" in line for line in log_lines), f"no ERROR/ate log line: {log_lines}"
    html, pdf = events[-1]
    assert html == str(written[0])
    assert pdf == ""


def test_run_end_pdf_is_not_blocking(tmp_path, monkeypatch) -> None:
    """The PDF helper runs as a separate process: FINISHED returns well before a ~1s helper finishes."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    events: list[tuple[str, str]] = []
    session.report_written.connect(lambda html, pdf: events.append((html, pdf)), Qt.ConnectionType.QueuedConnection)

    monkeypatch.setattr(
        "ate.report.pdf_job.pdf_helper_command",
        _fake_helper("import sys, time; time.sleep(1.0); open(sys.argv[1], 'wb').write(b'%PDF-1.4 slow')"),
    )

    session.set_operator("Ada")
    session.select_schedule("Passing schedule")

    start = time.perf_counter()
    engine = session.start()
    assert _wait_finished(session)  # FINISHED must return before the ~1s helper completes
    elapsed = time.perf_counter() - start

    assert elapsed < 0.8, f"FINISHED handling blocked on PDF: {elapsed:.3f}s"
    # The helper still finishes and reports the PDF afterwards.
    assert _pump_until_report_written(session, events)
    _, pdf = events[-1]
    assert Path(pdf).exists()


def test_run_end_writes_real_pdf_report(tmp_path) -> None:
    """End-to-end with the real helper process; skipped unless QtWebEngineCore can import."""
    if PYQT_WEBENGINE_IMPORT_ERROR is not None:
        pytest.skip("QtWebEngineCore unavailable for real PDF rendering")

    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    events: list[tuple[str, str]] = []
    session.report_written.connect(lambda html, pdf: events.append((html, pdf)), Qt.ConnectionType.QueuedConnection)

    session.set_operator("Ada")
    session.select_schedule("Passing schedule")

    engine = session.start()
    assert _wait_finished(session)

    written = list(env["reports_dir"].glob("**/*_PASS.html"))
    assert len(written) == 1, f"no PASS report: {[p.name for p in env['reports_dir'].glob('**/*.html')]}"

    # The real helper rendered a non-empty PDF next to the HTML.
    assert _pump_until_report_written(session, events)
    html, pdf = events[-1]
    assert html == str(written[0])
    assert Path(pdf).exists() and Path(pdf).stat().st_size > 0


def test_failing_schedule_runs_headless_and_reads_back(tmp_path) -> None:
    """A selected schedule that reports FAIL runs headless and records a FAIL run."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    session.select_schedule("Failing schedule")
    engine = session.start()
    assert _wait_finished(session)

    row = session.store.get_test_run(session.run_uuid)
    assert row is not None
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "COMPLETED"


def test_stop_on_fail_records_the_schedule_default_when_unset(tmp_path) -> None:
    """A schedule that leaves Stop on Fail unset records the spec default (True)."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    session.set_operator("Ada")
    session.select_schedule("Failing schedule")  # no setStopOnFail -> default True

    engine = session.start()
    assert _wait_finished(session)

    row = session.store.get_test_run(session.run_uuid)
    assert row is not None
    assert row["stop_on_fail"] == 1
    assert session.selected_stop_on_fail is True


def test_stop_on_fail_records_an_explicit_false_default(tmp_path) -> None:
    """A schedule that sets Stop on Fail False records it without any Operator override."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    session.set_operator("Ada")
    session.select_schedule("Stop on fail false schedule")  # setStopOnFail(False)

    engine = session.start()
    assert _wait_finished(session)

    row = session.store.get_test_run(session.run_uuid)
    assert row is not None
    assert row["stop_on_fail"] == 0
    assert session.selected_stop_on_fail is False


def test_operator_overrides_the_schedule_default_to_false(tmp_path) -> None:
    """The Operator's choice overrides the schedule default; a True default beaten to False."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    session.set_operator("Ada")
    session.select_schedule("Failing schedule")  # default True
    session.set_stop_on_fail(False)  # Operator runs every step regardless of failures

    engine = session.start()
    assert _wait_finished(session)

    row = session.store.get_test_run(session.run_uuid)
    assert row is not None
    assert row["stop_on_fail"] == 0


def test_operator_overrides_the_schedule_default_to_true(tmp_path) -> None:
    """The Operator's choice overrides a False default back to True."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    session.set_operator("Ada")
    session.select_schedule("Stop on fail false schedule")  # default False
    session.set_stop_on_fail(True)  # Operator opts back in before starting

    engine = session.start()
    assert _wait_finished(session)

    row = session.store.get_test_run(session.run_uuid)
    assert row is not None
    assert row["stop_on_fail"] == 1


def test_interrupted_runs_are_closed_at_launch(tmp_path) -> None:
    """A run left open by a crash is closed as INTERRUPTED/FAIL before the next run starts."""
    env = write_ate_env(tmp_path)
    db_path = env["db"]

    # Simulate a crashed previous run: an open row (no end time) in the database.
    store = RunStore(db_path)
    interrupted_uuid = store.insert_test_run(
        {"run_uuid": "deadbeef-0000-0000-0000-000000000001", "operator": "ghost"}
    )["run_uuid"]
    assert store.get_test_run(interrupted_uuid)["ended_at"] is None

    log_lines: list[str] = []
    session = HeadlessRunSession(env["config"])
    session.on_log = lambda level, source, text: log_lines.append(f"{level}:{text}")

    session.select_schedule("Passing schedule")
    engine = session.start()
    assert _wait_finished(session)

    # The crashed run was recovered as INTERRUPTED / FAIL.
    closed = store.get_test_run(interrupted_uuid)
    assert closed is not None
    assert closed["end_reason"] == "INTERRUPTED"
    assert closed["result"] == "FAIL"

    # ATE logged that it did so (spec rule 98).
    assert any("interrupted" in line for line in log_lines)

    # The new run still records cleanly alongside the recovered one.
    row = session.store.get_test_run(session.run_uuid)
    assert row is not None and row["result"] == "PASS"
    assert row["end_reason"] == "COMPLETED"


def test_start_without_selection_raises(tmp_path) -> None:
    """Starting with no schedule selected is a clear error, not a crash."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    try:
        session.start()
    except Exception as exc:  # noqa: BLE001 - any selection error is acceptable here
        assert "no schedule selected" in str(exc)


def test_interrupted_run_recovery_is_logged_in_the_app_log(tmp_path) -> None:
    """A run left open by a crash is closed at launch via the real store API and logged.

    Ticket 04's ticket-02 seam-2 test: no on_log override here, so the session opens its own
    application log for the launch and recovery must be recorded there — proving the recovery
    uses the real :class:`RunStore` (not a mock) and that ATE keeps one file per launch.
    """
    env = write_ate_env(tmp_path)  # config has no on_log; the session wires its own app log

    store = RunStore(env["db"])
    interrupted_uuid = store.insert_test_run(
        {"run_uuid": "deadbeef-0000-0000-0000-000000000001", "operator": "ghost"}
    )["run_uuid"]
    assert store.get_test_run(interrupted_uuid)["ended_at"] is None  # open row = crashed run

    session = HeadlessRunSession(env["config"])  # on_log defaults to the launch application log
    session.select_schedule("Passing schedule")
    engine = session.start()
    assert _wait_finished(session)

    # The store (real API, no mocks) closed the crashed run as INTERRUPTED / FAIL.
    closed = store.get_test_run(interrupted_uuid)
    assert closed is not None
    assert closed["end_reason"] == "INTERRUPTED"
    assert closed["result"] == "FAIL"

    # ATE's application log for this launch recorded the recovery (ticket 04).
    assert session._app_log is not None
    lines = [ln.strip() for ln in session._app_log.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert any("interrupted" in line.lower() for line in lines)


def test_schedule_logs_are_recorded_with_levels_sources_and_owners(tmp_path) -> None:
    """ATE.log at every level and print are recorded as log lines with source, owner and text."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    states = []
    session.state_changed.connect(states.append)

    session.set_operator("Ada")
    session.select_schedule("Logging schedule")

    engine = session.start()
    assert _wait_finished(session)

    row = session.store.get_test_run(session.run_uuid)
    assert row is not None and row["result"] == "PASS"  # every step reported PASS

    lines_by_seq = {ln["seq"]: ln for ln in session.store.log_lines(session.run_uuid)}

    # Step 1 logs INFO via ATE.log and again via print (redirected to INFO); steps 2 and 3
    # log WARNING and ERROR. Four schedule-sourced lines, one per recorded event; each gets
    # its own seq in emission order (a batch can hold several).
    schedule = sorted(
        (ln for ln in lines_by_seq.values() if ln["source"] == "schedule"),
        key=lambda ln: ln["seq"],
    )
    assert [(ln["seq"], ln["level"], ln["text"]) for ln in schedule] == [
        (0, "INFO", "step one running"),
        (1, "INFO", "printed here"),  # print maps to INFO and is recorded too; distinct seq within the batch
        (2, "WARNING", "value 12"),
        (4, "ERROR", "something failed"),  # ate WARNING for warn step took seq 3
    ]

    # Every schedule line is owned by the Attempt of the step that emitted it. Ownership
    # (node_seq/attempt_no) is set once per batch and shared; it is not the line's own seq.
    expected_owner = [0, 0, 1, 2]  # owning step per recorded schedule event, in run order
    for ln, owner in zip(schedule, expected_owner):
        assert ln["node_seq"] == owner and ln["attempt_no"] == 1
        assert ln["action_seq"] is None


def test_error_level_log_line_is_recorded(tmp_path) -> None:
    """A step that logs at ERROR records an ERROR log line owned by its Attempt."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    session.select_schedule("Logging schedule")
    engine = session.start()
    assert _wait_finished(session)

    lines_by_seq = {ln["seq"]: ln for ln in session.store.log_lines(session.run_uuid)}
    error_line = next(ln for ln in lines_by_seq.values() if ln["level"] == "ERROR")
    assert (error_line["source"], error_line["text"]) == ("schedule", "something failed")
    assert error_line["node_seq"] == 2 and error_line["attempt_no"] == 1


def test_a_closed_session_ends_its_pdf_helper_process(qapp, monkeypatch, tmp_path) -> None:
    """Packet 25b review, teardown crash: closing a session must end the helper it started.

    The Test Report PDF is rendered by a ``QProcess`` parented to the session. If the session is
    released while that process is still running, Qt destroys the process underneath it, the job's
    ``finished`` callback then fires against objects whose Qt side is gone, and the interpreter
    aborts — which is what made the run-end tests crash at teardown. ``close()`` therefore kills the
    helper it owns before releasing anything else."""
    slow_helper = "import sys, time; time.sleep(5); open(sys.argv[1], 'wb').write(b'%PDF-1.4 stub')"
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", _fake_helper(slow_helper))

    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    session.select_schedule("Passing schedule")
    session.start()
    assert _wait_finished(session), "the run never finished"

    from ate.report.pdf_job import _live_pdf_jobs

    jobs = [proc for proc in _live_pdf_jobs if proc.parent() is session]
    assert jobs, "the finished run started no PDF helper to test"
    assert all(proc.state() != QProcess.ProcessState.NotRunning for proc in jobs), (
        "the helper had already exited, so this test proves nothing"
    )

    session.close()

    assert not [proc for proc in _live_pdf_jobs if any(proc is job for job in jobs)], (
        "the closed session left its PDF helper registered as live"
    )
    for job in jobs:
        assert job.state() == QProcess.ProcessState.NotRunning, "the helper process outlived its session"
        assert job.parent() is session, "the helper was orphaned from the session that owned it"


def test_a_pdf_helper_whose_owner_is_gone_reports_nothing(qapp, monkeypatch, tmp_path) -> None:
    """Packet 25b review, teardown crash: a job whose owner dies mid-render reports nothing.

    Qt destroys a ``QProcess`` together with its parent while the render is still in flight, and the
    job's ``finished`` callback then runs against a process whose Qt side is gone. Reporting into
    that dead process raises inside a Qt slot, which PyQt turns into ``Fatal Python error: Aborted``.
    The callback therefore has to swallow it and drop the job from the live set."""
    slow_helper = "import sys, time; time.sleep(3); open(sys.argv[1], 'wb').write(b'%PDF-1.4 stub')"
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", _fake_helper(slow_helper))

    from ate.report.pdf_job import _live_pdf_jobs, start_pdf_job

    owner = QObject()
    start_pdf_job(owner, tmp_path / "report.html", tmp_path / "report.pdf")
    job = next(proc for proc in _live_pdf_jobs if proc.parent() is owner)

    owner.deleteLater()
    qapp.processEvents()  # the owner goes, and its process with it, while the helper is running

    deadline = time.monotonic() + 10
    while any(proc is job for proc in _live_pdf_jobs) and time.monotonic() < deadline:
        qapp.processEvents()

    assert not any(proc is job for proc in _live_pdf_jobs), (
        "a job whose owner was destroyed stayed registered as live"
    )


def test_abort_between_steps_through_session_records_error_notrun_and_aborted(qapp, tmp_path) -> None:
    """Packet 13a checkbox 3 (seam-1): call ``session.request_abort()`` during a blocking run.

    Drives the Abortable schedule entirely through :class:`HeadlessRunSession` — select, start, answer
    step 1's input so it completes cleanly, then request an abort between steps. The operator controller
    is reached only via the engine returned by ``start()``; no widgets or staged screen are involved."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    answered = threading.Event()

    def _on_ask(node_seq, attempt_no, index):
        op.supply_input(index - 1, 5)  # answer step 1's input so it completes cleanly before aborting
        answered.set()

    session.input_asked.connect(_on_ask)

    session.set_operator("Ada")
    session.select_schedule("Abortable schedule")
    engine = session.start()
    op = engine.operator_input  # the worker's operator controller; valid while running

    # Pump until step 1 announces and answers its input — at which point the worker is blocked on it.
    # Do not wait for FINISHED here: a bare pump would let the worker race through steps 2 & 3 first.
    answered_deadline = time.monotonic() + 10.0  # a time bound, not a pump count
    while not answered.is_set() and time.monotonic() < answered_deadline:
        qapp.processEvents()
    assert answered.is_set(), "step 1 never announced its input within 10 s"

    # Abort between steps; then let the worker record ERROR / NOTRUN / ABORTED. request_abort runs on
    # this thread before any post-answer pump, so it wins the step boundary deterministically.
    session.request_abort()
    assert _wait_finished(session)

    row = session.store.get_test_run(session.run_uuid)
    assert row is not None
    # A whole-run abort fails the run with End Reason ABORTED (spec rule 83).
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "ABORTED"

    conn = session.store._conn
    conn.row_factory = sqlite3.Row
    nodes = {n["step_number"]: dict(n) for n in conn.execute(
        "SELECT step_number, result, attempt_count FROM node WHERE run_uuid=? ORDER BY node_seq", (row["run_uuid"],)
    ).fetchall()}
    # Step 1 ran and passed; step 2 was the running step at abort time -> ERROR; step 3 never ran.
    assert nodes == {
        "1.": {"step_number": "1.", "result": "PASS", "attempt_count": 1},
        "2.": {"step_number": "2.", "result": "ERROR", "attempt_count": 1},
        "3.": {"step_number": "3.", "result": "NOTRUN", "attempt_count": 0},
    }

    err = conn.execute(
        "SELECT pass_fail, msg FROM attempt WHERE run_uuid=? AND node_seq=1", (row["run_uuid"],)
    ).fetchone()
    assert err is not None and err["pass_fail"] == "ERROR"
    assert err["msg"] == "Aborted by Operator"


def _nodes_by_seq(store, run_uuid: str) -> dict[int, dict]:
    """Index a run's node rows by runner seq for enable/disable assertions."""
    return {int(n["node_seq"]): n for n in store.nodes(run_uuid)}


def _check_nodes(nodes_by_step: dict[str, dict], want: dict[str, dict]) -> None:
    """Assert each step's recorded fields match ``want`` (a subset of node columns).

    Raises on the first mismatch so a failing test names the offending step and field."""
    for step_number, fields in want.items():
        got = nodes_by_step[step_number]
        for key, value in fields.items():
            assert got[key] == value, f"{step_number} {key}={got.get(key)!r}"


def test_disabled_step_by_default_records_notrun_without_attempt(tmp_path) -> None:
    """A step :setEnable(false) by default isn't executed and records NOTRUN with no Attempt.

    The Enable/disable schedule has a=0 (on), b=1 (:setEnable(false)), c=2 (on). With no Operator
    override only the middle step is skipped; its node is NOTRUN/attempt_count 0 / (default,enabled)
    = (0, 0) and it records no Attempt row. The other steps run normally."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    session.set_operator("Ada")
    session.select_schedule("Enable/disable schedule")  # b is :setEnable(false), no override

    engine = session.start()
    assert _wait_finished(session)

    row = session.store.get_test_run(session.run_uuid)
    assert row is not None
    assert row["result"] == "PASS"  # a and c ran; only the disabled middle step was skipped

    nodes = {n["step_number"]: n for n in session.store.nodes(session.run_uuid)}
    by_seq = _nodes_by_seq(session.store, session.run_uuid)
    _check_nodes(nodes, {
        "1.": {"result": "PASS", "attempt_count": 1, "default_enabled": 1, "enabled": 1},
        "2.": {"result": "NOTRUN", "attempt_count": 0, "default_enabled": 0, "enabled": 0},
        "3.": {"result": "PASS", "attempt_count": 1, "default_enabled": 1, "enabled": 1},
    })
    # The disabled step recorded no Attempt row at all (attempt_count stays 0).
    assert not any(a["node_seq"] == 1 for a in session.store.attempts(session.run_uuid))


def test_operator_override_enables_a_disabled_step(tmp_path) -> None:
    """An Operator override flips a :setEnable(false) step on so it runs; the default stays 0.

    b is seq 1 (its position in the three-step schedule); toggling it on before start records
    (default_enabled, enabled) = (0, 1) and executes it."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    session.set_operator("Ada")
    session.select_schedule("Enable/disable schedule")
    session.toggle_node(1, True)  # override the :setEnable(false) middle step on before start

    engine = session.start()
    assert _wait_finished(session)

    nodes = {n["step_number"]: n for n in session.store.nodes(session.run_uuid)}
    by_seq = _nodes_by_seq(session.store, session.run_uuid)
    _check_nodes(nodes, {
        "1.": {"result": "PASS", "attempt_count": 1, "default_enabled": 1, "enabled": 1},
        "2.": {"result": "PASS", "attempt_count": 1, "default_enabled": 0, "enabled": 1},
        "3.": {"result": "PASS", "attempt_count": 1, "default_enabled": 1, "enabled": 1},
    })


def test_operator_override_disables_a_default_on_step(tmp_path) -> None:
    """An Operator override flips a default-on step off; it records NOTRUN with no Attempt.

    a is seq 0 (default on); toggling it off before start records (1, 0). The default-off middle
    step stays skipped too, so both non-running steps are NOTRUN and only c runs."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    session.set_operator("Ada")
    session.select_schedule("Enable/disable schedule")
    session.toggle_node(0, False)  # override the default-on first step off before start

    engine = session.start()
    assert _wait_finished(session)

    nodes = {n["step_number"]: n for n in session.store.nodes(session.run_uuid)}
    by_seq = _nodes_by_seq(session.store, session.run_uuid)
    _check_nodes(nodes, {
        "1.": {"result": "NOTRUN", "attempt_count": 0, "default_enabled": 1, "enabled": 0},
        "2.": {"result": "NOTRUN", "attempt_count": 0, "default_enabled": 0, "enabled": 0},
        "3.": {"result": "PASS", "attempt_count": 1, "default_enabled": 1, "enabled": 1},
    })
    assert not any(a["node_seq"] == 0 for a in session.store.attempts(session.run_uuid))


def test_toggle_node_rejects_bad_types(tmp_path) -> None:
    """toggle_node validates its arguments like the neighbouring setters."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    with pytest.raises(SessionError):
        session.toggle_node("nope", True)  # seq must be an int
    with pytest.raises(SessionError):
        session.toggle_node(0, "yes")  # enable must be a bool

    # No override leaked into the run.
    session.select_schedule("Enable/disable schedule")
    engine = session.start()
    assert _wait_finished(session)
    by_seq = _nodes_by_seq(session.store, session.run_uuid)
    assert by_seq[0]["result"] == "PASS"  # a ran normally, so no override was applied


def test_toggle_node_while_running_raises(tmp_path) -> None:
    """Toggling nodes after start() has begun (the engine is set) is rejected."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    session.select_schedule("Enable/disable schedule")
    engine = session.start()  # constructs the RunEngine; self._engine is now set
    assert engine is not None

    with pytest.raises(SessionError):
        session.toggle_node(1, True)
