"""Packet 17c: Device tracing — one DEBUG line per Device operation.

When ``deviceTrace.enabled`` is set in the app config (off by default) the registry wraps every
handle it opens in a :class:`TracedDevice` and logs each ``open``, ``read``, ``write`` and ``close``
at ``DEBUG`` with source ``device``: the Device (type plus address or resource), the bytes escaped
and truncated past ``deviceTrace.maxBytes``, and the wall-clock time the call took in ms. Failures
are traced too — a refused open, a read that timed out, a write that failed.

Lines go through the LuaHost's own ``record_log``, the same path a device WARNING takes, so the run
engine attributes them to the Attempt or Pre-/Post-test Action that made the call and orders them
against that action's ``ATE.log`` lines. DEBUG keeps them out of the Test Report and in the debug
window. With tracing off there is no tracer, no wrapper, and the only cost is one ``if`` in
:meth:`DeviceRegistry.open_device`.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping

#: The length past which traced bytes are truncated (app config deviceTrace.maxBytes).
DEFAULT_TRACE_MAX_BYTES = 256

#: Control characters written as their own escape rather than \xNN.
_SIMPLE_ESCAPES = {0x0A: "\\n", 0x0D: "\\r", 0x09: "\\t", 0x5C: "\\\\", 0x22: "\\\""}


def escape_bytes(data: bytes, max_bytes: int) -> str:
    """Escape ``data`` for a trace line: printable ASCII as-is, ``\\n`` ``\\r`` ``\\t`` ``\\`` ``"``
    as escapes, everything else ``\\xNN``.

    Truncation is applied to the RAW bytes at ``max_bytes``, before escaping, so the quoted part is
    always a clean prefix of the payload; :func:`quoted_bytes` adds the `… (+N bytes)` tail."""
    out = []
    for byte in data[:max_bytes]:
        escape = _SIMPLE_ESCAPES.get(byte)
        if escape is not None:
            out.append(escape)
        elif 0x20 <= byte <= 0x7E:
            out.append(chr(byte))
        else:
            out.append(f"\\x{byte:02x}")
    return "".join(out)


def quoted_bytes(data: bytes, max_bytes: int) -> str:
    """The payload as a trace line writes it: the escaped bytes in quotes, then `… (+N bytes)` when
    the cap cut them off."""
    text = f"\"{escape_bytes(data, max_bytes)}\""
    if len(data) > max_bytes:
        text += f"… (+{len(data) - max_bytes} bytes)"
    return text


def elapsed_ms(started: float) -> float:
    """The wall-clock milliseconds since ``started``, a :func:`time.perf_counter` reading."""
    return (time.perf_counter() - started) * 1000.0


def device_label(name: str, params: Mapping[str, Any]) -> str:
    """The Device name used on every line for one device: the Device Type plus its address or resource.

    A handle's own ``describe()`` says the same thing, so an open that never connected names the
    device the way its later reads and writes would."""
    host, port = params.get("host"), params.get("port")
    if host is not None and port is not None:
        return f"{name} {host}:{port}"
    if port is not None:
        return f"{name} {port}"
    if host is not None:
        return f"{name} {host}"
    resource = params.get("resource")
    return f"{name} {resource}" if resource is not None else name


def handle_label(handle: Any, fallback: str) -> str:
    """The handle's own description, or the params-derived label if it cannot describe itself."""
    describe = getattr(handle, "describe", None)
    if describe is None:
        return fallback
    try:
        return str(describe())
    except Exception:  # noqa: BLE001 - a handle that cannot describe itself still traces by its params
        return fallback


