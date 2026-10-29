"""Packet 21c seam-1 tests: the Equipment, Configuration and Run log report sections.

Real `.atedef` trees and `.tslua` schedules on disk, runs end to end through
:class:`HeadlessRunSession`, rendered with :func:`render_report` and read back with the standard
library `html.parser`: Equipment rows in declaration order with nils blank and the empty state;
Media Releases limited to those that supplied a used file; every `run_file` with its expected and
computed SHA-256, a mismatch highlighted, an `ate.` module with no expected value and the stored
flag; Sub-schedule instances in tree order with their prefixes and no keyvals; the Run log grouped
by owner — the run, each Pre-test and Post-test Action, then each step Attempt — with DEBUG lines
and Device trace never appearing; the section gone when `include_run_log=False`; and `<b>x</b>` in
a log line or an Equipment field escaped rather than rendered."""

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

#: Equipment declared in a Pre-test Action: a full row, then nils, then a nil serial and cal due.
EQUIPMENT_SOURCE = """\
return ScheduleParams("Equipment schedule", "equipment declared in the Pre-test")
  :setSchedule({
    StepParams("check"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "ok") end),
  })
  :setPreTestAction(function(passFail)
    ATE.addEquipment("Power supply", "PSU-1", "GPIB", "2026-11-01", "bench unit")
    ATE.addEquipment("Multimeter")
    ATE.addEquipment("Oscilloscope", nil, "USB", nil, "no serial, no cal due")
  end)
"""

#: Nothing declared, so the section says so.
NO_EQUIPMENT_SOURCE = """\
return ScheduleParams("No equipment schedule", "a run with no addEquipment call")
  :setSchedule({
    StepParams("check"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "ok") end),
  })
"""

#: Requires a module from a dependency release and a bundled ate. module. Release UNUSED is a
#: dependency of the top-level release but supplies nothing this run loads.
CHAIN_MAIN = """\
local m = require("used_mod")
local dev = require("ate.devices")
return ScheduleParams("Chain schedule", "a used dependency and an unused one")
  :setSchedule({
    StepParams("check"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "module " .. m.name) end),
  })
"""
USED_MOD_SRC = 'return { name = "used module" }\n'
UNUSED_MOD_SRC = 'return { name = "never required" }\n'

#: The helper's bytes on disk disagree with the checksum its .atedef declares (WRONG_SHA), and the
#: schedule also requires an ate. module, which has no expected checksum at all.
FILE_MAIN = """\
local h = require("helper")
local dev = require("ate.devices")
return ScheduleParams("Files schedule", "a mismatched helper and an ate. module")
  :setSchedule({
    StepParams("check"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "helper " .. tostring(h.value)) end),
  })
"""
HELPER_SRC = "return { value = 42 }\n"
WRONG_SHA = "0" * 64

#: A Sub-schedule include one level deep, with a prefix and keyvals the report must not show.
INSTANCE_MAIN = """\
return ScheduleParams("Instances schedule", "a sub-schedule with a prefix")
  :setSchedule({
    StepParams("top"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "top step") end),
    StepParams("channel 1"):setStepNo("2."):setPrefix("2."):setInclude("child.tslua", { channel = 1 }),
  })
"""
INSTANCE_CHILD = """\
return ScheduleParams("Channel check", "one step under the header")
  :setSchedule({
    StepParams("output on"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "on") end),
  })
"""

#: A run-level WARNING (the mismatch), a Pre-test line, a retried step with two Attempts logging
#: INFO then WARNING (plus a DEBUG line that must not reach the report), and a Post-test ERROR.
RUNLOG_MAIN = """\
local attempts = 0
return ScheduleParams("Run log schedule", "run, pre, two attempts, post")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("first"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.log(Log.INFO, "attempt one info")
          ATE.log(Log.DEBUG, "attempt one debug")
          ATE.report(Result.FAIL, "cold joint")
        else
          ATE.log(Log.WARNING, "attempt two warning")
          ATE.report(Result.PASS, "warm now")
        end
      end),
  })
  :setPreTestAction(function(passFail) ATE.log(Log.INFO, "pretest info") end)
  :setPostTestAction(function(passFail) ATE.log(Log.ERROR, "posttest error") end)
"""

