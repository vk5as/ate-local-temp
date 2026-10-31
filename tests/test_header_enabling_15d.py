"""Packet 15d: enable state cascades through headers.

`setEnable` on an include step (a header) FORCES the default of every Test Step beneath it, the
Run screen's header box is derived from the steps under it (checked / unchecked / partial), and
clicking it sets them all — after which each step can still be toggled one by one. Real `.atedef`
trees and real `.tslua` files on disk, driven through the real `StagedRunScreen` stages offscreen,
plus the Run screen styling the mockup shows (`#eef1f5` header rows, bold names, `N/M done, K failed`).
"""

import hashlib
import json
import time
from pathlib import Path

from PyQt6.QtCore import Qt

from ate.catalog import find_definitions, include_map, module_map
from ate.engine.lua_host import LuaHost
from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.ui.staged_run_screen import StagedRunScreen

SHOTS = Path("/tmp/15d-shots")

#: A header whose children disagree: one step on by default, one off, one nested Sub-schedule.
MAIN_SRC = """\
return ScheduleParams("Header cascade schedule", "a header with mixed children")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("channel 1"):setStepNo("2."):setPrefix("2."):setInclude("channel.tslua", { channel = 1 }),
  })
"""

CHANNEL_SRC = """\
local kv = ...
local ch = kv.channel or 1
return ScheduleParams("Channel check", "One output channel")
  :setSchedule({
    StepParams("output on"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "channel " .. ch, tostring(ch), tostring(ch)) end),
    StepParams("output off"):setStepNo("2."):setEnable(false)
      :setAction(function() ATE.report(Result.PASS, "off " .. ch, tostring(ch), tostring(ch)) end),
    StepParams("nested"):setStepNo("3."):setPrefix("3."):setInclude("deep.tslua"),
  })
"""

DEEP_SRC = """\
return ScheduleParams("Deep schedule", "the grandchildren")
  :setSchedule({
    StepParams("deep one"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "deep one") end),
  })
"""

#: The same Sub-schedule with every step enabled and one that FAILs — the Run view's rollup needs
#: a completed step and a failed one under the same header.
FAILING_CHANNEL_SRC = """\
return ScheduleParams("Channel check", "One output channel, one failing step")
  :setSchedule({
    StepParams("output on"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "on") end),
    StepParams("output bad"):setStepNo("2.")
      :setAction(function() ATE.report(Result.FAIL, "bad", "0.1V", "2.4V") end),
    StepParams("nested"):setStepNo("3."):setPrefix("3."):setInclude("deep.tslua"),
  })
"""

#: The outer header forces its whole subtree off; the inner header forces its own back on.
FORCED_MAIN = """\
return ScheduleParams("Forcing main", "an outer header that forces its subtree off")
  :setSchedule({
    StepParams("outer"):setStepNo("1."):setPrefix("1."):setEnable(false):setInclude("mid.tslua"),
    StepParams("sibling"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "sibling") end),
  })
"""

MID_SRC = """\
return ScheduleParams("Mid schedule", "forced off, except its own include")
  :setSchedule({
    StepParams("kid"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "kid") end),
    StepParams("inner"):setStepNo("2."):setPrefix("2."):setEnable(true):setInclude("deep.tslua"),
  })
"""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_release(root: Path, label: str, files: dict[str, str]) -> Path:
    """Write `<label>.atedef` plus every file it lists, and return the release directory."""
    directory = root / label
    directory.mkdir(parents=True, exist_ok=True)
    directory.joinpath(f"{label}.atedef").write_text(
        json.dumps({
            "format": 1,
            "mediaNumber": f"MR-{label.upper()}",
            "name": f"Release {label}",
            "version": "1.0.0",
            "files": [{"path": rel, "sha256": _sha(src)} for rel, src in files.items()],
        }),
        encoding="utf-8",
        newline="\n",
    )
    for rel, src in files.items():
        directory.joinpath(rel).write_text(src, encoding="utf-8", newline="\n")
    return directory


