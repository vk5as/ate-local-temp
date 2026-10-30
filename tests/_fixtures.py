"""Shared helpers for ticket 02's seam tests: build a minimal on-disk ATE environment.

These write a config pointing at a fresh SQLite database and one Test Definition
directory holding three schedules (one that passes, one that fails, one that raises), so
the headless session and CLI can be exercised end-to-end against real files — no mocks.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from pathlib import Path

from ate.engine.operator_input import OperatorInput
from ate.engine.run_engine import RunEngine, RunStatus


# A schedule that reports PASS on its single step.
PASSING = """\
return ScheduleParams("Passing schedule", "A step that passes")
  :setDocTitle("Passing Test Report")
  :setSchedule({
    StepParams("LED colour"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "ok", "Green", "Green") end),
  })
"""

# A schedule that reports FAIL on its single step.
FAILING = """\
return ScheduleParams("Failing schedule", "A step that fails")
  :setSchedule({
    StepParams("LED colour"):setStepNo("1.")
      :setAction(function() ATE.report(Result.FAIL, "wrong colour", "Red", "Green") end),
  })
"""

# First step fails on every attempt, second passes, Stop on Fail armed (packet 12d): a failure
# prompts the operator, a Retry re-pauses on Attempt 2, and Continue/Abort decide the ending.
STEPS_FAIL_THEN_PASS = """\
return ScheduleParams("Fail then pass schedule", "first step fails, second passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("first"):setStepNo("1.")
      :setAction(function() ATE.report(Result.FAIL, "wrong colour", "Red", "Green") end),
    StepParams("second"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "ok now", "Blue", "Blue") end),
  })
"""

# Attempt 1 fails, Attempt 2 passes (packet 12d review): the attempt count is a Lua upvalue the
# step action closes over, so a Retry that passes ends the pause for good.
FAIL_THEN_PASS = """\
local attempts = 0
return ScheduleParams("Passes on retry schedule", "attempt one fails, attempt two passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("first"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "cold joint", "0.2V", "2.4V")
        else
          ATE.report(Result.PASS, "ok now", "2.4V", "2.4V")
        end
      end),
  })
"""

# A single-passing step with full document metadata (doc title/id/rev) set — so a report's Cover
# can be checked for every recorded doc field.
DOC_FIELDS = """\
return ScheduleParams("Doc fields schedule", "a step with full document metadata")
  :setDocTitle("Quarterly Power Audit")
  :setDocId("PS-AUD-2026-Q4")
  :setDocRev("3")
  :setSchedule({
    StepParams("LED colour"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "ok", "Green", "Green") end),
  })
"""

# A schedule whose first step raises a Lua error and whose second never runs.
ERRORING = """\
return ScheduleParams("Erroring schedule", "A step that raises a Lua error")
  :setSchedule({
    StepParams("boom"):setStepNo("1.")
      :setAction(function() error("kaboom from Lua") end),
    StepParams("never runs"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "should not run", "x", "y") end),
  })
"""

# A schedule with three ordinary steps: the middle one is disabled by default (:setEnable(false)).
# seq in the runner is the step's position here — a=0 (on), b=1 (off by default), c=2 (on) — so an
# Operator override targets it by that integer. Its action only reports if it actually runs, which
# lets a test confirm a disabled step never executes.
ENABLE_DISABLED = """\
return ScheduleParams("Enable/disable schedule", "steps with enable defaults and Operator overrides")
  :setSchedule({
    StepParams("a"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "ok a") end),
    StepParams("b"):setStepNo("2.")
      :setEnable(false)
      :setAction(function() ATE.report(Result.PASS, "should not run by default") end),
    StepParams("c"):setStepNo("3.")
      :setAction(function() ATE.report(Result.PASS, "ok c") end),
  })
"""

# A failing schedule that explicitly turns Stop on Fail off. Selecting it records stop_on_fail=0
# without any Operator override; the Steps stage radio would prefill unchecked for it.
STOP_ON_FAIL_FALSE = """\
return ScheduleParams("Stop on fail false schedule", "a step that fails and stops on fail is off")
  :setSchedule({
    StepParams("LED colour"):setStepNo("1.")
      :setAction(function() ATE.report(Result.FAIL, "wrong colour", "Red", "Green") end),
  })
  :setStopOnFail(false)
"""

# A schedule whose steps log at every level and also use print — exercising ticket 08's
# ATE.log(Log.X, ...) recording and the print -> INFO redirection.
LOGGING = """\
return ScheduleParams("Logging schedule", "Steps that log at every level")
  :setSchedule({
    StepParams("info step"):setStepNo("1.")
      :setAction(function()
        ATE.log(Log.INFO, "step one running")
        print("printed here")
        ATE.report(Result.PASS)
      end),
    StepParams("warn step"):setStepNo("2.")
      :setAction(function() ATE.log(Log.WARNING, "value", 12) end),
    StepParams("error step"):setStepNo("3.")
      :setAction(function() ATE.log(Log.ERROR, "something failed") end),
  })
"""

# A schedule whose single step runs its action but never calls ATE.report. It must record a
# NOREPORT attempt and log one warning, so a forgotten report is visible.
NEVER_REPORTS = """\
return ScheduleParams("Never reports schedule", "A step that runs but never reports")
  :setSchedule({
    StepParams("silent step"):setStepNo("1.")
      :setAction(function() ATE.log(Log.INFO, "did some work") end),
  })
"""

# A schedule that calls ATE.report at the top level, outside any Pre-test Action or step. Per the
# "where each call is allowed" rule this must raise while the file loads: only the builders,
# require, ATE.log and the enums are legal at load time. The report runs before ``return`` so it
# actually executes during loading (Lua does not run statements after a ``return``); the raised
# error surfaces as a load failure.
LOAD_REPORT = """\
ATE.report(Result.PASS, "not allowed here")
return ScheduleParams("Load report schedule", "reports at load time") :setSchedule({})
  :setPreTestAction(function() end)
