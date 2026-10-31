"""Packet 18b - UDP Devices: datagrams behind the same ATE API as TCP.

One datagram is one message, so there are no terminators and no partial reads except a datagram
``size`` cut short: the reader thread queues whole datagrams, ``read`` takes the oldest one (waiting
for the timeout only when the queue is empty), ``write`` sends one datagram per call, and the cap
counts the total bytes queued, dropping whole oldest datagrams on overflow. Seam-1 coverage against
a UDP peer on 127.0.0.1 (no mocks); the reader, failure, warning and overflow plumbing is the shared
streaming device in :mod:`ate.devices.stream`, so the tests here are the parts UDP changes.

The one UDP quirk the driver has to absorb: on Linux a connected UDP socket whose peer is not
listening answers an ICMP port-unreachable, so ``recv``/``send`` raise ConnectionRefusedError. A peer
that has not started listening yet is not a broken device - it may start later - so the reader keeps
polling and only the send that hit it fails.
"""

from __future__ import annotations

import errno
import socket
import tempfile
import time
from collections import deque
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication

from ate.devices.udp import UdpDriver
from ate.engine.lua_host import LuaHost

from ._fixtures import UdpLoopbackPeer
from .test_device_onerror import _dead_port
from .test_receive_buffer import _run_with_peer_limited
from .test_tcp_read_write import _step_message


def _params(peer: UdpLoopbackPeer, local_port: int = 0) -> dict:
    """The open params for a UDP device talking to ``peer``, as a Python dict."""
    return {"host": "127.0.0.1", "port": peer.port, "localPort": local_port, "timeout": 5}


def _open(peer: UdpLoopbackPeer, limit: int | None = None):
    """Open a UDP device on ``peer`` (driver level) and start its reader."""
    params = _params(peer)
    return UdpDriver().open(params, limit) if limit is not None else UdpDriver().open(params)


def _talk(dev, peer: UdpLoopbackPeer) -> None:
    """Send one datagram so the peer learns the device's local address, and wait until it did: a UDP
    peer has no connection to inherit, so it can only reply to what it has seen."""
    dev.write({"data": "go"})
    assert peer.wait_for_datagram(1), "the peer never saw the device's datagram"


def _wait_for(dev, predicate, timeout: float = 2.0) -> None:
    """Wait, under the device lock, until ``predicate(dev)`` holds (the reader notifies on every
    datagram, flush and failure) rather than sleeping for a guessed time."""
    with dev._cond:
        assert dev._cond.wait_for(lambda: predicate(dev), timeout), "device never reached the expected state"


def _step(body: str) -> str:
    """A one-step schedule that opens a UDP device on the peer as ``dev`` and runs ``body``."""
    return f"""return ScheduleParams("x") :setSchedule({{
    StepParams("io"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, {{host="127.0.0.1", port=__PORT__, localPort=0, timeout=5}})
{body}
        ATE.close(dev)
      end),
  }})"""


# --- Seam 1: a schedule talks to a UDP peer -------------------------------------------------------


def test_open_write_and_read_a_reply_through_lua(app: QCoreApplication) -> None:
    """Every open field is taken, the write goes out as one datagram, and the peer's reply comes back
    whole: count is the datagram's length, partial is false, err nil."""
    peer = UdpLoopbackPeer(datagrams=[b"pong"])
    peer.start()
    try:
        store, ru = _run_with_peer_limited(app, _step("""
        local w = ATE.write(dev, {data = "ping"})
        local r = ATE.read(dev, {timeout = 2})
        ATE.report(Result.PASS, w.count .. " " .. tostring(r.data) .. " " .. r.count .. " "
            .. tostring(r.partial) .. " " .. tostring(r.err))"""), peer.port)
    finally:
        peer.stop()

    assert peer.received == [b"ping"], f"the peer saw {peer.received}"
    assert _step_message(store, ru) == "4 pong 4 false nil"