def _config(tmp_path: Path, root: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "format": 1,
        "station": {"name": "BENCH-15D", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c12"},
        "defaultProject": "Power board",
        "database": {"type": "sqlite", "path": str(tmp_path / "ate.db")},
        "reports": {"directory": str(tmp_path / "reports")},
        "testDefinitions": {"directories": [str(root)]},
    }), encoding="utf-8", newline="\n")
    return path


def _host(root: Path, label: str, schedule: str) -> LuaHost:
    """A host for one schedule of a release, carrying that release's Sub-schedule map."""
    definition = next(d for d in find_definitions(root) if d.media_number == f"MR-{label.upper()}")
    return LuaHost(definition.path / schedule, modules=module_map(definition), includes=include_map(definition))


def _cascade_release(tmp_path: Path) -> Path:
    root = tmp_path / "defs"
    _write_release(root, "cascade", {"main.tslua": MAIN_SRC, "channel.tslua": CHANNEL_SRC, "deep.tslua": DEEP_SRC})
    return root


def _forced_release(tmp_path: Path) -> Path:
    root = tmp_path / "defs"
    _write_release(root, "forced", {"main.tslua": FORCED_MAIN, "mid.tslua": MID_SRC, "deep.tslua": DEEP_SRC})
    return root


def _failing_release(tmp_path: Path, stop_on_fail: bool = False) -> Path:
    """The cascade tree with every step enabled and one FAILing step under the header."""
    root = tmp_path / "defs"
    main = MAIN_SRC.replace(":setStopOnFail(false)", ":setStopOnFail(true)") if stop_on_fail else MAIN_SRC
    _write_release(root, "fail", {"main.tslua": main, "channel.tslua": FAILING_CHANNEL_SRC, "deep.tslua": DEEP_SRC})
    return root


def _open_screen(session: HeadlessRunSession, schedule: str) -> StagedRunScreen:
    """A StagedRunScreen sitting on the Steps & options stage for ``schedule``."""
    screen = StagedRunScreen(session)
    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    schedules = screen.test_schedule_stage.schedule_list
    item = next(schedules.item(i) for i in range(schedules.count())
                if schedule in schedules.item(i).text())
    schedules.setCurrentItem(item)
    screen._begin_run_from_selection()
    return screen


def _finish(qapp, session) -> str:
    """Pump until the run is FINISHED and its report written; return the HTML path."""
    written: list[str] = []
    session.report_written.connect(lambda html, pdf: written.append(html))
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if session._state == SessionState.FINISHED and written:
            return written[0]
        time.sleep(0.005)
    raise TimeoutError("the run never reached FINISHED with its report written")


def _nodes(session) -> dict[int, dict]:
    return {row["node_seq"]: row for row in session.store.nodes(session.run_uuid)}


# --- seam 1: forcing at load, and what the run records ----------------------


def test_a_header_set_enable_forces_the_default_of_every_step_beneath_it(tmp_path) -> None:
    """`setEnable(false)` on a header overrides its children's own defaults on the tree."""
    host = _host(_forced_release(tmp_path), "forced", "main.tslua")
    tree = host.tree
    # rows: outer header, kid, inner header, deep one, sibling
    assert [(t.kind, t.name) for t in tree] == [
        ("header", "outer"), ("step", "kid"), ("header", "inner"), ("step", "deep one"), ("step", "sibling"),
    ]
    assert [t.default_enabled for t in tree] == [False, False, True, True, True]
    # Forcing reaches beneath the header only: its own row keeps its own default, and the sibling
    # next to it is untouched.
    assert tree[0].step.enabled is False and tree[1].step.enabled is True
    assert tree[2].step.enabled is True  # the inner header re-forces its own subtree
    assert tree[3].depth == 2 and tree[3].parent == 2


def test_a_nested_header_re_forces_only_its_own_subtree(tmp_path) -> None:
    """The innermost `setEnable` wins inside its subtree; the rest of the outer force stands."""
    host = _host(_forced_release(tmp_path), "forced", "main.tslua")
    deep = [t for t in host.tree if t.name == "deep one"][0]
    kid = [t for t in host.tree if t.name == "kid"][0]
    assert (deep.default_enabled, kid.default_enabled) == (True, False)
    assert deep.step.enabled is True and kid.step.enabled is True  # the child's own default was never set


