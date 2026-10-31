"""Packet 15a seam-1 tests: optional Step Names, header rows, and the setter load errors.

Real `.tslua` files, loaded through `LuaHost` and run through `RunEngine` (or read back through
the Test Report / the Run screen). `setInclude` here only marks a row as a header — expanding
the Sub-schedule is packet 15b — so the header has no children and the run still completes.
"""

import json
from pathlib import Path

import pytest

from ate.catalog import list_schedule_rows
from ate.config import Config
from ate.engine.lua_host import HostError, LuaHost
from ate.engine.run_engine import RunEngine
from ate.report.html import write_report
from ate.storage.store import RunStore
from ate.ui.staged_run_screen import RunView, StepsOptionsStage

from ._fixtures import _drive, _sha256
from tests._fixtures import scratch_directory


# --- schedules ---------------------------------------------------------------

NAMED_AND_UNNAMED = """\
return ScheduleParams("Names schedule", "a named step and two unnamed ones")
  :setSchedule({
    StepParams("LED colour"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "ok", "Green", "Green") end),
    StepParams():setStepNo("2.")
      :setAction(function() ATE.report(Result.FAIL, "no name", "0", "1") end),
    StepParams(nil):setStepNo("3.")
      :setAction(function() ATE.report(Result.PASS, "also no name", "1", "1") end),
  })
"""

# Two ordinary steps with a header between them: the header has a Step Number and no Action.
HEADER_AMONG_STEPS = """\
return ScheduleParams("Header schedule", "a header row among two steps")
  :setSchedule({
    StepParams("first"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "ok", "1", "1") end),
    StepParams("group header"):setStepNo("2.")
      :setInclude("sub.tslua"):setPrefix("2."),
    StepParams("last"):setStepNo("3.")
      :setAction(function() ATE.report(Result.PASS, "ok too", "1", "1") end),
  })
"""

# A header with no Step Number and no name is legal: both are optional on an include step.
HEADER_WITHOUT_NUMBER = """\
return ScheduleParams("Numberless header schedule", "a header with no number and no name")
  :setSchedule({
    StepParams("first"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "ok", "1", "1") end),
    StepParams():setInclude("sub.tslua"),
  })
"""

# Packet 15b expands `setInclude`, so a schedule with a header needs the Sub-schedule file to
# exist. An empty one keeps every 15a expectation here intact: the header has no children.
EMPTY_SUB = 'return ScheduleParams("Empty sub", "an include with no steps") :setSchedule({})\n'


def _run(source: str, tmp_path: Path, app) -> tuple[RunStore, str]:
    """Write ``source`` as a schedule, run it to completion, return the store and run uuid."""
    schedule = tmp_path / "schedule.tslua"
    schedule.write_text(source, encoding="utf-8", newline="\n")
    (tmp_path / "sub.tslua").write_text(EMPTY_SUB, encoding="utf-8", newline="\n")  # packet 15b: the include needs its file
    store = RunStore(tmp_path / "ate.db")
    engine = RunEngine(store, str(schedule))
    captured: dict[str, str] = {}
    _drive(engine, app, lambda ru: captured.__setitem__("ru", ru))
    return store, captured["ru"]


def _nodes(store: RunStore, run_uuid: str) -> dict[int, dict]:
    """The recorded node rows keyed by node_seq."""
    return {n["node_seq"]: n for n in store.nodes(run_uuid)}


# --- Step Names ------------------------------------------------------------


def test_named_step_records_its_step_name(qapp, tmp_path) -> None:
    """`StepParams(name)` puts the name on the node row."""
    store, run_uuid = _run(NAMED_AND_UNNAMED, tmp_path, qapp)
    assert _nodes(store, run_uuid)[0]["step_name"] == "LED colour"


