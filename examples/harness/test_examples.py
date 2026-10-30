"""CI harness for the example Media Release (packets 29a-29e).

Every visible Test Schedule in ``examples/media-release/`` runs headless through the seam-1 run
session with scripted Operator answers, and its recorded Test Run is checked here: the Test
Definition scans clean (no broken rows, no checksum mismatch), the schedule metadata and Step
Numbers are recorded, every `Result` value appears, a step that never reports records NOREPORT, a
step that raises records ERROR, the log levels and Equipment are recorded, the step disabled by
default stays NOTRUN until the Operator enables it, Stop on Fail ends both ways, every Step Input
type and widget is declared, answered and recorded, and every run writes its report. Packet 29c
adds the flow schedule: a step that fails on Attempt 1 and passes on a scripted Retry, Pre-test and
Post-test Actions across a Sub-schedule tree, a hidden Sub-schedule included more than once with
different keyvals and prefixes, a nested include, a disabled header, and modules shared between the
includes. Packet 29d adds the Device schedule: every Device Type run against the simulator peers
`examples/harness/peers.py` starts, with its partial reads, timed-out reads, onError handlers,
per-call overrides, Device opened in a Pre-test Action, and the Device trace lines the harness
config turns on. Packet 29e closes the example out as a regression suite: every run writes its
Test Report (name, PDF beside it, Cover/Steps/Equipment/Configuration/Run log in the HTML), the
Cover shows every Run detail, Schedule and Media Release field and the Summary's seven counts, the
footer names the run UUID and both ATE versions, the Run log carries every INFO/WARNING/ERROR line
grouped by owner, and every visible schedule has a run that ends PASS.

Run it on its own with `pytest examples/`, or as part of the regular suite (`pytest`):
`testpaths` in pyproject.toml collects `examples` too.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from ate.catalog import (
    definition_mismatches,
    find_definitions,
    list_schedule_mismatches,
    list_schedule_rows,
    module_map,
)

from examples.harness.peers import FLUSHED_BYTES, REPLIES
from examples.harness.run_examples import RELEASE_DIR, RUNS, run_all, visible_schedules

#: The commands the Device schedule sends the SCPI peer, in include order: the Pre-test Device asks
#: one question, the TCP and SERIAL Sub-schedules four each (VISA never sends MULTI?, because it has
#: no receive buffer to flush).
SCPI_COMMANDS = (
    ["MEAS:VOLT?"]
    + ["*IDN?", "PARTIAL?", "NEVER?", "MULTI?"] * 2
    + ["*IDN?", "PARTIAL?", "NEVER?"]
)

#: Every Device Type the schedule opens, and the peer it reaches.
DEVICE_KINDS = ("TCP", "UDP", "SERIAL", "VISA")


def _ms(text: str) -> float:
    """The wall-clock milliseconds a Device trace line claims, from its trailing `(n.n ms)`."""
    return float(text.rsplit("(", 1)[1].removesuffix(" ms)"))


@pytest.fixture(scope="module")
def records(tmp_path_factory) -> dict[str, dict[str, Any]]:
    """Every example schedule run once headless, keyed by the harness's run key.

    The PDF helper is the one conftest.py installs for every test, so nothing here patches it."""
    return run_all(tmp_path_factory.mktemp("example-release"))


def test_example_release_scans_with_no_broken_entries_and_no_mismatches() -> None:
    """The Test Definition lists every example file, checksummed, and scans clean."""
    definitions = find_definitions(RELEASE_DIR)
    assert len(definitions) == 1
    definition = definitions[0]
    assert definition.broken is None
    assert definition.media_number == "MR-EXAMPLE"
    assert definition.name == "ATE Example Release"
    assert definition.version == "1.0.0"

    listed = {entry.rel_path for entry in definition.files}
    on_disk = {p.relative_to(RELEASE_DIR).as_posix()
               for p in RELEASE_DIR.rglob("*") if p.suffix in (".tslua", ".lua")}
    assert listed == on_disk, "every example file is in the Test Definition and nothing else"

    for entry in definition.files:
        expected = entry.expected_sha256
        assert expected, f"{entry.rel_path} has no recorded checksum"
        assert expected == hashlib.sha256((RELEASE_DIR / entry.rel_path).read_bytes()).hexdigest()

    assert definition_mismatches(definition) == []
    assert list_schedule_mismatches(RELEASE_DIR) == {}

    rows = list_schedule_rows(RELEASE_DIR)
    assert all(row.error is None for row in rows), "no broken rows in the picker"
    names = sorted(row.name for row in rows)
    assert names == ["Device types", "Example basics", "Flow and retry", "Report every Result",
                     "Step inputs", "Stop on fail"]
    assert sorted(visible_schedules()) == names

    # Packet 29c: the Sub-schedules are listed so includes can resolve them, but hidden means the
    # Operator is never offered one as a Test Schedule in its own right.
    hidden = sorted(entry.rel_path for entry in definition.files if entry.hidden)
    assert hidden == ["nest_a.tslua", "nest_b.tslua", "nest_c.tslua", "sub_channel.tslua",
                      "sub_device.tslua"]
    sub_schedule_names = {"Channel check", "Device check", "Nested A", "Nested B", "Nested C"}
    assert not sub_schedule_names & set(names), "hidden Sub-schedules are not offered in the picker"

    # Module names: derived from the path (lib/attempts.lua -> lib.attempts), and explicit where the
    # Test Definition gives a "module" field (modules/helper.lua -> example_helper).
    modules = module_map(definition)
    assert sorted(modules) == ["example_helper", "lib.attempts"]
    explicit = next(e for e in definition.files if e.rel_path == "modules/helper.lua")
    derived = next(e for e in definition.files if e.rel_path == "lib/attempts.lua")
    assert (explicit.module_name, derived.module_name) == ("example_helper", "lib.attempts")


def test_every_visible_example_schedule_runs_headless(records: dict[str, dict[str, Any]]) -> None:
    """Each example schedule runs to a recorded Test Run through the headless session.

    schedules, thirteen runs: Stop on fail three times (Continue, Abort, and with its failing
    steps disabled so the schedule has a green run), Example basics twice (its optional step left
    disabled, then enabled by the Operator), Flow and retry twice (Stop on Fail with a Retry, and
    with Stop on Fail switched off), Report every Result twice (as written, and with its FAIL/ERROR
    steps disabled), and the Device schedule three times (as written, with its disabled-by-default
    `onError` returns nil step enabled, and with its SCPI peer unreachable). The extra runs are the
    `RUNS` entries that repeat a schedule, so a new planned run is counted here rather than the
    total being hard-coded."""
    extra_runs = len(RUNS) - len({entry["schedule"] for entry in RUNS})
    assert len(records) == len(visible_schedules()) + extra_runs, \
        f"{len(RUNS)} runs planned, {len(records)} recorded"
    assert {entry["schedule"] for entry in RUNS} == set(visible_schedules())
    for record in records.values():
        run = record["run"]
        assert run is not None, "the run row is written"
        assert run["result"] in ("PASS", "FAIL")
        assert run["end_reason"] in ("COMPLETED", "STOPPED_ON_FAIL", "PRETEST_FAILED"), \
            "the Device schedule with its peer down ends PRETEST_FAILED"
        assert run["ended_at"], "the run was closed, not left open"
        assert [release["media_number"] for release in record["media_releases"]] == ["MR-EXAMPLE"]
        # Every file the run loaded is recorded with its expected and computed checksum. ATE's own
        # bundled modules (require("ate.devices")) are not in any Test Definition, so they have no
        # expected checksum and cannot mismatch.
        assert [file["kind"] for file in record["run_files"]], "the run records the files it loaded"
        assert all(file["expected_sha256"] == file["computed_sha256"]
                   for file in record["run_files"] if file["kind"] != "ate-module")

        # The report really arrives: HTML on disk, PDF beside it, named after this run's UUT.
        assert record["reports"], "report_written fired for this run"
        html, pdf = record["reports"][0]
        assert html and Path(html).exists(), f"the HTML report is missing: {html!r}"
        assert pdf and Path(pdf).exists(), f"the PDF report is missing: {pdf!r}"
        assert "EX-0001" in html and run["result"] in Path(html).name, "the report is this run's"

        # Every prompt was one the harness scripted: a schedule that pauses when it does not say it
        # will is a bug, not something to answer.
        assert record["unexpected_prompts"] == [], f"{run['schedule_name']} paused unexpectedly"


#: The Test Report's sections in the order the spec lists them
#: (.scratch/ate-v1-wayfinding/assets/test-report-format.md): Cover, Steps, Equipment,
#: Configuration, Run log. The Cover is everything before the first <h2>.
REPORT_SECTIONS = ("Steps", "Equipment", "Configuration", "Run log")

#: The Summary counts every Result value, zeros included, in the order the Cover prints them.
SUMMARY_LABELS = ("PASS", "FAIL", "ERROR", "NOREPORT", "SKIPPED", "FORCED_NOTRUN", "NOTRUN")

#: The Cover's Run-details labels, exactly as the template prints them.
COVER_LABELS = ("UUT Type", "Serial", "Project", "Station", "Start", "End", "Duration",
                "ATE version", "Stop on Fail", "Schedule", "Media Release")

#: How long a Duration cell looks: "45 s", "3 min 7 s" or "1 h 2 min 3 s".
DURATION_RE = re.compile(r"(?:\d+ h \d+ min \d+ s|\d+ min \d+ s|\d+ s)")

#: A time the report shows: local wall-clock with the Station's UTC offset beside it.
LOCAL_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{2}:\d{2}")


class _ReportParser(HTMLParser):
    """The Test Report's structure: every <h2> section, its <h3> groups and their table rows.

    Replaces the regex split on `<h2>`, which a heading with attributes, a tag nested inside a
    heading, or an escaped `&lt;h2&gt;` in a log line could all corrupt. Text is unescaped as it is
    collected, so an escaped heading stays text and never opens a section:

    * `sections` — heading -> the text under it, in document order, with "Cover" first;
    * `rows` — section -> every table row of that section as a list of cell texts;
    * `groups` — the current section's <h3> groups, each a (label, rows) pair.
    """

    COVER = "Cover"

    def __init__(self) -> None:
        super().__init__()
        self.sections: dict[str, str] = {self.COVER: ""}
        self.rows: dict[str, list[list[str]]] = {}
        self.groups: list[tuple[str, list[list[str]]]] = []
        self._section = self.COVER
        self._heading: list[str] | None = None
        self._cell: list[str] | None = None
        self._row: list[str] = []

    @staticmethod
    def _text(parts: list[str]) -> str:
        return " ".join("".join(parts).split())

    def _note(self, text: str) -> None:
        self.sections[self._section] += text + " "

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("h2", "h3"):
            self._heading = []
        elif tag == "td" or tag == "th":
            self._cell = []
        elif tag == "tr":
            self._row = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2":
            name = self._text(self._heading or [])
            self._section = name
            self.sections.setdefault(name, "")
            self.groups = []
            self._heading = None
        elif tag == "h3":
            name = self._text(self._heading or [])
            self._note(name)
            self.groups.append((name, []))
            self._heading = None
        elif tag == "td" or tag == "th":
            cell = self._text(self._cell or [])
            self._row.append(cell)
            self._note(cell)
            self._cell = None
        elif tag == "tr":
            if self._row:
                self.rows.setdefault(self._section, []).append(self._row)
                if self.groups:
                    self.groups[-1][1].append(self._row)
            self._row = []

    def handle_data(self, data: str) -> None:
        if self._heading is not None:
            self._heading.append(data)
        elif self._cell is not None:
            self._cell.append(unescape(data))
        else:
            self._note(unescape(data))


def _report_sections(text: str) -> dict[str, str]:
    """The report split on its <h2> headings, keyed by heading, in document order.

    The first chunk, before any <h2>, is the Cover."""
    return _parse_report(text).sections


def _parse_report(text: str) -> _ReportParser:
    """Parse one report's HTML into sections, rows and <h3> groups."""
    parser = _ReportParser()
    parser.feed(text)
    return parser


