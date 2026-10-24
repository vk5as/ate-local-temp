"""Packet 12c seam-1: Retry reruns a failed step's action from the start as a new Attempt (no
limit), every Attempt is recorded in its own attempt row with its final report values, and the
step's result is its last Attempt's — so a step that passes on retry no longer fails the run.

A failing step with Stop on Fail blocks the worker thread on a failure_prompt; the driver answers
it through engine.choose() from another thread, feeding it a scripted sequence of choices (RETRY
then ABORT, ...). Every attempt row, log line and input value must carry its own Attempt's number.
Verified against a real SQLite file with real .tslua schedules — no mocks."""

import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QCoreApplication

from ate.engine.run_engine import RunEngine, RunStatus
from ate.storage.store import RunStore


def _drive_with_choices(
    app: QCoreApplication,
    source: str,
    choices: list[str],
    stop_on_fail: bool = True,
    values_by_index: dict[int, float] | None = None,
) -> tuple[RunStore, dict, list[tuple[int, int]], list[tuple[int, int, int]]]:
    """Drive ``source`` on a fresh engine; answer the n-th Stop-on-Fail prompt with ``choices[n-1]``.

    Returns ``(store, run_row, prompts, asks)``: ``prompts`` is the (node_seq, attempt_no) of each
    failure_prompt in order and ``asks`` the (node_seq, attempt_no, index) of each input_asked. The
    pump loop runs in lock-step with the worker so the queued failure_prompt -> choose delivery
    wakes the blocked worker deterministically before we inspect rows (a bare QThread.wait races
    the step boundary)."""
    captured = {}
    prompts: list[tuple[int, int]] = []
    asks: list[tuple[int, int, int]] = []

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    def _on_failure(node_seq: int, attempt_no: int) -> None:
        prompts.append((node_seq, attempt_no))
        engine.choose(choices[len(prompts) - 1])  # answer the pending prompt; wakes the worker

    def _on_input_asked(node_seq: int, attempt_no: int, index: int) -> None:
        asks.append((node_seq, attempt_no, index))
        op = engine.operator_input
        key = index - 1  # 0-based position into this step's declarations
        value = values_by_index[key] if values_by_index is not None else float(key)
        op.supply_input(key, value)

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8")
        engine = RunEngine(store, str(schedule_path), stop_on_fail=stop_on_fail)
        engine.run_uuid_reported.connect(_capture)
        engine.failure_prompt.connect(_on_failure)
        engine.input_asked.connect(_on_input_asked)

        finished = threading.Event()
        engine.state_changed.connect(lambda status: finished.set() if status == RunStatus.FINISHED else None)

        engine.start()
        deadline = time.time() + 10.0
        while not finished.is_set():
            app.processEvents()
            if time.time() > deadline:
                raise TimeoutError("engine did not reach FINISHED within 10 s")

    return store, store.get_test_run(captured["run_uuid"]), prompts, asks


def _nodes(store: RunStore, run_uuid: str) -> dict[str, dict]:
    conn = store._conn
    conn.row_factory = sqlite3.Row
    return {
        n["step_number"]: dict(n)
        for n in conn.execute(
            "SELECT step_number, result, attempt_count FROM node WHERE run_uuid=? ORDER BY node_seq",
            (run_uuid,),
        ).fetchall()
    }


def _attempts(store: RunStore, run_uuid: str, node_seq: int = 0) -> list[dict]:
    conn = store._conn
    conn.row_factory = sqlite3.Row
    return [
        dict(a)
        for a in conn.execute(
            "SELECT attempt_no, pass_fail, msg, actual, expected, prompt_choice FROM attempt "
            "WHERE run_uuid=? AND node_seq=? ORDER BY attempt_no",
            (run_uuid, node_seq),
        ).fetchall()
    ]


