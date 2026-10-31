"""The TCP Device driver (packets 16a, 17a, 17b).

A raw socket stream - e.g. a LAN instrument taking SCPI on port 5025, or a UUT debug port. It
connects with :func:`socket.create_connection` and validates its params generically through the
shared registry validator before touching the network. The reader, the capped buffer, the
overflow, the failure and the warning logic are shared with the other background-buffered Device
Types in :mod:`ate.devices.stream`; this module only says what a socket is."""

from __future__ import annotations

import select
import socket
from typing import Any

from .registry import DEFAULT_RECEIVE_BUFFER_BYTES, Driver, validate_params
from .stream import (
    POLL_SECONDS,
    READ_CHUNK,
    READ_FIELDS,
    WRITE_FIELDS,
    _SendTimeout,
    _StreamDevice,
    _TransportClosed,
    transport_error_text,
)


class _TcpDevice(_StreamDevice):
    """Opaque handle returned by :meth:`TcpDriver.open`: a socket behind the shared stream logic.

    Lua sees it as userdata and only ever passes it back to ``read``/``write``/``close``. The
    buffer, the wait, the overflow and the failure are all in ``ate.devices.stream._StreamDevice``;
    what is TCP-specific is these three hooks."""

    def __init__(self, sock: socket.socket, params: dict[str, Any],
                 receive_buffer_bytes: int = DEFAULT_RECEIVE_BUFFER_BYTES) -> None:
        super().__init__(params, TcpDriver, receive_buffer_bytes)
        self.sock = sock

    def describe(self) -> str:
        """A one-line description of this device for log lines (host and port)."""
        return f"TCP {self.params['host']}:{self.params['port']}"

    def _recv(self) -> bytes | None:
        """Poll the socket for one chunk, or return None if nothing arrived within the poll time.

        An empty read is the peer closing: that is the end of the stream, so it is reported as a
        closed transport rather than as no data."""
        try:
            ready, _, _ = select.select([self.sock], [], [], POLL_SECONDS)
        except (OSError, ValueError) as exc:  # closed under us by close() (fd -1)
            raise _TransportClosed(str(exc)) from exc
        if not ready:
            return None
        try:
            chunk = self.sock.recv(READ_CHUNK)
        except OSError as exc:
            raise _TransportClosed(transport_error_text(exc)) from exc
        if not chunk:  # the peer closed: an orderly EOF
            raise _TransportClosed("connection closed by peer")
        return chunk

    def _send(self, chunk: bytes, seconds: float) -> int:
        """Send as much of ``chunk`` as the socket takes now.

        A socket still not writable when the deadline is up is this call's timeout; a send error
        (reset, broken pipe) is a dead connection."""
        if not select.select([], [self.sock], [], seconds)[1]:
            raise _SendTimeout("write timeout")
        try:
            return self.sock.send(chunk)
        except OSError as exc:
            raise _TransportClosed(transport_error_text(exc)) from exc

    def _close_transport(self) -> None:
        """Close the socket."""
        self.sock.close()


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

    # Per-call override fields for read/write (packet 16b) - the same for every streaming Device
    # Type, so they come from the shared stream module. All optional: an absent field falls back to
    # the open-time configuration. A field this Device Type doesn't know (a typo such as "buad"), a
    # missing required field ("data" for write), or a mistyped value always raises and names the field.
    read_fields = READ_FIELDS
    write_fields = WRITE_FIELDS

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