"""

# A schedule whose Pre-test Action calls ATE.input. In a Pre-test/Post-test Action that is not
# allowed (it belongs to steps), so it must raise when the action runs.
PRETEST_INPUT = """\
return ScheduleParams("Pretest input schedule", "input in a pretest action") :setSchedule({})
  :setPreTestAction(function() ATE.input(1) end)
"""

# Same restriction for a Post-test Action: input is not allowed there either.
POSTTEST_INPUT = """\
return ScheduleParams("Posttest input schedule", "input in a posttest action") :setSchedule({})
  :setPostTestAction(function() ATE.input(1) end)
"""

# Same restriction for ATE.report in a Pre-test Action: report is step-only (like input), so this
# must raise when the action runs through its phase context.
PRETEST_REPORT = """\
return ScheduleParams("Pretest report schedule", "report in a pretest action") :setSchedule({})
  :setPreTestAction(function() ATE.report(Result.PASS, "not allowed here") end)
"""

# A schedule whose Pre-test Action calls ATE.log — which IS legal everywhere except load time.
PRETEST_LOG = """\
return ScheduleParams("Pretest log schedule", "log in a pretest action is fine") :setSchedule({})
  :setPreTestAction(function() ATE.log(Log.INFO, "setup running") end)
"""

# A passing step wrapped by Pre-test and Post-test Actions that each log their phase and the pass_fail
# they were called with. An end-to-end run records both as action rows (outcome ok); the logged values
# prove the Pre-test saw the initial PASS and the Post-test the final result after its single step.
PRE_POST_PASS = """\
return ScheduleParams("Pre post pass schedule", "pre/post actions around one passing step")
  :setSchedule({
    StepParams("the step"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "ok") end),
  })
  :setPreTestAction(function(passFail) ATE.log(Log.INFO, "pretest "..passFail) end)
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "posttest "..passFail) end)
"""

# Same shape but the step fails: the Post-test Action must be called with FAIL (the run's final result),
# proving pass_fail flows from steps to Post-test. The Pre-test still ran first ("PASS").
PRE_POST_FAIL = """\
return ScheduleParams("Pre post fail schedule", "pre/post actions around one failing step")
  :setSchedule({
    StepParams("the step"):setStepNo("1.")
      :setAction(function() ATE.report(Result.FAIL, "nope") end),
  })
  :setPreTestAction(function(passFail) ATE.log(Log.INFO, "pretest "..passFail) end)
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "posttest "..passFail) end)
"""

# The Pre-test Action raises a Lua error. This packet does not abort on it (that is 13b); the action row
# records outcome "error" and execution continues to the step and then the Post-test Action ("ok").
PRE_ERROR_POST_OK = """\
return ScheduleParams("Pre error post ok schedule", "a Pre-test Action that raises")
  :setSchedule({
    StepParams("the step"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "still runs") end),
  })
  :setPreTestAction(function(passFail) error("boom in pretest "..passFail) end)
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "posttest "..passFail) end)
"""

# A schedule whose single step reads ATE.run.Operator (populated by the run engine from this run's
# context) and reports it — so an end-to-end run records the operator a step can read.
READS_RUN = """\
return ScheduleParams("Reads run schedule", "a step that reads ATE.run")
  :setSchedule({
    StepParams("read operator"):setStepNo("1.")
      :setAction(function() local op=ATE.run.Operator; ATE.report(Result.PASS, "operator is "..op) end),
  })
"""

# A schedule whose single step tries to write ATE.run — which must raise (the table is read-only).
# The raised error becomes an ERROR attempt and fails the run.
WRITES_RUN = """\
return ScheduleParams("Writes run schedule", "a step that writes ATE.run")
  :setSchedule({
    StepParams("hacker"):setStepNo("1.")
      :setAction(function() ATE.run.Operator = "injected" end),
  })
"""

# A schedule whose single step calls an isolated failing ATE function (ATE.boom). The function
# is registered on the driven host via RunEngine's ate_fns seam; calling it from Lua raises a
# Python exception that _AteTable._isolate must catch, log at ERROR, and return as graceful
# halves — never let reach run_engine or surface in Lua.
ISO_ISOLATED = """\
return ScheduleParams("Isolated schedule", "A step that calls an isolated failing ATE function")
  :setSchedule({
    StepParams("boom step"):setStepNo("1.")
      :setAction(function() return ATE.boom("bad value") end),
  })
"""

# A whole-run abort fixture (packets 13a, 30b review). Its first step blocks on a single INTEGER
# input, so the driver has a guaranteed window between steps: while that step waits it supplies its
# value and requests an abort. The engine then records the blocked step PASS, marks the next step ERROR
# "Aborted by Operator", leaves every later step NOTRUN, and closes ABORTED / FAIL — cleanly, with
# no busy-loop or blocking interruption (those come in 13b/13c).
ABORTABLE = """\
return ScheduleParams("Abortable schedule", "several passing steps; first blocks on an input")
  :setSchedule({
    StepParams("first"):setStepNo("1.")
      :setInputs({ [1] = { desc = "voltage", type = "INTEGER" } })
      :setAction(function() local v=ATE.input(1); ATE.report(Result.PASS, "ok "..v) end),
    StepParams("second"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "should not run") end),
    StepParams("third"):setStepNo("3.")
      :setAction(function() ATE.report(Result.PASS, "also should not run") end),
  })
