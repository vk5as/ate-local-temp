"""CI harness for the example Media Release (packet 29a).

Every visible Test Schedule in ``examples/media-release/`` runs headless through the seam-1 run
session with scripted Operator answers, and its recorded Test Run is checked here: the Test
Definition scans clean (no broken rows, no checksum mismatch), the schedule metadata and Step
Numbers are recorded, every `Result` value appears, a step that never reports records NOREPORT, a
step that raises records ERROR, the log levels and Equipment are recorded, the step disabled by
default stays NOTRUN, and Stop on Fail ends both ways.

Run it on its own with `pytest examples/`, or as part of the regular suite (`pytest`):
`testpaths` in pyproject.toml collects `examples` too.
"""

from __future__ import annotations

import hashlib
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
    """Every example schedule run once headless, keyed by the harness's run key."""
    return run_all(tmp_path_factory.mktemp("example-release"), stub_pdf=True)


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
    """Each example schedule runs to a recorded Test Run through the headless session."""
    assert len(records) == len(RUNS)
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

    nodes = record["nodes"]
    assert [node["step_number"] for node in nodes] == ["1.", "2.", "3.", "4.", "5.", "6."]
    assert [node["result"] for node in nodes] == ["PASS", "PASS", "PASS", "NOTRUN", "PASS", "PASS"]

    # Step 4 is disabled by default: it never runs, so it records NOTRUN with no Attempt.
    disabled = nodes[3]
    assert disabled["step_name"] == "Optional check"
    assert (disabled["default_enabled"], disabled["enabled"]) == (0, 0)
    assert disabled["attempt_count"] == 0

    # ATE.run is readable from a step action.
    first = record["attempts"][0]
    assert first["msg"] == "operator Ada Lovelace"
    assert (first["actual"], first["expected"]) == ("Ada Lovelace", "Ada Lovelace")

    # ATE.log at every level, plus print, which ATE redirects to INFO.
    schedule_logs = [(line["level"], line["text"]) for line in record["logs"] if line["source"] == "schedule"]
    assert schedule_logs == [
        ("DEBUG", "reading ATE.run for the run context"),
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

    # The scripted answer to the step's FLOAT input is recorded as an input_value row.
    answers = [(value["input_index"], value["type"], value["value"]) for value in record["input_values"]]
    assert answers == [(1, "FLOAT", "1.5")]


def test_results_schedule_records_every_result(records: dict[str, dict[str, Any]]) -> None:
    """One step per Result value, including NOREPORT and a Lua ERROR."""
    record = records["results"]
    run = record["run"]
    assert run["result"] == "FAIL", "a FAIL and an ERROR step make the run fail"
    assert run["end_reason"] == "COMPLETED", ":setStopOnFail(false) means no pause"

    steps = [(node["step_number"], node["result"]) for node in record["nodes"]]
    assert steps == [
        ("1.", "PASS"), ("2.", "PASS"), ("3.", "PASS"), ("4.", "PASS"), ("5.", "FAIL"),
        ("6.", "SKIPPED"), ("7.", "NOTRUN"), ("8.", "FORCED_NOTRUN"),
        ("9.", "NOREPORT"), ("10.", "ERROR"), ("11.", "ERROR"),
    ]

    by_node = {attempt["node_seq"]: attempt for attempt in record["attempts"]}
    assert len(by_node) == len(record["nodes"])

    # Numbers, strings and booleans all reach the record as text.
    assert (by_node[0]["actual"], by_node[0]["expected"]) == ("Green", "Green")
    assert (by_node[1]["actual"], by_node[1]["expected"]) == ("42", "42")
    assert (by_node[2]["actual"], by_node[2]["expected"]) == ("True", "True")

    # Two reports in one step: the last one is the step's result.
    assert (by_node[3]["pass_fail"], by_node[3]["msg"]) == ("PASS", "second reading")

    # The step that never reported has no message and ATE logged the warning.
    assert by_node[8]["pass_fail"] == "NOREPORT" and by_node[8]["msg"] is None
    assert any("never reported" in line["text"] for line in record["logs"])

    # The step that raised: ERROR with the Lua message, and a traceback in the log.
    assert by_node[9]["pass_fail"] == "ERROR"
    assert "example: the instrument did not answer" in by_node[9]["msg"]
    error_line = next(line for line in record["logs"]
                      if line["level"] == "ERROR" and "stack traceback" in line["text"])
    assert "report_results.tslua" in error_line["text"]


def test_stop_on_fail_continues_when_the_operator_continues(records: dict[str, dict[str, Any]]) -> None:
    """Continue at the prompt: the run carries on, still FAIL, End Reason COMPLETED."""
    record = records["continue"]
    run = record["run"]
    assert run["stop_on_fail"] == 1, ":setStopOnFail(true) is the run's default"
    assert run["result"] == "FAIL"
    assert run["end_reason"] == "COMPLETED"
    assert record["attempts"][0]["prompt_choice"] == "CONTINUE"
    assert [(node["step_number"], node["result"]) for node in record["nodes"]] == [("1.", "FAIL"), ("2.", "PASS")]


def test_stop_on_fail_aborts_when_the_operator_aborts(records: dict[str, dict[str, Any]]) -> None:
    """Abort at the prompt: the later step never runs and the End Reason is STOPPED_ON_FAIL."""
    record = records["abort"]
    run = record["run"]
    assert run["result"] == "FAIL"
    assert run["end_reason"] == "STOPPED_ON_FAIL"
    assert record["attempts"][0]["prompt_choice"] == "ABORT"
    nodes = [(node["step_number"], node["result"], node["attempt_count"]) for node in record["nodes"]]
    assert nodes == [("1.", "FAIL", 1), ("2.", "NOTRUN", 0)]
