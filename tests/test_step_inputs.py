"""Ticket 10 checkbox a — Step Input declarations validated per test-schedule-format.

Every declaration is checked against ``test-schedule-format.md``: the required fields, the six
input types, ENUM's RADIOBUT/CHECKBOX/PUSHBUT widget and enum array, INTEGER/FLOAT's strict range
(``{}`` or ``{min, max}``), STRING's pattern, PROMPT/POLL prompt text, plus tooltip and orient.
Bad declarations are load errors (surfaced as HostError when a schedule loads). Verified against
real Lua tables — no mocks."""

import tempfile
from pathlib import Path

import pytest

from ate.engine.lua_host import HostError, LuaHost
from ate.engine.step_inputs import InvalidStepInput, StepInput, validate_inputs

from ._fixtures import PASSING


# --- valid declarations ------------------------------------------------------


def test_valid_enum_radiobutton_is_normalised():
    """A RADIOBUT ENUM with orient HORIZ validates and carries its widget, labels and orientation."""
    decls = {1: {"desc": "LED colour", "type": "ENUM", "input": "RADIOBUT",
                 "enum": ["Green", "Red"], "orient": "HORIZ"}}
    got = validate_inputs(decls)
    assert len(got) == 1
    inp = got[0]
    assert inp.index == 1 and inp.desc == "LED colour" and inp.type == "ENUM"
    assert inp.widget == "RADIOBUT"
    assert inp.enum == ("Green", "Red")
    assert inp.orient == "HORIZ"


def test_valid_checkbox_and_pushbutton():
    """CHECKBOX carries its labels as a tuple; PUSHBUT is accepted like RADIOBUT."""
    decls = {1: {"desc": "channels", "type": "ENUM", "input": "CHECKBOX", "enum": ["A", "B"]}}
    got = validate_inputs(decls)
    assert got[0].widget == "CHECKBOX" and got[0].enum == ("A", "B")

    decls2 = {1: {"desc": "mode", "type": "ENUM", "input": "PUSHBUT", "enum": ["Fast"]}}
    assert validate_inputs(decls2)[0].widget == "PUSHBUT"


def test_valid_integer_and_float_ranges():
    """INTEGER/FLOAT accept an empty range (unbounded) and a two-key {min, max} range."""
    decls = {1: {"desc": "voltage", "type": "INTEGER", "range": {}},
             2: {"desc": "current", "type": "FLOAT", "range": {"min": 0, "max": 5}}}
    got = validate_inputs(decls)
    assert got[0].range == {} and got[1].range == {"min": 0, "max": 5}


def test_valid_string_with_pattern():
    """A STRING with a pattern validates; orient/tooltip are accepted on any type."""
    decls = {1: {"desc": "serial", "type": "STRING", "pattern": r"COM\d+", "orient": "VERT"}}
    got = validate_inputs(decls)
    assert got[0].pattern == r"COM\d+" and got[0].orient == "VERT"


def test_integer_and_float_keep_tooltip_and_orient():
    """tooltip and orient are declared on every type (test-schedule-format.md), the line edits too.

    Packet 29b review found INTEGER and FLOAT dropping both at validation."""
    got = validate_inputs({
        1: {"desc": "soak", "type": "INTEGER", "range": {"min": 1, "max": 10},
            "tooltip": "Whole minutes only", "orient": "VERT"},
        2: {"desc": "rail", "type": "FLOAT", "tooltip": "Read the meter", "orient": "HORIZ"},
    })
    assert got[0].tooltip == "Whole minutes only" and got[0].orient == "VERT"
    assert got[1].tooltip == "Read the meter" and got[1].orient == "HORIZ"
    # and they reach the stored node JSON in schedule form
    assert got[0].to_dict()["tooltip"] == "Whole minutes only"
    assert got[0].to_dict()["orient"] == "VERT"


def test_valid_prompt_and_poll():
    """PROMPT blocks on a string prompt; POLL accepts an array of prompt strings."""
    decls = {1: {"desc": "continue", "type": "PROMPT", "prompt": "Press to continue"}}
    assert validate_inputs(decls)[0].prompt == ("Press to continue",)

    decls2 = {1: {"desc": "status", "type": "POLL", "prompt": ["OK", "Error"]}}
    got = validate_inputs(decls2)
    assert got[0].prompt == ("OK", "Error")


def test_indices_are_preserved_in_order():
    """Declarations are returned in ascending index order regardless of dict insertion order."""
    decls = {3: {"desc": "c", "type": "STRING"}, 1: {"desc": "a", "type": "STRING"},
             2: {"desc": "b", "type": "STRING"}}
    got = validate_inputs(decls)
    assert [i.index for i in got] == [1, 2, 3]


# --- invalid declarations ----------------------------------------------------


def test_missing_desc_raises():
    with pytest.raises(InvalidStepInput):
        validate_inputs({1: {"type": "STRING"}})


