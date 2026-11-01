"""How a Device call gives way to a whole-run abort (packet 32b, story 83).

The run's abort flag reaches a Device through :meth:`DeviceRegistry.open_device`. A wait ATE owns (a
buffer, ``select``) runs in slices of :data:`POLL_SECONDS` and checks the flag between them. A call
only the OS or a C library can end (a connect, a DNS lookup, ``viRead``, a pyserial write) runs on a
thread of its own while the worker watches the flag: on an abort the worker raises and leaves the
call behind, which ends when its own timeout says so. Nothing polls an instrument, and nothing here
changes what a call does without an abort.

No flag (a driver used directly) means the plain blocking call.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional

#: How long a wait runs before it looks at the abort flag again.
POLL_SECONDS = 0.05


class DeviceAborted(Exception):
    """An abort ended this Device call. The ATE wrappers turn it into Lua's "aborted" error."""


def check_abort(flag: Any) -> None:
    """Raise :class:`DeviceAborted` if an abort has been requested."""
    if flag is not None and flag.aborted:
        raise DeviceAborted()


def wait_slice(flag: Any, remaining: float) -> float:
    """How long the next wait may last, with ``remaining`` seconds left on the call's own timeout."""
    if not remaining <= threading.TIMEOUT_MAX:  # NaN or too long to wait at all: the wait itself says so
        return remaining
    remaining = max(remaining, 0.0)
    return remaining if flag is None else min(remaining, POLL_SECONDS)


class _Call:
    """One call on its helper thread, and who is left to deal with its result."""

    def __init__(self, fn: Callable[[], Any], discard: Optional[Callable[[Any], None]]) -> None:
        self.fn = fn
        self.discard = discard
        self.lock = threading.Lock()
        self.outcome: Optional[tuple[Any, Optional[BaseException]]] = None
        self.abandoned = False
        self.thread = threading.Thread(target=self._run, daemon=True, name="ate-device-call")

    def _run(self) -> None:
        value, error = None, None
        try:
            value = self.fn()
        except BaseException as exc:  # noqa: BLE001 - handed to the worker, or dropped if it has gone
            # Not just Exception: a KeyboardInterrupt or SystemExit here would otherwise kill the
            # thread with no outcome, and the worker watching for an abort alone would wait forever.
            error = exc
        with self.lock:
            self.outcome = (value, error)
            late = self.abandoned
        if late and error is None and self.discard is not None:
            try:
                self.discard(value)
            except Exception:  # noqa: BLE001 - nobody is left to tell
                pass

    def abandon(self) -> bool:
        """Leave the call behind, unless it has already finished."""
        with self.lock:
            if self.outcome is not None:
                return False
            self.abandoned = True
            return True


def run_abortable(
    fn: Callable[[], Any],
    flag: Any,
    discard: Optional[Callable[[Any], None]] = None,
    on_abandon: Optional[Callable[[threading.Thread], None]] = None,
) -> Any:
    """``fn()``, unless an abort comes first: then raise :class:`DeviceAborted` and leave it running.

    ``discard`` gets what an abandoned call returns when it does end (a connect's new handle, say).
    ``on_abandon`` gets the thread still in the call. ``fn`` must not touch Lua: it runs off the worker."""
    if flag is None:
        return fn()
    call = _Call(fn, discard)
    call.thread.start()
    while True:
        call.thread.join(POLL_SECONDS)
        if call.outcome is not None:
            break
        if not call.thread.is_alive():
            # The helper is gone and recorded nothing: there is no outcome to wait for and no abort
            # to raise, so waiting would be a wait on a thread that can never finish.
            raise RuntimeError(f"the Device call thread ended without an outcome: {call.thread.name}")
        if flag.aborted and call.abandon():
            if on_abandon is not None:
                on_abandon(call.thread)
            raise DeviceAborted()
    value, error = call.outcome
    if error is not None:
        raise error
    return value


class AbortableCalls:
    """A Device's calls that cannot be interrupted, one at a time.

    A call left behind by an abort still holds the transport. The next call on this Device (a
    Post-test Action powering a supply down, story 84) waits for it, as abort-aware as any other wait,
    and :meth:`close_after` releases the transport only once it has ended."""

    def __init__(self) -> None:
        self.flag: Any = None
        self._left_behind: Optional[threading.Thread] = None

    def _keep(self, thread: threading.Thread) -> None:
        self._left_behind = thread

    def wait_for_left_behind(self) -> None:
        """Wait for a call an earlier abort left behind; a new abort ends this wait too."""
        while self._left_behind is not None and self._left_behind.is_alive():
            check_abort(self.flag)
            self._left_behind.join(POLL_SECONDS)
        self._left_behind = None

    def run(self, fn: Callable[[], Any]) -> Any:
        """``fn()`` through :func:`run_abortable`, after any call still in progress."""
        if self.flag is None:
            return fn()
        self.wait_for_left_behind()
        return run_abortable(fn, self.flag, on_abandon=self._keep)

    def close_after(self, close: Callable[[], None]) -> bool:
        """If a call is still left behind, have ``close`` run once it ends and return True at once."""
        thread = self._left_behind
        if thread is None or not thread.is_alive():
            return False

        def close_when_free() -> None:
            # Deliberately unbounded: the call this waits for ends on its own timeout (see the packet's
            # table of what an abort cannot interrupt), and joining a bounded slice would mean either
            # waiting again or closing across a call still using the transport. The thread is a daemon,
            # so it cannot keep the process alive: the close is late, never lost, never wrong.
            thread.join()
            try:
                close()
            except Exception:  # noqa: BLE001 - nobody is left to tell
                pass

        threading.Thread(target=close_when_free, daemon=True, name="ate-device-late-close").start()
        return True
