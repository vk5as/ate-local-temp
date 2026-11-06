# Changing the ATE application: a manual for an agent

ATE is a PySide6 desktop application that runs Lua Test Schedules against Devices and records every
Test Run in a SQLite database. This file is the developer manual, and it works alone: you do not need
the PDF, and `tests/test_manuals.py` fails if this file stops naming a module, a signal, a table or a
driver that the code has.

Order of work: read the rule in the next section, find the module in the map, make the change, run the
test that owns that behaviour, then update the documents that follow the change.

## What is proven, and what is not

Proven on this machine, by the tests named in this file:

- the suite: 1591 tests on Linux with `QT_QPA_PLATFORM=offscreen` — 1586 pass, 5 are skipped (the
  PyInstaller gate, the Linux package gate, the two container gates, the review-screenshot test), in
  336.65 s;
- that every signal the engine and the session emits is delivered on the main thread
  (`tests/test_signal_threads_32a.py`);
- that an abort ends a Device call on every Device Type, and how long that takes
  (`tests/test_device_abort_32b.py`);
- that the two worked examples at the end of this file work: they are executed against a temporary copy
  of `src/` by `tests/test_manuals.py`.

Not proven, and not claimed: real hardware (every Device test talks to a loopback peer, a pseudo-terminal
or pyvisa-py behind a `TCPIP0::127.0.0.1` resource); real Windows sessions, which only GitHub Actions has
ever run; a Chromium-rendered PDF, stubbed by `conftest.py` except in the four tests that must prove a PDF
has text. The container smokes did run here — see the gates section for the scratch-space catch.

Where a spec and the code disagree, this file follows the code and the disagreement is listed at the end.

## The rule that comes first

Nothing under `src/ate/engine/` or `src/ate/run_session/` imports widgets.
`tests/test_architecture_rules.py` parses every `.py` in those two packages with `ast` and fails on
`QtWidgets`, `uic`, `QtUiTools` or any module whose name contains `widgets`; the two packages are named
in `_GUARDED_DIRS`, and the guard is itself tested with a planted import so the pattern cannot rot.
`src/ate/dialogs.py` imports `QTimer` and `src/ate/report/pdf.py` imports `QApplication` inside a
function; neither runs on the worker, and neither is guarded.

Two reasons, both of which bite: a widget belongs to the thread that created it, and the worker is a
`QThread`; and a CI runner has no display, so a Test Run has to be drivable with no widgets at all.

The worker reaches the screen only through queued signals. A queued signal's slot runs on the thread
that owns the receiver object, not the thread that emitted. If you add a signal, add it to
`tests/test_signal_threads_32a.py`, which drives a real run with a Stop-on-Fail pause, a POLL and a
blocking input and records the thread each slot ran on.

## The module map

| File | What is in it |
|---|---|
| `src/ate/__init__.py` | the package itself: the version only, no engine and no widgets imported |
| `src/ate/__main__.py` | how `python -m ate` starts, including the PDF helper subprocess |
| `src/ate/_version.py` | the version from the git tag, computed once so build and run agree |
| `src/ate/manuals.py` | where the running install keeps the manual PDFs — the path `ate --help` prints |
| `src/ate/app.py` | the application shell: bare `ate` opens the GUI and wires the launch |
| `src/ate/catalog.py` | reads and validates a `.atedef`, lists schedules, resolves `require` chains |
| `src/ate/cli.py` | the `ate` command: `setup`, `db`, `definition`, and hidden `_pdf`/`_selftest` |
| `src/ate/config.py` | reads and validates the application config; `_SCHEMA` is the key list |
| `src/ate/devices/__init__.py` | the Device drivers and the per-run registry |
| `src/ate/devices/abort.py` | how a Device call gives way to a run abort (`POLL_SECONDS = 0.05`) |
| `src/ate/devices/registry.py` | `Driver`, `validate_params`, `DeviceRegistry`: open, close, snapshot |
| `src/ate/devices/serial.py` | the SERIAL driver: pyserial ports on the same buffered stream |
| `src/ate/devices/stream.py` | the buffered stream TCP, SERIAL and UDP share; `READ_FIELDS`, `WRITE_FIELDS` |
| `src/ate/devices/tcp.py` | the TCP driver, with the background reader and the receive buffer cap |
| `src/ate/devices/tracing.py` | one DEBUG line per Device operation (`DEFAULT_TRACE_MAX_BYTES = 256`) |
| `src/ate/devices/udp.py` | the UDP driver: a reader too, but a queue of whole datagrams instead of a byte buffer |
| `src/ate/devices/visa.py` | the VISA driver: read on demand, no reader |
| `src/ate/dialogs.py` | every modal ATE opens, and `dismiss_if_headless` for the ones with no Operator |
| `src/ate/engine/__init__.py` | the run engine package: flow control and the Lua host |
| `src/ate/engine/lua_host.py` | the sandbox, the `ATE` table, the schedule builders, the Device registry |
| `src/ate/engine/operator_input.py` | `OperatorInput`: the channel a blocking `ATE.input(n)` waits on |
| `src/ate/engine/run_engine.py` | `RunEngine`: the QThread worker, its nine signals, the abort flag |
| `src/ate/engine/step_inputs.py` | parsing and validating Step Input declarations |
| `src/ate/errors.py` | the exception types ATE raises |
| `src/ate/exception_log.py` | the hook that puts an uncaught exception in the log and in front of the Operator |
| `src/ate/history/__init__.py` | the UI-free queries over past Test Runs |
| `src/ate/history/session.py` | `HistorySession`: finds past runs, no widgets |
| `src/ate/log.py` | the application log: `open_app_log`, `AppLog.log` |
| `src/ate/lua/__init__.py` | the bundled Lua modules, reachable as `require("ate.devices")` |
| `src/ate/permissions.py` | the Station's group, ownership and ACLs; `RecordingPermissions` for tests |
| `src/ate/report/__init__.py` | the Test Report package |
| `src/ate/report/html.py` | writes the self-contained HTML report from a recorded run |
| `src/ate/report/log_export.py` | exports a finished run's Run log as CSV, JSON Lines or text |
| `src/ate/report/pdf.py` | HTML to PDF through a helper process, and the sandbox decision |
| `src/ate/report/pdf_job.py` | the session-owned `QProcess` that runs the helper |
| `src/ate/run_session/__init__.py` | the run session package: the object a layout binds to |
| `src/ate/run_session/headless.py` | `HeadlessRunSession`: states, signals, inputs, the close order |
| `src/ate/setup.py` | `ate setup`: directories, config and permissions for every install format |
| `src/ate/storage/__init__.py` | the storage package: the only place ATE talks SQL |
| `src/ate/storage/_uuid.py` | UUIDv7 run keys without `uuid.uuid7` |
| `src/ate/storage/backup.py` | `backup_for_migration` and `migrate_database`: the copy taken before a migration, and the migration both `ate db migrate` and `ate setup` call |
| `src/ate/storage/schema.py` | the Test Run record tables and `SCHEMA_VERSION` |
| `src/ate/storage/store.py` | `RunStore`: every write, the batching, `_log_seq`, the launch check |
| `src/ate/ui/__init__.py` | layouts and screens: the only place `QtWidgets` is allowed |
| `src/ate/ui/export_log_dialog.py` | the Run log export dialog: Level/Source/Owner and Save As |
| `src/ate/ui/history_screen.py` | the History screen: the table of past runs and its summary panel |
| `src/ate/ui/run_widgets.py` | the widgets both Run-screen layouts share |
| `src/ate/ui/theme.py` | the palette tokens, the platform fonts, the painted badges and the one application stylesheet |
| `src/ate/ui/staged_run_screen.py` | the staged Run screen: the vertical stack of stages |
| `src/ate/ui/three_pane_run_screen.py` | the three-pane Run screen: setup pane, step table, log |

## The run engine

`RunEngine` is a `QThread`. Its `run()` is a guard around `_execute()`: an exception that escapes is
handled by `_end_unexpectedly(trace)`, which records the run `INTERRUPTED`/`FAIL`, prints to stderr,
sends the message to the application log, closes every open Device and emits `FINISHED` anyway. Each of
those steps is attempted separately, because the exception may have come from the store itself.
Post-test Actions do not run after an unexpected end.

