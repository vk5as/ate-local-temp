"""The UDP Device driver (packet 18b).

Datagrams, not a stream: one datagram is one message, so there are no terminators and a read is
never partial except when ``size`` cuts a datagram short. The reader thread, the failure and the
warning logic are the shared streaming device in :mod:`ate.devices.stream`; the parts UDP changes
are the buffer - a queue of whole datagrams capped by their total bytes - one datagram per read, one
datagram per write, and the fact that UDP has no connection to lose.

The socket binds ``localPort`` (0 = any) and then connects to ``host:port``: connecting is what makes
``recv`` accept only that peer's datagrams and ``send`` need no address. On Linux it also means a
peer that is not listening answers an ICMP port-unreachable, so ``recv``/``send`` raise
:class:`ConnectionRefusedError`. A peer that has not started listening yet is not a broken device -
it may start later - so the reader shrugs that off and keeps polling; only a send is hurt by it, and
only the call that hit it.
"""

from __future__ import annotations

import collections
import errno
import math
import select
import socket
import time
from typing import Any

from .registry import DEFAULT_RECEIVE_BUFFER_BYTES, Driver, validate_params
from .stream import (
    POLL_SECONDS,
    READ_CHUNK,
    _SendTimeout,
    _StreamDevice,
    _TransportClosed,
    transport_error_text,
)


class _Refused(Exception):
    """The peer is not listening: an ICMP port-unreachable came back to the connected UDP socket.

    Raised separately from :class:`_TransportClosed` because there is no connection to lose - the
    peer may start listening later - so it fails one call, never the device (packet 18b)."""


class _Oversize(Exception):
    """The payload is too big for one datagram.

    Also a one-call failure, like :class:`_Refused`: the socket is fine, it just cannot hold that
    many bytes, so EMSGSIZE must not be read as a transport going away (packet 18b)."""


#: The most bytes a single UDP datagram can carry: the 65,536-byte UDP length minus its 8-byte header.
#: Checked before sending so an oversize write says what is wrong instead of tripping EMSGSIZE.
MAX_DATAGRAM_BYTES = 65507

#: What a write says when the peer is not listening. Linux raises ConnectionRefusedError from either
#: call and Windows ConnectionResetError (WSAECONNRESET) from the next ``recv`` only, so the reader
#: usually gets it first. One wording, reported by the next write on both.
REFUSED_TEXT = "connection refused: the peer is not listening"


#: Per-call ``read`` overrides for UDP. No ``readTerm``: a datagram is the message, so a terminator
#: is not a field this Device Type knows, and passing one raises naming it.
UDP_READ_FIELDS = {
    "size": ((int,), False),  # byte limit; a longer datagram is truncated and returned partial
    "timeout": ((int, float), False),  # per-call timeout in seconds (falls back to open-time)
}
#: Per-call ``write`` overrides: ``data`` is required, no terminator is appended.
UDP_WRITE_FIELDS = {
    "data": ((str, bytes), True),  # required; the whole payload goes out as one datagram
    "timeout": ((int, float), False),  # send timeout in seconds (falls back to open-time)
}


