"""Packet 34a: the manuals' examples really run, and the manuals cannot fall behind the code.

Three things are checked here:

1. Every Lua example under ``docs/manuals/examples/test-writer/`` runs headless through ATE
   (the same seam-1 harness the example Media Release uses) and records what the manual says
   it records.
2. Every example quoted in the Markdown manual is byte-for-byte the file on disk, every
   example is included by the LaTeX manual, and the ``.atedef`` beside them lists all of them.
3. Every ``ATE`` function, enum, ``ScheduleParams``/``StepParams`` setter, Device Type and
   ``ATE.run`` field the code defines is named in both test-writer manuals. The list is
   derived from the code, never hand-kept, so a manual that drifts fails the suite.

The coverage check is a table of manuals and name groups so packets 34b (admin, config keys)
and 34c (developer, modules and seams) can add their own entries without touching this file.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import pkgutil
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import pytest

from ate.devices.registry import Driver
from ate.engine.lua_host import _AteTable, _ENUMS, _RUN_FIELDS, _ScheduleParams, _StepParams

from examples.harness.peers import Peers
from examples.harness.run_examples import dead_port, run_schedule, write_config

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = ROOT / "docs/manuals/examples/test-writer"
MANUALS_DIR = ROOT / "docs/manuals"


# --- The manuals themselves ---------------------------------------------------------------

@dataclass(frozen=True)
class Manual:
    """One manual and the files that make it up, for the checks below."""

    name: str
    tex_dir: Path
    markdown: Path

    def tex_parts(self) -> list[Path]:
        """main.tex plus every chapter file it inputs."""
        return sorted(p for p in self.tex_dir.rglob("*.tex"))

    def sources(self) -> list[Path]:
        """Every source file of this manual: the LaTeX parts and the Markdown one."""
        return self.tex_parts() + [self.markdown]

    def texts(self) -> list[tuple[str, str]]:
        """``(path relative to the repository, text)`` for every source file."""
        return [(str(p.relative_to(ROOT)), p.read_text(encoding="utf-8")) for p in self.sources()]


TEST_WRITER = Manual(
    name="test-writer",
    tex_dir=MANUALS_DIR / "test-writer",
    markdown=MANUALS_DIR / "llm/test-writer.md",
)

#: Every manual this packet builds. 34b adds "admin" and 34c "developer" here, and a group of
#: names for each in COVERAGE below.
MANUALS: dict[str, Manual] = {"test-writer": TEST_WRITER}

#: The examples directory each manual is written against.
MANUAL_EXAMPLES: dict[str, Path] = {"test-writer": EXAMPLES_DIR}


# --- Names the code defines ---------------------------------------------------------------

def ate_table_functions() -> set[str]:
    """Every function the ``ATE`` table exposes to a Test Schedule.

    ``register`` is the engine's seam for adding a function, not something a schedule calls,
    so it is excluded. ``run`` is a table rather than a function and is named explicitly,
    because it is reached through ``__getattr__`` and not the registry.
    """
    table = _AteTable()
    names = {name for name in table._extra}
    for name, member in inspect.getmembers(table, callable):
        if not name.startswith("_") and name != "register":
            names.add(name)
    names.add("run")
    return names


def enum_names() -> set[str]:
    """Every enum table and every member of it."""
    return {name for name, members in _ENUMS.items()} | {m for members in _ENUMS.values() for m in members}


def builder_methods() -> set[str]:
    """Every setter ``ScheduleParams`` and ``StepParams`` offers."""
    return {
        name
        for cls in (_ScheduleParams, _StepParams)
        for name, _member in inspect.getmembers(cls, callable)
        if name.startswith("set")
    }


def device_types() -> set[str]:
    """Every Device Type ATE registers, taken from the drivers that serve them."""
    import ate.devices

    names = set()
    for module_info in pkgutil.iter_modules(ate.devices.__path__):
        module = importlib.import_module(f"ate.devices.{module_info.name}")
        for member in vars(module).values():
            if isinstance(member, type) and issubclass(member, Driver) and member is not Driver:
                names.add(member.name)
    return names


def run_fields() -> set[str]:
    """The fields ``ATE.run`` describes."""
    return set(_RUN_FIELDS)


#: A group of code-derived names, and the manual each must appear in. 34b adds the config keys
#: read from ate/config.py for the admin manual; 34c adds module and seam names for the
#: developer manual. Same shape, same failure.
COVERAGE: list[tuple[str, str, Callable[[], set[str]]]] = [
    ("test-writer", "ATE table functions", ate_table_functions),
    ("test-writer", "enum tables and their members", enum_names),
    ("test-writer", "ScheduleParams and StepParams setters", builder_methods),
    ("test-writer", "Device Types", device_types),
    ("test-writer", "ATE.run fields", run_fields),
]


def missing_from(texts: Iterable[tuple[str, str]], names: Iterable[str]) -> list[str]:
    """The names that appear nowhere in ``texts``.

    A name counts when it appears as a whole word, so ``readTerm`` does not count as ``read``
    and prose that says the word counts.
    """
    remaining = set(names)
    for _label, text in texts:
        for name in list(remaining):
            if re.search(rf"\b{re.escape(name)}\b", text):
                remaining.discard(name)
    return sorted(remaining)


def test_no_name_the_code_defines_is_missing_from_its_manual() -> None:
    """Every ATE function, enum, setter, Device Type and run field is named in both manuals."""
    for manual_name, group, derive in COVERAGE:
        gone = missing_from(MANUALS[manual_name].texts(), derive())
        assert not gone, f"the {manual_name} manual is missing from {group}: {', '.join(gone)}"


def test_the_coverage_check_can_fail() -> None:
    """Show the check bites: the same names against a manual with one removed reports it.

    This is not a bug report; it is the proof that a manual quietly losing a function is caught."""
    texts = MANUALS["test-writer"].texts()
    names = ate_table_functions()
    assert "addEquipment" in names
    stripped = [(label, text.replace("addEquipment", "somethingElse")) for label, text in texts]
    assert missing_from(stripped, names) == ["addEquipment"], "removing a name must show up as missing"
    assert not missing_from(texts, names), "the manual as written has every name"


# --- Running the examples ---------------------------------------------------------------

@dataclass
class Example:
    """One manual example, the run the harness makes of it, and what the manual claims."""

    file: str
    schedule: str
    result: str
    end_reason: str
    #: Fully prefixed Step Number -> Result, for ordinary steps only.
    steps: dict[str, str]
    inputs: dict[str, Any] = field(default_factory=dict)
    bad_inputs: dict[str, Any] = field(default_factory=dict)
    choices: list[str] = field(default_factory=list)
    enable: dict[str, bool] = field(default_factory=dict)
    polls: dict[str, tuple[int, str]] = field(default_factory=dict)
    #: Substrings that must appear in a WARNING line the run records.
    warns: list[str] = field(default_factory=list)
    equipment: int = 0
    #: (phase, outcome) for every Action row, in the order they ran.
    actions: tuple[tuple[str, str], ...] = ()


EXAMPLES_RUN: list[Example] = [
    Example(
        file="first.tslua",
        schedule="Manual: first schedule",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS"},
    ),
    Example(
        file="steps.tslua",
        schedule="Manual: steps",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS", "2.": "PASS", "3.": "NOTRUN", "4.": "NOREPORT"},
        warns=["never reported"],
    ),
    Example(
        file="inputs.tslua",
        schedule="Manual: step inputs",
        result="PASS",
        end_reason="COMPLETED",
        steps={n: "PASS" for n in ("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.")},
        inputs={
            "Pick one": "Green",
            "Tick several": ["A", "B"],
            "Press a button": "RETEST",
            "Whole number": 42,
            "Decimal": 1.25,
            "Text with a pattern": "AB-1234",
            "Blocking prompt": "CONTINUE",
        },
        bad_inputs={"Whole number": 500, "Decimal": 9.0, "Text with a pattern": "not a part number"},
        polls={"POLL in a loop": (1, "STOP")},
    ),
    Example(
        file="actions.tslua",
        schedule="Manual: Actions",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS"},
        equipment=1,
        actions=(("pre", "ok"), ("post", "ok")),
    ),
    Example(
        file="retry.tslua",
        schedule="Manual: retry",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS", "2.": "PASS"},
        choices=["RETRY"],
    ),
    Example(
        file="results.tslua",
        schedule="Manual: every Result",
        result="FAIL",
        end_reason="COMPLETED",
        steps={
            "1.": "PASS", "2.": "FAIL", "3.": "ERROR", "4.": "SKIPPED",
            "5.": "NOTRUN", "6.": "FORCED_NOTRUN", "7.": "NOREPORT",
        },
        warns=["never reported"],
    ),
    Example(
        file="main.tslua",
        schedule="Manual: sub-schedules",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS", "2.1.": "PASS", "2.2.1.": "PASS", "3.1.": "NOTRUN", "3.2.1.": "NOTRUN"},
        # main.tslua declares no Actions of its own; the two channel instances each have a
        # Pre-test and Post-test Action, and the disabled header's instance is skipped, so the
        # run records the enabled instance's pair only.
        actions=(("pre", "ok"), ("post", "ok")),
    ),
    Example(
        file="modules.tslua",
        schedule="Manual: modules",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS", "2.": "PASS"},
    ),
    Example(
        file="tcp.tslua",
        schedule="Manual: TCP",
        result="PASS",
        end_reason="COMPLETED",
        steps={n: "PASS" for n in ("1.", "2.", "3.", "4.", "5.")},
        equipment=1,
        warns=["timed out after", "no reply from the instrument"],
    ),
    Example(
        file="udp.tslua",
        schedule="Manual: UDP",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS", "2.": "PASS"},
        warns=["datagram truncated"],
    ),
    Example(
        file="serial.tslua",
        schedule="Manual: SERIAL",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS"},
    ),
    Example(
        file="visa.tslua",
        schedule="Manual: VISA",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS"},
    ),
    Example(
        file="errors.tslua",
        schedule="Manual: Device errors",
        result="FAIL",
        end_reason="COMPLETED",
        steps={"1.": "PASS", "2.": "ERROR", "3.": "ERROR", "4.": "PASS"},
        warns=["nothing is listening", "the Device is gone"],
    ),
    Example(
        file="left_open.tslua",
        schedule="Manual: a Device left open",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS"},
        warns=["left open at end of run"],
    ),
    Example(
        file="logging.tslua",
        schedule="Manual: logging",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS", "2.": "PASS"},
    ),
    Example(
        file="sandbox.tslua",
        schedule="Manual: the sandbox",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS", "2.": "PASS"},
    ),
]


@pytest.fixture(scope="module")
def bench(tmp_path_factory):
    """The simulator peers and a config for the manual's Media Release, for the whole module.

    Same shape as ``examples/harness``: the peers bind ephemeral ports, their addresses go into
    the environment, and the shipped example files run unchanged. ``ATE_DEAD_PORT`` is a port
    nothing listens on, for the schedule that shows an unreachable Device.
    """
    import os

    peers = Peers()
    peers.start()
    env = {
        "ATE_SCPI_HOST": "127.0.0.1",
        "ATE_SCPI_PORT": str(peers.scpi.port),
        "ATE_UDP_HOST": "127.0.0.1",
        "ATE_UDP_PORT": str(peers.udp.port),
        "ATE_DEAD_PORT": str(dead_port()),
    }
    saved = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        work = tmp_path_factory.mktemp("manual-examples")
        yield work, write_config(work, EXAMPLES_DIR)
    finally:
        peers.stop()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _step_results(record: dict[str, Any]) -> dict[str, str]:
    """Step Number -> Result for ordinary steps; header rows have no Result."""
    return {node["step_number"]: node["result"] for node in record["nodes"] if node["kind"] == "step"}


@pytest.mark.parametrize("example", EXAMPLES_RUN, ids=lambda e: e.file)
def test_a_manual_example_runs_as_the_manual_says(bench, example: Example) -> None:
    """Run one example headless and check the Test Run record against what the manual says."""
    _work, config = bench
    record = run_schedule(
        config,
        example.schedule,
        inputs=example.inputs or None,
        bad_inputs=example.bad_inputs or None,
        choices=example.choices or None,
        enable=example.enable or None,
        polls=example.polls or None,
        release_dir=EXAMPLES_DIR,
    )
    run = record["run"]
    assert run["result"] == example.result, f"{example.file}: run result {run['result']}"
    assert run["end_reason"] == example.end_reason, f"{example.file}: end reason {run['end_reason']}"
    assert _step_results(record) == example.steps, f"{example.file}: step results"
    assert len(record["equipment"]) == example.equipment, f"{example.file}: Equipment rows"
    if example.actions:
        got = tuple((action["phase"], action["outcome"]) for action in record["actions"])
        assert got == example.actions, f"{example.file}: Actions recorded {got}"
    for want in example.warns:
        logged = [line["text"] for line in record["logs"] if line["level"] == "WARNING"]
        assert any(want in text for text in logged), f"{example.file}: no WARNING containing {want!r}"


def test_retry_records_two_attempts_and_the_operators_choice(bench) -> None:
    """The retry example's step has two Attempts, and the Operator's Retry is recorded on the first."""
    _work, config = bench
    record = run_schedule(config, "Manual: retry", choices=["RETRY"], release_dir=EXAMPLES_DIR)
    retried = next(n for n in record["nodes"] if n["step_number"] == "1.")
    attempts = [a for a in record["attempts"] if a["node_seq"] == retried["node_seq"]]
    assert len(attempts) == 2, f"expected two Attempts, got {len(attempts)}"
    assert attempts[0]["pass_fail"] == "FAIL" and attempts[0]["prompt_choice"] == "RETRY"
    assert attempts[1]["pass_fail"] == "PASS" and attempts[1]["prompt_choice"] is None
    assert retried["attempt_count"] == 2 and retried["result"] == "PASS"


def test_an_abort_ends_the_run_and_the_posttest_action_still_runs(bench) -> None:
    """Story 83: an abort stops the run (within 10 s, aiming at 0.5 s), the Post-test Action runs
    afterwards all the same, and every step that never ran records NOTRUN."""
    from ate.config import read_config
    from ate.run_session.headless import HeadlessRunSession, SessionState
    from PySide6.QtCore import QCoreApplication
    from tests._fixtures import ABORT_MUST_STOP_S, wait_for_aborted_run

    _work, config = bench
    session = HeadlessRunSession(read_config(config))
    states: list[SessionState] = []
    session.state_changed.connect(states.append)
    session.set_operator("Ada Lovelace")
    session.set_project("Manual examples")
    session.set_uut_type("Example board")
    session.set_uut_serial("EX-MANUAL-1")
    session.select_schedule("Manual: abort")
    session.start()

    app = QCoreApplication.instance() or QCoreApplication([])
    deadline = time.monotonic() + ABORT_MUST_STOP_S
    while SessionState.STARTED not in states and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert SessionState.STARTED in states, "the abort example never started its run"

    finished = threading.Event()
    session.state_changed.connect(lambda state: finished.set() if state is SessionState.FINISHED else None)
    t0 = time.monotonic()
    session.request_abort()
    elapsed = wait_for_aborted_run(app, finished, t0)

    run_uuid = session.run_uuid
    run = session.store.get_test_run(run_uuid)
    assert run["result"] == "FAIL" and run["end_reason"] == "ABORTED", f"run row: {run}"
    nodes = {node["step_number"]: node for node in session.store.nodes(run_uuid) if node["kind"] == "step"}
    assert nodes["1."]["result"] == "ERROR", "the step running when the abort landed records ERROR"
    assert nodes["1."]["attempt_count"] == 1
    assert nodes["2."]["result"] == "NOTRUN", "a step that never ran records NOTRUN"
    actions = session.store.actions(run_uuid)
    assert len(actions) == 1 and actions[0]["phase"] == "post" and actions[0]["outcome"] == "ok", actions
    logged = [line["text"] for line in session.store.log_lines(run_uuid)]
    assert any("post-test action ran after the abort" in text for text in logged), logged
    session.close()


def test_a_schedule_that_loads_bad_is_listed_as_broken() -> None:
    """``forbidden.tslua`` opens a Device while the file is loading, which the host refuses: the
    schedule is listed as broken, and the rest of its Media Release is unaffected."""
    from ate.catalog import list_schedule_rows

    rows = list_schedule_rows(EXAMPLES_DIR)
    broken = [row for row in rows if row.error]
    assert len(broken) == 1, f"expected one broken schedule, got {[(r.name, r.error) for r in broken]}"
    assert "only legal after the schedule has loaded" in broken[0].error
    listed = {row.name for row in rows} - {row.name for row in broken}
    ran = {example.schedule for example in EXAMPLES_RUN} | {"Manual: abort"}
    assert listed == ran, f"schedules listed but never run, or run but not listed: {listed ^ ran}"


def test_a_checksum_mismatch_is_recorded_and_warned(bench) -> None:
    """Change one sha256 in a scratch copy of the Test Definition: the file still loads, the run
    records the mismatch, and the Run log says so."""
    work, _config = bench
    release = work / "mismatch-release"
    shutil.copytree(EXAMPLES_DIR, release)
    atedef = json.loads((release / "manual.atedef").read_text(encoding="utf-8"))
    for entry in atedef["files"]:
        if entry["path"] == "first.tslua":
            entry["sha256"] = "0" * 64
    (release / "manual.atedef").write_text(json.dumps(atedef), encoding="utf-8", newline="\n")

    mismatch_dir = work / "mismatch-config"
    mismatch_dir.mkdir()
    config = json.loads(write_config(mismatch_dir, release).read_text(encoding="utf-8"))
    # A mismatch only asks when the config says "ask"; this run stores the bytes so the record can
    # be checked for the stored content as well as the warning.
    config["database"]["storeMismatchedFiles"] = "always"
    (mismatch_dir / "config.json").write_text(json.dumps(config), encoding="utf-8", newline="\n")
    record = run_schedule(mismatch_dir / "config.json", "Manual: first schedule", release_dir=release)
    row = next(f for f in record["run_files"] if f["path"] == "first.tslua")
    assert row["mismatch"], f"first.tslua recorded no mismatch: {row}"
    logged = [line["text"] for line in record["logs"] if line["level"] == "WARNING"]
    assert any("checksum mismatch for first.tslua" in text for text in logged), logged
    assert record["run"]["result"] == "PASS", "a mismatched file still loads, and the run still passes"


def test_the_test_definition_lists_every_example_file() -> None:
    """Every Lua file beside the manual's ``.atedef`` is listed by it, and only those files."""
    atedef = json.loads((EXAMPLES_DIR / "manual.atedef").read_text(encoding="utf-8"))
    listed = {entry["path"] for entry in atedef["files"]}
    on_disk = {
        p.relative_to(EXAMPLES_DIR).as_posix()
        for p in EXAMPLES_DIR.rglob("*")
        if p.suffix in (".tslua", ".lua")
    }
    assert listed == on_disk, f"Test Definition and directory disagree: {listed ^ on_disk}"
    hidden = {entry["path"] for entry in atedef["files"] if entry.get("hidden")}
    assert hidden == {"channel.tslua", "led.tslua"}, "only the Sub-schedules are hidden"
    for entry in atedef["files"]:
        computed = hashlib.sha256((EXAMPLES_DIR / entry["path"]).read_bytes()).hexdigest()
        assert entry["sha256"] == computed, f"{entry['path']} checksum is stale: run 'ate definition update'"


