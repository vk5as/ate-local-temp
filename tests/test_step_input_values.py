"""Ticket 10 checkboxes b/c — ``ATE.input(n)`` returns the documented typed value.

Every input type is exercised through a real LuaHost with an injected :class:`OperatorInput`
controller: blocking types block the worker thread until supplied from another thread and then
return their typed value; POLL reads sticky state that becomes available once its button is
clicked; an undeclared ``n`` raises. Verified against real .tslua schedules on disk — no mocks."""

import tempfile
import threading
import time
from pathlib import Path

import pytest

from ate.engine.lua_host import NotAllowedError, LuaHost
from ate.engine.operator_input import OperatorInput
from ate.engine.run_engine import RunEngine, RunStatus
from ate.engine.step_inputs import InvalidStepInput, StepInput
from ate.storage.store import RunStore

from ._fixtures import _drive


# A minimal schedule that loads cleanly and has no steps; the tests set up per-step inputs directly.
_MINIMAL = (
    'return ScheduleParams("Inputs", "declares step inputs")\n'
    '  :setSchedule({})\n'
)


def _host(op: OperatorInput, source: str = _MINIMAL) -> LuaHost:
    """Build a real host on disk with ``op`` injected so tests own supply/click."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "schedule.tslua"
        path.write_text(source, encoding="utf-8", newline="\n")
        return LuaHost(str(path), operator_input=op)


def _host_with(op: OperatorInput, inputs):
    """A host with one step's validated declarations installed at ``inputs`` (index-ordered)."""
    host = _host(op)
    host._step_inputs = list(inputs)
    return host


# --- typed mapping per type ---------------------------------------------------


def test_integer_is_returned_as_int():
    op = OperatorInput()
    op.supply_input(0, 42)
    host = _host_with(op, [StepInput(index=1, desc="voltage", type="INTEGER")])
    value = host.inputs(1)  # a blocking input is consumed by its single read; check both value and type once
    assert value == 42 and isinstance(value, int)


def test_float_is_returned_as_float():
    op = OperatorInput()
    op.supply_input(0, 3.5)
    host = _host_with(op, [StepInput(index=1, desc="current", type="FLOAT")])
    value = host.inputs(1)  # a blocking input is consumed by its single read; check both value and type once
    assert value == 3.5 and isinstance(value, float)


def test_string_is_returned_as_str():
    op = OperatorInput()
    op.supply_input(0, "COM7")
    host = _host_with(op, [StepInput(index=1, desc="serial", type="STRING")])
    value = host.inputs(1)  # a blocking input is consumed by its single read; check both value and type once
    assert value == "COM7" and isinstance(value, str)


def test_prompt_returns_entered_text():
    op = OperatorInput()
    op.supply_input(0, "continue now")
    host = _host_with(op, [StepInput(index=1, desc="go", type="PROMPT", prompt=("Press on"))])
    assert host.inputs(1) == "continue now"


def test_enum_radiobutton_returns_single_label():
    op = OperatorInput()
    op.supply_input(0, "Red")
    host = _host_with(op, [StepInput(index=1, desc="colour", type="ENUM", widget="RADIOBUT", enum=("Green", "Red"))])
    assert host.inputs(1) == "Red"


def test_enum_pushbutton_returns_single_label():
    op = OperatorInput()
    op.supply_input(0, "Fast")
    host = _host_with(op, [StepInput(index=1, desc="mode", type="ENUM", widget="PUSHBUT", enum=("Slow", "Fast"))])
    assert host.inputs(1) == "Fast"


def test_enum_checkbox_returns_one_lua_table_not_a_tuple():
    """A CHECKBOX answer crosses into Lua as one real table, never a Python tuple.

    Packet 29b review: lupa is configured with ``unpack_returned_tuples=True``, so a Python tuple
    returned by ``ATE.input`` arrives in Lua as several return values and ``local v = ATE.input(1)``
    binds only the first label. The mapping therefore builds a Lua table."""
    op = OperatorInput()
    op.supply_input(0, ["A", "C"])
    host = _host_with(op, [StepInput(index=1, desc="channels", type="ENUM", widget="CHECKBOX", enum=("A", "B", "C"))])
    value = host.inputs(1)
    assert isinstance(value, host._table_cls), f"got {type(value)}: a tuple would spread into separate values"
    assert not isinstance(value, tuple)
    assert value[1] == "A" and value[2] == "C"