"""

# The same abort shape with a second blocking step (packet 30b review): the driver can prove the
# worker is parked inside step 2 before it clicks Abort, so a worker that races through steps 2 and
# 3 first cannot make the test read PASS instead of FAIL.
ABORTABLE_TWO_INPUTS = """\
return ScheduleParams("Abortable two-input schedule", "two steps block on an input")
  :setSchedule({
    StepParams("first"):setStepNo("1.")
      :setInputs({ [1] = { desc = "voltage", type = "INTEGER" } })
      :setAction(function() local v=ATE.input(1); ATE.report(Result.PASS, "ok "..v) end),
    StepParams("second"):setStepNo("2.")
      :setInputs({ [1] = { desc = "current", type = "INTEGER" } })
      :setAction(function() local v=ATE.input(1); ATE.report(Result.PASS, "ok "..v) end),
    StepParams("third"):setStepNo("3.")
      :setAction(function() ATE.report(Result.PASS, "also should not run") end),
  })
"""

# --- Packet 16a: Device registry + TCP open/close ------------------------------------------


# A step that opens a loopback TCP device (all required params) and leaves it open. The peer sees
# the connection while the step runs; because nothing closes it, the run-end teardown must close
# it with a WARNING log line and the peer must see the disconnect.
OPEN_TCP = """\
return ScheduleParams("Open TCP schedule", "opens a loopback TCP device and leaves it open")
  :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="", writeTerm=""})
        ATE.report(Result.PASS, "opened "..tostring(dev))
      end),
  })
"""

# A schedule that calls ATE.open at the top level while loading. Only the builders (including
# ate.devices), require, ATE.log and the enums are legal then; open must raise so its step never runs.
LOAD_OPEN = """\
ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="", writeTerm=""})
return ScheduleParams("Load open schedule", "opens at load time and must be rejected")
  :setSchedule({
    StepParams("should not run"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
"""

# A step that opens with an unknown field (a typo). The error is isolated: it never goes through
# onError, names the field, and is logged at ERROR — the step itself records no failure of its own.
BAD_PARAMS = """\
return ScheduleParams("Bad params schedule", "opens with an unknown field and must be isolated")
  :setSchedule({
    StepParams("bad"):setStepNo("1.")
      :setAction(function() ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, buad=5025, timeout=5, readTerm="", writeTerm=""}) end),
  })
"""

# A step that opens then closes the device and reports whether close returned {ok=true}. The peer
# sees both the connect and the disconnect while the run is live (nothing left open at run end).
CLOSE_TCP = """\
return ScheduleParams("Close TCP schedule", "opens then closes a loopback TCP device")
  :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="", writeTerm=""})
        local r = ATE.close(dev)
        -- tostring: this Lua build (lupa lua55) does not auto-convert booleans in string concat.
        ATE.report(Result.PASS, "closed "..tostring(r.ok))
      end),
  })
"""

# A step that opens and closes the device twice. Both closes report {ok=true}; the second is a no-op.
DOUBLE_CLOSE_TCP = """\
return ScheduleParams("Double close TCP schedule", "opens then double-closes a loopback TCP device")
  :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="", writeTerm=""})
        ATE.close(dev)
        ATE.close(dev)
        ATE.report(Result.PASS, "double closed")
      end),
  })
