"""Shared helpers for ticket 02's seam tests: build a minimal on-disk ATE environment.

These write a config pointing at a fresh SQLite database and one Test Definition
directory holding three schedules (one that passes, one that fails, one that raises), so
the headless session and CLI can be exercised end-to-end against real files — no mocks.
"""

from __future__ import annotations

import json
from pathlib import Path


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
    """Start the worker and pump events until its run key is actually delivered.

    The engine delivers ``run_uuid_reported`` through a queued connection onto this shared
    application's event loop, so the slot only fires when we process events. We wait for that
    delivery rather than on ``isRunning()``: this Qt 6.11 / PyQt6 build can report a QThread as
    stopped before it has emitted its queued cross-thread signal, and that misreport accumulates
    across tests driving workers off one shared application — after ticket 03's staged_run_screen
    tests leave lingering workers/timers on the loop, a subsequent worker reports ``isRunning()``
    while never delivering within the pump window. Keep pumping until the key arrives (or the cap
    is hit), then drain once more for anything queued just before the worker stopped. ``on_run_uuid``
    captures the run key; pass it from the caller's capture closure instead of connecting separately
    so there is one connection point.
    """
    from PyQt6.QtCore import QCoreApplication  # noqa: E402

    delivered = {"run_uuid": None}
    if on_run_uuid is not None:
        def _slot(run_uuid):
            delivered["run_uuid"] = run_uuid
            on_run_uuid(run_uuid)

        engine.run_uuid_reported.connect(_slot)
    else:
        engine.run_uuid_reported.connect(lambda _: None)  # still deliver, just ignore
    engine.start()
    for _ in range(2000):
        app.processEvents()
        if delivered["run_uuid"] is not None:
            break
    for _ in range(200):
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
