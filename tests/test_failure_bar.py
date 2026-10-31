"""Packet 12d: the Stop-on-Fail prompt bar in the staged Run view.

Most tests drive a real headless run of the "Fail then pass schedule" (first step fails on
every attempt, Stop on Fail armed) through the staged screen to a failure prompt; the
retry-passes test uses a schedule whose Attempt 1 fails and Attempt 2 passes. The
session's ``failure_prompt`` signal is latched without being answered, so the run stays
paused while a test clicks one of the bar's buttons: Continue keeps the run going, Retry
re-runs the step as Attempt 2, and Abort ends the run STOPPED_ON_FAIL. While the prompt is
pending the bar must name the failing step and highlight its row with the header on
WAITING, and all of that must clear once the run resumes — so every test fails if a button
is left unwired.
"""

import sqlite3
import time

from PyQt6.QtGui import QColor

from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.ui.staged_run_screen import FailurePromptBar, StagedRunScreen

from ._fixtures import WRITES_PDF, fake_pdf_helper, write_ate_env

SCHEDULE = "Fail then pass schedule"
RETRY_SCHEDULE = "Passes on retry schedule"


def _select_and_start(qapp, screen, schedule_name: str) -> None:
    """Select ``schedule_name`` and start a run through the Steps & options stage."""
    item = _find_item(screen.test_schedule_stage.schedule_list, schedule_name)
    screen.test_schedule_stage.schedule_list.setCurrentItem(item)
    # Preview the selected schedule's steps, then move to the final stage.
    screen._begin_run_from_selection()
    assert screen.steps_stack.currentIndex() == 2
    # Begin the run: capture fields and start through session.start().
    screen.steps_options_stage.run_button.click()


def _find_item(list_widget, substring: str):
    """Return the row whose text contains ``substring`` (raising if none match)."""
    for i in range(list_widget.count()):
        if substring in list_widget.item(i).text():
            return list_widget.item(i)
    raise AssertionError(f"no schedule row containing {substring!r}")


def _latch_prompts(session) -> list:
    """Record every failure prompt the session emits, without answering it.

    Connected after the screen's own handler, so by the time a prompt lands here the bar is
    already showing; the run stays paused until a test clicks one of the bar's buttons."""
    prompts: list = []
    session.failure_prompt.connect(lambda node_seq, attempt_no: prompts.append((node_seq, attempt_no)))
    return prompts


def _pump_prompts(qapp, prompts: list, count: int) -> None:
    """Pump the event loop until ``count`` prompts have been latched (or the deadline)."""
    deadline = time.monotonic() + 10.0  # a time bound, not a pump count: load-independent
    while time.monotonic() < deadline:
        qapp.processEvents()
        if len(prompts) >= count:
            return
        time.sleep(0.005)
    raise AssertionError(f"only {len(prompts)} prompt(s) arrived in 10 s, wanted {count}")


def _pump_finished(qapp, session) -> None:
    """Pump the event loop until the run reaches FINISHED (or the deadline)."""
    deadline = time.monotonic() + 10.0  # a time bound, not a pump count: load-independent
    while time.monotonic() < deadline:
        qapp.processEvents()
        if session._state == SessionState.FINISHED:
            return
        time.sleep(0.005)
    raise AssertionError(f"run did not finish in 10 s; state is {session._state}")


def _pump_report_written(qapp, session) -> None:
    """Pump until the run-end PDF job reports, so no helper process outlives the test.

    FINISHED spawns the HTML-to-PDF helper in a session-owned QProcess (spec 22b); letting the test
    end while it still runs destroys the session mid-render and aborts the process (suite pattern:
    fake the helper, then wait for report_written)."""
    done: list = []
    session.report_written.connect(lambda html, pdf: done.append((html, pdf)))
    deadline = time.monotonic() + 10.0  # a time bound, not a pump count: load-independent
    while time.monotonic() < deadline:
        qapp.processEvents()
        if done:
            return
        time.sleep(0.005)
    raise AssertionError("report_written never fired within 10 s")


def _run_row(store, run_uuid: str) -> dict:
    """The run's test_run row (result/end_reason)."""
    store._conn.row_factory = sqlite3.Row  # type: ignore[attr-defined]
    return dict(store._conn.execute(
        "SELECT result, end_reason FROM test_run WHERE run_uuid=?", (run_uuid,)
    ).fetchone())


