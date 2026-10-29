"""Seam-1 harness for the example Media Release: run every visible schedule headless.

Runs the Test Schedules in ``examples/media-release/`` through the headless run session
(config -> catalog -> RunEngine), answering every Step Input and every Stop-on-Fail prompt from
the script, and returns each Test Run record read back from the store.

Run it from a source checkout:

    QT_QPA_PLATFORM=offscreen python -m examples.harness.run_examples
    QT_QPA_PLATFORM=offscreen python examples/harness/run_examples.py

The assertions live in ``examples/harness/test_examples.py``, which the regular pytest suite
collects (``testpaths`` includes ``examples``), so CI runs this harness too. Under pytest the PDF
helper is already replaced by the session-wide stub in ``conftest.py``, so this harness never
patches it there; the CLI path patches it for the duration of the run only.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[2] / "src"  # so a bare checkout can import `ate`
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from PyQt6.QtCore import QCoreApplication  # noqa: E402

from ate.catalog import (  # noqa: E402
    include_map,
    list_schedules,
    module_map,
    resolve_schedule_definition,
    resolve_schedule_path,
)
from ate.engine.lua_host import LuaHost  # noqa: E402
from ate.run_session.headless import HeadlessRunSession, SessionState  # noqa: E402

RELEASE_DIR = Path(__file__).resolve().parents[1] / "media-release"

# One entry per headless run: the schedule to pick and the Operator's scripted answers.
#   inputs      - the value for the n in ATE.input(n), supplied when that input is asked.
#   bad_inputs  - tried first, for the same n, and must be refused by the declaration's own range
#                 check (packet 10a): the harness records the ValueError rather than swallowing it.
#   choices     - the answer to each Stop-on-Fail prompt, in order. A prompt with no scripted choice
#                 left is a failure: a schedule that pauses when it claims not to is a bug, not a
#                 question to answer.
#   enable      - steps the Operator turns on or off before the run starts (packet 11a).
# `run_all` checks the schedules against the catalog, so a new example schedule has to be added
# here or the harness fails.
RUNS: list[dict[str, Any]] = [
    {"key": "basics", "schedule": "Example basics", "inputs": {1: 1.5}, "bad_inputs": {1: 99.0}},
    {"key": "results", "schedule": "Report every Result"},
    {"key": "continue", "schedule": "Stop on fail", "choices": ["CONTINUE", "CONTINUE"]},
    {"key": "abort", "schedule": "Stop on fail", "choices": ["ABORT"]},
    # The same schedule again, with its disabled-by-default step enabled by the Operator.
    {"key": "basics_enabled", "schedule": "Example basics", "inputs": {1: 1.5},
     "enable": {"Optional check": True}},
]


def visible_schedules() -> list[str]:
    """Every visible Test Schedule in the example Media Release, in scan order."""
    return [name for name, _description, _media in list_schedules(RELEASE_DIR)]


def step_seq(schedule: str, step_name: str) -> int:
    """The runner seq of a step by name, from the same expanded tree the Steps stage previews."""
    definition = resolve_schedule_definition(RELEASE_DIR, schedule_name=schedule)
    if definition is None:
        raise AssertionError(f"no Test Definition covers schedule {schedule!r}")
    path = resolve_schedule_path(definition, schedule)
    host = LuaHost(path, modules=module_map(definition), includes=include_map(definition))
    for seq, node in enumerate(host.tree):
        if not node.is_header and node.name == step_name:
            return seq
    raise AssertionError(f"schedule {schedule!r} has no step named {step_name!r}")


def write_config(tmp_dir: Path) -> Path:
    """A config that points at the example release and a scratch database under ``tmp_dir``."""
    config = {
        "format": 1,
        "station": {"name": "BENCH-EXAMPLE", "uuid": "11111111-2222-3333-4444-555555555555"},
        "database": {"type": "sqlite", "path": str(tmp_dir / "ate.db")},
        "reports": {"directory": str(tmp_dir / "reports")},
        "testDefinitions": {"directories": [str(RELEASE_DIR)]},
    }
    path = tmp_dir / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _pump(app: QCoreApplication, done: Any, timeout: float = 15.0) -> bool:
    """Process events until ``done()`` is true (or the timeout passes)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if done():
            return True
        time.sleep(0.005)
    return False