def _cover_fields(rows: list[list[str]]) -> dict[str, str]:
    """The Cover's Run-details table as {label: value}."""
    return {row[0]: " ".join(row[1:]) for row in rows if row and row[0] in COVER_LABELS}


def test_report_sections_are_parsed_and_not_split_on_heading_markup() -> None:
    """A heading with attributes, a tag nested in it, and an escaped `<h2>` in a log line all survive."""
    html = """<!DOCTYPE html><html><body>
      <h1>Test Report</h1>
      <h2 class="page-break">Steps</h2>
      <table class="steps"><thead><tr><th>Step Number</th><th>Step Name / message</th></tr></thead>
      <tbody><tr><td>1.</td><td>A step that logged &lt;h2&gt;Failures&lt;/h2&gt;</td></tr></tbody></table>
      <h2 class="x"><span>Run log</span></h2>
      <h3>Run</h3>
      <table class="log"><thead><tr><th>Time</th><th>Level</th><th>Source</th><th>Text</th></tr></thead>
      <tbody><tr><td>2026-10-17 14:03:22 +10:30</td><td>INFO</td><td>ate</td><td>&lt;h2&gt;Failures</td></tr></tbody></table>
    </body></html>"""
    parser = _parse_report(html)

    assert list(parser.sections) == ["Cover", "Steps", "Run log"], f"sections are {list(parser.sections)}"
    assert "Test Report" in parser.sections["Cover"], "the text before the first heading is not the Cover"
    assert "<h2>Failures</h2>" in parser.sections["Steps"], "the escaped heading in a cell is not kept as text"
    assert "Failures" not in parser.sections, "an escaped <h2> in a log line opened a section"
    assert parser.groups == [("Run", [["Time", "Level", "Source", "Text"],
                                     ["2026-10-17 14:03:22 +10:30", "INFO", "ate", "<h2>Failures"]])], parser.groups


def _expected_run_log(record: dict[str, Any]) -> list[tuple[tuple[str, ...], list[str]]]:
    """The Run log the spec defines, built straight from the record.

    Groups in order: the run as a whole, then each Pre-test and Post-test Action in the order its
    Actions ran, then each step Attempt in tree order; every group in time (recorded) order. A line's
    owner is its `node_seq`/`attempt_no`, its `action_seq` (the owning instance), or nothing.
    `DEBUG` lines and the Device trace stay in the database and never reach a report."""
    logs = record["logs"]
    actions = record["actions"]
    instances = {row["instance_seq"]: row for row in record["schedule_instances"]}
    steps = {row["node_seq"]: row for row in record["nodes"] if row["kind"] == "step"}

    step_positions = [i for i, line in enumerate(logs) if line["node_seq"] is not None]
    first_step = step_positions[0] if step_positions else None
    last_step = step_positions[-1] if step_positions else None
    pre_instances = {a["instance_seq"] for a in actions if a["phase"] == "pre"}
    post_instances = {a["instance_seq"] for a in actions if a["phase"] == "post"}
    last_pre_row = max((a["seq"] for a in actions if a["phase"] == "pre" and a["seq"] is not None), default=None)

    def owner(line: dict[str, Any], index: int) -> tuple:
        if line["node_seq"] is not None:
            return ("step", line["node_seq"], int(line["attempt_no"] or 0))
        action_seq = line["action_seq"]
        if action_seq is None:
            return ("run",)
        if first_step is not None and index < first_step:
            return ("pre", action_seq)
        if last_step is not None and index > last_step:
            return ("post", action_seq)
        # A phase Action's lines recorded between the steps: settled by which phase owns the
        # instance, and by the action rows when an instance is in both phases.
        if action_seq in pre_instances and action_seq not in post_instances:
            return ("pre", action_seq)
        if action_seq in post_instances and action_seq not in pre_instances:
            return ("post", action_seq)
        if last_pre_row is not None and line["seq"] < last_pre_row:
            return ("pre", action_seq)
        return ("post", action_seq)

    grouped: dict[tuple, list[str]] = {}
    for index, line in enumerate(logs):
        if line["level"] in ("INFO", "WARNING", "ERROR"):
            # HTML collapses the newlines of a multi-line log line into spaces.
            grouped.setdefault(owner(line, index), []).append(" ".join((line["text"] or "").split()))

    def phase_order(phase: str) -> list[int]:
        return [a["instance_seq"] for a in sorted((a for a in actions if a["phase"] == phase),
                                                 key=lambda a: (a["run_order"] or 0, a["instance_seq"] or 0))]

    out: list[tuple[tuple[str, ...], list[str]]] = []
    if ("run",) in grouped:
        out.append((("Run",), grouped[("run",)]))
    for phase in ("pre", "post"):
        phrase = "Pre-test" if phase == "pre" else "Post-test"
        more_than_one = sum(1 for a in actions if a["phase"] == phase) > 1
        for instance_seq in phase_order(phase):
            key = (phase, instance_seq)
            if key in grouped:
                label = (phrase,) + ((instances[instance_seq]["schedule_name"], str(instance_seq)) if more_than_one else ())
                out.append((label, grouped[key]))
    for node_seq, node in steps.items():  # the record returns the tree in order
        for attempt in sorted(k[2] for k in grouped if k[0] == "step" and k[1] == node_seq):
            label = ("Step", node["step_number"], node["step_name"], f"attempt {attempt}")
            out.append((label, grouped[("step", node_seq, attempt)]))
    return out


