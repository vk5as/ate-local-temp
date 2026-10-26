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
and ``write_termination`` off, and every call is a raw byte call (``write_raw``/``read_raw``), so
the terminator the schedule gave is appended and stripped by ATE, never by pyvisa. The one VISA
attribute ATE does set is where a reply ends (``VI_ATTR_TERMCHAR``), so a read stops at the
terminator instead of waiting out the whole timeout, and ``VI_ATTR_SUPPRESS_END_EN`` is cleared on
open so a backend that got bytes but no terminator hands them back rather than dropping them.

pyvisa's timeout is in milliseconds; ATE's is in seconds, and it is converted per open and per call.
"""

from __future__ import annotations

import math
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


def _error_text(exc: Exception) -> str:
    """A VISA error as a short, readable string: ``VI_ERROR_CONN_LOST: The connection ... lost``."""
    abbreviation = getattr(exc, "abbreviation", "")
    description = getattr(exc, "description", "")
    return f"{abbreviation}: {description}" if abbreviation and description else (abbreviation or str(exc))


class _VisaDevice:
    """Opaque handle for an instrument read on demand: no reader thread, no buffer.

    Lua sees it as userdata and only ever passes it back to ``read``/``write``/``flush``/``close``.
    ``driver`` is the driver class that opened it, which is what the ATE wrappers validate the
    per-call fields against. ``_leftover`` is what one read took past the terminator or past
    ``size``: there is no background buffer, but a read that pulled more than the schedule asked for
    must not lose the rest either.
    """

    def __init__(self, resource: Any, params: dict[str, Any], driver: type[Driver]) -> None:
        self.resource = resource
        self.params = params
        self.driver = driver
        self._leftover = bytearray()
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

        Ends on ``readTerm`` (stripped) or on ``size`` bytes, whichever comes first; if neither is
        reached before ``timeout``, the bytes that did arrive are returned as ``partial = true`` and
        nothing arriving at all is the ``err = "timeout"`` failure. A VISA error that is not a
        timeout fails this call only, with the error's name as ``err``.
        """
        size = params.get("size")
        term = self._term(params, "readTerm")
        deadline = time.monotonic() + self._timeout(params)
        self._set_termchar(term)
        buffer = self._leftover
        while True:
            data = self._take_complete(term, size)
            if data is not None:
                return data, len(data), False, None
            remaining = deadline - time.monotonic()
            if remaining <= 0:  # decided on the buffer we just looked at, same as the streams
                if buffer:
                    data = bytes(buffer)
                    del buffer[:]
                    return data, len(data), True, None
                return None, 0, False, "timeout"
            self.resource.timeout = _milliseconds(remaining)
            want = READ_CHUNK if size is None else size - len(buffer)
            try:
                chunk = self.resource.read_raw(want)
            except errors.VisaIOError as exc:
                if exc.error_code == constants.StatusCode.error_timeout:
                    if buffer:
                        data = bytes(buffer)
                        del buffer[:]
                        return data, len(data), True, None
                    return None, 0, False, "timeout"
                return None, 0, False, _error_text(exc)
            except OSError as exc:  # the instrument went away: this call fails, the Device does not
                return None, 0, False, str(exc)
            if chunk:
                buffer.extend(chunk)

    def _take_complete(self, term: bytes, size: int | None) -> bytes | None:
        """Take a complete read from what is already in hand - up to a terminator or ``size`` bytes -
        or return None if neither is there yet (the caller holds no lock: there is no reader)."""
        buffer = self._leftover
        end = len(buffer) if size is None else min(len(buffer), size + len(term))
        idx = buffer.find(term, 0, end) if term else -1
        if idx >= 0:
            data = bytes(buffer[:idx])
            del buffer[:idx + len(term)]
            return data
        if size is not None and len(buffer) >= size:
            data = bytes(buffer[:size])
            del buffer[:size]
            return data
        return None

    def _set_termchar(self, term: bytes) -> None:
        """Tell the backend where a reply ends so a read stops at the terminator.

        A backend that refuses the attribute (or a resource type that has no terminator) just reads
        unterminated: the deadline still bounds the read, and the terminator is found in the bytes
        that came back.
        """
        setter = getattr(self.resource, "set_visa_attribute", None)
        if setter is None:
            return
        try:
            setter(constants.ResourceAttribute.termchar_enabled,
                   constants.VI_TRUE if term else constants.VI_FALSE)
            if term:
                setter(constants.ResourceAttribute.termchar, term[-1])
        except errors.VisaIOError:
            pass

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

        ``count`` is the bytes pyvisa sent, terminator included. pyvisa-py's socket write is
        blocking, so a send that cannot complete surfaces as a VISA error rather than as
        ``err = "timeout"``; NI-VISA honours the timeout attribute and reports it as one. Either
        way only this call fails.
        """
        payload = self._to_bytes(params["data"]) + self._term(params, "writeTerm")
        self.resource.timeout = _milliseconds(self._timeout(params))
        try:
            count = self.resource.write_raw(payload)
        except errors.VisaIOError as exc:
            return False, 0, _error_text(exc)
        except OSError as exc:
            return False, 0, str(exc)
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
        """The per-call ``timeout`` if given, else the open-time one."""
        return float(params.get("timeout", self.params["timeout"]))

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
        :class:`OSError` when the instrument cannot be reached - an open failure like a refused TCP
        connect, which the host reports through ``onError``.
        """
        validated = validate_params(self, params)
        timeout = float(validated["timeout"])
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"field 'timeout' must be a positive finite number, got {validated['timeout']!r}")

        manager = get_resource_manager(self._manager_factory)
        try:
            resource = manager.open_resource(
                validated["resource"],
                timeout=_milliseconds(timeout),
                read_termination=None,  # ATE appends and strips the terminators, pyvisa does not
                write_termination=None,
            )
        except errors.VisaIOError as exc:
            raise OSError(_error_text(exc)) from exc

        device = _VisaDevice(resource, validated, type(self))
        # pyvisa-py's TCPIP SOCKET sets SUPPRESS_END, which makes an unterminated read wait out the
        # whole timeout and then throw away the bytes it already had. Clearing it is what lets a
        # read report the partial bytes that arrived before the deadline.
        try:
            resource.set_visa_attribute(constants.ResourceAttribute.suppress_end_enabled, constants.VI_FALSE)
        except (errors.VisaIOError, AttributeError):
            pass
        return device

    def close(self, device: Any) -> None:
        """Release the resource; the handle's own ``close`` is idempotent."""
        device.close()
