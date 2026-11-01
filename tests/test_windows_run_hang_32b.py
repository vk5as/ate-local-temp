"""Packet 32b: what an abort does to a run that is about to pause or has many steps left.

The Windows CI failure this packet follows up: a run missed FINISHED, and abort then did not end its
worker in 10 s. These tests pin the two product faults found on the way, and the report a stuck
worker now leaves behind. Real lupa runtime, QThread and SQLite file; only the stalls are injected."""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ate.engine.run_engine import RunEngine, RunStatus, _live_engines
from ate.report.html import render_report
from ate.run_session.headless import HeadlessRunSession
from ate.storage import schema, store as store_module
from ate.storage.store import RunStore
from ate.ui.staged_run_screen import StagedRunScreen
from ate.ui.three_pane_run_screen import ThreePaneRunScreen
from tests import _fixtures as fixtures_module
from tests.test_three_pane_run_screen_25a import _pick, _settle, _start_staged_run, _wait_finished
from tests._fixtures import WRITES_PDF, describe_stuck_runs, fake_pdf_helper, scratch_directory, write_ate_env


def _schedule(steps: int, *, first_action: str = 'ATE.report(Result.PASS, "ok")', stop_on_fail: bool = True) -> str:
    rows = [f'StepParams("step 1"):setStepNo("1."):setAction(function() {first_action} end)']
    rows += [
        f'StepParams("step {i}"):setStepNo("{i}."):setAction(function() ATE.report(Result.PASS, "ok") end)'
        for i in range(2, steps + 1)
    ]
    return (
        f'return ScheduleParams("s", "d"):setStopOnFail({str(stop_on_fail).lower()})\n'
        ":setSchedule({\n" + ",\n".join(rows) + "\n})\n"
    )


def _engine(td: str, source: str, **kwargs) -> tuple[RunStore, RunEngine, threading.Event, dict]:
    store = RunStore(Path(td) / "ate.db")
    path = Path(td) / "schedule.tslua"
    path.write_text(source, encoding="utf-8", newline="\n")
    engine = RunEngine(store, str(path), stop_on_fail=True, **kwargs)
    finished, seen = threading.Event(), {}
    engine.run_uuid_reported.connect(lambda run_uuid: seen.update(run_uuid=run_uuid))
    engine.state_changed.connect(lambda status: finished.set() if status == RunStatus.FINISHED else None)
    return store, engine, finished, seen


