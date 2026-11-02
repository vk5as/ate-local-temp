"""14d review — an answer given before its ``ATE.input(n)`` ask is carried into the Attempt.

The run engine opens each Attempt with a clean ``OperatorInput`` window. An answer supplied in the
gap between STARTED and the step's first ask — an operator who typed ahead, or a harness driving the
engine directly — landed in ``pending`` and was wiped before the ask ever started. The worker then
waited for a value nobody would give, :meth:`OperatorInput.wait_for_asks` never returned, and the run
hung on that step with its status bar frozen on the Current timer. That is the flake in
``test_total_time_advances``, which answers the step as soon as the bar has ticked: on a loaded
runner the worker reaches the reset first, the answer is lost, and the test's own
"the run never finished once its input was answered" fires.

Every test here drives the engine through a real .tslua schedule on disk — no mocks — and the
Pre-Test Action parks the worker so the supply provably arrives before the ask."""

import time
from pathlib import Path

from ate.engine.operator_input import OperatorInput
from ate.engine.run_engine import RunEngine
from ate.storage.store import RunStore

# The Pre-Test Action sleeps, so the supply lands while the worker is parked and the step has not
# reached its ATE.input(1) yet.
EARLY_ANSWER = """\
return ScheduleParams("Answer before the ask", "a value supplied before ATE.input runs")
  :setSchedule({
    StepParams("wait for a voltage"):setStepNo("1.")
      :setInputs({ [1] = { desc = "voltage", type = "INTEGER" } })
      :setAction(function() local v = ATE.input(1); ATE.report(Result.PASS, "got " .. v) end),
  })
  :setPreTestAction(function(passFail) ATE.sleep(0.5) end)
"""

# Same shape, but the step declares no inputs at all: nothing is eligible to be carried.
NO_INPUTS = """\
return ScheduleParams("No inputs", "a step that asks for nothing")
  :setSchedule({
    StepParams("no asking"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "nothing asked") end),
  })
  :setPreTestAction(function(passFail) ATE.sleep(0.5) end)
"""

# A step that fails, so Stop-on-Fail can RETRY it: Attempt 2 must get its own answer, and the
# second answer is the one that passes.
RETRY_ANSWER = """\
return ScheduleParams("Retry re-asks", "each Attempt answers for itself")
  :setSchedule({
    StepParams("report what was supplied"):setStepNo("1.")
      :setInputs({ [1] = { desc = "attempt", type = "INTEGER" } })
      :setAction(function() local v = ATE.input(1)
        ATE.report((v == 8) and Result.PASS or Result.FAIL, "attempt " .. v) end),
  })
  :setPreTestAction(function(passFail) ATE.sleep(0.5) end)
"""


def _engine(tmp_path: Path, source: str, stop_on_fail: bool = False):
    """A real RunEngine over a schedule written to disk, with its own store."""
    path = tmp_path / "schedule.tslua"
    path.write_text(source, encoding="utf-8", newline="\n")
    store = RunStore(tmp_path / "ate.db")
    return RunEngine(store, str(path), stop_on_fail=stop_on_fail), store


def _start(qapp, engine) -> str:
    """Start the worker and hand back the run uuid as soon as the engine announces it."""
    uuid = {}
    engine.run_uuid_reported.connect(lambda u: uuid.setdefault("run", u))
    engine.start()
    deadline = time.time() + 5.0
    while "run" not in uuid and time.time() < deadline:
        qapp.processEvents()
    assert "run" in uuid, "the engine never announced a run uuid"
    return uuid["run"]


def _pump_until(qapp, predicate, timeout=5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        qapp.processEvents()
    return False


def test_an_answer_given_before_the_ask_is_not_wiped(qapp, tmp_path):
    """The supply arrives while the Pre-Test Action is parked; the run must still finish with it."""
    engine, store = _engine(tmp_path, EARLY_ANSWER)
    run = _start(qapp, engine)

    assert not engine.operator_input.is_waiting(0), (
        "the step had already asked, so this is not the before-the-ask race"
    )
    engine.operator_input.supply_input(0, 7)

    assert engine.wait(10_000), "the run hung: the answer given before the ask was cleared by the reset"
    qapp.sendPostedEvents()
    qapp.processEvents()

    assert [(a["pass_fail"], a["msg"]) for a in store.attempts(run)] == [("PASS", "got 7")]
    assert [(r["input_index"], r["type"], r["value"]) for r in store.input_values(run)] == [
        (1, "INTEGER", "7")
    ]


def test_an_answer_for_an_index_the_step_never_asks_is_still_dropped(qapp, tmp_path):
    """The carry-over is scoped to the indices this Attempt will ask for; a stray supply is cleared."""
    engine, store = _engine(tmp_path, NO_INPUTS)
    run = _start(qapp, engine)
    engine.operator_input.supply_input(0, "spurious")

    assert engine.wait(10_000), "the run with no inputs hung"
    qapp.sendPostedEvents()
    qapp.processEvents()
    assert store.input_values(run) == []


def test_a_retried_attempt_answers_from_its_own_supply(qapp, tmp_path):
    """RETRY re-asks: Attempt 2 inherits nothing from Attempt 1's answer."""
    engine, store = _engine(tmp_path, RETRY_ANSWER, stop_on_fail=True)
    run = _start(qapp, engine)

    assert _pump_until(qapp, lambda: engine.operator_input.is_waiting(0)), (
        "Attempt 1 never opened its ask"
    )
    engine.operator_input.supply_input(0, 7)
    assert _pump_until(qapp, lambda: engine.pause_pending()), "the failing Attempt never paused"
    engine.choose("RETRY")

    assert _pump_until(qapp, lambda: engine.operator_input.is_waiting(0)), (
        "Attempt 2 never opened its own ask"
    )
    engine.operator_input.supply_input(0, 8)
    assert engine.wait(10_000), "the retried run hung"
    qapp.sendPostedEvents()
    qapp.processEvents()

    rows = {(r["attempt_no"], r["value"]) for r in store.input_values(run)}
    assert rows == {(1, "7"), (2, "8")}, rows
    assert [(a["attempt_no"], a["pass_fail"], a["prompt_choice"]) for a in store.attempts(run)] == [
        (1, "FAIL", "RETRY"),
        (2, "PASS", None),
    ]


def test_reset_keeps_exactly_the_indices_it_is_given():
    """The controller's own contract: kept indices survive, everything else is cleared."""
    op = OperatorInput()
    op.supply_input(0, "for the step about to run")
    op.supply_input(1, "for some other step")

    op.reset(keep=[0])
    assert op.pending == {0: "for the step about to run"}
    # the carried answer is still attributed to the Attempt that will record it
    assert op.supplied_inputs() == [(0, "for the step about to run")]

    op.reset()
    assert op.pending == {}
    assert op.supplied_inputs() == []


def test_a_carried_answer_satisfies_the_ask_without_waiting():
    """``get_raw`` returns a carried answer at once, which is what stops the run hanging."""
    op = OperatorInput()
    op.supply_input(0, 7)
    op.reset(keep=[0])
    assert op.get_raw(0) == 7
    assert op._outstanding == 0, "the carried answer must not count as an outstanding ask"
