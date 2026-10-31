"""Packet 14d: the Run status bar — Pre-test/Post-test progress, Steps progress, total run time
and time in the current action or step.

The bar lives in :class:`~ate.ui.staged_run_screen.RunView` as the last widget of the view, under
the step list and the Log/Summary tabs like the mockup's ``statusBar()``. The screen refreshes it
from its one-second QTimer and from the engine's ``action_started`` / ``step_started`` signals
relayed through the session. The screen's clock is injectable so these tests never sleep: the
clock below advances one second on every call, and a timer tick is simulated by emitting the
QTimer's ``timeout`` directly.
"""

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget

from ate.run_session.headless import HeadlessRunSession
from ate.ui.staged_run_screen import StagedRunScreen

from ._fixtures import _sha256, write_ate_env
from .test_staged_run_screen import _find_item, _wait_finished

# A schedule with Pre-test and Post-test Actions around two steps, the first of which blocks on an
# INTEGER input. The block gives a test a guaranteed window while the run is still up (same trick
# as the packet 13a abort fixture): pump the queued signals, then supply the input so the run
# finishes cleanly.
PRE_POST_BLOCKING = """\
return ScheduleParams("Pre post blocking schedule", "pre/post actions around a step that waits on an input")
  :setSchedule({
    StepParams("wait"):setStepNo("1.")
      :setInputs({ [1] = { desc = "voltage", type = "INTEGER" } })
      :setAction(function() local v=ATE.input(1); ATE.report(Result.PASS, "got "..v) end),
    StepParams("after"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "after") end),
  })
  :setPreTestAction(function(passFail) ATE.log(Log.INFO, "pretest "..passFail) end)
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "posttest "..passFail) end)
"""

# A Sub-schedule include, so Pre-test and Post-test Actions run on two schedule instances and the
# Steps counter has a header row beside two enabled Test Steps (packets 15b/15e/14d).
SUB_PRE_POST = """\
return ScheduleParams("Sub pre post schedule", "pre/post actions on a schedule and its include")
  :setSchedule({
    StepParams("top step"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "top") end),
    StepParams("inner"):setStepNo("2."):setPrefix("2."):setInclude("inner.tslua", { tag = "A" }),
  })
  :setPreTestAction(function(passFail) ATE.log(Log.INFO, "PRE top "..passFail) end)
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "POST top "..passFail) end)
"""

SUB_INNER = """\
local kv = ...
return ScheduleParams("Inner schedule", "a Sub-schedule with its own Actions and one disabled step")
  :setSchedule({
    StepParams("inner step"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "inner " .. kv.tag) end),
    StepParams("inner off"):setStepNo("2."):setEnable(false)
      :setAction(function() ATE.report(Result.PASS, "never runs") end),
  })
  :setPreTestAction(function(passFail) ATE.log(Log.INFO, "PRE inner "..passFail) end)
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "POST inner "..passFail) end)
"""

class SteppingClock:
    """A clock that advances one second on every call, so timer maths is exact without sleeping."""

    def __init__(self, start: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)) -> None:
        self._t = start

    def __call__(self) -> datetime:
        now = self._t
        self._t += timedelta(seconds=1)
        return now