def test_step_params_without_a_name_still_loads(tmp_path) -> None:
    """`StepParams()` and `StepParams(nil)` are both valid: the name is None, not "None"."""
    schedule = tmp_path / "schedule.tslua"
    schedule.write_text(NAMED_AND_UNNAMED, encoding="utf-8", newline="\n")
    steps = LuaHost(schedule).schedule.steps
    assert steps[0].name == "LED colour"
    assert steps[1].name is None and steps[2].name is None


def test_unnamed_step_records_a_null_step_name(qapp, tmp_path) -> None:
    """An unnamed step's node row has `step_name` NULL, and the run still completes."""
    store, run_uuid = _run(NAMED_AND_UNNAMED, tmp_path, qapp)
    nodes = _nodes(store, run_uuid)
    assert nodes[1]["step_name"] is None and nodes[2]["step_name"] is None
    assert store.get_test_run(run_uuid)["result"] == "FAIL"  # the unnamed step failed


# --- header rows -------------------------------------------------------------


def test_include_step_is_a_header_on_the_loaded_tree(tmp_path) -> None:
    """The loaded step says what it is, so later packets need not guess from `action is None`."""
    schedule = tmp_path / "schedule.tslua"
    schedule.write_text(HEADER_AMONG_STEPS, encoding="utf-8", newline="\n")
    (tmp_path / "sub.tslua").write_text(EMPTY_SUB, encoding="utf-8", newline="\n")  # packet 15b expands the header
    steps = LuaHost(schedule).schedule.steps
    assert steps[1].is_header and steps[1].kind == "header"
    assert not steps[0].is_header and steps[0].kind == "step"


def test_header_records_no_result_and_no_attempt(qapp, tmp_path) -> None:
    """The run completes with a header in the middle: no Attempt, `result` NULL, `attempt_count` 0."""
    store, run_uuid = _run(HEADER_AMONG_STEPS, tmp_path, qapp)
    nodes = _nodes(store, run_uuid)
    assert len(nodes) == 3
    header = nodes[1]
    assert header["kind"] == "header"
    assert header["result"] is None
    assert header["attempt_count"] == 0
    assert header["step_name"] == "group header"
    assert header["step_number"] == "2."
    assert [a for a in store.attempts(run_uuid) if a["node_seq"] == 1] == []
    assert store.get_test_run(run_uuid)["result"] == "PASS"  # a header cannot make a run fail


def test_header_without_a_step_number_loads_and_runs(qapp, tmp_path) -> None:
    """A header's own Step Number is optional; without one the node row carries a null number."""
    store, run_uuid = _run(HEADER_WITHOUT_NUMBER, tmp_path, qapp)
    header = _nodes(store, run_uuid)[1]
    assert header["kind"] == "header"
    assert header["step_number"] is None
    assert store.get_test_run(run_uuid)["result"] == "PASS"


def test_headers_are_recorded_when_a_pretest_action_fails(qapp, tmp_path) -> None:
    """A run aborted before any step runs still records the header row, with no Result."""
    source = """\
return ScheduleParams("Pretest fail schedule", "header alongside NOTRUN steps")
  :setPreTestAction(function() error("calibration failed") end)
  :setSchedule({
    StepParams("first"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "ok", "1", "1") end),
    StepParams("group header"):setInclude("sub.tslua"),
    StepParams(nil):setStepNo("3.")
      :setAction(function() ATE.report(Result.PASS, "ok", "1", "1") end),
  })
"""
    store, run_uuid = _run(source, tmp_path, qapp)
    nodes = _nodes(store, run_uuid)
    assert len(nodes) == 3
    assert nodes[1]["kind"] == "header" and nodes[1]["result"] is None
    assert nodes[0]["result"] == "NOTRUN" and nodes[2]["result"] == "NOTRUN"
    assert store.get_test_run(run_uuid)["result"] == "FAIL"


