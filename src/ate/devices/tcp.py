"""The TCP Device driver (packets 16a, 17a).

A raw socket stream — e.g. a LAN instrument taking SCPI on port 5025, or a UUT debug port. It
connects with :func:`socket.create_connection` and validates its params generically through the
shared registry validator before touching the network. From `open` until `close`, a background
reader thread drains the socket into a buffer, so nothing is lost between ATE calls: a `read`
takes from the buffer first and only waits (honouring the read timeout) for what isn't there yet.
Received bytes a read didn't return (past a terminator or ``size``) stay in the buffer for the
next read. The buffer is capped (``receiveBufferBytes``): overflow drops the oldest bytes and the
reader remembers it, so the worker's next read reports ``overflow = true`` with one WARNING.
Once the connection fails (peer closed, reset) the reader marks the device failed exactly as a
read would: buffered bytes can still be read, after which every read and write returns that error —
and the failure is recorded on the device as one (time, message) warning that the host drains
promptly into a device WARNING stamped with that time (packet 17b). A write that times out fails
only that call; a send error fails the device like a reader error."""

from __future__ import annotations

import queue
import select
import socket
import threading
import time
from typing import Any

from .registry import DEFAULT_RECEIVE_BUFFER_BYTES, Driver, validate_params


class _TcpDevice:
    """Opaque handle returned by :meth:`TcpDriver.open`.

    Lua sees it as userdata and only ever passes it back to ``read``/``write``/``close``. It
    carries the live socket, a short description for log lines, and the read/write logic
    (packets 16b, 17a). A background reader thread moves bytes from the socket into ``_buffer``;
    every buffer access happens under ``_lock``, and ``read`` waits on ``_cond`` only for what the
    buffer doesn't already hold. ``_failed`` holds a connection failure (seen by the reader or a
    read/write); until then buffered bytes are always readable first. ``_overflow_pending`` marks
    a cap overflow since the previous read so the next read can carry ``overflow = true``.
    ``_warnings`` holds the device's failure warning(s) — one (time, message) per failure, put
    under the lock by ``_fail`` and drained by the host via ``take_warnings`` (packet 17b)."""

    def __init__(self, sock: socket.socket, params: dict[str, Any],
                 receive_buffer_bytes: int = DEFAULT_RECEIVE_BUFFER_BYTES) -> None:
        self.sock = sock
        self.params = params
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

    def describe(self) -> str:
        """A one-line description of this device for log lines (host and port)."""
        return f"TCP {self.params['host']}:{self.params['port']}"

    def start_reader(self) -> None:
        """Start the background reader thread (called from the driver's ``open``)."""
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="ate-tcp-reader"
        )
        self._reader_thread.start()

    def _reader_loop(self) -> None:
        """Drain the socket into the buffer until close, EOF, or an error.

        Polls the socket at most every 50 ms so close() is always prompt, and keeps its eyes on the
        device (never on its own): on an error it records the failure and wakes any read that was
        waiting, but does not log — logging happens from the worker thread."""
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self.sock], [], [], 0.05)
            except (OSError, ValueError) as exc:
                if not self._teardown:  # closed under us by close() (fd -1) is not a failure
                    self._fail(str(exc))
                return
            if not ready:
                continue
            try:
                chunk = self.sock.recv(65536)
            except OSError as exc:
                if not self._teardown:
                    self._fail(str(exc))
                return
            if not chunk:  # the peer closed: an orderly EOF
                if not self._teardown:
                    self._fail("connection closed by peer")
                return
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
        pause, and at action and run end. The device itself never calls this — the reader only
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

        Takes whatever the buffer already holds first — up to ``readTerm`` (stripped), ``size``
        bytes, or, once the connection failed, the buffered remainder — and only then waits, on
        the reader's condition, for the rest until the timeout. Returns:

        * ``(data, count, False, None)`` — complete: terminator seen, ``size`` reached, or the
          connection ended after these bytes.
        * ``(data, count, True, None)``  — timed out after some bytes (they are consumed).
        * ``(None, 0, False, "timeout")`` — timed out with no bytes.
        * ``(None, 0, False, err)``       — the connection failed (peer closed, reset) and nothing is
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
                    data = self._take(len(self._buffer))  # the connection ended after these bytes
                    return data, len(data), False, None
                remaining = deadline - time.monotonic()
                if remaining <= 0:  # decide under the lock, on the same buffer just checked
                    if not self._buffer:
                        return None, 0, False, "timeout"
                    data = self._take(len(self._buffer))
                    return data, len(data), True, None
                self._cond.wait(timeout=remaining)

    def _take_complete(self, term: bytes, size: int | None) -> bytes | None:
        """Take a complete read from the buffer — up to a terminator within ``size`` bytes, or
        ``size`` bytes — or return None if the buffer doesn't hold one yet."""
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
            self._cond.notify_all()  # a reader waiting for room gets the socket promptly
        return True, count, None

    def write(self, params: dict[str, Any]) -> tuple[bool, int, str | None]:
        """Send ``data`` with ``writeTerm`` appended and return ``(ok, count, err_or_None)``.

        ``count`` is the bytes actually sent: the whole payload, terminator included, on success; what
        went out before the failure otherwise. A payload that can't be sent within ``timeout`` fails
        only this call (err = "timeout"): the peer may drain later, so the device stays usable. A send
        error (reset, broken pipe) is a connection failure and fails the device — one (time, message)
        warning — and wakes a waiting read, so a later read fails through it too."""
        with self._lock:
            failed = self._failed
        if failed is not None:
            return False, 0, failed
        payload = self._to_bytes(params["data"]) + self._term(params, "writeTerm")
        deadline = time.monotonic() + self._timeout(params)
        sent = 0
        while sent < len(payload):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.sock], [], remaining)[1]:
                return False, sent, "timeout"  # this call's failure only; the connection is intact
            try:
                sent += self.sock.send(payload[sent:])
            except OSError as exc:
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
        """Stop the reader thread, close the socket, and join the reader. Safe to call twice — a
        second close only retries the join of a reader that had not stopped (packet 16a)."""
        if not self._stop.is_set():
            # Teardown first, so the reader treats the closing socket as the end, not a failure;
            # then the socket, which wakes a reader in select or recv so the join is prompt.
            self._teardown = True
            self._stop.set()
            try:
                self.sock.close()
            except OSError:
                pass  # already closed or a reset mid-close; nothing useful left to do
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2.0)
            if not self._reader_thread.is_alive():
                self._reader_thread = None  # a reader that did not stop stays referenced