A started engine keeps itself alive, because a `QThread` destroyed while running makes Qt abort the
process (`QThread: Destroyed while thread is still running`: SIGABRT on Linux, 0xC0000409 on Windows,
with nothing printed). `RunEngine.start` adds the engine to `_live_engines` and connects `finished` to
`_forget_ended_worker`, which waits for the thread and then drops it from the set.
`stop_live_engines(timeout_ms=5000)` aborts and joins every worker still in the set, and it is
registered with `atexit`. If it returns a non-empty list, Qt is about to kill the process.

Two things break this by accident:

- parenting a `RunEngine` to a widget. The engine is still in `_live_engines`, and the widget can be
  deleted underneath a live worker;
- calling `engine.wait()` on the main thread without first calling `request_abort()`. Use
  `stop_and_wait`, which sets the abort flag and then waits. `wait()` releases the GIL, so the worker
  keeps working while you wait.

`_execute()` works in this order: build a `LuaHost` for the schedule (which loads, validates, expands
Sub-schedules and registers the drivers); insert the `test_run` row and emit `run_uuid_reported`; run
the Pre-test Actions in order, each recorded in `action` (a Pre-test Action that raises ends the run
`PRETEST_FAILED` with every step NOTRUN); walk the step list, checking the abort flag at every boundary;
run each enabled step's Action once per Attempt, one `attempt` row each; pause on a Stop-on-Fail failure
until the Operator answers; run the Post-test Actions, which run even after an abort (spec story 84); record
the Media Release chain and every file used, close open Devices, and write the result and End Reason.

### The nine signals `RunEngine` declares

| Signal | Arguments | When it is emitted |
|---|---|---|
| `run_uuid_reported` | the run key | as soon as the `test_run` row exists; also on an unexpected end |
| `state_changed` | `RunStatus` | `IDLE` to `RUNNING` to `FINISHED` |
| `input_asked` | node_seq, attempt_no, index | a blocking `ATE.input(n)` has started waiting |
| `step_finished` | node_seq | that step's Attempts are done, so its POLL inputs close |
| `failure_prompt` | node_seq, attempt_no | a Stop-on-Fail failure is waiting for a choice |
| `prompt_answered` | node_seq, attempt_no, choice | the pause is over: when answered, and again when the worker moves on |
| `action_started` | `"pre"`/`"post"`, completed, total | a Pre- or Post-test Action is about to run |
| `step_started` | node_seq | an Attempt is about to run, again on each Retry |
| `log_lines_recorded` | run_uuid | a batch of log rows just landed in the store, so a pane can paint them while the run is still going (packet 40a) |

`prompt_answered` is emitted twice on purpose (packet 25b): the screen clears its prompt as soon as the
answer is given, and a `failure_prompt` still queued behind it must not leave an answered prompt painted.

`Signal(int)` in PySide6 is a C `int`. `emit(2**31)` with a slot connected returns normally and the slot
receives -2147483648 with a `RuntimeWarning`; `Signal(str)` turns a non-string into `''`. A slot cannot
see the bad value, so validate on the sending side and declare `Signal(object)` for anything that could
exceed `2**31 - 1`.

### What blocks the worker, and what an abort can end

- A Stop-on-Fail pause blocks with no timeout until the Operator answers. It now reads the abort flag
  (packet 32b found that a run-level abort arriving before the pause used to be lost). `choose` returns
  `None` once the flag is set.
- `ATE.input(n)` waits in `OperatorInput.get_raw`, which takes the abort flag and gives up.
- `ATE.sleep(seconds)` waits in slices, so an abort lands within about 50 ms.
- A busy Lua loop is interrupted by the count hook, `debug.sethook(check_abort, "", 1000)`, which calls
  `aborted()` every 1000 instructions; `pcall` and `xpcall` are wrapped to re-raise it.
- A Device call is interrupted by `src/ate/devices/abort.py`, which runs the call in slices of
  `POLL_SECONDS` and checks the flag between them.

Spec story 83 allows two exceptions to the 10 s bound, and the suite measures them instead of trying to
fix them: a command a step runs with `os.execute`, which the abort cannot end, and a busy loop
inside a coroutine, where the count hook never ticks
(`tests/test_manuals.py`: `test_a_command_in_a_step_holds_the_abort_until_it_finishes`,
`test_a_coroutine_loop_runs_to_completion_after_an_abort`). Neither is forbidden in a schedule.
`os.exit` is nil in a schedule, so it is not an exception at all: calling it in a step raises and the
step is recorded as an ERROR Attempt.

Three things cannot be interrupted: a SQLite commit already in progress, one Lua C call such as a
pattern match on a huge string, and `set_visa_attribute` or `resource.timeout` on the worker.

Allowed on the worker: step and Action closures, synchronously (`run_engine.py`'s docstring assumes
this --- the sandbox does not remove coroutines, but spec story 83 records a busy loop in one as an
exception to the abort bound, so `ATE.report` is attributed to a step by snapshotting the host's
report log before the closure and diffing it afterwards); store writes, each its own commit; Device
reads, writes, opens and closes through the registry; and the drains `_flush_device_warnings`,
`_flush_equipment` and `_record_files`.

Not allowed: touching a widget or importing `QtWidgets`; a loop that does not check the abort flag;
emitting into the engine from the main thread as if it were the worker. The main thread calls `choose()`
and `request_abort()`; it does not step the schedule.

One detail that misleads people reading log stamps: `record_log` stamps with `time.monotonic()` when the
line is recorded, and the drain maps it to wall-clock later. `log_line.at` is when the worker logged it,
not when it arrived.

## The run session

`HeadlessRunSession` (`src/ate/run_session/headless.py`) is what a screen binds to and what a headless
run uses. It is a `QObject`, imports no widgets, writes no SQL of its own, owns one engine at a time,
and forwards the engine's signals.

`SessionState`: `IDLE`, `SELECTING`, `STARTED`, `FINISHED`. The engine's `RunStatus` maps onto them by
`_map_run_status`; `STARTED` is announced on the calling thread before the worker starts, so a screen
can paint the run view before the first signal arrives.

### The ten signals `HeadlessRunSession` declares

| Signal | Arguments | Where it comes from |
|---|---|---|
| `state_changed` | `SessionState` | forwarded from the engine |
| `input_asked` | node_seq, attempt_no, index | forwarded from the engine |
| `input_answered` | node_seq, attempt_no, index, value | the session's own; the value is `object`, because a Step Input can be a number, a string or a list |
| `step_finished` | node_seq | forwarded from the engine |
| `failure_prompt` | node_seq, attempt_no | forwarded from the engine |
| `prompt_answered` | node_seq, attempt_no, choice | forwarded from the engine |
| `action_started` | `"pre"`/`"post"`, completed, total | forwarded from the engine |
| `step_started` | node_seq | forwarded from the engine |
| `report_written` | html_path, pdf_path | once per finished run; the second path is empty when there is no PDF |
| `log_appended` | run_uuid | forwarded from the engine's `log_lines_recorded` the moment log rows are in the store |

What the session enforces, not the screen:

- `start()` refuses to run with no schedule selected and refuses an empty Project.
- Before a run starts, `close_interrupted_runs` closes every run row with no `ended_at`, recording it
  `INTERRUPTED`/`FAIL` (spec rule 98).
- Stop on Fail resolves in one place: the Operator's explicit choice, otherwise the schedule's default
  (captured when the schedule was selected, `True` when unset).
- `supply_input` and `click_poll` check the step, the Attempt, the index and the value before the worker
  sees it. A bad value raises `ValueError` and is never recorded. `index` here is the 1-based `n` of
  `ATE.input(n)`; `OperatorInput` keys its channels from 0.
- `pause_pending()` is how a layout knows the run is paused. Use the session, not a layout's own prompt
  bar.

`close()` stops the worker, writes the HTML report if it is still owed, marks the session closed so a
queued `FINISHED` finds nothing to write, stops the PDF helper, closes the store, and closes the
application log only if the session opened its own. `__del__` stops the helper, because a session
dropped without `close()` leaves its `QProcess` to be destroyed by Qt mid-render. Do not close a store
while a worker is writing to it: `close()` calls `stop_and_wait()` first, and if the worker does not
stop within 5 s it logs an error rather than pretending.

