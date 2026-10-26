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

import time
from collections import deque

import pytest
from PyQt6.QtCore import QCoreApplication

from ate.devices.udp import UdpDriver

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
                if ln["level"] == "WARNING" and "after 4 byte" in ln["text"]]
    assert len(partials) == 1, f"a partial read is logged once, got {partials}"


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


def test_seam_overflow_drops_whole_oldest_datagrams_and_logs_one_warning(app: QCoreApplication) -> None:
    """With a 10-byte cap and a burst of 4+4+4+6, the queue keeps the newest datagrams that fit
    (dropping the oldest whole ones), the first read says overflow = true, and the run logs exactly
    one device WARNING for the episode. The sleep is only to let the whole burst land before the
    first read: reading straight away would race the reader thread against the queue."""
    peer = UdpLoopbackPeer(datagrams=[b"aaaa", b"bbbb", b"cccc", b"dddddd"])
    peer.start()
    try:
        store, ru = _run_with_peer_limited(app, _step("""
        ATE.write(dev, {data = "go"})
        ATE.sleep(0.4)  -- four polls of the reader: the burst is queued before anything reads it
        local r1 = ATE.read(dev, {timeout = 1})
        local r2 = ATE.read(dev, {timeout = 1})
        local r3 = ATE.read(dev, {timeout = 0.3}, function() return "empty" end)
        ATE.report(Result.PASS, r1.count .. " " .. tostring(r1.overflow) .. " "
            .. r2.count .. " " .. r3)"""), peer.port, limit=10)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "4 true 6 empty", "the two oldest datagrams were dropped"
    warnings = [ln for ln in store.log_lines(ru)
                if ln["level"] == "WARNING" and ln["source"] == "device"]
    assert len(warnings) == 1, f"one WARNING per overflow episode, got {len(warnings)}"
    assert "overflow" in warnings[0]["text"].lower() and "8" in warnings[0]["text"]
    warnings = [ln for ln in store.log_lines(ru)
                if ln["level"] == "WARNING" and ln["source"] == "device"]
    assert len(warnings) == 1, f"one WARNING per overflow episode, got {len(warnings)}"
    assert "overflow" in warnings[0]["text"].lower() and "8" in warnings[0]["text"]


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


def test_a_refused_write_fails_only_that_call_and_leaves_the_device_usable() -> None:
    """On Linux a connected UDP socket whose peer is not listening answers an ICMP port-unreachable,
    so the send is refused. The peer may start listening later, so that is not a device failure: the
    reader keeps polling, only that write fails, and the peer's datagrams still arrive."""
    peer = UdpLoopbackPeer()
    peer.start()
    try:
        dev = _open(peer)
        assert dev.write({"data": "go"})[0], "the peer is listening, so the first send goes out"
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


def test_seam_a_peer_that_is_not_listening_never_fails_the_device(app: QCoreApplication) -> None:
    """UDP has no connection to lose, so a peer nobody is listening on is not a broken device: the
    open succeeds, every write either goes out or fails that call through onError, and the read after
    them is a plain timeout rather than a failure. Nothing logs a device warning."""
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
        ATE.report(Result.PASS, ok + refused .. " calls survived " .. tostring(r))
      end),
  })"""
    store, ru = _run_with_peer_limited(app, source, _dead_port())

    message = _step_message(store, ru)
    assert message.endswith(" calls survived timeout"), f"no call failed the device: {message!r}"
    warnings = [ln for ln in store.log_lines(ru)
                if ln["level"] == "WARNING" and ln["source"] == "device"]
    assert not warnings, f"a peer that is not listening is not a device failure: {warnings}"



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