#: Pre-test and Post-test Actions on the top level AND on the Sub-schedule: two Actions in each
#: phase, so every group must name the instance it came from.
PHASES_MAIN = """\
return ScheduleParams("Phases schedule", "pre and post actions on two instances")
  :setSchedule({
    StepParams("top"):setStepNo("1.")
      :setAction(function() ATE.log(Log.INFO, "top step ran") ATE.report(Result.PASS, "top step") end),
    StepParams("channel 1"):setStepNo("2."):setPrefix("2."):setInclude("child.tslua", {}),
  })
  :setPreTestAction(function(passFail) ATE.log(Log.INFO, "top pre") end)
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "top post") end)
"""
PHASES_CHILD = """\
return ScheduleParams("Channel check", "its own pre and post actions")
  :setSchedule({
    StepParams("output on"):setStepNo("1.")
      :setAction(function() ATE.log(Log.INFO, "child step ran") ATE.report(Result.PASS, "on") end),
  })
  :setPreTestAction(function(passFail) ATE.log(Log.INFO, "child pre") end)
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "child post") end)
"""

#: A device whose open is refused: with deviceTrace on, every operation is traced at DEBUG with
#: source device, in the database and never in the report.
DEVICE_SOURCE = """\
return ScheduleParams("Device trace schedule", "a device open with tracing on")
  :setSchedule({
    StepParams("open"):setStepNo("1.")
      :setAction(function()
        ATE.log(Log.INFO, "trying the device")
        ATE.log(Log.DEBUG, "device debug line")
        local ok, err = pcall(function() ATE.open(DeviceType.TCP, {host = "127.0.0.1", port = 1, timeout = 1, readTerm = "", writeTerm = ""}) end)
        ATE.report(Result.PASS, ok and "opened" or "open refused")
      end),
  })
"""

#: Markup in an Equipment field and in a logged line.
MARKUP_SOURCE = """\
return ScheduleParams("Markup schedule", "equipment and log lines that contain markup")
  :setSchedule({
    StepParams("check"):setStepNo("1.")
      :setAction(function()
        ATE.log(Log.INFO, "log line with <b>x</b> in it")
        ATE.report(Result.PASS, "ok")
      end),
  })
  :setPreTestAction(function(passFail)
    ATE.addEquipment("Scope <b>x</b>", "<b>SN-1</b>", nil, nil, nil)
  end)
"""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_releases(tmp_path: Path, releases: dict[str, dict]) -> Path:
    """Write every release under ``defs/<label>/``; `files` maps rel path -> source, or (source, declared sha)."""
    root = tmp_path / "defs"
    for label, release in releases.items():
        directory = root / label
        directory.mkdir(parents=True, exist_ok=True)
        entries = []
        for rel, spec in release["files"].items():
            src, declared = spec if isinstance(spec, tuple) else (spec, None)
            entries.append({"path": rel, "sha256": declared or _sha(src)})
            (directory / rel).write_text(src, encoding="utf-8")
        (directory / f"{label}.atedef").write_text(
            json.dumps(
                {
                    "format": 1,
                    "mediaNumber": release["mediaNumber"],
                    "name": release["name"],
                    "version": release["version"],
                    "depends": release.get("depends", []),
                    "files": entries,
                }
            ),
            encoding="utf-8",
        )
    return root


