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
5. open the main window: the staged Run screen as the default view (constructing it scans the
   configured Test Definition directories, spec 31), the History screen reachable from the View
   menu, and the Run view's debug window inside the Run screen.

Both dialogs are injectable (`prompt`, `alert`) so tests never block on a real
:class:`~PyQt6.QtWidgets.QMessageBox`, and `exec_=False` returns the window instead of entering
the event loop.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Callable, Optional

from PyQt6.QtWidgets import QApplication, QMainWindow, QStackedWidget, QMessageBox

from ._version import __version__
from .config import Config, ConfigError, MissingConfigError, read_config
from .history.session import HistorySession
from .log import LogError, open_app_log
from .run_session.headless import HeadlessRunSession, SessionState
from .storage.store import DbLaunchStatus, check_database_at_launch
from .ui.history_screen import HistoryScreen
from .ui.staged_run_screen import StagedRunScreen


@dataclass
class LaunchOutcome:
    """What a launch produced: the exit code, and the main window when it got that far."""

    code: int
    window: Optional[QMainWindow] = None


class MainWindow(QMainWindow):
    """The application window: the Run screen by default, History from the View menu.

    ``session`` is the run session both screens bind to; the History switch is disabled while a
    Test Run is in progress when ``ui.allowHistoryDuringRun`` is false (spec 23c).
    """

    def __init__(
        self,
        session: HeadlessRunSession,
        config: Config,
        *,
        run_screen: Optional[StagedRunScreen] = None,
        history_screen: Optional[HistoryScreen] = None,
        app_log=None,
    ) -> None:
        super().__init__()
        self.session = session
        self.config = config
        self.app_log = app_log
        self.setWindowTitle(f"ATE {__version__} — {config.station_name}")

        self.history_session = HistorySession(session.store, config=session.config)
        self.run_screen = run_screen or StagedRunScreen(session)
        self.history_screen = history_screen or HistoryScreen(self.history_session)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.run_screen)  # stage 1: the Run screen is the launch view
        self.stack.addWidget(self.history_screen)
        self.setCentralWidget(self.stack)

        view_menu = self.menuBar().addMenu("View")
        self.run_action = view_menu.addAction("Run")
        self.history_action = view_menu.addAction("History")
        self.run_action.triggered.connect(self.show_run)
        self.history_action.triggered.connect(self.show_history)

        session.state_changed.connect(self._on_state_changed)
        # History's "New run for this UUT" comes back to the Run screen with the fields filled in.
        self.history_screen.new_run_requested.connect(self._new_run_from_history)

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
        """Tell History whether a run is up, and disable its switch when the config says so."""
        running = state == SessionState.STARTED
        self.history_session.set_run_in_progress(running)
        self.history_action.setEnabled(running is False or self.config.ui_allow_history_during_run)

    def _new_run_from_history(self, prefill: dict) -> None:
        """Switch to the Run screen and copy History's prefill into the session's fields."""
        self.show_run()
        setters = {
            "uut_type": self.session.set_uut_type,
            "uut_serial": self.session.set_uut_serial,
            "project": self.session.set_project,
        }
        for key, value in (prefill or {}).items():
            setter = setters.get(key)
            if setter is not None and value:
                setter(value)


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


def launch(
    argv: Optional[list[str]] = None,
    *,
    exec_: bool = True,
    prompt: Optional[Callable[[str], bool]] = None,
    alert: Optional[Callable[[str], None]] = None,
) -> LaunchOutcome:
    """Open the application from ``ate``'s arguments. Returns the exit code and the window.

    ``prompt`` answers the missing-config dialog (True creates the defaults), ``alert`` reports a
    refusal. With ``exec_=False`` the window is built and shown but the event loop is not entered,
    which is what the tests use.
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

    # The session reads the same config file and opens its own handle on this launch's log file.
    session = HeadlessRunSession(config.path)
    session.store.close_interrupted_runs(on_log=session.on_log)  # spec 98, logged by the store itself

    app = QApplication.instance() or QApplication([])
    window = MainWindow(session, config, app_log=app_log)
    if not exec_:
        return LaunchOutcome(0, window)
    window.show()
    return LaunchOutcome(app.exec(), window)
