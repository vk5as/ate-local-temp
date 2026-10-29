"""Packet 30a: the application shell — bare ``ate`` opens the GUI.

Launch sequence (each step reuses the module that already does it):

1. read the config once (:func:`ate.config.read_config`), answering the missing-config prompt
   ("Create default config and launch" / "Abort");
2. open the per-launch application log (:func:`ate.log.open_app_log`);
3. check the database (:func:`ate.storage.store.check_database_at_launch`) — a missing, empty,
   older or newer database is refused with its message in a dialog and a non-zero exit. The GUI
   never migrates (spec 22/23), and the check comes before the session because
   :class:`~ate.storage.store.RunStore` would create the tables;
4. create the run session, which opens the launch log and the store, and close every run left
   open by a crash (spec 98) through the session's own store;
5. open the main window: the Run screen in the layout `ui.layout` names (staged, or threePane) —
   both layouts are built and bound to the session so the View menu's Layout group can switch them
   for this session without touching a run (packet 25b) — the History screen reachable from the View
   menu, and the Run view's debug window inside the Run screen.

Both dialogs are injectable (`prompt`, `alert`) so tests never block on a real
:class:`~PyQt6.QtWidgets.QMessageBox`, and `exec_=False` returns the window instead of entering
the event loop.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QActionGroup
from PyQt6.QtWidgets import QApplication, QMainWindow, QStackedWidget, QMessageBox

from ._version import __version__
from .config import Config, ConfigError, MissingConfigError, read_config
from .history.session import HistorySession
from .log import LogError, open_app_log
from .run_session.headless import HeadlessRunSession, SessionError, SessionState
from .storage.store import DbLaunchStatus, check_database_at_launch
from .ui.history_screen import HistoryScreen
from .ui.run_widgets import load_schedule_steps
from .ui.staged_run_screen import StagedRunScreen
from .ui.three_pane_run_screen import ThreePaneRunScreen


@dataclass
class LaunchOutcome:
    """What a launch produced: the exit code, the main window when it got that far, and the
    QApplication that owns them (kept referenced so a non-looping launch cannot destroy it)."""

    code: int
    window: Optional[QMainWindow] = None
    app: Optional[QApplication] = None


class MainWindow(QMainWindow):
    """The application window: the Run screen by default, History from the View menu.

    ``session`` is the run session both screens bind to; the History switch is disabled while a
    Test Run is in progress when ``ui.allowHistoryDuringRun`` is false (spec 23c).

    Both Run layouts are built at launch and both listen to the same session, so the View menu's
    Layout group only changes which screen is visible (packet 25b): a run in progress is untouched,
    and the layout that is not showing keeps mirroring the session while its controls stay inert.
    """

    def __init__(
        self,
        session: HeadlessRunSession,
        config: Config,
        *,
        run_screen: Optional[StagedRunScreen | ThreePaneRunScreen] = None,
        history_screen: Optional[HistoryScreen] = None,
        app_log=None,
        confirm_close: Optional[Callable[[str], bool]] = None,
    ) -> None:
        super().__init__()
        self.session = session
        self.config = config
        self.app_log = app_log
        # Injectable "abort the run and quit?" answer so tests never block on a modal box (30a item 10).
        self._confirm_close = confirm_close or _close_confirm_dialog
        self._run_active = False
        self._released = False
        self.setWindowTitle(f"ATE {__version__} — {config.station_name}")

        self.history_session = HistorySession(session.store, config=session.config)

        # The two layouts, both bound to this session from the start (packet 25b).
        self.staged_screen = run_screen if isinstance(run_screen, StagedRunScreen) else StagedRunScreen(session)
        self.three_pane_screen = (
            run_screen if isinstance(run_screen, ThreePaneRunScreen) else ThreePaneRunScreen(session)
        )
        self.history_screen = history_screen or HistoryScreen(self.history_session)
        self.run_screens = {"staged": self.staged_screen, "threePane": self.three_pane_screen}
        # `ui.layout` is the Station's default layout; the View menu overrides it for this session only.
        self.layout = config.ui_layout if config.ui_layout in self.run_screens else "staged"
        self.run_screen = self.run_screens[self.layout]

        self.stack = QStackedWidget()
        self.stack.addWidget(self.staged_screen)
        self.stack.addWidget(self.three_pane_screen)
        self.stack.addWidget(self.history_screen)
        self.setCentralWidget(self.stack)
        self.stack.setCurrentWidget(self.run_screen)  # stage 1: the Run screen is the launch view
        # Exactly one layout acts and the other mirrors the session (packet 25b): both are named
        # explicitly, so neither screen carries a stale default at launch or after a switch.
        for name, screen in self.run_screens.items():
            screen.set_active(name == self.layout)

        view_menu = self.menuBar().addMenu("View")
        self.view_menu = view_menu
        self.run_action = view_menu.addAction("Run")
        self.history_action = view_menu.addAction("History")
        self.run_action.triggered.connect(self.show_run)
        self.history_action.triggered.connect(self.show_history)
        self.layout_menu = view_menu.addMenu("Layout")
        self.staged_layout_action = self.layout_menu.addAction("Staged")
        self.three_pane_layout_action = self.layout_menu.addAction("Three-pane")
        self.layout_actions = {
            "staged": self.staged_layout_action,
            "threePane": self.three_pane_layout_action,
        }
        self.layout_group = QActionGroup(self)
        self.layout_group.setExclusive(True)
        for name, action in self.layout_actions.items():
            action.setCheckable(True)
            self.layout_group.addAction(action)
            action.triggered.connect(lambda *_seen, name=name: self.set_layout(name))
        if not config.ui_allow_layout_switch:
            # The Station is fixed to its configured layout (spec: `ui.allowLayoutSwitch` off).
            for action in self.layout_actions.values():
                action.setEnabled(False)
        self._mark_layout()

        session.state_changed.connect(self._on_state_changed)
        # History's "New run for this UUT" comes back to the Run screen with the fields filled in.
        self.history_screen.new_run_requested.connect(self._new_run_from_history)

    # -- layout switching ---------------------------------------------------

    def set_layout(self, layout: str) -> None:
        """Show the Run screen in ``layout``, for this session only: the config is never written.

        The run is not touched — both layouts are listening to the same session, so the new screen
        already shows the step results so far, the input waiting, a pending failure prompt, the
        counts, the status-bar timers and the log. Only the visible pane changes, and the layout
        that is no longer showing goes inert (packet 25b).
        """
        if layout not in self.run_screens or layout == self.layout:
            return
        if not self.config.ui_allow_layout_switch:
            return
        previous = self.run_screen
        previous.set_active(False)
        self.layout = layout
        self.run_screen = self.run_screens[layout]
        # Carry the setup across while the target is still inert, then make it the acting layout: that
        # is where the store reads it deferred while hidden are drawn (packet 25b).
        self._carry_over(previous, self.run_screen)
        self.run_screen.set_active(True)
        # The layout that just came to the front takes the keyboard: its waiting input, or the prompt
        # it is paused on (packet 25b). `activate` only focuses the layout that is showing, so a
        # mid-run switch has to hand the focus over here.
        self.run_screen.focus_waiting()
        self._mark_layout()
        if self.stack.currentWidget() in (previous, self.run_screen):
            self.stack.setCurrentWidget(self.run_screen)

    def _mark_layout(self) -> None:
        """Tick the Layout action that matches the screen on screen."""
        for name, action in self.layout_actions.items():
            action.setChecked(name == self.layout)

    def _carry_over(self, source, target) -> None:
        """Copy the Operator's setup from one layout to the other (packet 25b).

        Each layout holds its own widgets, so a switch that only changed the visible pane would show
        empty fields: the four setup edits, the filter, the On failure pair and the picked schedule
        are copied across, and the target's steps preview is loaded for a run not yet started. A View
        switch is not an Operator action on the run, so nothing here writes to the session: the picker
        is re-rendered with its ``_carrying_over`` flag up, which is what stops it from re-selecting
        the schedule and moving a FINISHED session back to SELECTING (packet 30b review)."""
        for name in ("operator_edit", "project_edit", "uut_type_edit", "serial_edit"):
            src = getattr(source.fields, name, None)
            dst = getattr(target.fields, name, None)
            if src is not None and dst is not None:
                dst.setText(src.text())
        target.picker.filter_edit.setText(source.picker.filter_edit.text())
        # Re-rendering against the session's selection makes the picked schedule current here too, but
        # a switch is not the Operator picking a schedule: the target must not rebuild its step table
        # from that preview (packet 25b review).
        target._carrying_over = True
        target.picker._carrying_over = True
        try:
            target.picker.render()
        finally:
            target._carrying_over = False
            target.picker._carrying_over = False
        # A run that is up, or one this session just finished, already has its rows on both layouts:
        # reloading the schedule here would blank the Result/Attempts/Actual/Expected/Message cells and
        # the log the Operator switched to look at (packet 25b review). The target redraws from the
        # store instead; only the setup preview is re-loaded, and only when there is no run to show.
        has_run_to_show = self.session.run_uuid is not None and self.session.state in (
            SessionState.STARTED, SessionState.FINISHED
        )
        if has_run_to_show:
            target.mark_store_dirty()  # drawn as soon as this layout becomes the visible one
        elif self.session.selected_schedule:
            steps = load_schedule_steps(self.session, self.session.selected_schedule)
            if target is self.staged_screen:
                target.steps_options_stage.load_steps(steps)
            else:
                target._steps = steps
                target.table.load_steps(steps)
        # The On failure pair last: loading the steps re-applies the schedule's own default.
        stop_on_fail = source.stop_on_fail_radio.isChecked()
        target.stop_on_fail_radio.setChecked(stop_on_fail)
        target.continue_all_radio.setChecked(not stop_on_fail)

    # -- navigation ---------------------------------------------------------

    def show_run(self) -> None:
        """Show the Run screen."""
        self.stack.setCurrentWidget(self.run_screen)

    def show_history(self) -> None:
        """Show the History screen, unless a run in progress makes it unavailable."""
        if not self.history_session.available():
            return
        self.stack.setCurrentWidget(self.history_screen)

    def _on_state_changed(self, state: SessionState) -> None:
        """Tell the History screen whether a run is up, and disable its switch when the config says so.

        The *screen* method is called, not the session's: the screen refreshes its "A Test Run is
        in progress" state and re-enables its widgets through it (30a review item 9)."""
        running = state == SessionState.STARTED
        self._run_active = running
        self.history_screen.set_run_in_progress(running)
        self.history_action.setEnabled(running is False or self.config.ui_allow_history_during_run)
        if state == SessionState.FINISHED:
            self.history_screen.refresh()  # the finished run should appear in the table

    def _new_run_from_history(self, prefill: dict) -> None:
        """Switch to the Run screen prefilled from History's "New run for this UUT".

        This is the same New run as the run header's button (packet 30b review): after a run the
        staged layout shows the finished run, not the setup stages, so filling the stage-1 edits
        would land on hidden widgets. The layout is put back to its setup first and the session is
        moved FINISHED → SELECTING by re-selecting the schedule; only then are the fields written,
        so the Operator lands on the visible, prefilled stage 1 (or the three-pane setup pane) with
        Start live. The Run screen reads its widgets at Start, so prefilling only the session would
        be re-applied empty by the first Start (30a review item 8): UUT Type, serial and Project go
        into the first-stage edits, and the Test Schedule picker is made current, which selects it on
        the session through the screen's own wiring."""
        self.show_run()
        self.run_screen.start_new_run()
        # Either layout carries the same fields; the staged one keeps them in its first stage.
        stage = getattr(self.run_screen, "uut_operator_stage", None) or self.run_screen.fields
        setters = {
            "uut_type": (self.session.set_uut_type, stage.uut_type_edit),
            "uut_serial": (self.session.set_uut_serial, stage.serial_edit),
            "project": (self.session.set_project, stage.project_edit),
        }
        for key, value in (prefill or {}).items():
            entry = setters.get(key)
            if entry is not None and value:
                setter, widget = entry
                setter(value)
                widget.setText(value)
        schedule = (prefill or {}).get("schedule_name")
        if schedule:
            picker = self.run_screen.picker.schedule_list
            for index in range(picker.count()):
                item = picker.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == schedule:
                    picker.setCurrentItem(item)  # _on_schedule_selected selects it on the session
                    break

    # -- closing -------------------------------------------------------------

    def closeEvent(self, event) -> None:
        """Release the session, store and application log on exit; a run in progress must be
        aborted (with confirmation) and allowed to finish recording first (30a review item 10)."""
        if self._run_active:
            if not self._confirm_close("A Test Run is in progress. Abort it and quit?"):
                event.ignore()  # the Operator chose to keep the run going: the window stays open
                return
            try:
                self.session.request_abort()
            except SessionError:
                pass  # raced with a natural finish: nothing to abort
            deadline = time.time() + 10
            while self._run_active and time.time() < deadline:
                QApplication.processEvents()
        self._release_resources()
        event.accept()

    def _release_resources(self) -> None:
        """Close the session (store included) and the launch's application log, once."""
        if self._released:
            return
        self._released = True
        self.session.close()
        if self.app_log is not None:
            try:
                self.app_log.close()
            except LogError:
                pass