# --- The manuals quote the examples exactly --------------------------------------------

#: A listing in the Markdown manual opens with the file it came from:
#: ```lua:docs/manuals/examples/test-writer/first.tslua
LISTING_FENCE = re.compile(r"^```lua:(\S+)\s*$")


def markdown_listings(text: str) -> dict[str, str]:
    """Every fenced Lua block in the Markdown manual, keyed by the file it came from."""
    blocks: dict[str, str] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = LISTING_FENCE.match(lines[index])
        if match:
            path = match.group(1)
            body: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                body.append(lines[index])
                index += 1
            blocks[path] = "\n".join(body) + "\n"
        index += 1
    return blocks


def example_files(manual: Manual) -> list[Path]:
    """Every Lua file in the manual's example directory."""
    return sorted(p for p in MANUAL_EXAMPLES[manual.name].rglob("*") if p.suffix in (".tslua", ".lua"))


def test_every_example_is_quoted_exactly_in_the_markdown_manual() -> None:
    """No listing in the Markdown manual may differ from the file it came from."""
    manual = MANUALS["test-writer"]
    blocks = markdown_listings(manual.markdown.read_text(encoding="utf-8"))
    expected = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in example_files(manual)}
    assert set(blocks) == set(expected), (
        f"the Markdown manual quotes {sorted(set(blocks) - set(expected))} that are not examples, "
        f"and omits {sorted(set(expected) - set(blocks))}"
    )
    for path, body in expected.items():
        assert blocks[path] == body, f"{path}: the listing in {manual.markdown} differs from the file"


def test_every_example_is_included_by_the_latex_manual() -> None:
    """The LaTeX manual includes every example with \\lstinputlisting, so the PDF shows the file
    as it is rather than a retyped copy of it."""
    manual = MANUALS["test-writer"]
    included: set[Path] = set()
    for tex in manual.tex_parts():
        for line in tex.read_text(encoding="utf-8").splitlines():
            if "\\lstinputlisting" in line:
                argument = line.split("{", 1)[1].rsplit("}", 1)[0]
                included.add((tex.parent / argument).resolve())
    expected = {p.resolve() for p in example_files(manual)}
    assert included == expected, (
        f"the LaTeX manual includes {sorted(str(p) for p in included - expected)} that are not examples, "
        f"and omits {sorted(str(p) for p in expected - included)}"
    )
