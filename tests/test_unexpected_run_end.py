"""A run the worker cannot finish: the Operator can see why, and the run row closes at once.

An exception leaving the run engine used to reach stderr alone, with the row left open until the
next launch's recovery closed it.
"""


from PyQt6.QtWidgets import QApplication

from ate.engine.run_engine import RunEngine
from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.storage.store import RunStore

from ._fixtures import write_ate_env


def _pump(predicate, what: str, timeout: float = 20.0) -> None:
    """Deliver the worker's signals until ``predicate`` holds; never landing is a failure."""
    deadline = time.time() + timeout
    while not predicate():
        assert time.time() < deadline, what
        QApplication.processEvents()
        time.sleep(0.005)


def _broken_run(tmp_path) -> tuple[HeadlessRunSession, list[str]]:
    """Start a run on a session with an application log, and return what its report signal says."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    assert session.on_log is not None, "this test is about the application log"
    written: list[str] = []
    session.report_written.connect(lambda html, _pdf: written.append(html))
    session.select_schedule("Passing schedule")
    session.set_uut_type("U")
    session.set_uut_serial("S")
    session.set_operator("O")
    session.set_project("P")
    session.start()
    return session, written


def _finished(session: HeadlessRunSession) -> None:
    _pump(lambda: session.state == SessionState.FINISHED, "the screen was left waiting on a worker that has gone")


def _app_log_text(session: HeadlessRunSession) -> str:
    return session._app_log.path.read_text(encoding="utf-8")


def test_a_worker_that_raises_leaves_a_closed_run_a_logged_traceback_and_a_report(tmp_path, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("the wiring is broken")

    monkeypatch.setattr(RunEngine, "_insert_node", broken)
    session, written = _broken_run(tmp_path)
    _finished(session)

    row = session.store.get_test_run(session.run_uuid)
    assert row["ended_at"] is not None, "the run row stayed open"
    assert row["result"] == "FAIL" and row["end_reason"] == "INTERRUPTED", row

    errors = [line for line in session.store.log_lines(session.run_uuid) if line["level"] == "ERROR"]
    assert len(errors) == 1 and "Traceback" in errors[0]["text"], errors
    assert "the wiring is broken" in errors[0]["text"], errors
    assert errors[0]["node_seq"] is None, "the error belongs to the run, not a step"

    logged = _app_log_text(session)
    assert " ERROR " in logged and "Traceback" in logged and "the wiring is broken" in logged, logged

    _pump(lambda: written, "no Test Report was written")
    with open(written[0], encoding="utf-8") as report:
        assert "INTERRUPTED" in report.read()


def test_a_worker_that_raises_before_the_run_row_exists_is_still_logged(tmp_path, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("no row for you")

    monkeypatch.setattr(RunStore, "insert_test_run", broken)
    session, _written = _broken_run(tmp_path)
    _finished(session)

    assert session.run_uuid is None
    assert "no row for you" in _app_log_text(session)


def test_a_store_that_cannot_log_still_closes_the_run_and_finishes(tmp_path, monkeypatch):
    """The exception may be the store's: one failed step of the ending does not stop the next."""
    def broken(*_args, **_kwargs):
        raise RuntimeError("the wiring is broken")

    monkeypatch.setattr(RunEngine, "_insert_node", broken)
    monkeypatch.setattr(RunStore, "insert_log_lines", broken)
    session, _written = _broken_run(tmp_path)
    _finished(session)

    row = session.store.get_test_run(session.run_uuid)
    assert row["ended_at"] is not None and row["end_reason"] == "INTERRUPTED", row
    assert "the wiring is broken" in _app_log_text(session)


def test_a_run_failing_before_it_announces_its_key_still_gets_its_report(tmp_path, monkeypatch):
    """The row exists but the session was never told its key: the report needs it."""
    def broken(*_args, **_kwargs):
        raise RuntimeError("the wiring is broken")

    monkeypatch.setattr(RunStore, "insert_schedule_instance", broken)
    session, written = _broken_run(tmp_path)
    _finished(session)
    _pump(lambda: written, "the session never learned which run ended")
    assert session.store.get_test_run(session.run_uuid)["end_reason"] == "INTERRUPTED"