def _rows_by_step(store, run_uuid: str) -> dict:
    """The run's step-kind node rows keyed by step number."""
    store._conn.row_factory = sqlite3.Row  # type: ignore[attr-defined]
    return {
        row["step_number"]: dict(row)
        for row in store._conn.execute(
            "SELECT step_number, result, attempt_count FROM node WHERE run_uuid=? AND kind='step' "
            "ORDER BY node_seq",
            (run_uuid,),
        ).fetchall()
    }


def _attempts_for(store, run_uuid: str, node_seq: int) -> list:
    """The run's Attempt rows for one node, in attempt order."""
    store._conn.row_factory = sqlite3.Row  # type: ignore[attr-defined]
    return [
        dict(row)
        for row in store._conn.execute(
            "SELECT attempt_no, pass_fail, prompt_choice FROM attempt "
            "WHERE run_uuid=? AND node_seq=? ORDER BY attempt_no",
            (run_uuid, node_seq),
        ).fetchall()
    ]


def _highlighted_row(step_list):
    """The step-list row painted with the pause highlight (or ``None`` when clear)."""
    for i in range(step_list.count()):
        item = step_list.item(i)
        if item.background().color() == QColor("#fff0f0"):
            return item
    return None


def test_continue_button_keeps_the_run_going(qapp, tmp_path, monkeypatch):
    """Continue keeps the failed step's result and moves on: COMPLETED with result FAIL."""
    env = write_ate_env(tmp_path)
    # Fake the PDF helper so no QProcess outlives the session (suite pattern).
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", fake_pdf_helper(WRITES_PDF))
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    prompts = _latch_prompts(session)
    _select_and_start(qapp, screen, SCHEDULE)
    _pump_prompts(qapp, prompts, 1)
    assert prompts == [(0, 1)]  # paused on the first step's first attempt

    bar = screen.run_view.failure_prompt_bar
    assert bar.isVisible()
    assert bar.step_label.text() == "Step 1. FAIL"
    assert bar.message_label.text() == "wrong colour"
    # The failing step's row is highlighted while paused, and the header reads WAITING.
    assert _highlighted_row(screen.run_view.step_list) is not None
    assert "WAITING" in screen.run_view.state_header.text()

    bar.continue_button.click()
    # The bar and the pause's display state are gone the moment the choice is made, not when
    # the run finishes.
    assert not bar.isVisible()
    assert _highlighted_row(screen.run_view.step_list) is None
    assert "WAITING" not in screen.run_view.state_header.text()
    _pump_finished(qapp, session)
    _pump_report_written(qapp, session)

    store = session.store
    run = _run_row(store, session.run_uuid)
    assert run["result"] == "FAIL"  # a step failed, so the run's result stays FAIL
    assert run["end_reason"] == "COMPLETED"  # ...but the run kept going
    by_step = _rows_by_step(store, session.run_uuid)
    assert by_step["1."]["result"] == "FAIL"  # the failed step's result kept
    assert by_step["2."]["result"] == "PASS"  # moved on to the next step
    assert [(a["attempt_no"], a["pass_fail"], a["prompt_choice"]) for a in _attempts_for(store, session.run_uuid, 0)] == [
        (1, "FAIL", "CONTINUE")
    ]


def test_retry_button_reruns_the_failed_step_as_attempt_two(qapp, tmp_path, monkeypatch):
    """Retry re-runs the step's action as Attempt 2; the retried failure re-pauses."""
    env = write_ate_env(tmp_path)
    # Fake the PDF helper so no QProcess outlives the session (suite pattern).
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", fake_pdf_helper(WRITES_PDF))
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    prompts = _latch_prompts(session)
    _select_and_start(qapp, screen, SCHEDULE)
    _pump_prompts(qapp, prompts, 1)

    bar = screen.run_view.failure_prompt_bar
    assert bar.isVisible()
    assert bar.step_label.text() == "Step 1. FAIL"
    bar.retry_button.click()

    # The retried attempt fails again, so the bar reappears for Attempt 2.
    _pump_prompts(qapp, prompts, 2)
    assert prompts == [(0, 1), (0, 2)]
    assert bar.isVisible()
    assert bar.step_label.text() == "Step 1. FAIL"
    assert _highlighted_row(screen.run_view.step_list) is not None
    assert "WAITING" in screen.run_view.state_header.text()

    # Answer the re-paused prompt with Continue so the run can end.
    bar.continue_button.click()
    # The pause's display clears the moment the choice is made.
    assert not bar.isVisible()
    assert _highlighted_row(screen.run_view.step_list) is None
    _pump_finished(qapp, session)
    _pump_report_written(qapp, session)
    store = session.store
    run = _run_row(store, session.run_uuid)
    assert run["result"] == "FAIL"
    assert run["end_reason"] == "COMPLETED"
    by_step = _rows_by_step(store, session.run_uuid)
    assert by_step["1."]["result"] == "FAIL"
    assert by_step["1."]["attempt_count"] == 2  # the retry re-ran the step
    assert by_step["2."]["result"] == "PASS"
    assert [(a["attempt_no"], a["pass_fail"], a["prompt_choice"]) for a in _attempts_for(store, session.run_uuid, 0)] == [
        (1, "FAIL", "RETRY"),
        (2, "FAIL", "CONTINUE"),
    ]


