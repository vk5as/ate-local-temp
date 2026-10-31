"""Packet 16d — the `ate.devices` builders: params tables with chained setters.

Seam 1: a real lupa runtime loads the bundled ``ate/lua/devices.lua``, and every Device reached through
a builder is a real socket or a real pyserial port on a loopback peer. ``Tcp``/``Serial``/``Udp``/``Read``/
``Write`` return the params table itself — the setters live in a shared metatable's ``__index``, so
``pairs`` sees only the fields, the table goes straight into ``ATE.open``/``ATE.read``/``ATE.write``, and
the unknown-field check passes. Open builders carry every field their Device Type requires with the spec's
defaults (9600 baud, 8 data bits, 1 stop bit, ``Parity.NONE``, ``FlowControl.NONE``, ``timeout = 1.0``,
``readTerm = writeTerm = "\\n"``, ``localPort = 0``); ``Read()`` starts empty and ``Write(data)`` holds only
``data``, because a per-call field left out falls back to the Device's open configuration. Setters do not
validate: the same open/read/write check raises and names the field."""

import tempfile
from datetime import datetime
from pathlib import Path

import serial
from PyQt6.QtCore import QCoreApplication

from ate.devices.serial import SerialDriver
from ate.engine.lua_host import LuaHost

from ._fixtures import LoopbackPeer, UdpLoopbackPeer, wait_for_disconnect
from .test_tcp_read_write import _run_with_peer, _step_message


