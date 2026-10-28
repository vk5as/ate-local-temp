"""Packet 14d: the Run status bar — phase with Pre-/Post-test progress, total run time and
time in the current action or step.

The bar lives in :class:`~ate.ui.staged_run_screen.RunView` and is refreshed by the screen's
one-second QTimer and by the engine's ``action_started`` / ``step_started`` signals relayed
through the session. The screen's clock is injectable so these tests never sleep: the clock
below advances one second on every call, and a timer tick is simulated by emitting the
QTimer's ``timeout`` directly.
"""

import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget

from ate.run_session.headless import HeadlessRunSession
from ate.ui.staged_run_screen import StagedRunScreen

from ._fixtures import write_ate_env
from .test_staged_run_screen import _find_item, _select_and_start, _wait_finished

# A schedule with Pre-test and Post-test Actions around two steps, the first of which blocks
# on an INTEGER input. The block gives a test a guaranteed window while the run is still up
# (same trick as the packet 13a abort fixture): pump the queued signals, then supply the
# input so the run finishes cleanly.
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


def _add_blocking_schedule(env: dict[str, Path]) -> None:
    """Drop the Pre/post-blocking schedule into the env's Test Definition and its release manifest."""
    import json

    from ._fixtures import _sha256

    path = env["defs_dir"] / "pre_post_blocking.tslua"
    path.write_text(PRE_POST_BLOCKING, encoding="utf-8")
    atedef_path = env["defs_dir"] / "demo.atedef"
    atedef = json.loads(atedef_path.read_text(encoding="utf-8"))
    atedef["files"].append({"path": "pre_post_blocking.tslua", "sha256": _sha256(path)})
    atedef_path.write_text(json.dumps(atedef), encoding="utf-8")


def _status_labels(screen: StagedRunScreen) -> tuple[QLabel, QLabel, QLabel]:
    """Fetch the bar's three labels, asserting the bar is wired as specified."""
    bar = screen.run_view.status_bar
    assert isinstance(bar, QWidget)
    assert isinstance(bar.layout(), QHBoxLayout)
    return screen.run_view.status_phase, screen.run_view.status_total, screen.run_view.status_current


def test_status_bar_shows_phase_pretest_and_posttest_progress(qapp, tmp_path):
    """A run's status bar counts its Pre-test and Post-test Actions as they start."""
    session, screen = _screen(tmp_path, SteppingClock())
    phases: list[tuple[str, int, int]] = []
    snapshots: list[str] = []
    session.action_started.connect(lambda phase, done, total: (phases.append((phase, done, total)), snapshots.append(screen.run_view.status_phase.text())))
    session.step_started.connect(lambda node_seq: snapshots.append(screen.run_view.status_phase.text()))

    _start(qapp, screen, "Pre post pass")
    assert _wait_finished(qapp, session)
    qapp.processEvents()

    # The engine announces every phase in run order: one Pre-test Action, the step, one Post-test.
    assert phases == [("pre", 1, 1), ("post", 1, 1)]
    assert snapshots == ["Pre-test 1/1", "Running", "Post-test 1/1"]

    phase, _, _ = _status_labels(screen)
    assert phase.text() == "Finished"


def test_status_bar_total_time_advances(qapp, tmp_path):
    """The total run time ticks forward with the one-second timer while the run is up."""
    env = write_ate_env(tmp_path)
    _add_blocking_schedule(env)
    clock = SteppingClock()
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session, clock=clock)
    screen._refresh_schedule_list()  # the picker was populated before the extra schedule was written

    _start(qapp, screen, "Pre post blocking")
    # Answer the blocked step's input the moment it is asked for (packet 10's seam).
    session.input_asked.connect(lambda node_seq, attempt_no, index: screen.engine.operator_input.supply_input(index - 1, 7))
    # Pump until the bar has been refreshed for the run.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and "Run " not in screen.run_view.status_total.text():
        qapp.processEvents()

    _, total, _ = _status_labels(screen)
    before = _secs(total.text())
    for _ in range(3):
        screen._timer.timeout.emit()  # a QTimer tick, delivered without waiting for real seconds
        qapp.processEvents()
    after = _secs(total.text())
    assert after > before
    assert screen._timer.isActive()

    assert _wait_finished(qapp, session)


def test_current_item_timer_resets_on_each_action_and_step(qapp, tmp_path):
    """Every new action or step restarts the 'time in the current item' counter."""
    env = write_ate_env(tmp_path)
    _add_blocking_schedule(env)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session, clock=SteppingClock())
    screen._refresh_schedule_list()  # the picker was populated before the extra schedule was written
    snapshots: list[tuple[int, int]] = []
    session.action_started.connect(lambda phase, done, total: snapshots.append((_secs(screen.run_view.status_current.text()), _secs(screen.run_view.status_total.text()))))
    session.step_started.connect(lambda node_seq: snapshots.append((_secs(screen.run_view.status_current.text()), _secs(screen.run_view.status_total.text()))))

    _start(qapp, screen, "Pre post blocking")
    screen.engine.operator_input  # the run's engine is up; answer its input when asked
    session.input_asked.connect(lambda node_seq, attempt_no, index: screen.engine.operator_input.supply_input(index - 1, 7))
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

    phase, total, current = _status_labels(screen)
    frozen = (phase.text(), total.text(), current.text())
    assert frozen[0] == "Finished"
    assert _secs(total.text()) > 0
    assert not screen._timer.isActive()

    # If the timer had not stopped, real seconds would fire it and the injected clock would
    # advance the labels. Pump just over a timer interval and check nothing moved.
    deadline = time.monotonic() + 1.1
    while time.monotonic() < deadline:
        qapp.processEvents()
    assert (phase.text(), total.text(), current.text()) == frozen