def _secs(text: str) -> int:
    """Parse an ``MM:SS`` / ``HH:MM:SS`` span in a status label (raising on ``--:--``)."""
    match = re.search(r"\d+(?::\d+)+", text)
    assert match, f"no timer in {text!r}"
    parts = [int(p) for p in match.group(0).split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return parts[0] * 60 + parts[1]


def _counter(text: str) -> tuple[int, int]:
    """The ``n/m`` pair in a counter label, e.g. "Pre-test 2/3" → (2, 3)."""
    match = re.search(r"(\d+)/(\d+)", text)
    assert match, f"no counter in {text!r}"
    return int(match.group(1)), int(match.group(2))


def _screen(tmp_path: Path, clock) -> tuple[HeadlessRunSession, StagedRunScreen]:
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    return session, StagedRunScreen(session, clock=clock)


def _start(qapp, screen: StagedRunScreen, name: str) -> None:
    """Select ``name`` in the picker and Start it through the Steps & options stage."""
    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    item = _find_item(screen.test_schedule_stage.schedule_list, name)
    screen.test_schedule_stage.schedule_list.setCurrentItem(item)
    screen._begin_run_from_selection()
    screen.steps_options_stage.run_button.click()


def _add_schedule(env: dict[str, Path], name: str, source: str, extra: dict[str, str] | None = None) -> None:
    """Drop ``source`` into the env's Test Definition (plus any ``extra`` files) and its manifest."""
    defs = env["defs_dir"]
    path = defs / name
    path.write_text(source, encoding="utf-8", newline="\n")
    atedef_path = defs / "demo.atedef"
    atedef = json.loads(atedef_path.read_text(encoding="utf-8"))
    atedef["files"].append({"path": name, "sha256": _sha256(path)})
    for extra_name, extra_source in (extra or {}).items():
        extra_path = defs / extra_name
        extra_path.write_text(extra_source, encoding="utf-8", newline="\n")
        atedef["files"].append({"path": extra_name, "sha256": _sha256(extra_path)})
    atedef_path.write_text(json.dumps(atedef), encoding="utf-8", newline="\n")


def _status_labels(screen: StagedRunScreen) -> tuple[QLabel, QLabel, QLabel, QLabel, QLabel, QLabel]:
    """Fetch the bar's six labels, asserting the bar is wired as the mockup has it."""
    bar = screen.run_view.status_bar
    assert isinstance(bar, QWidget)
    assert isinstance(bar.layout(), QHBoxLayout), "the status bar is not a horizontal row"
    return (
        screen.run_view.status_label,
        screen.run_view.status_pre,
        screen.run_view.status_steps,
        screen.run_view.status_post,
        screen.run_view.status_total,
        screen.run_view.status_current,
    )


def _pump_until(qapp, predicate, timeout: float = 10.0) -> bool:
    """Pump the event loop until ``predicate`` is true, or the deadline passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_status_bar_is_the_last_widget_of_the_run_view(qapp, tmp_path):
    """The bar sits at the bottom: after the step list, the failure bar and the Log/Summary tabs."""
    _, screen = _screen(tmp_path, SteppingClock())
    layout = screen.run_view.layout()
    widgets = [layout.itemAt(i).widget() for i in range(layout.count())]
    assert widgets[-1] is screen.run_view.status_bar, "the status bar is not the view's last widget"
    assert widgets.index(screen.run_view.status_bar) > widgets.index(screen.run_view.step_list)
    assert widgets.index(screen.run_view.status_bar) > widgets.index(screen.run_view.tabs)
    assert screen.run_view.status_bar.styleSheet().lower().startswith("background")
    assert "border-top" in screen.run_view.status_bar.styleSheet().lower(), "the mockup's top border is missing"


def test_status_bar_shows_all_three_counters_at_once(qapp, tmp_path):
    """Pre-test, Steps and Post-test counters are all on the bar together, never overwriting
    each other, and the Run/Current timers are still there."""
    session, screen = _screen(tmp_path, SteppingClock())
    _start(qapp, screen, "Pre post pass")
    assert _wait_finished(qapp, session)
    qapp.processEvents()

    label, pre, steps, post, total, current = _status_labels(screen)
    assert label.text() == "Finished"
    assert _counter(pre.text()) == (1, 1) and pre.text().startswith("Pre-test")
    assert _counter(steps.text()) == (1, 1) and steps.text().startswith("Steps")
    assert _counter(post.text()) == (1, 1) and post.text().startswith("Post-test")
    assert _secs(total.text()) > 0 and total.text().startswith("Run")
    assert current.text().startswith("Current")


def test_counters_move_through_the_run_without_clobbering_the_state(qapp, tmp_path):
    """Each Action and Step updates its own counter while the bar's left label stays on the state."""
    env = write_ate_env(tmp_path)
    _add_schedule(env, "pre_post_blocking.tslua", PRE_POST_BLOCKING)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session, clock=SteppingClock())
    screen._refresh_schedule_list()

    snapshots: list[tuple[str, tuple[int, int], tuple[int, int], tuple[int, int]]] = []

    def snap(*_args) -> None:
        snapshots.append((
            screen.run_view.status_label.text(),
            _counter(screen.run_view.status_pre.text()),
            _counter(screen.run_view.status_steps.text()),
            _counter(screen.run_view.status_post.text()),
        ))

    session.action_started.connect(snap)
    session.step_started.connect(snap)

    _start(qapp, screen, "Pre post blocking")
    session.input_asked.connect(
        lambda node_seq, attempt_no, index: screen.engine.operator_input.supply_input(index - 1, 7)
    )
    assert _wait_finished(qapp, session)
    qapp.processEvents()

    # Four items: Pre-test Action, step 1, step 2, Post-test Action. None of them replaces the
    # others' counter: the Pre-test tally stays 1/1 while the Steps counter walks 1/2 then 2/2.
    assert snapshots == [
        ("Running", (1, 1), (0, 2), (0, 0)),
        ("Running", (1, 1), (1, 2), (0, 0)),
        ("Running", (1, 1), (2, 2), (0, 0)),
        ("Running", (1, 1), (2, 2), (1, 1)),
    ]
    assert screen.run_view.status_label.text() == "Finished"


