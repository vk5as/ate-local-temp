"""The per-run Device registry and its driver abstraction (packet 16a).

A :class:`DeviceRegistry` maps each Device Type name to the driver class that opens it, and
tracks every device this run has opened so leftovers can be closed at run end. One registry is
owned by one LuaHost / Test Run; it is created when the host starts a run and torn down when it
ends (see :meth:`RunEngine.run`).

Drivers are intentionally small: each declares the fields its ``open`` params accept, validates
them generically, connects, and returns an opaque handle. The registry never knows how a driver
connects — only that it produced a handle to track until close.

With Device tracing on (packet 17c, app config ``deviceTrace.enabled``) the registry also wraps every
handle it opens in :class:`ate.devices.tracing.TracedDevice` and logs each open/read/write/close as
one ``DEBUG`` line (source ``device``) through the host's log sink.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Callable, ClassVar, Mapping, Optional

from .tracing import DEFAULT_TRACE_MAX_BYTES, DeviceTracer, device_label, elapsed_ms, handle_label, TracedDevice


def _is_number(value: Any) -> bool:
    """True for Lua numbers (int or float), but never booleans — a boolean is not a number."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


#: The per-device receive buffer cap in bytes (app config receiveBufferBytes, default 16 MiB).
DEFAULT_RECEIVE_BUFFER_BYTES = 16_777_216


def _matches(value: Any, types: tuple[type, ...]) -> bool:
    """Whether ``value`` has one of the accepted Python types.

    A field that accepts numbers rejects booleans explicitly (``isinstance(True, int)`` is True in
    Python), so a boolean never satisfies an ``(int, float)`` spec."""
    return any(isinstance(value, t) and not isinstance(value, bool) for t in types)


class Driver(ABC):
    """One driver per Device Type.

    ``fields`` declares each accepted param as ``(accepted python types, required)`` so validation
    is generic: :func:`validate_params` checks every declared field for an unknown/missing/mistyped
    value and raises a :class:`ValueError` naming the offending field. A driver's ``open`` then
    connects using those validated fields and returns an opaque device handle."""

    #: The Device Type name this driver serves, e.g. ``"TCP"`` (matches a DeviceType enum member).
    name: ClassVar[str] = "driver"
    #: True when the driver's ``open`` takes a ``receive_buffer_bytes`` cap (17a). False by default:
    #: drivers without a background reader open from ``params`` alone.
    accepts_receive_buffer_bytes: ClassVar[bool] = False
    #: Each field maps to ``(accepted types, required)``; unknown fields are always rejected.
    fields: ClassVar[dict[str, tuple[tuple[type, ...], bool]]] = {}

    @abstractmethod
    def open(self, params: dict[str, Any]) -> Any:
        """Validate ``params`` and connect; return an opaque device handle."""

    def close(self, device: Any) -> None:
        """Release a previously opened device. The default is a no-op for drivers that need no
        teardown (packet 16a's TCP driver overrides this to close its socket)."""


def validate_params(
    driver: Driver, params: Mapping[str, Any], fields: dict | None = None
) -> dict[str, Any]:
    """Validate ``params`` against ``driver.fields`` (or an explicit ``fields`` mapping) and return the cleaned field mapping.

    Raises :class:`ValueError` naming the field for any unknown, missing or mistyped value — a
    schedule bug, never a Device failure (spec: "always raises, names the field"). Booleans are not
    accepted where a number is expected."""
    # ``fields`` lets read/write validate against their own subset; None means the driver's open fields.
    field_specs = fields if fields is not None else driver.fields
    known = set(field_specs)

    # Unknown fields trip first so a typo such as ``buad`` is caught before anything connects.
    for key in params.keys():
        if key not in known:
            raise ValueError(f"unknown field '{key}' for {driver.name} device")

    cleaned: dict[str, Any] = {}
    for field, (types, required) in field_specs.items():
        if field not in params:
            if required:
                raise ValueError(f"missing required field '{field}' for {driver.name} device")
            continue
        value = params[field]
        if not _matches(value, types):
            accepted = ", ".join(t.__name__ for t in types)
            raise ValueError(
                f"field '{field}' must be one of {accepted}, got {type(value).__name__}"
            )
        cleaned[field] = value
    return cleaned


