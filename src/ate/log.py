r"""ATE's application log (ticket 04).

One file per launch, named by start time, never rotated and never deleted — a plain
append-only record of ATE-level events for the life of one launch. It is kept separate
from the per-run logs the engine writes: this captures what *ATE* did at launch (config
load, interrupted-run recovery, fatal errors), not any individual Test Run.

The logger is file-backed rather than a Qt-signal callback so it can be opened once at
launch and written from anywhere — including worker threads — without threading a
display-dependent seam through every layer. Entries below the configured ``log.level``
are dropped before they reach the file; everything at or above is appended verbatim.

The module has no dependency on Qt, so its tests run headless (see ``tests/test_app_log.py``).
"""

from __future__ import annotations

import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional


from .errors import ATEError


class LogError(ATEError):
    """Raised when the application log cannot be opened or written."""


# Severity ordering; ``log.level`` selects the floor, entries below it are dropped.
_LEVELS = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
}

_NAME_RE = re.compile(r"^ate-app-\d{4}-\d{2}-\d{2}[T ]\d{2}-\d{2}-\d{2}")

# Reverse of ``_LEVELS`` so :meth:`AppLog.level` can name its own severity.
_LEVEL_NAMES = {severity: name for name, severity in _LEVELS.items()}


def _normalize_level(level: str) -> int:
    """Map a level name to its severity, defaulting unknown names to ``info``."""
    return _LEVELS.get(str(level).lower(), _LEVELS["info"])


class AppLog:
    """An append-only application log; one file per launch.

    The file is created (opened in append mode) on construction and named by the launch's
    start time, so each launch keeps its own history without overwriting earlier ones.
    :meth:`close` flushes and closes the handle but never removes the file — ATE's log for
    a launch is preserved after the process exits.
    """

    def __init__(self, directory: str | Path, level: str = "info", *, name: Optional[str] = None) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._level = _normalize_level(level)
        stamp = name or datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        self.path = self._dir / f"ate-app-{stamp}.log"
        try:
            self._fh = open(self.path, "a", encoding="utf-8")
        except OSError as exc:
            raise LogError(f"cannot open application log {self.path}: {exc}") from exc
        self._lock = threading.Lock()

    def level(self) -> str:
        """The configured minimum level this logger writes at."""
        return _LEVEL_NAMES.get(self._level, "info")

    def log(self, level: str, component: str, message: str) -> None:
        """Append one entry if ``level`` is at or above the configured threshold.

        ``component`` names the source (e.g. ``"ate"``); ``message`` is free text. Entries
        below the threshold are dropped here and never touch the file.
        """
        if _normalize_level(level) < self._level:
            return
        line = f"{datetime.now().isoformat(timespec='seconds')} {level.upper():<5} {component}: {message}\n"
        with self._lock:
            try:
                self._fh.write(line)
                self._fh.flush()
            except OSError as exc:
                raise LogError(f"cannot write application log {self.path}: {exc}") from exc

    def close(self) -> None:
        """Close the file handle. The launch keeps its own copy — this never deletes it."""
        with self._lock:
            try:
                self._fh.close()
            except OSError as exc:
                raise LogError(f"cannot close application log {self.path}: {exc}") from exc

    def __enter__(self) -> AppLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_app_log(directory: str | Path, level: str = "info") -> AppLog:
    """Open a fresh application log for one launch in ``directory`` at ``level``."""
    return AppLog(directory, level=level)
