"""Packet 10a — Step Input widgets on the staged Run screen.

Every declared Step Input is drawn under the step that declared it, one widget per type, and the
Operator answers it by clicking that widget: the run records exactly what the click or the typed
text said. Everything here drives the real :class:`StagedRunScreen` offscreen against real
``.atedef`` trees on disk — no session shortcuts, no mocked engine.

The look follows the run-screen mockup (`prototype/run-screen`, lines 49-54 and `inputHTML`):
an asking input paints `.inp.inp-on` — background `#fff6cc`, left border `#e0a800` — and takes focus;
an invalid value shows the mockup's `.err` line (`#b71c1c`) and is never submitted.
"""

import json
import time
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLineEdit,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.ui.staged_run_screen import StepInputWidget, StagedRunScreen

#: The mockup's `.inp.inp-on` (line 54): background and left border, read off the pixels.
ACTIVE_BG = (255, 246, 204)  # #fff6cc
ACTIVE_BORDER = (224, 168, 0)  # #e0a800
#: The mockup's `.err` (line 28).
ERR_RGB = (183, 28, 28)  # #b71c1c

SHOTS = Path("/tmp/10a-shots")

# One step declaring every Step Input type the format allows, with orient and tooltip set.
ALL_TYPES = """\
return ScheduleParams("Widget types schedule", "a step declaring every input type")
  :setSchedule({
    StepParams("every type"):setStepNo("1.")
      :setInputs({
        [1] = { desc = "LED colour", type = "ENUM", input = "RADIOBUT",
                enum = {"Green", "Red", "Off"}, orient = "HORIZ", tooltip = "Colour of the POWER LED" },
        [2] = { desc = "Fan modes fitted", type = "ENUM", input = "CHECKBOX",
                enum = {"Low", "Med", "High"}, orient = "VERT", tooltip = "Tick every mode present" },
        [3] = { desc = "Probe fitted?", type = "ENUM", input = "PUSHBUT",
                enum = {"Yes", "No"}, tooltip = "Was the probe fitted to TP3" },
        [4] = { desc = "Soak minutes", type = "INTEGER", range = {min = 1, max = 10}, tooltip = "Whole minutes only" },
        [5] = { desc = "Rail voltage", type = "FLOAT", range = {min = 0.5, max = 2.0}, orient = "HORIZ" },
        [6] = { desc = "Serial on label", type = "STRING", pattern = "^PSU-[0-9]{5}$" },
        [7] = { desc = "Remove the probe", type = "PROMPT", prompt = {"Done"} },
        [8] = { desc = "Front panel button", type = "POLL", prompt = {"Pressed", "Not working"},
                tooltip = "Click when you hear the relay" },
      })
      :setAction(function() ATE.report(Result.PASS, "nothing read") end),
  })
"""

# A step whose only input is one blocking INTEGER with a range: the smoke test answers it by typing
# into the widget and clicking OK.
BLOCKING_INTEGER = """\
return ScheduleParams("Voltage check schedule", "a step that waits for one INTEGER")
  :setSchedule({
    StepParams("voltage"):setStepNo("1.")
      :setInputs({ [1] = { desc = "measured voltage", type = "INTEGER", range = {min = 1, max = 10} } })
      :setAction(function()
        local v = ATE.input(1)
        ATE.report(Result.PASS, "voltage " .. v)
      end),
  })
"""

# Two steps: the first polls a button while it runs, the second waits on an INTEGER. The POLL's
# widget is live only while its own step runs, so it is closed by the time step 2 is asking.
POLL_THEN_BLOCKING = """\
return ScheduleParams("Poll then blocking schedule", "a POLL step followed by a blocking one")
  :setSchedule({
    StepParams("button press"):setStepNo("1.")
      :setInputs({ [1] = { desc = "front panel button", type = "POLL",
                          prompt = {"Pressed", "Not working"}, tooltip = "Click when you hear the relay" } })
      :setAction(function()
        local v = ATE.input(1)
        ATE.report(Result.PASS, "button " .. tostring(v))
      end),
    StepParams("trim gain"):setStepNo("2.")
      :setInputs({ [1] = { desc = "gain", type = "INTEGER", range = {min = 0, max = 5} } })
      :setAction(function()
        local g = ATE.input(1)
        ATE.report(Result.PASS, "gain " .. g)
      end),
  })
"""


