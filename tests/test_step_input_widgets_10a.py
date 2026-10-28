"""Packet 10a — Step Input widgets on the staged Run screen.

Every declared Step Input is drawn under the step that declared it, one widget per type, and the
Operator answers it by clicking that widget: the run records exactly what the click or the typed
text said. Everything here drives the real :class:`StagedRunScreen` offscreen against real
``.atedef`` trees on disk — no session shortcuts, no mocked engine.

The look follows the run-screen mockup (`prototype/run-screen`, lines 49-54 and `inputHTML`):
an asking input paints `.inp.inp-on` — background `#fff6cc`, left border `#e0a800` — and takes focus;
an invalid value shows the mockup's `.err` line (`#b71c1c`) and is never submitted.

The packet's review round added the rest of what the mockup promises: the line edit and the
radio/checkbox rows are real controls in the row (not decoration), a row's text is empty because the
widget already draws the description, an input is live only while the call that asked for it is
waiting, a POLL stays clickable for the whole of its step and closes when the step ends, the session
refuses an answer that belongs to no waiting input, and the widgets survive every rebuild of the
step list — including a Stop-on-Fail pause, which used to delete them and crash the process.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from PyQt6 import sip
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLineEdit,
    QLabel,
    QRadioButton,
    QSizePolicy,
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
#: `.inp input[type=text] { width: 140px }` and `.inputs { margin-left: 26px }`.
INPUT_WIDTH = 140
INDENT = 26

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

# A POLL and a blocking INTEGER on the SAME step: the POLL is live for the whole Attempt, so its
# buttons stay clickable while the step waits on the value, and every click is recorded.
POLL_WHILE_WAITING = """\
return ScheduleParams("Poll while waiting", "a POLL clicked while its own step waits for a number")
  :setSchedule({
    StepParams("probe"):setStepNo("1.")
      :setInputs({
        [1] = { desc = "front panel button", type = "POLL", prompt = {"Pressed", "Not working"},
                tooltip = "Click when you hear the relay" },
        [2] = { desc = "gain", type = "INTEGER", range = {min = 0, max = 5} },
      })
      :setAction(function()
        local g = ATE.input(2)
        local p = ATE.input(1)
        ATE.report(Result.PASS, "gain " .. g .. " button " .. tostring(p))
      end),
    StepParams("after"):setStepNo("2.")
      :setInputs({ [1] = { desc = "never clicked", type = "POLL", prompt = {"Done"} } })
      :setAction(function() ATE.report(Result.PASS, "the POLL was never clicked") end),
  })
"""

# Two blocking inputs read one after the other: the second widget stays inert until the action
# actually asks for it.
TWO_BLOCKING = """\
return ScheduleParams("Two reads schedule", "a step that waits on two inputs in turn")
  :setSchedule({
    StepParams("two reads"):setStepNo("1.")
      :setInputs({
        [1] = { desc = "first value", type = "INTEGER", range = {min = 1, max = 9} },
        [2] = { desc = "second value", type = "INTEGER", range = {min = 1, max = 9} },
      })
      :setAction(function()
        local a = ATE.input(1)
        local b = ATE.input(2)
        ATE.report(Result.PASS, a .. " then " .. b)
      end),
  })
"""

# Enough rows that the asked input is below the fold of the Run view: answering it scrolls to it.
_MANY = "\n".join(
    f'    StepParams("step {i}"):setStepNo("{i}."):setAction(function() ATE.report(Result.PASS, "ok") end),'
    for i in range(1, 31)
)
LONG_LAST_INPUT = (
    'return ScheduleParams("Long schedule", "thirty plain steps then one with an input")\n'
    "  :setSchedule({\n"
    + _MANY + "\n"
    '    StepParams("last"):setStepNo("31.")\n'
    '      :setInputs({ [1] = { desc = "final value", type = "INTEGER" } })\n'
    "      :setAction(function()\n"
    "        local v = ATE.input(1)\n"
    '        ATE.report(Result.PASS, "final " .. v)\n'
    "      end),\n"
    "  })\n"
)

# A Stop-on-Fail run with input rows on screen: the pause must not delete those widgets.
PAUSE_WITH_INPUTS = """\
return ScheduleParams("Pause with inputs", "an input step then a failing step, stop on fail")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("gain"):setStepNo("1.")
      :setInputs({ [1] = { desc = "gain", type = "INTEGER", range = {min = 0, max = 5} } })
      :setAction(function()
        local g = ATE.input(1)
        ATE.report(Result.PASS, "gain " .. g)
      end),
    StepParams("fails"):setStepNo("2.")
      :setInputs({ [1] = { desc = "retry count", type = "INTEGER" } })
      :setAction(function()
        local r = ATE.input(1)
        ATE.report(Result.FAIL, "failed at " .. r)
      end),
  })