class _UdpDevice(_StreamDevice):
    """Opaque handle returned by :meth:`UdpDriver.open`: a UDP socket behind the shared stream logic.

    The reader, the failure and the warning handling are :class:`ate.devices.stream._StreamDevice`;
    UDP replaces its byte buffer with a queue of whole datagrams (``_datagrams``, totalling
    ``_queued`` bytes) because a datagram is a message and cannot be split. The base's ``_buffer``
    stays empty: every path that reads it is overridden here."""

    #: A UDP read is whole or nothing, so a partial one is ``size`` cutting a datagram short, never
    #: the deadline catching it mid-bytes: the host words the WARNING as truncation, not a timeout.
    partial_read_is_truncation = True

    def __init__(self, sock: socket.socket, params: dict[str, Any],
                 receive_buffer_bytes: int = DEFAULT_RECEIVE_BUFFER_BYTES) -> None:
        super().__init__(params, UdpDriver, receive_buffer_bytes)
        self.sock = sock
        self._datagrams: collections.deque[bytes] = collections.deque()
        self._queued = 0  # bytes in _datagrams, the number the cap counts
        self._refused = False  # the reader saw a refusal that no write has reported yet

    def describe(self) -> str:
        """A one-line description of this device for log lines (the peer it is connected to)."""
        return f"UDP {self.params['host']}:{self.params['port']}"

    def _recv(self) -> bytes | None:
        """Pull one datagram, or None if nothing arrived within the poll time.

        UDP has no end of stream, so there is no EOF to report: an empty return is an empty
        datagram, which is a message, not "nothing". A connected UDP socket whose peer is not
        listening raises after an ICMP port-unreachable (see :data:`REFUSED_TEXT`) - the peer might
        start listening later, so that is "nothing this poll" rather than a device failure (packet
        18b), and the next write is the call that reports it."""
        try:
            ready, _, _ = select.select([self.sock], [], [], POLL_SECONDS)
        except (OSError, ValueError) as exc:  # closed under us by close() (fd -1)
            raise _TransportClosed(str(exc)) from exc
        if not ready:
            return None
        try:
            return self.sock.recv(READ_CHUNK)  # one recv is exactly one datagram
        except (ConnectionRefusedError, ConnectionResetError):
            with self._lock:
                self._refused = True
            return None  # peer not listening yet: keep polling
        except OSError as exc:
            raise _TransportClosed(transport_error_text(exc)) from exc

    def _reader_loop(self) -> None:
        """The shared drain loop, except that an empty datagram is data.

        The base skips a falsy chunk, which for a stream means nothing arrived; a zero-length UDP
        datagram is a real message, so it has to be queued too."""
        while not self._stop.is_set():
            try:
                datagram = self._recv()
            except _TransportClosed as exc:
                if not self._teardown:  # closed under us by close() is not a failure
                    self._fail(str(exc))
                return
            except Exception as exc:  # an error the transport didn't translate still fails the device
                if not self._teardown:
                    self._fail(f"{type(exc).__name__}: {exc}")
                return
            if datagram is not None:
                self._append(datagram)

    def _append(self, datagram: bytes) -> None:
        """Queue one whole datagram, dropping the oldest whole datagrams until the cap has room.

        The cap counts the total bytes of the queue, so dropping is by datagram: never half a
        message. A datagram bigger than the cap on its own cannot be queued at all and is dropped,
        counted in the overflow the next read reports."""
        with self._lock:
            if len(datagram) > self._limit:
                self._overflow_pending = True
                self._overflow_dropped += len(datagram)
                self._cond.notify_all()
                return
            while self._queued + len(datagram) > self._limit:  # an exact fit is not an overflow
                oldest = self._datagrams.popleft()
                self._queued -= len(oldest)
                self._overflow_pending = True
                self._overflow_dropped += len(oldest)
            self._datagrams.append(datagram)
            self._queued += len(datagram)
            self._cond.notify_all()  # wake a read waiting for a datagram

    def read(self, params: dict[str, Any]) -> tuple[bytes | None, int, bool, str | None]:
        """Read one datagram and return ``(data, count, partial, err_or_None)``.

        The oldest queued datagram is taken; if the queue is empty the read waits on the reader's
        condition until the timeout. Because a datagram arrives whole, the only partial read is one
        ``size`` cut short - and the rest of that datagram is gone, since a UDP message cannot be
        re-read in pieces. A timeout with nothing queued is ``(None, 0, False, "timeout")``, exactly
        like a stream's, and a datagram longer than the cap that was dropped never appears here."""
        size = params.get("size")
        deadline = time.monotonic() + self._timeout(params)
        with self._cond:
            while True:
                if self._datagrams:
                    datagram = self._datagrams.popleft()
                    self._queued -= len(datagram)
                    if size is not None and len(datagram) > size:
                        return datagram[:size], size, True, None  # truncated: the tail is discarded
                    return datagram, len(datagram), False, None
                if self._failed is not None:
                    return None, 0, False, self._failed
                remaining = deadline - time.monotonic()
                if remaining <= 0:  # decided under the lock, on the queue just checked
                    return None, 0, False, "timeout"
                self._cond.wait(timeout=remaining)

    def flush(self) -> tuple[bool, int, str | None]:
        """Discard every queued datagram and return ``(ok, count, err_or_None)``, count = bytes
        discarded."""
        with self._lock:
            if self._failed is not None:
                return False, 0, self._failed
            count = self._queued
            self._datagrams.clear()
            self._queued = 0
            self._cond.notify_all()
        return True, count, None

    def _timeout(self, params: dict[str, Any]) -> float:
        """The per-call ``timeout``, or the open-time one, for UDP (packet 18b).

        NaN and infinity are both greater than 0 but can never be met, so they are schedule bugs that
        raise naming the field - the same rule the driver applies when the Device is opened."""
        value = params.get("timeout", self.params["timeout"])
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"field 'timeout' must be greater than 0 and finite, got {value}")
        return float(value)

    def _send(self, datagram: bytes, seconds: float) -> int:
        """Send one whole datagram to the connected peer, waiting at most ``seconds``.

        A socket still not writable when the deadline is up is this call's timeout; an ICMP
        port-unreachable from a peer that is not listening yet is :class:`_Refused`, which fails only
        this call; an EMSGSIZE from a payload too big for a datagram is :class:`_Oversize`, which
        fails only this call too; anything else means the socket is gone."""
        if not select.select([], [self.sock], [], seconds)[1]:
            raise _SendTimeout("write timeout")
        try:
            return self.sock.send(datagram)
        except (ConnectionRefusedError, ConnectionResetError) as exc:
            raise _Refused(REFUSED_TEXT) from exc
        except OSError as exc:
            if exc.errno == errno.EMSGSIZE:  # too big for a datagram: the call, not the device
                raise _Oversize(str(exc)) from exc
            raise _TransportClosed(transport_error_text(exc)) from exc

    def write(self, params: dict[str, Any]) -> tuple[bool, int, str | None]:
        """Send ``data`` as one datagram and return ``(ok, count, err_or_None)``.

        One datagram per call, no terminator appended: ``count`` is the bytes of that datagram. A
        payload no datagram can hold is refused before the socket is touched, naming the size and the
        65507-byte limit; a send that cannot go out within ``timeout`` fails only this call; a refused
        peer (the peer is not listening yet) fails only this call too, because a UDP socket has no
        connection to lose. Any other send error is a dead socket and fails the device, like a stream.
        """
        with self._lock:
            failed = self._failed
            refused, self._refused = self._refused, False
        if failed is not None:
            return False, 0, failed
        if refused:
            return False, 0, REFUSED_TEXT  # as a pending socket error would: nothing goes out
        payload = self._to_bytes(params["data"])
        if len(payload) > MAX_DATAGRAM_BYTES:
            return False, 0, (
                f"datagram of {len(payload)} bytes exceeds the {MAX_DATAGRAM_BYTES}-byte UDP limit"
            )
        deadline = time.monotonic() + self._timeout(params)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, 0, "timeout"
        try:
            sent = self._send(payload, remaining)
        except _SendTimeout:
            return False, 0, "timeout"
        except _Refused as exc:
            return False, 0, str(exc)  # the peer is not listening: this call only, not the device
        except _Oversize as exc:
            return False, 0, str(exc)  # too big for a datagram: this call only, not the device
        except _TransportClosed as exc:
            self._fail(str(exc))
            return False, 0, str(exc)
        return True, sent, None

    def _close_transport(self) -> None:
        """Close the socket."""
        self.sock.close()


