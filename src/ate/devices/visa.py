"""The VISA Device driver (packet 19).

GPIB, USBTMC and VXI-11 instruments, addressed by a VISA resource string and reached through
whatever backend pyvisa finds: NI-VISA when it is installed, the bundled pyvisa-py otherwise. ATE
never ships NI-VISA, so on a machine without it every VISA Device runs on pyvisa-py.

VISA is the exception among the Device Types: it has **no background reader and no receive
buffer**. An instrument only speaks when it is queried, and polling it with reads that have nothing
pending makes SCPI instruments log *Query UNTERMINATED* (-420) and ties up the bus. So ``read``
reads on demand - up to ``size`` bytes, or until ``readTerm``, or until the timeout - ``flush``
discards nothing and returns ``count = 0`` and never sends an instrument clear, and a read or write
that fails fails **that call only**: nothing saw a break between calls, so the Device is not marked
failed and no device warning is queued.

ATE handles the terminators itself. The resource is opened with pyvisa's own ``read_termination``
and ``write_termination`` off, and every call moves raw bytes, so the terminator the schedule gave
is appended and stripped by ATE, never by pyvisa. Reads are exact: each backend read asks for no
more than the call still needs and stops at the terminator (``VI_ATTR_TERMCHAR``), so nothing is
read past what the call returns and ATE keeps no bytes between calls. ``VI_ATTR_SUPPRESS_END_EN``
is cleared on open so a backend that got bytes but no terminator hands them back rather than
dropping them.

On pyvisa-py's TCPIP SOCKET session, which honours neither a write timeout nor a read deadline
while bytes keep arriving, ATE does the socket I/O itself (see :func:`_socket_session`); every
other backend and session goes through ``viRead`` and ``write_raw``.

pyvisa's timeout is in milliseconds; ATE's is in seconds, and it is converted per open and per call.
"""

from __future__ import annotations

import math
import os
import select
import socket
import time
from typing import Any, Callable

import pyvisa
from pyvisa import constants, errors

from .registry import Driver, validate_params
from .stream import READ_FIELDS, WRITE_FIELDS

#: Bytes one on-demand read asks the instrument for when the schedule gave no ``size``.
READ_CHUNK = 4096

#: The process-wide pyvisa ResourceManager, built on the first open and reused after that.
_manager: Any = None


def _default_manager() -> Any:
    """pyvisa's own backend choice, falling back to the bundled pyvisa-py.

    ``ResourceManager()`` picks NI-VISA when an IVI library is present and raises when there is
    none - ``OSError`` from the ctypes wrapper when the library cannot be loaded, ``ValueError``
    when no backend can be imported - and ``"@py"`` is pyvisa-py, which covers TCPIP and USBTMC.
    GPIB still needs NI-VISA (or gpib-ctypes); ATE ships neither.
    """
    try:
        return pyvisa.ResourceManager()
    except (OSError, ValueError):
        return pyvisa.ResourceManager("@py")


def get_resource_manager(factory: Callable[[], Any] | None = None) -> Any:
    """The ResourceManager every VISA open uses: created lazily, then cached for the process.

    ``factory`` replaces it outright (the tests' scripted fake); an injected one is not cached, so
    each driver built with it gets its own.
    """
    global _manager
    if factory is not None:
        return factory()
    if _manager is None:
        _manager = _default_manager()
    return _manager


def _milliseconds(seconds: float) -> int:
    """ATE's seconds as pyvisa's milliseconds, never 0 - pyvisa reads 0 as "no timeout at all"."""
    return max(1, int(round(float(seconds) * 1000)))


def _check_connected(resource: Any) -> None:
    """Raise if a pyvisa-py SOCKET resource's TCP connect failed.

    pyvisa-py's connect waits only for the socket to be ready, and a refused connect is ready too, so
    the open "succeeds" and the refusal would surface at the first read. The socket's pending error
    is the connect's result; any other backend reports a failed connect from ``open_resource``.
    """
    session = _socket_session(resource)
    if session is None:
        return
    code = session.interface.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
    if code:
        raise OSError(code, os.strerror(code))