"""

#: The program the Stop-on-Fail subprocess tests run: it answers the widgets with real clicks,
#: answers the failure prompt with its buttons, and exits 0 only if the run FINISHED with every
#: input widget still alive. A segfault shows up as exit code 139.
PAUSE_CHILD = '''\
import hashlib
import json
import sys
import time
from pathlib import Path

from PyQt6 import sip
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.ui.staged_run_screen import StagedRunScreen
from tests._fixtures import WRITES_PDF, fake_pdf_helper

import ate.report.pdf_job
ate.report.pdf_job.pdf_helper_command = fake_pdf_helper(WRITES_PDF)

choice = sys.argv[1]
schedule = Path(sys.argv[2])
source = schedule.read_text(encoding="utf-8")
work = schedule.parent

defs = work / "defs"
defs.mkdir(parents=True, exist_ok=True)
(defs / "pause.tslua").write_text(source, encoding="utf-8")
(defs / "pause.atedef").write_text(json.dumps({
    "format": 1, "mediaNumber": "MR-10a", "name": "Pause release", "version": "1.0.0",
    "files": [{"path": "pause.tslua", "sha256": hashlib.sha256(source.encode()).hexdigest()}],
}), encoding="utf-8")
config = {"format": 1, "station": {"name": "BENCH-03", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c10"},
          "database": {"type": "sqlite", "path": str(work / "ate.db")},
          "reports": {"directory": str(work / "reports")},
          "testDefinitions": {"directories": [str(defs)]}}
config_path = work / "config.json"
config_path.write_text(json.dumps(config), encoding="utf-8")

app = QApplication([])
session = HeadlessRunSession(config_path)
screen = StagedRunScreen(session)
screen.resize(1280, 800)
screen.uut_operator_stage.operator_edit.setText("Ada")
screen._advance(1)
list_widget = screen.test_schedule_stage.schedule_list
row = next(list_widget.item(i) for i in range(list_widget.count())
           if "Pause with inputs" in list_widget.item(i).text())
list_widget.setCurrentItem(row)
screen._begin_run_from_selection()
screen.steps_options_stage.run_button.click()
screen.show()


def answer(seq, index, value):
    """Type `value` into that step's line edit and click OK, whenever it is live."""
    widget = screen.run_view.input_widget(seq, index)
    if widget is None or sip.isdeleted(widget) or widget.line_edit is None:
        return
    if widget.ok_button.isEnabled():
        widget.line_edit.setText(str(value))
        QTest.mouseClick(widget.ok_button, Qt.MouseButton.LeftButton)


prompts = 0
deadline = time.time() + 20
while time.time() < deadline and session._state != SessionState.FINISHED:
    app.processEvents()
    bar = screen.run_view.failure_prompt_bar
    if bar.isVisible():
        if prompts == 0:
            {"CONTINUE": bar.continue_button, "RETRY": bar.retry_button,
             "ABORT": bar.abort_button}[choice].click()
        else:
            bar.abort_button.click()  # a retried Attempt failing again: end the run
        prompts += 1
    answer(0, 1, 3)
    answer(1, 1, 7 + prompts)

QTest.qWait(150)
app.processEvents()
QTest.qWait(150)
app.processEvents()

widgets = [w for ws in screen.run_view.input_widgets.values() for w in ws]
alive = all(not sip.isdeleted(w) for w in widgets)
print("prompts", prompts, "widgets", len(widgets), "alive", alive)
if session._state != SessionState.FINISHED or not alive:
    print("run did not finish cleanly")
    sys.exit(1)