class DeviceRegistry:
    """Maps each Device Type name to its driver class and tracks this run's open devices.

    Handles are keyed by ``id`` so a handle that round-trips through Lua userdata (the same Python
    object) matches the one stored at open time; objects without custom equality hash by identity,
    which is exactly what we want for opaque handles."""

    def __init__(
        self,
        receive_buffer_bytes: int = DEFAULT_RECEIVE_BUFFER_BYTES,
        trace_enabled: bool = False,
        trace_max_bytes: int = DEFAULT_TRACE_MAX_BYTES,
        emit: Optional[Callable[..., Any]] = None,  # packet 17c: the host's record_log for trace lines
    ) -> None:
        self._drivers: dict[str, type[Driver]] = {}
        # id(handle) -> handle still open this run.
        self._open: dict[int, Any] = {}
        # Handles the schedule closed this run. Kept (not freed here) so the host can still drain
        # a device's failure warning after its handle went away (packet 17b: one WARNING per
        # failure, none lost on close); the host drops each one once drained (forget_closed).
        self._closed: dict[int, Any] = {}
        # The per-device receive buffer cap (app config) passed to every driver's open (17a).
        self._receive_buffer_bytes = receive_buffer_bytes
        # Packet 17c: with deviceTrace.enabled every open/read/write/close gets one DEBUG line,
        # emitted through ``emit`` (the LuaHost's record_log) so the engine owns it like an ATE.log
        # line. Off: tracer None, no wrapper on any handle, one `if` per call and nothing else.
        self._tracer = DeviceTracer(trace_max_bytes, emit) if (trace_enabled and emit is not None) else None

    def register(self, name: str, driver_cls: type[Driver]) -> "DeviceRegistry":
        """Register a driver class under ``name`` (a Device Type name like ``"TCP"``)."""
        self._drivers[name] = driver_cls
        return self

    def open_device(self, name: str, params: Mapping[str, Any]) -> Any:
        """Open a device of type ``name`` and start tracking it; returns the opaque handle.

        Packet 17c: with tracing on the open is timed and logged at DEBUG — a refused connect too,
        before the OSError goes on to the caller's ``onError`` — and the handle comes back wrapped in
        a :class:`TracedDevice` so its reads and writes are traced too. Off, this is the plain open:
        one `if` and no wrapper."""
        if name not in self._drivers:
            raise ValueError(f"unknown DeviceType '{name}'")
        driver_cls = self._drivers[name]

        def connect() -> Any:
            if driver_cls.accepts_receive_buffer_bytes:  # 17a: the buffer-cap rides on the driver's open
                return driver_cls().open(dict(params), self._receive_buffer_bytes)
            return driver_cls().open(dict(params))

        tracer = self._tracer
        if tracer is None:
            handle = connect()
            self._open[id(handle)] = handle
            return handle

        label = device_label(name, params)
        started = time.perf_counter()
        try:
            handle = connect()
        except OSError as exc:
            tracer.open(label, False, str(exc), elapsed_ms(started))
            raise
        tracer.open(label, True, None, elapsed_ms(started))
        handle = TracedDevice(handle, tracer, handle_label(handle, label))
        self._open[id(handle)] = handle
        return handle

    def close_device(self, handle: Any) -> bool:
        """Stop tracking ``handle`` and release it. Returns True if it was open (a real close), or
        False if it had already been closed — a double close is then a no-op that still succeeds.
        A closed handle is kept on the registry (``closed_handles``) so its failure warning can
        still be drained after the handle left ``snapshot()`` (packet 17b).

        Packet 17c: a real close is traced with the time its teardown took; a no-op second close
        traces nothing, since nothing happened."""
        key = id(handle)
        device = self._open.pop(key, None)
        if device is None:
            return False
        label = getattr(device, "label", None) or handle_label(device, "device")
        started = time.perf_counter()
        try:
            device.close()  # the handle's own teardown (TCP closes its socket); safe to call twice
        except OSError as exc:  # 17c: a close that failed is still traced, then the error carries on
            if self._tracer is not None:
                self._tracer.close(label, elapsed_ms(started), err=str(exc))
            raise
        if self._tracer is not None:
            self._tracer.close(label, elapsed_ms(started))
        self._closed[key] = device
        return True

    def closed_handles(self) -> list[Any]:
        """Every handle the schedule closed this run, still available to drain (packet 17b)."""
        return list(self._closed.values())

    def forget_closed(self, handle: Any) -> None:
        """Drop a closed handle once its warnings are drained and it can enqueue no more."""
        self._closed.pop(id(handle), None)

    def is_open(self, handle: Any) -> bool:
        """Whether ``handle`` is still open this run (not yet closed)."""
        return id(handle) in self._open

    def snapshot(self) -> list[Any]:
        """Return every handle still open this run, for run-end teardown."""
        return list(self._open.values())