def _drive_poll_retry(
    app: QCoreApplication, source: str, texts: dict[int, str]
) -> tuple[RunStore, dict, list[tuple[int, int]], list[tuple[int, int, int]]]:
    """Drive a Stop-on-Fail POLL schedule, clicking ``texts[attempt_no]`` on each Attempt's POLL ask
    and answering every failure prompt RETRY. ``_drive_with_choices`` can't be reused: its blocking
    ``supply_input`` path rejects POLL. Returns ``(store, run_row, prompts, asks)``."""
    captured = {}
    prompts: list[tuple[int, int]] = []
    asks: list[tuple[int, int, int]] = []

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    def _on_failure(node_seq: int, attempt_no: int) -> None:
        prompts.append((node_seq, attempt_no))
        engine.choose("RETRY")

    def _on_input_asked(node_seq: int, attempt_no: int, index: int) -> None:
        asks.append((node_seq, attempt_no, index))
        # input_asked is queued before the action runs; wait so its first POLL read is nil.
        time.sleep(0.05)
        engine.operator_input.click_poll(node_seq, attempt_no, index - 1, texts[attempt_no])

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8")
        engine = RunEngine(store, str(schedule_path), stop_on_fail=True)
        engine.run_uuid_reported.connect(_capture)
        engine.failure_prompt.connect(_on_failure)
        engine.input_asked.connect(_on_input_asked)

        finished = threading.Event()
        engine.state_changed.connect(lambda status: finished.set() if status == RunStatus.FINISHED else None)

        engine.start()
        deadline = time.time() + 10.0
        while not finished.is_set():
            app.processEvents()
            if time.time() > deadline:
                raise TimeoutError("engine did not reach FINISHED within 10 s")

    return store, store.get_test_run(captured["run_uuid"]), prompts, asks


def test_retry_then_pass_makes_run_pass_and_records_two_attempts(app: QCoreApplication) -> None:
    """Packet 12c checkbox 2: RETRY reruns the action; Attempt 1 is FAIL (prompt_choice RETRY) and
    Attempt 2 PASS. The step's result is its last Attempt's, so the run ends PASS / COMPLETED."""
    store, row, prompts, _asks = _drive_with_choices(app, RETRY_THEN_PASS_SOURCE, ["RETRY"])

    assert row is not None
    assert row["result"] == "PASS"  # the retried step passed, so the run passes
    assert row["end_reason"] == "COMPLETED"
    assert prompts == [(0, 1)]  # paused once, announcing Attempt 1 of step 0

    nodes = _nodes(store, row["run_uuid"])
    assert nodes["1."]["result"] == "PASS"  # the step's result is its last Attempt's
    assert nodes["1."]["attempt_count"] == 2  # two Attempts executed
    assert nodes["2."]["result"] == "PASS"

    atts = _attempts(store, row["run_uuid"])
    assert len(atts) == 2  # each Attempt is its own attempt row
    assert atts[0]["attempt_no"] == 1
    assert atts[0]["pass_fail"] == "FAIL"
    assert atts[0]["prompt_choice"] == "RETRY"
    assert (atts[0]["actual"], atts[0]["expected"]) == ("Red", "Green")  # Attempt 1's final report
    assert atts[1]["attempt_no"] == 2
    assert atts[1]["pass_fail"] == "PASS"
    assert atts[1]["prompt_choice"] is None  # a passing Attempt never sat at a prompt
    assert (atts[1]["actual"], atts[1]["expected"]) == ("Blue", "Blue")  # Attempt 2's final report

    conn = store._conn
    conn.row_factory = sqlite3.Row
    log_nos = [
        r["attempt_no"]
        for r in conn.execute(
            "SELECT attempt_no FROM log_line WHERE run_uuid=? AND node_seq=0", (row["run_uuid"],)
        ).fetchall()
    ]
    assert set(log_nos) == {1, 2}  # log lines carry each Attempt's number