print("FINISHED")
'''


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


def _left_edge(widget) -> int:
    """How far the row's first control sits from the left of its row — the mockup's 26 px indent."""
    return widget.pos().x() + min(label.pos().x() for label in widget.findChildren(QLabel))


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
    """The run view lists every step, and its inputs immediately below it, always visible.

    Packet 10a review: the row item carries no text of its own — the widget already draws the
    description, so any text would be painted twice — and the indent comes from the widget's own
    left margin, the mockup's `.inputs { margin-left: 26px }`."""
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
        assert item.text() == "", f"the input row draws {item.text()!r} under its widget"
        assert _left_edge(widget) >= INDENT, f"{widget.declaration.desc} is not indented 26px"
        assert widget.isVisible(), widget.declaration.desc
        for control in widget.controls:
            assert not control.isEnabled(), f"{widget.declaration.desc}: live before the run asked"


def test_the_line_edit_is_a_140px_control_in_the_row(qapp, tmp_path):
    """INTEGER/FLOAT/STRING draw their line edit as a real control, sized like the mockup.

    The review found the line edit built but never added to the layout, so the widget had no way to
    answer at all: it must be inside the row's control block, 140 px wide per `.inp input`."""
    env = _write_env(tmp_path, {"blocking.tslua": BLOCKING_INTEGER})
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    _select(screen, "Voltage check schedule")
    screen.steps_options_stage.run_button.click()
    screen.show()
    qapp.processEvents()

    widget = screen.run_view.input_widget(0, 1)
    controls_widget = widget.findChild(QWidget, "inp_w")
    assert widget.line_edit in widget.controls, "the line edit is not one of the row's controls"
    assert widget.line_edit.parent() is controls_widget, "the line edit is not in the row's control block"
    assert widget.line_edit.width() == INPUT_WIDTH, f"the line edit is {widget.line_edit.width()} px wide"
    assert widget.line_edit.isVisible()

    # `focusActive`: the waiting input gets the cursor, so Enter submits it.
    assert _pump_until(qapp, lambda: widget.line_edit.isEnabled()), "the input never opened"
    assert qapp.focusWidget() is widget.line_edit, "the asking line edit did not take focus"


def test_radio_and_checkbox_controls_open_and_close_with_the_step(qapp, tmp_path):
    """The radios and checkboxes are in `controls`, so `activate`/`deactivate` reach them."""
    env = _write_env(tmp_path, {"all.tslua": ALL_TYPES})
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    _select(screen, "Widget types schedule")
    screen._reveal_run_view()
    screen.show()
    qapp.processEvents()
    screen.run_view.load_run_steps(screen._load_schedule_steps("Widget types schedule"))

    radio = screen.run_view.input_widget(0, 1)
    check = screen.run_view.input_widget(0, 2)
    assert all(r in radio.controls for r in radio.radios), "the radios are not controls of the widget"
    assert radio.ok_button in radio.controls
    assert all(c in check.controls for c in check.checks), "the checkboxes are not controls of the widget"
    assert all(not control.isEnabled() for control in radio.controls + check.controls)

    # The mockup gives a button its content width, never the whole row.
    for button in (radio.ok_button, check.ok_button, screen.run_view.input_widget(0, 3).choice_buttons[0]):
        assert button.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Fixed


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


def test_the_answered_widget_survives_the_end_of_the_run(qapp, tmp_path):
    """Packet 10a review: the widgets showing the answers must not be deleted when the run ends.

    Rebuilding the step list used to destroy the item that carried each input widget, taking the
    widget with it; after the pump the view would then be painting a dead widget."""
    env = _write_env(tmp_path, {"blocking.tslua": BLOCKING_INTEGER})
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    _select(screen, "Voltage check schedule")
    screen.steps_options_stage.run_button.click()
    screen.show()
    qapp.processEvents()

    widget = screen.run_view.input_widget(0, 1)
    assert _pump_until(qapp, lambda: widget.ok_button.isEnabled()), "the input never opened"
    widget.line_edit.setText("4")
    QTest.mouseClick(widget.ok_button, Qt.MouseButton.LeftButton)
    assert _wait_finished(qapp, session)

    # The finished run re-renders every row: the same widget, still holding the Operator's answer.
    QTest.qWait(50)
    qapp.processEvents()
    QTest.qWait(50)
    qapp.processEvents()
    assert not sip.isdeleted(widget), "the input widget was deleted when the run finished"
    assert screen.run_view.input_widget(0, 1) is widget, "the finished run rebuilt a different widget"
    assert widget.line_edit.text() == "4", widget.line_edit.text()
    assert not sip.isdeleted(widget.line_edit)


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


# -- a blocking input opens only when its own call waits ---------------------


def test_the_second_blocking_input_stays_inert_until_its_own_call_waits(qapp, tmp_path):
    """Packet 10a review: every declared input is not live at the step's start — only the one the
    action is blocked on is. Widgets are addressed individually, never by scanning `controls`."""
    env = _write_env(tmp_path, {"two.tslua": TWO_BLOCKING})
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    _select(screen, "Two reads schedule")
    screen.steps_options_stage.run_button.click()
    screen.show()
    qapp.processEvents()

    first = screen.run_view.input_widget(0, 1)
    second = screen.run_view.input_widget(0, 2)

    assert _pump_until(qapp, lambda: first.ok_button.isEnabled()), "the first input never opened"
    assert not second.ok_button.isEnabled(), "the second input opened before ATE.input(2) was called"
    assert not second.line_edit.isEnabled()

    first.line_edit.setText("4")
    QTest.mouseClick(first.ok_button, Qt.MouseButton.LeftButton)
    assert _pump_until(qapp, lambda: second.ok_button.isEnabled()), "the second input never opened"
    assert not first.ok_button.isEnabled(), "the answered input stayed open"

    second.line_edit.setText("6")
    QTest.mouseClick(second.ok_button, Qt.MouseButton.LeftButton)
    assert _wait_finished(qapp, session)
    assert _recorded_inputs(session.store, session.run_uuid) == [(1, "INTEGER", "4"), (2, "INTEGER", "6")]


# -- POLL: clickable while its step runs, closed when it ends ----------------


def test_a_poll_button_stays_live_for_the_whole_of_its_step(qapp, tmp_path):
    """A POLL opens at its step's start, takes several clicks, and closes when the step ends."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    env = _write_env(tmp_path, {"poll.tslua": POLL_WHILE_WAITING})
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    _select(screen, "Poll while waiting")
    screen.steps_options_stage.run_button.click()
    screen.show()
    qapp.processEvents()

    poll = screen.run_view.input_widget(0, 1)
    gain = screen.run_view.input_widget(0, 2)
    later_poll = screen.run_view.input_widget(1, 1)

    assert poll.value_label.text() == "clicked: (none)", poll.value_label.text()
    assert all(not b.isEnabled() for b in poll.choice_buttons), "the POLL opened before its step ran"
    assert all(not c.isEnabled() for c in later_poll.controls), "step 2's POLL opened before its step ran"

    # The step waits on its INTEGER, and the POLL is clickable all the while.
    assert _pump_until(qapp, lambda: gain.ok_button.isEnabled()), "the step never reached ATE.input(2)"
    assert _pump_until(qapp, lambda: all(b.isEnabled() for b in poll.choice_buttons)), "the POLL never opened"
    assert ACTIVE_BG in _pixels(poll), "the live POLL is not styled active"

    pressed = next(b for b in poll.choice_buttons if b.text() == "Pressed")
    QTest.mouseClick(pressed, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert poll.value_label.text() == "clicked: Pressed", poll.value_label.text()
    assert all(b.isEnabled() for b in poll.choice_buttons), "the POLL closed on its first click"

    not_working = next(b for b in poll.choice_buttons if b.text() == "Not working")
    QTest.mouseClick(not_working, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert poll.value_label.text() == "clicked: Not working", poll.value_label.text()

    screen.run_view.grab().save(str(SHOTS / "poll-live.png"))

    gain.line_edit.setText("2")
    QTest.mouseClick(gain.ok_button, Qt.MouseButton.LeftButton)
    assert _wait_finished(qapp, session)

    # Both steps have run: nothing may click a POLL any more.
    assert all(not b.isEnabled() for b in poll.choice_buttons), "the POLL stayed clickable after its step"
    assert all(not b.isEnabled() for b in later_poll.choice_buttons), "an unclosed POLL is still live"
    assert not sip.isdeleted(poll) and not sip.isdeleted(later_poll)

    recorded = _recorded_inputs(session.store, session.run_uuid)
    assert (1, "POLL", "Not working") in recorded, recorded
    assert (2, "INTEGER", "2") in recorded, recorded
    assert "Done" not in [value for _index, _type, value in recorded], recorded


def test_a_run_finishes_even_when_a_poll_is_never_clicked(qapp, tmp_path):
    """Packet 10a review: a POLL nobody clicks must not hold the run — its step still records."""
    env = _write_env(tmp_path, {"poll.tslua": POLL_WHILE_WAITING})
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    _select(screen, "Poll while waiting")
    screen.steps_options_stage.run_button.click()
    screen.show()
    qapp.processEvents()

    poll = screen.run_view.input_widget(0, 1)
    gain = screen.run_view.input_widget(0, 2)
    assert _pump_until(qapp, lambda: gain.ok_button.isEnabled()), "the step never asked for its value"
    gain.line_edit.setText("3")
    QTest.mouseClick(gain.ok_button, Qt.MouseButton.LeftButton)
    assert _wait_finished(qapp, session), "the run hung on the un-clicked POLL of step 2"

    rows = _recorded_inputs(session.store, session.run_uuid)
    assert (2, "INTEGER", "3") in rows, rows
    assert all(not control.isEnabled() for widget in screen.run_view.input_widgets[1] for control in widget.controls)
    assert poll.value_label.text() == "clicked: (none)"


def test_the_asked_row_is_scrolled_into_view(qapp, tmp_path):
    """The mockup scrolls to the input it is waiting on: with 31 steps the asked row is below the fold."""
    env = _write_env(tmp_path, {"long.tslua": LONG_LAST_INPUT})
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    screen.resize(1280, 800)
    _select(screen, "Long schedule")
    screen._reveal_run_view()
    screen.show()
    qapp.processEvents()
    screen.steps_options_stage.run_button.click()
    qapp.processEvents()

    widget = screen.run_view.input_widget(30, 1)
    assert widget is not None, "the last step's input has no widget"
    assert _pump_until(qapp, lambda: widget.ok_button.isEnabled()), "the input never opened"
    viewport = screen.run_view.step_list.viewport()
    y = widget.pos().y()
    assert 0 <= y < viewport.height(), f"the asked row is at y={y}, outside the {viewport.height()} px view"

    widget.line_edit.setText("1")
    QTest.mouseClick(widget.ok_button, Qt.MouseButton.LeftButton)
    assert _wait_finished(qapp, session)


# -- the session refuses answers that belong to no waiting input -------------


def test_the_session_refuses_an_answer_for_a_step_that_is_not_asking(qapp, tmp_path):
    """Wrong step, wrong Attempt, wrong index, or an input that isn't waiting: all raise."""
    env = _write_env(tmp_path, {"two.tslua": TWO_BLOCKING})
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    _select(screen, "Two reads schedule")
    screen.steps_options_stage.run_button.click()
    screen.show()
    qapp.processEvents()

    first = screen.run_view.input_widget(0, 1)
    assert _pump_until(qapp, lambda: first.ok_button.isEnabled()), "the first input never opened"

    for args in ((1, 1, 1, 4),      # a step that is not running at all
                (0, 2, 1, 4),      # an Attempt that has not happened
                (0, 1, 3, 4),      # an index this step never declared
                (0, 1, 2, 4)):     # declared, but ATE.input(2) is not waiting yet
        try:
            session.supply_input(*args)
        except ValueError:
            continue
        raise AssertionError(f"supply_input{args} was accepted, though nothing asked for it")

    # A value the declaration rejects never reaches the worker either.
    try:
        session.supply_input(0, 1, 1, 99)
    except ValueError as exc:
        assert "Must be 1 to 9" in str(exc), str(exc)
    else:
        raise AssertionError("an out-of-range INTEGER was supplied")

    first.line_edit.setText("4")
    QTest.mouseClick(first.ok_button, Qt.MouseButton.LeftButton)
    second = screen.run_view.input_widget(0, 2)
    assert _pump_until(qapp, lambda: second.ok_button.isEnabled()), "the step never recovered"
    second.line_edit.setText("6")
    QTest.mouseClick(second.ok_button, Qt.MouseButton.LeftButton)
    assert _wait_finished(qapp, session)
    assert _recorded_inputs(session.store, session.run_uuid) == [(1, "INTEGER", "4"), (2, "INTEGER", "6")]


def test_the_session_refuses_a_poll_click_that_is_not_open(qapp, tmp_path):
    """A POLL click for a step that is not running, or a text its prompt never offered, raises."""
    env = _write_env(tmp_path, {"poll.tslua": POLL_WHILE_WAITING})
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    _select(screen, "Poll while waiting")
    screen.steps_options_stage.run_button.click()
    screen.show()
    qapp.processEvents()

    poll = screen.run_view.input_widget(0, 1)
    gain = screen.run_view.input_widget(0, 2)
    assert _pump_until(qapp, lambda: all(b.isEnabled() for b in poll.choice_buttons)), "the POLL never opened"

    for args in ((1, 1, 1, "Done"),    # step 2 has not started yet
                (0, 1, 1, "Maybe"),    # not one of the POLL's prompt texts
                (0, 1, 2, "Pressed"),  # that input is not a POLL
                (0, 2, 1, "Pressed")):  # an Attempt that is not running
        try:
            session.click_poll(*args)
        except ValueError:
            continue
        raise AssertionError(f"click_poll{args} was accepted, though nothing was open")

    pressed = next(b for b in poll.choice_buttons if b.text() == "Pressed")
    QTest.mouseClick(pressed, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    gain.line_edit.setText("2")
    QTest.mouseClick(gain.ok_button, Qt.MouseButton.LeftButton)
    assert _wait_finished(qapp, session)
    rows = sorted((i, v) for i, _t, v in _recorded_inputs(session.store, session.run_uuid))
    assert rows == [(1, "Pressed"), (2, "2")], rows


def test_a_widget_click_for_a_closed_input_is_shown_not_crashed(qapp, tmp_path):
    """If a widget is somehow clicked while its call is not waiting, the session's refusal is
    displayed as the mockup's inline error rather than raising out of the click."""
    env = _write_env(tmp_path, {"poll.tslua": POLL_WHILE_WAITING})
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    _select(screen, "Poll while waiting")
    screen.steps_options_stage.run_button.click()
    screen.show()
    qapp.processEvents()

    poll = screen.run_view.input_widget(0, 1)
    assert _pump_until(qapp, lambda: any(b.isEnabled() for b in poll.choice_buttons)), "the POLL never opened"
    # Force a click for a step/Attempt that is no longer open: the widget's own guard catches it.
    poll.attempt_no = 99
    button = poll.choice_buttons[0]
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert "no longer running" in poll.error_label.text() or "no POLL" in poll.error_label.text(), poll.error_label.text()
    assert poll.value_label.text() == "clicked: (none)"


# -- a Stop-on-Fail pause with input rows on screen ---------------------------


def _run_pause_child(choice: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run the pause scenario in a fresh process: a segfault is exit 139, not a test failure."""
    root = Path(__file__).resolve().parent.parent
    script = tmp_path / "pause_child.py"
    script.write_text(PAUSE_CHILD, encoding="utf-8")
    schedule = tmp_path / "pause.tslua"
    schedule.write_text(PAUSE_WITH_INPUTS, encoding="utf-8")
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONPATH"] = f"{root / 'src'}{os.pathsep}{root}"
    return subprocess.run(
        [sys.executable, str(script), choice, str(schedule)],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )


def test_pause_with_input_rows_exits_cleanly_on_continue(qapp, tmp_path):
    """Packet 10a review: answering a Stop-on-Fail prompt while input rows are on screen used to
    SIGSEGV the process (exit 139). The pause must keep those widgets alive."""
    result = _run_pause_child("CONTINUE", tmp_path)
    assert result.returncode == 0, f"exit {result.returncode}\n{result.stderr[-1500:]}"
    assert "FINISHED" in result.stdout, result.stdout


def test_pause_with_input_rows_exits_cleanly_on_retry(qapp, tmp_path):
    """RETRY reruns the failing step, re-asking its input: the retried Attempt's widgets stay alive."""
    result = _run_pause_child("RETRY", tmp_path)
    assert result.returncode == 0, f"exit {result.returncode}\n{result.stderr[-1500:]}"
    assert "FINISHED" in result.stdout, result.stdout


def test_pause_with_input_rows_exits_cleanly_on_abort(qapp, tmp_path):
    """ABORT ends the run from the pause with the input rows still on screen."""
    result = _run_pause_child("ABORT", tmp_path)
    assert result.returncode == 0, f"exit {result.returncode}\n{result.stderr[-1500:]}"
    assert "FINISHED" in result.stdout, result.stdout