class TcpDriver(Driver):
    """Opens and closes TCP Devices (DeviceType.TCP)."""

    name = "TCP"
    accepts_receive_buffer_bytes = True  # 17a: the reader thread caps its buffer from the config
    fields = {
        "host": ((str,), True),  # the instrument's host, e.g. "127.0.0.1" or a hostname
        "port": ((int, float), True),  # the port to connect to, e.g. 5025
        "timeout": ((int, float), True),  # connect, read and write timeout in seconds
        "readTerm": ((str, bytes), True),  # byte string stripped from reads; "" means none
        "writeTerm": ((str, bytes), True),  # byte string appended to writes; "" means none
    }

    # Per-call override fields for read/write (packet 16b). All optional — an absent field falls back
    # to the open-time configuration. They are a subset of ``fields``: host/port stay required only at
    # open, never here. A field this Device Type doesn't know (a typo such as "buad"), a missing
    # required field ("data" for write), or a mistyped value always raises and names the field.
    read_fields = {
        "size": ((int,), False),  # optional byte limit; None means read until terminator/timeout
        "timeout": ((int, float), False),  # per-call timeout in seconds (falls back to open-time)
        "readTerm": ((str, bytes), False),  # per-call terminator stripped from data ("" = none)
    }
    write_fields = {
        "data": ((str, bytes), True),  # required; raw bytes sent (terminator appended separately)
        "writeTerm": ((str, bytes), False),  # per-call terminator appended to data ("" = none)
        "timeout": ((int, float), False),  # send timeout in seconds (falls back to open-time)
    }

    def open(self, params: dict[str, Any],
             receive_buffer_bytes: int = DEFAULT_RECEIVE_BUFFER_BYTES) -> _TcpDevice:
        """Validate ``params`` and connect a TCP socket. Raises :class:`ValueError` naming any bad
        field before connecting."""
        validated = validate_params(self, params)
        host = validated["host"]
        port = int(validated["port"])  # normalise to an integer port for the address tuple
        sock = socket.create_connection((host, port), timeout=validated["timeout"])
        device = _TcpDevice(sock, validated, receive_buffer_bytes)
        device.start_reader()  # packet 17a: the reader runs from open to close
        return device