def test_headers_are_recorded_when_a_run_is_aborted(qapp, tmp_path) -> None:
    """An abort at the first step boundary marks later steps NOTRUN and headers as headers."""
    store = RunStore(tmp_path / "ate.db")
    schedule = tmp_path / "schedule.tslua"
    schedule.write_text(HEADER_AMONG_STEPS, encoding="utf-8", newline="\n")
    (tmp_path / "sub.tslua").write_text(EMPTY_SUB, encoding="utf-8", newline="\n")  # packet 15b expands the header
    engine = RunEngine(store, str(schedule))
    captured: dict[str, str] = {}
    engine.run_uuid_reported.connect(lambda ru: captured.__setitem__("ru", ru))
    engine.request_abort()
    _drive(engine, qapp)
    nodes = _nodes(store, captured["ru"])
    assert len(nodes) == 3
    assert nodes[1]["kind"] == "header" and nodes[1]["attempt_count"] == 0
    assert nodes[2]["result"] == "NOTRUN"
    assert nodes[0]["result"] == "ERROR"


def test_header_is_excluded_from_the_report_summary_counts(qapp, tmp_path) -> None:
    """The report counts the two steps, not the header row sitting between them."""
    store, run_uuid = _run(HEADER_AMONG_STEPS, tmp_path, qapp)
    config = Config(path=tmp_path / "config.json", reports_directory=tmp_path / "reports")
    html = write_report(store, run_uuid, config).read_text(encoding="utf-8")
    assert 'class="res pass">2' in html
    assert 'class="res notrun">0' in html
    # Packet 21b put the Steps section in: the header row does appear there (spec rule 68), with its
    # roll-up in the Result column instead of a Result, and it is still not counted above.
    assert 'class="rollup">0 steps' in html


# --- load errors -------------------------------------------------------------


def _load_error(source: str, tmp_path: Path) -> str:
    """Write ``source`` and return the HostError message loading it raises."""
    schedule = tmp_path / "schedule.tslua"
    schedule.write_text(source, encoding="utf-8", newline="\n")
    with pytest.raises(HostError) as excinfo:
        LuaHost(schedule)
    return str(excinfo.value)


def _one_step(step: str) -> str:
    """Wrap one Lua step in an otherwise valid schedule."""
    return (
        'return ScheduleParams("Bad schedule", "bad setter combination")\n'
        '  :setSchedule({\n'
        f'    {step},\n'
        '  })\n'
    )


@pytest.mark.parametrize("step", [
    'StepParams("x"):setStepNo("1."):setAction(function() end):setInclude("sub.tslua")',
    'StepParams("x"):setStepNo("1."):setInclude("sub.tslua"):setAction(function() end)',
])
def test_include_with_an_action_is_a_load_error(tmp_path, step) -> None:
    """`setAction` together with `setInclude` is a load error, whichever order they are called."""
    message = _load_error(_one_step(step), tmp_path)
    assert "schedule.tslua:1.:" in message  # the step's own number labels the error
    assert "setInclude step cannot also have an Action" in message


@pytest.mark.parametrize("step", [
    'StepParams("x"):setStepNo("1."):setInputs({ [1] = { desc = "a", type = "STRING" } }):setInclude("sub.tslua")',
    'StepParams("x"):setStepNo("1."):setInclude("sub.tslua"):setInputs({ [1] = { desc = "a", type = "STRING" } })',
])
def test_include_with_inputs_is_a_load_error(tmp_path, step) -> None:
    """`setInputs` together with `setInclude` is a load error, in either setter order."""
    message = _load_error(_one_step(step), tmp_path)
    assert "setInclude step cannot also have Step Inputs" in message


def test_prefix_without_include_is_a_load_error(tmp_path) -> None:
    """`setPrefix` means nothing on an ordinary step."""
    step = 'StepParams("x"):setStepNo("1."):setPrefix("2."):setAction(function() end)'
    message = _load_error(_one_step(step), tmp_path)
    assert "schedule.tslua:1.:" in message
    assert "setPrefix needs setInclude" in message