def _run(tmp_path: Path, releases: dict[str, dict], schedule: str, *, serial: str = "B-1",
         store_mismatched: Optional[bool] = None, config_extra: Optional[dict] = None) -> tuple[RunStore, str]:
    """Run one schedule headlessly through every release in `releases`; return (store, run_uuid)."""
    root = _write_releases(tmp_path, releases)
    config = {
        "format": 1,
        "station": {"name": "BENCH-21C", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c31"},
        "defaultProject": "Power board",
        "database": {"type": "sqlite", "path": str(tmp_path / "ate.db")},
        "reports": {"directory": str(tmp_path / "reports")},
        "testDefinitions": {"directories": [str(root)]},
    }
    config.update(config_extra or {})
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    session = HeadlessRunSession(config_path)
    session.set_operator("Alice")
    session.set_uut_type("Board")
    session.set_uut_serial(serial)
    session.select_schedule(schedule)
    session.start(store_mismatched=store_mismatched)

    # A Stop-on-Fail pause needs an answer: retry the first failure, continue on any later one.
    retried = {"done": False}

    def _answer(_seq: int, _attempt: int) -> None:
        if not retried["done"]:
            retried["done"] = True
            session.choose("RETRY")
        else:
            session.choose("CONTINUE")

    session.failure_prompt.connect(_answer)

    # Wait for the run-end report too: the PDF helper is a QProcess owned by the session, and a live
    # helper at teardown crashes this Qt build.
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


def _report(tmp_path: Path, releases: dict[str, dict], schedule: str, **kwargs) -> str:
    """Run a schedule headlessly and render its report; return the HTML."""
    store, run_uuid = _run(tmp_path, releases, schedule, **kwargs)
    return render_report(store, run_uuid, Config(path=tmp_path / "config.json"))


def _solo(files: dict) -> dict:
    """The one-release shape most tests need."""
    return {"solo": {"mediaNumber": "MR-21C", "name": "21c release", "version": "1.0.0", "files": files}}


class _Tables(HTMLParser):
    """Every table in the page as ``(class, [(row_class, [cell_text, ...]), ...])``, in page order."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[tuple[str, list[tuple[str, list[str]]]]] = []
        self._class = ""
        self._rows: Optional[list[tuple[str, list[str]]]] = None
        self._row_class = ""
        self._cells: list[str] = []
        self._parts: list[str] = []
        self._in_cell = False
        self._in_head = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table":
            self._class = dict(attrs).get("class") or ""
            self._rows = []
        elif tag == "thead":
            self._in_head = True
        elif tag == "tr" and self._rows is not None:
            self._row_class = "thead" if self._in_head else (dict(attrs).get("class") or "")
            self._cells = []
        elif tag in ("td", "th") and self._rows is not None:
            self._in_cell = True
            self._parts = []
        elif tag == "br" and self._in_cell:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._in_cell:
            self._in_cell = False
            self._cells.append(" ".join("".join(self._parts).split()))
        elif tag == "tr" and self._rows is not None:
            self._rows.append((self._row_class, self._cells))
        elif tag == "thead":
            self._in_head = False
        elif tag == "table" and self._rows is not None:
            self.tables.append((self._class, self._rows))
            self._rows = None

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._parts.append(data)


def _tables(html: str) -> list[tuple[str, list[tuple[str, list[str]]]]]:
    parser = _Tables()
    parser.feed(html)
    return parser.tables


def _table(html: str, class_name: str) -> list[tuple[str, list[str]]]:
    """The rows of the first table carrying `class_name`, heading row included."""
    for table_class, rows in _tables(html):
        if class_name in table_class:
            return rows
    raise AssertionError(f"no {class_name} table in the report")


def _data_rows(html: str, class_name: str) -> list[list[str]]:
    """A section table's data rows — everything outside its <thead>."""
    return [cells for row_class, cells in _table(html, class_name) if row_class != "thead"]


def _section(html: str, heading: str) -> str:
    """The page slice from one <h2> up to the next: that section's own content."""
    match = re.search(rf"<h2[^>]*>{heading}</h2>(.*?)(?=<h2|\Z)", html, re.S)
    assert match, f"no {heading} section in the report"
    return match.group(1)


def _run_log_groups(html: str) -> list[tuple[str, list[list[str]]]]:
    """The Run log section as (group label, data rows) pairs, in the order the groups appear."""
    body = _section(html, "Run log")
    groups = []
    for label, fragment in re.findall(r"<h3>(.*?)</h3>(.*?)(?=<h3|\Z)", body, re.S):
        rows = [cells for _, rows in _tables(fragment) for row_class, cells in rows if row_class != "thead"]
        groups.append((" ".join(label.split()), rows))
    return groups


# -- Equipment -----------------------------------------------------------


def test_equipment_rows_in_declaration_order_with_nil_blanks(tmp_path: Path) -> None:
    """One row per `ATE.addEquipment` call in the order it happened, every nil printed as a blank."""
    html = _report(tmp_path, _solo({"equip.tslua": EQUIPMENT_SOURCE}), "Equipment schedule")
    head = [cells for row_class, cells in _table(html, "equip") if row_class == "thead"]
    assert head == [["Name", "Serial", "Connection", "Cal due", "Notes"]], "the spec's columns, in order"
    assert _data_rows(html, "equip") == [
        ["Power supply", "PSU-1", "GPIB", "2026-11-01", "bench unit"],
        ["Multimeter", "", "", "", ""],
        ["Oscilloscope", "", "USB", "", "no serial, no cal due"],
    ], "declaration order kept, nils blank"


def test_equipment_section_says_none_declared(tmp_path: Path) -> None:
    """With no Equipment the section says "No equipment declared." instead of a table."""
    html = _report(tmp_path, _solo({"none.tslua": NO_EQUIPMENT_SOURCE}), "No equipment schedule")
    section = _section(html, "Equipment")
    assert "No equipment declared." in section
    assert "<table" not in section, "the empty state is one line, not an empty table"


# -- Configuration: Media Releases, Files, Sub-schedule instances -------


def test_media_releases_show_only_releases_that_supplied_a_file(tmp_path: Path) -> None:
    """The top-level release and the dependency that supplied a required module; the unused one is out."""
    html = _report(
        tmp_path,
        {
            "top": {
                "mediaNumber": "MR-TOP", "name": "Top release", "version": "1.0.0",
                "depends": [{"name": "Used release", "version": "^1.0"},
                            {"name": "Unused release", "version": "^1.0"}],
                "files": {"main.tslua": CHAIN_MAIN},
            },
            "used": {
                "mediaNumber": "MR-USED", "name": "Used release", "version": "1.2.0",
                "files": {"used_mod.lua": USED_MOD_SRC},
            },
            "unused": {
                "mediaNumber": "MR-UNUSED", "name": "Unused release", "version": "1.1.0",
                "files": {"unused_mod.lua": UNUSED_MOD_SRC},
            },
        },
        "Chain schedule",
    )
    rows = _data_rows(html, "media")
    assert [r[:3] for r in rows] == [
        ["MR-TOP", "Top release", "1.0.0"],
        ["MR-USED", "Used release", "1.2.0"],
    ], "the release that supplied nothing is not listed"
    configuration = _section(html, "Configuration")
    assert "MR-UNUSED" not in configuration
    for label, media in (("top", "MR-TOP"), ("used", "MR-USED")):
        atedef = _sha((tmp_path / "defs" / label / f"{label}.atedef").read_text(encoding="utf-8"))
        assert next(r for r in rows if r[0] == media)[3] == atedef, f"{media} prints a checksum that is not its own .atedef"


def test_files_show_expected_computed_and_stored(tmp_path: Path) -> None:
    """Every run_file row, with a mismatch highlighted, an `ate.` module having no expected value,
    and the stored flag reflecting database.storeMismatchedFiles."""
    html = _report(
        tmp_path,
        {
            "top": {
                "mediaNumber": "MR-FILES", "name": "Files release", "version": "2.0.0",
                "files": {"main.tslua": FILE_MAIN, "helper.lua": (HELPER_SRC, WRONG_SHA)},
            },
        },
        "Files schedule",
        config_extra={"database": {"type": "sqlite", "path": str(tmp_path / "ate.db"), "storeMismatchedFiles": "always"}},
        store_mismatched=True,
    )
    by_path = {row[0]: row for row in _data_rows(html, "files")}
    assert {"main.tslua", "helper.lua", "ate.devices"} <= set(by_path), "the schedule, its helper and the ate. module"

    helper = by_path["helper.lua"]
    assert helper[1] == "MR-FILES" and helper[2] == WRONG_SHA
    assert helper[3] == _sha(HELPER_SRC), "computed from the bytes actually loaded"
    assert helper[4] == "yes", "storeMismatchedFiles=always stored the mismatched bytes"

    clean = by_path["main.tslua"]
    assert clean[1] == "MR-FILES" and clean[2] == clean[3], "a matching file's expected equals its computed"
    assert clean[4] == "no", "matching files are never stored"

    ate_module = by_path["ate.devices"]
    assert ate_module[1] == "", "a bundled ate. module belongs to no Media Release"
    assert ate_module[2] == "", "an ate. module has no expected checksum"
    assert len(ate_module[3]) == 64 and ate_module[4] == "no"

    highlighted = [cells for row_class, cells in _table(html, "files") if row_class == "mismatch"]
    assert [h[0] for h in highlighted] == ["helper.lua"], "the mismatched row is the highlighted one"
    assert "⚠ One or more files did not match their recorded checksum" in html, "the cover banner still warns"
    assert "See the Configuration section" in html, "and points at the section that shows the files"


def test_a_mismatch_is_not_stored_when_the_setting_says_never(tmp_path: Path) -> None:
    """The Stored column follows database.storeMismatchedFiles, not the mismatch itself."""
    html = _report(
        tmp_path,
        {
            "top": {
                "mediaNumber": "MR-FILES2", "name": "Files release 2", "version": "2.0.0",
                "files": {"main.tslua": FILE_MAIN, "helper.lua": (HELPER_SRC, WRONG_SHA)},
            },
        },
        "Files schedule",
        config_extra={"database": {"type": "sqlite", "path": str(tmp_path / "ate.db"), "storeMismatchedFiles": "never"}},
    )
    rows = _table(html, "files")
    helper = next(cells for row_class, cells in rows if cells[0] == "helper.lua")
    assert helper[4] == "no", "never means the bytes are not recorded"
    assert any(row_class == "mismatch" and cells[0] == "helper.lua" for row_class, cells in rows), "still highlighted"


def test_sub_schedule_instances_in_tree_order_with_prefix(tmp_path: Path) -> None:
    """Every instance in tree order with its name, file and prefix; keyvals are not shown."""
    html = _report(
        tmp_path,
        _solo({"main.tslua": INSTANCE_MAIN, "child.tslua": INSTANCE_CHILD}),
        "Instances schedule",
    )
    rows = _data_rows(html, "instances")
    assert [r[0] for r in rows] == ["Instances schedule", "Channel check"], "tree order"
    assert rows[0] == ["Instances schedule", "main.tslua", ""], "the top level has no prefix"
    assert rows[1] == ["Channel check", "child.tslua", "2."], "the include's prefix as applied"
    assert "channel = 1" not in _section(html, "Configuration"), "keyvals are not shown"


# -- Run log -------------------------------------------------------------


def test_run_log_groups_run_pretest_attempts_then_posttest(tmp_path: Path) -> None:
    """Groups in the spec's order — the run, each Action, each Attempt — each in time order."""
    html = _report(
        tmp_path,
        {
            "top": {
                "mediaNumber": "MR-LOG", "name": "Run log release", "version": "1.0.0",
                "files": {"main.tslua": (RUNLOG_MAIN, WRONG_SHA)},
            },
        },
        "Run log schedule",
        config_extra={"database": {"type": "sqlite", "path": str(tmp_path / "ate.db"), "storeMismatchedFiles": "never"}},
    )
    groups = _run_log_groups(html)
    labels = [label for label, _ in groups]
    assert labels[0] == "Run", labels
    assert labels[1] == "Pre-test", "one Pre-test Action, so no instance is named"
    assert labels[2] == "Post-test", "the Actions come before the steps, as the spec lists them"
    assert labels[3] == "Step 1. first (attempt 1)"
    assert labels[4] == "Step 1. first (attempt 2)", labels

    texts = {label: [cells[3] for cells in rows] for label, rows in groups}
    assert any("checksum mismatch for main.tslua" in text for text in texts["Run"]), "the run-level warning is the run's"
    assert texts["Pre-test"] == ["pretest info"]
    assert texts["Step 1. first (attempt 1)"] == ["attempt one info"], "DEBUG lines are not in the group"
    assert texts["Step 1. first (attempt 2)"] == ["attempt two warning"]
    assert texts["Post-test"] == ["posttest error"]

    levels = {cells[1] for _, rows in groups for cells in rows}
    assert levels == {"INFO", "WARNING", "ERROR"}, "every level the spec reports appears, DEBUG does not"

    # Within the Run and Pre-test groups the lines are in the order they happened.
    for label, rows in groups:
        stamps = [cells[0] for cells in rows]
        assert all(a <= b for a, b in zip(stamps, stamps[1:])), f"{label}'s lines are not in time order"


def test_run_log_groups_name_the_instance_when_a_phase_has_several_actions(tmp_path: Path) -> None:
    """Two Pre-test Actions and two Post-test Actions: each group is labelled with its instance."""
    html = _report(
        tmp_path,
        _solo({"main.tslua": PHASES_MAIN, "child.tslua": PHASES_CHILD}),
        "Phases schedule",
    )
    labels = [label for label, _ in _run_log_groups(html)]
    assert labels == [
        "Pre-test (Phases schedule, instance 0)",
        "Pre-test (Channel check, instance 1)",
        "Post-test (Channel check, instance 1)",
        "Post-test (Phases schedule, instance 0)",
        "Step 1. top (attempt 1)",
        "Step 2.1. output on (attempt 1)",
    ], labels
    texts = {label: [cells[3] for cells in rows] for label, rows in _run_log_groups(html)}
    assert texts["Pre-test (Channel check, instance 1)"] == ["child pre"]
    assert texts["Post-test (Channel check, instance 1)"] == ["child post"], "Post-test Actions ran in reverse tree order"


def test_device_trace_and_debug_lines_never_appear(tmp_path: Path) -> None:
    """Device trace is recorded in the database with tracing on and kept out of the report."""
    store, run_uuid = _run(
        tmp_path,
        _solo({"device.tslua": DEVICE_SOURCE}),
        "Device trace schedule",
        config_extra={"deviceTrace": {"enabled": True, "maxBytes": 64}},
    )
    trace = [ln for ln in store.log_lines(run_uuid) if ln["source"] == "device" and ln["level"] == "DEBUG"]
    assert trace, "the schedule must really have produced Device trace lines"
    assert any(ln["level"] == "DEBUG" and ln["source"] == "schedule" for ln in store.log_lines(run_uuid)), "and a schedule DEBUG line too"

    html = render_report(store, run_uuid, Config(path=tmp_path / "config.json"))
    section = _section(html, "Run log")
    for ln in trace:
        assert ln["text"] not in section, f"a Device trace line reached the report: {ln['text']!r}"
    assert "DEBUG" not in section
    assert "device debug line" not in section
    assert "trying the device" in section, "the INFO line the step logged is there"
    assert all(cells[1] in ("INFO", "WARNING", "ERROR") for _, rows in _run_log_groups(html) for cells in rows)


def test_run_log_section_is_left_out_when_include_run_log_is_false(tmp_path: Path) -> None:
    """History regeneration can drop the Run log section entirely."""
    store, run_uuid = _run(tmp_path, _solo({"equip.tslua": EQUIPMENT_SOURCE}), "Equipment schedule")
    config = Config(path=tmp_path / "config.json")
    with_log = render_report(store, run_uuid, config)
    without = render_report(store, run_uuid, config, include_run_log=False)
    assert ">Run log<" in with_log
    assert not re.search(r"<h2[^>]*>Run log</h2>", without), "the section itself is gone, not just its rows"
    assert 'class="log"' not in without
    assert "Run log left out of this report." in without, "one line says why it is not there"
    assert ">Equipment<" in without and "Power supply" in without, "the other sections are unaffected"
    assert "pretest info" not in without


def test_log_line_and_equipment_markup_is_escaped(tmp_path: Path) -> None:
    """A `<b>` in a logged line or an Equipment field is data, never markup."""
    html = _report(tmp_path, _solo({"markup.tslua": MARKUP_SOURCE}), "Markup schedule")
    assert "<b>x</b>" not in html, "no stored value reaches the page as a live tag"
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert _data_rows(html, "equip") == [["Scope <b>x</b>", "<b>SN-1</b>", "", "", ""]], "the escapes parse back to the given text"
    logged = [cells[3] for _, rows in _run_log_groups(html) for cells in rows if "log line with" in cells[3]]
    assert logged == ["log line with <b>x</b> in it"]

EMPTY_MAIN = """\
local e = require("empty")
return ScheduleParams("Empty module schedule", "requires a zero-byte module")
  :setSchedule({
    StepParams("one"):setStepNo("1."):setAction(function() ATE.report(Result.PASS, "ok", "1", "1") end),
  })
"""

PREFAIL_MAIN = """\
return ScheduleParams("Pre-fail schedule", "its Pre-test Action raises")
  :setPreTestAction(function() error("calibration failed") end)
  :setPostTestAction(function() ATE.log(Log.INFO, "teardown ran") end)
  :setSchedule({
    StepParams("one"):setStepNo("1."):setAction(function() ATE.report(Result.PASS, "ok", "1", "1") end),
  })
"""


def test_an_empty_mismatched_file_that_was_stored_says_yes(tmp_path: Path) -> None:
    """Stored means the bytes were recorded, even when there are none (zero-byte file)."""
    html = _report(
        tmp_path,
        {"top": {"mediaNumber": "MR-EMPTY", "name": "Empty release", "version": "1.0.0",
                 "files": {"main.tslua": EMPTY_MAIN, "empty.lua": ("", WRONG_SHA)}}},
        "Empty module schedule",
        config_extra={"database": {"type": "sqlite", "path": str(tmp_path / "ate.db"), "storeMismatchedFiles": "always"}},
        store_mismatched=True,
    )
    empty = next(cells for _, cells in _table(html, "files") if cells[0] == "empty.lua")
    assert empty[4] == "yes"


def test_the_pre_test_failure_explanation_is_grouped_with_the_failed_action(tmp_path: Path) -> None:
    """With no step lines, the abort explanation still belongs to the Pre-test Action, not the Post-test."""
    html = _report(
        tmp_path,
        {"top": {"mediaNumber": "MR-PREFAIL", "name": "Pre-fail release", "version": "1.0.0",
                 "files": {"main.tslua": PREFAIL_MAIN}}},
        "Pre-fail schedule",
    )
    groups = {label: [row[-1] for row in rows] for label, rows in _run_log_groups(html)}
    pre = [texts for label, texts in groups.items() if label.startswith("Pre-test")]
    post = [texts for label, texts in groups.items() if label.startswith("Post-test")]
    assert pre and any("every step is NOTRUN" in t for t in pre[0]), groups
    assert post and post[0] == ["teardown ran"], groups


def test_a_mismatched_file_row_is_visibly_marked(tmp_path: Path) -> None:
    """The mismatch class has a real rule: a background tint and a ⚠ marker that survives greyscale."""
    html = _report(
        tmp_path,
        {"top": {"mediaNumber": "MR-MARK", "name": "Mark release", "version": "1.0.0",
                 "files": {"main.tslua": FILE_MAIN, "helper.lua": (HELPER_SRC, WRONG_SHA)}}},
        "Files schedule",
        store_mismatched=False,
    )
    style = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    assert re.search(r"\.mismatch\s*\{[^}]*background", style)
    assert re.search(r"tr\.mismatch td:first-child::before\s*\{[^}]*content", style)