def _check_run_log(parser: _ReportParser, record: dict[str, Any], key: str) -> None:
    """Every INFO/WARNING/ERROR line of the run is in the report, in the spec's groups, in time order."""
    groups = [(label, [row for row in rows if row[:1] != ["Time"]]) for label, rows in parser.groups]
    expected = _expected_run_log(record)
    logged = [line for line in record["logs"] if line["level"] in ("INFO", "WARNING", "ERROR")]
    assert sum(len(texts) for _, texts in expected) == len(logged), \
        f"{key}: the record has {len(logged)} reportable lines, the groups hold {sum(len(t) for _, t in expected)}"
    assert len(groups) == len(expected), f"{key}: the Run log has {len(groups)} groups, expected {len(expected)}"
    for (label, rows), (need, texts) in zip(groups, expected):
        assert all(part in label for part in need), f"{key}: run log group {label!r} is not {need}"
        assert [row[3] for row in rows] == texts, f"{key}: {label} lines are {rows!r}, not {texts!r}"
        times = [row[0] for row in rows]
        assert times == sorted(times), f"{key}: {label} lines are not in time order"
        assert all(row[1] in ("INFO", "WARNING", "ERROR") for row in rows), f"{key}: {label} has a level a report never prints"


def _offset_delta(offset: str) -> timedelta:
    """The run's stored UTC offset ("+10:30", "-05:00") as a timedelta."""
    body = (offset or "+00:00").strip()
    sign = -1 if body.startswith("-") else 1
    hours, minutes = (int(part) for part in body.lstrip("+-").split(":"))
    return sign * timedelta(hours=hours, minutes=minutes)


def _local_text(iso: str, offset: str) -> str:
    """The stored UTC instant as the report prints it: local wall-clock with the offset."""
    delta = _offset_delta(offset)
    local = datetime.fromisoformat(iso.replace("Z", "+00:00")) + delta
    sign = "-" if delta < timedelta(0) else "+"
    hours, minutes = divmod(abs(int(delta.total_seconds())), 3600)
    return f"{local:%Y-%m-%d %H:%M:%S} {sign}{hours:02d}:{minutes:02d}"


def _local_start_stamp(run: dict[str, Any]) -> str:
    """The run's local start as ``yyyymmdd-HHMMSS``, the stamp the report file name carries."""
    start = datetime.fromisoformat(run["started_at"].replace("Z", "+00:00")) + _offset_delta(run["utc_offset"])
    return start.strftime("%Y%m%d-%H%M%S")


def test_every_example_run_writes_its_test_report(records: dict[str, dict[str, Any]]) -> None:
    """Every example run writes its Test Report at run end - PASS, FAIL or PRETEST_FAILED alike.

    The harness waits on the session's `report_written` signal rather than sleeping (see
    `run_examples.run_schedule`), so this test only ever sees finished runs. Each report is the
    spec's `<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<local start>_<PASS|FAIL>.html` with
    the PDF beside it, and the HTML carries this run's UUT, serial, schedule, result and End Reason
    and all five sections in the order the spec gives them."""
    seen: dict[str, int] = {}  # stem -> how many runs already used it, in run order
    for key, record in records.items():
        run = record["run"]
        written = record["reports"]
        assert len(written) == 1, f"{key}: one report per run, got {written}"
        html_path, pdf_path = written[0]
        assert html_path and pdf_path, f"{key}: report_written emitted {html_path!r}, {pdf_path!r}"
        html, pdf = Path(html_path), Path(pdf_path)
        assert html.exists(), f"{key}: the HTML report is not on disk: {html}"
        assert pdf.exists(), f"{key}: the PDF report is not on disk: {pdf}"
        assert pdf.with_suffix(".html") == html, f"{key}: {pdf.name} is not the PDF of {html.name}"
        assert pdf.stat().st_size, f"{key}: {pdf.name} is empty"

        # Naming: the UUT Type is the directory, the file is UUT Type, serial, local start, result.
        # A report is never overwritten, so runs that start in the same second and end the same way
        # take the first free -2, -3 ... suffix (spec: File names).
        assert html.parent.name == run["uut_type"], f"{key}: {html.parent.name!r} is not the UUT Type folder"
        assert html.parent.parent.name == "reports", f"{key}: {html.parent.parent} is not reports.directory"
        stem = f"{run['uut_type']}_{run['uut_serial']}_{_local_start_stamp(run)}_{run['result']}"
        taken = seen.get(stem, 0) + 1
        seen[stem] = taken
        wanted = f"{stem}.html" if taken == 1 else f"{stem}-{taken}.html"
        assert html.name == wanted, f"{key}: {html.name!r} is not the spec's name {wanted!r}"

        raw = html.read_text(encoding="utf-8")
        parser = _parse_report(raw)
        sections = parser.sections
        for value in (run["uut_type"], run["uut_serial"], run["schedule_name"], run["result"], run["end_reason"]):
            assert value in raw, f"{key}: the report never mentions {value!r}"

        # Cover -> Steps -> Equipment -> Configuration -> Run log.
        assert [name for name in sections if name in REPORT_SECTIONS] == list(REPORT_SECTIONS), \
            f"{key}: sections are {list(sections)}"
        cover = sections["Cover"]
        assert (run["doc_title"] or run["schedule_name"]) in cover, f"{key}: the Cover has no document title"
        assert {"Summary", "Operator"} <= set(sections), f"{key}: the Cover has no Summary or Operator"
        assert list(sections).index("Summary") < list(sections).index("Steps"), \
            f"{key}: the Cover must come before the Steps"
        if run["end_reason"] == "PRETEST_FAILED":
            assert "failed:" in cover, f"{key}: the Cover does not name the failed Pre-test Action"

        # Steps: every step of the expanded tree, disabled ones included, with its Result.
        steps = sections["Steps"]
        for node in record["nodes"]:
            if node["kind"] == "step":
                assert node["step_name"] in steps, f"{key}: step {node['step_name']!r} is not in the Steps table"

        # Failures are on the Cover, and only when there are any.
        failed = [n for n in record["nodes"]
                  if n["kind"] == "step" and n["result"] in ("FAIL", "ERROR", "NOREPORT")]
        assert ("Failures" in sections) == bool(failed), f"{key}: the Failures section does not match the run"
        for node in failed:
            assert node["step_name"] in sections["Failures"], f"{key}: {node['step_name']!r} missing from Failures"

        equipment = sections["Equipment"]
        if record["equipment"]:
            for item in record["equipment"]:
                assert item["name"] in equipment, f"{key}: equipment {item['name']!r} is not in the report"
        else:
            assert "No equipment declared" in equipment, f"{key}: the empty Equipment section says nothing"

        configuration = sections["Configuration"]
        assert run["media_number"] in configuration, f"{key}: the Configuration section omits the release"
        for file in record["run_files"]:
            assert file["path"] in configuration, f"{key}: {file['path']!r} is not listed in Configuration"

        # A report written at run end always carries the Run log, and never the DEBUG lines or the
        # Device trace, which stay in the database.
        assert "Run log left out" not in sections["Run log"], f"{key}: a run-end report left the Run log out"
        _check_run_log(parser, record, key)
        assert "DEBUG" not in raw, f"{key}: DEBUG lines leaked into the report"