Driving a run with no display is the shape the suite and `examples/harness/run_examples.py` use. The
developer manual's own runner, kept beside the examples, is that shape written out in full — it is
the script `tests/test_manuals.py` runs for the worked example in chapter 16:

```python:docs/manuals/examples/developer/runner.py
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
25-second deadline and reports the run as it stands. The exit code is 0 only when the session
reached ``FINISHED``, so a caller can tell a completed run from one it gave up on.
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
from ate.catalog import (  # noqa: E402
    include_map,
    module_map,
    resolve_schedule_definition,
    resolve_schedule_path,
)
from ate.config import read_config  # noqa: E402
from ate.engine.lua_host import LuaHost  # noqa: E402
from ate.report.pdf import PDF_SKIPPED  # noqa: E402
from ate.run_session.headless import HeadlessRunSession, SessionState  # noqa: E402


def _step_names(config, schedule: str) -> dict[int, str]:
    """``node_seq -> Step Name`` for one schedule, the same tree the Run screen shows.

    A step's row is written to the record only after its last Attempt, so while a step is waiting
    for an input the store has nothing to look up: the names come from the loaded schedule.
    """
    definition = resolve_schedule_definition(*config.test_definition_dirs, schedule_name=schedule)
    path = resolve_schedule_path(definition, schedule) if definition else None
    if path is None:
        raise SystemExit(f"no Test Definition covers schedule {schedule!r}")
    host = LuaHost(path, modules=module_map(definition), includes=include_map(definition))
    return {seq: node.name for seq, node in enumerate(host.tree) if not node.is_header}


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

    config = read_config(Path(args.config))
    names = _step_names(config, args.schedule)
    session = HeadlessRunSession(config, pdf_choice=lambda _html: PDF_SKIPPED)
    try:
        states: list[SessionState] = []
        session.state_changed.connect(states.append)
        # A Step Input is answered when the worker asks (packet 10a): `input_asked` carries the node, the
        # Attempt and the 1-based index of the declaration. `--input` is written with the Step Name, so
        # the name is looked up in the schedule's tree before the value is supplied.
        refused: list[str] = []

        def _supply(node_seq: int, attempt_no: int, index: int) -> None:
            name = names.get(node_seq)
            if name not in inputs:
                print(f"WARNING: step {name!r} asked for input {index} and no --input answers it", file=sys.stderr)
                return
            try:
                session.supply_input(node_seq, attempt_no, index, inputs[name])
            except ValueError as exc:
                # The step's declaration rejected the value. This slot runs in the worker thread, so an
                # uncaught ValueError would be a traceback and a step waiting for an answer nobody gives.
                refused.append(f"step {name!r} input {index}: {inputs[name]!r} refused: {exc}")
                print(f"ERROR: {refused[-1]}", file=sys.stderr)

        if inputs:
            session.input_asked.connect(_supply)
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
        while SessionState.FINISHED not in states and not refused and time.monotonic() < deadline:
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
            "inputs": [v["value"] for v in session.store.input_values(session.run_uuid)],
            "logs": [line["text"] for line in session.store.log_lines(session.run_uuid)],
            "levels": [line["level"] for line in session.store.log_lines(session.run_uuid)],
            "refused_inputs": refused,
        }))
    finally:
        # Closed whatever the run did, as the harness does: a session left open keeps the database
        # and the app log handle open, and the scratch directory it wrote into cannot be removed.
        session.close()
    return 0 if SessionState.FINISHED in states else 1


if __name__ == "__main__":
    sys.exit(main())
```

Queued signals only deliver when the receiving thread's event loop runs. No loop, no signals.

## Screens, layouts and dialogs

`src/ate/ui/` is the only place `QtWidgets` is allowed. There are two Run layouts
(`src/ate/ui/staged_run_screen.py` and `src/ate/ui/three_pane_run_screen.py`) sharing the widgets in
`src/ate/ui/run_widgets.py`, plus `src/ate/ui/history_screen.py` and
`src/ate/ui/export_log_dialog.py`. Both layouts bind to the session and never to the engine.

Every modal in `src/ate/dialogs.py` has a `dismiss_if_headless` path: with no Operator there is nobody
to answer, so a dialog that blocks a headless run must answer itself. That is why the launch layer
injects dialogs in tests (`tests/test_gui_launch_30a.py` drives `launch()` with the dialogs injected,
so no window is ever shown).

## Storage

`RunStore` (`src/ate/storage/store.py`) is the only storage interface in ATE; `src/ate/storage/schema.py`
is the only place a table is defined. Every insert is its own commit (the engine is autocommit), which
makes the record append-only and crash-safe.

| Table | What it holds |
|---|---|
| `test_run` | the run row: station, operator, project, UUT, schedule, result, end_reason, times |
| `node` | one row per runner node: header or step, step_number, kind, result, attempt_count |
| `attempt` | one row per Attempt: result, msg, times, retry count |
| `action` | one row per Pre-test or Post-test Action |
| `log_line` | one row per Run log line: level, source, text, owner, `seq` |
| `input_value` | one row per Step Input answer, the value as JSON text |
| `equipment` | one row per Equipment item declared with `ATE.addEquipment` |
| `media_release` | the Media Release chain the run resolved, with versions and checksums |
| `run_file` | every file the run used, its sha256 and whether it mismatched |
| `schedule_instance` | the top-level schedule and every Sub-schedule include of the run |

Batching: `_SQL_MAX_VARIABLES = 999` is SQLite's parameter limit, and `_rows_per_statement` is
`_SQL_MAX_VARIABLES // len(table.columns)`, so a multi-row insert is chunked to stay under it. A new
table with more columns gets a smaller chunk automatically.

`_log_seq` is the per-run counter that gives `log_line`, `action`, `input_value` and `equipment` their
`seq`, so `(run_uuid, seq)` stays unique within a run and the report's order is the emission order. A
new row type that is emitted inline with steps must take its `seq` from the same counter, or the report
will order it wrong.

The launch check, `check_database_at_launch`, compares `PRAGMA user_version` with `SCHEMA_VERSION` and
returns `NEEDS_MIGRATE`, `TOO_OLD`, `NOT_A_DATABASE`, `NOT_READABLE` or `OK`. `ate db status` prints the
version; `ate db migrate` creates or upgrades, after the copy.

## The Lua host and the sandbox

`src/ate/engine/lua_host.py` treats a Test Schedule as code it has to account for, not as an enemy to
defend against: a schedule is the Station's own trusted code. Each schedule load gets a fresh
`lupa` runtime, created with `register_builtins=False`, and then `_lockdown` shadows the file-loading
entry points before anything is registered:

- `dofile`, `loadfile` and `load` are set to `nil` (the Lua standard library still provides them, so they
  have to be shadowed explicitly);
- `python` is set to nil, so the host's Python is not reachable from a schedule;
- `os.exit` is set to nil (packet 35): a step that calls it gets an error, not an exit. `os.execute`
  and `os.getenv` stay; lupa's `os` is only `clock`, `date`, `difftime`, `execute`, `exit`, `getenv`,
  `remove`, `rename`, `setlocale`, `time`, `tmpname`, so `os.abort` was never there to remove;
- `require` is ATE's own, and `package.path` and `package.cpath` are emptied: only the whitelist from the
  Test Definition and the modules in `src/ate/lua/` (as `require("ate.devices")`) can be loaded;
- `debug` is reduced to `debug.traceback`. The full library reaches the instance loader through
  `debug.getregistry`, strips read-only locks with `debug.setmetatable`, and can remove the abort hook;
- each schedule instance, the top level and every include, gets its own global table built by a loader
  that captured the stock `load` before the lockdown removed it.

The abort hook is installed before the schedule runs: `debug.sethook(check_abort, "", 1000)`, and
`pcall`/`xpcall` are wrapped to re-raise `aborted` so a schedule's own error handling cannot swallow an
abort. `print` is redirected to `ATE.log(INFO, ...)`.

