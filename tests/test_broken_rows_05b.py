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
import math
import time
from pathlib import Path

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QColor, QFontMetrics, QMouseEvent
from PyQt6.QtWidgets import QLabel, QWidget

from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.ui.run_widgets import BROKEN_MARKER
from ate.ui.staged_run_screen import StagedRunScreen, _broken_row_size_hint, _ROW_MARGIN_X, _wrapped_height

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

# A schedule that raises with a message far wider than the picker, so the `.err` line must wrap.
LONG_RAISING_SRC = 'error("unexpected symbol near <eof> ' + "while loading this schedule " * 20 + '")'

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
        (release / f"{label}.atedef").write_text(json.dumps(atedef), encoding="utf-8", newline="\n")
        for name, text in files.items():
            (release / name).write_text(text, encoding="utf-8", newline="\n")

    config = {
        "format": 1,
        "station": {"name": "BENCH-03", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c10"},
        "defaultProject": "Power board",
        "database": {"type": "sqlite", "path": str(tmp_path / "ate.db")},
        "reports": {"directory": str(tmp_path / "reports")},
        "testDefinitions": {"directories": [str(defs)]},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8", newline="\n")
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


#: A glyph is never painted in one flat colour. The strokes are anti-aliased against the row's
#: background, and the Linux runner's subpixel rendering fringes them blue: on that runner the
#: error text's ink was (184, 31, 60) and never literally #b71c1c, so an exact-pixel assertion
#: there tests the runner's font rather than ATE's stylesheet. Any ink within this distance of the
#: mockup's red is that red as this machine draws it; a colour that is not red is far outside it.
_ERR_INK_TOLERANCE = 45


def _nearest_ink(colours: set[tuple[int, int, int]], target: tuple[int, int, int]):
    """The painted colour closest to `target`, and its distance from it."""
    nearest = min(colours, key=lambda c: sum((c[i] - target[i]) ** 2 for i in range(3)))
    return nearest, math.sqrt(sum((nearest[i] - target[i]) ** 2 for i in range(3)))


#: A Unicode non-character, written with chr() because Python's \U escape takes only four hex
#: digits. No font anywhere contains it, so Qt draws the box it draws for any glyph it cannot find.
#: A marker whose ink is this box is a marker the Operator never sees.
_NO_GLYPH = chr(0xF0000)


def _glyph_ink(qapp, text: str) -> frozenset[tuple[int, int]]:
    """The pixels a QLabel draws for `text`, in the font the runner actually has."""
    label = QLabel(text)
    label.show()
    qapp.processEvents()
    image = label.grab().toImage()
    return frozenset(
        (x, y)
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y).lightness() < 200
    )


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
        assert row.splitlines()[0] == f"{BROKEN_MARKER} (Test Definition broken)", row

    # The schedule that raised while loading is its own row, named by the file that failed.
    raising = [t for t in texts if "broken.tslua" in t]
    assert len(raising) == 1, raising
    assert "MR-10422 · PSU-200 Tests 2.3.0" in raising[0]
    assert raising[0].splitlines()[0] == f"{BROKEN_MARKER} broken.tslua", raising[0]


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
        assert error_label is not None, item.text()
        nearest, distance = _nearest_ink(_pixels(error_label), BROKEN_ERR)
        assert distance <= _ERR_INK_TOLERANCE, (
            f"{item.text()} paints no ink near the mockup's #b71c1c: the closest pixel is "
            f"{nearest}, {distance:.0f} away"
        )
        assert nearest[0] > nearest[1] and nearest[0] > nearest[2], (
            f"{item.text()} paints {nearest}, which is not red"
        )
        assert "#b71c1c" in error_label.styleSheet()

    # A usable row keeps its plain look: no error widget, no broken background.
    for item in _usable_items(screen):
        assert list_widget.itemWidget(item) is None
        assert not item.text().startswith(BROKEN_MARKER)


def test_the_broken_marker_is_a_glyph_the_runner_font_has(qapp):
    """The warning on a broken row has to be ink, not Qt's "no such glyph" rectangle.

    The mockup draws `⛔`, and `⛔` (U+26D4) is not in DejaVu Sans — the only font the Linux CI
    runner installs — so on that runner every broken row in the picker was an empty box. Qt's
    missing-glyph box is drawn for a Unicode non-character too, so the marker's pixels are compared
    with it: if they are the same, nothing is being said."""
    missing = _glyph_ink(qapp, _NO_GLYPH)
    marker = _glyph_ink(qapp, BROKEN_MARKER)
    assert marker, f"{BROKEN_MARKER!r} drew nothing at all"
    assert marker != missing, (
        f"{BROKEN_MARKER!r} is not in this machine's default font: the row shows Qt's "
        f"missing-glyph box instead of a marker"
    )


def test_no_glyph_ate_paints_is_missing_from_the_font(qapp):
    """Every character the interface puts on screen must be drawable by the font it is drawn in.

    Found the hard way on a runner with only DejaVu Sans installed: a character the font lacks is
    silently drawn as an empty rectangle, and the row looks correct to whoever typed it. This reads
    the string literals of the interface code and renders each non-ASCII character in it."""
    import ast

    glyphs = set()
    for path in (Path(__file__).resolve().parents[1] / "src" / "ate").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                glyphs.update(ch for ch in node.value if ord(ch) > 127)

    missing = _glyph_ink(qapp, _NO_GLYPH)
    undrawable = {ch for ch in glyphs if _glyph_ink(qapp, ch) == missing}
    assert not undrawable, (
        f"ATE paints glyphs this machine has no font for: {sorted(undrawable)} "
        "(they render as Qt's empty box on a runner without an emoji font)"
    )


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

    # A schedule broken on load is named by its file, so the filter finds it by that file too.
    screen.test_schedule_stage.filter_edit.setText("broken.tslua")
    texts = _texts(screen)
    assert len(texts) == 1 and "MR-10422 · PSU-200 Tests 2.3.0" in texts[0], texts

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
    (env["defs"] / "late.atedef").write_text(json.dumps(late), encoding="utf-8", newline="\n")

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


# -- the review fixes ---------------------------------------------------------


def _write_long_error_env(tmp_path: Path) -> Path:
    """One Media Release whose only Test Schedule raises with an error wider than the picker."""
    defs = tmp_path / "defs"
    release = defs / "longerr"
    release.mkdir(parents=True, exist_ok=True)
    atedef = {
        "format": 1, "mediaNumber": "MR-10750", "name": "Long errors", "version": "1.0.0",
        "files": [{"path": "long.tslua", "sha256": hashlib.sha256(LONG_RAISING_SRC.encode()).hexdigest()}],
    }
    (release / "longerr.atedef").write_text(json.dumps(atedef), encoding="utf-8", newline="\n")
    (release / "long.tslua").write_text(LONG_RAISING_SRC, encoding="utf-8", newline="\n")
    config = {
        "format": 1,
        "station": {"name": "BENCH-03", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c10"},
        "defaultProject": "Power board",
        "database": {"type": "sqlite", "path": str(tmp_path / "ate.db")},
        "reports": {"directory": str(tmp_path / "reports")},
        "testDefinitions": {"directories": [str(defs)]},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8", newline="\n")
    return config_path


def _write_flagged_env(tmp_path: Path) -> Path:
    """One Media Release with a schedule that loads but whose bytes no longer match its checksum."""
    defs = tmp_path / "defs"
    release = defs / "flagged"
    release.mkdir(parents=True, exist_ok=True)
    atedef = {
        "format": 1, "mediaNumber": "MR-10760", "name": "Flagged Fixtures", "version": "1.0.0",
        # The recorded sha256 is of different bytes than the file on disk: a ⚠ flag, not a broken row.
        "files": [{"path": "good.tslua", "sha256": hashlib.sha256(b"edited after the release\n").hexdigest()}],
    }
    (release / "flagged.atedef").write_text(json.dumps(atedef), encoding="utf-8", newline="\n")
    (release / "good.tslua").write_text(GOOD_SRC, encoding="utf-8", newline="\n")
    config = {
        "format": 1,
        "station": {"name": "BENCH-03", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c10"},
        "defaultProject": "Power board",
        "database": {"type": "sqlite", "path": str(tmp_path / "ate.db")},
        "reports": {"directory": str(tmp_path / "reports")},
        "testDefinitions": {"directories": [str(defs)]},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8", newline="\n")
    return config_path


def test_rescans_do_not_leave_orphan_row_widgets(qapp, tmp_path):
    """The viewport's widget count stays constant across rescans: every row widget is freed first.

    `QListWidget.clear()` does not delete the widgets given to `setItemWidget`, so each rescan used to
    leave one orphan per broken row alive in the viewport (packet 05b review): 4 broken rows, 20
    rescans, 80 rows no Operator can see."""
    screen = _screen(tmp_path)
    screen.show()
    qapp.processEvents()
    list_widget = _list(screen)
    broken = len(_broken_items(screen))
    assert broken == 4, "the fixture has four broken rows to leak"

    def viewport_widgets() -> int:
        return len(list_widget.viewport().findChildren(QWidget))

    # A broken row is its QWidget plus its three labels; a usable row is plain item text.
    live = viewport_widgets()
    assert live == 4 * broken, live
    for _ in range(5):
        screen.test_schedule_stage.rescan_button.click()
        qapp.processEvents()
    assert viewport_widgets() == live, f"the viewport grew from {live} to {viewport_widgets()} widgets"
    for item in _broken_items(screen):
        assert list_widget.itemWidget(item) is not None, item.text()


def test_a_broken_current_row_never_begins_a_run(qapp, tmp_path):
    """The run path refuses when the picker's current row is not a runnable schedule.

    Nothing is picked and a broken row is made current, so there is no name to run; and the session
    can still carry an older pick while the picker stands on a broken row — the guard, not the empty
    name, is what stops the preview in that case."""
    env = _write_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    list_widget = _list(screen)
    broken = _broken_items(screen)

    list_widget.setCurrentItem(broken[0])
    screen._begin_run_from_selection()
    assert screen.steps_stack.currentIndex() == 0, "a broken row must not open the steps stage"
    assert screen.steps_options_stage.step_list.count() == 0
    assert session.selected_schedule in (None, "")
    assert screen.engine is None

    # A selection the picker no longer stands for: the guard is the only thing between it and a run.
    session.select_schedule("Good schedule")
    assert screen._selected_schedule_name() is None, "the current row is a broken one"
    screen._begin_run_from_selection()
    assert screen.steps_stack.currentIndex() == 0, "the stale pick must not preview its steps"
    assert screen.engine is None


def test_a_long_error_wraps_and_the_row_grows_to_fit_it(qapp, tmp_path):
    """At a 640 px list the `.err` line wraps and the row is tall enough for every wrapped line."""
    screen = StagedRunScreen(HeadlessRunSession(_write_long_error_env(tmp_path)))
    list_widget = _list(screen)
    list_widget.setFixedWidth(640)
    screen.test_schedule_stage.rescan_button.click()  # rebuild the rows at the new list width
    screen.show()
    qapp.processEvents()

    item = _broken_items(screen)[0]
    widget = list_widget.itemWidget(item)
    label = widget.findChild(QLabel, "schedule_error")
    viewport = list_widget.viewport().width()
    assert viewport <= 640, viewport
    assert label.wordWrap(), "the error label must wrap instead of running off the row"

    metrics = QFontMetrics(label.font())
    unwrapped = metrics.horizontalAdvance(label.text())
    assert unwrapped > viewport, f"the fixture error is only {unwrapped} px wide; nothing to wrap"

    wrapped = _wrapped_height(label.text(), label.font(), viewport - 2 * _ROW_MARGIN_X)
    lines = wrapped // metrics.lineSpacing()
    assert lines > 1, f"the wrapped error is {lines} line(s) tall"
    rect = list_widget.visualItemRect(item)
    # The row is exactly as tall as its three wrapped lines need: Qt's own guess for a word-wrapped
    # label sizes the row from an ideal width the list never has, so it is not what the row follows.
    assert rect.height() == _broken_row_size_hint(widget, viewport).height(), rect
    assert rect.height() >= wrapped, f"row is {rect.height()} px, the wrapped error needs {wrapped} px"
    assert label.width() <= viewport, f"the label is {label.width()} px wide in a {viewport} px list"
    assert list_widget.horizontalScrollBar().maximum() == 0, "a wrapped row must not scroll sideways"


def test_broken_rows_show_a_not_allowed_cursor(qapp, tmp_path):
    """The mockup's `.it.broken { cursor: not-allowed }`: the pointer says the row cannot be picked."""
    screen = _screen(tmp_path)
    list_widget = _list(screen)
    for item in _broken_items(screen):
        widget = list_widget.itemWidget(item)
        assert widget.cursor() == Qt.CursorShape.ForbiddenCursor, item.text()
        for label in widget.findChildren(QLabel):
            assert label.cursor() == Qt.CursorShape.ForbiddenCursor, item.text()
    for item in _usable_items(screen):
        assert list_widget.itemWidget(item) is None, item.text()


def test_a_load_error_shows_one_line_with_the_full_text_in_the_tooltip(qapp, tmp_path):
    """The row shows `broken.tslua:2: unexpected symbol near <eof>`, not the traceback or the scan path."""
    screen = _screen(tmp_path)
    list_widget = _list(screen)
    item = next(i for i in _broken_items(screen) if "broken.tslua" in i.text())
    shown = item.text().splitlines()[2]
    assert shown == "broken.tslua:2: unexpected symbol near <eof>", shown
    assert "/" not in shown and "failed to load" not in shown, shown
    assert "stack traceback" not in shown and "instance-env" not in shown, shown

    tooltip = item.toolTip()
    assert "failed to load" in tooltip and "stack traceback" in tooltip, tooltip
    assert list_widget.itemWidget(item).findChild(QLabel, "schedule_error").text() == shown

    # A definition-level error has no traceback to strip, so it is shown as it is.
    legacy = next(i for i in _broken_items(screen) if "Legacy Fixtures" in i.text())
    assert "listed file is missing: fixture_io.lua" in legacy.text().splitlines()[2], legacy.text()


def test_a_hidden_or_broken_current_row_clears_the_stale_notes(qapp, tmp_path):
    """Nothing may keep naming a schedule the picker no longer shows."""
    env = _write_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    list_widget = _list(screen)
    good = next(i for i in _usable_items(screen) if "Good schedule" in i.text())
    list_widget.setCurrentItem(good)
    assert session.selected_schedule == "Good schedule"
    assert "Good schedule" in screen.test_schedule_stage.notes_label.text()

    screen.test_schedule_stage.filter_edit.setText("Cycle")
    assert screen.test_schedule_stage.notes_label.text() == "", screen.test_schedule_stage.notes_label.text()
    assert screen.test_schedule_stage.mismatch_label.text() == ""
    assert session.selected_schedule in (None, ""), "the dropped pick must not still be selected"
    assert not screen.test_schedule_stage.run_button.isEnabled()
    assert not screen.steps_options_stage.run_button.isEnabled()

    screen.test_schedule_stage.filter_edit.setText("")
    list_widget.setCurrentItem(next(i for i in _usable_items(screen) if "Good schedule" in i.text()))
    assert session.selected_schedule == "Good schedule"
    list_widget.setCurrentItem(_broken_items(screen)[0])
    assert screen.test_schedule_stage.notes_label.text() == ""
    assert screen.test_schedule_stage.mismatch_label.text() == ""
    assert session.selected_schedule in (None, "")


def test_the_mismatch_flag_clears_when_the_picked_row_is_hidden(qapp, tmp_path):
    """The ⚠ line belongs to the schedule that is picked; hide it and the line goes with it."""
    session = HeadlessRunSession(_write_flagged_env(tmp_path))
    screen = StagedRunScreen(session)
    list_widget = _list(screen)
    list_widget.setCurrentItem(_usable_items(screen)[0])
    flagged = screen.test_schedule_stage.mismatch_label.text()
    assert "⚠" in flagged, flagged

    screen.test_schedule_stage.filter_edit.setText("nothing matches this")
    assert screen.test_schedule_stage.mismatch_label.text() == "", flagged
    assert screen.test_schedule_stage.notes_label.text() == ""
    assert session.selected_schedule in (None, "")


def test_stage_two_start_reports_a_schedule_that_broke_after_picking(qapp, tmp_path):
    """Start on the Steps stage is taken away when the picked schedule breaks, and never raises.

    The Operator previews the steps, then a Rescan finds the schedule's file no longer loads: the
    Steps stage's Start button is disabled like the picker's, and clicking it anyway shows the
    session's refusal instead of an uncaught SessionError."""
    env = _write_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    list_widget = _list(screen)
    good = next(i for i in _usable_items(screen) if "Good schedule" in i.text())
    list_widget.setCurrentItem(good)
    screen._begin_run_from_selection()
    assert screen.steps_stack.currentIndex() == 2
    assert screen.steps_options_stage.run_button.isEnabled()

    (env["defs"] / "demo" / "good.tslua").write_text(RAISING_SRC, encoding="utf-8", newline="\n")
    screen.test_schedule_stage.rescan_button.click()
    assert not screen.steps_options_stage.run_button.isEnabled(), "Start goes with the broken row"
    assert not screen.test_schedule_stage.run_button.isEnabled()

    screen.steps_options_stage.run_button.setEnabled(True)  # a click that would have raised
    screen.steps_options_stage.run_button.click()
    assert screen.engine is None, "no run started"
    error = screen.steps_options_stage.error_label.text()
    assert error.startswith(BROKEN_MARKER), error
    assert "schedule" in error.lower(), error


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


def test_picker_r2_screenshot_shows_a_wrapped_error_at_640_px(qapp, tmp_path):
    """The picker at a 640 px list width with a load error long enough to wrap, saved for review."""
    screen = StagedRunScreen(HeadlessRunSession(_write_long_error_env(tmp_path)))
    screen.resize(700, 460)
    list_widget = _list(screen)
    list_widget.setFixedWidth(640)
    screen.test_schedule_stage.rescan_button.click()
    screen.show()
    qapp.processEvents()

    shots = Path("/tmp/05b-shots")
    shots.mkdir(parents=True, exist_ok=True)
    # Every broken row is tall enough for its wrapped labels at the width it is actually shown at.
    for row in range(list_widget.count()):
        item = list_widget.item(row)
        widget = list_widget.itemWidget(item)
        if widget is not None:
            needed = widget.heightForWidth(list_widget.viewport().width())
            assert list_widget.visualItemRect(item).height() >= needed, "the wrapped error is clipped"
    image = list_widget.grab()
    assert image.width() == 640, image.width()
    assert image.save(str(shots / "picker-r2.png")), "the picker-r2 grab did not save"

    colours = _pixels(list_widget)
    assert BROKEN_BG in colours and BROKEN_ERR in colours, sorted(colours)[:10]