def test_a_header_with_no_set_enable_leaves_its_children_alone(tmp_path) -> None:
    """Without `setEnable` the header forces nothing: each step keeps its own default."""
    host = _host(_cascade_release(tmp_path), "cascade", "main.tslua")
    defaults = {t.name: t.default_enabled for t in host.tree}
    assert defaults == {
        "channel 1": True,   # the header itself: never forced
        "output on": True,
        "output off": False,  # its own :setEnable(false)
        "nested": True,
        "deep one": True,
    }


def test_forced_off_children_record_default_enabled_zero_and_run_notrun(qapp, tmp_path) -> None:
    """A run of the forced schedule records the children's default as 0 and never runs them."""
    session = HeadlessRunSession(_config(tmp_path, _forced_release(tmp_path)))
    session.select_schedule("Forcing main")
    session.start()
    _finish(qapp, session)
    nodes = _nodes(session)
    assert [nodes[seq]["kind"] for seq in range(5)] == ["header", "step", "header", "step", "step"]
    # The outer header forced its subtree off, but the inner header forced its own back on, so
    # something beneath the outer one did run: its `enabled` is "any step beneath was enabled".
    assert nodes[0]["default_enabled"] == 0 and nodes[0]["enabled"] == 1 and nodes[0]["result"] is None
    assert nodes[1]["default_enabled"] == 0 and nodes[1]["enabled"] == 0 and nodes[1]["result"] == "NOTRUN"
    # The inner header forced its own subtree back on: it runs, and its row says enabled.
    assert nodes[2]["default_enabled"] == 1 and nodes[2]["enabled"] == 1 and nodes[2]["result"] is None
    assert nodes[3]["default_enabled"] == 1 and nodes[3]["enabled"] == 1 and nodes[3]["result"] == "PASS"
    assert nodes[4]["result"] == "PASS"
    # A disabled step records no Attempt at all.
    assert not any(a["node_seq"] == 1 for a in session.store.attempts(session.run_uuid))
    assert session.store.get_test_run(session.run_uuid)["result"] == "PASS"


def test_a_header_over_an_empty_sub_schedule_records_its_default(qapp, tmp_path) -> None:
    """No Test Step beneath it: the record keeps the header's default, as the screen draws it checked."""
    root = tmp_path / "defs"
    main = """\
return ScheduleParams("Empty include main", "a header over nothing")
  :setSchedule({
    StepParams("empty"):setStepNo("1."):setPrefix("1."):setInclude("empty.tslua"),
    StepParams("real"):setStepNo("2."):setAction(function() ATE.report(Result.PASS, "ok") end),
  })
"""
    _write_release(root, "empty", {"main.tslua": main, "empty.tslua": 'return ScheduleParams("Empty", "") :setSchedule({})\n'})
    session = HeadlessRunSession(_config(tmp_path, root))
    session.select_schedule("Empty include main")
    session.start()
    _finish(qapp, session)
    header = _nodes(session)[0]
    assert header["kind"] == "header" and header["default_enabled"] == 1 and header["enabled"] == 1


# --- the Run screen: derived header boxes, cascades, and styling -------------


def test_header_rows_are_shaded_and_bold_before_the_run(qapp, tmp_path) -> None:
    """The pre-run list shades header rows `#eef1f5` and bolds them, and leaves steps alone."""
    screen = _open_screen(HeadlessRunSession(_config(tmp_path, _cascade_release(tmp_path))), "Header cascade")
    steps = screen.steps_options_stage.step_list
    header_rows = {0, 3}  # "channel 1" and its nested "nested" header
    for row in range(steps.count()):
        item = steps.item(row)
        if row in header_rows:
            assert item.background().color().name() == "#eef1f5"
            assert item.font().bold()
        else:
            assert not item.font().bold()
    assert [steps.item(i).text() for i in range(steps.count())] == [
        "2.  channel 1",
        "  2.1.  output on",
        "  2.2.  output off",
        "  2.3.  nested",
        "    2.3.1.  deep one",
    ]


