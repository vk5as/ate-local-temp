"""Packet 15e seam-1 tests: Pre-/Post-test Action ordering across the Sub-schedule tree.

Real `.atedef` releases and real `.tslua` files on disk, run end to end through
:class:`HeadlessRunSession`, with the `action` rows (each carrying `instance_seq`) and the log lines
those Actions emit read back. All Pre-test Actions run before the first Test Step, depth first through
the tree (parent first, each include in step order); all Post-test Actions run after the last step in
exactly the reverse order. A Sub-schedule whose whole subtree of Test Steps is disabled has both of its
Actions skipped. After a Pre-test failure only the instances whose Pre-test phase was reached get a
Post-test Action. Post-test Actions always run: the abort flag is cleared before them, an error in one
fails the run while the rest still run, and each is called with the run's overall pass/fail.

Header enable forcing (packet 15d) is out of scope, so every disabled case is written with the steps'
own `:setEnable(false)`.
"""

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QCoreApplication

from ate.catalog import find_definitions, include_map, module_map
from ate.engine.lua_host import LuaHost
from ate.run_session.headless import HeadlessRunSession, SessionState


# --- the two-level tree: main -> group -> leaf, and main -> leaf again ----------------
#
# Instances, in Pre-test order: 0 main, 1 group, 2 the leaf included by the group,
# 3 the leaf included directly by main. Post-test order is exactly the reverse.

MAIN_SRC = """\
return ScheduleParams("Order main", "Pre/Post-test Actions across a two-level include tree")
  :setSchedule({
    StepParams("main step"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "main step", "1", "1") end),
    StepParams("group A"):setStepNo("2."):setPrefix("2."):setInclude("group.tslua", { tag = "A" }),
    StepParams("leaf B"):setStepNo("3."):setPrefix("3."):setInclude("leaf.tslua", { tag = "B" }),
  })
  :setPreTestAction(function(passFail) ATE.log(Log.INFO, "PRE main " .. passFail) end)
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "POST main " .. passFail) end)
"""

GROUP_SRC = """\
local kv = ...
return ScheduleParams("Order group", "a Sub-schedule that includes another Sub-schedule")
  :setSchedule({
    StepParams("group step"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "group step " .. kv.tag, "1", "1") end),
    StepParams("leaf A1"):setStepNo("2."):setPrefix("1."):setInclude("leaf.tslua", { tag = kv.tag .. "1" }),
  })
  :setPreTestAction(function(passFail) ATE.log(Log.INFO, "PRE group " .. passFail) end)
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "POST group " .. passFail) end)
"""

LEAF_SRC = """\
local kv = ...
return ScheduleParams("Order leaf", "the innermost Sub-schedule")
  :setSchedule({
    StepParams("leaf step"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "leaf step " .. kv.tag, "1", "1") end),
  })
  :setPreTestAction(function(passFail) ATE.log(Log.INFO, "PRE leaf " .. kv.tag .. " " .. passFail) end)
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "POST leaf " .. kv.tag .. " " .. passFail) end)
"""

TREE = {"main.tslua": MAIN_SRC, "group.tslua": GROUP_SRC, "leaf.tslua": LEAF_SRC}

PRE_ORDER = ["PRE main PASS", "PRE group PASS", "PRE leaf A1 PASS", "PRE leaf B PASS"]
POST_ORDER = ["POST leaf B PASS", "POST leaf A1 PASS", "POST group PASS", "POST main PASS"]
INSTANCES = [0, 1, 2, 3]
STEP_NUMBERS = ["1.", "2.1.", "2.1.1.", "3.1."]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _release(root: Path, label: str, files: dict[str, str]) -> Path:
    """Write `<label>.atedef` listing every file in `files`, and the files beside it."""
    directory = root / label
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{label}.atedef").write_text(
        json.dumps(
            {
                "format": 1,
                "mediaNumber": f"MR-{label.upper()}",
                "name": f"Release {label}",
                "version": "1.0.0",
                "depends": [],
                "files": [{"path": rel, "sha256": _sha(src)} for rel, src in files.items()],
            }
        ),
        encoding="utf-8",
    )
    for rel, src in files.items():
        (directory / rel).write_text(src, encoding="utf-8")
    return directory


