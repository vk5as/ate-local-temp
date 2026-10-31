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
from typing import Any, Optional, Sequence

from ..storage.store import _now_iso
from PyQt6.QtCore import QObject, pyqtSignal


class OperatorInput(QObject):
    """Owns the blocking waits and POLL state for one step's inputs.

    The type map (:attr:`declarations`) is owned by the host so it can apply typed-value mapping;
    this controller only stores/serves raw values and tracks which are waiting on a supply."""

    # Emitted whenever a value is supplied or a POLL click changes state, so the GUI refreshes.
    values_changed = pyqtSignal()
    # Emitted the moment a blocking ``ATE.input(n)`` actually starts waiting — the exact instant
    # the n-th input becomes answerable (packet 10a review: only then is its widget live).
    asked = pyqtSignal(int, int, int)  # (node_seq, attempt_no, index): 1-based index

    def __init__(self) -> None:
        super().__init__()
        # A Condition doubles as the lock for every other field (a Condition is also a Lock).
        self._lock = threading.Condition()
        # How many blocking ATE.input calls are still waiting on a supply — incremented when a
        # blocking ask starts in :meth:`get_raw`, decremented when the answer arrives. The run
        # engine waits for this to reach zero before recording a step so a blocking value that
        # was delivered on another thread is never missed. POLL clicks never block the worker,
        # so they are not counted (packet 10a review: a step's end must not wait for a POLL).
        self._outstanding = 0
        # index -> last raw value supplied; consumed when get_raw reads it or cleared on reset.
        self.pending: dict[int, Any] = {}
        # 0-based indices whose get_raw is blocked right now — the only inputs whose widgets are live.
        self._waiting: set[int] = set()
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

    def reset(self, keep: Sequence[int] = ()) -> None:
        """Clear blocking values and POLL state — called at the start of each Attempt.

        ``keep`` is the set of 0-based indices the Attempt about to run declares as blocking. An
        operator (or a harness driving the engine directly) can answer an input before the Action's
        ``ATE.input(n)`` runs: the answer sits in :attr:`pending` until this reset wipes it, the ask
        then waits for an answer nobody will ever give, and the run hangs with its status bar stuck
        on the current step. An answer already given for an index this Attempt will ask for is
        carried into the new window instead of dropped; everything else is still cleared, so a
        retried Attempt's re-ask never inherits the previous Attempt's value (12c)."""
        with self._lock:
            # The answers to carry, and their log rows, are taken before the wipe.
            carried = {index: self.pending[index] for index in keep if index in self.pending}
            carried_log = [(index, raw) for index, raw in self._supplies_log if index in carried]
            self.pending.clear()
            self.poll_state.clear()
            self._outstanding = 0
            self._waiting.clear()
            # Drop any queued values so a fresh run/step starts clean.
            for ch in self._channels.values():
                while not ch.empty():
                    ch.get_nowait()
            self._channels.clear()
            # Start each step's recording window clean so its input values are attributed to it.
            self._supplies_log.clear()
            self._clicks_log.clear()
            # 14d review: the pre-ask answers come back, so get_raw finds them and records them.
            self.pending.update(carried)
            self._supplies_log = carried_log
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
            # Only an answer nobody has asked for yet is kept in `pending`: that is exactly the
            # pre-ask answer reset() carries into the Attempt that will ask (14d review). An answer
            # handed to a worker already blocked on this index belongs to that ask and is not
            # repeated, so a retried Attempt re-asks under its own number (12c).
            if index in self._waiting:
                self._channels[index].put_nowait(value)
            else:
                self.pending[index] = value
            # Record the raw supply (typed later by the run engine from the step's declarations).
            self._supplies_log.append((index, value))
            # Only a wait actually in progress is resolved by this answer (10a review).
            was_waiting = index in self._waiting
        # Answered an outstanding ask outside the lock so a worker waiting on wait_for_asks can wake.
        if was_waiting:
            self._note_resolved()
        self.values_changed.emit()

    def is_waiting(self, index: int) -> bool:
        """True while a worker thread is blocked in :meth:`get_raw` for this 0-based index.

        A blocking input can only be answered at the moment its ``ATE.input(n)`` call actually
        waits, which is what the session checks before a value is accepted (10a review)."""
        with self._lock:
            return index in self._waiting

    def get_raw(self, index: int, flag: Any = None, node_seq: Optional[int] = None, attempt_no: Optional[int] = None) -> Any:
        """Block until a value is supplied for ``index``; raise if none ever arrives.

        A value supplied before the wait starts is returned either way. When ``flag`` (the run's
        shared abort flag) is given, the wait runs in short slices so an operator abort wakes the
        thread and raises "aborted" into Lua (packet 13b); with no flag it blocks forever, as before.

        The wait is announced on :attr:`asked` the instant it starts, carrying the step and Attempt
        that asked, and it is un-counted as soon as the answer arrives or the run is aborted — so a
        step that declares inputs its action never reads never keeps the run waiting (10a review)."""
        with self._lock:
            # A value supplied before we started waiting is returned immediately.
            if index in self.pending:
                return self.pending.pop(index)
            ch = self._channels.setdefault(index, queue.Queue())
            self._outstanding += 1
            self._waiting.add(index)
        # Released the lock before blocking so supply_input can deliver while we wait.
        self.asked.emit(node_seq, attempt_no, index + 1)
        try:
            if flag is None:
                value = ch.get()
            else:
                # Slice the wait so an abort is observed within ~50 ms even when no value is
                # arriving; a value that does arrive is returned normally, ahead of any flag check.
                while True:
                    try:
                        value = ch.get(timeout=0.05)
                        break
                    except queue.Empty:
                        if flag.aborted:
                            raise RuntimeError("aborted")
        except RuntimeError:
            # Nobody will answer an aborted ask: drop it from the count the engine waits on.
            with self._lock:
                self._waiting.discard(index)
            self._note_resolved()
            raise
        with self._lock:
            self._waiting.discard(index)
        return value

    def click_poll(self, node_seq: int, attempt_no: int, index: int, text: str) -> None:
        """Record a POLL button click as sticky state for this step's context.

        Also appends the click to this controller's log so the run engine records it as an
        :class:`~ate.storage.store.input_value` row when that step finishes."""
        with self._lock:
            self.poll_state[(node_seq, attempt_no, index)] = text
            self._clicks_log.append((node_seq, attempt_no, index, _now_iso(), text))
        # A POLL never blocks the worker, so clicking one resolves no outstanding ask (10a review).
        self.values_changed.emit()

    def poll_value(self, node_seq: int, attempt_no: int, index: int) -> Optional[str]:
        """Return the sticky POLL text for this context, or None if not yet clicked."""
        with self._lock:
            return self.poll_state.get((node_seq, attempt_no, index))
