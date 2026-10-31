"""The SERIAL Device driver (packet 18a).

RS-232 or USB serial through pyserial: ``port`` is whatever pyserial accepts - a COM/tty name or a
pyserial URL such as ``socket://127.0.0.1:5025`` - opened with :func:`serial.serial_for_url`. Every
open field is required; the ranges and the ``Parity``/``FlowControl`` enums are checked before
anything is opened, so a bad value is a schedule bug that raises naming the field rather than a
Device failure, and a port that cannot be opened is an open failure like a refused TCP connect.

The reader, the buffer, the overflow, the failure and the warning logic are shared with TCP in
:mod:`ate.devices.stream`. The pyserial-specific parts: the reader polls the port with pyserial's
own short read timeout (50 ms), one byte at a time, so ``close()`` stays prompt and a disconnect
never loses the bytes the same read had already pulled; for pyserial an empty read means "nothing
arrived yet", not end of stream - pyserial 3.5's ``socket://`` handler raises
``SerialException('socket disconnected')`` when the peer really goes away, which is the background
failure the device records. A write sets pyserial's ``write_timeout`` to the time left and goes out
in pieces: pyserial raises ``SerialTimeoutException`` on a write timeout without saying how many
bytes got out, so the reported ``count`` is the pieces that made it out whole."""

from __future__ import annotations

from typing import Any

import serial

from .registry import DEFAULT_RECEIVE_BUFFER_BYTES, Driver, validate_params
from .stream import (
    POLL_SECONDS,
    READ_CHUNK,
    READ_FIELDS,
    WRITE_CHUNK,
    WRITE_FIELDS,
    _SendTimeout,
    _StreamDevice,
    _TransportClosed,
)

#: The Parity enum values (their Lua values are these strings) -> pyserial's parity constants.
_PARITY = {
    "NONE": serial.PARITY_NONE,
    "EVEN": serial.PARITY_EVEN,
    "ODD": serial.PARITY_ODD,
    "MARK": serial.PARITY_MARK,
    "SPACE": serial.PARITY_SPACE,
}
#: The allowed stopBits values -> pyserial's stopbit constants.
_STOPBITS = {
    1: serial.STOPBITS_ONE,
    1.5: serial.STOPBITS_ONE_POINT_FIVE,
    2: serial.STOPBITS_TWO,
}
#: A FlowControl enum value -> (rtscts, xonxoff, dsrdtr).
_FLOW = {
    "NONE": (False, False, False),
    "RTSCTS": (True, False, False),
    "XONXOFF": (False, True, False),
    "DSRDTR": (False, False, True),
}
#: dataBits is a plain byte count, 5..8.
_DATA_BITS = (5, 6, 7, 8)


class _SerialDevice(_StreamDevice):
    """Opaque handle returned by :meth:`SerialDriver.open`: a pyserial port behind the shared
    stream logic. ``ser`` is the live port object; Lua only ever hands the handle back to
    ``read``/``write``/``close``."""

    def __init__(self, ser: serial.Serial, params: dict[str, Any],
                 receive_buffer_bytes: int = DEFAULT_RECEIVE_BUFFER_BYTES) -> None:
        super().__init__(params, SerialDriver, receive_buffer_bytes)
        self.ser = ser

    def describe(self) -> str:
        """A one-line description of this device for log lines (the port as the schedule gave it)."""
        return f"SERIAL {self.params['port']}"

    def _recv(self) -> bytes | None:
        """Pull up to one poll's worth of bytes from the port.

        ``b""`` means "nothing arrived in this poll", not end of stream: pyserial stops reading at
        the port's 50 ms timeout, and a real serial port has no EOF. The bytes are taken one at a
        time on purpose - pyserial's ``socket://`` handler raises ``SerialException`` inside
        ``read`` the moment the peer goes away, and a read that asked for more than arrived would
        throw away the bytes it had already pulled. One byte per read puts the disconnect in the
        *next* poll, so the data that came before it is in the buffer. When the port dies while
        bytes are already in hand, they are appended before the failure is raised, so a hang-up
        never swallows a reply.
        """
        got = bytearray()
        try:
            while len(got) < READ_CHUNK:
                piece = self.ser.read(1)  # waits out the poll timeout; b"" = nothing this poll
                if not piece:
                    break
                got.extend(piece)
        except (serial.SerialException, OSError, AttributeError) as exc:  # SerialException is an OSError
            if got:
                self._append(bytes(got))  # what arrived before the port went away is not lost
            raise _TransportClosed(str(exc)) from exc
        return bytes(got) or None

    def _send(self, chunk: bytes, seconds: float) -> int:
        """Write at most one ``WRITE_CHUNK`` of the payload, with the time left as pyserial's write
        timeout. A port that will not take it in time raises SerialTimeoutException - that fails
        only this call - and any other error means the port is gone."""
        self.ser.write_timeout = seconds
        try:
            return self.ser.write(chunk[:WRITE_CHUNK])
        except serial.SerialTimeoutException as exc:
            raise _SendTimeout(str(exc)) from exc
        except (serial.SerialException, OSError) as exc:
            raise _TransportClosed(str(exc)) from exc

    def _close_transport(self) -> None:
        """Close the port."""
        self.ser.close()