def test_retry_then_fail_then_abort(app: QCoreApplication) -> None:
    """Packet 12c checkbox 1/2: RETRY on a step that fails again pauses a second time, announcing
    Attempt 2; ABORT there keeps both Attempt rows and ends STOPPED_ON_FAIL / FAIL."""
    store, row, prompts, _asks = _drive_with_choices(app, ALWAYS_FAIL_SOURCE, ["RETRY", "ABORT"])

    assert row is not None
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "STOPPED_ON_FAIL"
    assert prompts == [(0, 1), (0, 2)]  # the rerun's failure announced Attempt 2

    nodes = _nodes(store, row["run_uuid"])
    assert nodes["1."]["result"] == "FAIL"
    assert nodes["1."]["attempt_count"] == 2
    assert nodes["2."]["result"] == "NOTRUN"  # remaining steps NOTRUN

    atts = _attempts(store, row["run_uuid"])
    assert [(a["attempt_no"], a["pass_fail"], a["prompt_choice"]) for a in atts] == [
        (1, "FAIL", "RETRY"),
        (2, "FAIL", "ABORT"),
    ]


def test_three_attempts_no_limit_and_continue_after_retry(app: QCoreApplication) -> None:
    """Packet 12c checkbox 1: Retry has no limit — a third Attempt is offered after two RETRYs;
    CONTINUE then keeps the step's last (FAIL) result and moves on, ending COMPLETED / FAIL."""
    store, row, prompts, _asks = _drive_with_choices(
        app, ALWAYS_FAIL_SOURCE, ["RETRY", "RETRY", "CONTINUE"]
    )

    assert row is not None
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "COMPLETED"  # Continue moved on past the third failure
    assert prompts == [(0, 1), (0, 2), (0, 3)]  # three pauses, one per Attempt

    nodes = _nodes(store, row["run_uuid"])
    assert nodes["1."]["result"] == "FAIL"  # still its last Attempt's result
    assert nodes["1."]["attempt_count"] == 3
    assert nodes["2."]["result"] == "PASS"  # moved on to the next step

    atts = _attempts(store, row["run_uuid"])
    assert [(a["attempt_no"], a["pass_fail"], a["prompt_choice"]) for a in atts] == [
        (1, "FAIL", "RETRY"),
        (2, "FAIL", "RETRY"),
        (3, "FAIL", "CONTINUE"),
    ]


def test_retry_reasks_inputs_with_the_new_attempt_number(app: QCoreApplication) -> None:
    """Packet 12c checkbox 2: a retried step re-announces its inputs with the new Attempt's number,
    and each Attempt's input values are recorded under that Attempt's rows."""
    store, row, prompts, asks = _drive_with_choices(
        app, RETRY_INPUTS_SOURCE, ["RETRY"], values_by_index={0: 5.0}
    )

    assert row is not None
    assert row["result"] == "PASS"
    assert prompts == [(0, 1)]
    assert asks == [(0, 1, 1), (0, 2, 1)]  # the same input asked once per Attempt, numbered

    conn = store._conn
    conn.row_factory = sqlite3.Row
    inputs = [
        dict(r)
        for r in conn.execute(
            "SELECT attempt_no, input_index, value FROM input_value WHERE run_uuid=? AND node_seq=0 "
            "ORDER BY seq",
            (row["run_uuid"],),
        ).fetchall()
    ]
    assert [i["attempt_no"] for i in inputs] == [1, 2]  # each Attempt's values under its number
    assert [i["input_index"] for i in inputs] == [1, 1]
    assert inputs[0]["value"] == inputs[1]["value"]  # the same supplied value, recorded per Attempt

    nodes = _nodes(store, row["run_uuid"])
    assert nodes["1."]["result"] == "PASS"
    assert nodes["1."]["attempt_count"] == 2