def test_every_example_report_cover_shows_the_run_and_its_footer(records: dict[str, dict[str, Any]]) -> None:
    """The Cover's Run details, Schedule and Media Release, the Summary counts, and every page's footer.

    Every value the Cover prints comes from the Test Run record (`test-run-record.md`): the Duration
    is computed from start and end, the schedule file is the top-level instance's path (the `test_run`
    column the record defines is never written by the engine), and the release's name and version come
    from `media_release` row 0. Times are the Station's local time with the offset shown, and the
    footer names the run UUID, the ATE that recorded it and the ATE that generated the report."""
    for key, record in records.items():
        run = record["run"]
        raw = Path(record["reports"][0][0]).read_text(encoding="utf-8")
        parser = _parse_report(raw)
        fields = _cover_fields(parser.rows["Cover"])
        missing = [label for label in COVER_LABELS if label not in fields]
        assert not missing, f"{key}: the Cover is missing {missing}"

        assert fields["UUT Type"] == run["uut_type"] and fields["Serial"] == run["uut_serial"]
        assert fields["Project"] == run["project"], f"{key}: no Project on the Cover"
        assert fields["Station"] == run["station_name"], f"{key}: no Station on the Cover"
        assert fields["Start"] == _local_text(run["started_at"], run["utc_offset"]), f"{key}: Start is {fields['Start']!r}"
        assert fields["End"] == _local_text(run["ended_at"], run["utc_offset"]), f"{key}: End is {fields['End']!r}"
        assert LOCAL_TIME_RE.search(fields["Start"]) and LOCAL_TIME_RE.search(fields["End"]), \
            f"{key}: the times show no UTC offset"
        assert DURATION_RE.fullmatch(fields["Duration"]), f"{key}: Duration is {fields['Duration']!r}, not an elapsed time"
        assert fields["ATE version"] == run["ate_version"], f"{key}: no ATE version on the Cover"
        assert fields["Stop on Fail"] == ("on" if run["stop_on_fail"] else "off"), f"{key}: Stop on Fail is {fields['Stop on Fail']!r}"

        schedule = fields["Schedule"]
        assert run["schedule_name"] in schedule, f"{key}: the Cover omits the schedule name"
        assert (run["schedule_description"] or "") in schedule, f"{key}: the Cover omits the schedule description"
        top = next(i for i in record["schedule_instances"] if i["instance_seq"] == 0)
        assert top["file"] in schedule, f"{key}: the Cover omits the schedule file {top['file']!r}"

        release = fields["Media Release"]
        assert run["media_number"] in release, f"{key}: the Cover omits the Media Number"
        top_release = next(r for r in record["media_releases"] if r["seq"] == 0)
        assert top_release["name"] in release, f"{key}: the Cover omits the release name {top_release['name']!r}"
        assert f"v{top_release['version']}" in release, f"{key}: the Cover omits the release version"

        # Summary: all seven Result counts, zeros included, then retries and Operator overrides.
        counts = [row for row in parser.rows.get("Summary", []) if len(row) == len(SUMMARY_LABELS)]
        assert len(counts) == 2, f"{key}: the Summary has no count table"
        assert counts[0] == list(SUMMARY_LABELS), f"{key}: the Summary counts {counts[0]}"
        tally: dict[str, int] = {label: 0 for label in SUMMARY_LABELS}
        for node in record["nodes"]:
            if node["kind"] != "header":
                tally[node["result"] or "NOTRUN"] += 1
        assert [int(value) for value in counts[1]] == [tally[label] for label in SUMMARY_LABELS], \
            f"{key}: the Summary says {counts[1]}, the record says {[tally[label] for label in SUMMARY_LABELS]}"
        retried = sum(1 for node in record["nodes"]
                      if node["kind"] != "header" and (node["attempt_count"] or 0) > 1)
        overrides = sum(1 for node in record["nodes"]
                        if node["kind"] != "header" and bool(node["default_enabled"]) != bool(node["enabled"]))
        assert f"Retried steps: {retried}" in parser.sections["Summary"], f"{key}: no retried-step count"
        assert f"Operator enable overrides: {overrides}" in parser.sections["Summary"], f"{key}: no override count"

        # The footer every page carries: run UUID, recording ATE, generating ATE, page numbers.
        footer = re.search(r"@bottom-left\s*\{[^}]*content: \"([^\"]*)\"", raw)
        assert footer, f"{key}: the report has no footer box"
        text = footer.group(1)
        assert run["run_uuid"] in text, f"{key}: the footer carries no run UUID"
        assert f"Run recorded by ATE {run['ate_version'] or 'unknown'}" in text, f"{key}: footer says {text!r}"
        assert re.search(r"Report generated \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{2}:\d{2} by ATE \d+\.\d+\.\d+", text), \
            f"{key}: footer says {text!r}"
        assert "counter(page)" in raw and "counter(pages)" in raw, f"{key}: no Page X of Y footer"

        # A run with no organisation has no empty part at the start of the page header.
        header = re.search(r"@top-center\s*\{[^}]*content: \"([^\"]*)\"", raw)
        assert header, f"{key}: the report has no page header box"
        assert not header.group(1).startswith(" ·"), f"{key}: the header starts with a stray separator: {header.group(1)!r}"

        if run["end_reason"] == "PRETEST_FAILED":
            failed = next(a for a in record["actions"] if a["phase"] == "pre" and a["outcome"] == "error")
            instance = next(i for i in record["schedule_instances"] if i["instance_seq"] == failed["instance_seq"])
            banners = re.findall(r'<div class="banner">(.*?)</div>', raw)
            assert any(instance["schedule_name"] in b and failed["error"] in b for b in banners), \
                f"{key}: no banner names the schedule {instance['schedule_name']!r} and its error {failed['error']!r}: {banners}"
            if instance["prefix"]:
                assert any(instance["prefix"] in b for b in banners), f"{key}: the banner omits the include prefix"


def test_every_visible_example_schedule_has_a_green_run(records: dict[str, dict[str, Any]]) -> None:
    """Every schedule the catalog makes visible runs green in CI: at least one PASS/COMPLETED run.

    The schedules that exist to fail keep their failing runs; they just also need a run where the
    Operator disables the failing steps. The list comes from the catalog, so a new example schedule
    with no green run fails here rather than being quietly skipped."""
    green = {record["run"]["schedule_name"] for record in records.values()
             if record["run"]["result"] == "PASS" and record["run"]["end_reason"] == "COMPLETED"}
    missing = set(visible_schedules()) - green
    assert not missing, f"visible schedules with no PASS/COMPLETED run: {sorted(missing)}"



def test_basics_records_metadata_steps_equipment_and_log_levels(records: dict[str, dict[str, Any]]) -> None:
    """Schedule metadata, Step Numbers, an input, Equipment and every log level are recorded."""
    record = records["basics"]
    run = record["run"]
    assert run["schedule_description"] == "Schedule metadata, step numbers and the simple ATE calls"
    assert run["doc_title"] == "Example Media Release - Basics"
    assert run["doc_id"] == "MR-EXAMPLE-BASIC"
    assert run["doc_rev"] == "1.0"
    assert run["stop_on_fail"] == 0, ":setStopOnFail(false) is the run's default"
    assert run["operator"] == "Ada Lovelace"
    assert run["project"] == "Example project"
    assert run["uut_serial"] == "EX-0001"
    assert run["result"] == "PASS"
    assert run["end_reason"] == "COMPLETED"
    assert run["station_name"] == "BENCH-EXAMPLE"

    nodes = record["nodes"]
    assert [node["step_number"] for node in nodes] == ["1.", "2.", "3.", "4.", "5.", "6."]
    assert [node["result"] for node in nodes] == ["PASS", "PASS", "PASS", "NOTRUN", "PASS", "PASS"]

    # Step 4 is disabled by default: it never runs, so it records NOTRUN with no Attempt.
    disabled = nodes[3]
    assert disabled["step_name"] == "Optional check"
    assert (disabled["default_enabled"], disabled["enabled"]) == (0, 0)
    assert disabled["attempt_count"] == 0

    # ATE.run is readable from a step action: the schedule logs the Station, Project, UUT serial,
    # its own name and the Media Release it came from, and reports the Operator it read back.
    first = record["attempts"][0]
    assert first["msg"] == "operator Ada Lovelace"
    assert (first["actual"], first["expected"]) == ("Ada Lovelace", "Ada Lovelace")

    schedule_logs = [(line["level"], line["text"]) for line in record["logs"] if line["source"] == "schedule"]
    assert schedule_logs == [
        ("DEBUG", "reading ATE.run for the run context"),
        ("INFO", "station BENCH-EXAMPLE project Example project uut EX-0001"),
        ("INFO", "schedule Example basics release MR-EXAMPLE"),
        ("DEBUG", "debug detail"),
        ("INFO", "info message"),
        ("WARNING", "warning 42"),
        ("ERROR", "error message"),
        ("INFO", "print lands at INFO"),
    ]

    # ATE.addEquipment declares the Equipment the run used, in declaration order.
    assert record["equipment"] and all(field in record["equipment"][0] for field in ("name", "serial", "connection", "cal_due", "notes"))
    declared = record["equipment"][0]
    assert (declared["name"], declared["serial"], declared["connection"]) == ("Bench power supply", "PSU-123", "GPIB::1")
    assert (declared["cal_due"], declared["notes"]) == ("2026-12-01", "0-60 V, 4 quadrant")

    # Step 6's FLOAT input is declared with range {min = 0.1, max = 10}. The harness answers it
    # through the session, which first refuses the out-of-range value with a ValueError (packet 10a)
    # and then records the valid one.
    assert record["refused_inputs"] == ["input 1: 99.0 refused: Must be 0.1 to 10"]
    answers = [(value["input_index"], value["type"], value["value"]) for value in record["input_values"]]
    assert answers == [(1, "FLOAT", "1.5")]