class SerialDriver(Driver):
    """Opens and closes SERIAL Devices (DeviceType.SERIAL)."""

    name = "SERIAL"
    accepts_receive_buffer_bytes = True  # 17a: the reader thread caps its buffer from the config
    fields = {
        "port": ((str,), True),  # a COM/tty name or a pyserial URL, e.g. "socket://127.0.0.1:5025"
        "baud": ((int, float), True),  # bits per second, e.g. 9600; must be positive
        "dataBits": ((int,), True),  # 5-8
        "stopBits": ((int, float), True),  # 1, 1.5 or 2
        "parity": ((str,), True),  # a Parity value: NONE, EVEN, ODD, MARK, SPACE
        "flowControl": ((str,), True),  # a FlowControl value: NONE, RTSCTS, XONXOFF, DSRDTR
        "timeout": ((int, float), True),  # read and write timeout in seconds
        "readTerm": ((str, bytes), True),  # byte string stripped from reads; "" means none
        "writeTerm": ((str, bytes), True),  # byte string appended to writes; "" means none
    }

    # Same per-call overrides as every other streaming Device Type (packet 16b).
    read_fields = READ_FIELDS
    write_fields = WRITE_FIELDS

    def open(self, params: dict[str, Any],
             receive_buffer_bytes: int = DEFAULT_RECEIVE_BUFFER_BYTES) -> _SerialDevice:
        """Validate ``params`` and open the port through pyserial. Raises :class:`ValueError` naming
        any bad field - type, range or enum - before anything is opened.

        The port's own read timeout is the reader's poll interval, not the schedule's ``timeout``:
        the schedule's timeout is the deadline the shared buffer logic enforces, while pyserial has
        to stop polling quickly for close() to be prompt."""
        validated = validate_params(self, params)
        if validated["baud"] <= 0:
            raise ValueError(f"field 'baud' must be a positive int, got {validated['baud']}")
        if validated["dataBits"] not in _DATA_BITS:
            raise ValueError(
                f"field 'dataBits' must be one of 5, 6, 7, 8, got {validated['dataBits']}"
            )
        if validated["stopBits"] not in _STOPBITS:
            raise ValueError(
                f"field 'stopBits' must be one of 1, 1.5, 2, got {validated['stopBits']}"
            )
        if validated["parity"] not in _PARITY:
            raise ValueError(
                f"field 'parity' must be one of NONE, EVEN, ODD, MARK, SPACE, got {validated['parity']!r}"
            )
        if validated["flowControl"] not in _FLOW:
            raise ValueError(
                f"field 'flowControl' must be one of NONE, RTSCTS, XONXOFF, DSRDTR, "
                f"got {validated['flowControl']!r}"
            )
        rtscts, xonxoff, dsrdtr = _FLOW[validated["flowControl"]]
        ser = serial.serial_for_url(
            validated["port"],
            baudrate=int(validated["baud"]),
            bytesize=validated["dataBits"],
            parity=_PARITY[validated["parity"]],
            stopbits=_STOPBITS[validated["stopBits"]],
            rtscts=rtscts,
            xonxoff=xonxoff,
            dsrdtr=dsrdtr,
            timeout=POLL_SECONDS,
        )
        device = _SerialDevice(ser, validated, receive_buffer_bytes)
        device.start_reader()  # packet 17a: the reader runs from open to close
        return device