def test_retry_button_passes_the_step_on_second_attempt(qapp, tmp_path, monkeypatch):
    """When Attempt 2 passes, the bar stays hidden, no second prompt arrives, and the run ends PASS.

    The schedule's step action counts its attempts in a Lua upvalue: Attempt 1 reports FAIL,
    Attempt 2 reports PASS. A retry that passes must end the pause for good — not re-pause."""
    env = write_ate_env(tmp_path)
    # Fake the PDF helper so no QProcess outlives the session (suite pattern).
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", fake_pdf_helper(WRITES_PDF))
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    prompts = _latch_prompts(session)
    _select_and_start(qapp, screen, RETRY_SCHEDULE)
    _pump_prompts(qapp, prompts, 1)
    assert prompts == [(0, 1)]

    bar = screen.run_view.failure_prompt_bar
    assert bar.isVisible()
    assert bar.step_label.text() == "Step 1. FAIL"
    assert "WAITING" in screen.run_view.state_header.text()

    bar.retry_button.click()
    _pump_finished(qapp, session)
    assert prompts == [(0, 1)]  # a second pause would mean Attempt 2 failed again
    assert not bar.isVisible()
    assert _highlighted_row(screen.run_view.step_list) is None
    _pump_report_written(qapp, session)

    store = session.store
    run = _run_row(store, session.run_uuid)
    assert run["result"] == "PASS"  # the step passed on its second attempt
    assert run["end_reason"] == "COMPLETED"
    by_step = _rows_by_step(store, session.run_uuid)
    assert by_step["1."]["result"] == "PASS"  # the step's result is its last Attempt's
    assert by_step["1."]["attempt_count"] == 2
    assert [(a["attempt_no"], a["pass_fail"], a["prompt_choice"]) for a in _attempts_for(store, session.run_uuid, 0)] == [
        (1, "FAIL", "RETRY"),
        (2, "PASS", None),
    ]


def test_abort_button_ends_the_run_stopped_on_fail(qapp, tmp_path, monkeypatch):
    """Abort keeps the failed step's result, marks the rest NOTRUN, and ends STOPPED_ON_FAIL."""
    env = write_ate_env(tmp_path)
    # Fake the PDF helper so no QProcess outlives the session (suite pattern).
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", fake_pdf_helper(WRITES_PDF))
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    prompts = _latch_prompts(session)
    _select_and_start(qapp, screen, SCHEDULE)
    _pump_prompts(qapp, prompts, 1)

    bar = screen.run_view.failure_prompt_bar
    assert bar.isVisible()
    assert bar.step_label.text() == "Step 1. FAIL"
    assert bar.message_label.text() == "wrong colour"
    assert _highlighted_row(screen.run_view.step_list) is not None
    assert "WAITING" in screen.run_view.state_header.text()

    bar.abort_button.click()
    # The bar and the pause's display state are gone the moment the choice is made.
    assert not bar.isVisible()
    assert _highlighted_row(screen.run_view.step_list) is None
    assert "WAITING" not in screen.run_view.state_header.text()
    _pump_finished(qapp, session)
    _pump_report_written(qapp, session)

    store = session.store
    run = _run_row(store, session.run_uuid)
    assert run["result"] == "FAIL"
    assert run["end_reason"] == "STOPPED_ON_FAIL"
    by_step = _rows_by_step(store, session.run_uuid)
    assert by_step["1."]["result"] == "FAIL"  # the failed step's result kept
    assert by_step["2."]["result"] == "NOTRUN"  # the remaining step never ran
    assert [(a["attempt_no"], a["pass_fail"], a["prompt_choice"]) for a in _attempts_for(store, session.run_uuid, 0)] == [
        (1, "FAIL", "ABORT")
    ]


