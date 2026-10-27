"""Packet 05b — broken Test Definitions and Test Schedules on the Run screen.

The Run screen's schedule list shows every broken Test Definition and Test Schedule as a row with
its error, and none of them can be started. Everything here drives the real :class:`StagedRunScreen`
offscreen against real ``.atedef`` trees on disk: a dependency cycle (two broken Media Releases), one
definition listing a file that is not on disk, and a usable Media Release holding a schedule that
raises while loading next to one that runs.

The styling is checked against the mockup's values (`prototype/run-screen`, lines 97 and 167-169):
`.b-sched .it.broken` background `#fff5f5`, `.err` text `#b71c1c`. Qt cannot paint two colours on one
plain list item, so a broken row is a QWidget with three labels — name, Media Release, error — which is
exactly how the mockup draws it, and the test reads the pixel it paints.
"""

import hashlib
import json
import time
from pathlib import Path

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QColor, QMouseEvent
from PyQt6.QtWidgets import QLabel

from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.ui.staged_run_screen import StagedRunScreen

#: The mockup's broken-row colours, spelled out here so the assertions name the design directly.
BROKEN_BG = (255, 245, 245)  # #fff5f5
BROKEN_ERR = (183, 28, 28)  # #b71c1c

GOOD_SRC = """\
return ScheduleParams("Good schedule", "a schedule that loads and runs")
  :setSchedule({
    StepParams("ok"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "ok") end),
  })
"""

# An unclosed table: the file raises while the schedule loads, so only this row is broken.
RAISING_SRC = "return {\n"

CYCLE_A = {
    "format": 1, "mediaNumber": "MR-10600", "name": "Cycle A", "version": "1.0.0",
    "depends": [{"name": "Cycle B", "version": "1.0.0"}],
    "files": [{"path": "a.tslua", "sha256": hashlib.sha256(GOOD_SRC.encode()).hexdigest()}],
}
CYCLE_B = {
    "format": 1, "mediaNumber": "MR-10601", "name": "Cycle B", "version": "1.0.0",
    "depends": [{"name": "Cycle A", "version": "1.0.0"}],
    "files": [{"path": "b.tslua", "sha256": hashlib.sha256(GOOD_SRC.encode()).hexdigest()}],
}
LEGACY = {
    "format": 1, "mediaNumber": "MR-10611", "name": "Legacy Fixtures", "version": "0.9.0",
    # fixture_io.lua is listed but never written: the mockup's own example of a broken definition.
    "files": [{"path": "fixture_io.lua", "sha256": "0" * 64}],
}
DEMO = {
    "format": 1, "mediaNumber": "MR-10422", "name": "PSU-200 Tests", "version": "2.3.0",
    "files": [
        {"path": "good.tslua", "sha256": hashlib.sha256(GOOD_SRC.encode()).hexdigest()},
        {"path": "broken.tslua", "sha256": hashlib.sha256(RAISING_SRC.encode()).hexdigest()},
    ],
}


