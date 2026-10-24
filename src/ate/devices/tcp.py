"""The TCP Device driver (packet 16a).

A raw socket stream — e.g. a LAN instrument taking SCPI on port 5025, or a UUT debug port. It
connects with :func:`socket.create_connection` and validates its params generically through the
shared registry validator before touching the network.
"""

from __future__ import annotations

import select
import socket
import time
from typing import Any

from .registry import Driver, validate_params


class _TcpDevice:
    """Opaque handle returned by :meth:`TcpDriver.open`.

    Lua sees it as userdata and only ever passes it back to ``read``/``write``/``close``; this
    packet implements the connect/close half. It carries the live socket so teardown can close it,
    a short description for log lines, and the read/write logic (packet 16b). Bytes received but not
    yet consumed by a read (leftover past a terminator) are carried to the next read."""

    def __init__(self, sock: socket.socket, params: dict[str, Any]) -> None:
        self.sock = sock
        self.params = params
        # Bytes received but not yet consumed by a read (leftover past a terminator); carried to the next read.
        self._leftover: bytes = b""

    def describe(self) -> str:
        """A one-line description of this device for log lines (host and port)."""
        return f"TCP {self.params['host']}:{self.params['port']}"

    def read(self, params: dict[str, Any]) -> tuple[Any, int, bool, str | None]:
        """Read from the socket and return ``(data, count, partial, err_or_None)``.

        Reads up to ``size`` bytes (or all of it when none is given), stopping at ``readTerm`` or the
        timeout — whichever comes first. Terminators are stripped before returning so ``count`` is the
        number of data bytes received. Returns:

        * ``(data, count, False, None)`` — complete read (terminator seen or ``size`` reached).
        * ``(data, count, True, None)``  — timed out after some bytes but not a full read (partial).
        * ``(None, 0, False, "timeout")`` — timed out with no bytes.
        * ``(None, 0, False, err)``       — an unexpected socket failure (port gone, reset, ...).

        Every socket error is caught and returned as ``err`` so it surfaces to the schedule as a read
        result table rather than raising; only a genuine bug would escape here."""
        sock = self.sock
        size = params.get("size")
        term = self._effective_term(params.get("readTerm"))  # per-call override or open-time config
        to = self._effective_timeout(params.get("timeout"), self.params)  # per-call override or open-time config
        deadline = time.monotonic() + to if to is not None else None
        data = bytearray(self._leftover)
        
        while True:
            # Check if deadline reached with current data
            remaining = None if deadline is None else (deadline - time.monotonic())
            if remaining is None or remaining <= 0:
                # Timeout with what we have: if we have data, it's partial; if no data, it's timeout
                if data:
                    self._leftover = b""
                    return bytes(data), len(data), True, None  # received some bytes but didn't finish
                return None, 0, False, "timeout"  # nothing arrived and time ran out
            r, _, _ = select.select([sock], [], [], remaining)  # blocks until data or the timeout elapses
            if not r:
                # select timed out - if we have data, it's partial; otherwise timeout with no-bytes
                if data:
                    self._leftover = bytes(data)
                    return bytes(data), len(data), True, None
                return None, 0, False, "timeout"  # nothing arrived
            chunk = sock.recv(4096)
            if not chunk:  # the peer closed cleanly; whatever we have is a complete read
                self._leftover = b""
                remaining = None if deadline is None else (deadline - time.monotonic())
                if remaining is None or remaining <= 0:
                    self._leftover = b""
                    return bytes(data), len(data), False, None
                self._leftover = bytes(data)
                return bytes(data), len(data), True, None
            data.extend(chunk)
            
            # Check if we now have a complete read
            if term and term in data:
                idx = data.index(term)
                self._leftover = bytes(data[idx + len(term):])
                return bytes(data[:idx]), idx, False, None
            if size is not None and len(data) >= size:
                # Size reached: return only up to size; anything past size stays in buffer for next read
                if len(data) > size:
                    self._leftover = bytes(data[size:])
                else:
                    self._leftover = b""
                return bytes(data[:size]), size, False, None

    @staticmethod
    def _effective_timeout(override: Any, params: dict[str, Any]) -> float | None:
        """The timeout to use: the per-call override if given, else the open-time configuration."""
        if override is not None:
            return float(override)
        return params.get("timeout")  # open-time connect/read timeout in seconds (or None = block)

    @staticmethod
    def _to_bytes(value: Any) -> bytes:
        """Coerce a byte payload to ``bytes``.

        A value coming straight from Python is already bytes; one arriving from a Lua schedule action
        is a :class:`str` (lupa hands Lua strings back as text), so it is encoded — utf-8 first, latin-1
        for raw content that isn't valid utf-8. Anything else is a genuine argument error."""
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        if isinstance(value, str):
            try:
                return value.encode("utf-8")
            except UnicodeEncodeError:
                return value.encode("latin-1")
        raise TypeError(f"expected a string or bytes, got {type(value).__name__}")

    def _effective_term(self, override: Any) -> bytes:
        """The terminator to strip/append: the per-call override if given, else the open-time config."""
        term = override if override is not None else self.params.get("readTerm")
        return self._to_bytes(term) if term else b""

    def write(self, params: dict[str, Any]) -> tuple[bool, int, str | None]:
        """Write ``data`` (with ``writeTerm`` appended when set) and return ``(ok, count, err_or_None)``.

        Sends the whole payload within ``timeout`` seconds; ``count`` is the number of bytes actually
        sent — on success that includes the terminator, on a timeout it is what went out before giving up."""
        sock = self.sock
        data = self._to_bytes(params["data"])  # required by write_fields; a Lua string is encoded here
        payload = data + self._effective_term(params.get("writeTerm"))  # terminator appended to the raw bytes
        to = self._effective_timeout(params.get("timeout"), self.params)  # per-call override or open-time config
        total_sent = 0
        prev = sock.gettimeout()
        try:
            if to is not None:
                sock.settimeout(float(to))  # block the send up to the timeout so a busy peer fails cleanly
            while payload[total_sent:]:
                n = sock.send(payload[total_sent:])  # may return fewer than asked; loop until it drains
                if n is None:  # only a non-blocking socket returns None; we are always blocking, so this never happens
                    break
                total_sent += n
            return True, len(payload), None
        except (socket.timeout, OSError) as exc:
            return False, total_sent, str(exc)
        finally:
            sock.settimeout(prev)  # restore the socket's timeout so a timed write doesn't poison later reads

    def close(self) -> None:
        """Close the socket. Safe to call twice — a second close is a no-op (packet 16a)."""
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
        "size": ((int,), False),  # optional byte limit; None means read until terminator/timeout
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
