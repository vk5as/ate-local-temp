"""An exception nothing caught reaches the application log and the Operator (packet 32a).

PySide6 does not end the process when a slot, or a ``QThread.run`` override, raises. It hands the
exception to ``sys.excepthook`` and carries on, which by default only prints to stderr: the worker
keeps recording while a screen quietly stops updating. Replacing the hook is the one place that sees
all of them, on whichever thread they were raised. Qt's own message handler never sees them, because
they are Python's and not Qt's.

The previous hook still runs, so stderr and pytest-qt's capture see what they always saw.
"""

from __future__ import annotations

import sys
import threading
import traceback

from PySide6.QtCore import QObject, Signal

from .log import AppLog


class UnhandledExceptionLog(QObject):
    """Writes each unhandled exception to one launch's application log, and announces it."""

    # The log's path. May be emitted on a worker, so a widget connects with a queued connection.
    reported = Signal(str)

    def __init__(self, app_log: AppLog) -> None:
        super().__init__()
        self._app_log = app_log
        self._previous = None

    def install(self) -> None:
        """Become ``sys.excepthook``, keeping the current hook to pass everything on to."""
        self._previous = sys.excepthook
        sys.excepthook = self._hook

    def uninstall(self) -> None:
        """Put the previous hook back, unless something has replaced this one since."""
        if sys.excepthook == self._hook:
            sys.excepthook = self._previous

    def _hook(self, exc_type, exc, tb) -> None:
        try:
            # KeyboardInterrupt and SystemExit are someone asking to stop, not a fault.
            if issubclass(exc_type, Exception):
                trace = "".join(traceback.format_exception(exc_type, exc, tb)).rstrip()
                thread = threading.current_thread().name
                self._app_log.log("ERROR", "ate", f"Unhandled exception on thread {thread}:\n{trace}")
                self.reported.emit(str(self._app_log.path))
        except Exception:  # noqa: BLE001 - raising here is lost anyway, and the log may be closed by now
            pass
        finally:
            self._previous(exc_type, exc, tb)
