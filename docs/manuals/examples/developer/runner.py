#!/usr/bin/env python3
"""Drive one Test Run with no display, and print its record as JSON.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/developer/runner.py <config.json> "<schedule name>"

A headless run is a ``HeadlessRunSession`` and an event loop: the session's signals are queued, so
they only arrive while some thread pumps events. Nothing here imports a widget. The PDF helper is
replaced with a skip so the run finishes on a machine with no browser to render it; the HTML Test
Report is still written.

Answers are given from the script: ``--input STEP NAME=VALUE`` for every Step Input the schedule
declares, ``--choice`` for each Stop-on-Fail pause in the order the pauses come. A run nobody
answers never ends, so a schedule with inputs needs these flags or this script waits out its
25-second deadline and reports the run as it stands.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/developer -> the repository
try:
    import ate  # noqa: F401  # already importable: run from the repository root, or with PYTHONPATH
except ImportError:
    for _path in (_ROOT / "src", _ROOT):
        if str(_path) not in sys.path:
            sys.path.insert(0, str(_path))

from PySide6.QtCore import QCoreApplication  # noqa: E402
from ate.config import read_config  # noqa: E402
from ate.report.pdf import PDF_SKIPPED  # noqa: E402
from ate.run_session.headless import HeadlessRunSession, SessionState  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(prog="runner.py", description=__doc__)
    parser.add_argument("config", help="the config.json of the Station to run against")
    parser.add_argument("schedule", help="the Test Schedule name, as the Test Definition lists it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"))
    parser.add_argument("--abort-after", type=float, default=None, help="request an abort this many seconds in")
    args = parser.parse_args()

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        value = value.strip()
        try:
            inputs[name.strip()] = int(value)
        except ValueError:
            inputs[name.strip()] = float(value) if value.replace(".", "", 1).replace("-", "", 1).isdigit() else value

    session = HeadlessRunSession(read_config(Path(args.config)), pdf_choice=lambda _html: PDF_SKIPPED)
    states: list[SessionState] = []
    session.state_changed.connect(states.append)
    # A Stop-on-Fail pause blocks the worker until somebody answers it; a script is the only
    # somebody here, so every pause gets the scripted answer in order.
    answers = list(args.choice)
    session.failure_prompt.connect(lambda node_seq, attempt_no: session.choose(answers.pop(0) if answers else "CONTINUE"))
    session.set_operator("runner.py")
    session.set_project("Developer manual worked example")
    session.set_uut_type("Example board")
    session.set_uut_serial("DEV-34C")
    session.select_schedule(args.schedule)
    session.start()

    app = QCoreApplication.instance() or QCoreApplication([])
    started = time.monotonic()
    deadline = started + 25.0
    while SessionState.FINISHED not in states and time.monotonic() < deadline:
        if args.abort_after is not None and time.monotonic() - started >= args.abort_after:
            session.request_abort()
            args.abort_after = None  # request once, not every turn of the loop
        app.processEvents()
        time.sleep(0.02)

    run = session.store.get_test_run(session.run_uuid)
    print(json.dumps({
        "result": run["result"],
        "end_reason": run["end_reason"],
        "steps": {n["step_number"]: n["result"] for n in session.store.nodes(session.run_uuid) if n["kind"] == "step"},
        "logs": [line["text"] for line in session.store.log_lines(session.run_uuid)],
        "levels": [line["level"] for line in session.store.log_lines(session.run_uuid)],
    }))
    session.close()
    return 0 if SessionState.FINISHED in states else 1


if __name__ == "__main__":
    sys.exit(main())