def _pump_until(app, finished: threading.Event, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not finished.is_set() and time.monotonic() < deadline:
        app.processEvents()
        finished.wait(0.01)
    app.processEvents()
    return finished.is_set()


def _abort_lands_before_the_pause(td: str, steps: int = 3) -> tuple[RunStore, RunEngine, threading.Event, dict]:
    """A run whose first step aborts on the worker, after the Abort button's check and before its pause."""
    holder = {}

    def abort_now() -> bool:
        holder["engine"].request_abort()
        return True

    built = _engine(
        td,
        _schedule(steps, first_action='ATE.abort_now() ATE.report(Result.FAIL, "bad")'),
        ate_fns={"abort_now": abort_now},
    )
    holder["engine"] = built[1]
    return built


def test_an_abort_that_lands_just_before_a_pause_still_ends_the_run_aborted(qapp):
    """The Abort button asks `pause_pending()`, sees no pause, and sets the flag; the failing step then
    reaches its pause. Nothing read the flag there, so the worker waited for an answer already given.
    The run ends ABORTED, the step that really failed keeps its FAIL, and it finished before the abort,
    so it gets no "Aborted by Operator" Attempt and no Operator choice that nobody made."""
    with scratch_directory() as td:
        store, engine, finished, seen = _abort_lands_before_the_pause(td)
        prompts = []
        engine.failure_prompt.connect(lambda *pause: prompts.append(pause))
        engine.start()
        try:
            assert _pump_until(qapp, finished, 5.0), f"abort did not end the run\n{describe_stuck_runs()}"
            run = store.get_test_run(seen["run_uuid"])
            assert (run["result"], run["end_reason"]) == ("FAIL", "ABORTED")
            attempts = store.attempts(seen["run_uuid"])
            assert [(a["attempt_no"], a["pass_fail"], a["prompt_choice"]) for a in attempts] == [(1, "FAIL", None)]
            assert [n["result"] for n in store.nodes(seen["run_uuid"])] == ["FAIL", "NOTRUN", "NOTRUN"]
            assert prompts == []  # the abort came first, so no prompt goes up to be taken straight down
        finally:
            engine.stop_and_wait()


def _paused_engine(qapp, td: str, steps: int = 2):
    """A run parked at a Stop-on-Fail pause on its first step, with the prompt announced."""
    store, engine, finished, seen = _engine(td, _schedule(steps, first_action='ATE.report(Result.FAIL, "bad")'))
    prompted = threading.Event()
    engine.failure_prompt.connect(lambda *_pause: prompted.set())
    engine.start()
    assert _pump_until(qapp, prompted, 5.0), describe_stuck_runs()
    return store, engine, finished, seen


def test_an_abort_during_a_pause_ends_it_aborted_with_no_choice_recorded(qapp):
    with scratch_directory() as td:
        store, engine, finished, seen = _paused_engine(qapp, td)
        answered = []
        engine.prompt_answered.connect(lambda *pause: answered.append(pause))
        try:
            started = time.monotonic()
            engine.request_abort()
            assert _pump_until(qapp, finished, 5.0), describe_stuck_runs()
            assert time.monotonic() - started < 5.0
            assert store.get_test_run(seen["run_uuid"])["end_reason"] == "ABORTED"
            assert [(a["pass_fail"], a["prompt_choice"]) for a in store.attempts(seen["run_uuid"])] == [("FAIL", None)]
            assert answered == [(0, 1, "")]  # the layouts still hear that the pause is over
        finally:
            engine.stop_and_wait()


def test_the_prompts_own_abort_still_ends_stopped_on_fail(qapp):
    """Packet 12b: this one is the Operator's choice, recorded as such."""
    with scratch_directory() as td:
        store, engine, finished, seen = _paused_engine(qapp, td)
        try:
            assert engine.choose("ABORT") == (0, 1)
            assert _pump_until(qapp, finished, 5.0), describe_stuck_runs()
            assert store.get_test_run(seen["run_uuid"])["end_reason"] == "STOPPED_ON_FAIL"
            assert store.attempts(seen["run_uuid"])[0]["prompt_choice"] == "ABORT"
        finally:
            engine.stop_and_wait()


@pytest.mark.parametrize("delay", [0.0, 0.01, 0.04, 0.06, 0.1])
def test_a_choice_after_an_abort_is_dropped_whenever_it_comes(qapp, delay):
    """Abort then CONTINUE: the order of the two decides, not where the worker's 50 ms poll happens to be."""
    with scratch_directory() as td:
        store, engine, finished, seen = _paused_engine(qapp, td)
        try:
            engine.request_abort()
            time.sleep(delay)
            assert engine.choose("CONTINUE") is None
            assert _pump_until(qapp, finished, 5.0), describe_stuck_runs()
            assert store.get_test_run(seen["run_uuid"])["end_reason"] == "ABORTED"
            assert [(a["pass_fail"], a["prompt_choice"]) for a in store.attempts(seen["run_uuid"])] == [("FAIL", None)]
            assert [n["result"] for n in store.nodes(seen["run_uuid"])] == ["FAIL", "NOTRUN"]
        finally:
            engine.stop_and_wait()


def test_a_real_answer_given_with_the_abort_is_not_overwritten(qapp):
    """An Operator's CONTINUE already taken by the pause stays what the Attempt records."""
    with scratch_directory() as td:
        store, engine, finished, seen = _engine(td, _schedule(2, first_action='ATE.report(Result.FAIL, "bad")'))
        engine.failure_prompt.connect(lambda *_seen: (engine.choose("CONTINUE"), engine.request_abort()))
        engine.start()
        try:
            assert _pump_until(qapp, finished, 5.0), describe_stuck_runs()
            assert store.attempts(seen["run_uuid"])[0]["prompt_choice"] == "CONTINUE"
        finally:
            engine.stop_and_wait()


def test_abort_costs_a_few_commits_however_many_steps_are_left(qapp):
    """Every autocommit statement is a synced commit. One per NOTRUN row made the abort's own cost grow
    with the steps left: 60 rows at the 200 ms a commit took in the reproduction is past story 83's 10 s."""
    with scratch_directory() as td:
        started = threading.Event()
        release = threading.Event()

        def hold() -> bool:
            started.set()
            release.wait(5.0)
            return True

        store, engine, finished, seen = _engine(
            td, _schedule(400, first_action="ATE.hold()"), ate_fns={"hold": hold}
        )
        engine.start()
        try:
            assert _pump_until(qapp, started, 5.0)
            writes = _dml_statements(store)
            engine.request_abort()
            release.set()
            assert _pump_until(qapp, finished, 10.0), describe_stuck_runs()
            nodes = store.nodes(seen["run_uuid"])
            assert [n["node_seq"] for n in nodes] == list(range(400))
            assert {n["result"] for n in nodes[2:]} == {"NOTRUN"}  # the abort lands in step 1 or at step 2's boundary
            assert all(n["attempt_count"] == 0 and n["step_name"] == f"step {i + 1}" for i, n in enumerate(nodes) if i > 1)
            assert len(writes) < 20, f"{len(writes)} commits to end an aborted 400-step run"
        finally:
            release.set()
            engine.stop_and_wait()


#: SQLite's lowest default for SQLITE_MAX_VARIABLE_NUMBER (before 3.32). A newer build accepts far more,
#: so a batch that merely succeeds here proves nothing: the statements themselves are measured.
_SQLITE_MINIMUM_VARIABLE_LIMIT = 999


def _dml_statements(store: RunStore) -> list:
    """Record every write the store makes from here on."""
    writes = []
    real_run = store._run

    def recording(stmt):
        if getattr(stmt, "is_dml", False):
            writes.append(stmt)
        return real_run(stmt)

    store._run = recording
    return writes


def test_a_big_batch_of_nodes_stays_inside_sqlites_variable_limit():
    with scratch_directory() as td:
        store = RunStore(Path(td) / "ate.db")
        run_uuid = store.insert_test_run({"operator": "o", "project": "p"})["run_uuid"]
        writes = _dml_statements(store)
        store.insert_nodes(run_uuid, [{"node_seq": i, "kind": "step", "result": "NOTRUN"} for i in range(3000)])
        assert len(store.nodes(run_uuid)) == 3000
        assert max(len(stmt.compile().params) for stmt in writes) <= _SQLITE_MINIMUM_VARIABLE_LIMIT


def test_a_big_batch_of_log_lines_is_one_commit_a_batch_and_keeps_its_order():
    with scratch_directory() as td:
        store = RunStore(Path(td) / "ate.db")
        run_uuid = store.insert_test_run({"operator": "o", "project": "p"})["run_uuid"]
        writes = _dml_statements(store)
        lines = [{"level": "INFO", "source": "ate", "text": f"line {i}"} for i in range(400)]
        store.insert_log_lines(run_uuid, 0, lines)
        store.insert_log_lines(run_uuid, 0, [{"level": "INFO", "source": "ate", "text": "after"}])
        recorded = store.log_lines(run_uuid)
        assert [line["text"] for line in recorded] == [f"line {i}" for i in range(400)] + ["after"]
        assert [line["seq"] for line in recorded] == list(range(401))
        per = store_module._rows_per_statement(store_module.log_line)
        assert len(writes) == (400 + per - 1) // per + 1  # one per chunk of the batch, one for the last line
        assert max(len(stmt.compile().params) for stmt in writes) <= _SQLITE_MINIMUM_VARIABLE_LIMIT


def test_a_row_batch_is_sized_to_the_table_it_writes():
    """The chunk size comes from the column count, not from a number tuned to `node`'s twelve columns.

    80 rows of 13 columns are 1040 variables: past SQLite's lowest limit of 999. A table that gains a
    column would have been written in statements SQLite refuses."""
    wide = schema.Table(
        "wide", schema.metadata, *[schema.Column(f"c{i}", schema.Integer) for i in range(13)]
    )
    assert store_module._rows_per_statement(wide) == _SQLITE_MINIMUM_VARIABLE_LIMIT // 13, "the bound is per column, not fixed"
    with scratch_directory() as td:
        store = RunStore(Path(td) / "ate.db")
        run_uuid = store.insert_test_run({"operator": "o", "project": "p"})["run_uuid"]
        writes = _dml_statements(store)
        store.insert_nodes(run_uuid, [{"node_seq": i, "kind": "step", "result": "NOTRUN"} for i in range(400)])
        per = store_module._rows_per_statement(store_module.node)
        assert len(writes) == (400 + per - 1) // per, f"{len(writes)} statements for 400 rows at {per} a statement"
        assert max(len(stmt.compile().params) for stmt in writes) <= _SQLITE_MINIMUM_VARIABLE_LIMIT


def test_a_log_batch_failing_part_way_through_keeps_the_log_counter_true(monkeypatch):
    """The seq counter moves with the chunks that committed, not with the batch that failed.

    A batch is several statements: when the second of three fails, 80 rows are in the table and a
    counter still at the batch's start makes the next write collide on (run_uuid, seq)."""
    with scratch_directory() as td:
        store = RunStore(Path(td) / "ate.db")
        run_uuid = store.insert_test_run({"operator": "o", "project": "p"})["run_uuid"]
        chunk = store_module._rows_per_statement(store_module.log_line)
        lines = [{"level": "INFO", "source": "ate", "text": f"line {i}"} for i in range(chunk * 3)]
        real_run, chunks = store._run, 0

        def fail_the_second_chunk(stmt):
            nonlocal chunks
            if getattr(stmt, "is_dml", False):
                chunks += 1
                if chunks == 2:
                    raise RuntimeError("the second chunk of the batch failed")
            return real_run(stmt)

        monkeypatch.setattr(store, "_run", fail_the_second_chunk)
        with pytest.raises(RuntimeError, match="second chunk"):
            store.insert_log_lines(run_uuid, 0, lines)
        monkeypatch.undo()
        assert chunks >= 2, "the batch never got to a second chunk, so nothing was left behind"

        store.insert_log_lines(run_uuid, 0, [{"level": "WARNING", "source": "ate", "text": "after the failure"}])
        recorded = store.log_lines(run_uuid)
        assert [line["text"] for line in recorded] == [f"line {i}" for i in range(chunk)] + ["after the failure"]
        seqs = [line["seq"] for line in recorded]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), f"seqs reused after a half-written batch: {seqs}"


