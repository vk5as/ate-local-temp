"""Packet 29e rework: the Cover and footer fields the report was leaving out.

Driven through RunEngine exactly as `test_report_21a.py` does, then rendered with
:func:`render_report`: the Cover's Duration, the top-level schedule file and the Media Release name
and version, the footer every page carries (run UUID, "Run recorded by ATE x.y · Report generated
<local time> by ATE a.b"), a page header with no stray separator when `reports.organisation` is
empty, and a `PRETEST_FAILED` banner that names the schedule whose Pre-test Action failed.
"""

from __future__ import annotations

import re
from pathlib import Path

from ate._version import __version__
from ate.config import Config
from ate.engine.run_engine import RunEngine
from ate.report.html import _duration, render_report
from ate.storage.store import RunStore

from ._fixtures import DOC_FIELDS, PRE_ERROR_POST_OK, _drive
from tests._fixtures import scratch_directory

#: A Pre-test Action in an included Sub-schedule fails: the Cover must name that schedule.
INCLUDE_PRE_FAIL = """\
return ScheduleParams("Outer schedule", "an include whose Pre-test Action fails")
  :setSchedule({
    StepParams("the include"):setStepNo("1."):setPrefix("S"):setInclude("sub.tslua", {}),
  })
"""

SUB_PRE_FAIL = """\
return ScheduleParams("Inner schedule", "the Sub-schedule whose Pre-test Action raises")
  :setPreTestAction(function() error("inner pretest boom") end)
  :setSchedule({
    StepParams("inner step"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "never reached") end),
  })
"""


def _cfg(**overrides) -> Config:
    """A report config (path is required but unused by the report); apply overrides."""
    return Config(path=Path("/tmp/ate-report-test"), **overrides)


def _record(app: object, source: str = DOC_FIELDS, extra_files: dict[str, str] | None = None) -> tuple[RunStore, str, Path]:
    """Drive one schedule to completion; return (store, run_uuid, schedule path)."""
    captured: dict[str, str] = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with scratch_directory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8", newline="\n")
        for name, text in (extra_files or {}).items():
            (Path(td) / name).write_text(text, encoding="utf-8", newline="\n")
        engine = RunEngine(
            store,
            str(schedule_path),
            operator="Alice",
            uut_type="PowerSupply",
            uut_serial="PS7",
            station_name="BENCH-03",
            utc_offset="+10:30",
            ate_version="7.7.7",  # the version stamped on this run, distinct from the one rendering
        )
        engine.run_uuid_reported.connect(_capture)
        _drive(engine, app)

    return store, captured["run_uuid"], schedule_path


def _cover_field(html: str, label: str) -> str:
    """The Cover's Run-details cell for `label`, as written in the template."""
    match = re.search(rf"<tr><th>{label}</th><td>(.*?)</td></tr>", html, re.S)
    assert match, f"the Cover has no {label} row"
    return match.group(1)


def test_duration_computed_from_start_and_end(app: object) -> None:
    """A finished run prints its duration; an unfinished one prints nothing."""
    assert _duration("2026-10-17T14:00:00Z", "2026-10-17T15:02:03Z") == "1 h 2 min 3 s"
    assert _duration("2026-10-17T14:00:00Z", "2026-10-17T14:00:45Z") == "45 s"
    assert _duration("2026-10-17T14:00:00Z", "2026-10-17T14:03:00Z") == "3 min 0 s"
    assert _duration("2026-10-17T14:00:00Z", None) is None, "a run with no end has no duration"

    store, ru, _ = _record(app)
    html = render_report(store, ru, _cfg(reports_organisation="ACME"))
    duration = _cover_field(html, "Duration")
    assert duration != "—", "the Cover printed a dash for a run that ended"
    assert re.fullmatch(r"(\d+ h \d+ min \d+ s|\d+ min \d+ s|\d+ s)", duration), f"odd duration {duration!r}"
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \+10:30", html), "times show the offset"


def test_cover_names_the_schedule_file_and_the_media_release(app: object) -> None:
    """The Cover's Schedule row carries the description and file; the release row name and version."""
    store, ru, schedule_path = _record(app)
    store.insert_media_release(ru, seq=0, media_number="MR-29E", name="Report rework release", version="1.2.3")

    html = render_report(store, ru, _cfg(reports_organisation="ACME"))
    schedule = _cover_field(html, "Schedule")
    assert "Doc fields schedule" in schedule, "the Cover omits the top-level schedule name"
    assert "a step with full document metadata" in schedule, "the Cover omits the description"
    # The instance table stores the file as the schedule loader resolved it (a name or a path), and
    # the test_run column the spec lists is never written by the engine.
    assert schedule_path.name in schedule, "the Cover omits the schedule file"

    release = _cover_field(html, "Media Release")
    assert "MR-29E" in release and "Report rework release" in release and "v1.2.3" in release, release


def test_footer_names_the_run_uuid_and_both_ate_versions(app: object) -> None:
    """Every page's footer: the run UUID, the recording ATE version and the rendering ATE version."""
    store, ru, _ = _record(app)
    html = render_report(store, ru, _cfg(reports_organisation="ACME"))

    footer = re.search(r"@bottom-left\s*\{[^}]*content: \"([^\"]*)\"", html)
    assert footer, "the template has no footer content box"
    text = footer.group(1)
    assert ru in text, "the footer carries no run UUID"
    assert "Run recorded by ATE 7.7.7 · Report generated" in text, f"the footer does not name the ATE that recorded the run: {text!r}"
    assert f"by ATE {__version__}" in text, "the footer does not name the ATE rendering the report"
    assert re.search(r"Report generated \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \+10:30", text), text
    assert "counter(page)" in html and "counter(pages)" in html, "Page X of Y is missing"


def test_page_header_has_no_leading_separator_without_an_organisation(app: object) -> None:
    """An empty reports.organisation leaves no stray "· " at the start of the header."""
    store, ru, _ = _record(app)

    html = render_report(store, ru, _cfg(reports_organisation="ACME Test Labs"))
    assert '@top-center { content: "ACME Test Labs · Quarterly Power Audit · PS-AUD-2026-Q4 · rev 3"' in html

    html = render_report(store, ru, _cfg(reports_organisation=""))
    assert '@top-center { content: "Quarterly Power Audit · PS-AUD-2026-Q4 · rev 3"' in html
    assert 'content: " ·' not in html, "the header starts with a stray separator"


def test_pretest_failure_banner_names_the_schedule_that_failed(app: object) -> None:
    """PRETEST_FAILED names the schedule whose Pre-test Action errored, and its message."""
    store, ru, _ = _record(app, PRE_ERROR_POST_OK)
    html = render_report(store, ru, _cfg(reports_organisation="ACME"))
    assert "Pre error post ok schedule" in html, "the banner names no schedule"
    assert "boom in pretest" in html, "the banner carries no error message"

    # An Action inside an include is named by that include's schedule and its prefix.
    store, ru, _ = _record(app, INCLUDE_PRE_FAIL, {"sub.tslua": SUB_PRE_FAIL})
    html = render_report(store, ru, _cfg(reports_organisation="ACME"))
    row = store.get_test_run(ru)
    assert row["end_reason"] == "PRETEST_FAILED", f"the run ended {row['end_reason']}"
    banner = re.search(r"<div class=\"banner\">([^<]*)</div>", html)
    assert banner, "no banner on the Cover"
    assert "Inner schedule" in banner.group(1) and "Outer schedule" not in banner.group(1), banner.group(1)
    assert "S" in banner.group(1), "the include prefix is missing from the banner"
    assert "inner pretest boom" in html, "the banner carries no error message"
