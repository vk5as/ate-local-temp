"""Simulator peers for the example Device schedule (packet 29d).

No hardware is needed to run `devices.tslua`. Two small peers stand in for the instruments:

  * `ScpiPeer` - a TCP listener that answers line-delimited SCPI commands, and is reached three ways:
    directly as a `TCP` Device, as a `SERIAL` Device through pyserial's `socket://host:port` URL, and
    as a `VISA` instrument through pyvisa-py's `TCPIP::<host>::<port>::SOCKET` resource. It answers
    `*IDN?` and `MEAS:VOLT?`, replies to `PARTIAL?` with bytes and no terminator (the partial read),
    answers `MULTI?` with several lines so a `flush` has something to discard, and says nothing at all
    to `NEVER?` (the read that times out).
  * `UdpEchoPeer` - a UDP listener that answers every datagram with `ECHO:<datagram>`, except one
    beginning `DROP:`, which it records and ignores so a read has nothing to find.

Like the peers in `tests/_fixtures.py` both bind an ephemeral port on 127.0.0.1 in `__init__`, so
concurrent runs never collide on a fixed port; the harness writes the live ports into a copy of the
Test Definition before the run.
"""

from __future__ import annotations

import socket
import threading
from typing import Any

#: The SCPI peer's whole conversation, as (command, reply) with the replies' exact bytes. The
#: harness asserts against this table, so changing a reply changes what the run records.
REPLIES: dict[str, str] = {
    "*IDN?": "ATE SIM,scpi-peer,1.0,2026-01-01\n",
    "MEAS:VOLT?": "1.234\n",
    "PARTIAL?": "a reply whose terminator never arrives",
    "MULTI?": "chan=1\nchan=2\nchan=3\n",
    "NEVER?": "",  # silence: the read times out with nothing
}

#: The last line of MULTI?'s reply, which the schedule never reads: the bytes ATE.flush discards.
FLUSHED_BYTES = "chan=3\n"


class ScpiPeer:
    """A line-oriented SCPI simulator on an ephemeral port on 127.0.0.1."""

    #: How long a connection is waited on before the peer gives up reading it. A safety net so a
    #: peer can never block forever; the schedule closes every Device it opens.
    READ_TIMEOUT = 10.0

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self.commands: list[str] = []          # every command every Device sent, in order
        self.replies: list[str] = []          # the bytes the peer sent back, in order
        self.connections: list[socket.socket] = []
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Begin accepting connections; the listen socket was bound in `__init__`."""
        if not self._running:
            self._running = True
            self._thread = threading.Thread(target=self._accept_loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._running = False
        for conn in self.connections:
            try:
                conn.close()
            except OSError:
                pass
        try:
            self._sock.close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                break  # the listener is closed
            conn.settimeout(self.READ_TIMEOUT)
            self.connections.append(conn)
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        """Read commands line by line and answer each one from REPLIES."""
        buf = bytearray()
        while self._running:
            try:
                chunk = conn.recv(4096)
            except OSError:
                return  # the Device closed or reset the connection
            if not chunk:
                return  # orderly close: nothing more to answer
            buf.extend(chunk)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                command = line.decode("ascii", "replace").strip()
                self.commands.append(command)
                reply = REPLIES.get(command)
                if reply:
                    try:
                        conn.sendall(reply.encode("ascii"))
                        self.replies.append(reply)
                    except OSError:
                        return  # the Device went away mid-reply


class UdpEchoPeer:
    """A UDP peer on an ephemeral port that echoes every datagram it receives."""

    #: A datagram starting with this is recorded but never answered: that is the read the schedule
    # times out on.
    DROP_PREFIX = b"DROP:"

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self.received: list[bytes] = []  # every datagram the Device sent, in order
        self.echoes: list[bytes] = []    # what the peer answered, in order
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._running:
            self._running = True
            self._thread = threading.Thread(target=self._recv_loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._running = False
        try:
            self._sock.close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _recv_loop(self) -> None:
        while self._running:
            try:
                data, addr = self._sock.recvfrom(65536)  # one recv is one datagram
            except socket.timeout:
                continue  # a socket timeout just wakes the loop to check the stop flag
            except OSError:
                return  # the peer is closed
            self.received.append(data)
            if data.startswith(self.DROP_PREFIX):
                continue  # this one gets no reply: the schedule's read times out
            echo = b"ECHO:" + data
            try:
                self._sock.sendto(echo, addr)  # back to the Device that sent it
                self.echoes.append(echo)
            except OSError:
                return


class Peers:
    """The simulator peers the Device schedule talks to, started together and stopped together."""

    def __init__(self) -> None:
        self.scpi: ScpiPeer = ScpiPeer()
        self.udp: UdpEchoPeer = UdpEchoPeer()

    def start(self) -> None:
        self.scpi.start()
        self.udp.start()

    def stop(self) -> None:
        self.scpi.stop()
        self.udp.stop()

    def connection_count(self) -> int:
        """How many Devices connected to the SCPI peer (TCP, SERIAL, VISA and the Pre-test Device)."""
        return len(self.scpi.connections)

    def keyvals(self) -> dict[str, Any]:
        """The ports the schedule's keyvals need, for the harness to write into its copy."""
        return {"__SCPI_PORT__": self.scpi.port, "__UDP_PORT__": self.udp.port}