def test_ordinary_step_without_a_step_number_is_a_load_error(tmp_path) -> None:
    """A step that is not an include needs a Step Number; the error names its position."""
    step = 'StepParams("x"):setAction(function() end)'
    message = _load_error(_one_step(step), tmp_path)
    assert "schedule.tslua:1:" in message
    assert "ordinary step needs a Step Number" in message


def test_ordinary_step_without_an_action_is_a_load_error(tmp_path) -> None:
    """A step that is not an include needs an Action."""
    step = 'StepParams("x"):setStepNo("1.")'
    message = _load_error(_one_step(step), tmp_path)
    assert "schedule.tslua:1.:" in message
    assert "ordinary step needs an Action" in message


def test_an_empty_step_number_is_no_step_number(tmp_path) -> None:
    """`setStepNo("")` doesn't satisfy "required for an ordinary step"."""
    step = 'StepParams("x"):setStepNo(""):setAction(function() end)'
    message = _load_error(_one_step(step), tmp_path)
    assert "schedule.tslua:1:" in message
    assert "ordinary step needs a Step Number" in message


def test_a_non_string_step_number_is_a_load_error(tmp_path) -> None:
    """The setter table types the Step Number as a string; `setStepNo(7)` is not silently `"7"`."""
    step = 'StepParams("x"):setStepNo(7):setAction(function() end)'
    message = _load_error(_one_step(step), tmp_path)
    assert "Step Number must be a string" in message


@pytest.mark.parametrize("file", ["nil", "{}", '""'])
def test_set_include_without_a_file_name_is_a_load_error(tmp_path, file) -> None:
    """`setInclude(nil)` still makes an include step, and one with no usable file is a load error."""
    step = f'StepParams("x"):setStepNo("1."):setInclude({file}):setPrefix("1.")'
    message = _load_error(_one_step(step), tmp_path)
    assert "setInclude needs a file name" in message


def test_two_ordinary_steps_with_the_same_number_are_a_load_error(tmp_path) -> None:
    source = (
        'return ScheduleParams("Dup", "two steps share a number")\n'
        '  :setSchedule({\n'
        '    StepParams("a"):setStepNo("1."):setAction(function() end),\n'
        '    StepParams("b"):setStepNo("1."):setAction(function() end),\n'
        '  })\n'
    )
    message = _load_error(source, tmp_path)
    assert "schedule.tslua:1.:" in message and "duplicate Step Number '1.'" in message


def test_set_enable_and_set_prefix_on_a_header_are_allowed(tmp_path) -> None:
    schedule = tmp_path / "schedule.tslua"
    schedule.write_text(_one_step(
        'StepParams("group"):setInclude("sub.tslua"):setPrefix("3."):setEnable(false)'
    ), encoding="utf-8", newline="\n")
    (tmp_path / "sub.tslua").write_text(EMPTY_SUB, encoding="utf-8", newline="\n")  # packet 15b expands the header
    header = LuaHost(schedule).schedule.steps[0]
    assert header.is_header and header.enabled is False and header.prefix == "3."


def test_a_load_error_names_the_step_number_when_there_is_one(tmp_path) -> None:
    """The step is identified by its Step Number, not its position in the list."""
    step = 'StepParams("x"):setStepNo("4.2.1"):setPrefix("1.")'
    message = _load_error(_one_step(step), tmp_path)
    assert "schedule.tslua:4.2.1:" in message


def test_a_non_string_step_name_is_a_load_error(tmp_path) -> None:
    """A number or a table as the Step Name is a load error, not a name."""
    for name in ('7', '1.5', '{ a = 1 }', 'true'):
        message = _load_error(
            _one_step(f'StepParams({name}):setStepNo("1."):setAction(function() end)'), tmp_path
        )
        assert "Step Name must be a string" in message


