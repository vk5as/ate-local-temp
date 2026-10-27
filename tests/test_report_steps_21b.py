"""Packet 21b seam-1 tests: the Steps section of the HTML Test Report.

Real `.tslua` trees on disk, run end to end through :class:`HeadlessRunSession`, rendered with
:func:`render_report` and read back with the standard-library `html.parser` (no new dependencies):
the columns, a FAIL row's Expected/Actual, an ERROR step, retried steps with their earlier Attempts
as indented sub-rows, Sub-schedule headers with their roll-up and indentation, Operator override
labels, steps disabled by default (NOTRUN, not labelled as an override), the Operator's input values,
and the print CSS that repeats the step table's headings."""

from __future__ import annotations

import hashlib
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QCoreApplication

from ate.config import Config
from ate.report.html import render_report
from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.storage.store import RunStore

#: One step passes and one fails, so the columns and a failure's Expected/Actual are visible.
PASS_FAIL = """\
return ScheduleParams("Steps pass fail schedule", "one pass, one fail")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("LED colour"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "colour ok", "Green", "Green") end),
    StepParams("output voltage"):setStepNo("2.")
      :setAction(function() ATE.report(Result.FAIL, "too low", "1.9V", "2.4V") end),
  })
"""

#: Attempt 1 fails (the Operator answers RETRY), Attempt 2 passes: the step shows the last Attempt.
RETRY_SOURCE = """\
local attempts = 0
return ScheduleParams("Steps retry schedule", "a step retried until it passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("first"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "cold joint", "0.2V", "2.4V")
        else
          ATE.report(Result.PASS, "warm now", "2.4V", "2.4V")
        end
      end),
  })
"""

#: Step "b" is off by default; the Operator turns "a" off. Only a step whose `enabled` differs from
#: its `default_enabled` is labelled as an override.
ENABLE_SOURCE = """\
return ScheduleParams("Steps enable schedule", "default-disabled and Operator-disabled steps")
  :setSchedule({
    StepParams("a"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "ran a") end),
    StepParams("b"):setStepNo("2.")
      :setEnable(false)
      :setAction(function() ATE.report(Result.PASS, "should not run") end),
    StepParams("c"):setStepNo("3.")
      :setAction(function() ATE.report(Result.PASS, "ran c") end),
  })
"""

#: One step asks for its 1st input (STRING "LED colour"); the driver answers "Green".
INPUT_SOURCE = """\
return ScheduleParams("Steps input schedule", "a step with an Operator-entered input")
  :setSchedule({
    StepParams("colour check"):setStepNo("1.")
      :setInputs({ [1] = { desc = "LED colour", type = "STRING" } })
      :setAction(function() local c=ATE.input(1); ATE.report(Result.PASS, "saw " .. c, c, "Green") end),
  })
"""

#: A Lua error in a step becomes an ERROR Attempt whose message is the error text.
ERROR_SOURCE = """\
return ScheduleParams("Steps error schedule", "a step that raises")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("boom"):setStepNo("1.")
      :setAction(function() error("kaboom from Lua") end),
  })
"""

#: A Sub-schedule header whose two child steps disagree, sitting beside a top-level step.
SUB_MAIN = """\
return ScheduleParams("Steps sub schedule", "an include header over two steps")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("setup"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "setup done") end),
    StepParams("channel 1"):setStepNo("2."):setPrefix("2."):setInclude("channel.tslua", { channel = 1 }),
  })
"""

SUB_CHANNEL = """\
local kv = ...
local ch = kv.channel or 1
return ScheduleParams("Channel check", "one channel, one pass one fail")
  :setSchedule({
    StepParams("output on"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "channel " .. ch, tostring(ch), tostring(ch)) end),
    StepParams("output bad"):setStepNo("2.")
      :setAction(function() ATE.report(Result.FAIL, "bad " .. ch, "0.1V", "2.4V") end),
  })
"""