def _write_env(tmp_path: Path) -> dict[str, Path]:
    """Write four Test Definition directories (two cyclic, one missing a file, one mixed) and a config."""
    defs = tmp_path / "defs"
    defs.mkdir(parents=True, exist_ok=True)
    for label, atedef, files in (
        ("cycle_a", CYCLE_A, {"a.tslua": GOOD_SRC}),
        ("cycle_b", CYCLE_B, {"b.tslua": GOOD_SRC}),
        ("legacy", LEGACY, {}),  # deliberately no fixture_io.lua on disk
        ("demo", DEMO, {"good.tslua": GOOD_SRC, "broken.tslua": RAISING_SRC}),
    ):
        release = defs / label
        release.mkdir(parents=True, exist_ok=True)
        (release / f"{label}.atedef").write_text(json.dumps(atedef), encoding="utf-8")
        for name, text in files.items():
            (release / name).write_text(text, encoding="utf-8")

    config = {
        "format": 1,
        "station": {"name": "BENCH-03", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c10"},
        "database": {"type": "sqlite", "path": str(tmp_path / "ate.db")},
        "reports": {"directory": str(tmp_path / "reports")},
        "testDefinitions": {"directories": [str(defs)]},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return {"config": config_path, "defs": defs, "db": tmp_path / "ate.db"}


def _screen(tmp_path: Path) -> StagedRunScreen:
    """The real Run screen, built from the fixture config."""
    return StagedRunScreen(HeadlessRunSession(_write_env(tmp_path)["config"]))


def _list(screen: StagedRunScreen):
    return screen.test_schedule_stage.schedule_list


def _texts(screen: StagedRunScreen) -> list[str]:
    list_widget = _list(screen)
    return [list_widget.item(i).text() for i in range(list_widget.count())]


def _items(screen: StagedRunScreen) -> list:
    list_widget = _list(screen)
    return [list_widget.item(i) for i in range(list_widget.count())]


def _broken_items(screen: StagedRunScreen) -> list:
    """Every picker row that carries an error (the mockup's `.it.broken` rows)."""
    return [item for item in _items(screen) if item.data(Qt.ItemDataRole.UserRole + 1)]


def _usable_items(screen: StagedRunScreen) -> list:
    """Rows a run can start from: every row without an error."""
    return [item for item in _items(screen) if item.data(Qt.ItemDataRole.UserRole + 1) is None]


def _click(list_widget, item) -> None:
    """Press and release the left mouse button at a row, as an Operator's click does."""
    pos = list_widget.visualItemRect(item).center()
    press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(pos), QPointF(pos),
                       Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    lift = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(pos), QPointF(pos),
                       Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    list_widget.mousePressEvent(press)
    list_widget.mouseReleaseEvent(lift)


def _pixels(widget) -> set[tuple[int, int, int]]:
    """Every distinct colour a widget paints, straight from its grab."""
    image = widget.grab().toImage()
    return {(image.pixelColor(x, y).red(), image.pixelColor(x, y).green(), image.pixelColor(x, y).blue())
            for y in range(image.height()) for x in range(image.width())}


# -- the rows themselves -------------------------------------------------------


def test_broken_definitions_and_schedules_are_listed_with_their_errors(qapp, tmp_path):
    """Four broken rows appear: two cycle releases, one missing file, one schedule that raised."""
    screen = _screen(tmp_path)
    texts = _texts(screen)

    # The picker shows the whole catalog, not just what can run: one usable row plus four broken ones.
    assert len(texts) == 5, texts
    assert [t for t in texts if "Good schedule" in t] == ["Good schedule — a schedule that loads and runs"]

    broken = [t for t in texts if "(Test Definition broken)" in t]
    assert len(broken) == 3, broken
    for release, error in (("MR-10600 · Cycle A 1.0.0", "cycle"),
                           ("MR-10601 · Cycle B 1.0.0", "cycle"),
                           ("MR-10611 · Legacy Fixtures 0.9.0", "listed file is missing: fixture_io.lua")):
        row = next(t for t in broken if release in t)
        assert error in row, row
        assert row.splitlines()[0] == "⛔ (Test Definition broken)", row

    # The schedule that raised while loading is its own row, named by the file that failed.
    raising = [t for t in texts if "broken.tslua" in t]
    assert len(raising) == 1, raising
    assert "MR-10422 · PSU-200 Tests 2.3.0" in raising[0]
    assert raising[0].splitlines()[0] == "⛔ broken.tslua", raising[0]


def test_broken_rows_sit_after_the_usable_ones_in_scan_order(qapp, tmp_path):
    """Usable rows first, exactly as before; broken rows after them in the order the scan found them."""
    screen = _screen(tmp_path)
    list_widget = _list(screen)
    order = [(list_widget.row(item), item.text()) for item in _usable_items(screen)]
    broken_order = [list_widget.row(item) for item in _broken_items(screen)]

    assert [text for _, text in order] == ["Good schedule — a schedule that loads and runs"]
    assert broken_order == sorted(broken_order)
    assert broken_order[0] > order[0][0], broken_order


def test_a_broken_row_cannot_be_selected_or_started(qapp, tmp_path):
    """Clicking, setCurrentItem and Start all fail to start a run from a broken row."""
    env = _write_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    list_widget = _list(screen)
    broken = _broken_items(screen)
    assert broken, "the fixture has broken rows to test"

    # Nothing is selected at all yet, so Start is disabled.
    assert not screen.test_schedule_stage.run_button.isEnabled()

    for item in broken:
        _click(list_widget, item)
        assert not item.isSelected(), item.text()
        assert list_widget.currentItem() is not item, item.text()
        assert session.selected_schedule is None or session.selected_schedule == ""

    # Even forcing the current item cannot make a broken row startable.
    item = broken[0]
    list_widget.setCurrentItem(item)
    assert not item.isSelected()
    assert not screen.test_schedule_stage.run_button.isEnabled()
    screen._begin_run_from_selection()
    assert screen.steps_stack.currentIndex() == 0, "a broken row must not advance to the steps stage"
    assert session.selected_schedule in (None, "")

    # A good row selected, then a broken row made current: Start is off and the preview refuses.
    good = next(i for i in _usable_items(screen) if "Good schedule" in i.text())
    list_widget.setCurrentItem(good)
    assert screen.test_schedule_stage.run_button.isEnabled()
    screen._begin_run_from_selection()
    assert screen.steps_stack.currentIndex() == 2, "the good row still previews its steps"
    list_widget.setCurrentItem(broken[1])
    assert not screen.test_schedule_stage.run_button.isEnabled()
    screen._begin_run_from_selection()
    assert screen.steps_stack.currentIndex() == 2, "a broken row must not preview steps of its own"


def test_broken_rows_are_styled_like_the_mockup(qapp, tmp_path):
    """`.it.broken` paints #fff5f5 and `.err` paints #b71c1c, read straight off the pixels."""
    screen = _screen(tmp_path)
    screen.show()
    qapp.processEvents()
    list_widget = _list(screen)

    for item in _broken_items(screen):
        assert item.background().color() == QColor("#fff5f5"), item.text()
        widget = list_widget.itemWidget(item)
        assert widget is not None, item.text()
        assert BROKEN_BG in _pixels(widget), item.text()

        error_label = widget.findChild(QLabel, "schedule_error")
        assert error_label is not None and BROKEN_ERR in _pixels(error_label), item.text()
        assert "#b71c1c" in error_label.styleSheet()

    # A usable row keeps its plain look: no error widget, no broken background.
    for item in _usable_items(screen):
        assert list_widget.itemWidget(item) is None
        assert not item.text().startswith("⛔")


def test_the_filter_matches_broken_rows_by_name_release_and_error(qapp, tmp_path):
    """The filter box narrows the list to rows whose name, release or error contains the text."""
    screen = _screen(tmp_path)

    screen.test_schedule_stage.filter_edit.setText("fixture_io")
    texts = _texts(screen)
    assert len(texts) == 1 and "Legacy Fixtures" in texts[0], texts

    screen.test_schedule_stage.filter_edit.setText("MR-10601")
    texts = _texts(screen)
    assert len(texts) == 1 and "Cycle B" in texts[0], texts

    screen.test_schedule_stage.filter_edit.setText("Cycle")
    texts = _texts(screen)
    assert len(texts) == 2 and all("(Test Definition broken)" in t for t in texts), texts

    screen.test_schedule_stage.filter_edit.setText("nothing matches this")
    assert _texts(screen) == []

    # The schedule the Operator already picked stays picked while the filter still shows it.
    screen.test_schedule_stage.filter_edit.setText("")
    list_widget = _list(screen)
    good = next(i for i in _usable_items(screen) if "Good schedule" in i.text())
    list_widget.setCurrentItem(good)
    screen.test_schedule_stage.filter_edit.setText("PSU-200")
    assert list_widget.currentItem() is not None and "Good schedule" in list_widget.currentItem().text()
    assert screen.test_schedule_stage.run_button.isEnabled()

    screen.test_schedule_stage.filter_edit.setText("")
    assert len(_texts(screen)) == 5


def test_rescan_picks_up_a_file_broken_after_the_first_scan(qapp, tmp_path):
    """Rescan reloads the catalog, so a Test Definition added after launch shows up broken."""
    env = _write_env(tmp_path)
    screen = StagedRunScreen(HeadlessRunSession(env["config"]))
    assert len(_texts(screen)) == 5

    late = {
        "format": 1, "mediaNumber": "MR-10700", "name": "Late fixtures", "version": "0.1.0",
        "files": [{"path": "ghost.tslua", "sha256": "0" * 64}],
    }
    (env["defs"] / "late.atedef").write_text(json.dumps(late), encoding="utf-8")

    screen.test_schedule_stage.rescan_button.click()

    texts = _texts(screen)
    assert len(texts) == 6, texts
    assert any("(Test Definition broken)" in t and "MR-10700 · Late fixtures 0.1.0" in t
               and "ghost.tslua" in t for t in texts), texts


def test_the_good_schedule_still_starts_a_run(qapp, tmp_path):
    """Broken rows in the list do not stop a usable schedule from running to FINISHED."""
    env = _write_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    list_widget = _list(screen)

    good = next(i for i in _usable_items(screen) if "Good schedule" in i.text())
    list_widget.setCurrentItem(good)
    assert screen.test_schedule_stage.run_button.isEnabled()
    screen._begin_run_from_selection()
    screen.steps_options_stage.run_button.click()

    written = []
    session.report_written.connect(lambda html, pdf: written.append((html, pdf)))
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if session._state == SessionState.FINISHED and written:
            break
        time.sleep(0.005)
    else:
        raise TimeoutError("the good schedule never finished")

    assert session.selected_schedule == "Good schedule"
    assert session.store.get_test_run(session.run_uuid)["result"] == "PASS"


# -- the screenshot -----------------------------------------------------------


def test_picker_screenshot_is_saved_for_the_mockup_comparison(qapp, tmp_path):
    """A grab of the picker with its broken rows, saved for eyeballing against the mockup."""
    screen = _screen(tmp_path)
    screen.resize(460, 420)
    screen.show()
    qapp.processEvents()

    shots = Path("/tmp/05b-shots")
    shots.mkdir(parents=True, exist_ok=True)
    image = screen.test_schedule_stage.schedule_list.grab()
    assert image.save(str(shots / "picker.png")), "the picker grab did not save"

    # The picture itself proves the mockup's two colours are present in the list.
    colours = _pixels(screen.test_schedule_stage.schedule_list)
    assert BROKEN_BG in colours and BROKEN_ERR in colours, sorted(colours)[:10]
