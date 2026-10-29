"""CI harness for the example Media Release (packets 29a, 29b and 29c).

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
includes.

Run it on its own with `pytest examples/`, or as part of the regular suite (`pytest`):
`testpaths` in pyproject.toml collects `examples` too.
"""

from __future__ import annotations

import hashlib
import json
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
    assert names == ["Example basics", "Flow and retry", "Report every Result", "Step inputs", "Stop on fail"]
    assert sorted(visible_schedules()) == names

    # Packet 29c: the Sub-schedules are listed so includes can resolve them, but hidden means the
    # Operator is never offered one as a Test Schedule in its own right.
    hidden = sorted(entry.rel_path for entry in definition.files if entry.hidden)
    assert hidden == ["nest_a.tslua", "nest_b.tslua", "nest_c.tslua", "sub_channel.tslua"]
    sub_schedule_names = {"Channel check", "Nested A", "Nested B", "Nested C"}
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

    Five schedules, eight runs: Stop on fail is run twice (Continue and Abort), Example basics twice
    (its optional step left disabled, then enabled by the Operator) and Flow and retry twice (Stop on
    Fail with a Retry, and with Stop on Fail switched off), so the record count is the schedule count
    plus those three extra runs."""
    assert len(records) == len(visible_schedules()) + 3
    assert {entry["schedule"] for entry in RUNS} == set(visible_schedules())
    for record in records.values():
        run = record["run"]
        assert run is not None, "the run row is written"
        assert run["result"] in ("PASS", "FAIL")
        assert run["end_reason"] in ("COMPLETED", "STOPPED_ON_FAIL")
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
