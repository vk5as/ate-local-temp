"""Packet 21b seam-1 tests: the Steps section of the HTML Test Report.

Real `.tslua` trees on disk, run end to end through :class:`HeadlessRunSession`, rendered with
:func:`render_report` and read back with the standard-library `html.parser` (no new dependencies):
the columns, a FAIL row's Expected/Actual, an ERROR step, retried steps with their earlier Attempts
as indented sub-rows at one depth deeper, Sub-schedule headers indented by depth and labelled when
the Operator disabled everything beneath them, steps disabled by default (NOTRUN, not labelled as an
override), the Operator's input values (a CHECKBOX as a plain list), markup in every printed value
escaped rather than rendered, and the Steps table's `<thead>`/`<tbody>` structure that makes the
headings repeat on every printed page."""

from __future__ import annotations

import base64
import hashlib
import html as html_escape
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

from jinja2 import Environment, PackageLoader
from PyQt6.QtCore import QCoreApplication

from ate.config import Config
from ate.report.html import _report_context, render_report
from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.storage.schema import input_value
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

#: Step Name, message, Expected, Actual and an Operator STRING input all contain markup, as does the
#: UUT serial. None of it may reach the page as live HTML.
MARKUP_SOURCE = """\
return ScheduleParams("Steps markup schedule", "printed values that contain markup")
  :setSchedule({
    StepParams("flag <b>checked</b>"):setStepNo("1.")
      :setInputs({ [1] = { desc = "note", type = "STRING" } })
      :setAction(function()
        local n = ATE.input(1)
        ATE.report(Result.PASS, "seen <b>x</b>", n, "<script>expected</script>")
      end),
  })
"""

#: An ENUM input with the CHECKBOX widget: two labels selected, recorded as a JSON array of labels.
CHECKBOX_SOURCE = """\
return ScheduleParams("Steps checkbox schedule", "a checkbox input with two labels ticked")
  :setSchedule({
    StepParams("channels"):setStepNo("1.")
      :setInputs({ [1] = { desc = "channels", type = "ENUM", input = "CHECKBOX", enum = {"Green", "Blue"} } })
      :setAction(function()
        local c = ATE.input(1)
        ATE.report(Result.PASS, "channels asked")
      end),
  })
"""


class _StepsTable(HTMLParser):
    """Every row of the Steps table as ``(row_class, [(cell_text, cell_style), ...])``, in order."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[str, list[tuple[str, str]]]] = []
        self.has_tbody = False
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
        elif tag == "tbody":
            self.has_tbody = True
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


def _steps_sections(html: str) -> tuple[list[tuple[str, list[tuple[str, str]]]], list[tuple[str, list[tuple[str, str]]]], bool]:
    """The Steps table's `<thead>` rows, its `<tbody>` rows, and whether a `<tbody>` was seen."""
    parser = _StepsTable()
    parser.feed(html)
    head = [row for row in parser.rows if row[0] == "thead"]
    body = [row for row in parser.rows if row[0] != "thead"]
    return head, body, parser.has_tbody


class _CssRules(HTMLParser):
    """The report's inline CSS as {selector: declarations}, read from the <style> element."""

    def __init__(self) -> None:
        super().__init__()
        self._in_style = False
        self.text = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        self._in_style = tag == "style"

    def handle_endtag(self, tag: str) -> None:
        self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self.text += data


def _css_rules(html: str) -> dict[str, str]:
    """Every CSS rule in the report, keyed by its selector."""
    css = _CssRules()
    css.feed(html)
    return {sel.strip(): decl.strip() for sel, decl in re.findall(r"([^\n{}]+)\{([^}]*)\}", css.text)}


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
    """The padding-left in mm a cell carries — its indentation in the tree (0 when it has none)."""
    match = re.search(r"padding-left:\s*([\d.]+)mm", cell_style)
    return float(match.group(1)) if match else 0.0