def test_empty_checkbox_selection_is_an_empty_lua_table():
    op = OperatorInput()
    op.supply_input(0, [])
    host = _host_with(op, [StepInput(index=1, desc="channels", type="ENUM", widget="CHECKBOX", enum=("A", "B", "C"))])
    value = host.inputs(1)
    assert isinstance(value, host._table_cls)
    assert len(value) == 0


# --- blocking behaviour -------------------------------------------------------


def test_blocking_input_blocks_worker_until_supplied(app):
    """ATE.input(n) for a blocking type blocks the worker thread until supplied, then returns typed."""
    op = OperatorInput()
    host = _host_with(op, [StepInput(index=1, desc="voltage", type="INTEGER")])
    box = {}

    def worker():
        box["value"] = host.inputs(1)  # blocks on get_raw until supply_input wakes it

    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(0.3)  # give the worker time to block (no value supplied yet)
    assert "value" not in box, "blocking input returned before a value was supplied"

    op.supply_input(0, 12)  # supply from another thread; wakes the blocked get_raw
    thread.join(timeout=5)
    assert box["value"] == 12 and isinstance(box["value"], int)


def test_undeclared_index_raises_invalid_step_input():
    """An input number with no declaration raises InvalidStepInput (becomes an ERROR Attempt)."""
    op = OperatorInput()
    host = _host_with(op, [StepInput(index=1, desc="voltage", type="INTEGER")])
    with pytest.raises(Exception) as excinfo:  # noqa: B017 - InvalidStepInput is the contract
        host.inputs(99)
    assert "undeclared input 99" in str(excinfo.value)


def test_inputs_out_of_range_raises():
    op = OperatorInput()
    host = _host_with(op, [StepInput(index=1, desc="voltage", type="INTEGER")])
    with pytest.raises(Exception):  # noqa: B017 - InvalidStepInput for n below/above the range
        host.inputs(0)


# --- POLL ---------------------------------------------------------------------


def test_poll_is_nil_until_clicked_then_sticky():
    """POLL returns None until its button is clicked, then that text; sticky after a click."""
    op = OperatorInput()
    host = _host_with(op, [StepInput(index=1, desc="status", type="POLL", prompt=("OK", "Error"))])
    # Establish this step's run context (the run engine sets these before each action); POLL reads
    # are keyed by it so a click recorded for this step is read back under the same key.
    host._node_seq = 0
    host._attempt_no = 1
    # Not clicked yet -> nil.
    assert host.inputs(1) is None
    # The operator clicks the button from another thread; inputs() reads sticky state by context.
    op.click_poll(node_seq=0, attempt_no=1, index=0, text="OK")
    assert host.inputs(1) == "OK"


def test_poll_clickable_while_step_runs():
    """A POLL click during the step makes a subsequent read return that button's text."""
    op = OperatorInput()
    host = _host_with(op, [StepInput(index=1, desc="status", type="POLL", prompt=("OK", "Error"))])
    # Establish this step's run context (the run engine sets these before each action); POLL reads
    # are keyed by it so a click recorded for this step is read back under the same key.
    host._node_seq = 0
    host._attempt_no = 1
    box = {}

    def reader():
        # Read twice: first before any click (nil), then after the operator clicks.
        box["first"] = host.inputs(1)
        op.click_poll(node_seq=0, attempt_no=1, index=0, text="Error")
        box["after"] = host.inputs(1)

    thread = threading.Thread(target=reader)
    thread.start()
    time.sleep(0.2)  # let the first (nil) read happen before we click
    op.click_poll(node_seq=0, attempt_no=1, index=0, text="Error")
    thread.join(timeout=5)
    assert box["first"] is None
    assert box["after"] == "Error"


# --- integration through the Lua ATE.input path -------------------------------


def test_ate_input_rejects_call_outside_a_step(app):
    """ATE.input raises NotAllowedError when not in a step action (context check)."""
    op = OperatorInput()
    host = _host(op)  # no step inputs installed, context is "load"
    with pytest.raises(NotAllowedError):
        host.ate.input(1)