def test_a_failed_pretest_action_costs_a_few_commits_however_many_steps_there_are(qapp):
    """Every step is written NOTRUN; a commit each was 406 commits on a 400-step schedule."""
    with scratch_directory() as td:
        source = _schedule(400).replace(
            'return ScheduleParams("s", "d")',
            'return ScheduleParams("s", "d"):setPreTestAction(function() error("no supply") end)',
        )
        store, engine, finished, seen = _engine(td, source)
        writes = _dml_statements(store)
        engine.start()
        try:
            assert _pump_until(qapp, finished, 10.0), describe_stuck_runs()
            assert store.get_test_run(seen["run_uuid"])["end_reason"] == "PRETEST_FAILED"
            nodes = store.nodes(seen["run_uuid"])
            assert [n["node_seq"] for n in nodes] == list(range(400))
            assert {n["result"] for n in nodes} == {"NOTRUN"}
            assert len(writes) < 20, f"{len(writes)} commits to record a failed Pre-test Action"
        finally:
            engine.stop_and_wait()


def test_an_abort_before_many_disabled_steps_costs_a_few_commits(qapp):
    """Disabled steps come before the boundary's abort check would find a step to stop on."""
    with scratch_directory() as td:
        started, release = threading.Event(), threading.Event()

        def hold() -> bool:
            started.set()
            release.wait(5.0)
            return True

        source = _schedule(400, first_action="ATE.hold()")
        store, engine, finished, seen = _engine(
            td, source, ate_fns={"hold": hold}, overrides={seq: False for seq in range(1, 399)}
        )
        engine.start()
        try:
            assert _pump_until(qapp, started, 5.0)
            writes = _dml_statements(store)
            engine.request_abort()
            release.set()
            assert _pump_until(qapp, finished, 10.0), describe_stuck_runs()
            nodes = store.nodes(seen["run_uuid"])
            assert [n["node_seq"] for n in nodes] == list(range(400))
            assert nodes[398]["result"] == "NOTRUN" and nodes[399]["result"] == "ERROR"  # the next step to run
            assert store.attempts(seen["run_uuid"])[-1]["msg"] == "Aborted by Operator"
            assert len(writes) < 20, f"{len(writes)} commits to end an aborted 400-step run"
        finally:
            release.set()
            engine.stop_and_wait()