None of this is a defence against the person who wrote the schedule. `os.execute`, `os.remove` and
`os.rename` are reachable from a step: `os.execute("kill -9 $PPID")` ends ATE with the run row left
open — that command is a POSIX shell command, and a Windows Station's `os.execute` goes to `cmd.exe`,
which has no `kill` and no `$PPID` — and `os.remove` on the database takes the record away under a
running run. The lockdown buys traceability, not protection.

`_AteTable` is what a schedule sees. Every function is wrapped by `_isolate`, which turns a raised ATE
error into the `(false, message)` Device failure shape or an ERROR Attempt, naming the schedule file and
line. `ATE.run` is a read-only table built from `_RUN_FIELDS`; writing to it raises `ATE.run: read-only`,
and the engine fills it through `rawset` in `publish_run`. The enums (`Result`, `Log`, `DeviceType`,
`Parity`, `FlowControl`) are declared by `_declare_enums` as read-only tables.

`_context` is one of `load`, `pretest`, `posttest` or `step`, and a function that is only legal in one of
them raises when called in another: a Device opened while the file is loading is a load error, not a
runtime one.

To add an `ATE.` function, four things have to be true or it is not really available:

1. define it on `_AteTable` and `register` it, so the table exposes it;
2. record what it did through the host — the `logs`, `reports` or `equipment` lists — never by writing to
   the store directly, because the engine owns the rows and attributes them to the step or Action running;
3. check the context if it is only legal in one phase;
4. name it in both manuals: `ate_table_functions()` in `tests/test_manuals.py` reads the table off the
   object, and a function the code has that a manual does not show in code fails the suite.

`register` and `publish_run` are excluded from that rule: they build the table, they are not calls a
schedule makes.

`tests/test_lua_isolation.py` and `tests/test_lua_lockdown_calls.py` prove the sandbox;
`docs/manuals/examples/test-writer/forbidden.tslua` is a schedule that tries to open a Device while the
file is loading, which the host refuses.

## Devices

`DeviceRegistry` (one per run, created by the `LuaHost`) maps a Device Type name to a driver class and
tracks every handle opened this run so leftovers are closed at run end.

| Driver class | Device Type | How it reads and writes |
|---|---|---|
| `TcpDriver` | `TCP` | host/port socket with a background reader and the receive buffer cap |
| `SerialDriver` | `SERIAL` | a pyserial port, on the same buffered stream as TCP |
| `UdpDriver` | `UDP` | one datagram per message, drained by its own `_reader_loop` on the shared stream base; `size` truncates and the tail is discarded |
| `VisaDriver` | `VISA` | `_VisaDevice`, not a `_StreamDevice`: read on demand, no background reader |

A driver declares `name`, `fields` (each accepted open param as `(accepted types, required)`), and
`read_fields`/`write_fields` (usually `READ_FIELDS` and `WRITE_FIELDS` from
`src/ate/devices/stream.py`). `validate_params` rejects unknown, missing or mistyped fields before
anything connects, and never accepts a boolean where a number is expected.

Teardown belongs to the handle, not the driver: `DeviceRegistry.close_device` calls the handle's own
`close()`, and both `ATE.close` and the closing of Devices left open at run end go through it.
`Driver.close` is declared with a no-op default and never called — `VisaDriver.close` is the only
override, and it is dead — so a new Device Type puts its teardown on the handle `open` returns.
`_StreamDevice.close` stops the reader thread, closes the transport and joins the reader;
`_VisaDevice.close` closes the resource; both are safe to call twice.

`src/ate/devices/abort.py` wraps every Device call in `run_abortable`, which runs the call in slices of
`POLL_SECONDS = 0.05` and checks the run's abort flag between them. A connect or a DNS lookup is the
OS's to end: an abort leaves it behind and closes the handle it later produces.

An abort frees the worker, not the instrument. The helper thread left behind is joined in slices, and
all of an aborted run's leftover closes together get `_ABORT_CLOSE_SECONDS = 3.0`
(`src/ate/engine/run_engine.py`), each with a WARNING naming the Device. A Post-test Action on the same
Device waits for the call left behind, and a new run opening the same instrument while an abandoned call
is still running may find it busy. That is the boundary of what an abort can do, not a bug to fix.

`src/ate/devices/tracing.py` wraps every handle in `TracedDevice` when `deviceTrace.enabled` is set, and
emits one DEBUG line per open, read, write and close through the host's `record_log`, so the engine
attributes it to the Action that made it. Bytes are escaped and truncated at `deviceTrace.maxBytes`
(default `DEFAULT_TRACE_MAX_BYTES = 256`). A line looks like
`read TCP 127.0.0.1:5025 12 bytes "ACME,PSU,1\r\n" (4.1 ms)`.

A partial read logs a WARNING, and the handle says which kind it is: `partial_read_is_truncation` is
`True` for a Device Type with no stream and no deadline, so the line says `datagram truncated to N
byte(s)`; for a stream it says `timed out after N byte(s)`. Getting that wrong is a lie about the
Device, which is why the flag is on the handle rather than decided by the caller.

## Reports and the PDF helper

`src/ate/report/html.py` writes a self-contained HTML report named
`<UUT Type>/<UUT Type>_<serial>_<local start yyyymmdd-HHMMSS>_<PASS|FAIL>.html`.
`src/ate/report/pdf.py` renders it to PDF through a separate helper process (`ate _pdf`), started by
`src/ate/report/pdf_job.py` in a `QProcess` the session owns. Chromium's sandbox is decided after the
HTML is on disk; if the sandbox cannot be used and the Operator declines, the report still exists and
`report_written` still fires with an empty PDF path. `--allow-unsandboxed-pdf` is privileged-only.
`tests/test_pdf_sandbox_choice_30d.py` asserts the decision; the real render is stubbed by
`conftest.py` except in the four tests that must prove a PDF has text.

## Config and the exception log

`src/ate/config.py` reads one file, once, at launch. There is no per-user override, no environment
expansion in values, no `~` expansion; `<data>` expands to the platform data directory, and every other
relative path is resolved against the **config file's own directory** (`Config.data_dir` is
`self.path.parent`, so "the data directory" in the config sense is that directory; absolute paths are
used as given, and forward slashes work on Windows). `_SCHEMA` is the list of every key the
validator accepts, and `tests/test_manuals.py` derives the admin manual's key list from it, so a key
added to the code and not shown in code in the admin manual fails the suite. `_platform_data_dir` is
the seam the test suite redirects so no test writes to `/var/lib/ate` or `C:\ProgramData`.

`src/ate/exception_log.py` installs the hook that an uncaught exception reaches. Under PySide6 an
exception in a slot or in `QThread.run` does not end the process: it prints the traceback and carries on.
So the engine guard and this hook are not style, they are the difference between a run recorded as
`INTERRUPTED` and a run row left open with a screen waiting forever. The user-visible message is
`Internal error: this window may have stopped updating. A Test Run in progress keeps recording. Details: <log path>`.

## Packaging and release CI

Four artifacts ship: a Windows installer (Inno Setup, `packaging/windows/ate.iss`), a Linux `.deb`, a
tarball of the frozen bundle (`packaging/linux/install.sh` installs it), and a wheel. The first three
come from a PyInstaller bundle; the wheel carries no Qt at all.

The three manuals (test writer, station administrator, developer) are built before anything is
packaged: `docs/manuals/build.py` compiles each in a TeX Live image pinned by digest, stamps the
version being released on the title page, and stages the PDFs in `src/ate/manuals/`, which
`.gitignore` keeps out of the repository. That single staged directory is what makes them ship:
`ate.spec` sweeps every non-`.py` file under `src/ate`, and `[tool.setuptools.package-data]` lists
`*.pdf`, so the bundle and the wheel carry the same files. `verify_bundle()` fails on a missing or
empty PDF when `ATE_RELEASE_BUILD=1` is set (the release jobs set it); a build from a plain checkout
says plainly that it has no manuals and carries on. The names come from one list, `MANUAL_NAMES` in
`docs/manuals/build.py`, which `packaging/manuals.py` reads: a manual directory not in that list makes
the build fail rather than be left out silently. The `.deb` also copies the PDFs to
`/usr/share/doc/ate`, and `ate --help` prints the path the running install actually has:
`_internal/ate/manuals` inside a frozen bundle, `ate/manuals` inside the wheel's package,
`/usr/share/doc/ate` in a `.deb`. On a tag the `release` job attaches all six files to the GitHub
Release, each asset named with the version it belongs to, taken from the artifacts this same run
built and smoke-tested.

