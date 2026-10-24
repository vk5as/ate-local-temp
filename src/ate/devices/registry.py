"""The per-run Device registry and its driver abstraction (packet 16a).

A :class:`DeviceRegistry` maps each Device Type name to the driver class that opens it, and
tracks every device this run has opened so leftovers can be closed at run end. One registry is
owned by one LuaHost / Test Run; it is created when the host starts a run and torn down when it
ends (see :meth:`RunEngine.run`).

Drivers are intentionally small: each declares the fields its ``open`` params accept, validates
them generically, connects, and returns an opaque handle. The registry never knows how a driver
connects — only that it produced a handle to track until close.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Mapping


def _is_number(value: Any) -> bool:
    """True for Lua numbers (int or float), but never booleans — a boolean is not a number."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


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
    #: Each field maps to ``(accepted types, required)``; unknown fields are always rejected.
    fields: ClassVar[dict[str, tuple[tuple[type, ...], bool]]] = {}

    @abstractmethod
    def open(self, params: dict[str, Any]) -> Any:
        """Validate ``params`` and connect; return an opaque device handle."""

    def close(self, device: Any) -> None:
        """Release a previously opened device. The default is a no-op for drivers that need no
        teardown (packet 16a's TCP driver overrides this to close its socket)."""


def validate_params(driver: Driver, params: Mapping[str, Any]) -> dict[str, Any]:
    """Validate ``params`` against ``driver.fields`` and return the cleaned field mapping.

    Raises :class:`ValueError` naming the field for any unknown, missing or mistyped value — a
    schedule bug, never a Device failure (spec: "always raises, names the field"). Booleans are not
    accepted where a number is expected."""
    known = set(driver.fields)

    # Unknown fields trip first so a typo such as ``buad`` is caught before anything connects.
    for key in params.keys():
        if key not in known:
            raise ValueError(f"unknown field '{key}' for {driver.name} device")

    cleaned: dict[str, Any] = {}
    for field, (types, required) in driver.fields.items():
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

    def __init__(self) -> None:
        self._drivers: dict[str, type[Driver]] = {}
        # id(handle) -> handle still open this run.
        self._open: dict[int, Any] = {}

    def register(self, name: str, driver_cls: type[Driver]) -> "DeviceRegistry":
        """Register a driver class under ``name`` (a Device Type name like ``"TCP"``)."""
        self._drivers[name] = driver_cls
        return self

    def open_device(self, name: str, params: Mapping[str, Any]) -> Any:
        """Open a device of type ``name`` and start tracking it; returns the opaque handle."""
        if name not in self._drivers:
            raise ValueError(f"unknown DeviceType '{name}'")
        driver_cls = self._drivers[name]
        handle = driver_cls().open(dict(params))  # validate + connect before tracking anything
        self._open[id(handle)] = handle
        return handle

    def close_device(self, handle: Any) -> bool:
        """Stop tracking ``handle`` and release it. Returns True if it was open (a real close), or
        False if it had already been closed — a double close is then a no-op that still succeeds."""
        key = id(handle)
        device = self._open.pop(key, None)
        if device is None:
            return False
        device.close()  # the handle's own teardown (TCP closes its socket); safe to call twice
        return True

    def snapshot(self) -> list[Any]:
        """Return every handle still open this run, for run-end teardown."""
        return list(self._open.values())