def _builder_fields(statements: str) -> dict:
    """The fields of the builder the statements leave in ``t``, read out of Lua with ``pairs``.

    A real LuaHost loads the bundled module, so what comes back is exactly what the host hands the
    driver through :func:`_raw_params`: the table's own keys, never one of its setters."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "main.tslua"
        path.write_text('return ScheduleParams("x"):setSchedule({})', encoding="utf-8", newline="\n")
        host = LuaHost(str(path))
        collected = host._runtime.execute(
            "local devices = require('ate.devices')\n"
            + statements + "\n"
            "local keys = {} for k in pairs(t) do keys[#keys + 1] = k end\n"
            "table.sort(keys)\n"
            "local out = {} for _, k in ipairs(keys) do out[k] = t[k] end\n"
            "return out"
        )
    return {key: collected[key] for key in collected.keys()}


def _schedule(body: str) -> str:
    """A one-step schedule whose Action requires ``ate.devices`` as ``devices`` and runs ``body``."""
    return f"""return ScheduleParams("x") :setSchedule({{
    StepParams("io"):setStepNo("1.")
      :setAction(function()
        local devices = require("ate.devices")
{body}
      end),
  }})"""


# --- Seam 1: a schedule talks to a peer through the builders -------------------------------------


def test_tcp_builders_open_write_and_read_through_a_step(app: QCoreApplication) -> None:
    """``Tcp`` opens, ``Write`` sends with the builder's own writeTerm, ``Read`` reads with the
    open-time readTerm, and the peer sees the exact bytes."""
    peer = LoopbackPeer(payload=b"ping\n")
    peer.start()
    try:
        store, ru = _run_with_peer(app, _schedule("""
        local dev = ATE.open(DeviceType.TCP, devices.Tcp("127.0.0.1", __PORT__):setTimeout(5))
        local w = ATE.write(dev, devices.Write("Hi"))
        local r = ATE.read(dev, devices.Read():setTimeout(2))
        ATE.close(dev)
        ATE.report(Result.PASS, w.count .. " " .. tostring(r.data) .. " " .. r.count .. " "
            .. tostring(r.err))"""), peer.port)
    finally:
        peer.stop()

    assert wait_for_disconnect(peer)
    assert peer.received[peer.connections[0]] == b"Hi\n"
    assert _step_message(store, ru) == "3 ping 4 nil"


def test_serial_builders_open_write_and_read_through_a_step(app: QCoreApplication) -> None:
    """The same through a pyserial port reached over ``socket://``, with ``setBaud`` and
    ``setParity(Parity.EVEN)`` on the builder. The peer answers only after the device's write: pyserial
    clears the port's input inside ``open``, so a reply sent on accept could be lost (packet 31b)."""
    peer = LoopbackPeer(payload=b"ping\n", after_first_byte=True)
    peer.start()
    try:
        store, ru = _run_with_peer(app, _schedule("""
        local dev = ATE.open(DeviceType.SERIAL,
          devices.Serial("socket://127.0.0.1:__PORT__"):setBaud(115200):setParity(Parity.EVEN):setTimeout(5))
        local w = ATE.write(dev, devices.Write("Hi"))
        local r = ATE.read(dev, devices.Read():setTimeout(2))
        ATE.close(dev)
        ATE.report(Result.PASS, w.count .. " " .. tostring(r.data) .. " " .. r.count)"""), peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "3 ping 4"
    assert peer.received[peer.connections[0]] == b"Hi\n"


def test_serial_builder_setters_reach_the_pyserial_port() -> None:
    """The params the builder carries are the ones pyserial is opened with: baud 115200, EVEN parity,
    7 data bits, 2 stop bits, RTS/CTS — every setter lands on the live port object."""
    peer = LoopbackPeer()
    peer.start()
    try:
        params = _builder_fields(
            "local t = devices.Serial(\"socket://127.0.0.1:" + str(peer.port) + "\")"
            ":setBaud(115200):setParity(Parity.EVEN):setDataBits(7):setStopBits(2):setFlowControl(FlowControl.RTSCTS)"
        )
        dev = SerialDriver().open(params)
        try:
            assert peer.wait_for_connection()
            assert dev.ser.baudrate == 115200
            assert dev.ser.parity == serial.PARITY_EVEN
            assert dev.ser.bytesize == 7
            assert dev.ser.stopbits == 2
            assert dev.ser.xonxoff is False and dev.ser.rtscts is True
        finally:
            dev.close()
    finally:
        peer.stop()


def test_udp_builders_open_write_and_read_through_a_step(app: QCoreApplication) -> None:
    """UDP takes no terminators: ``Udp``'s fields are host/port/localPort/timeout, and the peer's reply
    comes back as one whole datagram."""
    peer = UdpLoopbackPeer(datagrams=[b"pong"])
    peer.start()
    try:
        store, ru = _run_with_peer(app, _schedule("""
        local dev = ATE.open(DeviceType.UDP, devices.Udp("127.0.0.1", __PORT__):setTimeout(5))
        local w = ATE.write(dev, devices.Write("ping"))
        local r = ATE.read(dev, devices.Read():setTimeout(2))
        ATE.close(dev)
        ATE.report(Result.PASS, w.count .. " " .. tostring(r.data) .. " " .. r.count .. " "
            .. tostring(r.partial) .. " " .. tostring(r.err))"""), peer.port)
    finally:
        peer.stop()

    assert peer.received[0] == b"ping"
    assert _step_message(store, ru) == "4 pong 4 false nil"


# --- The builders' fields and defaults, read back from Lua with pairs ---------------------------


def test_open_builders_carry_every_open_field_with_the_spec_defaults() -> None:
    """``pairs`` on an open builder gives exactly its Device Type's fields with the documented
    defaults: no setter shows up as a field, and nothing the open requires is missing."""
    assert _builder_fields('local t = devices.Tcp("127.0.0.1", 5025)') == {
        "host": "127.0.0.1", "port": 5025, "timeout": 1.0, "readTerm": "\n", "writeTerm": "\n",
    }
    assert _builder_fields('local t = devices.Serial("COM3")') == {
        "port": "COM3", "baud": 9600, "dataBits": 8, "stopBits": 1, "parity": "NONE",
        "flowControl": "NONE", "timeout": 1.0, "readTerm": "\n", "writeTerm": "\n",
    }
    assert _builder_fields('local t = devices.Udp("127.0.0.1", 5025)') == {
        "host": "127.0.0.1", "port": 5025, "localPort": 0, "timeout": 1.0,
    }
    assert _builder_fields('local t = devices.Udp("127.0.0.1", 5025):setLocalPort(1234)')["localPort"] == 1234
    for ctor in ('devices.Tcp("127.0.0.1", 5025)', 'devices.Serial("COM3")', 'devices.Udp("127.0.0.1", 5025)'):
        timeout = _builder_fields(f"local t = {ctor}")["timeout"]
        assert isinstance(timeout, float), f"{ctor}: timeout = 1.0 is a float, got {timeout!r}"


def test_builder_metatables_are_locked() -> None:
    """``getmetatable`` on a builder gives ``false``, so a schedule can't reach the shared setter
    table and change (or remove) a setter for every later builder of that kind."""
    fields = _builder_fields(
        "local t = {}\n"
        "for _, b in ipairs({devices.Tcp('h', 1), devices.Serial('COM3'), devices.Udp('h', 1),\n"
        "                    devices.Read(), devices.Write('x')}) do\n"
        "  t[#t + 1] = tostring(getmetatable(b))\n"
        "end"
    )
    assert fields == {1: "false", 2: "false", 3: "false", 4: "false", 5: "false"}


def test_read_and_write_builders_carry_only_their_own_fields() -> None:
    """``Read()`` starts empty and ``Write(data)`` holds only ``data``: defaulting a per-call
    ``timeout`` here would silently override the Device's own open timeout."""
    assert _builder_fields("local t = devices.Read()") == {}
    assert _builder_fields('local t = devices.Write("*IDN?")') == {"data": "*IDN?"}
    assert _builder_fields('local t = devices.Read():setSize(10):setTimeout(2):setReadTerm("")') == {
        "size": 10, "timeout": 2, "readTerm": "",
    }
    assert _builder_fields('local t = devices.Write("x"):setWriteTerm(""):setTimeout(3)') == {
        "data": "x", "timeout": 3, "writeTerm": "",
    }


def test_setters_chain_on_the_same_table() -> None:
    """Every setter hands back the same builder table, so the chain reads as one expression."""
    fields = _builder_fields(
        "local t = devices.Serial(\"COM3\"):setBaud(115200)\n"
        "if t:setStopBits(2) ~= t then error(\"a setter returned a different table\") end\n"
        "local t2 = t:setDataBits(7):setParity(Parity.EVEN)\n"
        "t = t2"
    )
    assert fields == {
        "port": "COM3", "baud": 115200, "dataBits": 7, "stopBits": 2, "parity": "EVEN",
        "flowControl": "NONE", "timeout": 1.0, "readTerm": "\n", "writeTerm": "\n",
    }


# --- Errors, defaults and the load-time require ----------------------------------------------------


def test_a_setter_the_device_type_lacks_is_a_lua_error(app: QCoreApplication) -> None:
    """``Tcp`` has no ``setBaud`` and ``Udp`` no ``setReadTerm``: calling one is an ordinary Lua
    "attempt to call a nil value", which the run engine turns into an ERROR Attempt."""
    peer = LoopbackPeer()
    peer.start()
    try:
        store, ru = _run_with_peer(app, _schedule("""
        ATE.log(Log.INFO, "tcp timeout " .. devices.Tcp("127.0.0.1", 1):setTimeout(2.0).timeout)
        ATE.log(Log.INFO, "tcp setBaud " .. tostring(devices.Tcp("127.0.0.1", 1).setBaud))
        local dead = devices.Udp("127.0.0.1", 1):setReadTerm("\\n")
        ATE.report(Result.PASS, "never reached")"""), peer.port)
    finally:
        peer.stop()

    node = next(n for n in store.nodes(ru) if n["kind"] == "step")
    assert node["result"] == "ERROR"
    (attempt,) = store.attempts(ru)
    assert "attempt to call a nil value" in attempt["msg"]
    assert any(ln["text"] == "tcp timeout 2.0" for ln in store.log_lines(ru))
    assert any(ln["text"] == "tcp setBaud nil" for ln in store.log_lines(ru))


def test_a_bad_setter_value_raises_naming_the_field_and_never_onError(app: QCoreApplication) -> None:
    """Setters do not validate: ``setDataBits(4)`` is caught by the SERIAL open check, which (as a
    schedule bug isolated at source ``ate``) returns false naming the field and never reaches ``onError``."""
    peer = LoopbackPeer()
    peer.start()
    try:
        store, ru = _run_with_peer(app, _schedule("""
        local handler = function(result) ATE.report(Result.FAIL, "onError ran: " .. tostring(result.err)) end
        local ok, msg = ATE.open(DeviceType.SERIAL,
          devices.Serial("socket://127.0.0.1:__PORT__"):setDataBits(4), handler)
        ATE.report(Result.PASS, tostring(ok) .. " " .. msg)"""), peer.port)
    finally:
        peer.stop()

    msg = _step_message(store, ru)
    assert msg.startswith("false ") and "field 'dataBits' must be one of 5, 6, 7, 8, got 4" in msg
    assert not any("onError ran" in ln["text"] for ln in store.log_lines(ru))
    assert any(ln["source"] == "ate" and ln["level"] == "ERROR" and "dataBits" in ln["text"]
              for ln in store.log_lines(ru))


def test_read_with_no_setters_uses_the_device_open_timeout(app: QCoreApplication) -> None:
    """An empty ``Read()`` carries no timeout, so the read waits out the Device's own 1.5 s and not a
    builder default of 1.0: the two log lines around it are 1.5 s apart, and the silent peer is never
    heard from."""
    peer = LoopbackPeer()  # accepts, sends nothing
    peer.start()
    try:
        store, ru = _run_with_peer(app, _schedule("""
        local dev = ATE.open(DeviceType.TCP, devices.Tcp("127.0.0.1", __PORT__):setTimeout(1.5))
        ATE.log(Log.INFO, "read start")
        local r = ATE.read(dev, devices.Read(), function(res) return res end)  -- onError hands back the failure table
        ATE.log(Log.INFO, "read end")
        ATE.close(dev)
        ATE.report(Result.PASS, tostring(r.err) .. " " .. r.count .. " " .. tostring(r.partial))"""), peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "timeout 0 false"
    lines = store.log_lines(ru)
    start = next(ln for ln in lines if ln["text"] == "read start")
    end = next(ln for ln in lines if ln["text"] == "read end")
    waited = (datetime.fromisoformat(end["at"]) - datetime.fromisoformat(start["at"])).total_seconds()
    assert 1.3 <= waited < 3.0, f"the read waited {waited}s, not the Device's 1.5s"


def test_requiring_ate_devices_at_load_time_and_in_a_step_both_work(app: QCoreApplication) -> None:
    """The module resolves while the schedule loads and again inside an Action, and both loads give
    working builders; requiring it twice in one run returns the same cached table."""
    peer = LoopbackPeer(payload=b"ok\n")
    peer.start()
    try:
        source = "local top = require(\"ate.devices\")\n" + _schedule("""
        local dev = ATE.open(DeviceType.TCP, top.Tcp("127.0.0.1", __PORT__):setTimeout(5))
        local again = require("ate.devices")
        local r = ATE.read(dev, again.Read():setTimeout(2))
        ATE.close(dev)
        ATE.report(Result.PASS, tostring(top.Tcp("h", 1).timeout) .. " " .. tostring(again == top) .. " "
            .. tostring(r.data))""")
        store, ru = _run_with_peer(app, source, peer.port)
    finally:
        peer.stop()

    assert _step_message(store, ru) == "1.0 true ok"