@pytest.mark.parametrize("screen_class", [StagedRunScreen, ThreePaneRunScreen])
@pytest.mark.parametrize("order", ["abort_then_click", "click_then_abort"])
def test_the_screen_takes_a_click_and_an_abort_in_either_order(qapp, tmp_path, monkeypatch, screen_class, order):
    """Abort then Continue: the `None` from `choose` leaves no error and no prompt, and the record says
    nobody chose. Continue then abort: the choice was real, so it stays, and the next step takes the abort."""
    env = write_ate_env(tmp_path)
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", fake_pdf_helper(WRITES_PDF))
    session = HeadlessRunSession(env["config"])
    screen = screen_class(session)
    prompted, answered = [], []
    session.failure_prompt.connect(lambda *pause: prompted.append(pause))
    session.prompt_answered.connect(lambda *pause: answered.append(pause))
    screen.show()
    qapp.processEvents()
    if screen_class is StagedRunScreen:
        _start_staged_run(screen, "Fail then pass schedule")
    else:
        _pick(screen, "Fail then pass schedule")
        screen.start_button.click()
    deadline = time.monotonic() + 10.0
    while not prompted and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert prompted, describe_stuck_runs()
    qapp.processEvents()
    continue_button = screen._paused_prompt_widget()
    assert session.pause_pending() and continue_button is not None
    run_uuid = session.run_uuid

    if order == "abort_then_click":
        session.request_abort()
        continue_button.click()
    else:
        continue_button.click()
        session.request_abort()
    assert _wait_finished(qapp, session), describe_stuck_runs()
    assert screen._paused_prompt_widget() is None
    assert answered  # both layouts clear on this, whoever ended the pause

    attempts = session.store.attempts(run_uuid)
    end_reason = session.store.get_test_run(run_uuid)["end_reason"]
    assert attempts[0]["pass_fail"] == "FAIL"
    if order == "abort_then_click":
        assert end_reason == "ABORTED"
        assert attempts[0]["prompt_choice"] is None
    else:
        assert end_reason in ("ABORTED", "COMPLETED")  # the worker, freed by the click, may finish first
        assert attempts[0]["prompt_choice"] == "CONTINUE"
    _settle(qapp, session)


