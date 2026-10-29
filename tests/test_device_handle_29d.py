"""Packet 29d review — a handle that is not a Device.

Seam-1 coverage of the spec rule "using a non-Device ... is a failure and takes the onError path":
when a failed ``ATE.open`` is recovered by an ``onError`` that returns a string, the schedule is left
holding a string where a Device handle belongs, and every later ``read``/``write``/``flush``/``close``
on it must fail as a Device failure does — through ``onError``, or as a raised
``<file>:<line>: ATE.<fn>: <value>: not a Device`` when there is no handler — never as a Python
``AttributeError`` dumped in the log as a traceback. Same for nil and for a number."""

import re

import pytest
from PyQt6.QtCore import QCoreApplication

from ._fixtures import LoopbackPeer
from .test_tcp_read_write import _run_with_peer, _step_message

# ``ATE.open`` on a peer with nothing to talk to, so a plain handle is not the issue here: the
# schedules below pass a literal string, number or nil where a Device belongs.
_BAD = {
    "string": '""',
    "number": "12",
    "nil": "nil",
}


def _run(app: QCoreApplication, body: str) -> tuple:
    """Run a one-step schedule whose action is ``body`` against a live but silent loopback peer."""
    peer = LoopbackPeer()
    peer.start()
    try:
        return _run_with_peer(app, f"""return ScheduleParams("x") :setSchedule({{
    StepParams("io"):setStepNo("1.")
      :setAction(function()
        local calls = {{}}
{body}
      end),
  }})""", peer.port)
    finally:
        peer.stop()


def _no_traceback(store, ru: str) -> None:
    """No line of this run's log is a Python traceback — the failure is an ATE failure, not a crash."""
    for line in store.log_lines(ru):
        assert "Traceback" not in line["text"], f"Python traceback in the log: {line['text']}"
        assert "AttributeError" not in line["text"], line["text"]


def _error_message(store, ru: str) -> str:
    """The single step's result, asserted ERROR, and the message its ERROR attempt recorded."""
    node = next(n for n in store.nodes(ru) if n["kind"] == "step")
    assert node["result"] == "ERROR"
    (attempt,) = store.attempts(ru)
    return attempt["msg"]


def test_read_on_a_string_handle_returns_the_handler_value(app: QCoreApplication) -> None:
    """A read on a string goes through the call's own onError, whose non-nil return is its result."""
    store, ru = _run(app, """
        local r = ATE.read("", {timeout = 0.1}, function(res, args)
          calls[#calls + 1] = tostring(res.ok) .. "|" .. res.err .. "|" .. tostring(res.count) .. "|" .. args.timeout
          return "handled"
        end)
        ATE.report(Result.PASS, r .. " " .. table.concat(calls, ","))""")
    _no_traceback(store, ru)
    assert _step_message(store, ru) == "handled nil|not a Device|0|0.1"


def test_read_on_a_nil_handle_raises_without_a_handler(app: QCoreApplication) -> None:
    """With no onError anywhere, a nil handle raises ``... ATE.read: nil: not a Device`` and the step
    records ERROR; nothing after the call runs."""
    store, ru = _run(app, """
        local dev = nil
        ATE.read(dev)
        ATE.report(Result.PASS, "never reached")""")
    _no_traceback(store, ru)
    msg = _error_message(store, ru)
    assert re.fullmatch(r"schedule\.tslua:\d+: ATE\.read: nil: not a Device", msg), msg
    assert not any(ln["text"] == "never reached" for ln in store.log_lines(ru))


def test_write_on_a_number_handle_falls_back_to_a_raising_failure(app: QCoreApplication) -> None:
    """A write on a number is a Device failure too: with no handler the step is ERROR, and the log
    holds the ATE message rather than a Python traceback."""
    store, ru = _run(app, """
        ATE.write(12, {data = "hi"})
        ATE.report(Result.PASS, "never reached")""")
    _no_traceback(store, ru)
    msg = _error_message(store, ru)
    assert re.fullmatch(r"schedule\.tslua:\d+: ATE\.write: a number: not a Device", msg), msg


def test_close_on_a_string_handle_goes_through_on_error(app: QCoreApplication) -> None:
    """Closing a string is not a no-op success: onError gets ``{ok = false, err}``, and its return is
    what the call returns."""
    store, ru = _run(app, """
        local c = ATE.close("closed?", function(res, args)
          calls[#calls + 1] = tostring(res.ok) .. "|" .. res.err
          return "recovered"
        end)
        ATE.report(Result.PASS, c .. " " .. table.concat(calls, ","))""")
    _no_traceback(store, ru)
    assert _step_message(store, ru) == "recovered false|not a Device"


