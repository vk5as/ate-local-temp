"""Packet 16c — Device failures go through `onError`.

Seam-1 coverage driving a real LuaHost + QThread RunEngine against a loopback peer (no mocks): a failed
open/read/write calls ``onError(opResult, opArgs)`` (the call's own handler, else the one given to
``ATE.open``); a non-nil return is what the call returns and the schedule carries on; a nil return or
no handler raises ``<file>:<line>: ATE.<fn>: <device>: <err>`` and the step records ERROR. Using a
closed Device fails the same way, a second close succeeds, and a bad param never reaches onError."""

import re
import socket

from PyQt6.QtCore import QCoreApplication

from ._fixtures import LoopbackPeer
from .test_tcp_read_write import _run_with_peer, _step_message

# Opens a TCP device on the peer; ``{handler}`` is replaced with the open call's trailing arguments.
_OPEN = 'ATE.open(DeviceType.TCP, {{host="127.0.0.1", port=__PORT__, timeout=5, readTerm="{term}", writeTerm=""}}{handler})'


def _schedule(body: str) -> str:
    """A one-step schedule running ``body``; ``calls`` collects what each handler saw."""
    return f"""return ScheduleParams("x") :setSchedule({{
    StepParams("io"):setStepNo("1.")
      :setAction(function()
        local calls = {{}}
{body}
      end),
  }})"""


def _open(handler: str = "", term: str = "") -> str:
    return _OPEN.format(term=term, handler=f", {handler}" if handler else "")


def _run(app: QCoreApplication, body: str, peer: LoopbackPeer | None = None, port: int | None = None):
    """Run ``body`` against ``peer`` (a silent one by default) and return ``(store, run_uuid)``."""
    peer = peer or LoopbackPeer()
    peer.start()
    try:
        return _run_with_peer(app, _schedule(body), port if port is not None else peer.port)
    finally:
        peer.stop()


def _step_error(store, ru: str) -> str:
    """The single step's result, asserted ERROR, and the message its ERROR attempt recorded."""
    node = next(n for n in store.nodes(ru) if n["kind"] == "step")
    assert node["result"] == "ERROR"
    (attempt,) = store.attempts(ru)
    assert attempt["pass_fail"] == "ERROR"
    return attempt["msg"]