def test_step_inputs_record_every_type_and_widget(records: dict[str, dict[str, Any]]) -> None:
    """Packet 29b: one step per Step Input type and widget, each answered through the session.

    The harness supplies an out-of-range INTEGER, an out-of-range FLOAT and a pattern-failing STRING
    first: the session refuses each with a ValueError before the valid value is recorded. The POLL is
    clicked from the pump loop only after its step's read loop has read nil a few times, so the loop
    ends on the click, not on the bound."""
    record = records["inputs"]
    run = record["run"]
    assert run["result"] == "PASS", "every step read its input and passed"
    assert run["end_reason"] == "COMPLETED"
    assert run["doc_title"] == "Example Media Release - Step Inputs"
    assert run["doc_id"] == "MR-EXAMPLE-INPUTS"

    steps = [(node["step_number"], node["step_name"], node["result"]) for node in record["nodes"]]
    assert steps == [
        ("1.", "Pick one with RADIOBUT", "PASS"),
        ("2.", "Tick several with CHECKBOX", "PASS"),
        ("3.", "Press a button with PUSHBUT", "PASS"),
        ("4.", "Whole number with a range", "PASS"),
        ("5.", "Decimal with a range", "PASS"),
        ("6.", "Text with a pattern", "PASS"),
        ("7.", "Blocking PROMPT", "PASS"),
        ("8.", "POLL read in a loop", "PASS"),
    ]

    # Every declaration is stored on its step's node row in schedule form: widget, options, range,
    # pattern, prompt, tooltip and orient — the fields the Run screen's widgets are built from.
    declared = [json.loads(node["inputs"]) for node in record["nodes"]]
    assert [(d[0]["type"], d[0].get("input")) for d in declared] == [
        ("ENUM", "RADIOBUT"), ("ENUM", "CHECKBOX"), ("ENUM", "PUSHBUT"),
        ("INTEGER", None), ("FLOAT", None), ("STRING", None), ("PROMPT", None), ("POLL", None),
    ]
    assert [d[0]["enum"] for d in declared[:3]] == [["Green", "Red", "Off"], ["A", "B", "C"], ["RETEST", "SKIP"]]
    assert declared[3][0]["range"] == {"min": 1, "max": 200}
    assert declared[4][0]["range"] == {"min": 0.5, "max": 2.5}
    assert declared[5][0]["pattern"] == "^[A-Z]{2}-[0-9]{4}$"
    assert declared[6][0]["prompt"] == ["CONTINUE"]
    assert declared[7][0]["prompt"] == ["KEEP GOING", "STOP"]
    # tooltip and orient apply to every declared type (test-schedule-format.md), the INTEGER and
    # FLOAT line edits included — they are kept through validation and stored on the node.
    assert [d[0]["orient"] for d in declared] == [
        "HORIZ", "VERT", "HORIZ", "VERT", "HORIZ", "VERT", "HORIZ", "HORIZ"
    ]
    assert [d[0]["tooltip"] for d in declared] == [
        "Which LED the board lights", "Tick every channel to measure", "One press answers the step",
        "Read the current clamp, not the supply", "Measured at the rail header",
        "Two letters, a hyphen, four digits", "Press CONTINUE when the probe is clipped on",
        "Click STOP at any time while this step runs",
    ]

    # The refused answers: the declaration's own range and pattern checks reject them (packet 10a),
    # so nothing is recorded until the Operator answers with a valid value.
    assert record["refused_inputs"] == [
        "input 1: 500 refused: Must be 1 to 200",
        "input 1: 9.0 refused: Must be 0.5 to 2.5",
        "input 1: 'not a part number' refused: Must match ^[A-Z]{2}-[0-9]{4}$",
    ]

    # One recorded row per input, typed from its declaration: the label a RADIOBUT/PUSHBUT picked,
    # the JSON array a CHECKBOX ticked, the numbers and text as Lua prints them, the POLL's click.
    answers = [(value["node_seq"], value["input_index"], value["type"], value["value"])
               for value in record["input_values"]]
    assert answers == [
        (0, 1, "ENUM", "Green"),
        (1, 1, "ENUM", '["A", "B"]'),
        (2, 1, "ENUM", "RETEST"),
        (3, 1, "INTEGER", "42"),
        (4, 1, "FLOAT", "1.25"),
        (5, 1, "STRING", "AB-1234"),
        (6, 1, "PROMPT", "CONTINUE"),
        (7, 1, "POLL", "STOP"),
    ]
    assert record["poll_clicks"] == [(7, 1, "STOP")], "the POLL was clicked exactly once"

    # Each step's Result comes from the value it read back; the POLL reports how many reads its
    # loop needed, which is bounded by the loop itself and never by the run hanging.
    by_seq = {attempt["node_seq"]: attempt for attempt in record["attempts"]}
    assert [by_seq[seq]["msg"] for seq in range(7)] == [
        "led is Green",
        "channels A, B",
        "operator pressed RETEST",
        "current 42 mA",
        "rail 1.25 V",
        "probe AB-1234",
        "probe confirmed with CONTINUE",
    ]
    assert [(by_seq[seq]["actual"], by_seq[seq]["expected"]) for seq in range(7)] == [
        ("Green", "Green"), ("A,B", "A,B"), ("RETEST", "RETEST"),
        ("42", "42"), ("1.25", "1.25"), ("AB-1234", "AB-1234"), ("CONTINUE", "CONTINUE"),
    ]
    poll = by_seq[7]
    assert (poll["pass_fail"], poll["actual"], poll["expected"]) == ("PASS", "STOP", "STOP")
    reads = int(poll["msg"].split(" after ")[1].split(" reads")[0])
    # The harness clicks only after POLL_CLICK_AFTER_SECONDS, and the loop reads every 0.1 s, so at
    # least three nil reads happened before the click; the bound of 8 is never reached.
    assert 3 <= reads <= 8, f"the POLL read loop reported {reads} reads: {poll['msg']!r}"


def test_results_schedule_records_every_result(records: dict[str, dict[str, Any]]) -> None:
    """One step per Result value, including NOREPORT and a Lua ERROR."""
    record = records["results"]
    run = record["run"]
    assert run["result"] == "FAIL", "a FAIL and an ERROR step make the run fail"
    assert run["end_reason"] == "COMPLETED", ":setStopOnFail(false) means no pause"

    # No pause at all: nothing was asked of the Operator, so no Attempt carries a prompt choice.
    assert run["stop_on_fail"] == 0
    assert all(attempt["prompt_choice"] is None for attempt in record["attempts"]), (
        "a run with Stop on Fail off never prompts, so no Attempt records a choice"
    )

    steps = [(node["step_number"], node["result"]) for node in record["nodes"]]
    assert steps == [
        ("1.", "PASS"), ("2.", "PASS"), ("3.", "PASS"), ("4.", "PASS"), ("5.", "FAIL"),
        ("6.", "SKIPPED"), ("7.", "NOTRUN"), ("8.", "FORCED_NOTRUN"),
        ("9.", "NOREPORT"), ("10.", "ERROR"), ("11.", "ERROR"),
    ]

    by_node = {attempt["node_seq"]: attempt for attempt in record["attempts"]}
    assert len(by_node) == len(record["nodes"])

    # Numbers, strings and booleans all reach the record as text — Lua's tostring, so `true` not
    # Python's `True`, and Lua's float repr rather than Python's.
    assert (by_node[0]["actual"], by_node[0]["expected"]) == ("Green", "Green")
    assert (by_node[1]["actual"], by_node[1]["expected"]) == ("42", "42")
    assert (by_node[2]["actual"], by_node[2]["expected"]) == ("true", "true")

    # Two reports in one step: the last one is the step's result.
    assert (by_node[3]["pass_fail"], by_node[3]["msg"]) == ("PASS", "second reading")

    # The step that never reported has no message, and ATE logged the warning at WARNING from "ate".
    assert by_node[8]["pass_fail"] == "NOREPORT" and by_node[8]["msg"] is None
    assert any(line["level"] == "WARNING" and line["source"] == "ate" and "never reported" in line["text"]
               for line in record["logs"])

    # The step that raised: ERROR with the Lua message, and a traceback in the log.
    assert by_node[9]["pass_fail"] == "ERROR"
    assert "example: the instrument did not answer" in by_node[9]["msg"]
    error_line = next(line for line in record["logs"]
                      if line["level"] == "ERROR" and "stack traceback" in line["text"])
    assert "report_results.tslua" in error_line["text"]


def test_stop_on_fail_continues_when_the_operator_continues(records: dict[str, dict[str, Any]]) -> None:
    """Continue at both prompts: the run carries on, still FAIL, End Reason COMPLETED."""
    record = records["continue"]
    run = record["run"]
    assert run["stop_on_fail"] == 1, ":setStopOnFail(true) is the run's default"
    assert run["result"] == "FAIL"
    assert run["end_reason"] == "COMPLETED"
    assert [(attempt["pass_fail"], attempt["prompt_choice"]) for attempt in record["attempts"]] == [
        ("FAIL", "CONTINUE"), ("PASS", None), ("ERROR", "CONTINUE"), ("PASS", None)
    ]
    assert [(node["step_number"], node["result"]) for node in record["nodes"]] == [
        ("1.", "FAIL"), ("2.", "PASS"), ("3.", "ERROR"), ("4.", "PASS")
    ]