class _StepsTable(HTMLParser):
    """Every row of the Steps table as ``(row_class, [(cell_text, cell_style), ...])``, in order."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[str, list[tuple[str, str]]]] = []
        self._in_steps = False
        self._tables = 0
        self._in_thead = False
        self._row_class = ""
        self._cells: list[tuple[str, str]] = []
        self._cell_depth = 0
        self._cell_style = ""
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table":
            self._tables += 1
            self._in_steps = any(name == "class" and value and "steps" in value for name, value in attrs)
        elif tag == "tr" and self._in_steps:
            self._row_class = ("thead" if self._in_thead else (dict(attrs).get("class") or ""))
            self._cells = []
        elif tag == "thead":
            self._in_thead = True
        elif tag in ("td", "th") and self._in_steps:
            self._cell_depth += 1
            self._cell_style = dict(attrs).get("style") or ""
            self._parts = []
        elif tag == "br" and self._cell_depth:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self._tables -= 1
            if self._tables == 0:
                self._in_steps = False
        elif tag == "thead":
            self._in_thead = False
        elif tag in ("td", "th") and self._cell_depth:
            self._cell_depth -= 1
            self._cells.append((" ".join("".join(self._parts).split()), self._cell_style))
        elif tag == "tr" and self._in_steps:
            self.rows.append((self._row_class, self._cells))

    def handle_data(self, data: str) -> None:
        if self._cell_depth:
            self._parts.append(data)


def _steps_rows(html: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """Every row of the rendered report's Steps table."""
    parser = _StepsTable()
    parser.feed(html)
    assert parser.rows, "no Steps table rows in the rendered report"
    return parser.rows


def _step_rows(html: str, step_number: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """The Steps rows carrying this Step Number — a main row, and its header's number for headers."""
    return [
        row for row in _steps_rows(html)
        if row[0] != "thead" and row[1][0][0] == step_number
    ]


def _sub_rows(html: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """The Attempt sub-rows of a retried step."""
    return [row for row in _steps_rows(html) if row[0] == "sub-row"]


def _indent(cell_style: str) -> float:
    """The padding-left in mm a row's first cell carries — its indentation in the tree."""
    match = re.search(r"padding-left:\s*([\d.]+)mm", cell_style)
    return float(match.group(1)) if match else 0.0


def _write_env(tmp_path: Path, files: dict[str, str]) -> Path:
    """Write one Test Definition release holding `files` (relative path -> source); return its root."""
    root = tmp_path / "defs"
    directory = root / "rel"
    directory.mkdir(parents=True, exist_ok=True)
    entries = [
        {"path": rel, "sha256": hashlib.sha256(src.encode("utf-8")).hexdigest()}
        for rel, src in files.items()
    ]
    (directory / "rel.atedef").write_text(
        json.dumps(
            {"format": 1, "mediaNumber": "MR-21B", "name": "Steps release", "version": "1.0.0", "files": entries}
        ),
        encoding="utf-8",
    )
    for rel, src in files.items():
        (directory / rel).write_text(src, encoding="utf-8")
    return root


def _run(tmp_path: Path, files: dict[str, str], schedule: str, *, enables: Optional[dict[int, bool]] = None,
         stop_on_fail: Optional[bool] = None, input_values: Optional[list[str]] = None) -> tuple[RunStore, str]:
    """Run one schedule through :class:`HeadlessRunSession` and return (store, run_uuid)."""
    root = _write_env(tmp_path, files)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "format": 1,
                "station": {"name": "BENCH-21B", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c21"},
                "database": {"type": "sqlite", "path": str(tmp_path / "ate.db")},
                "reports": {"directory": str(tmp_path / "reports")},
                "testDefinitions": {"directories": [str(root)]},
            }
        ),
        encoding="utf-8",
    )

    session = HeadlessRunSession(config_path)
    session.set_operator("Alice")
    session.set_uut_type("Board")
    session.set_uut_serial("B-1")
    session.select_schedule(schedule)
    if stop_on_fail is not None:
        session.set_stop_on_fail(stop_on_fail)
    for seq, enabled in (enables or {}).items():
        session.toggle_node(seq, enabled)

    engine = session.start()

    if input_values is not None:
        operator = engine.operator_input

        def _supply(_seq: int, _attempt: int, index: int) -> None:
            operator.supply_input(index - 1, input_values[index - 1])

        session.input_asked.connect(_supply)

    # A Stop-on-Fail pause needs an answer: retry the first failure, continue on any later one.
    retried = {"done": False}

    def _answer(_seq: int, _attempt: int) -> None:
        if not retried["done"]:
            retried["done"] = True
            session.choose("RETRY")
        else:
            session.choose("CONTINUE")

    session.failure_prompt.connect(_answer)

    # Wait for the run-end report too, so the PDF helper's QProcess has finished before the session
    # goes out of scope (a live helper at teardown crashes this Qt build).
    written: list[tuple[str, str]] = []
    session.report_written.connect(lambda html_path, pdf_path: written.append((html_path, pdf_path)))

    app = QCoreApplication.instance()
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        app.processEvents()
        if session._state == SessionState.FINISHED and written:
            return session.store, session.run_uuid
        time.sleep(0.005)
    raise TimeoutError("the run never reached FINISHED with its report written")


