"""The TCP Device driver (packet 16a).

A raw socket stream — e.g. a LAN instrument taking SCPI on port 5025, or a UUT debug port. It
connects with :func:`socket.create_connection` and validates its params generically through the
shared registry validator before touching the network.
"""

from __future__ import annotations

import socket
from typing import Any

from .registry import Driver, validate_params


class _TcpDevice:
    """Opaque handle returned by :meth:`TcpDriver.open`.

    Lua sees it as userdata and only ever passes it back to ``read``/``write``/``close``; this
    packet implements the connect/close half. It carries the live socket so teardown can close it,
    and a short description for the run-end warning."""

    def __init__(self, sock: socket.socket, params: dict[str, Any]) -> None:
        self.sock = sock
        self.params = params
        # Bytes received but not yet consumed by a read (leftover past a terminator); carried to the next read.
        self._leftover: bytes = b""

    def describe(self) -> str:
        """A one-line description of this device for log lines (host and port)."""
        return f"TCP {self.params['host']}:{self.params['port']}"

    def close(self) -> None:
        """Close the socket. Safe to call twice — a second close is a no-op."""
        sock = self.sock
        try:
            sock.close()
        except OSError:
            # Already closed or a reset mid-close; nothing useful left to do.
            pass


class TcpDriver(Driver):
    """Opens and closes TCP Devices (DeviceType.TCP)."""

    name = "TCP"
    fields = {
        "host": ((str,), True),  # the instrument's host, e.g. "127.0.0.1" or a hostname
        "port": ((int, float), True),  # the port to connect to, e.g. 5025
        "timeout": ((int, float), True),  # connect timeout in seconds
        "readTerm": ((str, bytes), True),  # byte string stripped from reads; "" means none
        "writeTerm": ((str, bytes), True),  # byte string appended to writes; "" means none
    }

    # Per-call override fields for read/write (packet 16b). All optional — an absent field falls back
    # to the open-time configuration. They are a subset of ``fields``: host/port stay required only at
    # open, never here. A field this Device Type doesn't know (a typo such as "buad"), a missing
    # required field ("data" for write), or a mistyped value always raises and names the field.
    read_fields = {
        "size": ((int, float), False),  # optional byte limit; None means read until terminator/timeout
        "timeout": ((int, float), False),  # per-call timeout in seconds (falls back to open-time)
        "readTerm": ((str, bytes), False),  # per-call terminator stripped from data ("" = none)
    }
    write_fields = {
        "data": ((str, bytes), True),  # required; raw bytes sent (terminator appended separately)
        "writeTerm": ((str, bytes), False),  # per-call terminator appended to data ("" = none)
        "timeout": ((int, float), False),  # send timeout in seconds (falls back to open-time)
    }

    def open(self, params: dict[str, Any]) -> _TcpDevice:
        """Validate ``params`` and connect a TCP socket. Raises :class:`ValueError` naming any bad
        field before connecting."""
        validated = validate_params(self, params)
        host = validated["host"]
        port = int(validated["port"])  # normalise to an integer port for the address tuple
        sock = socket.create_connection((host, port), timeout=validated["timeout"])
        return _TcpDevice(sock, validated)