def test_stop_on_fail_aborts_when_the_operator_aborts(records: dict[str, dict[str, Any]]) -> None:
    """Abort at the first prompt: the later steps never run and the End Reason is STOPPED_ON_FAIL."""
    record = records["abort"]
    run = record["run"]
    assert run["result"] == "FAIL"
    assert run["end_reason"] == "STOPPED_ON_FAIL"
    assert record["attempts"][0]["prompt_choice"] == "ABORT"
    nodes = [(node["step_number"], node["result"], node["attempt_count"]) for node in record["nodes"]]
    assert nodes == [("1.", "FAIL", 1), ("2.", "NOTRUN", 0), ("3.", "NOTRUN", 0), ("4.", "NOTRUN", 0)]
    assert all(a["prompt_choice"] is None for a in record["attempts"][1:]), "the run stopped at step 1"


def test_the_operator_can_enable_a_step_disabled_by_default(records: dict[str, dict[str, Any]]) -> None:
    """Packet 11a: the same schedule, with step 4 toggled on before the run — it runs and passes."""
    record = records["basics_enabled"]
    by_number = {node["step_number"]: node for node in record["nodes"]}
    optional = by_number["4."]
    assert optional["step_name"] == "Optional check"
    assert (optional["default_enabled"], optional["enabled"]) == (0, 1)
    assert (optional["result"], optional["attempt_count"]) == ("PASS", 1)
    assert any(a["msg"] == "only runs when enabled" for a in record["attempts"])
    assert record["run"]["result"] == "PASS"


def test_flow_retries_a_failed_step_under_stop_on_fail(records: dict[str, dict[str, Any]]) -> None:
    """Packet 29c: Attempt 1 FAILs, the Operator's Retry gives Attempt 2, and the step's Result is
    its last Attempt's. The attempt count lives in a required module: ATE gives a step no way to
    read its own Attempt number, so the schedule keeps the counter."""
    record = records["flow_retry"]
    run = record["run"]
    assert run["stop_on_fail"] == 1, ":setStopOnFail(true) is this schedule's default"
    assert run["end_reason"] == "COMPLETED", "a Retry that passes ends the run normally"
    assert run["result"] == "PASS", "no step's last Attempt failed"

    step = record["nodes"][0]
    assert (step["step_number"], step["step_name"]) == ("1.", "Retry this one")
    assert (step["attempt_count"], step["result"]) == (2, "PASS")

    attempts = [a for a in record["attempts"] if a["node_seq"] == 0]
    assert [(a["attempt_no"], a["pass_fail"], a["prompt_choice"]) for a in attempts] == [
        (1, "FAIL", "RETRY"), (2, "PASS", None)
    ]
    assert [a["msg"] for a in attempts] == ["attempt 1 joint is cold", "attempt 2 joint is warm"]
    assert [(a["actual"], a["expected"]) for a in attempts] == [("0.2", "2.4"), ("2.4", "2.4")]

    # Every other step in the tree ran exactly once, so the pause happened at step 1 only.
    assert all(a["attempt_no"] == 1 for a in record["attempts"] if a["node_seq"] != 0)


def test_flow_with_continue_on_fail_never_prompts(records: dict[str, dict[str, Any]]) -> None:
    """The same schedule with the Operator turning Stop on Fail off: no pause, no Retry, and step 1
    stays FAIL. The End Reason is still COMPLETED because the run walked every step."""
    record = records["flow_continue"]
    run = record["run"]
    assert run["stop_on_fail"] == 0, "the Operator's choice overrides :setStopOnFail(true)"
    assert run["end_reason"] == "COMPLETED", "continue on fail never stops the run"
    assert run["result"] == "FAIL", "step 1 failed on its only Attempt"
    assert record["unexpected_prompts"] == [], "a run with Stop on Fail off never pauses"
    assert all(a["prompt_choice"] is None for a in record["attempts"])

    step = record["nodes"][0]
    assert (step["attempt_count"], step["result"]) == (1, "FAIL")
    by_node = {a["node_seq"]: a for a in record["attempts"]}
    assert by_node[0]["msg"] == "attempt 1 joint is cold"

    # A Post-test Action is called with the run's overall result: FAIL here, PASS in the retry run.
    assert any(line["text"] == "top-level post-test sees FAIL" for line in record["logs"])


def test_pre_and_post_actions_run_depth_first_across_the_tree(records: dict[str, dict[str, Any]]) -> None:
    """Every Pre-test Action runs before the first step, depth first with parents first, and the
    Post-test Actions run afterwards in exactly the reverse order (test-schedule-format.md).

    The order is only provable because flow.tslua has an enabled include (step 6) AFTER the nested
    chain: depth first walks nest A -> B -> C before channel 4's Actions, while a walk that takes
    every sibling include before going down - breadth first - runs channel 4 before nest B and C.
    Asserted by schedule name and keyvals, not by the instance numbering that happens to match DFS."""
    for key in ("flow_retry", "flow_continue"):
        record = records[key]
        actions = record["actions"]
        pre = [a for a in actions if a["phase"] == "pre"]
        post = [a for a in actions if a["phase"] == "post"]
        assert all(a["outcome"] == "ok" for a in actions)
        assert len(pre) == len(post) == 7, "the top level plus six Sub-schedule instances that run steps"
        assert [a["run_order"] for a in pre] == list(range(7))
        assert [a["run_order"] for a in post] == list(range(7))

        by_instance = {i["instance_seq"]: (i["schedule_name"], json.loads(i["keyvals"]).get("channel"))
                       for i in record["schedule_instances"]}
        depth_first = [
            ("Flow and retry", None),   # the parent, before any of its includes
            ("Channel check", 1),       # step 2, then step 3: includes in step order
            ("Channel check", 2),
            ("Nested A", None),         # step 4 ...
            ("Nested B", None),         # ... walked all the way down ...
            ("Nested C", None),         # ... before the include that comes after it
            ("Channel check", 4),       # step 6
        ]
        assert [by_instance[a["instance_seq"]] for a in pre] == depth_first
        assert [by_instance[a["instance_seq"]] for a in post] == list(reversed(depth_first))

        # The Sub-schedule under the disabled header runs no step at all, so both of its Actions are
        # skipped even though its instance is recorded.
        assert {i["instance_seq"] for i in record["schedule_instances"]} == set(range(8))
        assert all(a["instance_seq"] != 6 for a in actions), "instance 6 is disabled end to end"

        logs = record["logs"]
        phase_lines = [line for line in logs if line["action_seq"] is not None]
        assert [line["text"] for line in phase_lines[:7]] == [
            "top-level pre-test: calibration ready", "pre-test for channel 1", "pre-test for channel 2",
            "pre-test of nest A", "pre-test of nest B", "pre-test of nest C", "pre-test for channel 4",
        ]
        assert all("(" not in line["text"] for line in phase_lines[:7]), (
            "a Pre-test Action takes no argument: nothing has run yet, so there is no result to pass"
        )
        assert [line["text"] for line in phase_lines[7:]] == [
            f"post-test for channel 4 ({record['run']['result']})",
            "post-test of nest C", "post-test of nest B", "post-test of nest A",
            f"post-test for channel 2 ({record['run']['result']})",
            f"post-test for channel 1 ({record['run']['result']})",
            f"top-level post-test sees {record['run']['result']}",
        ]

        first_step_line = min(i for i, line in enumerate(logs) if line["node_seq"] is not None)
        last_pre = max(i for i, line in enumerate(logs)
                       if line["action_seq"] is not None and "pre-test" in line["text"])
        first_post = min(i for i, line in enumerate(logs)
                        if line["action_seq"] is not None and "post-test" in line["text"])
        assert last_pre < first_step_line < first_post, (
            "every Pre-test Action ran before the first step, and every Post-test Action after the last"
        )