def test_close_on_a_string_handle_raises_when_no_handler_is_given(app: QCoreApplication) -> None:
    """``ATE.close("")`` with no onError raises rather than reporting ``{ok = true}``."""
    store, ru = _run(app, """
        local c = ATE.close("")
        ATE.report(Result.PASS, "close said " .. tostring(c.ok))""")
    _no_traceback(store, ru)
    msg = _error_message(store, ru)
    assert re.fullmatch(r"schedule\.tslua:\d+: ATE\.close: a string: not a Device", msg), msg


def test_flush_on_a_table_handle_fails_cleanly(app: QCoreApplication) -> None:
    """A Lua table is not a Device either; its own onError still gets the failure."""
    store, ru = _run(app, """
        local f = ATE.flush({describe = "not really"}, function(res, args) return res.err end)
        ATE.report(Result.PASS, f .. " " .. table.concat(calls, ","))""")
    _no_traceback(store, ru)
    assert _step_message(store, ru) == "not a Device "


def test_open_recovered_by_a_string_on_error_is_still_not_a_device(app: QCoreApplication) -> None:
    """The shape the schedules actually get: a refused connect whose onError returns "" leaves the
    schedule holding a string, and the later read on it fails the same clean way."""
    store, ru = _run(app, """
        local dev = ATE.open(DeviceType.TCP, {host = "127.0.0.1", port = 1, timeout = 0.2, readTerm = "", writeTerm = ""},
          function(res, args) return "" end)
        local r = ATE.read(dev, {timeout = 0.1})
        ATE.report(Result.PASS, "never reached " .. tostring(r))""")
    _no_traceback(store, ru)
    msg = _error_message(store, ru)
    assert re.fullmatch(r"schedule\.tslua:\d+: ATE\.read: a string: not a Device", msg), msg


@pytest.mark.parametrize("handle", ('""', "nil", "12"))
def test_every_device_call_on_a_bad_handle_reaches_its_handler(app: QCoreApplication, handle: str) -> None:
    """read, write, flush and close all validate the handle before touching the registry, so
    `onError` gets the failure for a string, a number and nil alike, and its return is the result."""
    store, ru = _run(app, f"""
        local dev = {handle}
        local r = ATE.read(dev, {{timeout = 0.1}}, function(res, args) calls[#calls + 1] = res.err; return "r" end)
        local w = ATE.write(dev, {{data = "x"}}, function(res, args) calls[#calls + 1] = res.err; return "w" end)
        local f = ATE.flush(dev, function(res, args) calls[#calls + 1] = res.err; return "f" end)
        local c = ATE.close(dev, function(res, args) calls[#calls + 1] = res.err; return "c" end)
        ATE.report(Result.PASS, r .. w .. f .. c .. " " .. table.concat(calls, ","))""")
    _no_traceback(store, ru)
    assert _step_message(store, ru) == "rwfc " + ",".join(["not a Device"] * 4)


@pytest.mark.parametrize("handle", ('""', "nil", "12"))
def test_every_device_call_on_a_bad_handle_raises_without_a_handler(app: QCoreApplication, handle: str) -> None:
    """Caught with pcall, each of the four calls raises ``ATE.<fn>: <value>: not a Device`` - the
    same shape as any other Device failure - and none of them is a Python ``AttributeError``."""
    store, ru = _run(app, f"""
        local dev = {handle}
        local ok1, e1 = pcall(ATE.read, dev)
        local ok2, e2 = pcall(ATE.write, dev, {{data = "x"}})
        local ok3, e3 = pcall(ATE.flush, dev)
        local ok4, e4 = pcall(ATE.close, dev)
        ATE.report(Result.PASS, tostring(ok1) .. tostring(ok2) .. tostring(ok3) .. tostring(ok4)
          .. " " .. e1 .. " " .. e2 .. " " .. e3 .. " " .. e4)""")
    _no_traceback(store, ru)
    msg = _step_message(store, ru)
    assert msg.startswith("falsefalsefalsefalse"), msg
    for name in ("read", "write", "flush", "close"):
        assert re.search(rf"ATE\.{name}: (nil|a string|a number): not a Device", msg), msg