def test_seam_a_truncated_datagram_is_partial_and_its_tail_is_gone(app: QCoreApplication) -> None:
    """``size`` is the only thing that makes a UDP read partial: the datagram comes back cut at 4 with
    partial = true, and the next read finds nothing, because the rest of that datagram is discarded."""
    peer = UdpLoopbackPeer(datagrams=[b"0123456789"])
    peer.start()
    try:
        store, ru = _run_with_peer_limited(app, _step("""
        ATE.write(dev, {data = "go"})
        local r1 = ATE.read(dev, {size = 4, timeout = 1})
        local r2 = ATE.read(dev, {timeout = 0.3}, function() return "empty" end)
        ATE.report(Result.PASS, r1.data .. " " .. r1.count .. " " .. tostring(r1.partial) .. " " .. r2)"""),
            peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "0123 4 true empty"
    partials = [ln for ln in store.log_lines(ru)
                if ln["level"] == "WARNING" and "datagram truncated to 4 byte" in ln["text"]]
    assert len(partials) == 1, f"a truncated datagram is logged once, got {partials}"


_BAD_OPEN_FIELDS = [
    ({"readTerm": "\n"}, "unknown field 'readTerm'"),
    ({"writeTerm": "\n"}, "unknown field 'writeTerm'"),
    ({"prot": "UDP"}, "unknown field 'prot'"),
    ({"host": None}, "missing required field 'host'"),
    ({"port": None}, "missing required field 'port'"),
    ({"localPort": None}, "missing required field 'localPort'"),
    ({"port": 0}, "field 'port' must be between 1 and 65535"),
    ({"port": 70000}, "field 'port' must be between 1 and 65535"),
    ({"localPort": -1}, "field 'localPort' must be between 0 and 65535"),
    ({"timeout": 0}, "field 'timeout' must be greater than 0"),
]


@pytest.mark.parametrize("change, expected", _BAD_OPEN_FIELDS)
def test_udp_takes_no_terminators_and_rejects_a_bad_field_naming_it(
    app: QCoreApplication, change: dict, expected: str
) -> None:
    """A field UDP doesn't know (readTerm, writeTerm, a typo) or a missing/bad value is a schedule
    bug: it raises naming the field, neither open's nor the call's onError runs, and no socket is
    opened. The peer is never touched, so nothing to stop."""
    params = {"host": "127.0.0.1", "port": "__PORT__", "localPort": 0, "timeout": 5}
    for field, value in change.items():
        if value is None:
            params.pop(field)
        else:
            params[field] = value
    lua = ", ".join(
        f"{field}={value}" if value == "__PORT__" else f"{field}={value!r}"
        for field, value in params.items()
    )
    store, ru = _run_with_peer_limited(
        app,
        f"""return ScheduleParams("x") :setSchedule({{
    StepParams("io"):setStepNo("1.")
      :setAction(function()
        local calls = {{}}
        local mark = function() calls[#calls + 1] = "called"; return "handled" end
        local msg = select(2, ATE.open(DeviceType.UDP, {{{lua}}}, mark))
        ATE.report(Result.PASS, #calls .. " " .. msg)
      end),
  }})""",
        12345,
    )
    count, *msg = _step_message(store, ru).split(" ", 1)
    assert count == "0", f"onError must never run for a schedule bug: {count} calls"
    assert expected in msg[0], f"the error must name the field: {msg[0]!r}"


@pytest.mark.parametrize("call, expected", [
    ('ATE.read(dev, {timeout = 1, readTerm = "\\n"}, mark)', "unknown field 'readTerm'"),
    ('ATE.write(dev, {data = "x", writeTerm = "\\n"}, mark)', "unknown field 'writeTerm'"),
    ('ATE.write(dev, {dat = "x"}, mark)', "unknown field 'dat'"),
    ('ATE.write(dev, {timeout = 1}, mark)', "missing required field 'data'"),
    ('ATE.read(dev, {size = 0}, mark)', "field 'size' must be a positive int"),
])
def test_a_per_call_field_udp_does_not_know_raises_naming_it(
    app: QCoreApplication, call: str, expected: str
) -> None:
    """readTerm/writeTerm are not fields for UDP, and neither is a typo or a missing data: the call
    raises naming the field without ever reaching onError, and nothing opens."""
    peer = UdpLoopbackPeer()
    peer.start()
    try:
        store, ru = _run_with_peer_limited(app, _step(f"""
        local calls = {{}}
        local mark = function() calls[#calls + 1] = "called"; return "handled" end
        local msg = select(2, {call})
        ATE.report(Result.PASS, #calls .. " " .. msg)"""), peer.port)
    finally:
        peer.stop()

    count, msg = _step_message(store, ru).split(" ", 1)
    assert count == "0", f"onError must never run for a schedule bug: {count} calls"
    assert expected in msg, f"the error must name the field: {msg!r}"


#: Lua's spellings of the numbers that are greater than 0 but not finite: NaN and infinity.
_NON_FINITE = (("nan", "0/0"), ("inf", "1/0"))


@pytest.mark.parametrize("name, lua", _NON_FINITE)
def test_a_non_finite_open_timeout_raises_naming_the_field(
    app: QCoreApplication, name: str, lua: str
) -> None:
    """A timeout must be a real number of seconds: NaN and infinity are both greater than 0 yet can
    never be met, so open raises naming 'timeout', onError never runs, and no socket is opened."""
    store, ru = _run_with_peer_limited(
        app,
        f"""return ScheduleParams("x") :setSchedule({{
    StepParams("io"):setStepNo("1.")
      :setAction(function()
        local calls = {{}}
        local mark = function() calls[#calls + 1] = "called"; return "handled" end
        local msg = select(2, ATE.open(DeviceType.UDP,
          {{host="127.0.0.1", port=12345, localPort=0, timeout={lua}}}, mark))
        ATE.report(Result.PASS, #calls .. " " .. tostring(msg))
      end),
  }})""",
        12345,
    )
    count, *msg = _step_message(store, ru).split(" ", 1)
    assert count == "0", f"onError must never run for a schedule bug: {count} calls"
    assert "field 'timeout' must be greater than 0" in msg[0], f"the error must name the field: {msg[0]!r}"
    assert name in msg[0].lower(), f"the error must show the value it got: {msg[0]!r}"


@pytest.mark.parametrize("name, lua", _NON_FINITE)
def test_a_non_finite_per_call_timeout_raises_naming_the_field(
    app: QCoreApplication, name: str, lua: str
) -> None:
    """The same rule for a per-call read timeout: the call raises naming 'timeout' and onError never
    runs, so the schedule cannot make a read wait forever on a number that is not a number."""
    peer = UdpLoopbackPeer()
    peer.start()
    try:
        store, ru = _run_with_peer_limited(app, _step(f"""
        local calls = {{}}
        local mark = function() calls[#calls + 1] = "called"; return "handled" end
        local msg = select(2, ATE.read(dev, {{timeout = {lua}}}, mark))
        ATE.report(Result.PASS, #calls .. " " .. tostring(msg))"""), peer.port)
    finally:
        peer.stop()

    count, msg = _step_message(store, ru).split(" ", 1)
    assert count == "0", f"onError must never run for a schedule bug: {count} calls"
    assert "field 'timeout' must be greater than 0" in msg, f"the error must name the field: {msg!r}"
    assert name in msg.lower(), f"the error must show the value it got: {msg!r}"


def test_python_timeouts_must_be_finite_and_positive_at_open_and_per_call() -> None:
    """The same check at the driver's own Python door: NaN, infinity, zero and a negative are all bad
    open values and bad per-call overrides, each raising a ValueError naming 'timeout'."""
    peer = UdpLoopbackPeer()
    peer.start()
    try:
        for bad in (float("nan"), float("inf"), 0, -1):
            with pytest.raises(ValueError, match="field 'timeout'"):
                UdpDriver().open(
                    {"host": "127.0.0.1", "port": peer.port, "localPort": 0, "timeout": bad}
                )
        dev = _open(peer)
        try:
            for bad in (float("nan"), float("inf"), 0, -1):
                with pytest.raises(ValueError, match="field 'timeout'"):
                    dev.read({"timeout": bad})
                with pytest.raises(ValueError, match="field 'timeout'"):
                    dev.write({"data": "x", "timeout": bad})
            assert dev._failed is None, "a bad timeout is a schedule bug, not a device failure"
        finally:
            dev.close()
    finally:
        peer.stop()


# --- the queue: whole datagrams, in order, one per read -----------------------------------------


def test_datagrams_that_arrive_before_the_read_are_queued_in_order() -> None:
    """Three datagrams sent before any read come back as three separate reads, oldest first: the
    queue holds messages whole, so nothing is merged and nothing is split."""
    peer = UdpLoopbackPeer()
    peer.start()
    try:
        dev = _open(peer)
        _talk(dev, peer)  # the peer learns the device's address from this datagram
        peer.send(b"one")
        peer.send(b"two")
        peer.send(b"three")
        _wait_for(dev, lambda d: len(d._datagrams) == 3)
        assert [dev.read({"timeout": 1}) for _ in range(3)] == [
            (b"one", 3, False, None), (b"two", 3, False, None), (b"three", 5, False, None)
        ]
        assert dev._datagrams == deque() and dev._queued == 0, "each read took exactly one datagram"
        dev.close()
    finally:
        peer.stop()


def test_size_truncates_a_datagram_and_discards_the_rest_of_it() -> None:
    """A datagram longer than size comes back cut at size with partial = true, and the tail is
    thrown away: the next read gets the next datagram, not the leftover bytes."""
    peer = UdpLoopbackPeer()
    peer.start()
    try:
        dev = _open(peer)
        _talk(dev, peer)
        peer.send(b"0123456789")
        peer.send(b"next")
        _wait_for(dev, lambda d: len(d._datagrams) == 2)
        assert dev.read({"size": 4, "timeout": 1}) == (b"0123", 4, True, None)
        assert dev.read({"timeout": 1}) == (b"next", 4, False, None)
        dev.close()
    finally:
        peer.stop()


def test_a_read_with_nothing_queued_times_out_and_is_never_partial() -> None:
    """UDP has no partial timeout: a datagram arrives whole or not at all, so an empty queue gives
    the same no-data timeout result TCP gives - data nil, count 0, partial false, err timeout."""
    peer = UdpLoopbackPeer()
    peer.start()
    try:
        dev = _open(peer)
        _talk(dev, peer)  # the peer heard us, but it sends nothing back
        assert dev.read({"timeout": 0.2}) == (None, 0, False, "timeout")
        dev.close()
    finally:
        peer.stop()


def test_an_empty_datagram_is_a_message_not_nothing() -> None:
    """A zero-length datagram is a valid message: it is queued and read back as empty data with
    count 0, rather than being skipped like a stream's empty read."""
    peer = UdpLoopbackPeer()
    peer.start()
    try:
        dev = _open(peer)
        _talk(dev, peer)
        peer.send(b"")
        _wait_for(dev, lambda d: len(d._datagrams) == 1)
        assert dev.read({"timeout": 1}) == (b"", 0, False, None)
        dev.close()
    finally:
        peer.stop()


def test_flush_discards_every_queued_datagram_and_counts_the_bytes() -> None:
    """ATE.flush empties the queue in one go and reports the total bytes it threw away."""
    peer = UdpLoopbackPeer()
    peer.start()
    try:
        dev = _open(peer)
        _talk(dev, peer)
        peer.send(b"aaa")
        peer.send(b"bbbb")
        peer.send(b"ccccc")
        _wait_for(dev, lambda d: len(d._datagrams) == 3)
        assert dev.flush() == (True, 12, None)
        _wait_for(dev, lambda d: d._queued == 0 and not d._datagrams)
        assert dev.read({"timeout": 0.2}) == (None, 0, False, "timeout")
        dev.close()
    finally:
        peer.stop()


# --- the cap, the refused peer, and close ---------------------------------------------------------


def test_seam_overflow_drops_whole_oldest_datagrams_and_logs_one_warning() -> None:
    """With a 10-byte cap and a burst of 4+4+4+6, the queue keeps the newest datagrams that fit
    (dropping the oldest whole ones), the first read says overflow = true, and exactly one device
    WARNING is logged for the episode.

    No guessed sleep: the burst is opened in step 1 and read in step 2, and the test waits on the
    device's own queue in between - `_queued == 10` means the reader has the whole burst in - so the
    first read provably sees the finished burst rather than whatever had arrived by then."""
    peer = UdpLoopbackPeer(datagrams=[b"aaaa", b"bbbb", b"cccc", b"dddddd"])
    peer.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schedule.tslua"
            path.write_text(
                """return ScheduleParams("x") :setSchedule({
    StepParams("burst"):setStepNo("1.")
      :setAction(function()
        dev = ATE.open(DeviceType.UDP, {host="127.0.0.1", port=__PORT__, localPort=0, timeout=5})
        ATE.write(dev, {data = "go"})
      end),
    StepParams("reads"):setStepNo("2.")
      :setAction(function()
        local r1 = ATE.read(dev, {timeout = 1})
        local r2 = ATE.read(dev, {timeout = 1})
        local r3 = ATE.read(dev, {timeout = 0.3}, function() return "empty" end)
        ATE.close(dev)
        ATE.report(Result.PASS, r1.data .. " " .. tostring(r1.overflow) .. " "
            .. r2.data .. " " .. r3)
      end),
  })""".replace(UdpLoopbackPeer.PORT_PLACEHOLDER, str(peer.port)),
                encoding="utf-8",
                newline="\n",
            )
            host = LuaHost(str(path), receive_buffer_bytes=10)
            steps = host.schedule.steps
            host._current_step = 0
            host._run_action(steps[0].action, "step")
            dev = host._devices.snapshot()[0]
            _wait_for(dev, lambda d: d._queued == 10)  # the reader has the whole burst queued
            host._current_step = 1
            host._run_action(steps[1].action, "step")

        assert host.reports[-1][1] == "cccc true dddddd empty", f"the two oldest datagrams were dropped: {host.reports}"
        warnings = [ln for ln in host.logs if ln[0] == "WARNING" and ln[1] == "device"]
        assert len(warnings) == 1, f"one WARNING per overflow episode, got {len(warnings)}"
        assert "overflow" in warnings[0][2].lower() and "dropped 8 " in warnings[0][2], warnings[0][2]
    finally:
        peer.stop()


def test_a_datagram_bigger_than_the_cap_is_dropped_whole() -> None:
    """A single datagram that cannot fit in the cap at all never enters the queue; it counts as an
    overflow the next read reports, and the queue stays empty."""
    peer = UdpLoopbackPeer()
    peer.start()
    try:
        dev = _open(peer, limit=10)
        _talk(dev, peer)
        peer.send(b"x" * 20)
        _wait_for(dev, lambda d: d._overflow_dropped == 20)
        assert dev._datagrams == deque(), "an oversized datagram is dropped, not squeezed in"
        assert dev.read({"timeout": 0.2}) == (None, 0, False, "timeout")
        assert dev.take_overflow() == (True, 20)
        dev.close()
    finally:
        peer.stop()


def test_an_oversize_datagram_fails_only_that_write_and_leaves_the_device_usable() -> None:
    """A payload no UDP datagram can hold (65507 bytes is the most one carries) is a bad call, not a
    dead device: the write fails with an err naming the size and the limit, nothing is warned, the
    device is not failed, and the next small write still goes out."""
    peer = UdpLoopbackPeer()
    peer.start()
    try:
        dev = _open(peer)
        ok, count, err = dev.write({"data": b"x" * 65508})
        assert not ok and count == 0, f"a 65508-byte datagram cannot be sent: {ok}, {count}"
        assert "65507" in err and "65508" in err, f"the err must name the limit and the size: {err!r}"
        assert dev._failed is None, "an oversize write is that call's failure, not the device's"
        assert dev.take_warnings() == [], "an oversize write logs no device warning"
        assert dev.write({"data": "still here"}) == (True, 10, None), "the device still works"
        assert peer.wait_for_datagram(1), "the small write reached the peer"
        assert peer.received == [b"still here"]
        assert dev.write({"data": b"y" * 65507}) == (True, 65507, None), "65507 bytes still fits"
        dev.close()
    finally:
        peer.stop()


def test_an_emsgsize_from_send_fails_only_that_call() -> None:
    """If the socket raises EMSGSIZE anyway (a smaller limit than we checked, or a platform quirk),
    that write fails alone: the device stays usable and logs nothing."""
    peer = UdpLoopbackPeer()
    peer.start()
    try:
        dev = _open(peer)

        class _SendIsTooLong:
            """A socket stand-in whose send always raises EMSGSIZE; recv, fileno and close are the
            real socket's, so only the send is hurt (a socket object will not let send be replaced)."""

            def __init__(self, real: socket.socket) -> None:
                self._real = real

            def fileno(self) -> int:
                return self._real.fileno()  # select on the write side still sees the live fd

            def send(self, _data: bytes) -> int:
                raise OSError(errno.EMSGSIZE, "Message too long")

            def recv(self, nbytes: int) -> bytes:
                return self._real.recv(nbytes)

            def close(self) -> None:
                self._real.close()

        dev.sock = _SendIsTooLong(dev.sock)  # the reader still works: only sends are hurt
        ok, count, err = dev.write({"data": "x"})
        assert (ok, count) == (False, 0), f"the send failed: {ok}, {count}"
        assert "too long" in err.lower(), f"the err says why: {err!r}"
        assert dev._failed is None, "EMSGSIZE is a send error, not a lost connection"
        assert dev.take_warnings() == []
        assert dev.read({"timeout": 0.2}) == (None, 0, False, "timeout"), "the device still reads"
        dev.close()
    finally:
        peer.stop()


def test_a_refused_write_fails_only_that_call_and_leaves_the_device_usable() -> None:
    """On Linux a connected UDP socket whose peer is not listening answers an ICMP port-unreachable,
    so the send is refused. The peer may start listening later, so that is not a device failure: the
    reader keeps polling, only that write fails, and the peer's datagrams still arrive."""
    peer = UdpLoopbackPeer()
    peer.start()
    try:
        dev = _open(peer)
        _talk(dev, peer)  # the peer has to see this first datagram before it can reply
        peer.unbind()  # nothing at that port any more: Linux starts refusing our sends
        err = None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and err is None:
            ok, _, err = dev.write({"data": "go", "timeout": 1})
        assert err is not None and err != "timeout", f"a send to a peer that is gone must be refused: {err}"
        assert dev._failed is None, "a refused peer is not a broken device"
        peer.rebind()
        peer.send(b"back")
        _wait_for(dev, lambda d: len(d._datagrams) == 1)
        assert dev.read({"timeout": 1}) == (b"back", 4, False, None)
        dev.close()
    finally:
        peer.stop()


def test_a_stopped_peer_leaves_no_drain_thread_and_frees_its_port() -> None:
    """The fixture's own lifetime is part of the seam: stop() closes the socket, but a recvfrom
    already blocked on it is not woken by that close, so without a poll timeout the drain thread
    would outlive the peer and keep its port bound - and the next test would bind a different one."""
    peer = UdpLoopbackPeer()
    peer.start()
    peer.stop()
    assert not peer._thread.is_alive(), "the drain thread is still polling after stop()"
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("127.0.0.1", peer.port))
    except OSError as exc:
        pytest.fail(f"a stopped peer still holds port {peer.port}: {exc}")
    finally:
        probe.close()


