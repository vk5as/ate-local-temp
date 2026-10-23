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

# A three-step schedule with its middle step disabled at the author's default. Used to cover
# enabling/disabling: seq 0 ("one") and seq 2 ("three") pass when enabled; seq 1 ("two")
# reports FAIL but is :setEnable(false) by default, so a run leaves it NOTRUN unless toggled on.
MULTI = """\
return ScheduleParams("Multi schedule", "Three steps, the middle one off by default")
  :setSchedule({
    StepParams("one"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "ok", "a", "a") end),
    StepParams("two"):setStepNo("2."):setEnable(false)
      :setAction(function() ATE.report(Result.FAIL, "should not run", "b", "c") end),
    StepParams("three"):setStepNo("3.")
      :setAction(function() ATE.report(Result.PASS, "ok", "d", "d") end),
  })
"""


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
    (defs_dir / "multi.tslua").write_text(MULTI, encoding="utf-8")

    atedef = {
        "format": 1,
        "mediaNumber": "REL-A",
        "name": "Demo release",
        "version": "1.0.0",
        "files": [
            {"path": "passing.tslua", "sha256": _sha256(defs_dir / "passing.tslua")},
            {"path": "failing.tslua", "sha256": _sha256(defs_dir / "failing.tslua")},
            {"path": "erroring.tslua", "sha256": _sha256(defs_dir / "erroring.tslua")},
            {"path": "multi.tslua", "sha256": _sha256(defs_dir / "multi.tslua")},
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
