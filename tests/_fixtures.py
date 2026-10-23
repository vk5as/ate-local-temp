"""Shared helpers for ticket 02's seam tests: build a minimal on-disk ATE environment.

These write a config pointing at a fresh SQLite database and one Test Definition
directory holding three schedules (one that passes, one that fails, one that raises), so
the headless session and CLI can be exercised end-to-end against real files — no mocks.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from ate.engine.operator_input import OperatorInput
from ate.engine.run_engine import RunEngine, RunStatus


# A schedule that reports PASS on its single step.
PASSING = """\
return ScheduleParams("Passing schedule", "A step that passes")
  :setDocTitle("Passing Test Report")
  :setSchedule({
    StepParams("LED colour"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "ok", "Green", "Green") end),
  })
"""

# A schedule that reports FAIL on its single step.
FAILING = """\
return ScheduleParams("Failing schedule", "A step that fails")
  :setSchedule({
    StepParams("LED colour"):setStepNo("1.")
      :setAction(function() ATE.report(Result.FAIL, "wrong colour", "Red", "Green") end),
  })
"""

# A schedule whose first step raises a Lua error and whose second never runs.
ERRORING = """\
return ScheduleParams("Erroring schedule", "A step that raises a Lua error")
  :setSchedule({
    StepParams("boom"):setStepNo("1.")
      :setAction(function() error("kaboom from Lua") end),
    StepParams("never runs"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "should not run", "x", "y") end),
  })
"""

# A schedule whose steps log at every level and also use print — exercising ticket 08's
# ATE.log(Log.X, ...) recording and the print -> INFO redirection.
LOGGING = """\
return ScheduleParams("Logging schedule", "Steps that log at every level")
  :setSchedule({
    StepParams("info step"):setStepNo("1.")
      :setAction(function()
        ATE.log(Log.INFO, "step one running")
        print("printed here")
        ATE.report(Result.PASS)
      end),
    StepParams("warn step"):setStepNo("2.")
      :setAction(function() ATE.log(Log.WARNING, "value", 12) end),
    StepParams("error step"):setStepNo("3.")
      :setAction(function() ATE.log(Log.ERROR, "something failed") end),
  })
"""

# A schedule whose single step runs its action but never calls ATE.report. It must record a
# NOREPORT attempt and log one warning, so a forgotten report is visible.
NEVER_REPORTS = """\
return ScheduleParams("Never reports schedule", "A step that runs but never reports")
  :setSchedule({
    StepParams("silent step"):setStepNo("1.")
      :setAction(function() ATE.log(Log.INFO, "did some work") end),
  })
"""

# A schedule that calls ATE.report at the top level, outside any Pre-test Action or step. Per the
# "where each call is allowed" rule this must raise while the file loads: only the builders,
# require, ATE.log and the enums are legal at load time. The report runs before ``return`` so it
# actually executes during loading (Lua does not run statements after a ``return``); the raised
# error surfaces as a load failure.
LOAD_REPORT = """\
ATE.report(Result.PASS, "not allowed here")
return ScheduleParams("Load report schedule", "reports at load time") :setSchedule({})
  :setPreTestAction(function() end)
"""

# A schedule whose Pre-test Action calls ATE.input. In a Pre-test/Post-test Action that is not
# allowed (it belongs to steps), so it must raise when the action runs.
PRETEST_INPUT = """\
return ScheduleParams("Pretest input schedule", "input in a pretest action") :setSchedule({})
  :setPreTestAction(function() ATE.input(1) end)
"""

# Same restriction for a Post-test Action: input is not allowed there either.
POSTTEST_INPUT = """\
return ScheduleParams("Posttest input schedule", "input in a posttest action") :setSchedule({})
  :setPostTestAction(function() ATE.input(1) end)
"""

# Same restriction for ATE.report in a Pre-test Action: report is step-only (like input), so this
# must raise when the action runs through its phase context.
PRETEST_REPORT = """\
return ScheduleParams("Pretest report schedule", "report in a pretest action") :setSchedule({})
  :setPreTestAction(function() ATE.report(Result.PASS, "not allowed here") end)
"""

# A schedule whose Pre-test Action calls ATE.log — which IS legal everywhere except load time.
PRETEST_LOG = """\
return ScheduleParams("Pretest log schedule", "log in a pretest action is fine") :setSchedule({})
  :setPreTestAction(function() ATE.log(Log.INFO, "setup running") end)
"""

# A schedule whose single step reads ATE.run.Operator (populated by the run engine from this run's
# context) and reports it — so an end-to-end run records the operator a step can read.
READS_RUN = """\
return ScheduleParams("Reads run schedule", "a step that reads ATE.run")
  :setSchedule({
    StepParams("read operator"):setStepNo("1.")
      :setAction(function() local op=ATE.run.Operator; ATE.report(Result.PASS, "operator is "..op) end),
  })
