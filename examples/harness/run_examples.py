"""Seam-1 harness for the example Media Release: run every visible schedule headless.

Runs the Test Schedules in ``examples/media-release/`` through the headless run session
(config -> catalog -> RunEngine), answering every Step Input and every Stop-on-Fail prompt
from the script, and returns each Test Run record read back from the store.

Run it from a source checkout:

    QT_QPA_PLATFORM=offscreen python -m examples.harness.run_examples
    QT_QPA_PLATFORM=offscreen python examples/harness/run_examples.py

The assertions live in ``examples/harness/test_examples.py``, which the regular pytest suite
collects (``testpaths`` includes ``examples``), so CI runs this harness too.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[2] / "src"  # so a bare checkout can import `ate`
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from PyQt6.QtCore import QCoreApplication  # noqa: E402

from ate.catalog import list_schedules  # noqa: E402
from ate.run_session.headless import HeadlessRunSession, SessionState  # noqa: E402

RELEASE_DIR = Path(__file__).resolve().parents[1] / "media-release"

# One entry per headless run: the schedule to pick and the Operator's scripted answers. `inputs`
# maps the n in ATE.input(n) to the value supplied, `choices` answers each Stop-on-Fail prompt in
# order. `run_all` checks the schedules against the catalog, so a new example schedule has to be
# added here or the harness fails.
RUNS: list[dict[str, Any]] = [
    {"key": "basics", "schedule": "Example basics", "inputs": {1: 1.5}},
    {"key": "results", "schedule": "Report every Result"},
    {"key": "continue", "schedule": "Stop on fail", "choices": ["CONTINUE"]},
    {"key": "abort", "schedule": "Stop on fail", "choices": ["ABORT"]},  # same schedule, stopped
]


def visible_schedules() -> list[str]:
    """Every visible Test Schedule in the example Media Release, in scan order."""
    return [name for name, _description, _media in list_schedules(RELEASE_DIR)]


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
    choices: list[str] | None = None,
    operator: str = "Ada Lovelace",
    project: str = "Example project",
    uut_type: str = "Example board",
    uut_serial: str = "EX-0001",
) -> dict[str, Any]:
    """Run one schedule headless and return its record: run row, nodes, attempts, logs, inputs, Equipment."""
    session = HeadlessRunSession(config)
    reports: list[tuple[str, str]] = []
    session.report_written.connect(lambda html, pdf: reports.append((html, pdf)))

    session.set_operator(operator)
    session.set_project(project)
    session.set_uut_type(uut_type)
    session.set_uut_serial(uut_serial)
    session.select_schedule(schedule)
    engine = session.start()

    if inputs:
        operator_input = engine.operator_input

        def _supply(_seq: int, _attempt: int, index: int) -> None:
            operator_input.supply_input(index - 1, inputs[index])

        session.input_asked.connect(_supply)

    answers = list(choices or [])

    def _answer(_seq: int, _attempt: int) -> None:
        session.choose(answers.pop(0) if answers else "CONTINUE")

    session.failure_prompt.connect(_answer)

    app = QCoreApplication.instance() or QCoreApplication([])
    if not _pump(app, lambda: session._state == SessionState.FINISHED):
        raise TimeoutError(f"schedule '{schedule}' never finished")
    _pump(app, lambda: reports, timeout=10.0)  # let the report and its PDF helper finish

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
    }


def run_all(tmp_dir: Path, *, stub_pdf: bool = False) -> dict[str, dict[str, Any]]:
    """Run every visible example schedule with its scripted answers; one record per run, keyed by `key`."""
    planned = {entry["schedule"] for entry in RUNS}
    missing = set(visible_schedules()) - planned
    if missing:
        raise AssertionError(f"example schedules not covered by the harness: {sorted(missing)}")
    config = write_config(tmp_dir)
    if stub_pdf:
        _stub_pdf_helper()
    return {entry["key"]: run_schedule(config, entry["schedule"],
                                      **{k: v for k, v in entry.items() if k not in ("key", "schedule")})
            for entry in RUNS}


def _stub_pdf_helper() -> None:
    """Stand in for the QWebEngine PDF helper so a bare run needs no browser process."""
    import os
    from ate.report import pdf_job

    def command(_html: str, pdf: str, _paper: str = "A4"):
        return sys.executable, ["-c", "import sys; open(sys.argv[1], 'wb').write(b'%PDF-1.4 stub')", pdf], dict(os.environ)

    pdf_job.pdf_helper_command = command


def main() -> int:
    _stub_pdf_helper()
    with tempfile.TemporaryDirectory() as tmp:
        records = run_all(Path(tmp))
    for record in records.values():
        run = record["run"]
        steps = [n for n in record["nodes"] if n["kind"] == "step"]
        print(f"{run['schedule_name']:>18}  {run['result']:<4} {run['end_reason']:<15} "
              f"{len(steps)} steps  {len(record['equipment'])} equipment  {len(record['logs'])} log lines")
    bad = [name for name, record in records.items() if record["run"] is None]
    if bad:
        print(f"FAILED: no run record for {bad}", file=sys.stderr)
        return 1
    print(f"ran {len(records)} headless runs of {len(visible_schedules())} example schedules")
    return 0


if __name__ == "__main__":
    sys.exit(main())