def test_an_unbound_peer_stops_draining_and_is_not_deaf_after_rebind() -> None:
    """unbind() takes the port out of service AND ends the drain: if the thread stayed blocked on the
    closed socket, rebind() would see a live thread and never start a new one, leaving the peer deaf
    while the device still writes to it."""
    peer = UdpLoopbackPeer()
    peer.start()
    try:
        dev = _open(peer)
        _talk(dev, peer)
        peer.unbind()
        assert not peer._thread.is_alive(), "the drain kept polling the closed socket"
        peer.rebind()
        peer.send(b"back")
        _wait_for(dev, lambda d: len(d._datagrams) == 1)
        assert dev.read({"timeout": 1}) == (b"back", 4, False, None), "a rebound peer still hears"
        dev.close()
    finally:
        peer.stop()


def test_seam_a_peer_that_is_not_listening_never_fails_the_device(app: QCoreApplication) -> None:
    """UDP has no connection to lose, so a peer nobody is listening on is not a broken device: the
    open succeeds, every write either goes out or is refused as that call's own err through onError,
    and the read after them is a plain timeout rather than a failure. Nothing logs a device warning.

    Whether any send is refused here depends on when the reader drains the queued ICMP errors, so
    this test only checks the outcome (no failure, a plain timeout, a clean close); the refusal path
    itself is pinned by the refused-write seam test below."""
    source = """return ScheduleParams("x") :setSchedule({
    StepParams("io"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, {host="127.0.0.1", port=__PORT__, localPort=0, timeout=1})
        local ok, refused = 0, 0
        for i = 1, 10 do
          local w = ATE.write(dev, {data = "ping", timeout = 1},
            function(res) refused = refused + 1; return res.err end)
          if type(w) == "table" then ok = ok + 1 end
        end
        local r = ATE.read(dev, {timeout = 0.2}, function(res) return res.err end)
        local c = ATE.close(dev)
        ATE.report(Result.PASS, ok .. " " .. refused .. " " .. tostring(r) .. " " .. tostring(c.ok))
      end),
  })"""
    store, ru = _run_with_peer_limited(app, source, _dead_port())

    _ok, _refused, read, closed = _step_message(store, ru).split(" ")
    assert read == "timeout", f"the device is not failed, so the read just times out: {read!r}"
    assert closed == "true", f"the sends that were refused left the device open: {closed!r}"
    warnings = [ln for ln in store.log_lines(ru)
                if ln["level"] == "WARNING" and ln["source"] == "device"]
    assert not warnings, f"a peer that is not listening is not a device failure: {warnings}"


