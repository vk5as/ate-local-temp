"""Packet 29a review tests: every value ATE stores as text is stored the way Lua prints it.

`ate-table-reference.md` ("Results") says Actual/Expected "may be a number, string or boolean, and
ATE stores them as text using `tostring`". Python's ``str`` is not Lua's ``tostring``: it writes
``True``/``False``, and its float repr differs from Lua's (`0.3333333333333333` vs Lua's
`0.33333333333333331`). The conversion therefore happens on the Lua side, at the point the value
leaves the runtime, so the text in the record is exactly what the schedule printed. A step's
recorded Step Input value and every `ATE.log` argument go through the same one function.

A real `.tslua` release is run end to end through :class:`HeadlessRunSession` and the `attempt` and
`input_value` rows are read back.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication

from ate.run_session.headless import HeadlessRunSession, SessionState

# One step per kind of value: booleans, a fraction, big floats, an integer, and a typed FLOAT input
# the Operator supplies. `true`/`false`, `1/3` and `1e20` are the ones Python's str() gets wrong.
VALUES_SRC = """\
return ScheduleParams("Text values", "how a reported value reaches the record as text")
  :setSchedule({
    StepParams("booleans"):setStepNo("1.")
      :setAction(function()
        ATE.log(Log.INFO, "fan running is", true)
        ATE.report(Result.PASS, "fan running", true, false)
      end),

    StepParams("a fraction"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "one third", 1/3, 1/3) end),

    StepParams("big numbers"):setStepNo("3.")
      :setAction(function() ATE.report(Result.PASS, "counts of charge", 1e20, 2^53) end),

    StepParams("whole numbers"):setStepNo("4.")
      :setAction(function() ATE.report(Result.PASS, "a float and an integer", 1.0, 42) end),

    -- The Operator's answer arrives as a Python float; the text stored is Lua's tostring of the
    -- typed value the schedule reads back, not Python's repr of the object the host held.
    StepParams("settling time"):setStepNo("5.")
      :setInputs({ [1] = { desc = "settling time, seconds", type = "FLOAT", range = { min = 0.1, max = 10 } } })
      :setAction(function()
        local seconds = ATE.input(1)
        ATE.report(Result.PASS, "settled", seconds, seconds)
      end),
  })
"""


def _config(tmp_path: Path) -> Path:
    root = tmp_path / "defs"
    root.mkdir(parents=True, exist_ok=True)
    (root / "values.tslua").write_text(VALUES_SRC, encoding="utf-8")
    (root / "values.atedef").write_text(
        json.dumps(
            {
                "format": 1,
                "mediaNumber": "MR-TEXT",
                "name": "Text values release",
                "version": "1.0.0",
                "files": [{"path": "values.tslua", "sha256": _sha(root / "values.tslua")}],
            }
        ),
        encoding="utf-8",
    )
    config = {
        "format": 1,
        "station": {"name": "BENCH-TEXT", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c30"},
        "database": {"type": "sqlite", "path": str(tmp_path / "ate.db")},
        "reports": {"directory": str(tmp_path / "reports")},
        "testDefinitions": {"directories": [str(root)]},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def session(qapp: QCoreApplication, tmp_path_factory) -> HeadlessRunSession:
    """The text-values release run once, with step 5's FLOAT input answered by the harness."""
    run = HeadlessRunSession(_config(tmp_path_factory.mktemp("text-values")))

    def _answer(node_seq: int, attempt_no: int, index: int) -> None:
        run.supply_input(node_seq, attempt_no, index, 1 / 3)

    run.input_asked.connect(_answer)
    written: list[tuple[str, str]] = []
    run.report_written.connect(lambda html, pdf: written.append((html, pdf)))
    finished = {"done": False}
    run.state_changed.connect(lambda state: finished.__setitem__("done", state is SessionState.FINISHED))
    run.select_schedule("Text values")
    run.start()

    deadline = time.monotonic() + 20.0
    while not (finished["done"] and written):
        qapp.processEvents()
        if time.monotonic() > deadline:
            raise TimeoutError("the text-values run never finished with its report written")
        time.sleep(0.005)
    yield run


def _attempt_by_step(session: HeadlessRunSession) -> dict[str, dict]:
    """Step Number -> that step's single Attempt row."""
    nodes = {n["step_number"]: n["node_seq"] for n in session.store.nodes(session.run_uuid) if n["kind"] == "step"}
    attempts = session.store.attempts(session.run_uuid)
    by_seq = {a["node_seq"]: a for a in attempts}
    return {number: by_seq[seq] for number, seq in nodes.items()}


def test_booleans_are_stored_as_lua_true_and_false(session: HeadlessRunSession) -> None:
    """Lua's `true` becomes "true", not Python's "True"."""
    step = _attempt_by_step(session)["1."]
    assert (step["actual"], step["expected"]) == ("true", "false")
    assert step["pass_fail"] == "PASS"


def test_log_arguments_go_through_lua_tostring(session: HeadlessRunSession) -> None:
    """`ATE.log(Log.INFO, "fan running is", true)` records "fan running is true"."""
    lines = [line["text"] for line in session.store.log_lines(session.run_uuid) if line["level"] == "INFO"]
    assert "fan running is true" in lines, lines


def test_floats_are_stored_as_lua_prints_them(session: HeadlessRunSession) -> None:
    """1/3, 1e20, 2^53 and 1.0 get Lua's repr: Python's would be 0.3333333333333333."""
    steps = _attempt_by_step(session)
    assert (steps["2."]["actual"], steps["2."]["expected"]) == ("0.33333333333333331", "0.33333333333333331")
    assert (steps["3."]["actual"], steps["3."]["expected"]) == ("1e+20", "9007199254740992.0")
    assert (steps["4."]["actual"], steps["4."]["expected"]) == ("1.0", "42")


def test_a_report_with_no_actual_stores_nothing(session: HeadlessRunSession) -> None:
    """nil stays nil: `ATE.report(Result.PASS, "msg")` records no Actual/Expected text."""
    steps = _attempt_by_step(session)
    assert steps["5."]["msg"] == "settled"


def test_the_recorded_input_value_uses_the_same_text(session: HeadlessRunSession) -> None:
    """The FLOAT the Operator supplied is stored as Lua prints it, not as Python reprs the float."""
    rows = [(r["input_index"], r["type"], r["value"]) for r in session.store.input_values(session.run_uuid)]
    assert rows == [(1, "FLOAT", "0.33333333333333331")]
    assert _attempt_by_step(session)["5."]["actual"] == "0.33333333333333331"