def _missing_config_dialog(path: str) -> bool:
    """The Operator's missing-config prompt: create the default config, or abort."""
    # QMessageBox takes a QWidget parent or None, never the QApplication itself (30a review).
    QApplication.instance() or QApplication([])  # Qt needs an application before any widget
    box = QMessageBox()
    box.setWindowTitle("ATE")
    box.setText(f"No config file at {path}")
    box.setInformativeText("Create a default config and launch, or stop?")
    create = box.addButton("Create default config and launch", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Abort", QMessageBox.ButtonRole.DestructiveRole)
    box.exec()
    return box.clickedButton() is create


def _alert_dialog(message: str) -> None:
    """Show a launch refusal and stop."""
    QApplication.instance() or QApplication([])
    # Built (not via QMessageBox.critical) so the exec seam stays visible to callers and tests.
    box = QMessageBox(QMessageBox.Icon.Critical, "ATE", message, QMessageBox.StandardButton.Ok, None)
    box.exec()


def _close_confirm_dialog(message: str) -> bool:
    """Ask whether to abort a Test Run that is still in progress when closing the window."""
    QApplication.instance() or QApplication([])
    box = QMessageBox(QMessageBox.Icon.Warning, "ATE", message)
    abort = box.addButton("Abort the run and quit", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Keep running", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    return box.clickedButton() is abort


def launch(
    argv: Optional[list[str]] = None,
    *,
    exec_: bool = True,
    prompt: Optional[Callable[[str], bool]] = None,
    alert: Optional[Callable[[str], None]] = None,
    confirm_close: Optional[Callable[[str], bool]] = None,
) -> LaunchOutcome:
    """Open the application from ``ate``'s arguments. Returns the exit code and the window.

    ``prompt`` answers the missing-config dialog (True creates the defaults), ``alert`` reports a
    refusal, and ``confirm_close`` answers the "abort the run and quit?" question when the window
    is closed while a Test Run is in progress. With ``exec_=False`` the window is built but
    neither shown nor looped — the tests use that; the real launch shows it and enters the loop.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="ate", description="Open the ATE application")
    parser.add_argument("--config", metavar="PATH", help="config file to read (default: the platform config)")
    args, _unknown = parser.parse_known_args(argv)

    prompt = prompt or _missing_config_dialog
    alert = alert or _alert_dialog

    try:
        config = read_config(args.config, on_missing_prompt=prompt)
    except MissingConfigError as exc:
        # The Operator chose Abort at the prompt: one clear line, not a re-worded restatement (30a review).
        alert(str(exc))
        return LaunchOutcome(1)
    except ConfigError as exc:
        alert(str(exc))
        return LaunchOutcome(1)

    try:
        app_log = open_app_log(config.log_directory, level=config.log_level)
    except LogError as exc:
        alert(str(exc))
        return LaunchOutcome(1)
    app_log.log("INFO", "ate", f"ATE {__version__} launched with config {config.path}")

    refusal = check_database_at_launch(config.database_path)
    if refusal.status is not DbLaunchStatus.OK:
        # Spec 22/23: the GUI never migrates; it stops and says what to do.
        app_log.log("ERROR", "ate", f"launch refused: {refusal.message}")
        app_log.close()
        alert(refusal.message)
        return LaunchOutcome(1)

    # The session reuses this launch's config and application log: one read, one log (30a review).
    session = HeadlessRunSession(config, app_log=app_log)
    session.store.close_interrupted_runs(on_log=session.on_log)  # spec 98, logged by the store itself

    app = QApplication.instance() or QApplication([])  # reused when a test already has one
    window = MainWindow(session, config, app_log=app_log, confirm_close=confirm_close)
    if not exec_:
        # Keep the application referenced: a local QApplication dying here would take the
        # window (and the session's store) with it (30a review item 2).
        return LaunchOutcome(0, window, app)
    window.show()
    return LaunchOutcome(app.exec(), window, app)