class _RefusesSends:
    """A socket stand-in that refuses its first ``n`` sends with ECONNREFUSED (what Linux gives a
    connected UDP socket after an ICMP port-unreachable) and passes everything else to the real
    socket. The real refusal depends on when the reader drains the ICMP error, so a test that needs
    one uses this instead."""

    def __init__(self, real: socket.socket, n: int) -> None:
        self._real, self._left = real, n

    def fileno(self) -> int:
        return self._real.fileno()

    def send(self, data: bytes) -> int:
        if self._left > 0:
            self._left -= 1
            raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
        return self._real.send(data)

    def recv(self, nbytes: int) -> bytes:
        return self._real.recv(nbytes)

    def close(self) -> None:
        self._real.close()


def test_seam_a_write_refused_by_a_peer_that_has_gone_costs_only_that_write() -> None:
    """The refusal path through Lua: the first three sends of step 2 are refused (a stand-in socket,
    so the count is exact), each comes back as that call's own err through onError, the other seven
    still go out, no call fails the device (the read after them is a plain timeout), and no device
    WARNING is logged."""
    peer = UdpLoopbackPeer()
    peer.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schedule.tslua"
            path.write_text(
                """return ScheduleParams("x") :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        dev = ATE.open(DeviceType.UDP, {host="127.0.0.1", port=__PORT__, localPort=0, timeout=1})
        ATE.write(dev, {data = "go"})
        ATE.report(Result.PASS, "listening")
      end),
    StepParams("refused"):setStepNo("2.")
      :setAction(function()
        local ok, refused = 0, 0
        for i = 1, 10 do
          local w = ATE.write(dev, {data = "ping", timeout = 1},
            function(res) refused = refused + 1; return res.err end)
          if type(w) == "table" then ok = ok + 1 end
        end
        local r = ATE.read(dev, {timeout = 0.2}, function(res) return res.err end)
        ATE.close(dev)
        ATE.report(Result.PASS, ok .. " " .. refused .. " " .. tostring(r))
      end),
  })""".replace(UdpLoopbackPeer.PORT_PLACEHOLDER, str(peer.port)),
                encoding="utf-8",
                newline="\n",
            )
            host = LuaHost(str(path))
            steps = host.schedule.steps
            host._current_step = 0
            host._run_action(steps[0].action, "step")
            dev = host._devices.snapshot()[0]
            dev.sock = _RefusesSends(dev.sock, 3)  # the peer has gone: the next three sends are refused
            host._current_step = 1
            host._run_action(steps[1].action, "step")

        assert host.reports[-1][1] == "7 3 timeout", f"each refusal cost only its own call: {host.reports}"
        warnings = [ln for ln in host.logs if ln[0] == "WARNING" and ln[1] == "device"]
        assert not warnings, f"a refused send is not a device failure: {warnings}"
    finally:
        peer.stop()