def test_header_duplicating_a_sibling_step_number_is_a_load_error(tmp_path) -> None:
    """A header's Step Number sits in the parent's numbering, so it is checked for duplicates."""
    source = """\
return ScheduleParams("Duplicate header schedule", "header dups a sibling")
  :setSchedule({
    StepParams("first"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "ok", "1", "1") end),
    StepParams("group header"):setStepNo("1."):setInclude("sub.tslua"),
  })
"""
    message = _load_error(source, tmp_path)
    assert "duplicate Step Number '1.'" in message


def test_catalog_reports_a_header_misuse_as_a_load_error(tmp_path) -> None:
    """The schedule picker shows these errors like any other load failure and never offers them."""
    defs = tmp_path / "defs"
    defs.mkdir()
    bad = defs / "bad.tslua"
    bad.write_text(
        'return ScheduleParams("Bad", "prefix without include")\n'
        '  :setSchedule({ StepParams("x"):setStepNo("1."):setPrefix("2.") })\n',
        encoding="utf-8",
        newline="\n",
    )
    good = defs / "good.tslua"
    good.write_text(HEADER_AMONG_STEPS, encoding="utf-8", newline="\n")
    sub = defs / "sub.tslua"  # packet 15b: the header's Sub-schedule must exist
    sub.write_text(EMPTY_SUB, encoding="utf-8", newline="\n")
    (defs / "bad.atedef").write_text(
        json.dumps({
            "format": 1, "mediaNumber": "REL-15A", "name": "15a release", "version": "1.0.0",
            "files": [{"path": "bad.tslua", "sha256": _sha256(bad)},
                      {"path": "good.tslua", "sha256": _sha256(good)},
                      {"path": "sub.tslua", "sha256": _sha256(sub)}],
        }),
        encoding="utf-8",
        newline="\n",
    )
    rows = list_schedule_rows(defs)
    errors = [r.error for r in rows if r.error]
    assert len(errors) == 1 and "setPrefix needs setInclude" in errors[0]
    assert any(r.error is None and r.name == "Header schedule" for r in rows)


# --- rendering ---------------------------------------------------------------


def test_run_screen_step_list_shows_no_none_for_an_unnamed_step(qapp) -> None:
    """The Steps & options rows for an unnamed step are blank after the number, never "None"."""
    with scratch_directory() as td:
        schedule = Path(td) / "schedule.tslua"
        schedule.write_text(HEADER_WITHOUT_NUMBER, encoding="utf-8", newline="\n")
        (Path(td) / "sub.tslua").write_text(EMPTY_SUB, encoding="utf-8", newline="\n")  # packet 15b expands the header
        stage = StepsOptionsStage()
        stage.load_steps(LuaHost(schedule).schedule.steps)
        texts = [stage.step_list.item(i).text() for i in range(stage.step_list.count())]
        assert texts == ["1.  first", ""]
        assert "None" not in " ".join(texts)


def test_run_view_rows_of_an_unnamed_step_have_no_none(qapp, tmp_path) -> None:
    """The finished-run step rows and the Summary tab leave an unnamed step blank."""
    store, run_uuid = _run(NAMED_AND_UNNAMED, tmp_path, qapp)
    view = RunView()
    view.refresh(store.nodes(run_uuid), store.attempts(run_uuid), [])
    rows = [view.step_list.item(i).text() for i in range(view.step_list.count())]
    assert "None" not in " ".join(rows)
    assert "None" not in view.tabs.widget(1).text()


def test_report_of_an_unnamed_step_shows_no_none(qapp, tmp_path) -> None:
    """A failing unnamed step shows up in the report Failures table with an empty name."""
    store, run_uuid = _run(NAMED_AND_UNNAMED, tmp_path, qapp)
    config = Config(path=tmp_path / "config.json", reports_directory=tmp_path / "reports")
    html = write_report(store, run_uuid, config).read_text(encoding="utf-8")
    assert "None" not in html
    assert "no name" in html  # the Attempt message still names the failure
