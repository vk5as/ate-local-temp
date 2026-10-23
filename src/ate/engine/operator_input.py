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

from ..storage.store import _now_iso
from PyQt6.QtCore import QObject, pyqtSignal


class OperatorInput(QObject):
    """Owns the blocking waits and POLL state for one step's inputs.

    The type map (:attr:`declarations`) is owned by the host so it can apply typed-value mapping;
    this controller only stores/serves raw values and tracks which are waiting on a supply."""

    # Emitted whenever a value is supplied or a POLL click changes state, so the GUI refreshes.
    values_changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        # A Condition doubles as the lock for every other field (a Condition is also a Lock).
        self._lock = threading.Condition()
        # How many announced inputs are still waiting on an answer — incremented when a step
        # announces its n-th input, decremented when that answer arrives (supply_input / click_poll).
        # The run engine waits for this to reach zero before recording the step's values so a POLL
        # click is always recorded while it runs rather than lost if the worker records first.
        self._outstanding = 0
        # index -> last raw value supplied; consumed when get_raw reads it or cleared on reset.
        self.pending: dict[int, Any] = {}
        # POLL sticky text keyed by (node_seq, attempt_no, index); reset at run/step start.
        self.poll_state: dict[tuple[int, int, int], Optional[str]] = {}
        # one blocking channel per input index so a supplied value wakes the waiting worker.
        self._channels: dict[int, queue.Queue] = {}
        # Ordered (index, raw_value) of every blocking supply since the last reset; the run engine
        # snapshots it after each step's action to record that step's input values.
        self._supplies_log: list[tuple[int, Any]] = []
        # Ordered POLL clicks since the last reset — (node_seq, attempt_no, index, text) — so a
        # clicked button is recorded even though it never blocks the worker thread.
        self._clicks_log: list[tuple[int, int, int, str]] = []

    def set_declarations(self, declarations: list[Any]) -> None:
        """Set the type map for this step's inputs (index -> StepInput)."""
        with self._lock:
            self.declarations = list(declarations)

    def note_asked(self) -> None:
        """Record that a step is asking for one more input (called before it is announced)."""
        with self._lock:
            self._outstanding += 1

    def _note_resolved(self) -> None:
        """An answer arrived for an outstanding ask — wake any worker waiting on :meth:`wait_for_asks`."""
        with self._lock:
            if self._outstanding > 0:
                self._outstanding -= 1
                self._lock.notify_all()

    def wait_for_asks(self, timeout: Optional[float] = None) -> None:
        """Block until every announced input has been answered.

        Called by the run engine before it records a step's values. Blocking supplies already wake
        this thread via :meth:`get_raw`, but POLL clicks never block — so without this wait a worker
        could record a step (and drop an unanswered click) before the operator's answer is delivered.
        A timeout guards against a schedule that declares inputs its action never asks for."""
        with self._lock:
            while self._outstanding > 0:
                remaining = None if timeout is None else timeout
                self._lock.wait(remaining)

    def reset(self) -> None:
        """Clear blocking values and POLL state — called at Test Run start and each step start."""
        with self._lock:
            self.pending.clear()
            self.poll_state.clear()
            self._outstanding = 0
            # Drop any queued values so a fresh run/step starts clean.
            for ch in self._channels.values():
                while not ch.empty():
                    ch.get_nowait()
            self._channels.clear()
            # Start each step's recording window clean so its input values are attributed to it.
            self._supplies_log.clear()
            self._clicks_log.clear()
            self.values_changed.emit()

    def supplied_inputs(self) -> list[tuple[int, Any]]:
        """Snapshot (and clear) the ordered blocking supplies recorded since the last reset."""
        with self._lock:
            out = list(self._supplies_log)
            self._supplies_log.clear()
            return out

    def poll_clicks(self) -> list[tuple[int, int, int, str]]:
        """Snapshot (and clear) POLL clicks recorded since the last reset."""
        with self._lock:
            out = list(self._clicks_log)
            self._clicks_log.clear()
            return out

    def supply_input(self, index: int, value: Any) -> None:
        """Supply a value for a blocking input; wakes any worker waiting on that index.

        Records the raw value under this controller's active step context so the run engine can
        attribute it to that step when it records :class:`~ate.storage.store.input_value` rows."""
        with self._lock:
            self.pending[index] = value
            # Deliver to any waiting worker on this channel.
            ch = self._channels.get(index)
            if ch is not None:
                ch.put_nowait(value)
            # Record the raw supply (typed later by the run engine from the step's declarations).
            self._supplies_log.append((index, value))
        # Answered an outstanding ask outside the lock so a worker waiting on wait_for_asks can wake.
        self._note_resolved()
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
        """Record a POLL button click as sticky state for this step's context.

        Also appends the click to this controller's log so the run engine records it as an
        :class:`~ate.storage.store.input_value` row when that step finishes."""
        with self._lock:
            self.poll_state[(node_seq, attempt_no, index)] = text
            self._clicks_log.append((node_seq, attempt_no, index, _now_iso(), text))
        # Answered an outstanding ask outside the lock so a worker waiting on wait_for_asks can wake.
        self._note_resolved()
        self.values_changed.emit()

    def poll_value(self, node_seq: int, attempt_no: int, index: int) -> Optional[str]:
        """Return the sticky POLL text for this context, or None if not yet clicked."""
        with self._lock:
            return self.poll_state.get((node_seq, attempt_no, index))