def test_the_report_shows_no_choice_for_a_pause_an_abort_ended(qapp, tmp_path, monkeypatch):
    """The report prints a choice beside each Attempt of a retried step. The real RETRY shows, and the
    Attempt whose pause the abort ended shows none. History shows no choice at all, only the End Reason."""
    env = write_ate_env(tmp_path)
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", fake_pdf_helper(WRITES_PDF))
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    prompted = []
    session.failure_prompt.connect(lambda *pause: prompted.append(pause))
    screen.show()
    qapp.processEvents()
    _start_staged_run(screen, "Fail then pass schedule")

    def pump_to_prompt(count: int) -> None:
        deadline = time.monotonic() + 10.0
        while len(prompted) < count and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        assert len(prompted) == count, describe_stuck_runs()

    pump_to_prompt(1)
    run_uuid = session.run_uuid
    session.choose("RETRY")
    pump_to_prompt(2)
    session.request_abort()
    assert _wait_finished(qapp, session), describe_stuck_runs()
    attempts = session.store.attempts(run_uuid)
    assert [(a["attempt_no"], a["pass_fail"], a["prompt_choice"]) for a in attempts] == [(1, "FAIL", "RETRY"), (2, "FAIL", None)]
    row = session.store.get_test_run(run_uuid)
    assert (row["result"], row["end_reason"]) == ("FAIL", "ABORTED")
    assert session.store.nodes(run_uuid)[0]["result"] == "FAIL"
    assert session.store.nodes(run_uuid)[0]["attempt_count"] == 2
    report = render_report(session.store, run_uuid, session._config)
    assert report.count("→ RETRY") == 1 and "→ ABORT" not in report and "→ None" not in report
    assert "ABORTED" in report
    _settle(qapp, session)