def test_a_header_box_derives_from_its_children_and_clicking_it_sets_them_all(qapp, tmp_path) -> None:
    """Mixed children give a partial box; a header click sets them all, one by one after that."""
    session = HeadlessRunSession(_config(tmp_path, _cascade_release(tmp_path)))
    screen = _open_screen(session, "Header cascade")
    steps = screen.steps_options_stage.step_list

    header, on, off, nested, deep = 0, 1, 2, 3, 4
    assert steps.item(on).checkState() == Qt.CheckState.Checked and steps.item(deep).checkState() == Qt.CheckState.Checked
    assert steps.item(nested).checkState() == Qt.CheckState.Checked  # one step, on
    assert steps.item(header).checkState() == Qt.CheckState.PartiallyChecked  # on/off/nested mixed

    # Clicking the header (Qt turns an indeterminate box to checked) sets every step beneath it.
    steps.item(header).setCheckState(Qt.CheckState.Checked)
    assert session.node_enables == {1: True, 2: True, 4: True}  # the header rows themselves: never stored
    assert steps.item(off).checkState() == Qt.CheckState.Checked
    assert steps.item(header).checkState() == Qt.CheckState.Checked

    # Unchecking the nested header re-derives the header above it too.
    steps.item(nested).setCheckState(Qt.CheckState.Unchecked)
    assert steps.item(deep).checkState() == Qt.CheckState.Unchecked
    assert steps.item(header).checkState() == Qt.CheckState.PartiallyChecked
    steps.item(nested).setCheckState(Qt.CheckState.Checked)
    assert steps.item(header).checkState() == Qt.CheckState.Checked

    # Children can still be toggled one by one afterwards, and the header reads back live.
    steps.item(off).setCheckState(Qt.CheckState.Unchecked)
    assert session.node_enables == {1: True, 2: False, 4: True}
    assert steps.item(header).checkState() == Qt.CheckState.PartiallyChecked
    assert steps.item(nested).checkState() == Qt.CheckState.Checked

    # A click on that partial header lands on Checked again...
    steps.item(header).setCheckState(Qt.CheckState.Checked)
    assert session.node_enables == {1: True, 2: True, 4: True}
    assert steps.item(off).checkState() == Qt.CheckState.Checked

    # ...and from Checked the next click turns everything off: the run records them NOTRUN.
    steps.item(header).setCheckState(Qt.CheckState.Unchecked)
    assert session.node_enables == {1: False, 2: False, 4: False}
    assert steps.item(nested).checkState() == Qt.CheckState.Unchecked
    assert steps.item(header).checkState() == Qt.CheckState.Unchecked

    session.start()
    _finish(qapp, session)
    nodes = _nodes(session)
    for seq in (1, 2, 4):
        assert nodes[seq]["result"] == "NOTRUN" and nodes[seq]["enabled"] == 0
    assert nodes[0]["enabled"] == 0  # nothing beneath the header was enabled
    assert not any(a["node_seq"] in (1, 2, 4) for a in session.store.attempts(session.run_uuid))


def test_a_partial_header_click_turns_every_child_on_and_they_run(qapp, tmp_path) -> None:
    """From a partial box a click lands on Checked, and the run records every child enabled."""
    session = HeadlessRunSession(_config(tmp_path, _cascade_release(tmp_path)))
    screen = _open_screen(session, "Header cascade")
    steps = screen.steps_options_stage.step_list
    steps.item(1).setCheckState(Qt.CheckState.Unchecked)  # one child off -> header partial
    assert steps.item(0).checkState() == Qt.CheckState.PartiallyChecked

    steps.item(0).setCheckState(Qt.CheckState.Checked)  # the click an indeterminate box makes
    assert steps.item(0).checkState() == Qt.CheckState.Checked
    assert session.node_enables == {1: True, 2: True, 4: True}

    session.start()
    _finish(qapp, session)
    nodes = _nodes(session)
    assert [nodes[seq]["result"] for seq in range(5)] == [None, "PASS", "PASS", None, "PASS"]
    assert [nodes[seq]["enabled"] for seq in range(5)] == [1, 1, 1, 1, 1]
    assert session.store.get_test_run(session.run_uuid)["result"] == "PASS"