def test_a_hidden_sub_schedule_included_more_than_once_gets_its_own_keyvals_and_prefix(records: dict[str, dict[str, Any]]) -> None:
    """The expanded tree, its headers, and the per-instance keyvals and prefixes of one hidden file."""
    record = records["flow_retry"]
    tree = [(node["step_number"], node["kind"], node["step_name"], node["result"]) for node in record["nodes"]]
    assert tree == [
        ("1.", "step", "Retry this one", "PASS"),
        ("2.", "header", "Channel 1", None),
        ("2.1.", "step", "Shared state 1", "PASS"),
        ("2.2.", "step", "Output off 1", "PASS"),
        ("3.", "header", "Channel 2", None),
        ("3.1.", "step", "Shared state 2", "PASS"),
        ("3.2.", "step", "Output off 2", "PASS"),
        ("4.", "header", "Nested chain", None),
        ("4.1.", "step", "A step", "PASS"),
        ("4.2.", "header", "B header", None),
        ("4.2.1.", "step", "B step", "PASS"),
        ("4.2.2.", "header", "C header", None),
        ("4.2.2.1.", "step", "C step", "PASS"),
        ("5.", "header", "Channel not fitted", None),
        ("5.1.", "step", "Shared state 3", "NOTRUN"),
        ("5.2.", "step", "Output off 3", "NOTRUN"),
        ("6.", "header", "Channel 4", None),
        ("6.1.", "step", "Shared state 4", "PASS"),
        ("6.2.", "step", "Output off 4", "PASS"),
        ("7.", "step", "Device params, not opened", "PASS"),
    ]
    # A header is not a Test Step: no Result, no Attempt, and its number comes from the parent's
    # numbering, not its own setPrefix.
    assert all(node["attempt_count"] == 0 for node in record["nodes"] if node["kind"] == "header")

    included = [i for i in record["schedule_instances"] if i["file"] == "sub_channel.tslua"]
    assert [(i["prefix"], json.loads(i["keyvals"])) for i in included] == [
        ("2.", {"channel": 1, "label": "left rail"}),
        ("3.", {"channel": 2, "label": "right rail"}),
        ("5.", {"channel": 3, "label": "not fitted"}),
        ("6.", {"channel": 4, "label": "rear rail"}),
    ]

    # The keyvals show up in the step's own recorded Message, and the prefixes in its Step Number.
    by_node = {attempt["node_seq"]: attempt for attempt in record["attempts"]}
    assert by_node[2]["msg"] == "left rail channel 1 is shared call 1"
    assert by_node[5]["msg"] == "right rail channel 2 is shared call 2"
    assert by_node[17]["msg"] == "rear rail channel 4 is shared call 3"
    assert [(node["step_number"], node["instance_seq"]) for node in record["nodes"]
            if node["step_name"].startswith("Shared state")] == [("2.1.", 1), ("3.1.", 2), ("5.1.", 6), ("6.1.", 7)]


def test_a_nested_include_expands_and_a_disabled_header_runs_nothing(records: dict[str, dict[str, Any]]) -> None:
    """A includes B includes C, prefixes stack outside in, and setEnable on a header forces every
    step beneath it off - which records NOTRUN with no Attempt."""
    record = records["flow_retry"]
    nested = [(node["step_number"], node["step_name"], node["kind"], node["result"])
              for node in record["nodes"] if node["step_number"].startswith("4.")]
    assert nested == [
        ("4.", "Nested chain", "header", None),
        ("4.1.", "A step", "step", "PASS"),
        ("4.2.", "B header", "header", None),
        ("4.2.1.", "B step", "step", "PASS"),
        ("4.2.2.", "C header", "header", None),
        ("4.2.2.1.", "C step", "step", "PASS"),
    ]

    header = next(node for node in record["nodes"] if node["step_number"] == "5.")
    assert (header["step_name"], header["default_enabled"], header["enabled"]) == ("Channel not fitted", 0, 0)
    beneath = [node for node in record["nodes"]
               if node["step_number"].startswith("5.") and node["kind"] == "step"]
    assert [(node["result"], node["attempt_count"], node["default_enabled"], node["enabled"])
            for node in beneath] == [("NOTRUN", 0, 0, 0), ("NOTRUN", 0, 0, 0)]


def test_required_modules_share_one_state_across_the_includes(records: dict[str, dict[str, Any]]) -> None:
    """require is cached for the Test Run, so flow.tslua, sub_channel.tslua and nest_a.tslua see one
    lib.attempts table, and flow.tslua plus every live channel include see one example_helper."""
    record = records["flow_retry"]
    by_node = {attempt["node_seq"]: attempt for attempt in record["attempts"]}
    assert (by_node[2]["actual"], by_node[5]["actual"], by_node[17]["actual"]) == ("1", "2", "3"), (
        "one counter across the three includes that ran"
    )
    assert (by_node[2]["expected"], by_node[5]["expected"], by_node[17]["expected"]) == ("1", "2", "4")
    # nest_a.tslua reads the same table the top-level step incremented twice through its Retry.
    assert (by_node[8]["actual"], by_node[8]["expected"]) == ("2", "2")

    # In the run with no Retry the same module still holds flow.tslua's counter, at 1.
    other = {attempt["node_seq"]: attempt for attempt in records["flow_continue"]["attempts"]}
    assert other[8]["actual"] == "1"

    # example_helper is required by two schedules, and its use count runs across the whole run: each
    # channel include that ran used the same table, and flow.tslua's own step is use 4. The include
    # under the disabled header ran no step, so it never counted.
    helper_lines = [line["text"] for line in record["logs"] if " probe at 127.0.0.1:" in line["text"]]
    assert helper_lines == [
        "channel 1 probe at 127.0.0.1:5021 (helper 1.0, use 1)",
        "channel 2 probe at 127.0.0.1:5022 (helper 1.0, use 2)",
        "channel 4 probe at 127.0.0.1:5024 (helper 1.0, use 3)",
    ]
    assert any(line["text"] == "SCPI instrument at 127.0.0.1:5025 (helper 1.0, use 4)"
              for line in record["logs"]), "the same helper table, counted once for the whole run"

    used = {file["path"]: file for file in record["run_files"]}
    assert {"flow.tslua", "sub_channel.tslua", "nest_a.tslua", "nest_b.tslua", "nest_c.tslua",
            "lib/attempts.lua", "modules/helper.lua"} <= set(used)
    bundled = used["ate.devices"]
    assert bundled["kind"] == "ate-module" and bundled["expected_sha256"] is None
    assert len(bundled["computed_sha256"]) == 64, "ATE records the checksum of the bundled module it used"
    assert bundled["mismatch"] == 0


def test_every_device_type_is_opened_written_read_flushed_and_closed(records: dict[str, dict[str, Any]]) -> None:
    """Packet 29d: TCP, UDP, SERIAL and VISA each run against the peers the harness started.

    The Device schedule's own steps check the outcomes, so every one of them reports PASS: the reply
    matches what the peer answered, the partial reads are partial, the timed-out read is recovered by
    the handler `open` gave, and the flushes discard exactly what the buffer held. The `onError`
    that returns nil is disabled by default, so the green run leaves it NOTRUN."""
    record = records["devices"]
    run = record["run"]
    assert (run["result"], run["end_reason"]) == ("PASS", "COMPLETED")

    steps = [n for n in record["nodes"] if n["kind"] == "step"]
    expected = [("1.", "Pre-test Device still open", "PASS")]
    for index, kind in enumerate(DEVICE_KINDS, start=2):
        for number, suffix in enumerate(("open, write and read", "partial read", "read times out",
                                        "flush", "close"), start=1):
            expected.append((f"{index}.{number}.", f"{kind} {suffix}", "PASS"))
    expected += [("6.", "Close the Pre-test Device", "PASS"), ("7.", "Read a closed Device", "NOTRUN")]
    assert [(n["step_number"], n["step_name"], n["result"]) for n in steps] == expected

    closed_by_schedule = next(n for n in steps if n["step_name"] == "Read a closed Device")
    assert (closed_by_schedule["default_enabled"], closed_by_schedule["enabled"],
            closed_by_schedule["attempt_count"]) == (0, 0, 0)

    peers = record["peers"]
    assert peers["scpiConnections"] == 4, "the Pre-test Device plus the TCP, SERIAL and VISA Devices"
    assert peers["scpiCommands"] == SCPI_COMMANDS, "the schedule's commands, in include order"
    assert peers["udpReceived"] == [b"PING", b"a datagram longer than the size asked for",
                                   b"DROP:no reply"]
    assert peers["udpEchoes"] == [b"ECHO:PING", b"ECHO:a datagram longer than the size asked for"]

    # ATE closes nothing the schedule closed itself: the run ends with no Device left open.
    assert not [line for line in record["logs"] if "left open at end of run" in line["text"]]