def _report(tmp_path: Path, files: dict[str, str], schedule: str, **kwargs) -> str:
    """Run a schedule headlessly and render its report; return the HTML."""
    store, run_uuid = _run(tmp_path, files, schedule, **kwargs)
    return render_report(store, run_uuid, Config(path=tmp_path / "config.json"))


# -- the table itself ---------------------------------------------------------


def test_step_table_columns_and_pass_fail_rows(tmp_path: Path) -> None:
    """One row per node with the spec's columns; a FAIL row prints its stored Expected and Actual."""
    html = _report(tmp_path, {"steps.tslua": PASS_FAIL}, "Steps pass fail schedule")
    rows = _steps_rows(html)

    assert rows[0][1] == [
        ("Step Number", ""),
        ("Step Name / message", ""),
        ("Expected", ""),
        ("Actual", ""),
        ("Result", ""),
    ], "the Steps table has the columns the format specifies"

    passing = _step_rows(html, "1.")[0]
    assert passing[1][0][0] == "1." and passing[1][1][0] == "LED colour colour ok"
    assert passing[1][2][0] == "Green" and passing[1][3][0] == "Green"
    assert passing[1][4][0] == "PASS"

    failing = _step_rows(html, "2.")[0]
    assert "too low" in failing[1][1][0]
    # Printed exactly as stored: no units or limits added by ATE.
    assert failing[1][2][0] == "2.4V" and failing[1][3][0] == "1.9V"
    assert failing[1][4][0] == "FAIL"


def test_retried_step_shows_attempt_subrows(tmp_path: Path) -> None:
    """A retried step shows "↻ n" and its earlier Attempts as indented sub-rows with the choice."""
    html = _report(tmp_path, {"retry.tslua": RETRY_SOURCE}, "Steps retry schedule")
    main = _step_rows(html, "1.")[0]
    subs = _sub_rows(html)
    assert len(subs) == 1, "Attempt 1 appears as a sub-row under the step's main row"

    sub = subs[0]
    assert "↻ 2" in main[1][1][0], "the retried step shows its Attempt count"
    assert main[1][4][0] == "PASS" and main[1][3][0] == "2.4V"  # the main row is the last Attempt
    assert sub[0] == "sub-row" and "cold joint" in sub[1][1][0]
    assert "→ RETRY" in sub[1][1][0], "the sub-row prints the prompt choice after the message"
    assert sub[1][4][0] == "FAIL" and sub[1][3][0] == "0.2V" and sub[1][2][0] == "2.4V"
    assert _indent(sub[1][0][1]) > _indent(main[1][0][1]), "Attempt sub-rows are indented"