`packaging/linux/docker_smoke.py`, the container driver a human runs, tags a fresh copy of the
checkout to build against: `ATE_SMOKE_TAG` names the tag, and with nothing named it is this
checkout's own `git describe --tags`. The staged manuals are then checked against that version
rather than supplying it — read from the PDFs, the expectation would be the thing being checked, and
manuals left over from the last release would make every check in the run agree with itself. A build
that reports `1.2.3` while its manuals say something else is a version mismatch rather than a
packaging bug, and the driver refuses it instead of letting the smoke run report it as one.

Measured on the machine that built this manual: the three PDFs are just under 0.8 MB. The bundle is
447 files and 489.6 MB with them and 444 files and 488.8 MB without — 0.17% of a bundle. Packaged from
that bundle, the tarball gains 0.80 MB (199.8 MB to 200.6 MB), the `.deb` 1.6 MB (148.1 MB to 149.7 MB —
paid twice, in the bundle and in `/usr/share/doc/ate`), and the wheel 0.80 MB (304 KB to 1.11 MB),
which is 264% of the wheel: it ships no Qt, so the manuals are the largest thing in it.

```bash
python packaging/build_pyinstaller.py --smoke
python packaging/build_pyinstaller.py --verify dist/ate   # check an existing bundle: no build, no run
python -m build --wheel --no-isolation -o dist
python packaging/windows/build_installer.py --dry-run --dist dist
```

`verify_bundle()` is the gate: a bundle must carry the licence files generated by
`packaging/licence/notices.py --out build/licences` (four files), the report template, the bundled Lua
modules, the WebEngine binary and its `.pak` and `icudtl.dat` resources — the gate checks that some
are present, not how many. `packaging/pyinstaller/ate.spec`'s excludes list
has history: `QtWebEngineWidgets` imports `PySide6.QtWebChannel` itself, and excluding it produced a
bundle that looked fine and then died in `ate _pdf`.
`tests/test_pyinstaller_build_27a.py` runs the WebEngine import and compares what it pulls in with the
excludes list, so the list cannot drift again.

`.github/workflows/ci.yml` runs the suite as one job with a 2×2 matrix — `ubuntu-latest` and
`windows-latest`, each on CPython 3.13 and 3.14 — so four job runs, `fail-fast: false`, with
`QT_QPA_PLATFORM=offscreen` and `PYTHONFAULTHANDLER=1` at job level.
Two settings are load-bearing because of failures, not taste: `timeout-minutes` is 30 on Linux and 40 on
Windows (the first real run hung on Windows for an hour on a modal dialog with no Operator there), and a
step checks that `PySide6-Addons`, `PySide6-Essentials` and `shiboken6` are the same version and match
the Qt the runtime reports (two Qt builds in one process is how the first Windows run died with
`0xc0000139`).

`.github/workflows/release.yml` runs on a tag or by hand: `manuals` builds the PDFs first, then
`windows-installer`, `linux-build` and `linux-smoke` (matrix `deb`, `tar`, `wheel`) --- four job
definitions, six job runs. On a tag only, a fifth job, `release`, publishes the GitHub Release for that
tag from the artifacts this same run uploaded: the six manuals, the installer, the `.deb`, the tarball
and the wheel.

```bash
ATE_BUILD_TESTS=1 pytest -m build -q      # the real PyInstaller bundle, and the .deb built from it
ATE_DOCKER_TESTS=1 pytest -m build -q     # the debian:stable container smokes
```

Both gates are off by default and marked `build`. `tests/test_release_ci_28c.py` asserts the gate is
written as documented. Both ran for this manual on the machine that wrote it, with `TMPDIR` pointed at a
directory on disk, and each number below is a separate run of a separate command: the build gate alone
gave `2 passed, 2 skipped, 1587 deselected in 223.72 s`, the container gate alone gave
`1 passed in 533.95 s`, and both flags together gave `4 passed, 1587 deselected in 768.00 s`. They
belong to one machine in one afternoon and are not comparable with a CI job. The container gate needs
scratch space on disk: the first attempt wrote its artifacts into `/tmp`, which here is a small tmpfs,
and died with `No space left on device`. CI runs it either way.

## The test suite

`conftest.py` is not boilerplate; every fixture in it exists because of a failure.

- One `QApplication` for the session: this Qt 6.11 build segfaults at interpreter shutdown when a live
  `QApplication` is finalised alongside any `QCoreApplication`. The binding's `QApplication` does not
  accept a `platform=` keyword, so offscreen comes from `QT_QPA_PLATFORM`.
- Widget trees and workers are torn down per test (`_stop_workers`, `_destroy_widget_tree`). A
  `StagedRunScreen` keeps a live worker and a one-second timer for its whole lifetime; if it survives
  into the next test its queued signals fire into that test's widgets.
- Every started engine is stopped after every test (`_stop_running_engines` wraps `RunEngine.start`
  weakly, then aborts and joins). If it cannot, the test fails with "a RunEngine worker outlived its
  test".
