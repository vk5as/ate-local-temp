"""Packet 20 seam-1 tests: Equipment declared with `ATE.addEquipment`.

Real `.atedef` releases and real `.tslua` files on disk, run end to end through
:class:`HeadlessRunSession`, with the `equipment` rows read back. A schedule declares the Equipment
on the bench in its Pre-test Action (the usual place), in a step, or in its Post-test Action, and
each call is recorded as it happens, in call order, owned by the Action or step Attempt that made
it. All five arguments are strings; only `name` is required and the rest may be nil, which stores
NULL. A repeat of the same `name` and `serial` adds no row and logs one DEBUG line. A bad type —
or a missing `name` — is a Lua error that fails the step or Action like any other. A call at the
top level of the file is refused, because the file loads before a Test Run exists, so such a
schedule never runs at all.
"""

import hashlib
import json
import time
from pathlib import Path
from typing import Optional

import pytest
from PyQt6.QtCore import QCoreApplication

from ate.catalog import list_schedule_rows
from ate.run_session.headless import HeadlessRunSession, SessionError, SessionState

# The bench schedule: three pieces declared in the Pre-test Action, one in the step, one in the
# Post-test Action — the order the rows must come back in.
BENCH_SRC = """\
return ScheduleParams("Equipment bench", "declares the Equipment the Test Run used")
  :setSchedule({
    StepParams("check output"):setStepNo("1.")
      :setAction(function()
        ATE.addEquipment("Oscilloscope", "SN-100", "GPIB", "2026-01-15", "bench 2")
        ATE.report(Result.PASS, "check output", "1", "1")
      end),
  })
  :setPreTestAction(function(passFail)
    ATE.addEquipment("Power supply", "PSU-1")
    ATE.addEquipment("Multimeter")
    ATE.addEquipment("Fixture jig", nil, "BENCH-3", "2027-06-01")
  end)
  :setPostTestAction(function(passFail)
    ATE.addEquipment("Thermometer", "TH-9", "USB", "2026-11-30")
  end)
"""


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
        newline="\n",
    )
    for rel, src in files.items():
        (directory / rel).write_text(src, encoding="utf-8", newline="\n")
    return directory


