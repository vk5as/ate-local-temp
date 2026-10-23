"""The OperatorInput controller — mediates ``ATE.input(n)`` between worker and operator.

Ticket 10 checkboxes b, c, d. A step's action runs in the run engine's QThread; when it calls
``ATE.input(n)`` for a blocking type this controller blocks that thread until the value is supplied
from the main/operator thread via :meth:`supply_input`. ``POLL`` inputs are non-blocking sticky
state keyed by ``(node_seq, attempt_no, index)``. Every change emits :attr:`values_changed` so the
GUI can refresh.

Blocking delivery uses a per-index :class:`queue.Queue` rather than a :class:`threading.Condition`:
the Condition's ``wait()`` deadlocks in this Qt 6.11 / PyQt6 6.11 build once an offscreen
``QApplication`` is present (a session-wide fixture pumps the event loop around every test), so the
worker never wakes on ``notify_all()``. A plain queue has no such interaction with Qt's signal
handling and still blocks-and-wakes correctly under the same conditions."""

from __future__ import annotations

import queue
import threading
from typing import Any, Optional

from PyQt6.QtCore import QObject, pyqtSignal


class OperatorInput(QObject):
    """Owns the blocking waits and POLL state for one step's inputs.

    The type map (:attr:`declarations`) is owned by the host so it can apply typed-value mapping;
    this controller only stores/serves raw values and tracks which are waiting on a supply."""

    # Emitted whenever a value is supplied or a POLL click changes state, so the GUI refreshes.
    values_changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        # index -> last raw value supplied; consumed when get_raw reads it or cleared on reset.
        self.pending: dict[int, Any] = {}
        # POLL sticky text keyed by (node_seq, attempt_no, index); reset at run/step start.
        self.poll_state: dict[tuple[int, int, int], Optional[str]] = {}
        # one blocking channel per input index so a supplied value wakes the waiting worker.
        self._channels: dict[int, queue.Queue] = {}

    def set_declarations(self, declarations: list[Any]) -> None:
        """Set the type map for this step's inputs (index -> StepInput)."""
        with self._lock:
            self.declarations = list(declarations)

    def reset(self) -> None:
        """Clear blocking values and POLL state — called at Test Run start and each step start."""
        with self._lock:
            self.pending.clear()
            self.poll_state.clear()
            # Drop any queued values so a fresh run/step starts clean.
            for ch in self._channels.values():
                while not ch.empty():
                    ch.get_nowait()
            self._channels.clear()
            self.values_changed.emit()

    def supply_input(self, index: int, value: Any) -> None:
        """Supply a value for a blocking input; wakes any worker waiting on that index."""
        with self._lock:
            self.pending[index] = value
            # Deliver to any waiting worker on this channel.
            ch = self._channels.get(index)
            if ch is not None:
                ch.put_nowait(value)
            self.values_changed.emit()

    def get_raw(self, index: int) -> Any:
        """Block until a value is supplied for ``index``; raise if none ever arrives."""
        with self._lock:
            # A value supplied before we started waiting is returned immediately.
            if index in self.pending:
                return self.pending.pop(index)
            ch = self._channels.setdefault(index, queue.Queue())
        # Released the lock before blocking so supply_input can deliver while we wait.
        return ch.get()

    def click_poll(self, node_seq: int, attempt_no: int, index: int, text: str) -> None:
        """Record a POLL button click as sticky state for this step's context."""
        with self._lock:
            self.poll_state[(node_seq, attempt_no, index)] = text
            self.values_changed.emit()

    def poll_value(self, node_seq: int, attempt_no: int, index: int) -> Optional[str]:
        """Return the sticky POLL text for this context, or None if not yet clicked."""
        with self._lock:
            return self.poll_state.get((node_seq, attempt_no, index))