def test_empty_desc_raises():
    with pytest.raises(InvalidStepInput):
        validate_inputs({1: {"desc": "", "type": "STRING"}})


def test_unknown_type_raises():
    with pytest.raises(InvalidStepInput, match="type must be one of"):
        validate_inputs({1: {"desc": "x", "type": "BOOLEAN"}})


def test_enum_without_widget_raises():
    with pytest.raises(InvalidStepInput, match="ENUM input must be one of"):
        validate_inputs({1: {"desc": "x", "type": "ENUM", "enum": ["a"]}})


def test_enum_bad_widget_raises():
    with pytest.raises(InvalidStepInput):
        validate_inputs({1: {"desc": "x", "type": "ENUM", "input": "BUTTON", "enum": ["a"]}})


def test_enum_empty_labels_raises():
    with pytest.raises(InvalidStepInput, match="at least one option label"):
        validate_inputs({1: {"desc": "x", "type": "ENUM", "input": "RADIOBUT", "enum": []}})


def test_enum_non_string_label_raises():
    with pytest.raises(InvalidStepInput):
        validate_inputs({1: {"desc": "x", "type": "ENUM", "input": "RADIOBUT", "enum": ["a", 2]}})


def test_integer_single_key_range_raises():
    """A range with only one bound (e.g. {max}) is rejected — the format is strict."""
    with pytest.raises(InvalidStepInput, match="range must be"):
        validate_inputs({1: {"desc": "x", "type": "INTEGER", "range": {"max": 5}}})


def test_range_non_numeric_bound_raises():
    with pytest.raises(InvalidStepInput):
        validate_inputs({1: {"desc": "x", "type": "FLOAT", "range": {"min": 0, "max": "hi"}}})


def test_string_pattern_not_a_string_raises():
    with pytest.raises(InvalidStepInput, match="STRING pattern must be a string"):
        validate_inputs({1: {"desc": "x", "type": "STRING", "pattern": 123}})


def test_prompt_missing_raises():
    with pytest.raises(InvalidStepInput, match="PROMPT/POLL needs 'prompt'"):
        validate_inputs({1: {"desc": "x", "type": "PROMPT"}})


def test_bad_orient_raises():
    with pytest.raises(InvalidStepInput, match="orient must be one of"):
        validate_inputs({1: {"desc": "x", "type": "STRING", "pattern": r"\d+", "orient": "DIAGONAL"}})


def test_tooltip_not_a_string_raises():
    with pytest.raises(InvalidStepInput, match="tooltip must be a string"):
        validate_inputs({1: {"desc": "x", "type": "STRING", "pattern": r"\d+", "tooltip": 42}})


def test_field_on_wrong_type_raises():
    """A field that only applies to one type is rejected wherever it doesn't belong."""
    # enum on an INTEGER
    with pytest.raises(InvalidStepInput):
        validate_inputs({1: {"desc": "x", "type": "INTEGER", "input": "RADIOBUT", "enum": ["a"]}})
    # range on a STRING
    with pytest.raises(InvalidStepInput):
        validate_inputs({1: {"desc": "x", "type": "STRING", "range": {}}})


# --- integration through the Lua host ----------------------------------------


def _host_with_source(source: str) -> LuaHost:
    """Build a host for one schedule on disk (named schedule.tslua so errors carry its name)."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "schedule.tslua"
        path.write_text(source, encoding="utf-8", newline="\n")
        return LuaHost(str(path))


def test_host_loads_a_schedule_with_valid_inputs():
    """A schedule declaring inputs loads cleanly and the runner can read them back."""
    source = (
        'return ScheduleParams("Inputs", "declared inputs")\n'
        '  :setSchedule({\n'
        '    StepParams("Colour"):setStepNo("1.")\n'
        '      :setInputs({ [1] = { desc = "LED colour", type = "ENUM", input = "RADIOBUT",\n'
        '                         enum = {"Green", "Red"}, orient = "HORIZ" } })\n'
        '      :setAction(function() end)\n'
        '  })\n'
    )
    host = _host_with_source(source)
    step = host.schedule.steps[0]
    keys = list(step.inputs.keys())
    assert keys == [1]
    # The declaration round-trips through the host untouched until validation runs.
    assert dict(step.inputs)[1]["type"] == "ENUM"


def test_host_rejects_a_bad_input_declaration():
    """A schedule with a bad declaration fails to load — the error carries the schedule's filename."""
    source = (
        'return ScheduleParams("Bad", "bad input")\n'
        '  :setSchedule({\n'
        '    StepParams("Colour"):setStepNo("1."):setAction(function() end)\n'
        '      :setInputs({ [1] = { desc = "LED colour", type = "ENUM", input = "BUTTON" } })\n'
        '  })\n'
    )
    with pytest.raises(HostError) as excinfo:
        _host_with_source(source)
    assert "schedule.tslua:" in str(excinfo.value)
    assert "ENUM input must be one of" in str(excinfo.value)
