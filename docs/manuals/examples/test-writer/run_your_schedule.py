#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``. An answer the step's own declaration rejects (a whole number
given as ``42.0``) exits 1 too, naming the value and the rule it broke.

``--poll`` clicks a POLL button by Step Name. A step with more than one POLL input needs the
input number as well: ``--poll "Step name=2=STOP"`` clicks the second input's button.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME[=N]=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; add =N for the "
                             "Nth POLL input of that step (default 1); repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="give up on a run that never finishes after this many seconds (default 60)")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        number, _, button = value.partition("=")
        if button:
            try:
                number = int(number)
            except ValueError:
                print(f"--poll {item!r}: the POLL input number must be an integer, "
                      f"as in STEP NAME[=N]=BUTTON", file=sys.stderr)
                return 1
        else:
            number, button = 1, value
        polls[name.strip()] = (number, button.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                    timeout=args.timeout,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
