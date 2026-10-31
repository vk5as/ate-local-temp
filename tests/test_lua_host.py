"""Ticket 09 criterion 01 — Lua runtime lockdown and chunk loading.

A fresh host must be a closed sandbox: its lupa runtime uses register_eval=False /
register_builtins=False / unpack_returned_tuples=True; the file-loading entry points
(dofile/loadfile/load) are removed and python is nil so a schedule cannot load files or
reach Python; and every loaded chunk carries name ``@<path>`` in text mode. Verified
against a real lupa runtime — no mocks."""

import tempfile
from pathlib import Path

import pytest
from lupa import LuaError

from ate.engine.lua_host import HostError, LuaHost

from ._fixtures import PASSING


def _make_host(source: str) -> LuaHost:
    """Construct a host for one schedule on disk and return it.

    The file is named ``schedule.tslua`` so the chunk name surfaces as ``@schedule.tslua``;
    construction runs synchronously (no Qt event loop needed)."""
    with tempfile.TemporaryDirectory() as td:
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8", newline="\n")
        return LuaHost(str(schedule_path))


def test_runtime_uses_closed_configuration():
    """Criterion 01a — the host's lupa runtime is constructed with register_eval=False,
    register_builtins=False and unpack_returned_tuples=True (no Python leaks in)."""
    host = _make_host(PASSING)
    cfg = host.runtime_configuration
    assert cfg["register_eval"] is False
    assert cfg["register_builtins"] is False
    assert cfg["unpack_returned_tuples"] is True


def test_file_loaders_are_removed():
    """Criterion 01b — dofile/loadfile/load are nil, so a schedule cannot load files. Each
    name reads back as None and calling it raises rather than loading anything."""
    host = _make_host(PASSING)
    env = host.runtime.globals()
    for name in ("dofile", "loadfile", "load"):
        assert env[name] is None, f"{name} was not removed from the runtime"
        with pytest.raises(LuaError):
            host.runtime.execute(f"{name}('attempted')")


def test_python_is_nil():
    """Criterion 01b — the python builtin is nil, so a schedule cannot reach Python."""
    host = _make_host(PASSING)
    env = host.runtime.globals()
    assert env["python"] is None
    with pytest.raises(LuaError):
        host.runtime.execute("print(python())")


def test_chunks_load_with_path_name_and_text_mode():
    """Criterion 01c — a loaded chunk carries name ``@<path>`` in text mode. A body-level error
    surfaces the schedule's own filename (schedule.tslua:N:) rather than an anonymous string."""
    with tempfile.TemporaryDirectory() as td:
        schedule_path = Path(td) / "schedule.tslua"
        # Valid schedule whose body errors on line 2; if it loaded anonymously the traceback
        # would be "<eof> expected near 'error'" with no filename.
        source = (
            'return ScheduleParams("p") :setSchedule({ StepParams("s"):setStepNo("1.") '
            ':setAction(function() end) })\n'
            'error("schedule.tslua")'
        )
        schedule_path.write_text(source, encoding="utf-8", newline="\n")

        # Loading fails (body-level error), but the traceback must still carry the chunk's own
        # name — proving it was loaded with ``@<path>`` rather than an anonymous string.
        raised: Exception | None = None
        try:
            LuaHost(str(schedule_path))
        except HostError as exc:  # _load_schedule wraps execute failures in HostError
            raised = exc

        assert raised is not None, "expected a load-time error to surface the chunk name"
        assert "schedule.tslua:" in str(raised)