"""


class LoopbackPeer:
    """A controlled TCP listener on 127.0.0.1 that accepts connections in a background thread.

    Each peer binds an ephemeral port (so sequential tests never collide), :meth:`start` begins
    accepting, and each accepted socket is recorded so the test can assert a connection arrived;
    :meth:`stop` closes everything. Every accepted connection also gets a reader thread that records
    the bytes the device sends back into ``received`` and flags the connection in ``closed`` when its
    other end hits EOF or an error (a clean disconnect). A per-peer ``payload`` is sent to each new
    connection on accept, so a schedule's read has data buffered the moment it connects;
    :func:`wait_for_disconnect` polls for the disconnect.

    pyserial's ``socket://`` handler clears the port's input buffer inside ``open``, so bytes sent on
    accept can be flushed away before the Device's reader ever starts (packet 31b). A SERIAL test
    that wants bytes in the buffer hands them over with :meth:`send` after the open has returned,
    which is provably after that flush; or, for a test driven through the run engine (where the test
    can't act between ATE calls), passes ``after_first_byte=True`` so the payload goes out only once
    the device has written, which also proves the open returned."""

    #: Placeholder substituted with the live peer port when writing a schedule to disk, so a static
    #: Lua string can target whatever ephemeral port this run's peer chose.
    PORT_PLACEHOLDER = "__PORT__"

    #: How long a "reset" peer waits for the device's first byte before hanging up anyway. The RST
    #: must not go out straight on accept: it can land before the client's connect() returns, so the
    #: open itself fails instead of the connection dying later.
    RESET_GRACE = 1.0

    #: How long a "close" peer with no payload of its own waits for a test-side :meth:`send` before
    #: hanging up anyway: the wait is a safety net for a test that never sends, not a timing guess.
    HANGUP_GRACE = 10.0

    def __init__(self, payload: bytes = b"", hangup: str | None = None, after_first_byte: bool = False) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        # bytes the device sent back on each connection (device -> peer), keyed by the accepted socket.
        self.received: dict[socket.socket, bytes] = {}
        # connections whose other end has closed (the reader saw EOF/error).
        self.closed: set[socket.socket] = set()
        # accepted sockets and their reader thread; lets wait_for_disconnect reuse the recorded state.
        self._readers: dict[socket.socket, threading.Thread] = {}
        self.connections: list[socket.socket] = []
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._running = False
        # Sent to each accepted connection on accept (buffered by TCP), so a read has data ready.
        self._payload = payload
        # Hang up after sending the payload: "close" is an orderly close (the device reads EOF),
        # "reset" sets SO_LINGER 0 so the close sends an RST (the device's next recv/send fails) —
        # but only once the device's first byte has arrived or RESET_GRACE has elapsed, so the RST
        # can never beat the client's connect(). No byte-recording reader thread for these.
        self._hangup = hangup
        # Set by send(): a "close" peer with no payload of its own hangs up only after this.
        self._sent = threading.Event()
        # Hold the payload until the device's first byte arrives (it is recorded in ``received``):
        # pyserial's input reset inside open can't discard a reply sent after the device wrote.
        self._after_first_byte = after_first_byte

    def _first_byte(self, conn: socket.socket) -> bytes:
        """Wait (up to HANGUP_GRACE) for the device's first bytes when ``after_first_byte`` is set;
        return what arrived so the caller records it."""
        if not (self._payload and self._after_first_byte):
            return b""
        try:
            conn.settimeout(self.HANGUP_GRACE)
            return conn.recv(4096)
        except OSError:
            return b""
        finally:
            try:
                conn.settimeout(None)
            except OSError:
                pass

    def start(self) -> None:
        """Begin accepting connections (bind/listen already happened in ``__init__``)."""
        if not self._running:
            self._running = True
            self._thread.start()

    def wait_for_connection(self, n: int = 1, timeout: float = 2.0) -> bool:
        """Block until at least ``n`` connections have been accepted (or ``timeout`` elapses).

        The accept loop runs on a background thread; the connection is established when the device's
        :func:`socket.create_connection` returns but the append may not be visible yet, so callers
        that assert immediately after connecting poll here instead of racing the thread."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.connections) >= n:
                return True
            time.sleep(0.02)
        return len(self.connections) >= n

    def send(self, data: bytes) -> None:
        """Send ``data`` to every connection accepted so far.

        The SERIAL tests use this instead of a construction-time ``payload``: pyserial flushes the
        port's input buffer inside ``open``, so a payload handed to the peer at accept can be
        discarded before the reader starts. Sent after the open returns, the bytes are provably
        later than that flush, so the reader cannot miss them."""
        for conn in list(self.connections):
            try:
                conn.sendall(data)
            except OSError:
                pass  # the connection went away; there is nothing to send
        self._sent.set()

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                break  # stopped or the listen socket closed
            self.connections.append(conn)  # recorded first so wait_for_connection sees it immediately
            if self._hangup:
                # Hung up on a thread of its own so a delayed RST never blocks the accept loop.
                th = threading.Thread(target=self._hang_up, args=(conn,), daemon=True)
                self._readers[conn] = th
                th.start()
                continue
            th = threading.Thread(target=self._handle_conn, args=(conn,), daemon=True)
            self._readers[conn] = th
            th.start()

    def _hang_up(self, conn: socket.socket) -> None:
        """Send the payload, then hang up: "close" is an orderly close (the device reads EOF),
        "reset" sends an RST — but only after the device has sent its first byte, which proves the
        client's connect() returned. A device that never writes gets the reset after RESET_GRACE.
        A "close" peer given no payload waits for a test-side send() first, so the hang-up cannot
        beat the reader (see :meth:`send`)."""
        try:
            if self._payload:
                first = self._first_byte(conn)
                if first:
                    self.received[conn] = first
                conn.sendall(self._payload)
            elif self._hangup == "close":
                self._sent.wait(self.HANGUP_GRACE)  # the test sends the reply itself
            if self._hangup == "reset":
                try:
                    conn.settimeout(self.RESET_GRACE)
                    conn.recv(1)  # the device's first byte: the open is confirmed
                except OSError:
                    pass  # no byte within the grace (or the peer side is already gone)
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except OSError:
            pass  # the device already went away; still close and record it below
        finally:
            try:
                conn.close()
            finally:
                self.closed.add(conn)

    def _handle_conn(self, conn: socket.socket) -> None:
        """Send the peer's payload (if any), then record every byte the device sends until it closes."""
        buf = bytearray(self._first_byte(conn))  # empty unless after_first_byte holds the payload back
        if self._payload:
            try:
                conn.sendall(self._payload)  # buffered by TCP; a read grabs it as soon as it connects
            except OSError:
                pass  # connection already gone; nothing to send
        while self._running:
            try:
                chunk = conn.recv(4096)
            except OSError:
                break  # reset by the device side -> record what we have and stop
            if not chunk:  # orderly close reads as EOF
                break
            buf.extend(chunk)
        self.received[conn] = bytes(buf)
        self.closed.add(conn)

    def stop(self) -> None:
        """Stop accepting and close the listener."""
        self._running = False
        try:
            self._sock.close()
        except OSError:
            pass


class UdpLoopbackPeer:
    """A UDP peer on 127.0.0.1 that records the device's datagrams and answers with its own.

    Like :class:`LoopbackPeer` it binds an ephemeral port in ``__init__`` so tests never collide on a
    fixed one. UDP has no connection, so it learns the device's address from the source of the first
    datagram it receives and :meth:`send` replies to that - which is exactly the address the device
    accepts datagrams from, since its socket is connected to this peer's port. ``datagrams`` are sent
    in order as soon as the device first writes to the peer, so a schedule that writes and then reads
    finds the queue already filled. :meth:`unbind` and :meth:`rebind` take the port out of service and
    put it back: Linux answers a send to an unbound port with an ICMP port-unreachable, which is the
    ConnectionRefusedError a connected UDP socket then raises."""

    #: Placeholder substituted with the live peer port when writing a schedule to disk.
    PORT_PLACEHOLDER = "__PORT__"

    #: How long a drain waits for a datagram before waking to check the stop flag. Closing a socket
    #: from another thread does not wake a recvfrom already blocked on it, so the drain can only be
    #: stopped by a recv that times out (packet 18b review).
    RECV_POLL_SECONDS = 0.05

    def __init__(self, datagrams: list[bytes] | None = None) -> None:
        self.received: list[bytes] = []  # datagrams the device sent, in arrival order
        self.peer_addr: tuple[str, int] | None = None  # the device's local address, from its first datagram
        self._burst = list(datagrams or [])  # sent once, as soon as the device first talks to us
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self.bind()

    def bind(self, port: int | None = None) -> int:
        """Bind the peer's socket - on ``port`` when given, so a peer can return to a port a device is
        already connected to - and remember it as ``self.port``.

        The socket is given a short timeout so the drain loop wakes on the stop flag: closing a socket
        from another thread does not wake a recvfrom already blocked on it (packet 18b)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port if port is not None else 0))
        sock.settimeout(self.RECV_POLL_SECONDS)
        self._sock = sock
        self.port = sock.getsockname()[1]
        return self.port

    def start(self) -> None:
        """Start draining the device's datagrams on a background thread."""
        if not self._running:
            self._running = True
            self._thread = threading.Thread(target=self._recv_loop, daemon=True)
            self._thread.start()

    def unbind(self) -> None:
        """Close the socket so nothing listens on the port any more, which is what makes Linux refuse
        the device's sends, and end the drain so it is not left polling a dead socket."""
        self._running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._join_drain()

    def rebind(self) -> None:
        """Bind again on the same port and start a fresh drain, since unbind() ended the old one."""
        self.bind(self.port)
        if self._thread is None or not self._thread.is_alive():
            self._running = True
            self._thread = threading.Thread(target=self._recv_loop, daemon=True)
            self._thread.start()

    def send(self, data: bytes) -> None:
        """Send one datagram to the device, from this peer's port (the only one it accepts)."""
        if self.peer_addr is None:
            raise RuntimeError("the peer has not seen a datagram from the device yet")
        self._sock.sendto(data, self.peer_addr)

    def _join_drain(self) -> None:
        """Wait for the drain thread to finish (it wakes on the stop flag within RECV_POLL_SECONDS)."""
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            assert not self._thread.is_alive(), "the drain thread outlived the peer's socket"

    def _recv_loop(self) -> None:
        while self._running:
            sock = self._sock
            if sock is None:
                break  # unbind() took the port out of service; rebind() starts a fresh loop
            try:
                data, addr = sock.recvfrom(65536)  # one recv is one datagram
            except socket.timeout:
                continue  # nothing in this poll window: loop and check the stop flag again
            except OSError:
                break  # unbind() or stop() closed the socket
            self.peer_addr = addr  # the device's local address: replies go back to it
            self.received.append(data)
            for datagram in self._burst:  # reply to the first datagram with the whole burst
                try:
                    sock.sendto(datagram, addr)  # the same socket we just read from
                except OSError:
                    pass
            self._burst = []

    def wait_for_datagram(self, n: int = 1, timeout: float = 2.0) -> bool:
        """Block until at least ``n`` datagrams have arrived (or ``timeout`` elapses)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.received) >= n:
                return True
            time.sleep(0.02)
        return len(self.received) >= n

    def stop(self) -> None:
        """Stop draining, close the socket (already closed by unbind() is fine), and wait for the
        drain thread to be gone so the port is really free."""
        self._running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._join_drain()


def wait_for_disconnect(peer: LoopbackPeer, timeout: float = 2.0, n: int = 1) -> bool:
    """Poll until ``n`` recorded peer connections have closed (the device closed it, or a hang-up peer
    closed/reset its side), or ``timeout`` elapses.

    Every accepted socket has a peer thread (reader, or hang-up) that adds it to ``closed`` when the
    connection ends, so this only reads that set — never recv, which would race the reader and steal
    bytes. Returns True if ``n`` recorded connections were seen to close; False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sum(conn in peer.closed for conn in peer.connections) >= n:
            return True
        time.sleep(0.02)
    return False


def _drive(engine: "RunEngine", app, on_run_uuid=None) -> None:
    """Run the worker to completion, then deliver the signals it queued onto this thread.

    The worker's signals reach the test through queued connections, so they only fire once the
    main thread processes posted events. Wait on the thread itself (a real deadline, not a fixed
    number of ``processEvents`` calls, which a slow or busy machine can exhaust before the worker
    has even emitted), then flush the posted events so every queued slot has run."""
    if on_run_uuid is not None:
        engine.run_uuid_reported.connect(on_run_uuid)
    engine.start()
    assert engine.wait(10_000), "run engine did not finish within 10 s"
    app.sendPostedEvents()
    app.processEvents()


def _sha256(path: Path) -> str:
    return __import__("hashlib").sha256(Path(path).read_bytes()).hexdigest()


def write_ate_env(tmp_path: Path) -> dict[str, Path]:
    """Write a config + one Test Definition with three schedules; return path map.

    Returns ``{"config": Path, "defs_dir": Path, "db": Path}``. The database file is not
    created yet — the caller can assert on launch-time refusals before migrating it.
    """
    db = tmp_path / "ate.db"
    reports_dir = tmp_path / "reports"
    defs_dir = tmp_path / "defs"
    defs_dir.mkdir(parents=True, exist_ok=True)
    (defs_dir / "passing.tslua").write_text(PASSING, encoding="utf-8")
    (defs_dir / "failing.tslua").write_text(FAILING, encoding="utf-8")
    (defs_dir / "stop_on_fail_false.tslua").write_text(STOP_ON_FAIL_FALSE, encoding="utf-8")
    (defs_dir / "erroring.tslua").write_text(ERRORING, encoding="utf-8")
    (defs_dir / "logging.tslua").write_text(LOGGING, encoding="utf-8")
    (defs_dir / "pre_post_pass.tslua").write_text(PRE_POST_PASS, encoding="utf-8")
    (defs_dir / "pre_post_fail.tslua").write_text(PRE_POST_FAIL, encoding="utf-8")
    (defs_dir / "pre_error_post_ok.tslua").write_text(PRE_ERROR_POST_OK, encoding="utf-8")
    (defs_dir / "doc_fields.tslua").write_text(DOC_FIELDS, encoding="utf-8")

    # Ticket 10 schedules: one blocking input, one POLL-only step, and a step declaring every Step
    # Input type. Written into the demo Test Definition so seam/staged tests can select them by name.
    (defs_dir / "blocking_input.tslua").write_text(BLOCKING_INPUT, encoding="utf-8")
    (defs_dir / "poll_input.tslua").write_text(POLL_INPUT, encoding="utf-8")
    (defs_dir / "all_types_inputs.tslua").write_text(ALL_TYPES_INPUTS, encoding="utf-8")
    (defs_dir / "abortable.tslua").write_text(ABORTABLE, encoding="utf-8")
    (defs_dir / "abortable_two_inputs.tslua").write_text(ABORTABLE_TWO_INPUTS, encoding="utf-8")
    (defs_dir / "enable_disabled.tslua").write_text(ENABLE_DISABLED, encoding="utf-8")
    (defs_dir / "fail_then_pass.tslua").write_text(STEPS_FAIL_THEN_PASS, encoding="utf-8")
    (defs_dir / "passes_on_retry.tslua").write_text(FAIL_THEN_PASS, encoding="utf-8")

    # Packet 16a schedules are exercised directly by the seam tests in
    # test_device_registry_16a.py (which substitute the live peer port), so they are not part of
    # this demo Test Definition.

    atedef = {
        "format": 1,
        "mediaNumber": "REL-A",
        "name": "Demo release",
        "version": "1.0.0",
        "files": [
            {"path": "passing.tslua", "sha256": _sha256(defs_dir / "passing.tslua")},
            {"path": "failing.tslua", "sha256": _sha256(defs_dir / "failing.tslua")},
            {"path": "stop_on_fail_false.tslua", "sha256": _sha256(defs_dir / "stop_on_fail_false.tslua")},
            {"path": "erroring.tslua", "sha256": _sha256(defs_dir / "erroring.tslua")},
            {"path": "logging.tslua", "sha256": _sha256(defs_dir / "logging.tslua")},
            {"path": "pre_post_pass.tslua", "sha256": _sha256(defs_dir / "pre_post_pass.tslua")},
            {"path": "pre_post_fail.tslua", "sha256": _sha256(defs_dir / "pre_post_fail.tslua")},
            {"path": "pre_error_post_ok.tslua", "sha256": _sha256(defs_dir / "pre_error_post_ok.tslua")},
            {"path": "blocking_input.tslua", "sha256": _sha256(defs_dir / "blocking_input.tslua")},
            {"path": "poll_input.tslua", "sha256": _sha256(defs_dir / "poll_input.tslua")},
            {"path": "all_types_inputs.tslua", "sha256": _sha256(defs_dir / "all_types_inputs.tslua")},
            {"path": "abortable.tslua", "sha256": _sha256(defs_dir / "abortable.tslua")},
            {"path": "abortable_two_inputs.tslua", "sha256": _sha256(defs_dir / "abortable_two_inputs.tslua")},
            {"path": "enable_disabled.tslua", "sha256": _sha256(defs_dir / "enable_disabled.tslua")},
            {"path": "fail_then_pass.tslua", "sha256": _sha256(defs_dir / "fail_then_pass.tslua")},
            {"path": "passes_on_retry.tslua", "sha256": _sha256(defs_dir / "passes_on_retry.tslua")},
        ],
    }
    (defs_dir / "demo.atedef").write_text(json.dumps(atedef), encoding="utf-8")

    config = {
        "format": 1,
        "station": {"name": "BENCH-03", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c10"},
        "defaultProject": "Power board",
        "database": {"type": "sqlite", "path": str(db)},
        "reports": {"directory": str(reports_dir)},
        "testDefinitions": {"directories": [str(defs_dir)]},
        # The log directory is spelled out so the default <data>/logs never reaches the real
        # platform data directory in a test run (packet 30c resolved <data> for real).
        "log": {"directory": str(tmp_path / "logs")},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    return {"config": config_path, "defs_dir": defs_dir, "db": db, "reports_dir": reports_dir}


def read_run_row(store, run_uuid: str) -> dict:
    """Return a recorded run row as a dict (or ``None``)."""
    return store.get_test_run(run_uuid)


# --- Ticket 10 step inputs ---------------------------------------------------


# A schedule whose single step asks one blocking INTEGER input, then reports it back. The driver
# supplies a value from another thread while the worker waits; once supplied the step records the
# typed value and passes.
BLOCKING_INPUT = """\
return ScheduleParams("Blocking input schedule", "a step that blocks on one INTEGER input")
  :setSchedule({
    StepParams("voltage"):setStepNo("1.")
      :setInputs({ [1] = { desc = "voltage", type = "INTEGER" } })
      :setAction(function() local v=ATE.input(1); ATE.report(Result.PASS, "voltage "..v) end),
  })
"""

# A schedule whose one step asks for a POLL and a blocking PROMPT. The POLL returns nil until its
# button is clicked (recorded as an input_value row of type POLL), then that text; sticky across the
# step and reset at each run/step start.
#
# The PROMPT is not decoration: a POLL click never blocks the worker, so in a POLL-only step the
# worker's read and the operator thread's click race, and a click that arrives after the Attempt's
# input window closes is dropped. The driver holds the blocking answer back until the click has
# landed, so the worker is provably still inside the window when the click happens.
POLL_INPUT = """\
return ScheduleParams("Poll input schedule", "a step whose POLL button is clicked while it waits on a blocking input")
  :setSchedule({
    StepParams("status check"):setStepNo("1.")
      :setInputs({
        [1] = { desc = "device status", type = "POLL", prompt = {"OK", "Error"} },
        [2] = { desc = "operator go", type = "PROMPT", prompt = {"go"} },
      })
      :setAction(function()
        ATE.input(2)                -- blocks this worker until the operator thread answers
        local s = ATE.input(1)      -- POLL: nil until clicked, then the button text
        ATE.report(Result.PASS, "status " .. s)
      end),
  })
"""

# A schedule whose single step declares every Step Input type. Each is supplied through the real
# run engine and recorded as its own input_value row: CHECKBOX arrives as a list of labels and is
# stored as JSON; POLL clicks are recorded when clicked; the rest carry their typed value.
ALL_TYPES_INPUTS = """\
return ScheduleParams("All types schedule", "a step that declares every Step Input type")
  :setSchedule({
    StepParams("mixed"):setStepNo("1.")
      :setInputs({
        [1] = { desc = "voltage", type = "INTEGER", range = {min = 0, max = 100},
                tooltip = "Supply voltage in volts", orient = "VERT" },
        [2] = { desc = "current", type = "FLOAT", tooltip = "Ammeter reading in amps", orient = "HORIZ" },
        [3] = { desc = "serial port", type = "STRING" },
        [4] = { desc = "mode", type = "ENUM", input = "RADIOBUT", enum = {"Slow", "Fast"} },
        [5] = { desc = "channels", type = "ENUM", input = "CHECKBOX", enum = {"A", "B", "C"} },
        [6] = { desc = "prompt text", type = "PROMPT", prompt = {"typed"} },
      })
      :setAction(function()
        local v=ATE.input(1); local c=ATE.input(2); local s=ATE.input(3)
        local m=ATE.input(4); local ch=ATE.input(5); local p=ATE.input(6)
        ATE.report(Result.PASS, "v"..v.." c"..c.." s"..s.." m"..m[1].." ch"..ch[1].." p"..p)
      end),
  })
"""


# How the driver answers each announced input while pumping events — its own vocabulary, distinct
# from which schedule is on disk (BLOCKING_INPUT / POLL_INPUT / ALL_TYPES_INPUTS). A blocking
# schedule supplies a value; a poll schedule clicks its button.
INPUTS_BLOCKING = "blocking"  # supply every blocking input from another thread while it waits
INPUTS_POLL = "poll"         # click every POLL button while its step runs
INPUTS_ALL = "all"           # both: supply blocking inputs and click polls as the run proceeds


# Ticket 10 concurrent-supply driver helpers. The worker announces each asked input via the queued
# ``input_asked`` signal before its action runs; these handlers supply a blocking value or click a
# POLL button from another thread (so a blocked get_raw wakes while the worker waits and a POLL is
# clicked while its step runs), then pump events until FINISHED.


def _default_input_value(key: int) -> Any:
    """A type-appropriate default for a blocking supply when no explicit value is given.

    Position-based so a bare driver still records something sensible per index; tests that assert
    exact values pass ``values_by_index`` instead. CHECKBOX returns a list of labels so it is stored
    as JSON (ticket 10 checkbox e)."""
    if key == 4:
        return ["a", "b"]  # CHECKBOX -> stored as a JSON array
    if key == 3:
        return "option"  # RADIOBUT / PUSHBUT single label
    return float(key)  # INTEGER / FLOAT / STRING / PROMPT all coerce fine


def _pump_until_finished(app, engine, op, strategy=INPUTS_BLOCKING, values_by_index=None):
    """Announce-and-supply inputs concurrently, then pump events until the run reaches FINISHED.

    The handler runs on this (pumping) thread; a blocking supply uses a plain queue so it wakes the
    worker's ``get_raw`` synchronously while we wait, and POLL clicks set sticky state immediately."""
    def _on_input_asked(seq, attempt_no, index):
        key = index - 1  # 0-based position into this step's declarations
        if strategy == INPUTS_POLL:
            op.click_poll(node_seq=seq, attempt_no=attempt_no, index=key, text="clicked")
        else:
            value = values_by_index[key] if values_by_index is not None else _default_input_value(key)
            op.supply_input(key, value)

    finished = threading.Event()
    engine.input_asked.connect(_on_input_asked)
    engine.state_changed.connect(lambda status: finished.set() if status == RunStatus.FINISHED else None)

    deadline = time.time() + 10.0
    while not finished.is_set():
        app.processEvents()
        if time.time() > deadline:
            raise TimeoutError("run did not reach FINISHED within 10 s")


def _drive_with_operator(app, engine, op, strategy=INPUTS_BLOCKING, values_by_index=None):
    """Start the worker with an injected OperatorInput and pump until FINISHED.

    For tests that construct the RunEngine directly: start it here (it will block on a blocking input)
    then hand control to :func:`_pump_until_finished`, which supplies/clicks inputs from this thread."""
    engine.start()
    _pump_until_finished(app, engine, op, strategy, values_by_index=values_by_index)


# --- Packet 25a schedules ------------------------------------------------------
#
# Added onto the demo Test Definition by a helper rather than in write_ate_env, so the tests that
# count the picker's rows are unaffected until they ask for these.


def _append_file(defs_dir: Path, name: str, source: str) -> None:
    """Write one schedule file into the demo Test Definition and register its checksum."""
    path = defs_dir / name
    path.write_text(source, encoding="utf-8")
    atedef = json.loads((defs_dir / "demo.atedef").read_text(encoding="utf-8"))
    atedef["files"].append({"path": name, "sha256": _sha256(path)})
    (defs_dir / "demo.atedef").write_text(json.dumps(atedef), encoding="utf-8")


def add_long_schedule(tmp_path: Path, steps: int = 60, fail_at: int = 30) -> str:
    """Add a 60-step schedule that pauses on a Stop-on-Fail failure at step 30.

    Long enough for the Run screen to read the store while the worker is still inserting rows,
    which is where packet 25a's "database is locked" abort showed up: a read whose statement is
    still open when its connection goes back to the pool collides with the next INSERT on it."""
    defs = tmp_path / "defs"
    schedule = ",\n".join(
        f'    StepParams("step {i}"):setStepNo("{i}.")\n'
        f'      :setAction(function() ATE.log(Log.INFO, "step {i} running") '
        f'ATE.report({"Result.FAIL" if i == fail_at else "Result.PASS"}, "ok {i}") end)'
        for i in range(1, steps + 1)
    )
    source = (
        f'return ScheduleParams("Long 60 schedule", "sixty steps, Stop on Fail at {fail_at}")\n'
        "  :setStopOnFail(true)\n"
        f"  :setSchedule({{\n{schedule}\n  }})\n"
    )
    _append_file(defs, "long60.tslua", source)
    return "Long 60 schedule"


def add_header_schedule(tmp_path: Path) -> str:
    """Add a schedule with two Sub-schedule headers: one over two Test Steps, one over one.

    The headers are `setInclude` rows (packet 15b) over child files in the same Media Release, so the
    step table has boxes derived from the rows beneath them, and the header with two children can
    read part-ticked."""
    defs = tmp_path / "defs"
    pair = (
        'return ScheduleParams("Pair sub-schedule", "two steps under one header")\n'
        "  :setSchedule({\n"
        '    StepParams("measure a"):setStepNo("1.")\n'
        '      :setAction(function() ATE.report(Result.PASS, "a ok") end),\n'
        '    StepParams("measure b"):setStepNo("2.")\n'
        '      :setAction(function() ATE.report(Result.PASS, "b ok") end),\n'
        "  })\n"
    )
    child = (
        'return ScheduleParams("Channel sub-schedule", "one step per channel")\n'
        "  :setSchedule({\n"
        '    StepParams("measure"):setStepNo("1.")\n'
        '      :setAction(function() ATE.report(Result.PASS, "channel ok") end),\n'
        "  })\n"
    )
    _append_file(defs, "pair.tslua", pair)
    _append_file(defs, "channel.tslua", child)
    parent = (
        'return ScheduleParams("Sub-schedule header schedule", "two headers over three steps")\n'
        "  :setSchedule({\n"
        '    StepParams("pair header"):setStepNo("2."):setPrefix("2."):setInclude("pair.tslua"),\n'
        '    StepParams("channel 1"):setStepNo("3."):setPrefix("3."):setInclude("channel.tslua", { channel = 1 }),\n'
        "  })\n"
    )
    _append_file(defs, "headers.tslua", parent)
    return "Sub-schedule header schedule"


def add_logging_input_schedule(tmp_path: Path) -> str:
    """Add a schedule that logs at INFO and DEBUG, then blocks on one INTEGER input.

    The lines logged before `ATE.input` are in the store by the time the ask is announced, so a log
    pane refreshed on the session's signals shows them while the step is still waiting. The step also
    declares a POLL, whose widget stays live until the run ends: a step interrupted by an abort never
    reaches `step_finished`, so closing inputs at FINISHED is the only thing that shuts it."""
    defs = tmp_path / "defs"
    source = (
        'return ScheduleParams("Logging input schedule", "logs, then blocks on an input")\n'
        "  :setSchedule({\n"
        '    StepParams("voltage"):setStepNo("1.")\n'
        '      :setInputs({ [1] = { desc = "voltage", type = "INTEGER" },\n'
        '                   [2] = { desc = "operator confirm", type = "POLL", prompt = {"OK", "Bad"} } })\n'
        "      :setAction(function()\n"
        '        ATE.log(Log.INFO, "measuring before the Operator answers")\n'
        '        ATE.log(Log.DEBUG, "probe chatter before the ask")\n'
        "        local v = ATE.input(1)\n"
        '        ATE.report(Result.PASS, "voltage " .. v)\n'
        "      end),\n"
        "  })\n"
    )
    _append_file(defs, "logging_input.tslua", source)
    return "Logging input schedule"


def add_failing_poll_schedule(tmp_path: Path) -> str:
    """Add a Stop-on-Fail schedule whose failing step also declares a POLL Step Input.

    The POLL is announced before the Action runs, so its widget is live while the step is running and
    stays live through the pause: a run the Operator aborts ends with an input that must be closed."""
    defs = tmp_path / "defs"
    source = (
        'return ScheduleParams("Failing poll schedule", "a failing step with a POLL input")\n'
        "  :setStopOnFail(true)\n"
        "  :setSchedule({\n"
        '    StepParams("first"):setStepNo("1.")\n'
        '      :setInputs({ [1] = { desc = "operator decision", type = "POLL", prompt = {"OK", "Bad"} } })\n'
        '      :setAction(function() ATE.report(Result.FAIL, "the UUT is bad") end),\n'
        '    StepParams("second"):setStepNo("2.")\n'
        '      :setAction(function() ATE.report(Result.PASS, "ok") end),\n'
        "  })\n"
    )
    _append_file(defs, "failing_poll.tslua", source)
    return "Failing poll schedule"


def fake_pdf_helper(script: str):
    """A stand-in for :func:`ate.report.pdf.pdf_helper_command`: a tiny Python process that gets the
    PDF path as ``argv[1]``, so tests exercise the real QProcess path without QWebEngine.

    It takes the same arguments as the real helper, including packet 30d's ``unsandboxed``."""
    import os
    import sys

    def command(html_path, pdf_path, paper="A4", unsandboxed=False, log=None):  # noqa: ANN001 - mirrors pdf_helper_command
        return sys.executable, ["-c", script, str(pdf_path)], dict(os.environ)

    return command


WRITES_PDF = "import sys; open(sys.argv[1], 'wb').write(b'%PDF-1.4 stub')"


def recording_pdf_helper(record: list, script: str = WRITES_PDF):
    """A stand-in that keeps the child trivial but records what the real helper would have been given.

    Each call appends ``{"args": [...], "env": {...}}` for one PDF (packet 30d): the test then asserts
    which sandbox settings reached the helper without spawning QWebEngine."""
    import sys

    from ate.report.pdf import pdf_helper_command

    def command(html_path, pdf_path, paper="A4", unsandboxed=False, log=None):  # noqa: ANN001 - mirrors pdf_helper_command
        _program, args, env = pdf_helper_command(html_path, pdf_path, paper, bool(unsandboxed), log)
        record.append({"args": args, "env": env})
        return sys.executable, ["-c", script, str(pdf_path)], env

    return command