"""

# A schedule whose single step tries to write ATE.run — which must raise (the table is read-only).
# The raised error becomes an ERROR attempt and fails the run.
WRITES_RUN = """\
return ScheduleParams("Writes run schedule", "a step that writes ATE.run")
  :setSchedule({
    StepParams("hacker"):setStepNo("1.")
      :setAction(function() ATE.run.Operator = "injected" end),
  })
"""

# A schedule whose single step calls an isolated failing ATE function (ATE.boom). The function
# is registered on the driven host via RunEngine's ate_fns seam; calling it from Lua raises a
# Python exception that _AteTable._isolate must catch, log at ERROR, and return as graceful
# halves — never let reach run_engine or surface in Lua.
ISO_ISOLATED = """\
return ScheduleParams("Isolated schedule", "A step that calls an isolated failing ATE function")
  :setSchedule({
    StepParams("boom step"):setStepNo("1.")
      :setAction(function() return ATE.boom("bad value") end),
  })
"""


def _drive(engine: "RunEngine", app, on_run_uuid=None) -> None:
    """Run the worker to completion, then deliver the signals it queued onto this thread.

    The worker's signals reach the test through queued connections, so they only fire once the
    main thread processes posted events. Wait on the thread itself (a real deadline, not a fixed
    number of ``processEvents`` calls, which a slow or busy machine can exhaust before the worker
    has even emitted), then flush the posted events so every queued slot has run."""
    if on_run_uuid is not None:
        engine.run_uuid_reported.connect(on_run_uuid)
    engine.start()
    assert engine.wait(10_000), "run engine did not finish within 10 s"
    app.sendPostedEvents()
    app.processEvents()


def _sha256(path: Path) -> str:
    return __import__("hashlib").sha256(Path(path).read_bytes()).hexdigest()


def write_ate_env(tmp_path: Path) -> dict[str, Path]:
    """Write a config + one Test Definition with three schedules; return path map.

    Returns ``{"config": Path, "defs_dir": Path, "db": Path}``. The database file is not
    created yet — the caller can assert on launch-time refusals before migrating it.
    """
    db = tmp_path / "ate.db"
    defs_dir = tmp_path / "defs"
    defs_dir.mkdir(parents=True, exist_ok=True)
    (defs_dir / "passing.tslua").write_text(PASSING, encoding="utf-8")
    (defs_dir / "failing.tslua").write_text(FAILING, encoding="utf-8")
    (defs_dir / "erroring.tslua").write_text(ERRORING, encoding="utf-8")
    (defs_dir / "logging.tslua").write_text(LOGGING, encoding="utf-8")

    # Ticket 10 schedules: one blocking input, one POLL-only step, and a step declaring every Step
    # Input type. Written into the demo Test Definition so seam/staged tests can select them by name.
    (defs_dir / "blocking_input.tslua").write_text(BLOCKING_INPUT, encoding="utf-8")
    (defs_dir / "poll_input.tslua").write_text(POLL_INPUT, encoding="utf-8")
    (defs_dir / "all_types_inputs.tslua").write_text(ALL_TYPES_INPUTS, encoding="utf-8")

    atedef = {
        "format": 1,
        "mediaNumber": "REL-A",
        "name": "Demo release",
        "version": "1.0.0",
        "files": [
            {"path": "passing.tslua", "sha256": _sha256(defs_dir / "passing.tslua")},
            {"path": "failing.tslua", "sha256": _sha256(defs_dir / "failing.tslua")},
            {"path": "erroring.tslua", "sha256": _sha256(defs_dir / "erroring.tslua")},
            {"path": "logging.tslua", "sha256": _sha256(defs_dir / "logging.tslua")},
            {"path": "blocking_input.tslua", "sha256": _sha256(defs_dir / "blocking_input.tslua")},
            {"path": "poll_input.tslua", "sha256": _sha256(defs_dir / "poll_input.tslua")},
            {"path": "all_types_inputs.tslua", "sha256": _sha256(defs_dir / "all_types_inputs.tslua")},
        ],
    }
    (defs_dir / "demo.atedef").write_text(json.dumps(atedef), encoding="utf-8")

    config = {
        "format": 1,
        "station": {"name": "BENCH-03", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c10"},
        "database": {"type": "sqlite", "path": str(db)},
        "testDefinitions": {"directories": [str(defs_dir)]},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    return {"config": config_path, "defs_dir": defs_dir, "db": db}


def read_run_row(store, run_uuid: str) -> dict:
    """Return a recorded run row as a dict (or ``None``)."""
    return store.get_test_run(run_uuid)


# --- Ticket 10 step inputs ---------------------------------------------------


# A schedule whose single step asks one blocking INTEGER input, then reports it back. The driver
# supplies a value from another thread while the worker waits; once supplied the step records the
# typed value and passes.
BLOCKING_INPUT = """\
return ScheduleParams("Blocking input schedule", "a step that blocks on one INTEGER input")
  :setSchedule({
    StepParams("voltage"):setStepNo("1.")
      :setInputs({ [1] = { desc = "voltage", type = "INTEGER" } })
      :setAction(function() local v=ATE.input(1); ATE.report(Result.PASS, "voltage "..v) end),
  })
