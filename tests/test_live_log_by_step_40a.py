"""Packet 40a — the live Run log grouped by Test Step, in both layouts.

GitHub issue #3 ("rc4 issues"): a line the worker records must reach the Log tab the moment it is
recorded, not at the end of the step or the schedule, and the panes must group the lines under one
label per Test Step. Every test here drives a real :class:`HeadlessRunSession` (and, for the switch,
a real `MainWindow`) offscreen; the grouping itself lives in the shared
:class:`~ate.ui.run_widgets.LogPane`, so both layouts render the same text.
"""

import inspect
import time
from typing import Optional

import pytest
from PySide6.QtWidgets import QLabel, QPlainTextEdit

from ate.app import launch
from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.storage.store import RunStore, new_uuid7
import ate.ui.history_screen as history_screen
import ate.ui.run_widgets as run_widgets
import ate.ui.staged_run_screen as staged_run_screen
from ate.ui.run_widgets import LogPane
from ate.ui.staged_run_screen import StagedRunScreen
from ate.ui.three_pane_run_screen import ThreePaneRunScreen

from ._fixtures import (
    LoopbackPeer,
    WRITES_PDF,
    add_bulky_log_schedule,
    add_failing_poll_schedule,
    add_grouped_log_schedule,
    add_latched_log_schedule,
    add_open_device_log_schedule,
    add_phased_latch_schedule,
    add_plain_log_schedule,
    add_quiet_latched_schedule,
    add_steady_log_schedule,
    fake_pdf_helper,
    fill_operator_fields,
    write_ate_env,
)

STEP_HEADING = "── Step 1: supply ──"
PRE_HEADING = "── Pre-test ──"
POST_HEADING = "── Post-test ──"
RUN_HEADING = "── Run ──"


@pytest.fixture
def fake_pdf(monkeypatch):
    """Stand in for the QWebEngine PDF helper so a finished run's report job stays cheap."""
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", fake_pdf_helper(WRITES_PDF))


def _wait(qapp, predicate, timeout: float = 10.0) -> bool:
    """Pump events until ``predicate`` holds; a timeout is a failure, not a quiet pass."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    raise AssertionError(f"predicate never held within {timeout} s")


def _pick(screen, name):
    """Select the three-pane picker row whose label contains ``name``."""
    list_widget = screen.picker.schedule_list
    for i in range(list_widget.count()):
        item = list_widget.item(i)
        widget = list_widget.itemWidget(item)
        if widget is None:
            if name in item.text():
                list_widget.setCurrentItem(item)
                return item
            continue
        for label in widget.findChildren(QLabel):
            if name in label.text():
                list_widget.setCurrentItem(item)
                return item
    raise AssertionError(f"no schedule row containing {name!r}")


def _start_staged(qapp, screen, name):
    """Drive the staged layout from setup to a started run through the schedule picker."""
    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    items = screen.test_schedule_stage.schedule_list
    for i in range(items.count()):
        if name in items.item(i).text():
            items.setCurrentItem(items.item(i))
            break
    screen._begin_run_from_selection()
    screen.steps_options_stage.run_button.click()


def _live_input(qapp, session, widgets) -> object:
    """Wait for the step's blocking INTEGER input to become live and return its widget."""
    def _asked():
        for ws in widgets:
            for widget in ws:
                if widget.line_edit is not None and widget.line_edit.isEnabled():
                    return widget
        return None

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        widget = _asked()
        if widget is not None:
            return widget
        time.sleep(0.01)
    raise AssertionError("the input never became live")


def _answer(qapp, widget):
    widget.line_edit.setText("7")
    widget.ok_button.click()
    qapp.processEvents()


def _settle(qapp, session, timeout=5.0):
    """Let the report helper finish before the session goes out of scope."""
    if session.run_uuid is not None:
        finished = {"done": False}
        session.report_written.connect(lambda *_seen: finished.__setitem__("done", True))
        deadline = time.monotonic() + timeout
        while not finished["done"] and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
    session.close()


def _step_not_finished(session) -> bool:
    """True while step 1 has no recorded Attempt result yet."""
    for node in session.store.nodes(session.run_uuid):
        if node.get("step_name") == "supply":
            return node.get("result") in (None, "NOTRUN")
    return True


def _count(lines: list[str], needle: str) -> int:
    """How many of the pane's rendered lines contain ``needle``."""
    return sum(1 for line in lines if needle in line)


def _group_of(lines: list[str], needle: str) -> Optional[str]:
    """The heading of the group the first line containing ``needle`` sits under."""
    heading = None
    for line in lines:
        if line.startswith("──") and line.endswith("──"):
            heading = line
        elif needle in line:
            return heading
    return None


def test_three_pane_shows_a_step_log_while_the_step_is_blocked(qapp, tmp_path, fake_pdf, monkeypatch):
    """Issue #3 in the three-pane layout, with the step blocked on nothing but its own wait.

    Packet 40a review, item 1: the step logs at INFO and DEBUG, then loops on a latch the test
    releases (`ATE_LATCH_OPEN`) — no Step Input, no state change, nothing else that could repaint
    the pane. The layout has already drawn the step's heading before the step logs anything, so the
    line can only reach the Log tab through the session's ``log_appended`` notification: removing
    the layout's connection to that signal makes this test fail."""
    env = write_ate_env(tmp_path)
    name = add_latched_log_schedule(tmp_path)
    session = HeadlessRunSession(env["config"])
    heard: list[str] = []
    session.log_appended.connect(lambda run_uuid: heard.append(run_uuid))
    screen = ThreePaneRunScreen(session)
    fill_operator_fields(screen)
    screen.show()
    qapp.processEvents()
    _pick(screen, name)
    screen.start_button.click()

    # The step's own lines are logged 0.3 s in, well after every refresh the start itself causes.
    _wait(qapp, lambda: STEP_HEADING in screen.log_pane.rendered_lines())
    assert "measuring the supply" not in "\n".join(screen.log_pane.rendered_lines())

    _wait(qapp, lambda: "measuring the supply" in "\n".join(screen.log_pane.rendered_lines()))
    pane = screen.log_pane.rendered_lines()
    assert STEP_HEADING in pane, "the step's lines are not under a step heading"
    assert PRE_HEADING in pane and pane.index(PRE_HEADING) < pane.index(STEP_HEADING)
    assert _step_not_finished(session), "the step had already finished"
    assert heard, "the session never announced a recorded log line"
    assert set(heard) == {session.run_uuid}
    # INFO and above only in the Log tab; every level in the Debug window.
    assert not any("probe chatter" in line for line in pane)
    assert any("probe chatter" in line for line in screen.debug_window.rendered_lines())

    monkeypatch.setenv("ATE_LATCH_OPEN", "1")  # release the wait; nothing else repaints the pane
    assert _wait(qapp, lambda: session.state == SessionState.FINISHED)
    assert any("latch released" in line for line in screen.log_pane.rendered_lines())
    _settle(qapp, session)


def test_staged_shows_a_step_log_while_the_step_is_blocked(qapp, tmp_path, fake_pdf, monkeypatch):
    """The same blocked-step live behaviour in the staged layout's Run view."""
    env = write_ate_env(tmp_path)
    name = add_latched_log_schedule(tmp_path)
    session = HeadlessRunSession(env["config"])
    heard: list[str] = []
    session.log_appended.connect(lambda run_uuid: heard.append(run_uuid))
    screen = StagedRunScreen(session)
    fill_operator_fields(screen)
    screen.show()
    qapp.processEvents()
    _start_staged(qapp, screen, name)

    _wait(qapp, lambda: STEP_HEADING in screen.run_view.log_pane.rendered_lines())
    assert "measuring the supply" not in "\n".join(screen.run_view.log_pane.rendered_lines())

    _wait(qapp, lambda: "measuring the supply" in "\n".join(screen.run_view.log_pane.rendered_lines()))
    pane = screen.run_view.log_pane.rendered_lines()
    assert STEP_HEADING in pane, pane
    assert _step_not_finished(session), "the step had already finished"
    assert heard and set(heard) == {session.run_uuid}
    assert not any("probe chatter" in line for line in pane)
    assert any("probe chatter" in line for line in screen.run_view.debug_window.rendered_lines())

    monkeypatch.setenv("ATE_LATCH_OPEN", "1")
    assert _wait(qapp, lambda: session.state == SessionState.FINISHED)
    assert any("latch released" in line for line in screen.run_view.log_pane.rendered_lines())
    _settle(qapp, session)


