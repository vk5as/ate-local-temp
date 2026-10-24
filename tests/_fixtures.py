"""Shared helpers for ticket 02's seam tests: build a minimal on-disk ATE environment.

These write a config pointing at a fresh SQLite database and one Test Definition
directory holding three schedules (one that passes, one that fails, one that raises), so
the headless session and CLI can be exercised end-to-end against real files — no mocks.
"""

from __future__ import annotations

import json
import socket
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

# A whole-run abort fixture (packets 13a). Its first step blocks on a single INTEGER input, so the
# driver has a guaranteed window between steps: while that step waits it supplies its value and
# requests an abort. The engine then records the blocked step PASS, marks the next step ERROR
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


# --- Packet 16b: TCP read/write ----------------------------------------------------------


# The peer sends a full terminator-terminated message on accept (buffered by TCP). size=20 is generous
# so one recv grabs everything and the open-time readTerm "\n" strips it -> complete read. r.data carries
# the bytes as an ASCII hex string (binary-safe through the result table); the test decodes it and
# compares to the known payload, and reports count/partial/err for inspection.
READ_COMPLETE = """\
return ScheduleParams("Read complete schedule", "reads a full terminator-terminated message")
  :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="\\n", writeTerm=""})
        local r = ATE.read(dev, {size=20})
        ATE.report(Result.PASS, "data "..r.data.." count "..r.count.." partial "..tostring(r.partial).." err "..(r.err or ""))
        -- Echo the result fields back to the peer as newline-delimited tokens so the test can assert
        -- exact values through the recorded bytes (data/count/partial/err; data is hex, never nil here).
        ATE.write(dev, {data = r.data .. "\n" .. tostring(r.count) .. "\n" .. tostring(r.partial) .. "\n" .. (r.err or "")})
      end),
  })
"""

# Same shape but the payload contains high bytes (>=128) that would break per-byte "\ddd" escapes.
# read's result carries them as a hex string and the step echoes it back so the peer records exactly
# what came through the result table; the test decodes those recorded bytes and checks they equal the
# original payload, proving raw bytes survive the round trip. Reports count/partial/err for inspection.
BINARY_ECHO = """\
return ScheduleParams("Binary echo schedule", "reads raw bytes (hex-encoded) and echoes them back")
  :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="\\n", writeTerm=""})
        local r = ATE.read(dev, {size=64})
        -- Echo the result fields back to the peer so the test can decode exactly what read returned.
        ATE.write(dev, {data = r.data .. "\n" .. tostring(r.count) .. "\n" .. tostring(r.partial) .. "\n" .. (r.err or "")})
        ATE.report(Result.PASS, "data "..r.data.." count "..r.count.." partial "..tostring(r.partial).." err "..(r.err or ""))
      end),
  })
"""

# The peer sends only a few bytes then goes quiet; the read grabs them but times out before the rest,
# so it returns partial=true with count = the bytes received. Also exercises the per-call timeout override.
PARTIAL_TIMEOUT = """\
return ScheduleParams("Partial read schedule", "reads some bytes then times out before the rest")
  :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="\\n", writeTerm=""})
        local r = ATE.read(dev, {size=10, timeout=0.5})
        ATE.report(Result.PASS, "data "..r.data.." count "..r.count.." partial "..tostring(r.partial).." err "..(r.err or ""))
        -- Echo the result fields back to the peer so the test can assert exact values through recorded bytes.
        ATE.write(dev, {data = r.data .. "\n" .. tostring(r.count) .. "\n" .. tostring(r.partial) .. "\n" .. (r.err or "")})
      end),
  })
"""

# The peer sends nothing; the read waits out its timeout with zero bytes -> {data=nil,count=0,err="timeout"}.
TIMEOUT_NOBYTES = """\
return ScheduleParams("Read timeout schedule", "times out waiting for any bytes")
  :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="\\n", writeTerm=""})
        local r = ATE.read(dev, {size=10, timeout=0.5})
        ATE.report(Result.PASS, "data "..r.data.." count "..r.count.." partial "..tostring(r.partial).." err "..(r.err or ""))
        -- Echo the result fields back to the peer so the test can assert exact values through recorded bytes.
        ATE.write(dev, {data = r.data .. "\n" .. tostring(r.count) .. "\n" .. tostring(r.partial) .. "\n" .. (r.err or "")})
      end),
  })
"""

# Opens with no write terminator and appends one per-call; the peer records exactly what was sent so the
# test can assert count includes the terminator (4 = "cmd" + "\n").
WRITE_TERM = """\
return ScheduleParams("Write terminator schedule", "writes data with a per-call terminator appended")
  :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="", writeTerm=""})
        local w = ATE.write(dev, {data="cmd", writeTerm="\\n"})
        ATE.report(Result.PASS, "ok "..tostring(w.ok).." count "..w.count.." err "..(w.err or ""))
      end),
  })
"""