- Files are closed strictly (`_release_files` closes every `RunStore`, `HeadlessRunSession` and `AppLog`
  and asserts nothing under pytest's temp directory is still open). On Linux an open file can be
  deleted; on Windows it cannot, so a Windows-only handle is caught here.
- The data directory is redirected: the autouse fixture points `ate.config._platform_data_dir` at
  pytest's basetemp.
- Privilege is pinned: `ATE_ELEVATED=0` for the session, session-scoped on purpose, because a
  module-scoped fixture that starts a run runs before any per-test fixture.
- The PDF helper is a stub by default (`_fake_pdf_helper` replaces
  `ate.report.pdf_job.pdf_helper_command`), except in the four tests that must prove a PDF has text.
- `report_qt_fatal` keeps Qt's fatal message from dying inside pytest-qt's capture.

`tests/_fixtures.py` holds the seams: `LoopbackPeer`, `UdpLoopbackPeer`, `temp_directory`,
`executable_stub`, `fake_pdf_helper`, `recording_pdf_helper`, `wait_for_disconnect`,
`wait_for_aborted_run`, `watch_store_writes`, `describe_stuck_runs`, `SlowAbortWarning` and
`assert_no_swallowed_exception`.

Commands the suite runs and checks, all in a temporary Station:

```bash
QT_QPA_PLATFORM=offscreen python -m pytest -q -p no:cacheprovider
QT_QPA_PLATFORM=offscreen python -m examples.harness.run_examples
python -m ate db migrate /tmp/station/data/ate.db --yes
python -c "import sqlite3, sys; rows=sqlite3.connect(sys.argv[1]).execute('SELECT step_number,result,attempt_count FROM node ORDER BY node_seq'); print(rows.fetchall())" /tmp/station/data/ate.db
```

The database path is an argument, not a string inside the `-c` code: on Windows it arrives with
backslashes, and a Windows path pasted into a string literal is a `SyntaxError` about a truncated
`\U` escape before SQLite is ever opened.

`python docs/manuals/build.py developer` builds this manual's PDF in Docker with texlive; it is run by
hand, not by the suite, which checks the sources rather than the rendered pages.

### The rule for a timing test (packet 31e)

1. Start the clock on an event, not on a sleep. The old abort test timed from after `time.sleep(0.1)`
   with no proof the action had begun. `_drive_abort` starts on the worker being inside `ATE.sleep`'s
   wait, or on `input_asked`, and stops at `FINISHED`.
2. Separate the requirement from the expectation. Spec story 83: an abort should stop within 0.5 s and must
   stop within 10 s. A test fails past 10 s and warns past 0.5 s, so a slow runner stays visible instead
   of being a false failure.
3. A stamp means what the code did, not what the test expected. Bracket each `record_log` call on the
   worker and require `at` to fall inside the bracket.
4. Show that the bound can fail. Every bound here has a test that plants a stall and proves the test
   catches it: a 1 s injected stall gives "abort took 1.011 s"; a busy loop of `1e10` iterations fails
   the 10 s requirement.
5. Do not widen a timeout. Packet 32b's checklist says it plainly: the 10 s wait stays and can still
   fail, and when it does it says what the worker was doing.
6. Run under load. The convention is ten repetitions of the changed files with six busy loops on three
   cores. Several flakes only appear under contention.

Name a new test after the packet that added it (`tests/test_device_abort_32b.py` is the style), put
shared helpers in `tests/_fixtures.py`, and use `assert_no_swallowed_exception` if your test starts a Qt
process: exit 0 no longer means nothing went wrong.

## PySide6 traps, found the hard way

ATE uses PySide6 because PyQt6's wheels are GPL and shipping a GPL binding under an MIT label is a
licence gap (`docs/adr/0001-pyside6-instead-of-pyqt6.md`). Each of these is a trap when writing code or
a test here.

- An exception in a slot or in `QThread.run` does not end the process. PyQt6 aborted with SIGABRT;
  PySide6 prints the traceback and carries on.
- A child process's exit code is not a signal of correctness. Use `assert_no_swallowed_exception(stderr)`.
- `Signal(int)` is a C `int`, as above. `Signal(str)` turns `5` and `None` into `''`.
- Two Qt builds in one process: `PySide6-Addons` pins `PySide6-Essentials` and `shiboken6` to the same
  version, and CI checks that the Qt the runtime reports matches. If it does not, WebEngine loads a
  second Qt and Windows dies with `0xc0000139` the moment WebEngine is imported. Linux tolerates it
  until it does not.
- `QtWebEngineWidgets` imports `QtWebChannel`: the packaging bug that produced a bundle that looked
  complete and failed at render time.
- Deleted objects: PyQt6 says `wrapped C/C++ object of type Thing has been deleted`, PySide6 says
  `libshiboken: Internal C++ object (Thing) already deleted.` Every catch here is `except RuntimeError`,
  so both behave the same. `sip.isdeleted(x)` became `not shiboken6.isValid(x)`.
- A second `QCoreApplication` raises in PySide6 and was tolerated by PyQt6. This is why the suite keeps
  exactly one application, and why `conftest.py` destroys the widget tree before quitting: an orphaned
  `QWidget` finalised at interpreter shutdown segfaults in this Qt 6.11 build either way.
- API differences that only show in tests: `findChildren` takes one type, not a tuple; `QWidget.cursor()`
  does not compare equal to a `Qt.CursorShape` enum, read `.shape()`; `QWIDGETSIZE_MAX` is not exported,
  so tests use the literal `(1 << 24) - 1`.
- Offscreen on Windows has no fonts: Qt's offscreen platform looks for fonts in Qt's own (empty) library
  directory, so every character draws as the missing-glyph box. `conftest.py` sets `QT_QPA_FONTDIR` to
  Windows' font directory before any Qt import, and every subprocess a test starts inherits it. A real
  operator on the `windows` platform has no such problem.

None of these were run "on both bindings": only PySide6 was tested here. The ADR records the comparison
as a reviewer's probe where a claim rests on one.

## Reading a failed Test Run

Read the traces in this order; the order is what makes the cause visible.

1. `test_run.result` is `PASS` or `FAIL`. `end_reason` is the story: `COMPLETED` (the schedule ran to its
   end), `PRETEST_FAILED` (a Pre-test Action raised; every step NOTRUN), `STOPPED_ON_FAIL` (the Operator
   answered `ABORT` at a prompt), `ABORTED` (a run-level `request_abort()`, wherever it landed),
   `INTERRUPTED` (the worker ended on an exception it could not handle, or the process died with the run
   open and launch closed it).
2. A step's `result` is one of `PASS`, `FAIL`, `ERROR`, `SKIPPED`, `NOTRUN`, `FORCED_NOTRUN` or
   `NOREPORT`. `NOREPORT` means the step ran and never called `ATE.report`: a schedule bug, recorded as a
   WARNING rather than silently counted as a pass.
3. The Run log is the `log_line` table (level, source `schedule`/`ate`/`device`, owner the Action or the
   Attempt). The application log is the per-launch file at `<data>/logs/ate-app-<time>.log`, where an
   unhandled exception's traceback goes. Different files, different questions: "what did this run do"
   versus "what did the application do wrong".
4. If a test fails with "a RunEngine worker outlived its test", read the `describe_stuck_runs` section:
   the live workers, what each session thinks its state is, what the store recorded, the store's write
   timings with any write still in flight, and every thread's stack from `faulthandler`. Long or
   in-flight writes mean the disk; normal writes with the worker's stack elsewhere mean a fault the suite
   has not found.

Warnings after an abort: `left open at end of run` is a Device the schedule never closed;
`was still closing when the aborted run ended` means the close outlived the run's 3 s bound
(`_ABORT_CLOSE_SECONDS = 3.0`) and will finish later; `datagram truncated to N byte(s)` and
`timed out after N byte(s)` are different read failures and the Device says which one it is.

## Worked example: adding a Device Type

Run by `tests/test_manuals.py`: it copies `src/` into a temporary directory, puts this file at
`src/ate/devices/echo.py`, makes the three edits to `src/ate/engine/lua_host.py` shown below --- the
import, the register line, the enum member --- and runs the schedule headless against that copy.

```python:docs/manuals/examples/developer/echo_driver.py
"""The ECHO Device driver: the developer manual's worked example of a Device Type.

Nothing here touches a transport. ``open`` returns a handle that keeps the bytes a Test
Schedule writes, and ``read`` hands them back, so a schedule can exercise open, write, read,
flush and close against a Device Type that did not exist five minutes ago. That is enough to
prove the steps the manual gives: declare the fields, name the Device Type, register the
driver, add the enum member. ``tests/test_manuals.py`` copies this file into a temporary copy
of ``src/ate``, applies the three edits the manual shows, and runs ``echo.tslua`` against it.
"""

from __future__ import annotations

from typing import Any

from .registry import Driver, validate_params
from .stream import READ_FIELDS, WRITE_FIELDS


class _EchoDevice:
    """The handle an ECHO Device is: the bytes written, in the order they were written.

    ``driver`` is what the ATE wrappers validate a per-call read/write field against, and
    ``describe`` is what makes a value a Device handle at all (packet 29d review)."""

    #: A read cut short by `size` is a truncation, never a deadline catching a stream mid-bytes.
    partial_read_is_truncation = True

    def __init__(self, label: str) -> None:
        self.driver = EchoDriver
        self._label = label
        self._buffer = bytearray()

    def describe(self) -> str:
        return f"ECHO {self._label}"

    def read(self, params: dict[str, Any]) -> tuple[bytes, int, bool, str | None]:
        term = params.get("readTerm")
        if isinstance(term, str):
            term = term.encode()
        size = params.get("size")
        data = bytes(self._buffer)
        if term:
            head, found, tail = data.partition(term)
            self._buffer = bytearray(tail) if found else bytearray()
            data = head if found else data
        else:
            self._buffer.clear()
        if size is not None and len(data) > size:
            self._buffer.extend(data[size:])
            data = data[:size]
            return data, len(data), True, None
        return data, len(data), False, None

    def write(self, params: dict[str, Any]) -> tuple[bool, int, str | None]:
        payload = params["data"]
        if isinstance(payload, str):
            payload = payload.encode()
        term = params.get("writeTerm") or b""
        if isinstance(term, str):
            term = term.encode()
        self._buffer.extend(payload)
        self._buffer.extend(term)
        return True, len(payload) + len(term), None

    def flush(self) -> tuple[bool, int, str | None]:
        count = len(self._buffer)
        self._buffer.clear()
        return True, count, None

    def take_overflow(self) -> tuple[bool, int]:
        return False, 0  # no cap on this buffer, so no bytes are ever dropped

    def close(self) -> None:
        """Teardown the registry actually calls: ``close_device`` reaches the handle, not the driver."""
        self._buffer.clear()


class EchoDriver(Driver):
    """A Device Type that answers with exactly what the Test Schedule wrote to it."""

    name = "ECHO"
    fields = {
        "label": ((str,), True),            # named in every trace line and WARNING for this Device
        "timeout": ((int, float), False),   # accepted so a schedule can pass one; nothing waits on it
    }
    read_fields = READ_FIELDS
    write_fields = WRITE_FIELDS

    def open(self, params: dict[str, Any]) -> _EchoDevice:
        """Validate the params and return the handle. A bad field raises before anything else."""
        validated = validate_params(self, params)
        return _EchoDevice(validated["label"])

    # No driver-level `close`: the registry closes the handle, so teardown lives on the handle.
```

Two of the three edits are in `src/ate/engine/lua_host.py`: import the new module in the style the
other drivers use (`serial`, `udp` and `visa` are imported from their own modules, not through the
package's `__init__`), and add the register line beside the others.

```python
from ..devices.abort import DeviceAborted
from ..devices.echo import EchoDriver  # the developer manual's worked example
```

```python
        self._devices.register("TCP", TcpDriver)
        self._devices.register("SERIAL", SerialDriver)  # packet 18a: pyserial ports, same API
        self._devices.register("UDP", UdpDriver)  # packet 18b: datagrams, one datagram per message
        self._devices.register("VISA", VisaDriver)  # packet 19: pyvisa resources, no background reader
        self._devices.register("ECHO", EchoDriver)  # the developer manual's worked example
```

Add the enum member in `_ENUMS`, in the same file. The Device Type table is flat — the member's name
maps to its label:

```python
    "DeviceType": {
        "ECHO": "ECHO",  # the developer manual's worked example
        "SERIAL": "SERIAL",
        "TCP": "TCP",
        "UDP": "UDP",
        "VISA": "VISA",
    },
```

Then run it. `runner.py` is the headless driver shown above; the schedule is:

```lua:docs/manuals/examples/developer/echo.tslua
-- echo.tslua: the ECHO Device Type from the developer manual, run for real.
--
-- The manual tells a developer to add a driver, register it and add the enum member. This
-- schedule is what proves it worked: tests/test_manuals.py copies src/ate into a temporary
-- directory, puts echo_driver.py in it as src/ate/devices/echo.py, applies the three edits the
-- manual shows, and runs this schedule headless against that copy.
--
-- Demonstrates: a Device Type the application does not ship; the fields a driver declares;
-- a read cut short by `size` being a truncation; the trace lines a Device produces when
-- deviceTrace.enabled is on; and a Step Input answered from the script with --input.
return ScheduleParams("Developer: echo Device", "open an ECHO Device, write to it, read it back")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Open and write"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.ECHO, {label = "manual-example"})
        local w = ATE.write(dev, {data = "hello", writeTerm = "\n"})
        ATE.report((w.ok and w.count == 6) and Result.PASS or Result.FAIL,
                   "wrote " .. w.count .. " bytes including the terminator", w.count, 6)
        ATE.close(dev)
      end),

    -- A Device left open is closed by the engine at run end with one WARNING (packet 16a), so
    -- step 2 opens one and deliberately does not close it.
    StepParams("Leave a Device open"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.ECHO, {label = "left-open"})
        ATE.write(dev, {data = "unbounded"})
        ATE.report(Result.PASS, "the run will close this Device for me")
      end),

    -- `size` cuts a read short: partial is true, and for this Device Type the WARNING says
    -- "truncated", not "timed out", because there is no stream and no deadline.
    StepParams("Cut a read with size"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.ECHO, {label = "sized"})
        ATE.write(dev, {data = "ABCDEFGH"})
        local r = ATE.read(dev, {size = 4})
        ATE.report((r.partial and r.count == 4 and r.data == "ABCD") and Result.PASS or Result.FAIL,
                   "read " .. r.count .. " of 8 bytes", r.count, 4)
        ATE.close(dev)
      end),

    -- A Step Input blocks the worker until it is answered. runner.py answers it from
    -- --input "Operator reading=42", which is how a headless run gets an Operator.
    StepParams("Operator reading"):setStepNo("4.")
      :setInputs({ [1] = { desc = "board current, mA", type = "INTEGER" } })
      :setAction(function()
        local mA = ATE.input(1)
        ATE.report((mA == 42) and Result.PASS or Result.FAIL, "operator read " .. mA .. " mA", mA, 42)
      end),
  })
```

```bash
python runner.py /tmp/station/config/config.json "Developer: echo Device" --input "Operator reading=42"   # PYTHONPATH=<the copy of src>
```

What the run records, asserted by the test: result `PASS`, `end_reason` `COMPLETED`, all four steps
`PASS`, and these lines in the Run log:

- `open ECHO ok (0.3 ms)` — the open line names the Device as `device_label` derives it from the open
  params (type plus address); an `ECHO` has no address, so it is just `ECHO`. The calls after it carry
  the handle's own `describe()`
- `write ECHO manual-example 6 bytes "hello\n" (0.0 ms)`
- `read ECHO sized 4 bytes "ABCD" partial (0.0 ms)`
- `ATE.read: datagram truncated to 4 byte(s)`
- `device ECHO left-open left open at end of run` (WARNING)

The negative cases are tested too. A schedule that names an enum member that does not exist gets `nil`:
`ATE.open(DeviceType.ECHO, ...)` raises `deviceType must be a DeviceType enum member, got NoneType`, the
step records an ERROR Attempt, and the run fails. With the enum member present and no register line,
`DeviceRegistry.open_device` raises `unknown DeviceType 'ECHO'`. A Device Type is real only when the
driver, the register line and the enum member all exist.

Then update: a test file named after the packet, the Device Type list in both test-writer manuals
(`device_types()` and `device_drivers()` derive it from the classes that exist), and
`docs/manuals/llm/test-writer.md`.

## Worked example: adding a schema version

Also run by `tests/test_manuals.py`, against a temporary copy of `src/`.

Add the table to `src/ate/storage/schema.py` and bump the version. The comment explaining each bump is
the convention: `input_value` made it 2, `media_release` and `run_file` made it 3, `schedule_instance`
made it 4, `equipment` made it 5.

```python
SCHEMA_VERSION = 6
```

```python
review_note = Table(
    "review_note",
    metadata,
    Column("run_uuid", Text, primary_key=True),
    Column("seq", Integer, primary_key=True),  # the composite key, as every other run table
    Column("text", Text, nullable=True),
    Column("at", Text, nullable=True),
)
```

Nothing else is needed: `RunStore._ensure_schema` calls `metadata.create_all`, which reflects each table
through the backend's own catalog and never raises on a database that already has it, and sets
`PRAGMA user_version` only when `missing_tables` found something to create. Use the record's types:
UUIDs as 36-character `Text`, times as ISO-8601 `Text`, booleans as `Integer` 0/1, structured values as
JSON `Text`. No SQLite-only column: a Postgres backend is a connection change. The only raw SQL in the
storage layer is the two pragmas, and they stay in `src/ate/storage/store.py`.

Then:

```bash
python -m ate db migrate /tmp/station/data/ate.db --yes
```

prints `backed up ... to ....backup-<YYYYMMDD-HHMMSS>` and `created or upgraded database at ...
(schema version 6)`. The test runs it against the same file more than once --- first with the shipped
code, which creates the version 5 record and makes no copy because there was no database to copy, then
with the patched copy, which copies the version 5 database beside itself and adds `review_note` on
top --- and asserts the set of tables only grew and that the copy still says version 5. Pointed at the
version 6 database, the shipped code exits 1 with `this ATE is too old for ...; update ATE`: that is
`check_database_at_launch` returning `TOO_OLD`.

`src/ate/storage/backup.py` is where the copy comes from, and `migrate_database` is the routine both
`ate db migrate` and `ate setup` call, so an installer upgrade and a hand-run upgrade take the same
copy. A copy is taken exactly when this build has a table to create: none for a file that does not
exist, none for one that holds no tables at all, one whenever `missing_tables(path)` is non-empty --
including a database already stamped at this version that is missing a table. SQLite's
`Connection.backup` writes `<db>.backup-<YYYYMMDD-HHMMSS>` beside the file and never over an existing
name; the copy is written as `<name>.partial`, checked (`integrity_check`, the same tables, the same
version) and only then renamed. The copy is given up on when it stops moving for
`BACKUP_LOCK_TIMEOUT_S = 10.0` seconds: a database held by another process is refused inside that
time rather than waited out, and a database that keeps copying is never refused for taking a long
time. If the copy cannot be written the migration is refused: the command names the path and the
reason, exits 1, and changes nothing.

The timestamped backup is automatic; the Administrator does not need to make it before an upgrade.

And `create_all` does not alter a table that already exists, so adding a column is not a version bump ---
it needs an explicit migration, written and tested as one. Packet 35 removed the silent version of that
mistake where nothing is created at all: `RunStore._ensure_schema` stamps `PRAGMA user_version` only
when a table was created, so a database storing an older version that already has every table gets its
own launch status, `NO_MIGRATION`, and `db status`, `db migrate` and the launch dialog refuse it with
one line, e.g. `the database at ... stores schema version 5 and already holds every table this ATE
declares: this ATE has no migration from version 5 to 6, so it is not stamped as version 6, and
`ate db migrate` on it changes nothing`. There is
no `ALTER` or `add_column` anywhere in `src/ate`, and every insert names its columns by hand
(`insert_test_run` builds its row field by field), so an upgrade never fills a new column. What the
guard cannot see is a bump that adds a column *next to* a new table: the new table makes
`missing_tables` non-empty, so the stamp is raised and the column is never added. A real migration
registry -- steps from N to N+1, run in order, each tested -- is needed before the next schema version.

A new table is not a new report: `src/ate/report/html.py` reads through the store's query methods, so a
table nothing queries is invisible on the page. Batching applies to a new row type as well
(`_rows_per_statement` keeps an insert under `_SQL_MAX_VARIABLES = 999`), and `_log_seq` is what orders a
new row against the rest of the run.

## Documents, and where a change has to go

| Where | What it is |
|---|---|
| `GLOSSARY.md` | the words: Test Run, Test Step, Attempt, Media Release, Device Type |
| `docs/adr/0001-pyside6-instead-of-pyqt6.md` | a settled decision, with what was not tested |
| `src/ate/` and `tests/` | the implemented behaviour and its executable checks |
| `docs/manuals/` | the three manuals and the three agent manuals |
| `docs/building-from-source.md` | how to build, including these PDFs |

A change is not finished when the code works:

- a new config key goes to `src/ate/config.py`'s `_SCHEMA` and to both admin manuals;
- a new `ate` subcommand or flag goes to `src/ate/cli.py` and the admin manual;
- a new `ATE` function, enum or setter goes to `src/ate/engine/lua_host.py` and both test-writer manuals;
- a new Device Type goes to the driver, the register line, the enum member and the test-writer manuals;
- a new module under `src/ate/` goes to this file and to `docs/manuals/developer/`;
- a new table goes to `src/ate/storage/schema.py`, the version bump, and both developer manuals;
- a command a manual tells someone to run goes into `COMMANDS_RUN`/`COMMANDS_NOT_RUN` (admin) or
  `DEV_COMMANDS_RUN`/`DEV_COMMANDS_NOT_RUN` (developer) with its exit code, the output it must produce,
  or the reason it is not run.

## Commands in this manual, and what covers them

Run by `tests/test_manuals.py`: `python packaging/windows/build_installer.py --dry-run --dist dist`,
`python -m build --wheel --no-isolation -o dist`,
`QT_QPA_PLATFORM=offscreen python -m examples.harness.run_examples`, the `sqlite3` query above, and
`python -m ate db migrate /tmp/station/data/ate.db --yes`.

Shown but not run here, with what covers them: the pytest command (the suite cannot run itself; it is the
command CI runs, and the counts above come from running it); `ATE_BUILD_TESTS=1 pytest -m build` and its
`-q` form (by design not part of a normal run: it builds a PyInstaller bundle, and it was run for this
manual, 2 passed and 2 skipped in 223.72 s); `ATE_DOCKER_TESTS=1 pytest -m build` in both forms (needs
Docker and network in a `debian:stable` container; run here with `TMPDIR` pointed at a directory on disk,
1 passed in 533.95 s — on a machine whose `/tmp` is a small tmpfs it fails with `No space left on device`);
`python packaging/build_pyinstaller.py --smoke` (the gate above calls the same script's
`build_bundle()` and `verify_bundle()`); `python packaging/build_pyinstaller.py --verify dist/ate`
(needs a built bundle; `tests/test_linux_packages_28b.py` runs `verify_bundle()` against a real bundle
and a deliberately incomplete one); and the `runner.py` line in the Device example, which the
worked-example test runs against a temporary copy of `src/`.

To build the PDF of the manual this file is the other half of:

```bash
python docs/manuals/build.py developer   # Docker, texlive/texlive:latest
```

## Never

- Never import `QtWidgets` in `src/ate/engine/` or `src/ate/run_session/`.
- Never parent a `RunEngine` to a widget, and never `wait()` on the main thread without
  `request_abort()` first.
- Never widen a test timeout to make a failure go away; make the test able to fail instead.
- Never compare a `log_line.at` stamp with a deadline measured at drain time.
- Never add a config key, an `ATE` function, a Device Type, a module or a table without updating the
  manual that lists it: the coverage test derives the list from the code and will fail.
- Never claim a Device behaviour from a spec or an asset file without checking `src/ate/devices/`.
- Never run `groupadd`, `chown`, `chmod` or `icacls` on the test machine for a permission claim; use
  `RecordingPermissions` or the gated `debian:stable` container.
- Never run a Test Schedule's file-loading functions (`dofile`, `loadfile`, `load`) in a sandbox test as
  if they worked: they are set to nil, and a schedule that calls one gets an ERROR Attempt.

## What this manual cannot state

- Real hardware behaviour. Every Device test uses a loopback peer, a pseudo-terminal, or pyvisa-py
  behind a socket. No GPIB instrument, real serial adapter or NI-VISA stack has been opened by this
  suite.
- Real Windows behaviour a human watched. The same tests run on `windows-latest` in CI, and the
  release workflow installed the Windows installer there: at `v0.1.0-rc2` (run 41198438867)
  `packaging/windows/ate.iss` was compiled and the installer silently installed, run, upgraded and
  uninstalled on that runner, which is what the admin manual says. None of it happened on this
  machine and nobody watched a window open. The `icacls` paths, the Inno installer and the frozen
  Windows bundle are asserted here by reading scripts and by `tests/test_release_ci_27c.py`, which
  checks the workflow's shape, never an installed program.
- Any container other than `debian:stable`. `ATE_DOCKER_TESTS=1 pytest -m build` and
  `packaging/linux/docker_smoke.py` do pass here — 1 passed in 533.95 s with `TMPDIR` on a disk-backed
  directory — but the first attempt on this machine's `/tmp` failed with `No space left on device`, and
  nothing has ever been smoked on Fedora, Ubuntu or a SELinux-enforcing host.
  `tests/test_release_ci_28c.py` asserts the gate and the smoke scripts' logic.
- The cause of the packet 32b stall. One Windows CI run left a Test Run stalled for tens of seconds. The
  fixes and the diagnostic are proven; the disk being the culprit is a belief, not a measurement. The
  next stall can be read: long or in-flight writes point at the disk, normal writes with the worker's
  stack elsewhere point at something nobody has found.
- Both Qt bindings. The PySide6/PyQt6 differences come from the packet 32a switch and its notes; they
  were not measured side by side here.

Where the documents and the code disagree, the code was followed and the document left as it is:

- spec rule 08 says `log_line` rows are written "about once a second". They are inserted per step.
- The 32a notes say the release workflow has "5 jobs". It defines three: `windows-installer`,
  `linux-build`, and `linux-smoke` with a `deb`/`tar`/`wheel` matrix --- five job runs.
- `_location()` finds a step's file and line by searching the schedule text for `:setAction(`, not from
  the Lua call stack, so a multi-line Action, a helper module or a Sub-schedule can report the wrong
  place.
- The staged Run screen is a vertical stack, not the prototype layout in
  `run-screen-prototype.html` the review notes refer to.

Numbers in this manual are snapshots: the suite's size and its elapsed time move with every packet. The
names, signals, tables and drivers are not snapshots --- the suite derives them from the code. The checks
catch a manual falling behind the code, not a manual ahead of it.