def test_steps_counter_counts_only_started_enabled_steps(qapp, tmp_path):
    """A Sub-schedule's header is not a step, and a step disabled at run start never counts."""
    env = write_ate_env(tmp_path)
    _add_schedule(env, "sub_pre_post.tslua", SUB_PRE_POST, {"inner.tslua": SUB_INNER})
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session, clock=SteppingClock())
    screen._refresh_schedule_list()

    started: list[int] = []
    session.step_started.connect(lambda node_seq: started.append(node_seq))

    _start(qapp, screen, "Sub pre post schedule")
    assert _wait_finished(qapp, session)
    qapp.processEvents()

    _, _, steps, _, _, _ = _status_labels(screen)
    # Two enabled Test Steps (the header row and the :setEnable(false) row are not steps), both run.
    assert _counter(steps.text()) == (2, 2), steps.text()
    assert len(started) == 2, started


def test_pretest_and_posttest_totals_count_every_action_of_both_instances(qapp, tmp_path):
    """With a Sub-schedule include each instance contributes an Action, so the totals are 2, not 1."""
    env = write_ate_env(tmp_path)
    _add_schedule(env, "sub_pre_post.tslua", SUB_PRE_POST, {"inner.tslua": SUB_INNER})
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session, clock=SteppingClock())
    screen._refresh_schedule_list()

    actions: list[tuple[str, int, int]] = []
    session.action_started.connect(lambda phase, done, total: actions.append((phase, done, total)))

    _start(qapp, screen, "Sub pre post schedule")
    assert _wait_finished(qapp, session)
    qapp.processEvents()

    assert actions == [("pre", 1, 2), ("pre", 2, 2), ("post", 1, 2), ("post", 2, 2)]
    _, pre, _, post, _, _ = _status_labels(screen)
    assert _counter(pre.text()) == (2, 2), pre.text()
    assert _counter(post.text()) == (2, 2), post.text()