"""

# A schedule whose single step's only input is a POLL button. It returns nil until the operator
# clicks one of its buttons (recorded as an input_value row of type POLL), then that text; sticky
# across the step and reset at each run/step start.
POLL_INPUT = """\
return ScheduleParams("Poll input schedule", "a step whose only input is a POLL button")
  :setSchedule({
    StepParams("status check"):setStepNo("1.")
      :setInputs({ [1] = { desc = "device status", type = "POLL", prompt = {"OK", "Error"} } })
      :setAction(function() local s=ATE.input(1); ATE.report(Result.PASS, "status "..s) end),
  })
"""

# A schedule whose single step declares every Step Input type. Each is supplied through the real
# run engine and recorded as its own input_value row: CHECKBOX arrives as a list of labels and is
# stored as JSON; POLL clicks are recorded when clicked; the rest carry their typed value.
ALL_TYPES_INPUTS = """\
return ScheduleParams("All types schedule", "a step that declares every Step Input type")
  :setSchedule({
    StepParams("mixed"):setStepNo("1.")
      :setInputs({
        [1] = { desc = "voltage", type = "INTEGER" },
        [2] = { desc = "current", type = "FLOAT" },
        [3] = { desc = "serial port", type = "STRING" },
        [4] = { desc = "mode", type = "ENUM", input = "RADIOBUT", enum = {"Slow", "Fast"} },
        [5] = { desc = "channels", type = "ENUM", input = "CHECKBOX", enum = {"A", "B", "C"} },
        [6] = { desc = "prompt text", type = "PROMPT", prompt = {"typed"} },
      })
      :setAction(function()
        local v=ATE.input(1); local c=ATE.input(2); local s=ATE.input(3)
        local m=ATE.input(4); local ch=ATE.input(5); local p=ATE.input(6)
        ATE.report(Result.PASS, "v"..v.." c"..c.." s"..s.." m"..m[1].." ch"..ch[1].." p"..p)
      end),
  })
"""


# How the driver answers each announced input while pumping events — its own vocabulary, distinct
# from which schedule is on disk (BLOCKING_INPUT / POLL_INPUT / ALL_TYPES_INPUTS). A blocking
# schedule supplies a value; a poll schedule clicks its button.
INPUTS_BLOCKING = "blocking"  # supply every blocking input from another thread while it waits
INPUTS_POLL = "poll"         # click every POLL button while its step runs
INPUTS_ALL = "all"           # both: supply blocking inputs and click polls as the run proceeds


# Ticket 10 concurrent-supply driver helpers. The worker announces each asked input via the queued
# ``input_asked`` signal before its action runs; these handlers supply a blocking value or click a
# POLL button from another thread (so a blocked get_raw wakes while the worker waits and a POLL is
# clicked while its step runs), then pump events until FINISHED.


def _default_input_value(key: int) -> Any:
    """A type-appropriate default for a blocking supply when no explicit value is given.

    Position-based so a bare driver still records something sensible per index; tests that assert
    exact values pass ``values_by_index`` instead. CHECKBOX returns a list of labels so it is stored
    as JSON (ticket 10 checkbox e)."""
    if key == 4:
        return ["a", "b"]  # CHECKBOX -> stored as a JSON array
    if key == 3:
        return "option"  # RADIOBUT / PUSHBUT single label
    return float(key)  # INTEGER / FLOAT / STRING / PROMPT all coerce fine


def _pump_until_finished(app, engine, op, strategy=INPUTS_BLOCKING, values_by_index=None):
    """Announce-and-supply inputs concurrently, then pump events until the run reaches FINISHED.

    The handler runs on this (pumping) thread; a blocking supply uses a plain queue so it wakes the
    worker's ``get_raw`` synchronously while we wait, and POLL clicks set sticky state immediately."""
    def _on_input_asked(seq, attempt_no, index):
        key = index - 1  # 0-based position into this step's declarations
        if strategy == INPUTS_POLL:
            op.click_poll(node_seq=seq, attempt_no=attempt_no, index=key, text="clicked")
        else:
            value = values_by_index[key] if values_by_index is not None else _default_input_value(key)
            op.supply_input(key, value)

    finished = threading.Event()
    engine.input_asked.connect(_on_input_asked)
    engine.state_changed.connect(lambda status: finished.set() if status == RunStatus.FINISHED else None)

    deadline = time.time() + 10.0
    while not finished.is_set():
        app.processEvents()
        if time.time() > deadline:
            raise TimeoutError("run did not reach FINISHED within 10 s")


def _drive_with_operator(app, engine, op, strategy=INPUTS_BLOCKING, values_by_index=None):
    """Start the worker with an injected OperatorInput and pump until FINISHED.

    For tests that construct the RunEngine directly: start it here (it will block on a blocking input)
    then hand control to :func:`_pump_until_finished`, which supplies/clicks inputs from this thread."""
    engine.start()
    _pump_until_finished(app, engine, op, strategy, values_by_index=values_by_index)