def test_the_run_view_header_rows_roll_up_the_steps_beneath_them(qapp, tmp_path) -> None:
    """After a run with a failure the header row reads `N/M done, K failed` and is shaded."""
    session = HeadlessRunSession(_config(tmp_path, _failing_release(tmp_path)))
    screen = _open_screen(session, "Header cascade")
    screen.steps_options_stage.run_button.click()
    _finish(qapp, session)
    rows = [screen.run_view.step_list.item(i).text() for i in range(screen.run_view.step_list.count())]
    assert rows == [
        "2.  channel 1  3/3 done, 1 failed",
        "  2.1.  output on  →  PASS",
        "  2.2.  output bad  →  FAIL",
        "  2.3.  nested  1/1 done",
        "    2.3.1.  deep one  →  PASS",
    ]
    for row in (0, 3):
        item = screen.run_view.step_list.item(row)
        assert item.background().color().name() == "#eef1f5"
        assert item.font().bold()
    assert not screen.run_view.step_list.item(1).font().bold()


def test_the_paused_run_view_shows_a_partial_rollup(qapp, tmp_path) -> None:
    """Mid-run, a header rolls up only the steps that already have a Result."""
    session = HeadlessRunSession(_config(tmp_path, _failing_release(tmp_path, stop_on_fail=True)))
    screen = _open_screen(session, "Header cascade")
    prompts: list = []
    session.failure_prompt.connect(lambda seq, attempt: prompts.append((seq, attempt)))
    screen.steps_options_stage.run_button.click()
    deadline = time.monotonic() + 10.0
    while not prompts and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert prompts == [(2, 1)]
    rows = [screen.run_view.step_list.item(i).text() for i in range(screen.run_view.step_list.count())]
    # The failing step's node row is written only after its last Attempt resolves, so the header
    # above it counts the steps already recorded.
    assert rows == [
        "2.  channel 1  1/3 done",
        "  2.1.  output on  →  PASS",
        "  2.2.  output bad  →  FAIL",
        "  2.3.  nested",
        "    2.3.1.  deep one  →  …",
    ]
    screen.run_view.failure_prompt_bar.continue_button.click()
    assert _finish(qapp, session)


# --- the mockup screenshots --------------------------------------------------


def _pixels(widget) -> set[tuple[int, int, int]]:
    """Every RGB triple in the widget's own grab — the check that the shading really painted."""
    image = widget.grab().toImage()
    return {(
        (image.pixel(x, y) >> 16) & 0xFF,
        (image.pixel(x, y) >> 8) & 0xFF,
        image.pixel(x, y) & 0xFF,
    ) for y in range(image.height()) for x in range(image.width())}


HEADER_RGB = (238, 241, 245)  # #eef1f5


def test_the_pre_run_and_run_view_screenshots_carry_the_header_shading(qapp, tmp_path) -> None:
    """Save the two mockup shots and read their pixels back: header rows really are #eef1f5."""
    SHOTS.mkdir(parents=True, exist_ok=True)

    session = HeadlessRunSession(_config(tmp_path, _cascade_release(tmp_path)))
    screen = _open_screen(session, "Header cascade")
    assert screen.steps_options_stage.step_list.item(0).checkState() == Qt.CheckState.PartiallyChecked
    screen.steps_options_stage.step_list.grab().save(str(SHOTS / "prerun-partial.png"))

    root = tmp_path / "defs2"
    _write_release(root, "rollup", {"main.tslua": MAIN_SRC,
                                    "channel.tslua": FAILING_CHANNEL_SRC, "deep.tslua": DEEP_SRC})
    run_session = HeadlessRunSession(_config(tmp_path, root))
    run_screen = _open_screen(run_session, "Header cascade")
    run_screen.steps_options_stage.run_button.click()
    _finish(qapp, run_session)
    assert "3/3 done, 1 failed" in run_screen.run_view.step_list.item(0).text()
    run_screen.run_view.step_list.grab().save(str(SHOTS / "runview-rollup.png"))

    assert (SHOTS / "prerun-partial.png").stat().st_size > 0
    assert (SHOTS / "runview-rollup.png").stat().st_size > 0
    assert HEADER_RGB in _pixels(screen.steps_options_stage.step_list)
    assert HEADER_RGB in _pixels(run_screen.run_view.step_list)