def test_close_then_open_gives_a_new_device_and_a_clean_close_leaves_no_failure() -> None:
    """Closing a UDP device stops its reader and closes the socket without recording a failure;
    opening again with the same params gives a different handle that works."""
    peer = UdpLoopbackPeer()
    peer.start()
    try:
        first = _open(peer)
        first.write({"data": "go"})
        first.close()
        assert first.take_warnings() == [], "a clean close is not a failure"
        assert first.quiescent(), "the reader thread is gone after close"
        second = _open(peer)
        second.write({"data": "again"})
        assert peer.wait_for_datagram(2), "the reopened device still reaches the peer"
        second.close()
    finally:
        peer.stop()


def test_an_unexpected_reader_error_still_fails_the_device() -> None:
    """An exception the transport didn't translate must not kill the reader silently: the device is
    marked failed with one warning, so reads fail instead of timing out forever."""
    peer = UdpLoopbackPeer()
    peer.start()
    try:
        dev = _open(peer)

        def boom():
            raise RuntimeError("boom")

        dev._recv = boom  # the reader looks _recv up on each poll
        _wait_for(dev, lambda d: d._failed is not None)
        assert dev._failed == "RuntimeError: boom"
        assert dev.read({"timeout": 1})[3] == "RuntimeError: boom"
        assert len(dev.take_warnings()) == 1
        dev.close()
    finally:
        peer.stop()