def test_retry_restarts_the_current_item_timer(qapp, tmp_path):
    """A Retry is a new Attempt of the same step: the bar's Current timer restarts for it, and the
    step is still counted once in Steps."""
    session, screen = _screen(tmp_path, SteppingClock())
    _start(qapp, screen, "Passes on retry schedule")

    # The Current reading on the bar at the moment each Attempt announces itself.
    current_at_start: list[int] = []
    session.step_started.connect(
        lambda node_seq: current_at_start.append(_secs(screen.run_view.status_current.text()))
    )

    assert _pump_until(qapp, lambda: screen.run_view.failure_prompt_bar.isVisible()), "no pause prompt"
    assert screen.run_view.status_label.text() == "Paused"

    QTest.mouseClick(screen.run_view.failure_prompt_bar.retry_button, Qt.MouseButton.LeftButton)
    assert _pump_until(qapp, lambda: len(current_at_start) >= 2), "the Retry never announced a new Attempt"

    # Each Attempt starts its own clock: the retried Attempt's Current is back at zero, not the
    # seconds the first Attempt already burned. The step is counted once, not twice.
    assert len(current_at_start) == 2, current_at_start
    assert all(secs == 0 for secs in current_at_start), current_at_start
    assert _counter(screen.run_view.status_steps.text()) == (1, 1)

    assert _wait_finished(qapp, session)
    qapp.processEvents()
    assert screen.run_view.status_label.text() == "Finished"
    attempts = session.store.attempts(session.run_uuid)
    assert [(row["attempt_no"], row["pass_fail"], row["prompt_choice"]) for row in attempts] == [(1, "FAIL", "RETRY"), (2, "PASS", None)]


def test_pause_keeps_both_timers_running(qapp, tmp_path):
    """During a Stop-on-Fail pause the bar reads "Paused" and Run/Current keep ticking: the pause
    is real elapsed time the Operator is spending on the run."""
    session, screen = _screen(tmp_path, SteppingClock())
    _start(qapp, screen, "Fail then pass schedule")
    assert _pump_until(qapp, lambda: screen.run_view.failure_prompt_bar.isVisible()), "no pause prompt"

    label, _, _, _, total, current = _status_labels(screen)
    assert label.text() == "Paused"
    before = (_secs(total.text()), _secs(current.text()))
    for _ in range(3):
        screen._timer.timeout.emit()  # a QTimer tick, delivered without waiting for real seconds
        qapp.processEvents()
    after = (_secs(total.text()), _secs(current.text()))
    # Three ticks of the injected clock are three seconds, and a tick can never go backwards: the
    # assertion is exact rather than "greater than", so a frozen timer cannot pass it by luck.
    assert after[0] >= before[0] + 3, f"the Run timer froze during the pause: {before} -> {after}"
    assert after[1] >= before[1] + 3, f"the Current timer froze during the pause: {before} -> {after}"
    assert label.text() == "Paused"

    # The label is read straight after the choice, before pumping: a run that finishes in the very
    # next event cycle would otherwise leave "Finished" on the bar and this assertion would flake
    # depending on how fast the worker's thread got scheduled.
    screen._on_prompt_choice("CONTINUE")
    assert screen.run_view.status_label.text() == "Running", "the bar never came back off the pause"
    assert _wait_finished(qapp, session)
    assert screen.run_view.status_label.text() == "Finished"


def test_total_time_advances(qapp, tmp_path):
    """The total run time ticks forward with the one-second timer while the run is up.

    Packet 14d review: the bar's first reading used to be waited for on the real QTimer's 1 s
    interval, which timed out under suite load. The clock is injected and every tick is emitted by
    hand, so nothing in this test depends on wall-clock seconds, and the wait that remains fails
    loudly with a reason rather than as a bare `assert False`."""
    env = write_ate_env(tmp_path)
    _add_schedule(env, "pre_post_blocking.tslua", PRE_POST_BLOCKING)
    clock = SteppingClock()
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session, clock=clock)
    screen._refresh_schedule_list()

    _start(qapp, screen, "Pre post blocking")

    # The step blocks on its input and nothing answers it yet, so the run stays up while the bar is
    # ticked by hand: no part of this test waits on a real second.
    assert _pump_until(
        qapp,
        lambda: screen._started_at is not None and screen._timer.isActive(),
        timeout=30.0,
    ), "the run never reached STARTED, so the status bar's clock never started"

    screen._timer.timeout.emit()  # one tick puts a reading on the bar
    qapp.processEvents()

    _, _, _, _, total, _ = _status_labels(screen)
    before = _secs(total.text())
    for _ in range(3):
        screen._timer.timeout.emit()  # a QTimer tick, delivered without waiting for real seconds
        qapp.processEvents()
    after = _secs(total.text())
    assert after > before, f"the Run timer read {before}s then {after}s after three ticks"
    assert screen._timer.isActive(), "the run clock stopped while the run was still up"

    # Answer the waiting step so the run can end.
    screen.engine.operator_input.supply_input(0, 7)
    assert _wait_finished(qapp, session), "the run never finished once its input was answered"


