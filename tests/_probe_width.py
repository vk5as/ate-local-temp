"""Temporary probe: does a wide RunView alone break the RADIOBUT QTest click?"""
import time

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

from ate.run_session.headless import HeadlessRunSession
from ate.ui.staged_run_screen import StagedRunScreen

from ._fixtures import write_ate_env

SRC = """\
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


def test_probe(qapp, tmp_path):
    env = write_ate_env(tmp_path)
    defs = env["defs_dir"]
    import hashlib, json
    (defs / "radio.tslua").write_text(SRC, encoding="utf-8")
    atedef = json.loads((defs / "demo.atedef").read_text(encoding="utf-8"))
    atedef["files"].append({"path": "radio.tslua", "sha256": hashlib.sha256(SRC.encode()).hexdigest()})
    (defs / "demo.atedef").write_text(json.dumps(atedef), encoding="utf-8")

    for width in (290, 628, 1280):
        session = HeadlessRunSession(env["config"])
        screen = StagedRunScreen(session)
        screen.resize(width, 533)
        lst = screen.test_schedule_stage.schedule_list
        item = next(lst.item(i) for i in range(lst.count()) if "Radio" in lst.item(i).text())
        lst.setCurrentItem(item)
        screen._begin_run_from_selection()
        screen.steps_options_stage.run_button.click()
        screen.show()
        qapp.processEvents()
        w = screen.run_view.input_widget(0, 1)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not w.ok_button.isEnabled():
            qapp.processEvents()
        radio = w.radios[1]
        print(f"width={width} screen={screen.geometry()} radio={radio.geometry()} visible={radio.isVisible()} "
              f"item={w.input_item.sizeHint()} widget={w.geometry()} vp={screen.run_view.step_list.viewport().geometry()}")
        QTest.mouseClick(radio, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        print("  clicked:", [r.isChecked() for r in w.radios])
        screen.run_view.step_list.setCurrentItem(w.input_item)
        QTest.mouseClick(radio, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        print("  clicked after select:", [r.isChecked() for r in w.radios])