def _error_text(exc: Exception) -> str:
    """A VISA error as a short, readable string: ``VI_ERROR_CONN_LOST: The connection ... lost``."""
    abbreviation = getattr(exc, "abbreviation", "")
    description = getattr(exc, "description", "")
    return f"{abbreviation}: {description}" if abbreviation and description else (abbreviation or str(exc))


#: What one backend read says about the reply: more may follow, the instrument signalled END, or
#: the instrument hung up.
_MORE, _END, _EOF = object(), object(), object()


class _ReadTimeout(Exception):
    """Nothing arrived on the socket before the deadline."""


def _socket_session(resource: Any) -> Any:
    """The pyvisa-py TCPIP SOCKET session under ``resource``, or None for any other backend or session.

    pyvisa-py's SOCKET session does not honour a deadline: its write waits for the socket with no
    timeout at all, and its read checks the timeout only when the line goes quiet, so a reply
    dribbling in faster than its poll interval holds a read far past it. On that session ATE does
    the socket I/O itself, with its own deadline.

    This couples ATE to pyvisa-py internals (as of pyvisa-py 0.8 / pyvisa 1.16.2,
    ``pyvisa_py/tcpip.py``): the backend keeps its sessions in ``visalib.sessions`` keyed by the
    resource's session handle, and a ``TCPIPSocketSession`` keeps its connected socket in
    ``interface`` and the bytes it received past a terminator in ``_pending_buffer``. If any of
    that is missing - NI-VISA, a USB or VXI-11 session, a pyvisa-py that changed - this returns None
    and ATE falls back to the VISA calls (``viRead``, ``write_raw``).
    """
    try:
        session = resource.visalib.sessions[resource.session]
    except Exception:  # noqa: BLE001 - NI-VISA has no ``sessions``; a closed resource has no session
        return None
    if not isinstance(getattr(session, "interface", None), socket.socket):
        return None
    if not isinstance(getattr(session, "_pending_buffer", None), bytearray):
        return None
    return session


def _cut(data: bytes | bytearray, want: int, term_byte: bytes) -> bytes:
    """The first ``want`` bytes of ``data``, or fewer: through the first ``term_byte`` if there is one."""
    data = bytes(data[:want])
    end = data.find(term_byte) if term_byte else -1
    return data if end < 0 else data[:end + 1]


def _wait_readable(sock: socket.socket, seconds: float) -> bool:
    """Block until ``sock`` has something to read (or hung up), for at most ``seconds``."""
    return bool(select.select([sock], [], [], seconds)[0])


def _socket_read(session: Any, want: int, term_byte: bytes, seconds: float) -> tuple[bytes, object]:
    """One read on a pyvisa-py SOCKET session's socket: ``(chunk, status)``, raising ``_ReadTimeout``
    when nothing arrives within ``seconds``.

    Byte-exact: at most ``want`` bytes, and none past ``term_byte``. The socket is peeked first, so
    only what the call keeps is taken off it and the rest of the reply stays in the socket. Bytes
    pyvisa-py itself already took off the socket come first. One wait covers the whole of
    ``seconds``, so an instrument with nothing to say sees a single read, not a poll.
    """
    pending = session._pending_buffer
    if pending:
        chunk = _cut(pending, want, term_byte)
        del pending[:len(chunk)]
        return chunk, _MORE
    sock = session.interface
    if not _wait_readable(sock, seconds):
        raise _ReadTimeout()
    peeked = sock.recv(want, socket.MSG_PEEK)
    if not peeked:
        return b"", _EOF
    return sock.recv(len(_cut(peeked, want, term_byte))), _MORE