# The peer closes its end right after accepting (fail_write); the write can't send and must fail with
# ok=false + an error message, never raising. Closing in-step leaves nothing open at run end.
WRITE_FAIL = """\
return ScheduleParams("Write failure schedule", "writes to a connection the peer has closed")
  :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="", writeTerm=""})
        local w = ATE.write(dev, {data="x"})
        ATE.close(dev)
        ATE.report(Result.PASS, "ok "..tostring(w.ok).." count "..w.count.." err "..(w.err or ""))
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
    :func:`wait_for_disconnect` polls for the disconnect."""

    #: Placeholder substituted with the live peer port when writing a schedule to disk, so a static
    #: Lua string can target whatever ephemeral port this run's peer chose.
    PORT_PLACEHOLDER = "__PORT__"

    def __init__(self, payload: bytes = b"", fail_write: bool = False) -> None:
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
        # When true the peer closes its end of the socket right after accepting, so any device write
        # gets EPIPE — a deterministic write failure (no reader thread is started for such connections).
        self._fail_write = fail_write

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

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                break  # stopped or the listen socket closed
            self.connections.append(conn)  # recorded first so wait_for_connection sees it immediately
            if self._fail_write:
                # Close the peer's end right away (synchronously here, before create_connection returns
                # to the device) so any subsequent write gets EPIPE — a deterministic failure.
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            th = threading.Thread(target=self._handle_conn, args=(conn,), daemon=True)
            self._readers[conn] = th
            th.start()

    def _handle_conn(self, conn: socket.socket) -> None:
        """Send the peer's payload (if any), then record every byte the device sends until it closes."""
        if self._payload:
            try:
                conn.sendall(self._payload)  # buffered by TCP; a read grabs it as soon as it connects
            except OSError:
                pass  # connection already gone; nothing to send
        buf = bytearray()
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

    def send_to_last(self, data: bytes, timeout: float = 2.0) -> bool:
        """Wait for the latest connection to be recorded, then send ``data`` on it (for ad-hoc sends)."""
        deadline = time.time() + timeout
        while not self.connections and time.time() < deadline:
            time.sleep(0.02)
        if self.connections:
            try:
                self.connections[-1].sendall(data)
                return True
            except OSError:
                return False
        return False

    def stop(self) -> None:
        """Stop accepting and close the listener."""
        self._running = False
        try:
            self._sock.close()
        except OSError:
            pass


def wait_for_disconnect(peer: LoopbackPeer, timeout: float = 2.0) -> bool:
    """Poll until a recorded peer connection closes (device side closed it), or ``timeout`` elapses.

    When the peer has reader threads for an accepted socket (not under fail_write mode), we rely only
    on the ``closed`` set that the reader thread populates at EOF/error — no recv/fallback to avoid
    racing with the reader and stealing bytes. For connections under ``fail_write`` that have no reader
    thread, we fall back to polling recv (the 16a open/close schedules with no readers). Returns True
    if any recorded connection was seen to close; False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for conn in list(peer.connections):
            # Check peer.closed first — this is populated by the reader thread when it sees EOF
            # For connections under fail_write (no reader thread), we must check closed anyway
            if conn in peer.closed:
                return True
            # Connections with a reader thread should only use closed set (avoid recv racing).
            # Only fallback to recv polling for connections under fail_write mode that lack reader threads.
            if conn not in peer._readers:
                # This connection has no reader — safe to poll recv
                try:
                    conn.settimeout(0.05)
                    r = conn.recv(1)
                    if r == b"":  # an orderly close reads as EOF
                        return True
                except socket.timeout:
                    continue  # still open
                except (ConnectionError, OSError):  # reset by the device side
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
    (defs_dir / "enable_disabled.tslua").write_text(ENABLE_DISABLED, encoding="utf-8")

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
            {"path": "enable_disabled.tslua", "sha256": _sha256(defs_dir / "enable_disabled.tslua")},
        ],
    }
    (defs_dir / "demo.atedef").write_text(json.dumps(atedef), encoding="utf-8")

    config = {
        "format": 1,
        "station": {"name": "BENCH-03", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c10"},
        "database": {"type": "sqlite", "path": str(db)},
        "reports": {"directory": str(reports_dir)},
        "testDefinitions": {"directories": [str(defs_dir)]},
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

# A schedule whose single step's only input is a POLL button. It returns nil until the operator
# clicks one of its buttons (recorded as an input_value row of type POLL), then that text; sticky
# across the step and reset at each run/step start.
POLL_INPUT = """\
return ScheduleParams("Poll input schedule", "a step whose only input is a POLL button")
  :setSchedule({
    StepParams("status check"):setStepNo("1.")
      :setInputs({ [1] = { desc = "device status", type = "POLL", prompt = {"OK", "Error"} } })
      :setAction(function() local s=ATE.input(1); ATE.report(Result.PASS, "status "..s) end),
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
        [1] = { desc = "voltage", type = "INTEGER" },
        [2] = { desc = "current", type = "FLOAT" },
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


def fake_pdf_helper(script: str):
    """A stand-in for :func:`ate.report.pdf.pdf_helper_command`: a tiny Python process that gets the
    PDF path as ``argv[1]``, so tests exercise the real QProcess path without QWebEngine."""
    import os
    import sys

    def command(html_path, pdf_path, paper="A4"):  # noqa: ANN001 - mirrors pdf_helper_command
        return sys.executable, ["-c", script, str(pdf_path)], dict(os.environ)

    return command


WRITES_PDF = "import sys; open(sys.argv[1], 'wb').write(b'%PDF-1.4 stub')"