def _dead_port() -> int:
    """A loopback port with nothing listening, so a connect is refused."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_read_uses_its_own_handler_with_result_and_args(app: QCoreApplication) -> None:
    """The read's own onError gets the failure table and the params passed in, open's handler is not
    called, and its non-nil return is what ATE.read returns; the step carries on."""
    store, ru = _run(app, f"""
        local dev = {_open('function() calls[#calls + 1] = "open" end')}
        local params = {{timeout = 0.1, size = 4}}
        local r = ATE.read(dev, params, function(res, args)
          calls[#calls + 1] = tostring(res.data).."|"..res.count.."|"..tostring(res.partial).."|"..res.err
            .."|"..tostring(args == params)
          return "handled"
        end)
        ATE.report(Result.PASS, r.." "..table.concat(calls, ","))""")

    assert _step_message(store, ru) == "handled nil|0|false|timeout|true"


def test_write_falls_back_to_open_handler(app: QCoreApplication) -> None:
    """A write with no onError of its own uses the one given to ATE.open, with the write's params."""
    store, ru = _run(app, f"""
        local dev = {_open('function(res, args) calls[#calls + 1] = res.err.."|"..tostring(res.ok).."|"..res.count.."|"..args.data; return 7 end')}
        ATE.close(dev)
        local w = ATE.write(dev, {{data = "x"}})
        ATE.report(Result.PASS, w.." "..table.concat(calls, ","))""")

    assert _step_message(store, ru) == "7 device closed|false|0|x"


def test_read_falls_back_to_open_handler_when_params_are_skipped(app: QCoreApplication) -> None:
    """``ATE.read(dev, onError)`` takes the function as the handler; with neither, open's is used."""
    store, ru = _run(app, f"""
        local dev = {_open('function(res, args) calls[#calls + 1] = "open:"..res.err.."|"..tostring(args); return 1 end')}
        ATE.close(dev)
        ATE.read(dev, function(res, args) calls[#calls + 1] = "own:"..res.err.."|"..tostring(args); return 1 end)
        ATE.read(dev)
        ATE.report(Result.PASS, table.concat(calls, ","))""")

    assert _step_message(store, ru) == "own:device closed|nil,open:device closed|nil"


def test_handler_returning_nil_raises_and_step_errors(app: QCoreApplication) -> None:
    """An onError that returns nil is called, then the read raises: the step is ERROR and nothing after
    the call runs."""
    store, ru = _run(app, f"""
        local dev = {_open()}
        ATE.read(dev, {{timeout = 0.1}}, function() ATE.log(Log.INFO, "handler ran") end)
        ATE.report(Result.PASS, "carried on")""")

    msg = _step_error(store, ru)
    assert re.fullmatch(r"schedule\.tslua:\d+: ATE\.read: TCP 127\.0\.0\.1:\d+: timeout", msg), msg
    assert any(ln["text"] == "handler ran" for ln in store.log_lines(ru))


def test_no_handler_raises_with_file_line_function_and_device(app: QCoreApplication) -> None:
    """With no onError anywhere a failed read raises ``<file>:<line>: ATE.read: <device>: <err>``, which
    the run records as the step's ERROR; a schedule catching it with pcall sees the same string."""
    store, ru = _run(app, f"""
        local dev = {_open()}
        local ok, err = pcall(ATE.read, dev, {{timeout = 0.1}})
        ATE.log(Log.INFO, "caught "..tostring(ok).." "..err)
        ATE.read(dev, {{timeout = 0.1}})
        ATE.report(Result.PASS, "carried on")""")

    msg = _step_error(store, ru)
    # The line number is not pinned: ATE errors currently report the step's :setAction( line, not
    # the call's own line (shared _location bug, see review-notes.md). Tighten once that is fixed.
    assert re.fullmatch(r"schedule\.tslua:\d+: ATE\.read: TCP 127\.0\.0\.1:\d+: timeout", msg), msg
    assert any(ln["text"] == f"caught false {msg}" for ln in store.log_lines(ru))


def test_open_failure_goes_through_open_handler(app: QCoreApplication) -> None:
    """A refused connect calls open's onError with ``{ok = false, err}`` and the open params; its
    non-nil return is what ATE.open returns."""
    store, ru = _run(app, f"""
        local d = {_open('function(res, args) calls[#calls + 1] = tostring(res.ok).."|"..args.host; return "no device" end')}
        ATE.report(Result.PASS, d.." "..table.concat(calls, ","))""", port=_dead_port())

    assert _step_message(store, ru) == "no device false|127.0.0.1"


def test_open_failure_without_handler_errors_the_step(app: QCoreApplication) -> None:
    """A refused connect with no onError raises, naming ATE.open and the host and port."""
    port = _dead_port()
    store, ru = _run(app, f"""
        local d = {_open()}
        ATE.report(Result.PASS, "carried on")""", port=port)

    msg = _step_error(store, ru)
    assert re.match(rf"schedule\.tslua:\d+: ATE\.open: TCP 127\.0\.0\.1:{port}: ", msg), msg  # line not pinned, as above
    assert "refused" in msg.lower()


def test_closed_device_read_and_write_fail_through_handler_and_second_close_succeeds(app: QCoreApplication) -> None:
    """After close, read and write fail with ``err = "device closed"`` through onError (no raw Python
    error), and closing again returns ``{ok = true}`` without calling onError."""
    store, ru = _run(app, f"""
        local dev = {_open('function(res) calls[#calls + 1] = "close handler" end')}
        ATE.close(dev)
        local keep = function(res) return res end
        local r = ATE.read(dev, {{timeout = 0.1}}, keep)
        local w = ATE.write(dev, {{data = "x"}}, keep)
        local c = ATE.close(dev)
        ATE.report(Result.PASS, tostring(r.data).."|"..r.count.."|"..tostring(r.partial).."|"..r.err
          .." "..tostring(w.ok).."|"..w.count.."|"..w.err.." "..tostring(c.ok).." "..#calls)""",
        peer=LoopbackPeer(payload=b"buffered\n"))

    assert _step_message(store, ru) == "nil|0|false|device closed false|0|device closed true 0"


def test_closed_device_read_without_handler_errors_the_step(app: QCoreApplication) -> None:
    """Reading a closed Device with no onError raises "device closed" as the step's ERROR."""
    store, ru = _run(app, f"""
        local dev = {_open()}
        ATE.close(dev)
        ATE.read(dev, {{timeout = 0.1}})""")

    assert re.fullmatch(r"schedule\.tslua:\d+: ATE\.read: TCP 127\.0\.0\.1:\d+: device closed", _step_error(store, ru))


def test_failed_device_returns_buffered_data_before_failing_through_handler(app: QCoreApplication) -> None:
    """After a reset, bytes already buffered are still read (no onError), and only the next read fails,
    through onError, with the connection error."""
    store, ru = _run(app, f"""
        local dev = {_open('function(res) calls[#calls + 1] = res.err; return "failed" end', term=chr(92) + "n")}
        local a = ATE.read(dev, {{timeout = 2}})
        local b = ATE.read(dev, {{timeout = 2}})
        local c = ATE.read(dev, {{timeout = 2}})
        ATE.report(Result.PASS, a.data.."|"..b.data.."|"..c.."|"..#calls.."|"..calls[1])""",
        peer=LoopbackPeer(payload=b"a\nb", hangup="reset"))

    a, b, c, n, err = _step_message(store, ru).split("|")
    assert (a, b, c, n) == ("a", "b", "failed", "1")
    assert "reset" in err.lower()


def test_bad_param_never_calls_handler(app: QCoreApplication) -> None:
    """A bad param on read, write or open returns the graceful ``false, msg`` and calls neither the
    call's own onError nor open's."""
    store, ru = _run(app, f"""
        local mark = function() calls[#calls + 1] = "called"; return "handled" end
        local dev = {_open('mark')}
        local out = {{}}
        out[#out + 1] = select(2, ATE.read(dev, {{sizee = 1}}, mark))
        out[#out + 1] = select(2, ATE.read(dev, {{sizee = 1}}))
        out[#out + 1] = select(2, ATE.write(dev, {{writeTerm = "x"}}, mark))
        out[#out + 1] = select(2, ATE.open(DeviceType.TCP, {{host = "127.0.0.1"}}, mark))
        ATE.report(Result.PASS, #calls.."\\n"..table.concat(out, "\\n"))""")

    n, *msgs = _step_message(store, ru).split("\n")
    assert n == "0"
    assert "ATE.read: unknown field 'sizee'" in msgs[0]
    assert "ATE.read: unknown field 'sizee'" in msgs[1]
    assert "ATE.write: missing required field 'data'" in msgs[2]
    assert "ATE.open: missing required field 'port'" in msgs[3]
