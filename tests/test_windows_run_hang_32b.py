"""Packet 32b: what an abort does to a run that is about to pause or has many steps left.

The Windows CI failure this packet follows up: a run missed FINISHED, and abort then did not end its
worker in 10 s. These tests pin the two product faults found on the way, and the report a stuck
worker now leaves behind. Real lupa runtime, QThread and SQLite file; only the stalls are injected."""

import threading
import time
from pathlib import Path

from ate.engine.run_engine import RunEngine, RunStatus, _live_engines
from ate.storage.store import RunStore
from tests._fixtures import describe_stuck_runs, scratch_directory


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


def test_an_abort_that_lands_just_before_a_pause_still_ends_the_run(qapp):
    """The Abort button asks `pause_pending()`, sees no pause, and sets the flag; the failing step then
    parks. Nothing read the flag there, so the worker waited for an answer that had already been given."""
    with scratch_directory() as td:
        holder = {}

        def abort_now() -> bool:  # on the worker, after the check and before the failure's pause
            holder["engine"].request_abort()
            return True

        store, engine, finished, seen = _engine(
            td,
            _schedule(3, first_action='ATE.abort_now() ATE.report(Result.FAIL, "bad")'),
            ate_fns={"abort_now": abort_now},
        )
        holder["engine"] = engine
        engine.start()
        try:
            assert _pump_until(qapp, finished, 5.0), f"abort did not end the run\n{describe_stuck_runs()}"
            run = store.get_test_run(seen["run_uuid"])
            assert (run["result"], run["end_reason"]) == ("FAIL", "STOPPED_ON_FAIL")
            assert store.attempts(seen["run_uuid"])[0]["prompt_choice"] == "ABORT"
            assert [n["result"] for n in store.nodes(seen["run_uuid"])] == ["FAIL", "NOTRUN", "NOTRUN"]
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
            writes = []
            real_run = store._run
            store._run = lambda stmt: (writes.append(stmt) if getattr(stmt, "is_dml", False) else None, real_run(stmt))[1]
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


def test_a_big_batch_of_nodes_stays_inside_sqlites_variable_limit():
    with scratch_directory() as td:
        store = RunStore(Path(td) / "ate.db")
        run_uuid = store.insert_test_run({"operator": "o", "project": "p"})["run_uuid"]
        store.insert_nodes(run_uuid, [{"node_seq": i, "kind": "step", "result": "NOTRUN"} for i in range(3000)])
        assert len(store.nodes(run_uuid)) == 3000


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
