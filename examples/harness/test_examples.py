"""CI harness for the example Media Release (packet 29a).

Every visible Test Schedule in ``examples/media-release/`` runs headless through the seam-1 run
session with scripted Operator answers, and its recorded Test Run is checked here: the Test
Definition scans clean (no broken rows, no checksum mismatch), the schedule metadata and Step
Numbers are recorded, every `Result` value appears, a step that never reports records NOREPORT, a
step that raises records ERROR, the log levels and Equipment are recorded, the step disabled by
default stays NOTRUN until the Operator enables it, Stop on Fail ends both ways, and every run
writes its report.

Run it on its own with `pytest examples/`, or as part of the regular suite (`pytest`):
`testpaths` in pyproject.toml collects `examples` too.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from ate.catalog import (
    definition_mismatches,
    find_definitions,
    list_schedule_mismatches,
    list_schedule_rows,
)

from examples.harness.run_examples import RELEASE_DIR, RUNS, run_all, visible_schedules


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
    assert names == ["Example basics", "Report every Result", "Stop on fail"]
    assert sorted(visible_schedules()) == names


def test_every_visible_example_schedule_runs_headless(records: dict[str, dict[str, Any]]) -> None:
    """Each example schedule runs to a recorded Test Run through the headless session.

    Three schedules, five runs: Stop on fail is run twice (Continue and Abort) and Example basics
    twice (its optional step left disabled, then enabled by the Operator), so the record count is
    the schedule count plus those two extra runs."""
    assert len(records) == len(visible_schedules()) + 2
    assert {entry["schedule"] for entry in RUNS} == set(visible_schedules())
    for record in records.values():
        run = record["run"]
        assert run is not None, "the run row is written"
        assert run["result"] in ("PASS", "FAIL")
        assert run["end_reason"] in ("COMPLETED", "STOPPED_ON_FAIL")
        assert run["ended_at"], "the run was closed, not left open"
        assert [release["media_number"] for release in record["media_releases"]] == ["MR-EXAMPLE"]
        assert all(file["expected_sha256"] == file["computed_sha256"] for file in record["run_files"])
        assert record["run_files"], "the run records the schedule file it loaded"

        # The report really arrives: HTML on disk, PDF beside it, named after this run's UUT.
        assert record["reports"], "report_written fired for this run"
        html, pdf = record["reports"][0]
        assert html and Path(html).exists(), f"the HTML report is missing: {html!r}"
        assert pdf and Path(pdf).exists(), f"the PDF report is missing: {pdf!r}"
        assert "EX-0001" in html and run["result"] in Path(html).name, "the report is this run's"

        # Every prompt was one the harness scripted: a schedule that pauses when it does not say it
        # will is a bug, not something to answer.
        assert record["unexpected_prompts"] == [], f"{run['schedule_name']} paused unexpectedly"


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