def test_header_abort_while_prompt_pending_ends_the_run_stopped_on_fail(qapp, tmp_path, monkeypatch):
    """The header Abort button answers a pending prompt with ABORT instead of stalling the paused worker.

    request_abort() only sets a step-boundary flag, which the worker (blocked in the pause) never
    reaches — so with a prompt up, the header's Abort must take the same path as the bar's own
    Abort button: answer the pending prompt and end the run STOPPED_ON_FAIL."""
    env = write_ate_env(tmp_path)
    # Fake the PDF helper so no QProcess outlives the session (suite pattern).
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", fake_pdf_helper(WRITES_PDF))
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    prompts = _latch_prompts(session)
    _select_and_start(qapp, screen, SCHEDULE)
    _pump_prompts(qapp, prompts, 1)

    bar = screen.run_view.failure_prompt_bar
    assert bar.isVisible()
    assert "WAITING" in screen.run_view.state_header.text()

    # The header's Abort button (not the bar's) ends the paused run.
    screen.run_view.abort_button.click()
    # The bar and the pause's display state are gone the moment the choice is made.
    assert not bar.isVisible()
    assert _highlighted_row(screen.run_view.step_list) is None
    assert "WAITING" not in screen.run_view.state_header.text()
    _pump_finished(qapp, session)
    _pump_report_written(qapp, session)

    store = session.store
    run = _run_row(store, session.run_uuid)
    assert run["result"] == "FAIL"
    assert run["end_reason"] == "STOPPED_ON_FAIL"
    by_step = _rows_by_step(store, session.run_uuid)
    assert by_step["1."]["result"] == "FAIL"  # the failed step's result kept
    assert by_step["2."]["result"] == "NOTRUN"  # the remaining step never ran
    assert [(a["attempt_no"], a["pass_fail"], a["prompt_choice"]) for a in _attempts_for(store, session.run_uuid, 0)] == [
        (1, "FAIL", "ABORT")
    ]


def test_second_click_on_a_hidden_bar_does_not_answer_again(qapp, tmp_path, monkeypatch):
    """A double-click on the same bar button answers the prompt once, not twice.

    The bar hides itself on the first click; a second click while it is hidden must be ignored, so
    the session's choose() sees exactly one answer (packet 12d review)."""
    env = write_ate_env(tmp_path)
    # Fake the PDF helper so no QProcess outlives the session (suite pattern).
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", fake_pdf_helper(WRITES_PDF))
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    prompts = _latch_prompts(session)
    _select_and_start(qapp, screen, SCHEDULE)
    _pump_prompts(qapp, prompts, 1)

    bar = screen.run_view.failure_prompt_bar
    assert bar.isVisible()
    # Count how many times the prompt is actually answered.
    chosen: list = []
    real_choose = session.choose
    monkeypatch.setattr(session, "choose", lambda choice: (chosen.append(choice), real_choose(choice)))

    bar.continue_button.click()
    bar.continue_button.click()  # a rapid second click, while the bar is already hidden
    assert chosen == ["CONTINUE"]
    assert not bar.isVisible()
    _pump_finished(qapp, session)
    _pump_report_written(qapp, session)
    store = session.store
    run = _run_row(store, session.run_uuid)
    assert run["result"] == "FAIL"
    assert run["end_reason"] == "COMPLETED"
    assert [(a["attempt_no"], a["pass_fail"], a["prompt_choice"]) for a in _attempts_for(store, session.run_uuid, 0)] == [
        (1, "FAIL", "CONTINUE")
    ]


def test_bar_paints_red_with_white_labels_offscreen(qapp):
    """The bar must actually paint its red background and white labels.

    A plain QWidget subclass does not paint a stylesheet background unless it sets
    WA_StyledBackground (packet 12d review: a real screenshot shows it grey with black
    text), so render it offscreen and inspect the pixels — an empty area of the bar is
    its own background, and the label text is white."""
    bar = FailurePromptBar()
    bar.show_prompt("1.", "FAIL", "wrong colour")
    bar.resize(800, 48)
    bar.show()
    img = bar.grab().toImage()
    # A pixel in the bar's top margin is clear of every child widget.
    assert img.pixelColor(100, 3).name() == "#c62828"
    # The label text is white: scan the label zone (clear of the light-grey buttons on
    # the right) for bright pixels only white-on-red can produce.
    found_white = False
    for y in range(12, 40):
        for x in range(16, 420):
            c = img.pixelColor(x, y)
            if c.red() > 200 and c.green() > 200 and c.blue() > 200:
                found_white = True
                break
        if found_white:
            break
    assert found_white, "no white label text found in the bar"