def _write_env(tmp_path: Path, schedules: dict[str, str]) -> dict[str, Path]:
    """One Test Definition holding ``schedules`` (name -> source) and a config pointing at it."""
    defs = tmp_path / "defs"
    defs.mkdir(parents=True, exist_ok=True)
    files = [{"path": name, "sha256": __import__("hashlib").sha256(text.encode()).hexdigest()}
             for name, text in schedules.items()]
    for name, text in schedules.items():
        (defs / name).write_text(text, encoding="utf-8")
    (defs / "widgets.atedef").write_text(
        json.dumps({"format": 1, "mediaNumber": "MR-10a", "name": "Widget release", "version": "1.0.0",
                    "files": files}),
        encoding="utf-8",
    )
    config = {
        "format": 1,
        "station": {"name": "BENCH-03", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c10"},
        "database": {"type": "sqlite", "path": str(tmp_path / "ate.db")},
        "reports": {"directory": str(tmp_path / "reports")},
        "testDefinitions": {"directories": [str(defs)]},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return {"config": config_path, "defs": defs}


def _find_item(list_widget, substring):
    """The schedule row whose text contains ``substring``."""
    for i in range(list_widget.count()):
        if substring in list_widget.item(i).text():
            return list_widget.item(i)
    raise AssertionError(f"no schedule row containing {substring!r}")


def _select(screen, schedule_name: str) -> None:
    """Pick a schedule and preview its steps, exactly as an Operator does."""
    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    screen.test_schedule_stage.schedule_list.setCurrentItem(
        _find_item(screen.test_schedule_stage.schedule_list, schedule_name)
    )
    screen._begin_run_from_selection()
    assert screen.steps_stack.currentIndex() == 2


def _pump_until(qapp, predicate, timeout: float = 10.0) -> bool:
    """Pump the event loop until ``predicate`` is true, or time out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _wait_finished(qapp, session) -> bool:
    """Pump until the run reaches FINISHED (a Pause-on-Fail never happens in these schedules)."""
    return _pump_until(qapp, lambda: session._state == SessionState.FINISHED)


def _pixels(widget) -> set[tuple[int, int, int]]:
    """Every distinct colour a widget paints, straight from its grab."""
    image = widget.grab().toImage()
    return {(image.pixelColor(x, y).red(), image.pixelColor(x, y).green(), image.pixelColor(x, y).blue())
            for y in range(image.height()) for x in range(image.width())}


def _recorded_inputs(store, run_uuid: str) -> list[tuple[int, str, str]]:
    """(input_index, type, value) of every input_value row the run recorded, in order."""
    return [(row["input_index"], row["type"], row["value"]) for row in store.input_values(run_uuid)]


# -- the widgets exist, per type, under their step ---------------------------


def test_every_declared_input_has_a_widget_under_its_step(qapp, tmp_path):
    """Each Step Input type renders the widget kind the mockup's inputHTML uses."""
    screen = StagedRunScreen(HeadlessRunSession(_write_env(tmp_path, {"all.tslua": ALL_TYPES})["config"]))
    _select(screen, "Widget types schedule")

    widgets = screen.steps_options_stage.input_widgets[0]
    assert len(widgets) == 8, [w.declaration.type for w in widgets]

    radio, check, push, integer, flt, string, prompt, poll = widgets
    assert isinstance(radio, StepInputWidget) and radio.declaration.type == "ENUM"
    assert len(radio.radios) == 3 and all(isinstance(r, QRadioButton) for r in radio.radios)
    assert radio.ok_button is not None and radio.line_edit is None

    assert len(check.checks) == 3 and all(isinstance(c, QCheckBox) for c in check.checks)
    assert check.ok_button is not None

    # PUSHBUT: one button per choice, no OK — the click is the answer.
    assert len(push.choice_buttons) == 2
    assert [b.text() for b in push.choice_buttons] == ["Yes", "No"]
    assert push.ok_button is None

    assert isinstance(integer.line_edit, QLineEdit) and integer.ok_button is not None
    assert integer.line_edit.placeholderText() == "1 to 10"
    assert flt.line_edit.placeholderText() == "0.5 to 2.0"
    assert string.line_edit.placeholderText() == "^PSU-[0-9]{5}$"

    assert [b.text() for b in prompt.choice_buttons] == ["Done"]
    assert [b.text() for b in poll.choice_buttons] == ["Pressed", "Not working"]

    # orient: VERT stacks the controls, HORIZ (the mockup default) puts them in a row.
    for widget, vertical in ((radio, False), (check, True), (flt, False)):
        layout = widget.findChild(QWidget, "inp_w").layout()
        assert isinstance(layout, QVBoxLayout if vertical else QHBoxLayout), widget.declaration.desc

    # tooltip: the hover text the declaration carries.
    assert radio.toolTip() == "Colour of the POWER LED"
    assert push.toolTip() == "Was the probe fitted to TP3"
    assert flt.toolTip() == ""


def test_inputs_sit_as_rows_under_the_step_that_declared_them(qapp, tmp_path):
    """The run view lists every step, and its inputs immediately below it, always visible."""
    screen = StagedRunScreen(HeadlessRunSession(_write_env(tmp_path, {"all.tslua": ALL_TYPES})["config"]))
    _select(screen, "Widget types schedule")
    screen._reveal_run_view()
    screen.show()
    qapp.processEvents()
    screen.run_view.load_run_steps(screen._load_schedule_steps("Widget types schedule"))

    step_list = screen.run_view.step_list
    rows = [step_list.item(i) for i in range(step_list.count())]
    assert rows[0].text() == "1.  every type", rows[0].text()
    for offset in range(1, 9):
        item = rows[offset]
        widget = step_list.itemWidget(item)
        assert isinstance(widget, StepInputWidget), item.text()
        assert item.text().startswith("  "), item.text()  # indented one level under the step
        assert widget.isVisible(), widget.declaration.desc
        for control in widget.controls:
            assert not control.isEnabled(), f"{widget.declaration.desc}: live before the run asked"


# -- a blocking input: inert, then live, then closed with its value ----------


def test_blocking_input_is_answered_through_its_widget(qapp, tmp_path):
    """The packet's smoke test: type into the widget, click OK, and the run records that value."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    env = _write_env(tmp_path, {"blocking.tslua": BLOCKING_INTEGER})
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    _select(screen, "Voltage check schedule")

    widget = screen.run_view.input_widget(0, 1)
    assert widget is None, "the Run view has no rows until the run starts"

    screen.steps_options_stage.run_button.click()
    screen.show()
    qapp.processEvents()
    widget = screen.run_view.input_widget(0, 1)
    assert widget is not None, "the step's input has no widget on the Run view"

    # Before the worker asks: every control is disabled.
    assert all(not control.isEnabled() for control in widget.controls)
    assert ACTIVE_BG not in _pixels(widget)

    # While ATE.input(1) waits, the widget is live and styled the mockup's way.
    assert _pump_until(qapp, lambda: all(control.isEnabled() for control in widget.controls)), (
        "the widget never opened while the worker blocked on ATE.input(1)"
    )
    pixels = _pixels(widget)
    assert ACTIVE_BG in pixels, "the asking input does not paint #fff6cc"
    assert ACTIVE_BORDER in pixels, "the asking input has no #e0a800 left border"
    assert qapp.focusWidget() in widget.controls, "the active input did not take focus"

    screen.run_view.grab().save(str(SHOTS / "inputs-active.png"))

    QTest.keyClicks(widget.line_edit, "7")
    QTest.mouseClick(widget.ok_button, Qt.MouseButton.LeftButton)

    assert _wait_finished(qapp, session)

    rows = _recorded_inputs(session.store, session.run_uuid)
    assert rows == [(1, "INTEGER", "7")], rows

    # Answered: disabled again, and the value it recorded is what the widget shows.
    assert all(not control.isEnabled() for control in widget.controls)
    assert widget.line_edit.text() == "7"
    assert ACTIVE_BG not in _pixels(widget)
    screen.run_view.grab().save(str(SHOTS / "inputs-done.png"))


def test_an_out_of_range_integer_shows_the_error_and_is_not_submitted(qapp, tmp_path):
    """The mockup's inline `.err` for a bad value: shown, and the step keeps waiting."""
    env = _write_env(tmp_path, {"blocking.tslua": BLOCKING_INTEGER})
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    _select(screen, "Voltage check schedule")
    screen.steps_options_stage.run_button.click()
    screen.show()
    qapp.processEvents()

    widget = screen.run_view.input_widget(0, 1)
    assert _pump_until(qapp, lambda: widget.ok_button.isEnabled()), "the input never opened"

    QTest.keyClicks(widget.line_edit, "99")
    QTest.mouseClick(widget.ok_button, Qt.MouseButton.LeftButton)
    qapp.processEvents()

    assert widget.error_label.text() == "Must be 1 to 10", widget.error_label.text()
    assert ERR_RGB in _pixels(widget.error_label), "the error is not the mockup's #b71c1c"
    assert _recorded_inputs(session.store, session.run_uuid) == [], "a rejected value was submitted"
    # The step is still waiting, so the widget is still live.
    assert widget.ok_button.isEnabled()

    # Not a number at all: the mockup's other message.
    QTest.keyClicks(widget.line_edit, "abc")
    QTest.mouseClick(widget.ok_button, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert widget.error_label.text() == "Enter a whole number", widget.error_label.text()

    QTest.keyClicks(widget.line_edit, "abc")  # replaced below; keeps the assertion below simple
    widget.line_edit.setText("3")
    QTest.mouseClick(widget.ok_button, Qt.MouseButton.LeftButton)
    assert _wait_finished(qapp, session)
    assert _recorded_inputs(session.store, session.run_uuid) == [(1, "INTEGER", "3")]


def test_a_radio_input_needs_a_choice_before_ok(qapp, tmp_path):
    """RADIOBUT with nothing ticked shows 'Choose one' and submits nothing."""
    src = """\
return ScheduleParams("Radio schedule", "a RADIOBUT answered by picking one")
  :setSchedule({
    StepParams("LED colour"):setStepNo("1.")
      :setInputs({ [1] = { desc = "LED colour", type = "ENUM", input = "RADIOBUT",
                           enum = {"Green", "Red", "Off"}, orient = "HORIZ" } })
      :setAction(function()
        local c = ATE.input(1)
        ATE.report(Result.PASS, "colour " .. c)
      end),
  })
"""
    env = _write_env(tmp_path, {"radio.tslua": src})
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    _select(screen, "Radio schedule")
    screen.steps_options_stage.run_button.click()
    screen.show()
    qapp.processEvents()

    widget = screen.run_view.input_widget(0, 1)
    assert _pump_until(qapp, lambda: widget.ok_button.isEnabled()), "the input never opened"

    QTest.mouseClick(widget.ok_button, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert widget.error_label.text() == "Choose one", widget.error_label.text()
    assert _recorded_inputs(session.store, session.run_uuid) == []

    radio = next(r for r in widget.radios if r.text() == "Red")
    QTest.mouseClick(radio, Qt.MouseButton.LeftButton)
    QTest.mouseClick(widget.ok_button, Qt.MouseButton.LeftButton)
    assert _wait_finished(qapp, session)
    assert _recorded_inputs(session.store, session.run_uuid) == [(1, "ENUM", "Red")]
    assert [r.isChecked() for r in widget.radios] == [False, True, False]
    assert all(not control.isEnabled() for control in widget.controls)


# -- POLL: clickable only while its step runs, and its click is recorded ----


def test_a_poll_button_is_live_only_while_its_step_runs(qapp, tmp_path):
    """The POLL opens when its step announces it, closes when answered, and its click is recorded."""
    env = _write_env(tmp_path, {"poll.tslua": POLL_THEN_BLOCKING})
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    _select(screen, "Poll then blocking schedule")

    poll = screen.steps_options_stage.input_widgets[0][0]
    assert all(not control.isEnabled() for control in poll.controls), "a POLL is live before the run"

    screen.steps_options_stage.run_button.click()
    screen.show()
    qapp.processEvents()

    poll = screen.run_view.input_widget(0, 1)
    gain = screen.run_view.input_widget(1, 1)

    # Its step announced its inputs, so the POLL is clickable now.
    assert _pump_until(qapp, lambda: any(b.isEnabled() for b in poll.choice_buttons)), "the POLL never opened"
    assert ACTIVE_BG in _pixels(poll), "the live POLL is not styled active"
    assert all(not b.isEnabled() for b in gain.choice_buttons), "step 2's input opened too early"

    pressed = next(b for b in poll.choice_buttons if b.text() == "Pressed")
    QTest.mouseClick(pressed, Qt.MouseButton.LeftButton)
    qapp.processEvents()

    # Answered: closed, and it shows what was clicked.
    assert all(not b.isEnabled() for b in poll.choice_buttons)
    assert poll.value_label.text() == "clicked: Pressed", poll.value_label.text()

    # Step 2 is now the one running: its input is live, the POLL's is not.
    assert _pump_until(qapp, lambda: gain.ok_button.isEnabled()), "step 2's input never opened"
    assert all(not b.isEnabled() for b in poll.choice_buttons), "the POLL stayed clickable after its step"
    assert screen.run_view.input_widget(0, 1) is poll, "the POLL widget was rebuilt mid-run"

    QTest.keyClicks(gain.line_edit, "2")
    QTest.mouseClick(gain.ok_button, Qt.MouseButton.LeftButton)
    assert _wait_finished(qapp, session)

    recorded = _recorded_inputs(session.store, session.run_uuid)
    assert (1, "POLL", "Pressed") in recorded, recorded
    assert (1, "INTEGER", "2") in recorded, recorded