def test_operator_override_labelled_and_default_disable_not(tmp_path: Path) -> None:
    """A step the Operator disabled is labelled; a step off by default is NOTRUN but not labelled."""
    html = _report(
        tmp_path, {"enable.tslua": ENABLE_SOURCE}, "Steps enable schedule", enables={0: False}
    )
    rows = _steps_rows(html)

    disabled = _step_rows(html, "1.")[0]
    assert disabled[1][4][0] == "NOTRUN"
    assert "disabled by Operator" in disabled[1][1][0]

    by_default = _step_rows(html, "2.")[0]
    assert by_default[1][4][0] == "NOTRUN"
    assert "Operator" not in by_default[1][1][0], "a step off by default is not an override"

    ran = _step_rows(html, "3.")[0]
    assert ran[1][4][0] == "PASS" and "Operator" not in ran[1][1][0]
    assert len(rows) == 4, "the heading row plus one row per node of the expanded tree, disabled steps included"

    # The Operator turning on a step that is off by default is an override in the other direction.
    html_on = _report(tmp_path, {"enable.tslua": ENABLE_SOURCE}, "Steps enable schedule", enables={1: True})
    forced_on = _step_rows(html_on, "2.")[0]
    assert forced_on[1][4][0] == "PASS", "the step the Operator enabled ran"
    assert "enabled by Operator" in forced_on[1][1][0]


def test_input_values_print_under_the_step(tmp_path: Path) -> None:
    """Operator-entered input values appear as a small line under the name and message."""
    html = _report(
        tmp_path, {"inputs.tslua": INPUT_SOURCE}, "Steps input schedule", input_values=["Green"]
    )
    row = _step_rows(html, "1.")[0]
    assert "Input 1 (LED colour): Green" in row[1][1][0]
    assert "saw Green" in row[1][1][0]  # the last Attempt's message is there too


def test_error_step_shows_error_result_and_message(tmp_path: Path) -> None:
    """A step whose action raised is an ERROR row with the error text as its message."""
    html = _report(tmp_path, {"error.tslua": ERROR_SOURCE}, "Steps error schedule")
    row = _step_rows(html, "1.")[0]
    assert row[1][4][0] == "ERROR"
    assert "kaboom from Lua" in row[1][1][0]


def test_sub_schedule_header_indent_and_rollup(tmp_path: Path) -> None:
    """An include header is indented by depth, its name spans to Actual, and its Result holds counts."""
    html = _report(
        tmp_path,
        {"main.tslua": SUB_MAIN, "channel.tslua": SUB_CHANNEL},
        "Steps sub schedule",
    )
    rows = _steps_rows(html)
    header = _step_rows(html, "2.")[0]
    assert header[0] == "hdr", "header rows are styled as headers"
    assert len(header[1]) == 3, "the header's name spans Step Name through Actual"
    assert "channel 1" in header[1][1][0]
    assert header[1][2][0] == "2 steps: 1 PASS, 1 FAIL"  # counts of the steps beneath it
    assert "PASS" not in header[1][1][0]

    children = _step_rows(html, "2.1.") + _step_rows(html, "2.2.")
    assert len(children) == 2
    top_indent = _indent(_step_rows(html, "1.")[0][1][0][1])  # a top-level row sits at depth 0
    assert _indent(header[1][0][1]) == top_indent, "a top-level header sits at the top level"
    for child in children:
        assert _indent(child[1][0][1]) == _indent(header[1][0][1]) + 4, "a header's steps are indented one level deeper"


def test_step_table_headings_repeat_on_every_page(tmp_path: Path) -> None:
    """The print CSS makes the step table's column headings repeat after every page break (rule 32)."""
    html = _report(tmp_path, {"steps.tslua": PASS_FAIL}, "Steps pass fail schedule")
    assert "thead { display: table-header-group; }" in html
    assert "page-break-inside: avoid" in html, "a step row is never split across pages"