def _config(tmp_path: Path, root: Path) -> Path:
    config = {
        "format": 1,
        "station": {"name": "BENCH-20", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c20"},
        "defaultProject": "Power board",
        "database": {"type": "sqlite", "path": str(tmp_path / "ate.db"), "storeMismatchedFiles": "always"},
        "reports": {"directory": str(tmp_path / "reports")},
        "testDefinitions": {"directories": [str(root)]},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8", newline="\n")
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


def _run(qapp: QCoreApplication, tmp_path: Path, label: str, files: dict[str, str], name: str = "Equipment bench") -> HeadlessRunSession:
    """Run `main.tslua` of a freshly written release to FINISHED and return the session."""
    root = _release(tmp_path / "defs", label, files)
    session = HeadlessRunSession(_config(tmp_path, root))
    session.select_schedule(name)
    session.start()
    _pump(qapp, session)
    return session


def _equipment(session: HeadlessRunSession) -> list[dict]:
    """This run's `equipment` rows, in declaration order."""
    return session.store.equipment(session.run_uuid)


def _names(session: HeadlessRunSession) -> list[str]:
    return [row["name"] for row in _equipment(session)]


def _step_nodes(session: HeadlessRunSession) -> dict[str, int]:
    """Step Number -> node_seq, so an owner check never hardcodes a tree index."""
    return {n["step_number"]: n["node_seq"] for n in session.store.nodes(session.run_uuid) if n["kind"] == "step"}


# --- where the calls come from, and the order they land in -------------------------


def test_equipment_from_pre_step_and_post_is_recorded_in_call_order(qapp, tmp_path) -> None:
    """One Pre-test Action, one step, one Post-test Action: five rows, in the order called."""
    session = _run(qapp, tmp_path, "bench", {"main.tslua": BENCH_SRC})
    rows = _equipment(session)
    assert [r["name"] for r in rows] == ["Power supply", "Multimeter", "Fixture jig", "Oscilloscope", "Thermometer"]
    assert [r["serial"] for r in rows] == ["PSU-1", None, None, "SN-100", "TH-9"]
    assert [r["connection"] for r in rows] == [None, None, "BENCH-3", "GPIB", "USB"]
    assert [r["cal_due"] for r in rows] == [None, None, "2027-06-01", "2026-01-15", "2026-11-30"]
    assert [r["notes"] for r in rows] == [None, None, None, "bench 2", None]
    # seq is the declaration order (the run's shared row counter also numbers Actions and log
    # lines), and every row carries the time it was declared.
    assert all(a["seq"] < b["seq"] for a, b in zip(rows, rows[1:]))
    assert all(r["at"] for r in rows)
    # A Pre-test/Post-test Action owns its rows through action_seq; the step through its Attempt.
    for row in rows[:3] + rows[4:]:
        assert (row["action_seq"], row["node_seq"], row["attempt_no"]) == (0, None, None), row
    step_seq = _step_nodes(session)["1."]
    assert (rows[3]["action_seq"], rows[3]["node_seq"], rows[3]["attempt_no"]) == (None, step_seq, 1)
    run = session.store.get_test_run(session.run_uuid)
    assert (run["result"], run["end_reason"]) == ("PASS", "COMPLETED")


def test_a_field_that_is_nil_stays_null(qapp, tmp_path) -> None:
    """`ATE.addEquipment("Multimeter")` records just the name; the rest is NULL, not blank."""
    files = {"main.tslua": """\
return ScheduleParams("One piece", "nil optional fields")
  :setSchedule({
    StepParams("step"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "step", "1", "1") end),
  })
  :setPreTestAction(function(passFail) ATE.addEquipment("Multimeter") end)
"""}
    session = _run(qapp, tmp_path, "nil", files, "One piece")
    rows = _equipment(session)
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "Multimeter"
    assert (row["serial"], row["connection"], row["cal_due"], row["notes"]) == (None, None, None, None)
    assert row["at"]  # every declaration gets a time


def test_cal_due_is_stored_as_given_text(qapp, tmp_path) -> None:
    """ATE never parses `cal_due`: an expiry is recorded exactly as the schedule wrote it."""
    files = {"main.tslua": """\
return ScheduleParams("One piece", "free-text calibration due")
  :setSchedule({
    StepParams("step"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "step", "1", "1") end),
  })
  :setPreTestAction(function(passFail) ATE.addEquipment("Scope", "SN-1", nil, "next due: Q3 2027", nil) end)
"""}
    session = _run(qapp, tmp_path, "caldue", files, "One piece")
    row = _equipment(session)[0]
    assert row["cal_due"] == "next due: Q3 2027"
    assert row["connection"] is None


# --- a repeat adds no row ----------------------------------------------------------


def test_a_repeat_of_the_same_name_and_serial_is_ignored_with_a_debug_line(qapp, tmp_path) -> None:
    """The same Equipment declared twice in one run is one row, and the duplicate is logged DEBUG."""
    files = {"main.tslua": """\
return ScheduleParams("Repeat bench", "declares the same Equipment from a Pre-test Action and a step")
  :setSchedule({
    StepParams("check"):setStepNo("1.")
      :setAction(function()
        ATE.addEquipment("Oscilloscope", "SN-100", "GPIB", "2026-01-15", "declared again in the step")
        ATE.report(Result.PASS, "check", "1", "1")
      end),
  })
  :setPreTestAction(function(passFail) ATE.addEquipment("Oscilloscope", "SN-100", "GPIB", "2026-01-15") end)
"""}
    session = _run(qapp, tmp_path, "repeat", files, "Repeat bench")
    rows = _equipment(session)
    assert len(rows) == 1  # the first call wins; the repeat adds nothing
    assert (rows[0]["name"], rows[0]["serial"], rows[0]["notes"]) == ("Oscilloscope", "SN-100", None)
    debug = [l for l in session.store.log_lines(session.run_uuid) if l["level"] == "DEBUG"]
    assert len(debug) == 1
    assert "ATE.addEquipment" in debug[0]["text"]
    assert "Oscilloscope" in debug[0]["text"]
    assert "SN-100" in debug[0]["text"]
    # The DEBUG line is ATE's own, and it belongs to the Attempt that made the repeat call.
    assert debug[0]["source"] == "ate"
    assert debug[0]["node_seq"] == _step_nodes(session)["1."]
    assert debug[0]["attempt_no"] == 1


def test_the_same_name_with_a_different_serial_is_new_equipment(qapp, tmp_path) -> None:
    """Only name AND serial matching makes a repeat: a second serial is another row."""
    files = {"main.tslua": """\
return ScheduleParams("Two scopes", "one name, two serials, one repeat")
  :setSchedule({
    StepParams("check"):setStepNo("1.")
      :setAction(function()
        ATE.addEquipment("Oscilloscope", "SN-200")
        ATE.addEquipment("Oscilloscope", "SN-100")  -- the Pre-test Action already declared this one
        ATE.addEquipment("Fixture jig")
        ATE.addEquipment("Fixture jig", "JIG-1")
        ATE.report(Result.PASS, "check", "1", "1")
      end),
  })
  :setPreTestAction(function(passFail) ATE.addEquipment("Oscilloscope", "SN-100") end)
"""}
    session = _run(qapp, tmp_path, "twoscopes", files, "Two scopes")
    rows = _equipment(session)
    assert [(r["name"], r["serial"]) for r in rows] == [
        ("Oscilloscope", "SN-100"),   # the Pre-test Action's declaration
        ("Oscilloscope", "SN-200"),   # a different serial is different Equipment
        ("Fixture jig", None),        # nil serial counts as its own Equipment
        ("Fixture jig", "JIG-1"),     # ... and a serial makes it a new one
    ]
    debug = [l for l in session.store.log_lines(session.run_uuid) if l["level"] == "DEBUG"]
    assert len(debug) == 1 and "Oscilloscope" in debug[0]["text"] and "SN-100" in debug[0]["text"]
    # A different serial is different Equipment: the step's own rows keep the step's owner.
    step_seq = _step_nodes(session)["1."]
    assert all((r["node_seq"], r["attempt_no"]) == (step_seq, 1) for r in rows[1:])


# --- a bad argument fails the step or Action --------------------------------------


BAD_ARGS = [
    ("a number for the name", "123", "name"),
    ("no name at all", "", "name is required"),
    ("a number for the serial", '"Scope", 123', "serial"),
    ("a boolean for the serial", '"Scope", true', "serial"),
    ("a table for the name", '{ "Scope" }', "name"),
    ("a number for the notes", '"Scope", nil, nil, nil, 7', "notes"),
    ("an empty name", '""', "name is required"),
    ("a sixth argument", '"Scope", "S1", "GPIB", "2027", "note", "extra"', "at most 5 arguments"),
]


@pytest.mark.parametrize("what, args, names_field", BAD_ARGS)
def test_a_bad_argument_type_raises_and_fails_the_step(qapp, tmp_path, what: str, args: str, names_field: str) -> None:
    """Every non-string, non-nil argument is a Lua error: the Attempt is ERROR and no row is written."""
    files = {"main.tslua": f"""\
return ScheduleParams("Bad equipment", "a step that passes {what}")
  :setStopOnFail(false)
  :setSchedule({{
    StepParams("check"):setStepNo("1.")
      :setAction(function()
        ATE.addEquipment({args})
        ATE.report(Result.PASS, "check", "1", "1")
      end),
  }})
"""}
    session = _run(qapp, tmp_path, "bad", files, "Bad equipment")
    assert _equipment(session) == []
    attempts = session.store.attempts(session.run_uuid)
    assert len(attempts) == 1
    assert attempts[0]["pass_fail"] == "ERROR"
    assert "ATE.addEquipment" in (attempts[0]["msg"] or "")
    assert names_field in (attempts[0]["msg"] or "")
    # The step failed, so the run is FAIL, and the error is logged like any other step error.
    assert session.store.get_test_run(session.run_uuid)["result"] == "FAIL"
    assert any(l["level"] == "ERROR" and "ATE.addEquipment" in l["text"]
               for l in session.store.log_lines(session.run_uuid))


def test_a_bad_argument_in_a_pretest_action_fails_the_run_before_any_step(qapp, tmp_path) -> None:
    """The same rule applies to a Pre-test Action: it errors, the run aborts, nothing is recorded."""
    files = {"main.tslua": """\
return ScheduleParams("Bad pretest", "a Pre-test Action with a number for the serial")
  :setSchedule({
    StepParams("check"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "check", "1", "1") end),
  })
  :setPreTestAction(function(passFail) ATE.addEquipment("Scope", 123) end)
"""}
    session = _run(qapp, tmp_path, "badpre", files, "Bad pretest")
    actions = session.store.actions(session.run_uuid)
    assert actions and actions[0]["outcome"] == "error"
    assert "ATE.addEquipment" in (actions[0]["error"] or "")
    assert _equipment(session) == []
    assert session.store.attempts(session.run_uuid) == []
    run = session.store.get_test_run(session.run_uuid)
    assert (run["result"], run["end_reason"]) == ("FAIL", "PRETEST_FAILED")


# --- a call at load time -----------------------------------------------------------


def test_a_load_time_call_raises_so_the_schedule_never_runs(qapp, tmp_path) -> None:
    """Equipment belongs to a Pre-test Action, a step or a Post-test Action; the top level of the
    file loads before a Test Run exists, so such a schedule is refused at load and never runs."""
    files = {"main.tslua": """\
ATE.addEquipment("Bench scope", "SN-1")
return ScheduleParams("Load time equipment", "declares Equipment while the file loads")
  :setSchedule({
    StepParams("check"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "check", "1", "1") end),
  })
"""}
    root = _release(tmp_path / "defs", "loadtime", files)
    # The picker reports the schedule as broken, naming the function ATE refused.
    broken = [row for row in list_schedule_rows(root) if row.error]
    assert len(broken) == 1
    assert "ATE.addEquipment" in broken[0].error
    assert "only allowed" in broken[0].error

    session = HeadlessRunSession(_config(tmp_path, root))
    session.select_schedule("Load time equipment")
    with pytest.raises(SessionError) as excinfo:
        session.start()
    assert "not available" in str(excinfo.value)
    # Nothing ran: the run was never recorded, so there is no Equipment row either.
    assert session.store.query_runs() == []
    assert session.run_uuid is None


# --- Sub-schedules ----------------------------------------------------------------


SUB_SRC = """\
local kv = ...
return ScheduleParams("Bench sub", "a Sub-schedule that declares its own Equipment")
  :setSchedule({
    StepParams("sub check"):setStepNo("1.")
      :setAction(function()
        ATE.addEquipment("Sub jig", "JIG-" .. kv.tag)
        ATE.report(Result.PASS, "sub check", "1", "1")
      end),
  })
  :setPreTestAction(function(passFail) ATE.addEquipment("Sub supply", "SUB-PSU") end)
  :setPostTestAction(function(passFail) ATE.addEquipment("Sub load", nil, "BENCH-9") end)
"""

MAIN_SRC = """\
return ScheduleParams("Equipment main", "a schedule whose Sub-schedule declares Equipment too")
  :setSchedule({
    StepParams("main step"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "main step", "1", "1") end),
    StepParams("bench A"):setStepNo("2."):setPrefix("2."):setInclude("bench_sub.tslua", { tag = "A" }),
  })
  :setPreTestAction(function(passFail) ATE.addEquipment("Main supply", "MAIN-PSU") end)
"""


def test_equipment_declared_in_an_included_sub_schedule_is_recorded(qapp, tmp_path) -> None:
    """Every schedule in the tree declares its own Equipment, each row owned by that schedule's own
    Action or step Attempt, and the whole run keeps one declaration order."""
    session = _run(qapp, tmp_path, "sub", {"main.tslua": MAIN_SRC, "bench_sub.tslua": SUB_SRC}, "Equipment main")
    rows = _equipment(session)
    assert [r["name"] for r in rows] == ["Main supply", "Sub supply", "Sub jig", "Sub load"]
    # main's Pre-test Action is instance 0, the include's is instance 1; each owns its own rows.
    assert rows[0]["action_seq"] == 0 and rows[1]["action_seq"] == 1
    sub_step = _step_nodes(session)["2.1."]
    assert (rows[2]["action_seq"], rows[2]["node_seq"], rows[2]["attempt_no"]) == (None, sub_step, 1)
    assert rows[3]["action_seq"] == 1 and rows[3]["node_seq"] is None
    assert [r["serial"] for r in rows] == ["MAIN-PSU", "SUB-PSU", "JIG-A", None]
    run = session.store.get_test_run(session.run_uuid)
    assert (run["result"], run["end_reason"]) == ("PASS", "COMPLETED")


def test_a_repeat_across_a_sub_schedule_is_still_ignored(qapp, tmp_path) -> None:
    """The run's Equipment is one set: an include repeating what the top level declared adds no row."""
    files = {"main.tslua": MAIN_SRC.replace('"Main supply", "MAIN-PSU"', '"Sub supply", "SUB-PSU"'),
             "bench_sub.tslua": SUB_SRC}
    session = _run(qapp, tmp_path, "subrepeat", files, "Equipment main")
    rows = _equipment(session)
    assert [r["name"] for r in rows] == ["Sub supply", "Sub jig", "Sub load"]
    debug = [l for l in session.store.log_lines(session.run_uuid) if l["level"] == "DEBUG"]
    assert len(debug) == 1 and "SUB-PSU" in debug[0]["text"]


def test_no_serial_and_an_empty_serial_are_the_same_equipment(qapp, tmp_path) -> None:
    files = {"main.tslua": """\
return ScheduleParams("Equipment bench", "nil vs empty serial")
  :setSchedule({
    StepParams("declare"):setStepNo("1.")
      :setAction(function()
        ATE.addEquipment("Jig")
        ATE.addEquipment("Jig", "")
        ATE.report(Result.PASS, "ok", "1", "1")
      end),
  })
"""}
    session = _run(qapp, tmp_path, "nilserial", files)
    assert _names(session) == ["Jig"]
    debug = [l["text"] for l in session.store.log_lines(session.run_uuid) if l["level"] == "DEBUG"]
    assert any(t == "ATE.addEquipment: Jig already declared - ignored" for t in debug), debug


def test_equipment_is_in_the_store_while_the_step_is_still_running(qapp, tmp_path) -> None:
    """"Recorded as it happens": the row exists before the declaring step ends."""
    files = {"main.tslua": """\
return ScheduleParams("Equipment bench", "declare, then keep running")
  :setSchedule({
    StepParams("declare"):setStepNo("1.")
      :setAction(function()
        ATE.addEquipment("Scope", "S1")
        local t0 = os.clock()  -- busy, with no further ATE call that could flush the row instead
        while os.clock() - t0 < 3 do end
        ATE.report(Result.PASS, "ok", "1", "1")
      end),
  })
"""}
    root = _release(tmp_path / "defs", "midrun", files)
    session = HeadlessRunSession(_config(tmp_path, root))
    session.select_schedule("Equipment bench")
    session.start()
    try:
        deadline = time.monotonic() + 2.5
        seen, running = [], False
        while time.monotonic() < deadline and not seen:
            qapp.processEvents()
            if session.run_uuid:
                seen = session.store.equipment(session.run_uuid)
                running = session._state != SessionState.FINISHED
            time.sleep(0.01)
        assert [r["name"] for r in seen] == ["Scope"]
        assert running, "the row must be there while the step is still running"
    finally:
        _pump(qapp, session)