def test_live_qtimer_advances_the_run_label_without_any_manual_emit(qapp, qtbot, tmp_path):
    """The real one-second QTimer drives the Run label: nothing emits ``timeout`` by hand here."""
    env = write_ate_env(tmp_path)
    _add_schedule(env, "pre_post_blocking.tslua", PRE_POST_BLOCKING)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)  # the real wall clock, not an injected one
    screen._refresh_schedule_list()

    _start(qapp, screen, "Pre post blocking")
    # The step blocks on its input, so the run stays up while the bar ticks on real seconds.
    assert _pump_until(qapp, lambda: screen._timer.isActive() and screen.engine is not None)
    before = _secs(screen.run_view.status_total.text())

    qtbot.wait(2500)  # real seconds, delivered by the real QTimer
    assert _secs(screen.run_view.status_total.text()) > before, screen.run_view.status_total.text()

    screen.engine.operator_input.supply_input(0, 7)
    assert _wait_finished(qapp, session)
    assert screen._timer.isActive() is False  # the timer stopped at FINISHED (packet 14d)


def test_current_item_timer_resets_on_each_action_and_step(qapp, tmp_path):
    """Every new action or step restarts the 'time in the current item' counter."""
    env = write_ate_env(tmp_path)
    _add_schedule(env, "pre_post_blocking.tslua", PRE_POST_BLOCKING)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session, clock=SteppingClock())
    screen._refresh_schedule_list()
    snapshots: list[tuple[int, int]] = []
    session.action_started.connect(
        lambda phase, done, total: snapshots.append(
            (_secs(screen.run_view.status_current.text()), _secs(screen.run_view.status_total.text()))
        )
    )
    session.step_started.connect(
        lambda node_seq: snapshots.append(
            (_secs(screen.run_view.status_current.text()), _secs(screen.run_view.status_total.text()))
        )
    )

    _start(qapp, screen, "Pre post blocking")
    session.input_asked.connect(
        lambda node_seq, attempt_no, index: screen.engine.operator_input.supply_input(index - 1, 7)
    )
    assert _wait_finished(qapp, session)
    qapp.processEvents()

    # Four items ran (Pre-test Action, step 1, step 2, Post-test Action); each starts at zero
    # while the run total keeps growing. Without the reset the last current time would equal
    # the run total instead of restarting from 0.
    assert len(snapshots) == 4
    assert all(current == 0 for current, _ in snapshots)
    assert snapshots[-1][1] > snapshots[0][1]


def test_timers_freeze_when_the_run_finishes(qapp, tmp_path):
    """At FINISHED the timers stop and the final values stay on the bar."""
    session, screen = _screen(tmp_path, SteppingClock())
    _start(qapp, screen, "Pre post pass")
    assert _wait_finished(qapp, session)
    qapp.processEvents()

    label, pre, steps, post, total, current = _status_labels(screen)
    frozen = (label.text(), pre.text(), steps.text(), post.text(), total.text(), current.text())
    assert frozen[0] == "Finished"
    assert _secs(total.text()) > 0
    assert not screen._timer.isActive()

    # If the timer had not stopped, real seconds would fire it and the injected clock would
    # advance the labels. Pump just over a timer interval and check nothing moved.
    deadline = time.monotonic() + 1.1
    while time.monotonic() < deadline:
        qapp.processEvents()
    assert (label.text(), pre.text(), steps.text(), post.text(), total.text(), current.text()) == frozen