class DeviceTracer:
    """Builds one DEBUG line per Device operation and hands it to the run's log sink.

    ``emit`` is the LuaHost's ``record_log``, so a trace line joins the run's ordered log, gets the
    owner the engine gives every other line from that action, and reaches the debug window; DEBUG
    keeps it out of the report."""

    def __init__(self, max_bytes: int, emit: Callable[..., Any]) -> None:
        self._max_bytes = max(0, int(max_bytes))
        self._emit = emit

    def _line(self, text: str) -> None:
        self._emit("DEBUG", text, source="device")

    def open(self, label: str, ok: bool, err: str | None, elapsed: float) -> None:
        """The open itself: `open TCP 127.0.0.1:5025 ok (1.2 ms)`, or the failure with its error."""
        detail = "ok" if ok else f"failed: {err}"
        self._line(f"open {label} {detail} ({elapsed:.1f} ms)")

    def write(self, label: str, count: int, payload: bytes, err: str | None, elapsed: float) -> None:
        """`write <device> 6 bytes "*IDN?\\n" (0.3 ms)`, with `err "..."` after the quote when the send failed."""
        flags = "" if err is None else f" err \"{err}\""
        self._line(
            f"write {label} {count} bytes {quoted_bytes(payload, self._max_bytes)}{flags} ({elapsed:.1f} ms)"
        )

    def read(
        self, label: str, count: int, data: bytes | None, partial: bool, err: str | None, elapsed: float
    ) -> None:
        """`read <device> 12 bytes "ACME,PSU,1\\r\\n" (4.1 ms)`, plus `partial` or `err "timeout"`
        when they apply."""
        flags = ""
        if partial:
            flags += " partial"
        if err is not None:
            flags += f" err \"{err}\""
        payload = b"" if data is None else bytes(data)
        self._line(
            f"read {label} {count} bytes {quoted_bytes(payload, self._max_bytes)}{flags} ({elapsed:.1f} ms)"
        )

    def close(self, label: str, elapsed: float, err: str | None = None) -> None:
        """`close <device> (0.1 ms)` — a close has no bytes, only its teardown time, plus the error
        when the teardown itself failed."""
        detail = "" if err is None else f" failed: {err}"
        self._line(f"close {label}{detail} ({elapsed:.1f} ms)")


def _write_payload(params: Mapping[str, Any], device: Any) -> bytes:
    """The exact bytes a write sends: the data plus the terminator the driver appends.

    UDP has no terminator field, so ``writeTerm`` is simply absent there and the payload is the data
    alone."""
    term = params.get("writeTerm")
    if term is None:
        term = getattr(device, "params", {}).get("writeTerm", b"")

    def as_bytes(value: Any) -> bytes:
        return value.encode("utf-8") if isinstance(value, str) else bytes(value)

    return as_bytes(params.get("data", b"")) + as_bytes(term)


class TracedDevice:
    """The handle a schedule sees when tracing is on: it forwards everything to the real device and
    times the reads and writes that go through it.

    Only ``read`` and ``write`` are wrapped — the registry times ``open`` and ``close`` — and every
    other attribute (``driver``, ``describe``, ``take_warnings``, …) is looked up on the wrapped
    handle, so the host's device plumbing sees the contract it always did."""

    __slots__ = ("_device", "_tracer", "_label")

    def __init__(self, device: Any, tracer: DeviceTracer, label: str) -> None:
        self._device = device
        self._tracer = tracer
        self._label = label  # the same Device name on every line for this handle

    @property
    def label(self) -> str:
        """The Device name this handle's trace lines carry, so a close names it like its reads did."""
        return self._label

    def read(self, params: dict[str, Any]) -> tuple[Any, int, bool, str | None]:
        """Read through the real device and trace what came back, including a timeout."""
        started = time.perf_counter()
        result = self._device.read(params)
        data, count, partial, err = result
        self._tracer.read(self._label, count, data, partial, err, elapsed_ms(started))
        return result

    def write(self, params: dict[str, Any]) -> tuple[bool, int, str | None]:
        """Write through the real device and trace the payload it tried to send."""
        payload = _write_payload(params, self._device)
        started = time.perf_counter()
        result = self._device.write(params)
        ok, count, err = result
        self._tracer.write(self._label, count, payload, err, elapsed_ms(started))
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._device, name)

    def close(self) -> Any:
        """Release the real device; the registry times and traces the close around this."""
        return self._device.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._device, name)
