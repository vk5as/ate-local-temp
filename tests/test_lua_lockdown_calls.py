"""Ticket 09 criterion b — where each ATE call is allowed.

The Lua host tracks which execution context it is in (load time, a Pre-test Action, a Post-test
Action or a step) and the ``ATE`` functions that are illegal there raise before running:

* While the schedule loads only the builders, ``require``, ``ATE.log`` and the enums may be used;
  calling either ``ATE.report`` or ``ATE.input`` at load time raises.
* Pre-test and Post-test Actions may use everything except ``ATE.input`` and ``ATE.report``; a call
  to either there raises when the action runs through its phase context, as does calling either at
  load time (only the builders, ``require``, ``ATE.log`` and the enums are legal then).

Verified against a real lupa runtime — no mocks."""

from pathlib import Path

import pytest
from lupa import LuaError

from ate.engine.lua_host import HostError, NotAllowedError, LuaHost

from ._fixtures import LOAD_REPORT, PRETEST_INPUT, POSTTEST_INPUT, PRETEST_REPORT
from tests._fixtures import scratch_directory


def _make_host(source: str) -> LuaHost:
    """Construct a host for one schedule on disk and return it."""
    with scratch_directory() as td:
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8", newline="\n")
        return LuaHost(str(schedule_path))


def test_report_in_pretest_action_raises():
    """Criterion b — a Pre-test Action may not call ATE.report; running it through its phase
    context raises with the schedule's file:line and the function name (report is step-only, like
    input)."""
    host = _make_host(PRETEST_REPORT)
    action = host.schedule.pre_test_action

    assert callable(action), "the fixture's pretest action should be a real closure"

    raised: Exception | None = None
    try:
        # Run it with the Pre-test phase active, exactly as the run engine would.
        host._run_action(action, "pretest")
    except NotAllowedError as exc:
        raised = exc

    assert raised is not None, "ATE.report in a pretest action must raise"
    text = str(raised)
    assert "schedule.tslua:" in text
    assert "ATE.report:" in text


def test_report_at_load_time_raises_with_file_line():
    """Criterion b — load time allows only the builders/require/log/enums; a top-level ATE.report
    raises while the file loads. The error carries the schedule's file:line and names the function."""
    with scratch_directory() as td:
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(LOAD_REPORT, encoding="utf-8", newline="\n")

        raised: Exception | None = None
        try:
            LuaHost(str(schedule_path))  # loading the chunk runs it; report must raise here
        except HostError as exc:
            raised = exc

        assert raised is not None, "a load-time ATE.report should surface as a load error"
        text = str(raised)
        assert "schedule.tslua:" in text  # the chunk's own file:line (loaded as @<path>)
        assert "ATE.report:" in text      # the function name is named


def test_input_in_pretest_action_raises():
    """Criterion b — a Pre-test Action may not call ATE.input; running it through its phase context
    raises with the schedule's file:line and the function name."""
    host = _make_host(PRETEST_INPUT)
    action = host.schedule.pre_test_action

    assert callable(action), "the fixture's pretest action should be a real closure"

    raised: Exception | None = None
    try:
        # Run it with the Pre-test phase active, exactly as the run engine would.
        host._run_action(action, "pretest")
    except NotAllowedError as exc:
        raised = exc

    assert raised is not None, "ATE.input in a pretest action must raise"
    text = str(raised)
    assert "schedule.tslua:" in text
    assert "ATE.input:" in text


def test_input_in_posttest_action_raises():
    """Criterion b — the same restriction applies to Post-test Actions."""
    host = _make_host(POSTTEST_INPUT)
    action = host.schedule.post_test_action

    assert callable(action), "the fixture's posttest action should be a real closure"

    raised: Exception | None = None
    try:
        host._run_action(action, "posttest")
    except NotAllowedError as exc:
        raised = exc

    assert raised is not None, "ATE.input in a posttest action must raise"
    text = str(raised)
    assert "schedule.tslua:" in text
    assert "ATE.input:" in text