def test_every_device_is_traced_and_declared_as_equipment(records: dict[str, dict[str, Any]]) -> None:
    """With `deviceTrace.enabled`, every Device operation is recorded and every Device is Equipment.

    Tracing is on in the harness's config, so each open, write, read and close writes one DEBUG line
    with source `device` naming the Device Type and its address (ate-table-reference.md, "Tracing").
    `ATE.addEquipment` declares the instruments the run used: ATE does not link Equipment rows to
    Devices, so the schedule has to describe each one itself."""
    record = records["devices"]
    trace = [line for line in record["logs"]
             if line["source"] == "device" and line["level"] == "DEBUG"]
    for kind in DEVICE_KINDS:
        for operation in ("open", "write", "read", "close"):
            assert any(line["text"].startswith(f"{operation} {kind} ") for line in trace), \
                f"no {operation} traced for a {kind} Device"

    # The Device the Pre-test Action opened is traced from the Action, before any step ran.
    pre_open = next(line for line in trace if line["text"].startswith("open TCP "))
    assert pre_open["node_seq"] is None and pre_open["action_seq"] is not None
    assert any(line["node_seq"] == 0 for line in trace if line["text"].startswith("write TCP "))

    idn = REPLIES["*IDN?"].rstrip("\n")
    assert any(f'{len(idn)} bytes "{idn}"' in line["text"] for line in trace)
    # MULTI?'s reply arrives as one chunk: the schedule reads two of its lines and discards the
    # third with flush. Only open, read, write and close are traced, so flush has no line of its own
    # - the step's reported count is what records it.
    assert any('"chan=1"' in line["text"] for line in trace)
    assert any('"chan=2"' in line["text"] for line in trace)

    # The per-call overrides are visible in the trace: the write sends CRLF even though the Device
    # was opened with LF, and the stream partial reads waited their 0.4 s override rather than the
    # 2.0 s the Device was opened with. UDP's partial read is a `size` truncation, so it returns at
    # once and waits nothing.
    for kind in ("TCP", "SERIAL", "VISA"):
        assert any(line["text"].startswith(f"write {kind} ") and '7 bytes "*IDN?\\r\\n"' in line["text"]
                   for line in trace), f"no CRLF write traced for {kind}"
    stream_waits = [_ms(line["text"]) for line in trace
                    if " partial (" in line["text"] and "UDP" not in line["text"]]
    assert len(stream_waits) == 3 and all(300 <= wait <= 900 for wait in stream_waits), stream_waits

    equipment = record["equipment"]
    assert len(equipment) == 5, "the Pre-test Device plus one row per Device Type"
    assert all(field in row for row in equipment for field in ("name", "serial", "connection"))
    connections = [row["connection"] for row in equipment]
    for kind in DEVICE_KINDS:
        assert any(connection.startswith(f"{kind} ") for connection in connections), \
            f"no Equipment row describes a {kind} Device"
    assert equipment[0]["cal_due"] == "2026-01-01"


def test_partial_reads_and_timeouts_record_what_their_results_say(records: dict[str, dict[str, Any]]) -> None:
    """A partial read logs a WARNING and is not a failure; a read that times out with nothing is."""
    record = records["devices"]
    logs = record["logs"]
    trace = [line["text"] for line in logs if line["source"] == "device"]

    # Three stream Devices timed out mid-bytes; UDP's partial read is `size` cutting a datagram
    # short, which the host says is truncation rather than a timeout.
    assert sum("timed out after 38 byte(s)" in line["text"] for line in logs
               if line["source"] == "ate") == 3
    assert sum("datagram truncated to 10 byte(s)" in line["text"] for line in logs
               if line["source"] == "ate") == 1
    assert sum(" partial (" in text for text in trace) == 4

    # The read that got nothing at all is a Device failure: its trace line carries the error, and the
    # handler given to `open` is the fallback that catches it. Each waited the 0.3 s its call asked
    # for, not the 2.0 s the Device was opened with.
    assert sum('0 bytes "" err "timeout"' in text for text in trace) == 4
    timeout_waits = [_ms(text) for text in trace if 'err "timeout"' in text]
    assert len(timeout_waits) == 4 and all(200 <= wait <= 700 for wait in timeout_waits), timeout_waits
    assert sorted(line["text"] for line in logs if "device error: timeout" in line["text"]) == [
        "SERIAL device error: timeout", "TCP device error: timeout", "UDP device error: timeout",
        "VISA device error: timeout",
    ]

    partial = sorted((attempt["actual"], attempt["expected"]) for attempt in record["attempts"]
                     if " with no terminator" in attempt["msg"])
    assert partial == [("10", "10"), ("38", "38"), ("38", "38"), ("38", "38")]

    # Flush discards what the buffer held: 7 bytes for TCP and SERIAL (MULTI?'s unread third line),
    # 0 for UDP (every echo was read) and for VISA, which has no receive buffer.
    flushed = sorted(attempt["actual"] for attempt in record["attempts"]
                     if attempt["msg"].startswith("flush discarded"))
    assert flushed == ["0", "0", str(len(FLUSHED_BYTES)), str(len(FLUSHED_BYTES))]


def test_an_on_error_that_returns_nil_raises_and_records_error(records: dict[str, dict[str, Any]]) -> None:
    """The step disabled by default, run with the Operator enabling it.

    Reading a Device that is already closed is a Device failure, so it goes through `onError`. This
    handler returns nil, so the call raises, the step records Result.ERROR, and the schedule's own
    `ATE.report` never runs."""
    record = records["devices_nil_onerror"]
    step = next(n for n in record["nodes"] if n["step_name"] == "Read a closed Device")
    assert (step["default_enabled"], step["enabled"]) == (0, 1)
    assert step["result"] == "ERROR" and step["attempt_count"] == 1

    raised = next(line for line in record["logs"]
                  if line["level"] == "ERROR" and "ATE.read" in line["text"])
    assert "device closed" in raised["text"], "the Device was closed by step 6"
    assert not any(attempt["msg"] == "never reached" for attempt in record["attempts"])
    assert record["run"]["result"] == "FAIL"
    assert record["unexpected_prompts"] == [], "this schedule stops on fail off, so no pause"


def test_a_per_call_read_term_ends_only_that_read(records: dict[str, dict[str, Any]]) -> None:
    """The header's promise: a `readTerm` on one call, not on the Device.

    The Pre-test Device is opened with the builder's default `\\n`. Step 1 reads once with the
    per-call terminator "3", which cuts the peer's `1.234\\n` answer after "1.2" and leaves the rest
    in the receive buffer for the next read, which uses the Device's own terminator. The trace shows
    both reads, so the override demonstrably applied to one call only."""
    record = records["devices"]
    attempt = next(a for a in record["attempts"] if a["msg"].startswith('readTerm "3" gave'))
    assert (attempt["pass_fail"], attempt["actual"], attempt["expected"]) == ("PASS", "4", "4")
    assert attempt["msg"] == 'readTerm "3" gave 1.2, the rest 4', attempt["msg"]

    trace = [line["text"] for line in record["logs"] if line["source"] == "device"]
    assert any(t.startswith("read TCP ") and '3 bytes "1.2"' in t for t in trace), "the readTerm read is traced"
    assert any(t.startswith("read TCP ") and '1 bytes "4"' in t for t in trace), "the rest of the reply is read next"


def test_a_second_close_does_nothing_and_traces_nothing(records: dict[str, dict[str, Any]]) -> None:
    """Device lifetime, pinned: a Device closed twice returns `{ok = true` both times, and only the
    close that actually happened is traced.

    Step 6 calls `ATE.close` on the Pre-test Device twice, so the schedule makes six close calls
    across five Devices while the trace carries five close lines."""
    record = records["devices"]
    step = next(n for n in record["nodes"] if n["step_name"] == "Close the Pre-test Device")
    assert (step["result"], step["attempt_count"]) == ("PASS", 1)
    (attempt,) = [a for a in record["attempts"] if a["msg"].startswith("discarded ")]
    assert attempt["pass_fail"] == "PASS", "the second close is a no-op that still reports success"
    assert (attempt["actual"], attempt["expected"]) == ("true/true", "true/true"), \
        "both ATE.close calls returned {ok = true}"
    closes = [line["text"] for line in record["logs"]
              if line["source"] == "device" and line["level"] == "DEBUG" and line["text"].startswith("close ")]
    assert len(closes) == 5, f"one traced close per Device, none for the no-op second close: {closes}"
    assert sum(text.startswith("close TCP ") for text in closes) == 2, "the Pre-test Device and the TCP include"


def test_the_device_schedule_fails_cleanly_when_its_scpi_peer_is_down(records: dict[str, dict[str, Any]]) -> None:
    """Packet 29d review: the same schedule with nothing listening on the SCPI port.

    The Pre-test open fails, so `onError` runs; it logs the reason and returns nil, which means no
    recovery, so `ATE.open` raises. A Pre-test failure ends the run on its own: `PRETEST_FAILED`,
    result `FAIL`, every step `NOTRUN`, nothing opened, and no Python traceback in the log - the
    failure is reported as a Device failure, not as an engine crash."""
    record = records["devices_peer_down"]
    logs = record["logs"]
    assert not any("Traceback" in line["text"] for line in logs), "a failed open must not crash the engine"
    assert not any("AttributeError" in line["text"] for line in logs)

    assert (record["run"]["result"], record["run"]["end_reason"]) == ("FAIL", "PRETEST_FAILED")
    pre = [a for a in record["actions"] if a["phase"] == "pre"]
    assert pre and pre[0]["outcome"] == "error", "the Pre-test Action raised, and is recorded as such"

    steps = [n for n in record["nodes"] if n["kind"] == "step"]
    assert all(n["result"] == "NOTRUN" for n in steps), "the run ends before any step runs"
    assert not any(n["attempt_count"] for n in steps)

    assert any("SCPI peer unreachable" in line["text"] for line in logs), "the handler logged why"
    assert any("ATE.open" in line["text"] for line in logs), "and the raise names the call and the Device"

    peers = record["peers"]
    assert peers["scpiConnections"] == 0 and peers["scpiCommands"] == [], "nothing reached the SCPI peer"
    assert peers["udpReceived"] == [] and peers["udpEchoes"] == [], "no step ran, so no Device was opened"