def _config(tmp_path: Path, root: Path) -> Path:
    config = {
        "format": 1,
        "station": {"name": "BENCH-15E", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c11"},
        "database": {"type": "sqlite", "path": str(tmp_path / "ate.db"), "storeMismatchedFiles": "always"},
        "reports": {"directory": str(tmp_path / "reports")},
        "testDefinitions": {"directories": [str(root)]},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _pump(qapp: QCoreApplication, session: HeadlessRunSession, bound: float = 20.0) -> None:
    """Pump the event loop until the run is FINISHED and its report written."""
    written: list[tuple[str, str]] = []
    session.report_written.connect(lambda html, pdf: written.append((html, pdf)))
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        qapp.processEvents()
        if session._state == SessionState.FINISHED and written:
            return
        time.sleep(0.005)
    raise TimeoutError("the run never reached FINISHED with its report written")


def _run(qapp: QCoreApplication, tmp_path: Path, label: str, files: dict[str, str]) -> HeadlessRunSession:
    """Run `main.tslua` of a freshly written release to FINISHED and return the session."""
    root = tmp_path / "defs"
    _release(root, label, files)
    session = HeadlessRunSession(_config(tmp_path, root))
    session.select_schedule("Order main")
    session.start()
    _pump(qapp, session)
    return session


def _action_rows(session: HeadlessRunSession) -> list[dict]:
    """This run's `action` rows, in the order they were written."""
    conn = session.store._conn
    conn.row_factory = sqlite3.Row
    return [
        dict(row)
        for row in conn.execute(
            "SELECT seq, instance_seq, phase, run_order, outcome, error FROM action "
            "WHERE run_uuid=? ORDER BY seq",
            (session.run_uuid,),
        ).fetchall()
    ]


def _phases(session: HeadlessRunSession) -> list[tuple[str, int]]:
    return [(row["phase"], row["instance_seq"]) for row in _action_rows(session)]


def _phase_lines(session: HeadlessRunSession) -> list[str]:
    """The PRE/POST log lines, in the order they were recorded."""
    return [
        line["text"]
        for line in session.store.log_lines(session.run_uuid)
        if line["text"].startswith(("PRE ", "POST "))
    ]


def _step_results(session: HeadlessRunSession) -> list[tuple[str, Optional[str]]]:
    return [(n["step_number"], n["result"]) for n in session.store.nodes(session.run_uuid) if n["kind"] == "step"]


# --- the order across a tree ----------------------------------------------------------


def test_pre_actions_run_depth_first_and_post_actions_in_exact_reverse(qapp, tmp_path) -> None:
    """main -> group -> leaf plus main -> leaf: four instances, Pre in tree order, Post in reverse."""
    session = _run(qapp, tmp_path, "order", TREE)
    rows = _action_rows(session)
    assert [(r["phase"], r["instance_seq"]) for r in rows] == [("pre", s) for s in INSTANCES] + [("post", s) for s in reversed(INSTANCES)]
    # run_order is each Action's position within its own phase, in the order that phase ran.
    assert [r["run_order"] for r in rows] == [0, 1, 2, 3, 0, 1, 2, 3]
    assert all(r["outcome"] == "ok" for r in rows)
    assert _phase_lines(session) == PRE_ORDER + POST_ORDER
    # Every Pre-test Action ran before the first step, and every Post-test Action after the last.
    assert _step_results(session) == [(n, "PASS") for n in STEP_NUMBERS]
    run = session.store.get_test_run(session.run_uuid)
    assert (run["result"], run["end_reason"]) == ("PASS", "COMPLETED")


def test_a_top_level_only_schedule_is_unchanged(qapp, tmp_path) -> None:
    """With no includes the Pre/Post Actions still run once each, on instance 0."""
    files = dict(TREE)
    files["main.tslua"] = """\
return ScheduleParams("Order main", "a schedule with no Sub-schedules")
  :setSchedule({
    StepParams("main step"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "main step", "1", "1") end),
  })
  :setPreTestAction(function(passFail) ATE.log(Log.INFO, "PRE main " .. passFail) end)
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "POST main " .. passFail) end)
"""
    session = _run(qapp, tmp_path, "plain", files)
    assert _phases(session) == [("pre", 0), ("post", 0)]
    assert _phase_lines(session) == ["PRE main PASS", "POST main PASS"]


def test_a_schedule_with_no_test_steps_of_its_own_is_not_a_disabled_one(qapp, tmp_path) -> None:
    """A header-only parent and a step-less Sub-schedule both still get their Actions."""
    files = dict(TREE)
    files["main.tslua"] = """\
return ScheduleParams("Order main", "no Test Steps of its own, just one include")
  :setSchedule({
    StepParams("group A"):setStepNo("1."):setPrefix("1."):setInclude("group.tslua", { tag = "A" }),
  })
  :setPreTestAction(function(passFail) ATE.log(Log.INFO, "PRE main " .. passFail) end)
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "POST main " .. passFail) end)
"""
    files["group.tslua"] = """\
return ScheduleParams("Order group", "a Sub-schedule with no Test Steps at all")
  :setSchedule({})
  :setPreTestAction(function(passFail) ATE.log(Log.INFO, "PRE group " .. passFail) end)
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "POST group " .. passFail) end)
"""
    session = _run(qapp, tmp_path, "nosteps", files)
    assert _phases(session) == [("pre", 0), ("pre", 1), ("post", 1), ("post", 0)]
    assert _phase_lines(session) == ["PRE main PASS", "PRE group PASS", "POST group PASS", "POST main PASS"]
    assert session.store.get_test_run(session.run_uuid)["result"] == "PASS"


# --- a fully disabled Sub-schedule ----------------------------------------------------


def test_a_fully_disabled_include_runs_neither_of_its_actions(qapp, tmp_path) -> None:
    """Both leaf instances have only disabled steps, so only main and the group get Actions."""
    files = dict(TREE)
    files["leaf.tslua"] = """\
local kv = ...
return ScheduleParams("Order leaf", "a Sub-schedule whose every step is disabled")
  :setSchedule({
    StepParams("leaf step"):setStepNo("1."):setEnable(false)
      :setAction(function() ATE.report(Result.PASS, "leaf step " .. kv.tag, "1", "1") end),
  })
  :setPreTestAction(function(passFail) ATE.log(Log.INFO, "PRE leaf " .. kv.tag .. " " .. passFail) end)
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "POST leaf " .. kv.tag .. " " .. passFail) end)
"""
    session = _run(qapp, tmp_path, "off", files)
    assert _phases(session) == [("pre", 0), ("pre", 1), ("post", 1), ("post", 0)]
    assert _phase_lines(session) == ["PRE main PASS", "PRE group PASS", "POST group PASS", "POST main PASS"]
    assert _step_results(session) == [("1.", "PASS"), ("2.1.", "PASS"), ("2.1.1.", "NOTRUN"), ("3.1.", "NOTRUN")]
    run = session.store.get_test_run(session.run_uuid)
    assert (run["result"], run["end_reason"]) == ("PASS", "COMPLETED")


def test_a_schedule_is_skipped_only_when_its_whole_subtree_is_disabled(qapp, tmp_path) -> None:
    """The group's step is off but the leaf under it is on: the group still gets its Actions, and a
    parent whose entire subtree is off gets none at all."""
    files = dict(TREE)
    files["main.tslua"] = """\
return ScheduleParams("Order main", "everything beneath one include is disabled")
  :setSchedule({
    StepParams("main step"):setStepNo("1."):setEnable(false)
      :setAction(function() ATE.report(Result.PASS, "main step", "1", "1") end),
    StepParams("group A"):setStepNo("2."):setPrefix("2."):setInclude("group.tslua", { tag = "A" }),
  })
  :setPreTestAction(function(passFail) ATE.log(Log.INFO, "PRE main " .. passFail) end)
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "POST main " .. passFail) end)
"""
    files["group.tslua"] = """\
local kv = ...
return ScheduleParams("Order group", "a Sub-schedule whose steps are all disabled")
  :setSchedule({
    StepParams("group step"):setStepNo("1."):setEnable(false)
      :setAction(function() ATE.report(Result.PASS, "group step", "1", "1") end),
    StepParams("leaf A1"):setStepNo("2."):setPrefix("1."):setInclude("leaf.tslua", { tag = kv.tag }),
  })
  :setPreTestAction(function(passFail) ATE.log(Log.INFO, "PRE group " .. passFail) end)
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "POST group " .. passFail) end)
"""
    files["leaf.tslua"] = """\
local kv = ...
return ScheduleParams("Order leaf", "the innermost Sub-schedule, every step disabled")
  :setSchedule({
    StepParams("leaf step"):setStepNo("1."):setEnable(false)
      :setAction(function() ATE.report(Result.PASS, "leaf step", "1", "1") end),
  })
  :setPreTestAction(function(passFail) ATE.log(Log.INFO, "PRE leaf " .. passFail) end)
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "POST leaf " .. passFail) end)
"""
    session = _run(qapp, tmp_path, "subtree", files)
    # Three instances, every step in each subtree disabled: no Action runs anywhere, but the run is
    # still a clean PASS with all steps NOTRUN.
    assert _phases(session) == []
    assert _step_results(session) == [("1.", "NOTRUN"), ("2.1.", "NOTRUN"), ("2.1.1.", "NOTRUN")]
    run = session.store.get_test_run(session.run_uuid)
    assert (run["result"], run["end_reason"]) == ("PASS", "COMPLETED")


def test_operator_disabling_every_step_of_an_include_skips_its_actions(qapp, tmp_path) -> None:
    """The Operator's enable overrides count as much as the author's `:setEnable(false)`."""
    root = tmp_path / "defs"
    _release(root, "override", TREE)
    definition = next(d for d in find_definitions(root) if d.media_number == "MR-OVERRIDE")
    host = LuaHost(definition.path / "main.tslua", modules=module_map(definition), includes=include_map(definition))
    leaf_seqs = [i for i, node in enumerate(host.tree) if not node.is_header and node.instance == 2]
    session = HeadlessRunSession(_config(tmp_path, root))
    session.select_schedule("Order main")
    for seq in leaf_seqs:
        session.toggle_node(seq, False)
    session.start()
    _pump(qapp, session)
    assert _phases(session) == [("pre", 0), ("pre", 1), ("pre", 3), ("post", 3), ("post", 1), ("post", 0)]
    assert _phase_lines(session) == ["PRE main PASS", "PRE group PASS", "PRE leaf B PASS",
                                     "POST leaf B PASS", "POST group PASS", "POST main PASS"]
    assert _step_results(session) == [("1.", "PASS"), ("2.1.", "PASS"), ("2.1.1.", "NOTRUN"), ("3.1.", "PASS")]


# --- a Pre-test failure in an include -------------------------------------------------


def test_a_pretest_failure_in_an_include_runs_no_steps_and_only_started_posts(qapp, tmp_path) -> None:
    """The group's Pre-test Action raises: nothing runs, and only main and the group get a Post-test."""
    files = dict(TREE)
    files["group.tslua"] = GROUP_SRC.replace(
        ':setPreTestAction(function(passFail) ATE.log(Log.INFO, "PRE group " .. passFail) end)',
        ':setPreTestAction(function(passFail) error("group calibration failed") end)',
    )
    session = _run(qapp, tmp_path, "prefail", files)
    # main's Pre ran, the group's Pre started and failed, and neither leaf was ever reached.
    assert _phases(session) == [("pre", 0), ("pre", 1), ("post", 1), ("post", 0)]
    rows = _action_rows(session)
    assert [r["outcome"] for r in rows] == ["ok", "error", "ok", "ok"]
    assert rows[1]["outcome"] == "error" and rows[1]["error"].endswith("group calibration failed")
    assert session.store.attempts(session.run_uuid) == []
    assert all(n["result"] == "NOTRUN" and n["attempt_count"] == 0
               for n in session.store.nodes(session.run_uuid) if n["kind"] == "step")
    run = session.store.get_test_run(session.run_uuid)
    assert (run["result"], run["end_reason"]) == ("FAIL", "PRETEST_FAILED")
    assert any("group calibration failed" in line["text"] for line in session.store.log_lines(session.run_uuid))
    # The run already failed, so every Post-test Action is told FAIL.
    posts = [l["text"] for l in session.store.log_lines(session.run_uuid) if l["text"].startswith("POST")]
    assert posts and all(text.endswith("FAIL") for text in posts), posts


def test_a_top_level_pretest_failure_is_unchanged_by_15e(qapp, tmp_path) -> None:
    """The top-level Pre-test fails first, so no included instance started and none is torn down."""
    files = dict(TREE)
    files["main.tslua"] = MAIN_SRC.replace(
        ':setPreTestAction(function(passFail) ATE.log(Log.INFO, "PRE main " .. passFail) end)',
        ':setPreTestAction(function(passFail) error("main setup failed") end)',
    )
    session = _run(qapp, tmp_path, "prefailtop", files)
    assert _phases(session) == [("pre", 0), ("post", 0)]
    assert [r["outcome"] for r in _action_rows(session)] == ["error", "ok"]
    run = session.store.get_test_run(session.run_uuid)
    assert (run["result"], run["end_reason"]) == ("FAIL", "PRETEST_FAILED")
    assert all(n["result"] == "NOTRUN" for n in session.store.nodes(session.run_uuid) if n["kind"] == "step")


# --- Post-test Actions always run -----------------------------------------------------


def test_an_error_in_one_post_action_fails_the_run_and_the_others_still_run(qapp, tmp_path) -> None:
    """The group's Post-test Action raises; the leaves before it and main after it still tear down."""
    files = dict(TREE)
    files["group.tslua"] = GROUP_SRC.replace(
        ':setPostTestAction(function(passFail) ATE.log(Log.INFO, "POST group " .. passFail) end)',
        ':setPostTestAction(function(passFail) error("group teardown failed") end)',
    )
    session = _run(qapp, tmp_path, "posterror", files)
    rows = _action_rows(session)
    assert [(r["phase"], r["instance_seq"]) for r in rows] == [("pre", s) for s in INSTANCES] + [("post", s) for s in reversed(INSTANCES)]
    assert [r["outcome"] for r in rows] == ["ok", "ok", "ok", "ok", "ok", "ok", "error", "ok"]
    assert rows[6]["outcome"] == "error" and rows[6]["error"].endswith("group teardown failed")
    run = session.store.get_test_run(session.run_uuid)
    assert (run["result"], run["end_reason"]) == ("FAIL", "COMPLETED")
    # The Post Actions after the error see the run as failed.
    assert _phase_lines(session) == PRE_ORDER + ["POST leaf B PASS", "POST leaf A1 PASS", "POST main FAIL"]


def test_every_post_action_still_runs_after_an_abort(qapp, tmp_path) -> None:
    """An abort during the steps ends the run ABORTED, yet all four Post-test Actions still run."""
    files = dict(TREE)
    files["main.tslua"] = MAIN_SRC.replace(
        ':setAction(function() ATE.report(Result.PASS, "main step", "1", "1") end)',
        ':setAction(function() for i=1,1e9 do end end)',
    )
    root = tmp_path / "defs"
    _release(root, "abort", files)
    session = HeadlessRunSession(_config(tmp_path, root))
    session.select_schedule("Order main")
    session.start()
    session.request_abort()
    _pump(qapp, session)
    run = session.store.get_test_run(session.run_uuid)
    assert (run["result"], run["end_reason"]) == ("FAIL", "ABORTED")
    assert _phases(session) == [("pre", s) for s in INSTANCES] + [("post", s) for s in reversed(INSTANCES)]
    assert all(r["outcome"] == "ok" for r in _action_rows(session))
    assert _phase_lines(session) == PRE_ORDER + [line.replace(" PASS", " FAIL") for line in POST_ORDER]  # an aborted run is FAIL
    assert _step_results(session) == [("1.", "ERROR"), ("2.1.", "NOTRUN"), ("2.1.1.", "NOTRUN"), ("3.1.", "NOTRUN")]
    # Only the interrupted busy step records an Attempt.
    assert len(session.store.attempts(session.run_uuid)) == 1


# --- the pass/fail handed to an included Post-test Action -----------------------------


def test_included_post_actions_receive_the_runs_overall_result(qapp, tmp_path) -> None:
    """A step deep in the tree fails (Stop on Fail off): every Post-test Action is called with FAIL."""
    files = dict(TREE)
    files["main.tslua"] = MAIN_SRC.replace("  :setSchedule(", "  :setStopOnFail(false)\n  :setSchedule(")
    files["group.tslua"] = GROUP_SRC.replace('Result.PASS, "group step "', 'Result.FAIL, "group step "')
    session = _run(qapp, tmp_path, "postfail", files)
    run = session.store.get_test_run(session.run_uuid)
    assert (run["result"], run["end_reason"]) == ("FAIL", "COMPLETED")
    assert _phase_lines(session) == [
        "PRE main PASS", "PRE group PASS", "PRE leaf A1 PASS", "PRE leaf B PASS",
        "POST leaf B FAIL", "POST leaf A1 FAIL", "POST group FAIL", "POST main FAIL",
    ]