class UdpDriver(Driver):
    """Opens and closes UDP Devices (DeviceType.UDP)."""

    name = "UDP"
    accepts_receive_buffer_bytes = True  # 17a: the reader thread caps its queue from the config
    fields = {
        "host": ((str,), True),  # the peer's host, e.g. "127.0.0.1" or a hostname
        "port": ((int,), True),  # the remote port to talk to, 1-65535
        "localPort": ((int,), True),  # the local port to bind; 0 lets the OS pick one
        "timeout": ((int, float), True),  # read and write timeout in seconds
    }

    # UDP has no terminators (one datagram is one message), so readTerm/writeTerm are not fields this
    # Device Type knows: passing one is a schedule bug that raises naming it.
    read_fields = UDP_READ_FIELDS
    write_fields = UDP_WRITE_FIELDS

    def open(self, params: dict[str, Any],
             receive_buffer_bytes: int = DEFAULT_RECEIVE_BUFFER_BYTES) -> _UdpDevice:
        """Validate ``params`` and open a UDP socket. Raises :class:`ValueError` naming any bad field
        before the socket is created.

        The socket binds ``localPort`` first and then connects to ``host:port``, which is what limits
        ``recv`` to that peer's datagrams and lets ``send`` take no address."""
        validated = validate_params(self, params)
        port = int(validated["port"])
        if not 1 <= port <= 65535:
            raise ValueError(f"field 'port' must be between 1 and 65535, got {validated['port']}")
        local_port = int(validated["localPort"])
        if not 0 <= local_port <= 65535:
            raise ValueError(
                f"field 'localPort' must be between 0 and 65535, got {validated['localPort']}"
            )
        if not math.isfinite(validated["timeout"]) or validated["timeout"] <= 0:
            raise ValueError(
                f"field 'timeout' must be greater than 0 and finite, got {validated['timeout']}"
            )
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", local_port))
        sock.connect((validated["host"], port))  # connect for the peer filter, not for a connection
        device = _UdpDevice(sock, validated, receive_buffer_bytes)
        device.start_reader()  # packet 17a: the reader runs from open to close
        return device

