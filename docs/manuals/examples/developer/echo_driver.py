"""The ECHO Device driver: the developer manual's worked example of a Device Type.

Nothing here touches a transport. ``open`` returns a handle that keeps the bytes a Test
Schedule writes, and ``read`` hands them back, so a schedule can exercise open, write, read,
flush and close against a Device Type that did not exist five minutes ago. That is enough to
prove the steps the manual gives: declare the fields, name the Device Type, register the
driver, add the enum member. ``tests/test_manuals.py`` copies this file into a temporary copy
of ``src/ate``, applies the three edits the manual shows, and runs ``echo.tslua`` against it.
"""

from __future__ import annotations

from typing import Any

from .registry import Driver, validate_params
from .stream import READ_FIELDS, WRITE_FIELDS


class _EchoDevice:
    """The handle an ECHO Device is: the bytes written, in the order they were written.

    ``driver`` is what the ATE wrappers validate a per-call read/write field against, and
    ``describe`` is what makes a value a Device handle at all (packet 29d review)."""

    #: A read cut short by `size` is a truncation, never a deadline catching a stream mid-bytes.
    partial_read_is_truncation = True

    def __init__(self, label: str) -> None:
        self.driver = EchoDriver
        self._label = label
        self._buffer = bytearray()

    def describe(self) -> str:
        return f"ECHO {self._label}"

    def read(self, params: dict[str, Any]) -> tuple[bytes, int, bool, str | None]:
        term = params.get("readTerm")
        if isinstance(term, str):
            term = term.encode()
        size = params.get("size")
        data = bytes(self._buffer)
        if term:
            head, found, tail = data.partition(term)
            self._buffer = bytearray(tail) if found else bytearray()
            data = head if found else data
        else:
            self._buffer.clear()
        if size is not None and len(data) > size:
            self._buffer.extend(data[size:])
            data = data[:size]
            return data, len(data), True, None
        return data, len(data), False, None

    def write(self, params: dict[str, Any]) -> tuple[bool, int, str | None]:
        payload = params["data"]
        if isinstance(payload, str):
            payload = payload.encode()
        term = params.get("writeTerm") or b""
        if isinstance(term, str):
            term = term.encode()
        self._buffer.extend(payload)
        self._buffer.extend(term)
        return True, len(payload) + len(term), None

    def flush(self) -> tuple[bool, int, str | None]:
        count = len(self._buffer)
        self._buffer.clear()
        return True, count, None

    def take_overflow(self) -> tuple[bool, int]:
        return False, 0  # no cap on this buffer, so no bytes are ever dropped

    def close(self) -> None:
        self._buffer.clear()


class EchoDriver(Driver):
    """A Device Type that answers with exactly what the Test Schedule wrote to it."""

    name = "ECHO"
    fields = {
        "label": ((str,), True),            # named in every trace line and WARNING for this Device
        "timeout": ((int, float), False),   # accepted so a schedule can pass one; nothing waits on it
    }
    read_fields = READ_FIELDS
    write_fields = WRITE_FIELDS

    def open(self, params: dict[str, Any]) -> _EchoDevice:
        """Validate the params and return the handle. A bad field raises before anything else."""
        validated = validate_params(self, params)
        return _EchoDevice(validated["label"])

    def close(self, device: _EchoDevice) -> None:
        device.close()