def run_schedule(
    config: Path,
    schedule: str,
    *,
    inputs: dict[int, Any] | None = None,
    bad_inputs: dict[int, Any] | None = None,
    choices: list[str] | None = None,
    enable: dict[str, bool] | None = None,
    operator: str = "Ada Lovelace",
    project: str = "Example project",
    uut_type: str = "Example board",
    uut_serial: str = "EX-0001",
) -> dict[str, Any]:
    """Run one schedule headless and return its record: run row, nodes, attempts, logs, inputs, Equipment, reports."""
    session = HeadlessRunSession(config)
    reports: list[tuple[str, str]] = []
    session.report_written.connect(lambda html, pdf: reports.append((html, pdf)))
    states: list[SessionState] = []
    session.state_changed.connect(states.append)  # the public signal; never read session._state

    session.set_operator(operator)
    session.set_project(project)
    session.set_uut_type(uut_type)
    session.set_uut_serial(uut_serial)
    session.select_schedule(schedule)
    # A selection clears the pending toggles, so the Operator's step overrides go on after it
    # (packet 11a: toggle_node is refused while a run is in progress, so this is the idle window).
    for name, on in (enable or {}).items():
        session.toggle_node(step_seq(schedule, name), on)
    engine = session.start()

    refused: list[str] = []
    bad_tried: set[int] = set()

    def _supply(node_seq: int, attempt_no: int, index: int) -> None:
        # Answer only when the run asks (packet 10a): `input_asked` fires exactly when the step's
        # ATE.input(n) call blocks. First try the out-of-range value: the session must refuse it
        # with a ValueError, and the step stays waiting for the real answer.
        if index in (bad_inputs or {}) and index not in bad_tried:
            bad_tried.add(index)
            try:
                session.supply_input(node_seq, attempt_no, index, bad_inputs[index])
            except ValueError as exc:
                refused.append(f"input {index}: {bad_inputs[index]!r} refused: {exc}")
        session.supply_input(node_seq, attempt_no, index, (inputs or {})[index])

    if inputs:
        session.input_asked.connect(_supply)

    answers = list(choices or [])
    unexpected: list[tuple[int, int]] = []

    def _answer(node_seq: int, attempt_no: int) -> None:
        if not answers:
            # A prompt with no scripted choice means the schedule paused when it said it would not:
            # a failure, not a question to answer. Record it and stop the run cleanly (the pause
            # blocks with no timeout, so it must be answered to end the run).
            unexpected.append((node_seq, attempt_no))
            session.choose("ABORT")
            return
        session.choose(answers.pop(0))

    session.failure_prompt.connect(_answer)

    app = QCoreApplication.instance() or QCoreApplication([])
    if not _pump(app, lambda: SessionState.FINISHED in states):
        raise TimeoutError(f"schedule '{schedule}' never finished")
    if not _pump(app, lambda: reports, timeout=10.0):
        raise TimeoutError(f"schedule '{schedule}' finished without writing its report")

    store = session.store
    run_uuid = session.run_uuid
    return {
        "run": store.get_test_run(run_uuid),
        "nodes": store.nodes(run_uuid),
        "attempts": store.attempts(run_uuid),
        "logs": store.log_lines(run_uuid),
        "equipment": store.equipment(run_uuid),
        "input_values": store.input_values(run_uuid),
        "media_releases": store.media_releases(run_uuid),
        "run_files": store.run_files(run_uuid),
        "reports": reports,
        "refused_inputs": refused,
        "unexpected_prompts": unexpected,
    }


def run_all(tmp_dir: Path) -> dict[str, dict[str, Any]]:
    """Run every visible example schedule with its scripted answers; one record per run, keyed by `key`."""
    planned = {entry["schedule"] for entry in RUNS}
    missing = set(visible_schedules()) - planned
    if missing:
        raise AssertionError(f"example schedules not covered by the harness: {sorted(missing)}")
    config = write_config(tmp_dir)
    return {entry["key"]: run_schedule(config, entry["schedule"],
                                      **{k: v for k, v in entry.items() if k not in ("key", "schedule")})
            for entry in RUNS}


@contextmanager
def pdf_stub():
    """Stand in for the QWebEngine PDF helper, then put the real one back.

    Only the CLI path needs this: under pytest ``conftest.py``'s autouse fixture already replaces
    ``pdf_helper_command`` for every test, so the harness patches nothing there.
    """
    from ate.report import pdf_job
    from tests._fixtures import WRITES_PDF, fake_pdf_helper

    original = pdf_job.pdf_helper_command
    pdf_job.pdf_helper_command = fake_pdf_helper(WRITES_PDF)
    try:
        yield
    finally:
        pdf_job.pdf_helper_command = original


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp, pdf_stub():
        records = run_all(Path(tmp))
    for key, record in records.items():
        run = record["run"]
        steps = [n for n in record["nodes"] if n["kind"] == "step"]
        print(f"{key:>15}  {run['schedule_name']:>18}  {run['result']:<4} {run['end_reason']:<15} "
              f"{len(steps)} steps  {len(record['equipment'])} equipment  {len(record['logs'])} log lines")
    bad = [key for key, record in records.items() if record["run"] is None]
    if bad:
        print(f"FAILED: no run record for {bad}", file=sys.stderr)
        return 1
    print(f"ran {len(records)} headless runs of {len(visible_schedules())} example schedules")
    return 0


if __name__ == "__main__":
    sys.exit(main())