def test_poll_state_resets_for_each_attempt(app: QCoreApplication) -> None:
    """Packet 12c: a retried Attempt starts with a fresh POLL window — Attempt 2 reads nil until
    clicked again (no sticky value from Attempt 1), and each click is recorded under its own Attempt."""
    store, row, prompts, asks = _drive_poll_retry(app, RETRY_POLL_SOURCE, {1: "ok", 2: "ok2"})

    assert row is not None
    assert row["result"] == "PASS"
    assert row["end_reason"] == "COMPLETED"
    assert prompts == [(0, 1)]
    assert asks == [(0, 1, 1), (0, 2, 1)]

    atts = _attempts(store, row["run_uuid"])
    assert [(a["attempt_no"], a["pass_fail"]) for a in atts] == [(1, "FAIL"), (2, "PASS")]
    assert atts[0]["msg"] == "attempt 1 saw ok"
    assert atts[1]["msg"] == "attempt 2 saw ok2"  # Attempt 1's "ok" did not stick

    conn = store._conn
    conn.row_factory = sqlite3.Row
    inputs = [
        dict(r)
        for r in conn.execute(
            "SELECT attempt_no, input_index, value FROM input_value WHERE run_uuid=? AND node_seq=0 "
            "ORDER BY seq",
            (row["run_uuid"],),
        ).fetchall()
    ]
    assert [i["attempt_no"] for i in inputs] == [1, 2]  # one click per Attempt, under its number
    assert [i["value"] for i in inputs] == ["ok", "ok2"]

    nodes = _nodes(store, row["run_uuid"])
    assert nodes["1."]["result"] == "PASS"
    assert nodes["1."]["attempt_count"] == 2


# --- schedules ----------------------------------------------------------------
#
# ``local attempts = 0`` is a schedule-level local; each action closure captures it as an upvalue,
# so re-running the same action on a retry increments the shared count — the cleanest way to make
# Attempt 1 fail and later Attempts pass.


RETRY_THEN_PASS_SOURCE = """\
local attempts = 0
return ScheduleParams("Retry then pass schedule", "first attempt fails, the retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("colour"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        ATE.log(Log.INFO, "attempt " .. attempts .. " running") -- one line per Attempt, owned by it
        if attempts == 1 then
          ATE.report(Result.FAIL, "wrong colour", "Red", "Green")
        else
          ATE.report(Result.PASS, "ok now", "Blue", "Blue")
        end
      end),
    StepParams("after"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "ok", "Blue", "Blue") end),
  })
"""


# Fails on every attempt: retrying never fixes it, so the operator's choices decide the ending.
ALWAYS_FAIL_SOURCE = """\
return ScheduleParams("Always fail schedule", "the step fails on every attempt")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("broken"):setStepNo("1.")
      :setAction(function() ATE.report(Result.FAIL, "still wrong", "Red", "Green") end),
    StepParams("after"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "ok", "Blue", "Blue") end),
  })
"""


# A step with one blocking INTEGER input that fails on its first attempt and passes on retry.
RETRY_INPUTS_SOURCE = """\
local attempts = 0
return ScheduleParams("Retry inputs schedule", "asks for a voltage; first attempt fails, the retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("voltage"):setStepNo("1.")
      :setInputs({ [1] = { desc = "voltage", type = "INTEGER" } })
      :setAction(function()
        attempts = attempts + 1
        local v = ATE.input(1)
        if attempts == 1 then
          ATE.report(Result.FAIL, "wrong voltage", "Red", "Green")
        else
          ATE.report(Result.PASS, "voltage " .. v)
        end
      end),
  })
"""


# A step whose only input is a POLL button: the action spins until it is clicked, fails on the first
# Attempt and passes on the retry, reporting the text each Attempt saw.
RETRY_POLL_SOURCE = """\
local attempts = 0
return ScheduleParams("Retry poll schedule", "a POLL step that fails first and passes on retry")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("status"):setStepNo("1.")
      :setInputs({ [1] = { desc = "device status", type = "POLL", prompt = {"ok", "ok2"} } })
      :setAction(function()
        attempts = attempts + 1
        local s = ATE.input(1)
        while s == nil do s = ATE.input(1) end
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt 1 saw " .. s)
        else
          ATE.report(Result.PASS, "attempt 2 saw " .. s)
        end
      end),
  })
"""