def test_the_signal_repaints_the_panes_without_a_poll(qapp, tmp_path, fake_pdf):
    """A `log_appended` emission alone is enough to redraw the panes from the store.

    After a finished run the panes are cleared and nothing else refreshes them; emitting the signal
    (the same queued path the worker's inserts use) must fill them again. That is what proves the
    layouts are driven by the notification rather than by polling or by guessing from other signals."""
    env = write_ate_env(tmp_path)
    name = add_grouped_log_schedule(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    fill_operator_fields(screen)
    screen.show()
    qapp.processEvents()
    _start_staged(qapp, screen, name)
    widget = _live_input(qapp, session, screen.run_view.input_widgets.values())
    _answer(qapp, widget)
    assert _wait(qapp, lambda: session.state == SessionState.FINISHED)

    screen.run_view.log_pane.clear()
    session.log_appended.emit(session.run_uuid)
    qapp.processEvents()
    assert screen.run_view.log_pane.rendered_lines(), "the panes did not redraw from the signal"
    _settle(qapp, session)


def test_the_log_is_grouped_by_step_in_schedule_order(qapp, tmp_path, fake_pdf):
    """One heading per scheduled step, in schedule order, with its lines under it.

    Pre-test lines come before the steps and Post-test lines after them. A step that ran and logged
    nothing says "No output."; the disabled step is still listed and says "Not run."; a step that
    logged never gets a note; the DEBUG line never reaches the Log tab.
    """
    env = write_ate_env(tmp_path)
    name = add_grouped_log_schedule(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    fill_operator_fields(screen)
    screen.show()
    qapp.processEvents()
    _start_staged(qapp, screen, name)
    widget = _live_input(qapp, session, screen.run_view.input_widgets.values())
    _answer(qapp, widget)
    assert _wait(qapp, lambda: session.state == SessionState.FINISHED)

    pane = screen.run_view.log_pane.rendered_lines()
    headings = [PRE_HEADING, STEP_HEADING, "── Step 2: silent step ──", "── Step 3: disabled step ──", POST_HEADING]
    found = [pane.index(h) for h in headings]
    assert all(a < b for a, b in zip(found, found[1:])), f"headings out of order: {pane}"
    text = "\n".join(pane)
    assert "pretest warmup" in text and "posttest teardown" in text
    assert RUN_HEADING not in pane, "a run with no run-level lines still drew a Run group"
    assert pane.count("No output.") == 1, pane  # only the step that ran and logged nothing
    assert pane.count("Not run.") == 1, pane  # the disabled step never ran
    assert "never runs" not in text, "the disabled step's line was recorded"
    step_start = pane.index(STEP_HEADING)
    step_end = pane.index("── Step 2: silent step ──")
    assert any("measuring the supply" in line for line in pane[step_start:step_end])
    # Extra refreshes (step finish, state change) must not duplicate a line.
    screen._refresh_log()
    screen._refresh_log()
    pane_after = screen.run_view.log_pane.rendered_lines()
    assert pane_after.count("measuring the supply") == pane.count("measuring the supply")
    assert pane_after == pane, "a second refresh changed the grouped log"
    _settle(qapp, session)


def test_a_stop_on_fail_abort_still_lists_the_steps_that_never_ran(qapp, tmp_path, fake_pdf):
    """Steps after a Stop-on-Fail end keep a heading and say "Not run." rather than vanishing."""
    env = write_ate_env(tmp_path)
    name = add_failing_poll_schedule(tmp_path)
    session = HeadlessRunSession(env["config"])
    session.failure_prompt.connect(lambda seq, attempt: session.choose("ABORT"))
    screen = StagedRunScreen(session)
    fill_operator_fields(screen)
    screen.show()
    qapp.processEvents()
    _start_staged(qapp, screen, name)
    assert _wait(qapp, lambda: session.state == SessionState.FINISHED)

    pane = screen.run_view.log_pane.rendered_lines()
    assert "── Step 2: second ──" in pane, pane
    assert pane[pane.index("── Step 2: second ──") + 1] == "Not run."
    _settle(qapp, session)


def test_run_level_lines_get_their_own_group():
    """Rows the store owns to nobody (``node_seq`` NULL, no Action) sit in a `── Run ──` group.

    The grouping is exercised straight on the shared pane so the exact group order is visible:
    Pre-test, the steps in schedule order, Post-test, then the run-level lines."""
    pane = LogPane("")
    nodes = [
        {"node_seq": 0, "kind": "step", "step_number": "1.", "step_name": "supply", "result": "PASS"},
        {"node_seq": 1, "kind": "header", "step_number": "2.", "step_name": "a header"},
        {"node_seq": 2, "kind": "step", "step_number": "2.1", "step_name": "measure", "result": "NOTRUN"},
    ]
    actions = [  # each Action row is written after the lines its instance logged (the engine's order)
        {"seq": 1, "instance_seq": 1, "phase": "pre", "run_order": 0},
        {"seq": 4, "instance_seq": 2, "phase": "post", "run_order": 0},
    ]
    pane.add_lines(
        [
            {"seq": 0, "level": "INFO", "source": "schedule", "text": "warmup", "action_seq": 1},
            {"seq": 1, "level": "INFO", "source": "schedule", "text": "in step one", "node_seq": 0},
            {"seq": 2, "level": "WARNING", "source": "ate", "text": "a device was left open"},
            {"seq": 3, "level": "INFO", "source": "schedule", "text": "teardown", "action_seq": 2},
        ],
        nodes,
        actions,
    )
    out = pane.rendered_lines()
    assert out[0] == PRE_HEADING, out
    assert "── Step 2.: a header ──" not in out  # headers are containers, not Test Steps
    step_order = [pane_i for pane_i, line in enumerate(out) if line.startswith("── Step ")]
    assert out.index("── Post-test ──") > max(step_order)
    assert out.index(RUN_HEADING) > out.index("── Post-test ──")
    assert out[out.index(RUN_HEADING) + 1].endswith("a device was left open")
    assert out.count("Not run.") == 1  # only the never-run "measure" step (packet 40a review, item 5)
    assert sum(1 for line in out if "in step one" in line) == 1


def test_a_device_left_open_warns_in_the_run_group_of_a_real_run(qapp, tmp_path, fake_pdf):
    """The same grouping against real rows: the teardown's Device warning owns to nobody.

    The run-end close writes a WARNING with no `node_seq` and no `action_seq`, so it sits under
    `── Run ──` rather than inside the step that opened the Device."""
    env = write_ate_env(tmp_path)
    peer = LoopbackPeer(payload=b"hi\n")
    peer.start()
    try:
        name = add_open_device_log_schedule(tmp_path, peer.port)
        session = HeadlessRunSession(env["config"])
        screen = ThreePaneRunScreen(session)
        fill_operator_fields(screen)
        screen.show()
        qapp.processEvents()
        _pick(screen, name)
        screen.start_button.click()
        assert _wait(qapp, lambda: session.state == SessionState.FINISHED)

        pane = screen.log_pane.rendered_lines()
        assert any("opening the device" in line for line in pane), pane
        assert RUN_HEADING in pane, pane
        assert "left open at end of run" in pane[pane.index(RUN_HEADING) + 1], pane
        step_group = pane[pane.index("── Step 1: open ──"):pane.index(RUN_HEADING)]
        assert not any("left open" in line for line in step_group), step_group
        _settle(qapp, session)
    finally:
        peer.stop()


def test_append_to_a_group_keeps_the_scroll_and_the_tail(qapp):
    """Ticket 39's scroll rule survives the grouping: appending keeps the Operator's place.

    Middle-of-pane readers stay put when a later line lands; a reader pinned to the tail follows
    the tail. A line landing in an *earlier* group re-renders the whole text, and the same rule
    applies to the scroll position."""
    pane = LogPane("")
    pane.show()
    qapp.processEvents()
    nodes = [{"node_seq": 0, "kind": "step", "step_number": "1.", "step_name": "supply"}]
    lines = [{"seq": i, "level": "INFO", "source": "schedule", "text": f"line {i}"} for i in range(200)]
    pane.add_lines(lines, nodes)
    bar = pane.text_widget().verticalScrollBar()
    bar.setValue(60)
    pane.add_lines(lines + [{"seq": 200, "level": "INFO", "source": "schedule", "text": "still step one"}], nodes)
    assert bar.value() == 60, "the append threw the Operator out of the middle of the log"
    assert any("still step one" in line for line in pane.rendered_lines())
    bar.setValue(bar.maximum())
    pane.add_lines(
        lines + [{"seq": 200, "level": "INFO", "source": "schedule", "text": "still step one"},
                {"seq": 201, "level": "INFO", "source": "schedule", "text": "and again"}],
        nodes,
    )
    assert bar.value() == bar.maximum(), "the tail was not followed"
    pane.clear()
    pane.add_lines(lines, nodes)
    assert pane.rendered_lines()[0] == "── Step 1: supply ──"


def test_a_layout_switch_mid_run_counts_every_line_once(qapp, tmp_path, fake_pdf, monkeypatch):
    """Packet 40a review, item 2: switching layouts while a step is blocked, then again after it ends.

    The Run log is painted while the step is still waiting, so the switch has to carry the lines
    the hidden layout deferred without duplicating or dropping any. Every line the run has
    recorded appears exactly once in whichever layout is on screen, at the mid-run switch and
    again after the run finishes."""
    env = write_ate_env(tmp_path)
    name = add_latched_log_schedule(tmp_path)
    RunStore(env["db"])  # migrate the database before the window opens on it
    outcome = launch(["--config", str(env["config"])], exec_=False, prompt=lambda path: True, alert=lambda m: None)
    window = outcome.window
    assert window is not None
    session = window.session
    session.select_schedule(name)
    session.set_operator("Ada Lovelace")
    session.set_project("PSU-200 Production")
    session.set_uut_type("PowerSupply")
    session.set_uut_serial("PS-1")
    session.start()

    staged = window.staged_screen
    three = window.three_pane_screen

    _wait(qapp, lambda: STEP_HEADING in staged.run_view.log_pane.rendered_lines())
    _wait(qapp, lambda: "measuring the supply" in "\n".join(staged.run_view.log_pane.rendered_lines()))
    assert _step_not_finished(session), "the step finished before the switch was made"

    window.three_pane_layout_action.trigger()
    qapp.processEvents()
    assert window.stack.currentWidget() is three
    mid = three.log_pane.rendered_lines()
    mid_recorded = [ln["text"] for ln in session.store.log_lines(session.run_uuid) if str(ln["level"]) != "DEBUG"]
    assert mid_recorded, "the run had recorded nothing to switch over"
    for text in mid_recorded:
        assert _count(mid, text) == 1, f"{text!r} shown { _count(mid, text) } times: {mid}"
    assert _count(mid, STEP_HEADING) == 1, mid

    window.staged_layout_action.trigger()
    qapp.processEvents()
    assert window.stack.currentWidget() is staged
    back = staged.run_view.log_pane.rendered_lines()
    for text in mid_recorded:
        assert _count(back, text) == 1, f"{text!r} shown { _count(back, text) } times: {back}"

    monkeypatch.setenv("ATE_LATCH_OPEN", "1")
    assert _wait(qapp, lambda: session.state == SessionState.FINISHED)

    window.three_pane_layout_action.trigger()
    qapp.processEvents()
    end = three.log_pane.rendered_lines()
    end_recorded = [ln["text"] for ln in session.store.log_lines(session.run_uuid) if str(ln["level"]) != "DEBUG"]
    for text in end_recorded:
        assert _count(end, text) == 1, f"{text!r} shown { _count(end, text) } times: {end}"
    assert _count(end, "latch released") == 1, end
    assert _count(end, "posttest teardown") == 1, end

    window.staged_layout_action.trigger()
    qapp.processEvents()
    end_staged = staged.run_view.log_pane.rendered_lines()
    assert end_staged == end, "the two layouts painted the same run differently"
    _settle(qapp, session)


def test_a_step_that_has_not_run_yet_says_so(qapp, tmp_path, fake_pdf, monkeypatch):
    """Packet 40a review, item 5, three-pane: a step nobody has run does not read as silent.

    While step 1 is blocked, steps 2 and 3 have no Attempt yet, so both say "Not run yet." rather
    than "No output.". Once the run is FINISHED the step that ran and logged nothing keeps
    "No output." and the disabled one says "Not run."."""
    env = write_ate_env(tmp_path)
    name = add_latched_log_schedule(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = ThreePaneRunScreen(session)
    fill_operator_fields(screen)
    screen.show()
    qapp.processEvents()
    _pick(screen, name)
    screen.start_button.click()

    _wait(qapp, lambda: _count(screen.log_pane.rendered_lines(), "measuring the supply"))
    mid = screen.log_pane.rendered_lines()
    assert _count(mid, "Not run yet.") == 1, mid  # the step the worker has not reached
    assert _count(mid, "Not run.") == 1, mid  # the disabled step says so from the start (item 5)
    assert "No output." not in mid, mid

    monkeypatch.setenv("ATE_LATCH_OPEN", "1")
    assert _wait(qapp, lambda: session.state == SessionState.FINISHED)
    end = screen.log_pane.rendered_lines()
    assert "Not run yet." not in end, end
    assert end[end.index("── Step 2: silent step ──") + 1] == "No output.", end
    assert end[end.index("── Step 3: disabled step ──") + 1] == "Not run.", end
    _settle(qapp, session)


def test_a_step_that_has_not_run_yet_says_so_in_the_staged_view(qapp, tmp_path, fake_pdf, monkeypatch):
    """The same note rules in the staged layout's Run view."""
    env = write_ate_env(tmp_path)
    name = add_latched_log_schedule(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    fill_operator_fields(screen)
    screen.show()
    qapp.processEvents()
    _start_staged(qapp, screen, name)

    _wait(qapp, lambda: _count(screen.run_view.log_pane.rendered_lines(), "measuring the supply"))
    mid = screen.run_view.log_pane.rendered_lines()
    assert _count(mid, "Not run yet.") == 1, mid
    assert _count(mid, "Not run.") == 1, mid
    assert "No output." not in mid, mid

    monkeypatch.setenv("ATE_LATCH_OPEN", "1")
    assert _wait(qapp, lambda: session.state == SessionState.FINISHED)
    end = screen.run_view.log_pane.rendered_lines()
    assert "Not run yet." not in end, end
    assert end[end.index("── Step 2: silent step ──") + 1] == "No output.", end
    assert end[end.index("── Step 3: disabled step ──") + 1] == "Not run.", end
    _settle(qapp, session)


def test_an_action_line_is_grouped_while_its_action_is_running(qapp, tmp_path, fake_pdf, monkeypatch):
    """Packet 40a review, item 4, three-pane: the phase group exists before the Action's row does.

    A Pre-/Post-test Action's own row is written when the Action ends, so the lines it logs while it
    is running have an `action_seq` and no Action row to match. Both Actions here block on a latch the
    test releases, so the pane can be read at exactly that moment: the Pre-test line must sit under
    `── Pre-test ──` and the Post-test line under `── Post-test ──` while each is still running,
    never transiently in `── Run ──`."""
    env = write_ate_env(tmp_path)
    name = add_phased_latch_schedule(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = ThreePaneRunScreen(session)
    fill_operator_fields(screen)
    screen.show()
    qapp.processEvents()
    _pick(screen, name)
    screen.start_button.click()

    pane = screen.log_pane
    _wait(qapp, lambda: _count(pane.rendered_lines(), "pretest action running"))
    mid = pane.rendered_lines()
    assert not session.store.actions(session.run_uuid), "the Pre-test Action row already existed"
    assert _group_of(mid, "pretest action running") == PRE_HEADING, mid
    assert PRE_HEADING in mid and mid.index(PRE_HEADING) + 1 < len(mid) and "pretest action running" in mid[mid.index(PRE_HEADING) + 1], mid
    assert RUN_HEADING not in mid, mid

    monkeypatch.setenv("PRE_LATCH_OPEN", "1")
    _wait(qapp, lambda: _count(pane.rendered_lines(), "posttest action running"))
    mid = pane.rendered_lines()
    assert _count(mid, "pretest action running") == 1, mid  # the Action row landing did not duplicate it
    assert _group_of(mid, "posttest action running") == POST_HEADING, mid
    assert "pretest action running" in mid[mid.index(PRE_HEADING) + 1], mid
    assert RUN_HEADING not in mid, mid

    monkeypatch.setenv("POST_LATCH_OPEN", "1")
    assert _wait(qapp, lambda: session.state == SessionState.FINISHED)
    end = pane.rendered_lines()
    assert _count(end, "pretest action running") == 1 and _count(end, "posttest action running") == 1, end
    fresh = LogPane("")
    fresh.add_lines(session.store.log_lines(session.run_uuid), screen._log_group_rows(), session.store.actions(session.run_uuid))
    assert end == fresh.rendered_lines(), "the live grouping ended somewhere else than a full render"
    _settle(qapp, session)


def test_an_action_line_is_grouped_while_its_action_is_running_in_the_staged_view(qapp, tmp_path, fake_pdf, monkeypatch):
    """The same phase-grouping rule in the staged layout's Run view."""
    env = write_ate_env(tmp_path)
    name = add_phased_latch_schedule(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    fill_operator_fields(screen)
    screen.show()
    qapp.processEvents()
    _start_staged(qapp, screen, name)

    pane = screen.run_view.log_pane
    _wait(qapp, lambda: _count(pane.rendered_lines(), "pretest action running"))
    mid = pane.rendered_lines()
    assert not session.store.actions(session.run_uuid), "the Pre-test Action row already existed"
    assert _group_of(mid, "pretest action running") == PRE_HEADING, mid
    assert RUN_HEADING not in mid, mid

    monkeypatch.setenv("PRE_LATCH_OPEN", "1")
    _wait(qapp, lambda: _count(pane.rendered_lines(), "posttest action running"))
    mid = pane.rendered_lines()
    assert _count(mid, "pretest action running") == 1, mid
    assert _group_of(mid, "posttest action running") == POST_HEADING, mid
    assert RUN_HEADING not in mid, mid

    monkeypatch.setenv("POST_LATCH_OPEN", "1")
    assert _wait(qapp, lambda: session.state == SessionState.FINISHED)
    end = pane.rendered_lines()
    assert _count(end, "pretest action running") == 1 and _count(end, "posttest action running") == 1, end
    fresh = LogPane("")
    fresh.add_lines(session.store.log_lines(session.run_uuid), screen._log_group_rows(), session.store.actions(session.run_uuid))
    assert end == fresh.rendered_lines(), "the live grouping ended somewhere else than a full render"
    _settle(qapp, session)


def test_a_chatty_run_repaints_bounded_times_not_once_per_line(qapp, tmp_path, fake_pdf, monkeypatch):
    """Packet 40a review, item 3 and packet 40a fix 2, item 2: 5,000 lines arriving one by one stay bounded.

    The worker writes the burst one row at a time and announces every row on ``log_appended``. The
    layout must coalesce the notifications (one read per interval, and at least one of those reads lands
    while the step is still running), read only the rows that are new, never read the whole log, append
    rather than repaint where that is possible, keep every repaint within the window, and end with
    exactly what one full render from the store shows."""
    env = write_ate_env(tmp_path)
    name = add_bulky_log_schedule(tmp_path)
    session = HeadlessRunSession(env["config"])
    announced: list[str] = []
    session.log_appended.connect(lambda run_uuid: announced.append(run_uuid))
    screen = ThreePaneRunScreen(session)
    fill_operator_fields(screen)
    screen.show()
    qapp.processEvents()
    _pick(screen, name)
    screen.start_button.click()

    pane, widget = screen.log_pane, screen.log_pane.text_widget()
    paints: dict[str, int] = {"set": 0, "append": 0}
    widest = {"lines": 0}
    real_set, real_append = widget.setPlainText, widget.appendPlainText

    def counted_set(text):
        paints["set"] += 1
        widest["lines"] = max(widest["lines"], len(text.splitlines()))
        real_set(text)

    def counted_append(text):
        paints["append"] += 1
        real_append(text)

    widget.setPlainText, widget.appendPlainText = counted_set, counted_append
    store = session.store
    real_tail = getattr(store, "log_lines_tail", None)
    real_after, real_full = store.log_lines_after, store.log_lines
    reads = {"full": 0, "tail": 0, "after": 0, "mid_stream": 0}

    def full_read(uuid):
        reads["full"] += 1
        return real_full(uuid)

    def tail_read(uuid, limit, min_level=None):
        reads["tail"] += 1
        return real_tail(uuid, limit, min_level=min_level)

    def after_read(uuid, seq, min_level=None):
        rows = real_after(uuid, seq, min_level=min_level)
        reads["after"] += 1
        if rows and _step_not_finished(session):
            reads["mid_stream"] += 1
        return rows

    store.log_lines, store.log_lines_after = full_read, after_read
    if real_tail is not None:
        store.log_lines_tail = tail_read

    _wait(qapp, lambda: len(announced) >= 5000 and _count(pane.rendered_lines(), "burst line 5000") == 1, timeout=30.0)

    assert len(announced) >= 5000, f"the worker announced only {len(announced)} batches"
    assert reads["full"] == 0, f"{reads['full']} whole-log reads on the live path"
    assert reads["after"] * 5 <= len(announced), f"{reads['after']} reads for {len(announced)} notifications: not coalesced"
    assert reads["mid_stream"] >= 1, "no refresh landed while the step was still logging"
    assert paints["set"] <= 20, f"{paints['set']} full re-renders for {len(announced)} notifications"
    assert paints["append"] >= 1, "the live path never appended: it repainted instead"
    window_bound = run_widgets.LOG_PANE_LIMIT + 10  # the window, headings, notes and notice
    assert widest["lines"] <= window_bound, f"a repaint drew {widest['lines']} lines: the window is not bounded"

    _assert_store_window(pane, store, session.run_uuid, "INFO", screen._log_group_rows(),
                         store.actions(session.run_uuid))

    monkeypatch.setenv("ATE_LATCH_OPEN", "1")
    assert _wait(qapp, lambda: session.state == SessionState.FINISHED)
    _assert_store_window(pane, store, session.run_uuid, "INFO", screen._log_group_rows(),
                         store.actions(session.run_uuid))
    widget.setPlainText, widget.appendPlainText = real_set, real_append
    store.log_lines, store.log_lines_after = real_full, real_after
    if real_tail is not None:
        store.log_lines_tail = real_tail
    _settle(qapp, session)


def test_a_new_run_cancels_a_log_read_still_queued(qapp, tmp_path, fake_pdf, monkeypatch):
    """Packet 40a review, item 3: a coalesced read belongs to the run that queued it.

    The layout defers the read by :attr:`RunScreenMixin.LOG_COALESCE_MS` after a notification. If
    another run starts in that gap, the panes have just been emptied for it (packet 30b review) and
    the pending read paints the finished run's lines back onto them — the Operator then reads a log
    that is not the run in front of them. The run-start handler cancels the timer; without that
    cancellation the assertions below fail."""
    env = write_ate_env(tmp_path)
    name = add_latched_log_schedule(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = ThreePaneRunScreen(session)
    fill_operator_fields(screen)
    screen.show()
    qapp.processEvents()
    _pick(screen, name)
    screen.start_button.click()

    _wait(qapp, lambda: _count(screen.log_pane.rendered_lines(), "measuring the supply"))
    monkeypatch.setenv("ATE_LATCH_OPEN", "1")
    _wait(qapp, lambda: session.state == SessionState.FINISHED)

    # The last burst's read is still queued when the Operator starts another run.
    screen._log_timer.start()
    screen.start_new_run()
    screen.start_button.click()
    _wait(qapp, lambda: session.state == SessionState.STARTED)
    assert not screen._log_timer.isActive(), "the finished run's queued log read is still pending"
    for _ in range(40):
        qapp.processEvents()
    pane = screen.log_pane.rendered_lines()
    assert not any("measuring the supply" in line for line in pane), pane
    assert not any("latch released" in line for line in pane), pane

    # and the new run's own lines still arrive live.
    _wait(qapp, lambda: _count(screen.log_pane.rendered_lines(), "measuring the supply"))
    _wait(qapp, lambda: session.state == SessionState.FINISHED)
    _settle(qapp, session)


def test_a_new_run_cancels_a_log_read_still_queued_in_the_staged_view(qapp, tmp_path, fake_pdf, monkeypatch):
    """The same cancelled read in the staged layout's Run view."""
    env = write_ate_env(tmp_path)
    name = add_latched_log_schedule(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    fill_operator_fields(screen)
    screen.show()
    qapp.processEvents()
    _start_staged(qapp, screen, name)

    _wait(qapp, lambda: _count(screen.run_view.log_pane.rendered_lines(), "measuring the supply"))
    monkeypatch.setenv("ATE_LATCH_OPEN", "1")
    _wait(qapp, lambda: session.state == SessionState.FINISHED)

    screen._log_timer.start()
    screen.run_view.new_run_button.click()
    for _ in range(20):
        qapp.processEvents()
    screen.steps_options_stage.run_button.click()
    _wait(qapp, lambda: session.state == SessionState.STARTED)
    assert not screen._log_timer.isActive(), "the finished run's queued log read is still pending"
    for _ in range(40):
        qapp.processEvents()
    pane = screen.run_view.log_pane.rendered_lines()
    assert not any("measuring the supply" in line for line in pane), pane
    assert not any("latch released" in line for line in pane), pane

    _wait(qapp, lambda: _count(screen.run_view.log_pane.rendered_lines(), "measuring the supply"))
    _wait(qapp, lambda: session.state == SessionState.FINISHED)
    _settle(qapp, session)


def test_extending_the_last_group_appends_without_repainting():
    """Packet 40a review, item 3: the pane keeps its groups, so a tail append needs no repaint."""
    pane = LogPane("")
    nodes = [{"node_seq": 0, "kind": "step", "step_number": "1.", "step_name": "supply"}]
    pane.add_lines([{"seq": 0, "level": "INFO", "source": "schedule", "text": "the first line", "node_seq": 0}], nodes, [])
    first = pane.full_render_count()
    for seq in range(1, 5000):
        pane.append_rows([{"seq": seq, "level": "INFO", "source": "schedule", "text": f"line {seq}", "node_seq": 0}], nodes, [])
    assert pane.full_render_count() == first, f"{pane.full_render_count() - first} extra full re-renders"
    assert pane.last_seq() == 4999
    lines = pane.rendered_lines()
    assert len(lines) == 5001, len(lines)  # heading + 5,000 lines
    assert lines[0] == "── Step 1: supply ──" and lines[-1].endswith("line 4999")


def test_a_line_for_an_earlier_group_repaints_once_per_refresh():
    """Packet 40a review, item 3: rows ahead of the tail cost one repaint per refresh, not per row."""
    pane = LogPane("")
    nodes = [
        {"node_seq": 0, "kind": "step", "step_number": "1.", "step_name": "supply"},
        {"node_seq": 1, "kind": "step", "step_number": "2.", "step_name": "later"},
    ]
    pane.add_lines([{"seq": 0, "level": "INFO", "source": "schedule", "text": "the later step started", "node_seq": 1}], nodes, [])
    before = pane.full_render_count()
    for batch in range(10):
        pane.append_rows(
            [{"seq": 1 + batch * 50 + i, "level": "INFO", "source": "schedule",
              "text": f"earlier line {batch * 50 + i}", "node_seq": 0} for i in range(50)],
            nodes, [],
        )
    assert pane.full_render_count() == before + 10, f"{pane.full_render_count() - before} repaints for 10 refreshes"
    assert _count(pane.rendered_lines(), "earlier line") == 500


def test_a_layout_switch_never_duplicates_a_line(qapp, tmp_path, fake_pdf):
    """Switch layouts after a finished run; every recorded line appears exactly once.

    The mid-run switch, while a step is still blocked, is the one in
    :func:`test_a_layout_switch_mid_run_counts_every_line_once`."""
    env = write_ate_env(tmp_path)
    name = add_grouped_log_schedule(tmp_path)
    RunStore(env["db"])  # migrate the database before the window opens on it
    outcome = launch(["--config", str(env["config"])], exec_=False, prompt=lambda path: True, alert=lambda m: None)
    window = outcome.window
    assert window is not None
    session = window.session
    session.select_schedule(name)
    session.set_operator("Ada Lovelace")
    session.set_project("PSU-200 Production")
    session.set_uut_type("PowerSupply")
    session.set_uut_serial("PS-1")
    session.start()

    staged = window.staged_screen
    three = window.three_pane_screen
    widget = _live_input(qapp, session, staged.run_view.input_widgets.values())
    _answer(qapp, widget)
    assert _wait(qapp, lambda: session.state == SessionState.FINISHED)

    window.three_pane_layout_action.trigger()
    qapp.processEvents()
    assert window.stack.currentWidget() is three
    three_lines = three.log_pane.rendered_lines()
    assert any("measuring the supply" in line for line in three_lines)
    assert sum(1 for line in three_lines if "measuring the supply" in line) == 1
    assert "No output." in three_lines
    window.staged_layout_action.trigger()
    qapp.processEvents()
    assert window.stack.currentWidget() is staged
    staged_lines = staged.run_view.log_pane.rendered_lines()
    assert sum(1 for line in staged_lines if "measuring the supply" in line) == 1
    assert sum(1 for line in staged_lines if "pretest warmup" in line) == 1
    assert staged_lines == three_lines, "the two layouts painted the same run differently"
    _settle(qapp, session)


def _action_row(seq: int, instance: int, phase: str, run_order: int, at: str) -> dict:
    """An Action row as the store returns it: written at the Action's end, so `seq` is after its lines."""
    return {"seq": seq, "instance_seq": instance, "phase": phase, "run_order": run_order,
            "started_at": at, "ended_at": at, "outcome": "ok", "error": None}


def _line(seq: int, text: str, at: str, action_seq: int) -> dict:
    return {"seq": seq, "level": "INFO", "source": "schedule", "text": text, "at": at, "action_seq": action_seq}


def test_a_post_line_in_the_pre_end_millisecond_groups_as_post_while_running():
    """Packet 40a fix 2, item 4 (live): a same-millisecond tie cannot move a Post-test line into Pre-test.

    The Pre-test Action ended at `tie`, and the Post-test Action logs in that very millisecond. The
    Post-test has no Action row yet, so its line belongs to the Action the session announced as
    running (`live_phase`). The old timestamp rule read it as the Pre-test's window and put it under
    `── Pre-test ──`."""
    pane = LogPane("")
    nodes = [{"node_seq": 0, "kind": "step", "step_number": "1.", "step_name": "supply"}]
    tie = "2026-11-05T10:00:00.000+00:00"
    actions = [_action_row(5, 1, "pre", 0, tie)]
    pane.add_lines([_line(4, "pretest warmup", tie, 1)], nodes, actions)
    pane.append_rows([_line(6, "posttest teardown", tie, 1)], nodes, actions, live_phase="post")
    assert _group_of(pane.rendered_lines(), "posttest teardown") == POST_HEADING, pane.rendered_lines()
    assert _group_of(pane.rendered_lines(), "pretest warmup") == PRE_HEADING, pane.rendered_lines()


def test_a_post_line_in_the_pre_end_millisecond_groups_as_post_after_restore():
    """Packet 40a fix 2, item 4 (restore): with both Action rows recorded the tie still groups by row order.

    The Post-test Action row exists, so the line's owner is the first Action row of its instance written
    after the line (`seq`), which is the Post-test row. Nothing reads the clock."""
    pane = LogPane("")
    nodes = [{"node_seq": 0, "kind": "step", "step_number": "1.", "step_name": "supply"}]
    tie = "2026-11-05T10:00:00.000+00:00"
    actions = [_action_row(5, 1, "pre", 0, tie), _action_row(9, 1, "post", 0, tie)]
    pane.add_lines([_line(4, "pretest warmup", tie, 1), _line(6, "posttest teardown", tie, 1)], nodes, actions)
    assert _group_of(pane.rendered_lines(), "posttest teardown") == POST_HEADING, pane.rendered_lines()
    assert _group_of(pane.rendered_lines(), "pretest warmup") == PRE_HEADING, pane.rendered_lines()


def test_a_second_pre_action_of_one_instance_stays_in_pre_while_running():
    """Packet 40a fix 2, item 4 (Strata note 5): two Pre-test Actions of one instance, the second running.

    The first Pre-test Action's row has landed, so the old timestamp rule read every later line of the
    instance as its Post-test. The second Pre-test is the one running now (`live_phase` names its
    phase), so its line stays under `── Pre-test ──`."""
    pane = LogPane("")
    nodes = [{"node_seq": 0, "kind": "step", "step_number": "1.", "step_name": "supply"}]
    first_end = "2026-11-05T10:00:00.000+00:00"
    later = "2026-11-05T10:00:00.250+00:00"
    actions = [_action_row(3, 1, "pre", 0, first_end)]
    pane.add_lines([_line(2, "first pretest", first_end, 1)], nodes, actions)
    pane.append_rows([_line(5, "second pretest running", later, 1)], nodes, actions, live_phase="pre")
    assert _group_of(pane.rendered_lines(), "second pretest running") == PRE_HEADING, pane.rendered_lines()
    assert RUN_HEADING not in pane.rendered_lines(), pane.rendered_lines()


def _line_after(lines: list[str], heading: str) -> str:
    """The line directly under a heading (packet 40a fix 2 test helper)."""
    return lines[lines.index(heading) + 1]


def test_a_full_pane_trims_in_batches_not_with_one_repaint_per_line(monkeypatch):
    """The cap is hard; overflowing it drops a batch to the low-water mark."""
    monkeypatch.setattr(run_widgets, "LOG_PANE_LIMIT", 50)
    monkeypatch.setattr(run_widgets, "LOG_PANE_SLACK", 10)
    pane = LogPane("")
    nodes = [{"node_seq": 0, "kind": "step", "step_number": "1.", "step_name": "stream"}]
    for seq in range(205):
        pane.append_rows([{"seq": seq, "level": "INFO", "source": "schedule",
                           "text": f"row {seq}", "node_seq": 0}], nodes, [])
        shown = [line for line in pane.rendered_lines() if "schedule row " in line]
        assert len(shown) <= 50, f"cap exceeded at {seq}: {len(shown)} rows"
        if seq == 50:
            assert len(shown) == 45
        if seq >= 50:
            assert pane.rendered_lines()[0] == (
                "Earlier lines are not shown. The full log is in the run record: "
                "use Export log on the History screen."
            )
            assert _line_after(pane.rendered_lines(), "── Step 1: stream ──") == run_widgets._dropped_note(seq + 1 - len(shown))
    assert pane.full_render_count() == 27


@pytest.mark.parametrize("row_count", [19, 20, 21, 22, 23, 24, 25, 44, 45, 46])
def test_a_full_render_and_the_live_path_keep_suffix_windows(monkeypatch, tmp_path, row_count):
    """Recovery keeps the newest limit rows; each live pane keeps a suffix of that window."""
    monkeypatch.setattr(run_widgets, "LOG_PANE_LIMIT", 20)
    monkeypatch.setattr(run_widgets, "LOG_PANE_SLACK", 2)
    store = RunStore(tmp_path / "ate.db")
    uuid = _seed_run(store, "suffix windows")
    nodes = [{"node_seq": n, "kind": "step", "step_number": f"{n + 1}.",
              "step_name": f"group {n}"} for n in range(2)]
    for i in range(row_count):
        store.insert_log_lines(uuid, 0, [{"level": "DEBUG" if i % 4 == 0 else "INFO",
                                         "source": "schedule", "text": f"row {i}"}], node_seq=i % 2)
    log, debug = LogPane(""), LogPane("")
    for row in store.log_lines(uuid):
        debug.append_rows([row], nodes, [])
        if row["level"] != "DEBUG":
            log.append_rows([row], nodes, [])
    recovery_log, recovery_debug = LogPane(""), LogPane("")
    run_widgets.draw_log_panes(recovery_log, recovery_debug, store, uuid, nodes, [])
    for live, recovery, level in ((log, recovery_log, "INFO"), (debug, recovery_debug, None)):
        tail = store.log_lines_tail(uuid, 20, min_level=level)
        assert _drawn_rows(recovery) == [run_widgets._format_log_line(r) for r in sorted(tail, key=lambda r: (r["node_seq"], r["seq"]))]
        _assert_store_window(live, store, uuid, level, nodes, [])


def _drawn_rows(pane):
    # Timestamp, padded level and source columns distinguish rows from headings and notes.
    import re
    return [line for line in pane.rendered_lines() if re.match(r"^.{9} (DEBUG|INFO|WARNING|ERROR|CRITICAL) +", line)]


def _assert_store_window(pane, store, uuid, level, nodes, actions):
    tail = store.log_lines_tail(uuid, run_widgets.LOG_PANE_LIMIT, min_level=level)
    shown = _drawn_rows(pane)
    assert 0 < len(shown) <= run_widgets.LOG_PANE_LIMIT, f"cap exceeded: {len(shown)} rows"
    expected = tail[-len(shown):]
    # Group order can differ from seq order; compare each pane's drawn group order.
    assert sorted(shown) == sorted(run_widgets._format_log_line(row) for row in expected), "live rows must be a suffix of recovery"
    omitted = run_widgets.trimmed_counts(store, uuid, expected[0]["seq"], level, nodes, actions)
    fresh = LogPane("")
    fresh.add_lines(expected, nodes, actions, dropped=omitted)
    assert pane.rendered_lines() == fresh.rendered_lines(), "store window or dropped counts disagree"


def test_a_group_whose_lines_are_all_trimmed_keeps_its_heading_and_count(monkeypatch, tmp_path):
    """Packet 40a fix 3, item 2: a group the window pushed out entirely is not erased.

    Ten run-level WARNING lines, then 25 step lines: the newest 20 rows are all step lines, so not one
    run line is left. The pane still draws "── Run ──" with the count of the lines it left out under it —
    on the live path, and again after the recovery render from the store."""
    monkeypatch.setattr(run_widgets, "LOG_PANE_LIMIT", 20, raising=False)
    monkeypatch.setattr(run_widgets, "LOG_PANE_SLACK", 5, raising=False)
    nodes = [{"node_seq": 0, "kind": "step", "step_number": "1.", "step_name": "work"}]
    store = RunStore(tmp_path / "ate.db")
    run_uuid = new_uuid7()
    store.insert_test_run({
        "run_uuid": run_uuid, "station_uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c10", "station_name": "BENCH-03",
        "operator": "Ada", "project": "Power board", "uut_type": "PSU", "uut_serial": "PSU-001",
        "ate_version": "1.2.3", "schedule_name": "Trimmed run group", "media_number": "REL-A",
        "stop_on_fail": True, "utc_offset": "+00:00",
    })
    store.insert_log_lines(run_uuid, 0, [{"level": "WARNING", "source": "ate", "text": f"warning {i}"} for i in range(10)])
    store.insert_log_lines(run_uuid, 0, [{"level": "INFO", "source": "schedule", "text": f"work {i}"} for i in range(25)], node_seq=0)

    pane = LogPane("")
    for row in store.log_lines(run_uuid):
        pane.append_rows([row], nodes, [])
    live = pane.rendered_lines()
    assert RUN_HEADING in live and _line_after(live, RUN_HEADING) == "… 10 earlier lines not shown", live[:4]
    assert pane.held_rows() <= 20 and _count(live, "ate warning ") == 0, live[:4]

    recovered = LogPane("")
    run_widgets.draw_log_panes(recovered, LogPane(""), store, run_uuid, nodes, store.actions(run_uuid))
    lines = recovered.rendered_lines()
    assert RUN_HEADING in lines and _line_after(lines, RUN_HEADING) == "… 10 earlier lines not shown", lines[:4]
    assert _count(lines, "schedule work ") == 20 and _count(lines, "ate warning ") == 0, lines


def test_a_trimmed_pre_test_or_post_test_group_keeps_its_heading(monkeypatch):
    """Packet 40a fix 3, item 2: the same rule for the Pre- and Post-test groups.

    A group with nothing left in the window and no Action row to announce it still gets its heading and
    its count of the lines that fell out, in display order: Pre-test first, then the steps, then
    Post-test."""
    pane = LogPane("")
    pane.add_lines([], [{"node_seq": 0, "kind": "step", "step_number": "1.", "step_name": "work"}], [],
                   dropped={("pre",): 2, ("post",): 1})
    lines = pane.rendered_lines()
    assert PRE_HEADING in lines and POST_HEADING in lines, lines
    assert lines.index(PRE_HEADING) < lines.index("── Step 1: work ──") < lines.index(POST_HEADING), lines
    assert _line_after(lines, PRE_HEADING) == run_widgets._dropped_note(2), lines
    assert _line_after(lines, POST_HEADING) == run_widgets._dropped_note(1), lines


def _seed_run(store: RunStore, name: str) -> str:
    """A run row with nothing in it, for the pane tests that read the store."""
    run_uuid = new_uuid7()
    store.insert_test_run({
        "run_uuid": run_uuid, "station_uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c10", "station_name": "BENCH-03",
        "operator": "Ada", "project": "Power board", "uut_type": "PSU", "uut_serial": "PSU-001",
        "ate_version": "1.2.3", "schedule_name": name, "media_number": "REL-A",
        "stop_on_fail": True, "utc_offset": "+00:00",
    })
    return run_uuid


def test_one_line_left_out_reads_singular():
    """Packet 40a fix 3, item 3: one line dropped is one line, not "lines"."""
    assert run_widgets._dropped_note(1) == "… 1 earlier line not shown", run_widgets._dropped_note(1)
    assert run_widgets._dropped_note(2) == "… 2 earlier lines not shown", run_widgets._dropped_note(2)
    pane = LogPane("")
    pane.add_lines([], [{"node_seq": 0, "kind": "step", "step_number": "1.", "step_name": "work"}], [], dropped={("run",): 1})
    lines = pane.rendered_lines()
    assert _line_after(lines, RUN_HEADING) == "… 1 earlier line not shown", lines


def test_the_trim_notice_names_a_button_on_the_history_screen():
    """Packet 40a fix 3, item 3: the notice points at the Export log button, which is on the History screen.

    The run screens have no Export log button, so the notice told the Operator to use a button they can
    not find while the run is up."""
    notice = run_widgets.LOG_TRIM_NOTICE
    assert notice == "Earlier lines are not shown. The full log is in the run record: use Export log on the History screen.", notice
    assert 'QPushButton("Export log")' in inspect.getsource(history_screen.HistoryScreen), "the History screen has no Export log button"


def test_a_step_disabled_in_the_store_reads_as_not_run(tmp_path):
    """Packet 40a fix 3, item 3: `enabled` is stored as 0/1, and 0 is disabled.

    `_step_note` checked `enabled is False`, which a stored row never is, so a step the Operator turned
    off said "Not run yet." until the run ended. A falsy value is disabled; a row that says nothing is
    unknown and keeps the "not run yet" wording."""
    store = RunStore(tmp_path / "ate.db")
    run_uuid = _seed_run(store, "Disabled step")
    store.insert_node(run_uuid, 0, kind="step", step_number="1.", step_name="off", enabled=False)
    row = store.nodes(run_uuid)[0]
    assert row["enabled"] == 0, row
    assert run_widgets._step_note(("step", 0), row, False, set()) == "Not run.", row
    assert run_widgets._step_note(("step", 1), {"node_seq": 1, "enabled": 1}, False, set()) == "Not run yet."
    assert run_widgets._step_note(("step", 2), {"node_seq": 2}, False, set()) == "Not run yet."
    assert run_widgets._step_note(("step", 3), {"node_seq": 3, "enabled": True}, True, set()) == "Not run."


def test_an_unknown_log_level_names_the_level_it_was_given(tmp_path):
    """Packet 40a fix 3, item 3: a level no log line can have is a mistake worth naming.

    `min_level="TRACE"` used to die inside `list.index` with `'TRACE' is not in list`; it now says which
    level is unknown, and a level the log does use still filters case-insensitively."""
    store = RunStore(tmp_path / "ate.db")
    run_uuid = _seed_run(store, "Level filter")
    store.insert_log_lines(run_uuid, 0, [{"level": "INFO", "source": "ate", "text": "info line"},
                                         {"level": "DEBUG", "source": "ate", "text": "debug line"}])
    assert [r["text"] for r in store.log_lines_tail(run_uuid, 10, min_level="info")] == ["info line"]
    for bad_read in (
        lambda: store.log_lines_tail(run_uuid, 10, min_level="TRACE"),
        lambda: store.log_lines_after(run_uuid, 0, min_level="TRACE"),
        lambda: store.log_line_counts(run_uuid, 10, min_level="TRACE"),
    ):
        with pytest.raises(ValueError) as exc:
            bad_read()
        assert "unknown log level: TRACE" in str(exc.value), str(exc.value)


def test_the_finished_staged_run_passes_the_run_context_to_the_panes(qapp, tmp_path, fake_pdf, monkeypatch):
    """Packet 40a fix 3, item 3: both staged paths render the finished run with the same notes.

    `RunScreenMixin._refresh_log` hands the panes whether the run is over, which steps started and which
    Action is live; the staged `RunView` drew the finished run with none of them, so the same run read
    differently in the two layouts. Watch the call the RunView makes and check it carries the context."""
    env = write_ate_env(tmp_path)
    name = add_latched_log_schedule(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    fill_operator_fields(screen)
    screen.show()
    qapp.processEvents()
    calls: list[dict] = []
    real = staged_run_screen.draw_log_panes

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(staged_run_screen, "draw_log_panes", spy)
    monkeypatch.setenv("ATE_LATCH_OPEN", "1")  # the step's wait ends at once: the run is quick
    _start_staged(qapp, screen, name)
    assert _wait(qapp, lambda: session.state == SessionState.FINISHED)
    finished = [c for c in calls if c.get("run_finished")]
    assert finished, f"the finished render passed no run context: {calls}"
    assert "started_steps" in finished[-1] and "live_phase" in finished[-1], finished[-1]
    pane = screen.run_view.log_pane.rendered_lines()
    assert pane[pane.index("── Step 3: disabled step ──") + 1] == "Not run.", pane
    _settle(qapp, session)


def test_a_real_run_keeps_the_newest_rows_in_the_log_and_the_debug_window(qapp, tmp_path, fake_pdf, monkeypatch):
    """Packet 40a fix 2, item 1 and fix 3, item 1 in both panes of a real run: 200 lines logged, a window of 50.

    Each pane keeps at most the newest 50 rows of its own level filter. Both say so on their first line; the
    step keeps its heading with the count of the lines left out; the Pre-test heading stays although its
    only line is outside the window."""
    monkeypatch.setattr(run_widgets, "LOG_PANE_LIMIT", 50, raising=False)
    monkeypatch.setattr(run_widgets, "LOG_PANE_SLACK", 10, raising=False)
    monkeypatch.setenv("ATE_LATCH_OPEN", "1")  # the step's wait ends at once: the run is quick
    env = write_ate_env(tmp_path)
    # Four windows exercise dropping and tail retention without 5,000 durable writes.
    name = add_bulky_log_schedule(tmp_path, count=200)
    session = HeadlessRunSession(env["config"])
    screen = ThreePaneRunScreen(session)
    fill_operator_fields(screen)
    screen.show()
    qapp.processEvents()
    _pick(screen, name)
    screen.start_button.click()
    assert _wait(qapp, lambda: session.state == SessionState.FINISHED, timeout=30.0)

    log = screen.log_pane.rendered_lines()
    debug = screen.debug_window.rendered_lines()
    notice = run_widgets.LOG_TRIM_NOTICE
    assert log[0] == notice and debug[0] == notice, (log[:2], debug[:2])
    actions = session.store.actions(session.run_uuid)
    for pane, min_level in ((screen.log_pane, "INFO"), (screen.debug_window, None)):
        _assert_store_window(pane, session.store, session.run_uuid, min_level,
                             screen._log_group_rows(), actions)
    window_rows = run_widgets.LOG_PANE_LIMIT
    assert log[-1].endswith("posttest teardown"), log[-3:]
    assert _count(log, "burst line") <= window_rows - 1 and any(line.endswith("burst line 200") for line in log), log[-3:]
    assert not any(line.endswith(" burst line 1") for line in log), "a line older than the window was drawn"
    assert PRE_HEADING in log and _line_after(log, PRE_HEADING) == run_widgets._dropped_note(1), log[:4]
    _settle(qapp, session)


def test_a_steady_stream_refreshes_while_the_step_is_still_logging(qapp, tmp_path, fake_pdf):
    """Packet 40a fix 2, item 3: a line every 30 ms for about a second must reach the pane mid-stream.

    The old single-shot timer was restarted by every notification, so a steady stream never refreshed
    until it stopped. Throttled, the pane shows the lines while the step is still running, several
    times over."""
    env = write_ate_env(tmp_path)
    name = add_steady_log_schedule(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = ThreePaneRunScreen(session)
    fill_operator_fields(screen)
    screen.show()
    qapp.processEvents()
    _pick(screen, name)
    screen.start_button.click()

    pane = screen.log_pane
    seen: list[int] = []
    deadline = time.monotonic() + 20.0
    while session.state != SessionState.FINISHED and time.monotonic() < deadline:
        qapp.processEvents()
        if _step_not_finished(session):
            count = _count(pane.rendered_lines(), "steady line")
            if not seen or seen[-1] != count:
                seen.append(count)
        time.sleep(0.005)
    assert session.state == SessionState.FINISHED, "the stream never finished"
    mid_stream = [count for count in seen if count > 0]
    assert len(mid_stream) >= 3, f"the pane drew the stream {len(mid_stream)} times while it was still running: {seen}"
    _wait(qapp, lambda: _count(pane.rendered_lines(), "steady line") == 33)
    _settle(qapp, session)


def test_a_silent_step_says_no_output_once_it_starts_with_no_log_rows(qapp, tmp_path, fake_pdf, monkeypatch):
    """Packet 40a fix 2, item 5: the notes follow the run even when no log row arrives.

    Before the Pre-test ends the log is empty and every scheduled step is listed "Not run yet." under
    its heading (the empty-log pane used to show no headings at all). When the silent step starts its
    note becomes "No output." with no line logged; the step after it still says "Not run yet."."""
    env = write_ate_env(tmp_path)
    name = add_quiet_latched_schedule(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = ThreePaneRunScreen(session)
    fill_operator_fields(screen)
    screen.show()
    qapp.processEvents()
    _pick(screen, name)
    screen.start_button.click()

    pane = screen.log_pane
    _wait(qapp, lambda: _count(pane.rendered_lines(), "Not run yet.") == 2)
    empty = pane.rendered_lines()
    assert session.store.log_lines(session.run_uuid) == [], "the silent run logged a line"
    assert _line_after(empty, "── Step 1: quiet ──") == "Not run yet.", empty
    assert _line_after(empty, "── Step 2: after ──") == "Not run yet.", empty

    monkeypatch.setenv("PRE_LATCH_OPEN", "1")
    _wait(qapp, lambda: _line_after(pane.rendered_lines(), "── Step 1: quiet ──") == "No output.")
    started = pane.rendered_lines()
    assert session.store.log_lines(session.run_uuid) == [], "the silent step logged a line"
    assert _line_after(started, "── Step 2: after ──") == "Not run yet.", started

    monkeypatch.setenv("ATE_LATCH_OPEN", "1")
    assert _wait(qapp, lambda: session.state == SessionState.FINISHED)
    assert _line_after(pane.rendered_lines(), "── Step 2: after ──") == "No output.", pane.rendered_lines()
    _settle(qapp, session)


def test_a_second_run_on_the_screen_shows_no_post_test_group_it_never_had(qapp, tmp_path, fake_pdf, monkeypatch):
    """Packet 40a fix 2, item 5: a run with no Actions after a run with Actions draws no Pre/Post groups.

    The first run ends with its Post-test Action; the second run has none. Its pane must not carry the
    first run's phase into an empty `── Post-test ──` (or Pre-test) group."""
    monkeypatch.setenv("PRE_LATCH_OPEN", "1")
    monkeypatch.setenv("POST_LATCH_OPEN", "1")
    env = write_ate_env(tmp_path)
    first = add_phased_latch_schedule(tmp_path)
    second = add_plain_log_schedule(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = ThreePaneRunScreen(session)
    fill_operator_fields(screen)
    screen.show()
    qapp.processEvents()
    _pick(screen, first)
    screen.start_button.click()
    assert _wait(qapp, lambda: session.state == SessionState.FINISHED)
    assert POST_HEADING in screen.log_pane.rendered_lines(), "the first run had its Post-test group"
    first_run = session.run_uuid

    _pick(screen, second)
    screen.start_button.click()
    assert _wait(qapp, lambda: session.run_uuid not in (None, first_run))
    assert _wait(qapp, lambda: session.state == SessionState.FINISHED)
    lines = screen.log_pane.rendered_lines()
    assert "plain line" in "\n".join(lines), lines
    assert POST_HEADING not in lines and PRE_HEADING not in lines, lines
    _settle(qapp, session)


@pytest.mark.parametrize("anchor_seq", [5, 25])
@pytest.mark.parametrize("incremental", [False, True])
def test_batch_trim_preserves_the_first_visible_log_row(qapp, monkeypatch, anchor_seq, incremental):
    monkeypatch.setattr(run_widgets, "LOG_PANE_LIMIT", 50)
    monkeypatch.setattr(run_widgets, "LOG_PANE_SLACK", 10)
    pane = LogPane("")
    pane.resize(800, 220)
    pane.show()
    nodes = [{"node_seq": 0, "kind": "step", "step_number": "1.", "step_name": "stream"}]
    def row(seq):
        # Repeated text makes row identity (seq), rather than text matching, essential.
        return {"seq": seq, "level": "INFO", "source": "schedule",
                "text": "repeated" if seq % 2 else f"row {seq}", "node_seq": 0}
    if incremental:
        for seq in range(50):
            pane.append_rows([row(seq)], nodes, [])
    else:
        pane.add_lines([row(seq) for seq in range(50)], nodes, [])
    qapp.processEvents()
    text = pane.findChild(QPlainTextEdit)
    text.verticalScrollBar().setValue(anchor_seq + 1)  # heading, then seq 0
    qapp.processEvents()
    before = text.firstVisibleBlock().text()
    assert text.firstVisibleBlock().blockNumber() == anchor_seq + 1
    pane.append_rows([row(50)], nodes, [])
    qapp.processEvents()
    if anchor_seq >= 6:
        assert text.firstVisibleBlock().text() == before
        assert text.firstVisibleBlock().blockNumber() == anchor_seq - 6 + 3
    else:
        assert text.firstVisibleBlock().blockNumber() == 3
    pane.close()


@pytest.mark.parametrize("debug", [False, True], ids=["log", "debug"])
@pytest.mark.parametrize("anchor", ["heading", "note", "trimmed_row", "empty_group", "notice", "bottom"])
def test_batch_trim_preserves_every_visible_block(qapp, monkeypatch, debug, anchor):
    monkeypatch.setattr(run_widgets, "LOG_PANE_LIMIT", 50)
    monkeypatch.setattr(run_widgets, "LOG_PANE_SLACK", 10)
    pane = run_widgets.DebugWindow() if debug else LogPane("")
    pane.resize(800, 220)
    pane.show()
    nodes = [{"node_seq": i, "kind": "step", "step_number": f"{i + 1}.",
              "step_name": f"group {i}"} for i in range(2)]

    def row(seq):
        return {"seq": seq, "level": "INFO", "source": "schedule",
                "text": f"row {seq}", "node_seq": 0 if seq < 15 else 1}

    # A note/notice already exists before the next overflow.
    initial = range(11, 61) if anchor in ("note", "notice", "empty_group") else range(50)
    pane.add_lines([row(seq) for seq in initial], nodes, [],
                   dropped={("step", 0): 11} if initial.start else None)
    qapp.processEvents()
    text = pane.findChild(QPlainTextEdit)
    lines = text.toPlainText().splitlines()
    expected = {
        "heading": "── Step 2: group 1 ──",
        "note": "… 11 earlier lines not shown",
        "notice": run_widgets.LOG_TRIM_NOTICE,
        "trimmed_row": next((line for line in lines if line.endswith("row 5")), ""),
        "empty_group": next(line for line in lines if line.endswith("row 11")) if initial.start else "",
    }.get(anchor)
    bar = text.verticalScrollBar()
    bar.setValue(bar.maximum() if anchor == "bottom" else lines.index(expected))
    qapp.processEvents()
    if anchor != "bottom":
        assert text.firstVisibleBlock().text() == expected
    pane.append_rows([row(initial.stop)], nodes, [])
    qapp.processEvents()
    if anchor == "bottom":
        assert bar.value() == bar.maximum()
    elif anchor == "trimmed_row":
        assert text.firstVisibleBlock().text().endswith("row 6")
    elif anchor == "empty_group":
        assert text.firstVisibleBlock().text() == "── Step 1: group 0 ──"
    elif anchor == "note":
        assert text.firstVisibleBlock().text() == "… 15 earlier lines not shown"
    else:
        assert text.firstVisibleBlock().text() == expected
    pane.close()


def test_a_trimmed_step_group_keeps_its_heading_and_count(monkeypatch):
    monkeypatch.setattr(run_widgets, "LOG_PANE_LIMIT", 20)
    nodes = [{"node_seq": i, "kind": "step", "step_number": f"{i + 1}.",
              "step_name": f"group {i}"} for i in range(2)]
    rows = [{"seq": seq, "level": "INFO", "source": "schedule",
             "text": f"row {seq}", "node_seq": 0 if seq < 10 else 1}
            for seq in range(40)]
    pane = LogPane("")
    for row in rows:
        pane.append_rows([row], nodes, [])
    lines = pane.rendered_lines()
    heading = "── Step 1: group 0 ──"
    assert heading in lines
    assert _line_after(lines, heading) == "… 10 earlier lines not shown"
    assert "No output." not in lines and "Not run yet." not in lines
    assert not any(line.endswith(f"row {seq}") for seq in range(10) for line in lines)
    pane.close()


def test_patching_only_the_limit_keeps_a_tenth_as_trim_slack(monkeypatch):
    monkeypatch.setattr(run_widgets, "LOG_PANE_LIMIT", 50)
    pane = LogPane("")
    nodes = [{"node_seq": 0, "kind": "step", "step_number": "1.", "step_name": "work"}]
    rows = [{"seq": seq, "level": "INFO", "source": "schedule",
             "text": f"row {seq}", "node_seq": 0} for seq in range(51)]
    pane.add_lines(rows[:50], nodes, [])
    pane.append_rows(rows[50:], nodes, [])
    assert pane.held_rows() == 45
    assert pane._rows[0]["seq"] == 6
    pane.close()