def _name_indent(row: tuple[str, list[tuple[str, str]]]) -> float:
    """Indentation of a row's Step Name / message cell: the depth indent lives there, not on the
    fixed-width Step Number cell, so deeper rows visibly move right."""
    return _indent(row[1][1][1])


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
         stop_on_fail: Optional[bool] = None, input_values: Optional[list] = None,
         serial: str = "B-1") -> tuple[RunStore, str]:
    """Run one schedule through :class:`HeadlessRunSession` and return (store, run_uuid)."""
    root = _write_env(tmp_path, files)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "format": 1,
                "station": {"name": "BENCH-21B", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c21"},
                "defaultProject": "Power board",
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
    session.set_uut_serial(serial)
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


def test_markup_in_printed_values_is_escaped(tmp_path: Path) -> None:
    """Every value the report prints is escaped, never injected as live HTML."""
    html = _report(
        tmp_path,
        {"markup.tslua": MARKUP_SOURCE},
        "Steps markup schedule",
        input_values=["<b>x</b>"],
        serial="<b>serial</b>",
    )
    assert "<script>" not in html, "no printed value reaches the page as a live tag"
    assert "&lt;script&gt;expected&lt;/script&gt;" in html

    # Read back through the parser: the escapes are data, so the cells still say the markup.
    row = _step_rows(html, "1.")[0]
    assert "<b>checked</b>" in row[1][1][0], "the Step Name's <b> is escaped text, not a tag"
    assert "seen <b>x</b>" in row[1][1][0], "the ATE.report message's <b> is escaped text"
    assert row[1][2][0] == "<script>expected</script>", "Expected is escaped text"
    assert row[1][3][0] == "<b>x</b>", "the Operator's STRING input value is escaped text"
    assert "&lt;b&gt;serial&lt;/b&gt;" in html, "the UUT serial on the Cover is escaped"


def test_autoescape_does_not_change_the_cover_bytes(tmp_path: Path) -> None:
    """Cover markup is marked safe, so escaping changes nothing but the escaped characters."""
    store, run_uuid = _run(tmp_path, {"steps.tslua": PASS_FAIL}, "Steps pass fail schedule")
    logo = tmp_path / "logo.png"
    logo.write_bytes(bytes(range(1, 256)))
    config = Config(
        path=tmp_path / "config.json",
        reports_organisation="ACME & Co",
        reports_logo=logo,
        reports_paper_size="A4",
    )
    escaped = render_report(store, run_uuid, config)
    # The same context rendered with autoescape off is what the Cover used to emit.
    plain = Environment(loader=PackageLoader("ate.report", "templates")).get_template("report.html.j2").render(
        **_report_context(store, run_uuid, config)
    )
    assert html_escape.unescape(escaped) == plain, "only escaping differs from the pre-autoescape page"
    assert f"data:image/png;base64,{base64.b64encode(logo.read_bytes()).decode()}" in escaped, "the logo data URI is untouched"
    assert "margin: 24mm 14mm 18mm 14mm;" in escaped, "the print margin CSS is untouched"
    assert escaped.count("<br>") == plain.count("<br>"), "the <br> joins are still joins"


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
    assert _name_indent(sub) > _name_indent(main), "Attempt sub-rows are indented one level deeper"
    assert _indent(main[1][0][1]) == 0.0 and _indent(sub[1][0][1]) == 0.0, "the Step Number cell never moves"


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


def test_checkbox_input_prints_as_labels_not_json(tmp_path: Path) -> None:
    """A CHECKBOX is recorded as a JSON array of labels but printed as "Green, Blue"."""
    html = _report(
        tmp_path, {"checkbox.tslua": CHECKBOX_SOURCE}, "Steps checkbox schedule",
        input_values=[["Green", "Blue"]],
    )
    row = _step_rows(html, "1.")[0]
    assert "Input 1 (channels): Green, Blue" in row[1][1][0]
    assert '["Green"' not in html, "the stored JSON array never reaches the page"


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
    top_indent = _name_indent(_step_rows(html, "1.")[0])  # a top-level row sits at depth 0
    assert _name_indent(header) == top_indent, "a top-level header sits at the top level"
    for child in children:
        assert _name_indent(child) == _name_indent(header) + 4, "a header's steps are indented one level deeper"


def test_header_labelled_when_operator_disables_all_its_steps(tmp_path: Path) -> None:
    """Spec rule 66: a header whose `enabled` differs from its default is labelled, steps only or not."""
    html = _report(
        tmp_path,
        {"main.tslua": SUB_MAIN, "channel.tslua": SUB_CHANNEL},
        "Steps sub schedule",
        enables={2: False, 3: False},
    )
    header = _step_rows(html, "2.")[0]
    assert "disabled by Operator" in header[1][1][0], "the Sub-schedule header is labelled an override"
    assert header[1][2][0] == "2 steps: 2 NOTRUN", "its roll-up still counts the steps beneath it"
    for child in _step_rows(html, "2.1.") + _step_rows(html, "2.2."):
        assert child[1][4][0] == "NOTRUN" and "disabled by Operator" in child[1][1][0]
    untouched = _step_rows(html, "1.")[0]
    assert "Operator" not in untouched[1][1][0], "a header's label does not leak to its neighbours"


def test_step_table_headings_repeat_on_every_page(tmp_path: Path) -> None:
    """The Steps table's structure is what repeats its headings: <thead> headings, rows in <tbody>."""
    html = _report(tmp_path, {"steps.tslua": PASS_FAIL}, "Steps pass fail schedule")
    head, body, has_tbody = _steps_sections(html)
    assert has_tbody, "the step rows sit in a <tbody>"
    assert len(head) == 1, "the Steps table has exactly one heading row, inside its <thead>"
    assert [cell[0] for cell in head[0][1]] == [
        "Step Number", "Step Name / message", "Expected", "Actual", "Result"
    ], "the <thead> carries the five column headings"
    assert len(body) == 2, "both steps are data rows in the <tbody>, not part of the heading"

    rules = _css_rules(html)
    assert "page-break-inside: avoid" in rules.get("tr", ""), "a step row is never split across pages"
    assert "thead" not in rules, "Chromium repeats a <thead> by default; the display rule was inert"


def test_input_values_read_back_in_recorded_order(tmp_path: Path) -> None:
    """`input_values` sorts by `seq`, like every other child read in the store.

    The rows are written out of seq order on purpose: the ORDER BY is what makes the read
    order-stable on any backend, rather than leaving it to the database's scan order."""
    store = RunStore(tmp_path / "order.db")
    for seq, index in ((7, 1), (2, 2), (9, 3)):  # written out of order on purpose
        store._run(
            input_value.insert().values(
                run_uuid="run-order", seq=seq, node_seq=0, attempt_no=1, input_index=index,
                type="STRING", value=f"v{seq}", at="2026-01-01T00:00:00+00:00",
            )
        )
    got = [r["seq"] for r in store.input_values("run-order")]
    assert got == [2, 7, 9], "the read is ordered by seq, not by insertion order"