def test_a_stuck_worker_explains_itself(qapp):
    """The report names where every thread is and how the worker's writes went."""
    with scratch_directory() as td:
        started, release = threading.Event(), threading.Event()

        def stuck_in_this_function() -> bool:
            started.set()
            release.wait(10.0)
            return True

        store, engine, finished, _seen = _engine(
            td, _schedule(2, first_action="ATE.stuck()"), ate_fns={"stuck": stuck_in_this_function}
        )
        engine.start()
        try:
            assert _pump_until(qapp, started, 5.0)
            assert engine in _live_engines
            report = describe_stuck_runs()
            assert "stuck_in_this_function" in report  # the worker's stack
            assert "running=True" in report and "aborted=False" in report and "pause_pending=False" in report
            assert "store writes:" in report
        finally:
            release.set()
            engine.stop_and_wait()
            _pump_until(qapp, finished, 5.0)


_HOOK_TESTS = """
import threading
import time
from pathlib import Path

from ate.engine.run_engine import RunEngine
from ate.storage.store import RunStore
from tests import _fixtures


def _hold_a_worker(qapp, tmp_path):
    started, release = threading.Event(), threading.Event()

    def worker_is_held_here():
        started.set()
        release.wait(20.0)
        return True

    path = tmp_path / "schedule.tslua"
    path.write_text(
        'return ScheduleParams("s", "d"):setSchedule({ '
        'StepParams("one"):setStepNo("1."):setAction(function() ATE.hold() end) })\\n',
        encoding="utf-8",
        newline="\\n",
    )
    store = RunStore(tmp_path / "ate.db")
    engine = RunEngine(store, str(path), ate_fns={"hold": worker_is_held_here})
    engine.start()
    deadline = time.monotonic() + 10.0
    while not started.is_set() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    return engine, release


def test_fails_with_a_worker_running(qapp, tmp_path):
    engine, release = _hold_a_worker(qapp, tmp_path)
    try:
        assert False, "THE TEST'S OWN FAILURE"
    finally:
        threading.Timer(0.2, release.set).start()


def test_fails_when_the_report_itself_breaks(qapp, tmp_path, monkeypatch):
    engine, release = _hold_a_worker(qapp, tmp_path)
    monkeypatch.setattr(_fixtures, "describe_stuck_runs", lambda *_: 1 / 0)
    try:
        assert False, "THE SECOND TEST'S OWN FAILURE"
    finally:
        threading.Timer(0.2, release.set).start()
"""


def test_the_report_hook_adds_its_section_and_never_replaces_the_failure(tmp_path):
    """Drives the real `pytest_runtest_makereport` in a pytest of its own: a failure with a worker running
    gets the section, and a report that breaks while being written leaves the test's own failure standing."""
    root = Path(__file__).resolve().parent.parent
    test_file = tmp_path / "test_hook_32b.py"
    test_file.write_text(_HOOK_TESTS, encoding="utf-8", newline="\n")
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "conftest", "-c", str(root / "pyproject.toml"),
         f"--rootdir={root}", "--basetemp", str(tmp_path / "t"), str(test_file)],
        cwd=root, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(root), str(root / "src")]), "QT_QPA_PLATFORM": "offscreen"},
    )
    output = done.stdout + done.stderr
    assert "2 failed" in output, output
    assert "THE TEST'S OWN FAILURE" in output and "THE SECOND TEST'S OWN FAILURE" in output
    assert "RunEngine worker still running" in output and "worker_is_held_here" in output
    assert "could not describe the stuck run" in output and "ZeroDivisionError" in output
    assert "INTERNALERROR" not in output


def test_describe_stuck_runs_survives_each_part_failing(monkeypatch):
    def broken(*_args, **_kwargs):
        raise OSError("no file for you")

    monkeypatch.setattr(fixtures_module.faulthandler, "dump_traceback", broken)
    monkeypatch.setattr(fixtures_module.store_writes, "describe", broken)
    report = describe_stuck_runs()
    assert "stacks unavailable" in report and "no file for you" in report


def test_describe_engine_survives_an_engine_whose_cpp_side_has_gone():
    class Gone:
        def __getattr__(self, name):
            raise RuntimeError("Internal C++ object already deleted")

    assert "already deleted" in fixtures_module._describe_engine(Gone(), ())