def _socket_write(sock: socket.socket, payload: bytes, seconds: float) -> tuple[bool, int, str | None]:
    """Send ``payload`` on ``sock`` within ``seconds``: ``(ok, count, err_or_None)``, ``count`` the
    bytes that went out. The socket's own timeout bounds each send and is put back afterwards."""
    deadline = time.monotonic() + seconds
    view = memoryview(payload)
    sent = 0
    previous = sock.gettimeout()
    try:
        while sent < len(payload):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, sent, "timeout"
            sock.settimeout(remaining)
            try:
                sent += sock.send(view[sent:])
            except TimeoutError:
                return False, sent, "timeout"
    finally:
        sock.settimeout(previous)
    return True, sent, None


class _VisaDevice:
    """Opaque handle for an instrument read on demand: no reader thread, no buffer.

    Lua sees it as userdata and only ever passes it back to ``read``/``write``/``flush``/``close``.
    ``driver`` is the driver class that opened it, which is what the ATE wrappers validate the
    per-call fields against. There is no ``_leftover``: every read asks the backend for exactly what
    the call still needs and stops at the terminator, so nothing is ever taken past what the call
    returns - the rest of a reply stays the instrument's (or the backend's) to give.
    """

    def __init__(self, resource: Any, params: dict[str, Any], driver: type[Driver]) -> None:
        self.resource = resource
        self.params = params
        self.driver = driver
        self._closed = False

    def describe(self) -> str:
        """A one-line description of this device for log lines (the resource as the schedule gave it)."""
        return f"VISA {self.params['resource']}"

    @staticmethod
    def _to_bytes(value: str | bytes) -> bytes:
        """Payloads and terminators arrive as bytes from Lua; a Python ``str`` is UTF-8 encoded."""
        return value.encode("utf-8") if isinstance(value, str) else bytes(value)

    def read(self, params: dict[str, Any]) -> tuple[bytes | None, int, bool, str | None]:
        """Read on demand and return ``(data, count, partial, err_or_None)``.

        Ends on ``readTerm`` (stripped), on ``size`` bytes, or on an END from an instrument whose
        bus has one, whichever comes first; if none is reached before ``timeout``, the bytes that did
        arrive are returned as ``partial = true`` and nothing arriving at all is the
        ``err = "timeout"`` failure. A VISA error that is not a timeout fails this call only, with
        the error's name as ``err``.

        Each backend read asks for no more than the call still needs and stops at the terminator's
        last byte, so a read never takes bytes past what it returns, and none is issued once the
        call's goal is met. A read with nothing pending is one blocking read for the whole timeout:
        VISA never polls.
        """
        size = params.get("size")
        term = self._term(params, "readTerm")
        deadline = time.monotonic() + self._timeout(params)
        session = _socket_session(self.resource)
        exact = session is not None or self._set_termchar(term)
        # A raw socket has no END: pyvisa-py's "success" (and NI-VISA's with SUPPRESS_END off) only
        # means a TCP segment ended, which says nothing about where the reply ends.
        has_end = session is None and getattr(self.resource, "resource_class", "") != "SOCKET"
        buffer = bytearray()
        status = _MORE
        while True:
            if term and buffer.endswith(term):  # exact reads: a terminator can only be at the end
                data = bytes(buffer[:-len(term)])
                return data, len(data), False, None
            if size is not None and len(buffer) >= size:
                return bytes(buffer), len(buffer), False, None
            if status is _END and has_end:
                return bytes(buffer), len(buffer), False, None
            if status is _EOF:  # the instrument hung up: what came before it is this call's partial
                if buffer:
                    break
                return None, 0, False, "connection closed by the instrument"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            want = READ_CHUNK if size is None else size - len(buffer)
            if term and not exact:
                want = 1  # the backend cannot stop at the terminator, so take one byte at a time
            try:
                if session is not None:
                    chunk, status = _socket_read(session, want, term[-1:], remaining)
                else:
                    chunk, status = self._visa_read(want, remaining)
            except _ReadTimeout:
                break
            except errors.VisaIOError as exc:
                if exc.error_code == constants.StatusCode.error_timeout:
                    break
                return None, 0, False, _error_text(exc)
            except OSError as exc:  # the instrument went away: this call fails, the Device does not
                return None, 0, False, str(exc)
            except Exception as exc:  # noqa: BLE001 - e.g. a torn-down session: still this call's failure, not a crash
                return None, 0, False, f"{type(exc).__name__}: {exc}"
            buffer.extend(chunk)
        if buffer:
            return bytes(buffer), len(buffer), True, None
        return None, 0, False, "timeout"

    def _visa_read(self, want: int, seconds: float) -> tuple[bytes, object]:
        """One viRead of at most ``want`` bytes, waiting at most ``seconds``: ``(chunk, status)``.

        The low-level call, not ``read_raw``: ``read_raw`` keeps reading while the backend reports
        a full count (so ``size`` would read on past the limit) and throws away everything it had if
        a later read fails. ``status`` is ``_END`` when the backend reported END.
        """
        self.resource.timeout = _milliseconds(seconds)
        with self.resource.ignore_warning(constants.StatusCode.success_max_count_read,
                                          constants.StatusCode.success_device_not_present):
            chunk, status = self.resource.visalib.read(self.resource.session, want)
        return bytes(chunk), _END if status == constants.StatusCode.success else _MORE

    def _set_termchar(self, term: bytes) -> bool:
        """Tell the backend where a reply ends so a read stops at the terminator; True if it took it.

        A backend that refuses the attribute (or a resource type that has no terminator) cannot stop
        at the terminator, and :meth:`read` then reads a byte at a time rather than read past it.
        """
        setter = getattr(self.resource, "set_visa_attribute", None)
        if setter is None:
            return not term
        try:
            setter(constants.ResourceAttribute.termchar_enabled,
                   constants.VI_TRUE if term else constants.VI_FALSE)
            if term:
                setter(constants.ResourceAttribute.termchar, term[-1])
        except errors.VisaIOError:
            return not term
        return True

    def flush(self) -> tuple[bool, int, str | None]:
        """VISA has no receive buffer, so flush discards nothing: ``(True, 0, None)``.

        It never calls ``clear()``: an instrument clear is not what ATE's flush means, and one sent
        unprompted would reset the instrument the schedule is talking to.
        """
        return True, 0, None

    def take_overflow(self) -> tuple[bool, int]:
        """No buffer means no overflow, ever."""
        return False, 0

    def write(self, params: dict[str, Any]) -> tuple[bool, int, str | None]:
        """Send ``data`` with ``writeTerm`` appended and return ``(ok, count, err_or_None)``.

        ``count`` is the bytes sent, terminator included. A write that cannot send everything within
        ``timeout`` fails with ``err = "timeout"``, and only this call fails.

        pyvisa-py's TCPIP SOCKET write waits for the socket with no timeout at all, so on that
        session ATE sends on the session's socket itself with its own deadline, and ``count`` is
        what went out before it. Every other backend gets ``write_raw`` with the timeout set on the
        resource, which NI-VISA honours (``VI_ATTR_TMO_VALUE``); its timeout error carries no count.
        """
        payload = self._to_bytes(params["data"]) + self._term(params, "writeTerm")
        seconds = self._timeout(params)
        session = _socket_session(self.resource)
        if session is not None:
            try:
                return _socket_write(session.interface, payload, seconds)
            except OSError as exc:
                return False, 0, str(exc)
        self.resource.timeout = _milliseconds(seconds)
        try:
            count = self.resource.write_raw(payload)
        except errors.VisaIOError as exc:
            if exc.error_code == constants.StatusCode.error_timeout:
                return False, 0, "timeout"
            return False, 0, _error_text(exc)
        except OSError as exc:
            return False, 0, str(exc)
        except Exception as exc:  # noqa: BLE001 - whatever the backend raised fails this call only
            return False, 0, f"{type(exc).__name__}: {exc}"
        return True, count, None

    def close(self) -> None:
        """Close the resource. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        try:
            self.resource.close()
        except (errors.VisaIOError, OSError):
            pass  # already gone; there is nothing left to release

    def _timeout(self, params: dict[str, Any]) -> float:
        """The per-call ``timeout`` if given, else the open-time one.

        NaN and infinity can never be met and 0 or less would be a silent instant timeout, so they
        are schedule bugs that raise naming the field - the rule the driver applies at open."""
        value = params.get("timeout", self.params["timeout"])
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"field 'timeout' must be a positive finite number, got {value!r}")
        return float(value)

    def _term(self, params: dict[str, Any], field: str) -> bytes:
        """The per-call terminator ``field`` if given, else the open-time one, as bytes."""
        return self._to_bytes(params.get(field, self.params[field]))


class VisaDriver(Driver):
    """Opens and closes VISA Devices (DeviceType.VISA), through pyvisa's available backend."""

    name = "VISA"
    accepts_receive_buffer_bytes = False  # no background reader, so there is no buffer to cap
    fields = {
        "resource": ((str,), True),  # e.g. "GPIB0::5::INSTR" or "TCPIP0::127.0.0.1::5025::SOCKET"
        "timeout": ((int, float), True),  # read and write timeout in seconds, finite and > 0
        "readTerm": ((str, bytes), True),  # byte string stripped from reads; "" means none
        "writeTerm": ((str, bytes), True),  # byte string appended to writes; "" means none
    }

    # The same per-call overrides as the streaming Device Types: all optional, each falling back to
    # the open-time configuration.
    read_fields = READ_FIELDS
    write_fields = WRITE_FIELDS

    def __init__(self, manager_factory: Callable[[], Any] | None = None) -> None:
        #: Injectable for tests: a factory for the resource manager in place of pyvisa's hunt for a
        #: backend. Without it, the process-wide manager is used.
        self._manager_factory = manager_factory

    def open(self, params: dict[str, Any]) -> _VisaDevice:
        """Validate ``params`` and open the resource.

        Raises :class:`ValueError` naming any bad field before anything is opened, and
        :class:`OSError` naming the resource for anything that stops it opening - an unreachable or
        refusing instrument, a resource string the backend cannot parse, a resource class with no
        backend installed - which the host reports through ``onError``. Whatever pyvisa raised, a
        resource that did open before the failure is closed again.
        """
        validated = validate_params(self, params)
        timeout = float(validated["timeout"])
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"field 'timeout' must be a positive finite number, got {validated['timeout']!r}")

        manager = get_resource_manager(self._manager_factory)
        name = validated["resource"]
        milliseconds = _milliseconds(timeout)
        try:
            # open_timeout bounds pyvisa-py's TCP connect, which would otherwise wait 10 s.
            resource = manager.open_resource(name, open_timeout=milliseconds)
        except Exception as exc:  # noqa: BLE001 - pyvisa-py raises bare Exception, ValueError, ...
            raise OSError(f"{name}: {_error_text(exc)}") from exc
        try:
            if not hasattr(resource, "write_raw"):
                raise OSError("not a message-based resource")
            resource.timeout = milliseconds
            resource.read_termination = None  # ATE appends and strips the terminators, pyvisa does not
            resource.write_termination = None
            _check_connected(resource)
            # SUPPRESS_END (pyvisa-py's SOCKET default) makes an unterminated viRead wait out the
            # whole timeout and then throw away the bytes it had; with it off the bytes come back.
            try:
                resource.set_visa_attribute(constants.ResourceAttribute.suppress_end_enabled, constants.VI_FALSE)
            except (errors.VisaIOError, AttributeError):
                pass
        except Exception as exc:  # noqa: BLE001 - whatever failed, the half-opened resource is released
            try:
                resource.close()
            except Exception:  # noqa: BLE001 - already gone
                pass
            raise OSError(f"{name}: {_error_text(exc)}") from exc
        return _VisaDevice(resource, validated, type(self))

    def close(self, device: Any) -> None:
        """Release the resource; the handle's own ``close`` is idempotent."""
        device.close()
