"""The streaming Device shared by the background-buffered Device Types (packets 17a, 17b, 18a).

TCP and SERIAL are the same machine with a different transport: from ``open`` to ``close`` a
reader thread drains the port into a capped buffer, ``read`` takes whatever is already there
before waiting (honouring the read timeout) for what is not, an overflow drops the oldest bytes
and reports ``overflow = true`` with one WARNING on the next read, and a transport failure marks
the device failed exactly once - the bytes already buffered stay readable, after which every
read and write returns that error, and the failure is recorded as one (time, message) warning the
host drains (packet 17b). A write that cannot get out within its timeout fails only that call; a
send error fails the device like a reader error. What a transport *is* - a socket, a pyserial
port - lives in the hooks below: poll it for data, send bytes with a deadline, close it, describe
it.

The per-call override fields for ``read`` and ``write`` are identical for every streaming Device
Type, so they live here too as ``READ_FIELDS``/``WRITE_FIELDS``; each driver exposes them as
``read_fields``/``write_fields`` and the handle carries the driver that opened it so the ATE
wrappers validate against the right Device Type.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

from .registry import DEFAULT_RECEIVE_BUFFER_BYTES, Driver

#: How long one reader poll waits for data before the loop re-checks the stop flag, so close()
#: never has to wait out a full read.
POLL_SECONDS = 0.05
#: Bytes one poll pulls from the transport.
READ_CHUNK = 65536
#: Bytes one write hands to the transport at a time. pyserial raises on a write timeout without
#: saying how many bytes reached the wire, so a payload goes out in pieces and the reported count
#: is the pieces that made it out whole.
WRITE_CHUNK = 4096

#: Per-call ``read`` overrides (packet 16b): all optional, each falling back to the open-time
#: configuration. An unknown field, a mistyped value, or (for size) a non-positive number is a
#: schedule bug that raises naming the field, never a Device failure.
READ_FIELDS = {
    "size": ((int,), False),  # optional byte limit; None means read until terminator/timeout
    "timeout": ((int, float), False),  # per-call timeout in seconds (falls back to open-time)
    "readTerm": ((str, bytes), False),  # per-call terminator stripped from data ("" = none)
}
#: Per-call ``write`` overrides: ``data`` is required, the rest fall back to the open-time config.
WRITE_FIELDS = {
    "data": ((str, bytes), True),  # required; raw bytes sent (terminator appended separately)
    "writeTerm": ((str, bytes), False),  # per-call terminator appended to data ("" = none)
    "timeout": ((int, float), False),  # send timeout in seconds (falls back to open-time)
}


class _TransportClosed(Exception):
    """The transport is gone - peer closed, connection reset, adapter unplugged. Its message is
    the failure the device reports (packet 17b)."""


class _SendTimeout(Exception):
    """The transport could not accept the bytes before the call's deadline. This fails only the
    call: the connection itself is intact."""


class _StreamDevice:
    """Opaque handle for a background-buffered stream, whatever the transport underneath is.

    Lua sees it as userdata and only ever passes it back to ``read``/``write``/``close``. A
    background reader thread moves bytes from the transport into ``_buffer``; every buffer access
    happens under ``_lock``, and ``read`` waits on ``_cond`` only for what the buffer doesn't
    already hold. ``_failed`` holds a transport failure (seen by the reader or a read/write);
    until then buffered bytes are always readable first. ``_overflow_pending`` marks a cap
    overflow since the previous read so the next read can carry ``overflow = true``. ``_warnings``
    holds the device's failure warning(s) - one (time, message) per failure, put under the lock by
    ``_fail`` and drained by the host via ``take_warnings`` (packet 17b). ``driver`` is the driver
    that opened it, which is what the ATE wrappers validate the per-call fields against."""

    #: A Device Type whose reads are whole messages (UDP datagrams) can only be partial because
    #: ``size`` cut one short; a stream is partial when the deadline catches it mid-bytes. The host
    #: reads this to word the partial-read WARNING correctly (packets 17a/18b).
    partial_read_is_truncation = False

    def __init__(self, params: dict[str, Any], driver: type[Driver],
                 receive_buffer_bytes: int = DEFAULT_RECEIVE_BUFFER_BYTES) -> None:
        self.params = params
        self.driver = driver
        self._buffer = bytearray()
        self._limit = int(receive_buffer_bytes)
        self._failed: str | None = None
        self._overflow_pending = False
        self._overflow_dropped = 0  # bytes dropped by the cap since the previous read
        self._warnings: queue.Queue = queue.Queue()  # (monotonic timestamp, message), one per failure
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._stop = threading.Event()  # set by close() so the reader stops
        self._teardown = False  # set by close() so the reader never records a failure on us
        self._reader_thread: threading.Thread | None = None

    # --- transport hooks: the only parts that differ between TCP and SERIAL -------------------

    def describe(self) -> str:
        """A one-line description of this device for log lines."""
        raise NotImplementedError

    def _recv(self) -> bytes | None:
        """Pull at most one poll's worth of data from the transport: return the bytes, or None if
        nothing arrived within ``POLL_SECONDS``. Raise :class:`_TransportClosed` when the
        transport has ended or broken."""
        raise NotImplementedError

    def _send(self, chunk: bytes, seconds: float) -> int:
        """Hand ``chunk`` to the transport, waiting at most ``seconds``, and return the number of
        bytes accepted. Raise :class:`_SendTimeout` if it cannot go in time,
        :class:`_TransportClosed` if the transport is gone."""
        raise NotImplementedError

    def _close_transport(self) -> None:
        """Release the transport itself (socket close, serial port close)."""
        raise NotImplementedError

    # --- shared reader, buffer, failure and warning logic ---------------------------------------

    def start_reader(self) -> None:
        """Start the background reader thread (called from the driver's ``open``)."""
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name=f"ate-{self.driver.name.lower()}-reader"
        )
        self._reader_thread.start()

    def _reader_loop(self) -> None:
        """Drain the transport into the buffer until close, an end of stream, or an error.

        Polls at most every ``POLL_SECONDS`` so close() is always prompt, and keeps its eyes on the
        device (never on its own): on an error it records the failure and wakes any read that was
        waiting, but does not log - logging happens from the worker thread."""
        while not self._stop.is_set():
            try:
                chunk = self._recv()
            except _TransportClosed as exc:
                if not self._teardown:  # closed under us by close() is not a failure
                    self._fail(str(exc))
                return
            except Exception as exc:  # an error the transport didn't translate still fails the device
                if not self._teardown:
                    self._fail(f"{type(exc).__name__}: {exc}")
                return
            if chunk:
                self._append(chunk)

    def _fail(self, message: str) -> None:
        """Mark the device failed and wake any waiting read (the reader thread, or a failed write).

        The first failure wins: later calls (the race of a write error and the reader hitting the
        same close) only wake the waiters and never touch the buffer again, so a device records
        exactly one (time, message) warning in ``_warnings``."""
        with self._lock:
            if self._failed is not None:
                self._cond.notify_all()  # a read may still be waiting on the buffer
                return
            self._failed = message
            self._warnings.put((time.monotonic(), f"{self.describe()}: {message}"))
            self._cond.notify_all()

    def take_warnings(self) -> list[tuple[float, str]]:
        """Drain and return any (monotonic timestamp, message) failure warnings not yet taken.

        Safe to call from the worker thread only: the host drains it at every ATE call, during a
        pause, and at action and run end. The device itself never calls this - the reader only
        enqueues."""
        out: list[tuple[float, str]] = []
        while True:
            try:
                out.append(self._warnings.get_nowait())  # blocking get() would hang on the last item
            except queue.Empty:
                return out

    def _append(self, chunk: bytes) -> None:
        """Extend the buffer with ``chunk``, dropping the oldest bytes when the cap is exceeded;
        remembers that an overflow happened for the next read."""
        with self._lock:
            drop = len(self._buffer) + len(chunk) - self._limit
            if drop > 0:  # an exact fit drops nothing and is not an overflow
                self._overflow_pending = True
                self._overflow_dropped += drop
            if len(chunk) >= self._limit:  # the chunk alone fills the buffer
                del self._buffer[:]
                self._buffer.extend(chunk[-self._limit:])  # only the newest bytes survive
            else:
                if drop > 0:
                    del self._buffer[:drop]  # drop the oldest, keep the newest
                self._buffer.extend(chunk)
            self._cond.notify_all()  # wake a read waiting on the buffer

    def take_overflow(self) -> tuple[bool, int]:
        """Return ``(overflowed, dropped)`` for the cap overflows since the previous call and reset
        both, atomically under ``_lock`` so a drop the reader records concurrently is never lost."""
        with self._lock:
            overflowed, dropped = self._overflow_pending, self._overflow_dropped
            self._overflow_pending = False
            self._overflow_dropped = 0
        return overflowed, dropped

    def read(self, params: dict[str, Any]) -> tuple[bytes | None, int, bool, str | None]:
        """Read and return ``(data, count, partial, err_or_None)``.

        Takes whatever the buffer already holds first - up to ``readTerm`` (stripped), ``size``
        bytes, or, once the transport failed, the buffered remainder - and only then waits, on the
        reader's condition, for the rest until the timeout. Returns:

        * ``(data, count, False, None)`` - complete: terminator seen, ``size`` reached, or the
          transport ended after these bytes.
        * ``(data, count, True, None)``  - timed out after some bytes (they are consumed).
        * ``(None, 0, False, "timeout")`` - timed out with no bytes.
        * ``(None, 0, False, err)``       - the transport failed (peer closed, reset) and nothing is
          buffered."""
        size = params.get("size")
        term = self._term(params, "readTerm")
        deadline = time.monotonic() + self._timeout(params)
        with self._cond:
            while True:
                data = self._take_complete(term, size)
                if data is not None:
                    return data, len(data), False, None
                if self._failed is not None:
                    if not self._buffer:
                        return None, 0, False, self._failed
                    data = self._take(len(self._buffer))  # the transport ended after these bytes
                    return data, len(data), False, None
                remaining = deadline - time.monotonic()
                if remaining <= 0:  # decide under the lock, on the same buffer just checked
                    if not self._buffer:
                        return None, 0, False, "timeout"
                    data = self._take(len(self._buffer))
                    return data, len(data), True, None
                self._cond.wait(timeout=remaining)

    def _take_complete(self, term: bytes, size: int | None) -> bytes | None:
        """Take a complete read from the buffer - up to a terminator within ``size`` bytes, or
        ``size`` bytes - or return None if the buffer doesn't hold one yet."""
        end = len(self._buffer) if size is None else size + len(term)
        idx = self._buffer.find(term, 0, end) if term else -1
        if idx >= 0:
            data = self._take(idx)
            del self._buffer[: len(term)]
            return data
        if size is not None and len(self._buffer) >= size:
            return self._take(size)
        return None

    def _take(self, n: int) -> bytes:
        """Remove and return the first ``n`` buffered bytes (the caller holds ``_lock``)."""
        data = bytes(self._buffer[:n])
        del self._buffer[:n]
        return data

    def flush(self) -> tuple[bool, int, str | None]:
        """Empty the buffer and return ``(ok, count, err_or_None)`` with ``count`` = bytes discarded.

        Packet 17a: the worker reports the overflow (if any) and clears its own flag on the next
        read, so a flush neither reports nor cancels one; failures go through ``onError`` in the
        same shape as read/write, and a device the reader marked failed reports its error."""
        with self._lock:
            if self._failed is not None:
                return False, 0, self._failed
            count = len(self._buffer)
            del self._buffer[:]
            self._cond.notify_all()  # a reader waiting for room gets the transport promptly
        return True, count, None

    def write(self, params: dict[str, Any]) -> tuple[bool, int, str | None]:
        """Send ``data`` with ``writeTerm`` appended and return ``(ok, count, err_or_None)``.

        ``count`` is the bytes actually sent: the whole payload, terminator included, on success;
        what went out before the failure otherwise. A payload that can't be sent within ``timeout``
        fails only this call (err = "timeout"): the peer may drain later, so the device stays
        usable. A send error (reset, broken pipe) is a transport failure and fails the device - one
        (time, message) warning - and wakes a waiting read, so a later read fails through it too."""
        with self._lock:
            failed = self._failed
        if failed is not None:
            return False, 0, failed
        payload = self._to_bytes(params["data"]) + self._term(params, "writeTerm")
        deadline = time.monotonic() + self._timeout(params)
        sent = 0
        while sent < len(payload):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, sent, "timeout"
            try:
                sent += self._send(payload[sent:], remaining)
            except _SendTimeout:
                return False, sent, "timeout"
            except _TransportClosed as exc:
                self._fail(str(exc))
                return False, sent, str(exc)
        return True, sent, None

    def _timeout(self, params: dict[str, Any]) -> float:
        """The per-call ``timeout`` if given, else the open-time one."""
        return float(params.get("timeout", self.params["timeout"]))

    def _term(self, params: dict[str, Any], field: str) -> bytes:
        """The per-call terminator ``field`` if given, else the open-time one, as bytes."""
        return self._to_bytes(params.get(field, self.params[field]))

    @staticmethod
    def _to_bytes(value: str | bytes) -> bytes:
        """Byte payloads arrive as bytes from Lua (see ``_AteTable._raw_params``); a ``str`` from a
        Python caller is encoded as UTF-8."""
        return value.encode("utf-8") if isinstance(value, str) else bytes(value)

    def quiescent(self) -> bool:
        """True once the reader has stopped for good, so no further warning can be enqueued."""
        return self._stop.is_set() and self._reader_thread is None

    def close(self) -> None:
        """Stop the reader thread, close the transport, and join the reader. Safe to call twice - a
        second close only retries the join of a reader that had not stopped (packet 16a)."""
        if not self._stop.is_set():
            # Teardown first, so the reader treats the closing transport as the end, not a failure;
            # then the transport, which wakes a reader in its poll so the join is prompt.
            self._teardown = True
            self._stop.set()
            try:
                self._close_transport()
            except OSError:
                pass  # already closed or a reset mid-close; nothing useful left to do
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2.0)
            if not self._reader_thread.is_alive():
                self._reader_thread = None  # a reader that did not stop stays referenced