def test_undeclared_input_through_lua_raises_invalid_step_input(app):
    """A bad ATE.input(n) inside a step action surfaces as InvalidStepInput carrying "undeclared input n".

    The built-in ``ATE.input`` method raises its native exception verbatim through seam-1 (it is not
    one of the dynamically-registered functions that :meth:`_AteTable._isolate` wraps), so an undeclared
    index becomes exactly what the run engine records as an ERROR Attempt's content. Driving the real Lua
    entry point — ``host.ate.input(9)`` under a step context — asserts this contract rather than bypassing
    it with a direct ``host.inputs()`` call."""
    op = OperatorInput()
    source = (
        'return ScheduleParams("Bad input", "declares one input")\n'
        '  :setSchedule({\n'
        '    StepParams("v"):setStepNo("1.")\n'
        '      :setInputs({ [1] = { desc = "voltage", type = "INTEGER" } })\n'
        '      :setAction(function() ATE.input(9) end),\n'
        '  })\n'
    )
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "schedule.tslua"
        path.write_text(source, encoding="utf-8", newline="\n")
        host = LuaHost(str(path), operator_input=op)
        # Put the single declared input in place so only index 9 is undeclared.
        host._step_inputs = [StepInput(index=1, desc="voltage", type="INTEGER")]
        # A step action context lets the call pass the context check and reach the index lookup.
        host._context = "step"
        with pytest.raises(InvalidStepInput) as excinfo:
            host.ate.input(9)  # the real Lua entry point, not a direct host.inputs() bypass
        assert "undeclared input 9" in str(excinfo.value)


# --- what a step action actually binds (packet 29b review) ---------------------

# One step whose action reads its input into a single local and reports what Lua sees: the type,
# the length and the joined contents. A CHECKBOX that spread into several return values would bind
# only the first label, so `#v` and `table.concat` would fail rather than report.
_READ_BACK = """\
return ScheduleParams("Read back schedule", "one step that inspects what ATE.input returns")
  :setSchedule({
    StepParams("__TYPE__"):setStepNo("1.")
      :setInputs({ [1] = { desc = "value", type = "__TYPE__"__EXTRA__ } })
      :setAction(function()
        local v = ATE.input(1)
        if type(v) == "table" then
          ATE.report(Result.PASS, type(v) .. " " .. #v .. " " .. table.concat(v, "|"))
        else
          ATE.report(Result.PASS, type(v) .. " " .. tostring(v))
        end
      end),
  })
"""


def _run_read_back(app, type_name: str, extra: str, value):
    """Run one step that reads its input into a local and reports Lua's view of it."""
    op = OperatorInput()
    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        path = Path(td) / "read_back.tslua"
        path.write_text(
            _READ_BACK.replace("__TYPE__", type_name).replace("__EXTRA__", extra), encoding="utf-8",
            newline="\n",
        )
        engine = RunEngine(store, str(path), operator_input=op)
        engine.input_asked.connect(lambda seq, attempt, index: op.supply_input(index - 1, value))
        uuid = {}
        engine.run_uuid_reported.connect(lambda u: uuid.__setitem__("u", u))
        finished = threading.Event()
        engine.state_changed.connect(lambda status: finished.set() if status == RunStatus.FINISHED else None)
        engine.start()
        deadline = time.time() + 10.0
        while not finished.is_set():
            app.processEvents()
            if time.time() > deadline:
                raise TimeoutError("run did not reach FINISHED within 10 s")
        attempts = store.attempts(uuid["u"])
    return attempts[0]["pass_fail"], attempts[0]["msg"]


def test_checkbox_reads_back_as_one_lua_table_in_a_step_action(app):
    """`local v = ATE.input(1)` on a CHECKBOX binds one table holding every label."""
    result, msg = _run_read_back(app, "ENUM", ', input = "CHECKBOX", enum = {"A", "B", "C"}', ["A", "B"])
    assert result == "PASS", f"the action errored: {msg}"
    assert msg == "table 2 A|B", msg


def test_empty_checkbox_reads_back_as_an_empty_lua_table(app):
    """Nothing ticked is a legitimate answer: an empty table, not nil and not several values."""
    result, msg = _run_read_back(app, "ENUM", ', input = "CHECKBOX", enum = {"A", "B", "C"}', [])
    assert result == "PASS", f"the action errored: {msg}"
    assert msg == "table 0 ", msg


@pytest.mark.parametrize(
    "type_name,extra,supplied,expected",
    [
        ("INTEGER", ", range = {min = 1, max = 10}", 42, "number 42"),
        ("FLOAT", ", range = {min = 0.5, max = 2.0}", 1.25, "number 1.25"),
        ("STRING", ", pattern = \"^AB\"", "AB12", "string AB12"),
        ("PROMPT", ", prompt = {\"GO\"}", "GO", "string GO"),
    ],
)
def test_other_input_types_stay_scalars(app, type_name, extra, supplied, expected):
    """Only CHECKBOX returns a table; every other type still binds one number or string."""
    result, msg = _run_read_back(app, type_name, extra, supplied)
    assert result == "PASS", f"the action errored: {msg}"
    assert msg == expected, msg
