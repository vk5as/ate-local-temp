# ATE v1

Status: ready-for-agent

## Problem Statement

A test engineering team runs scripted tests against units, mostly hardware, on test benches. They need a desktop application that an Operator can sit in front of to run a Test Schedule against a UUT: enter who is testing what, pick the schedule, answer the prompts the schedule asks, react when a step fails, and walk away with a trustworthy Test Report.

Today there is nothing that does all of this together. The team needs:

- Test Schedules written in Lua that drive real Devices (serial, TCP, UDP and VISA instruments) and report a result per Test Step.
- Released sets of Lua files (Media Releases) installed side by side, with every file checksummed so a run can prove exactly which code it executed.
- A complete, append-only record of every Test Run (who, what, which code, every step, every Attempt, every value entered, every log line) that a report can be regenerated from years later.
- Test Reports as HTML and PDF, written automatically at the end of every run, with space for the Operator's signature.
- A History screen to find past runs, regenerate their reports and export their logs.
- A Windows-first install that a non-admin Operator can use day to day, with Linux supported on a best-effort basis.

## Solution

ATE is a Python + PyQt6 desktop application that runs Lua 5.5 Test Schedules (through the stock `lupa` wheels) on a background worker thread while the GUI stays responsive.

- An admin installs ATE and Media Releases on a Station. `ate setup` prepares the Station: data directories, permissions, a machine-wide config and the local SQLite database.
- At launch ATE scans the configured directories for Test Definitions (`.atedef` files), validates them, resolves their dependencies, checks every checksum and lists every visible Test Schedule in one flat list, with broken ones shown with their errors.
- The Operator enters Operator, Project, UUT Type and serial, picks a Test Schedule, adjusts which steps are enabled and whether to Stop on Fail, and starts the run. A confirmation appears only when a checksum mismatches.
- ATE runs every Pre-test Action, then each enabled Test Step, then every Post-test Action. Steps talk to Devices through the `ATE` Lua table, ask the Operator for Step Inputs, and report a `Result`. On a failure with Stop on Fail, the run pauses for the Operator to Continue, Retry or Abort.
- Everything is written to the database as it happens. At the end ATE writes the HTML and PDF Test Report.
- History lists past runs with filters, regenerates reports, exports Run logs, and starts a new run for the same UUT.

The Run screen comes in two layouts, staged (default) and three-pane, both built on one UI-free run session, so the engine is testable without a GUI.

## User Stories

### Station setup and configuration

1. As an admin, I want a Windows installer that installs ATE per-machine and puts `ate` on the `PATH`, so that every Operator on the Station can use it.
2. As an admin, I want the installer to run `ate setup`, so that the data directories, config and database are ready before the first launch.
3. As an admin, I want `ate setup` to give ordinary users write access to the database, reports and logs but keep `config.json` admin-only, so that Operators can run tests without being able to change the Station's configuration.
4. As an admin, I want `ate setup` to create a default `config.json` with a fresh `station.uuid` only if none exists, so that upgrades never overwrite my configuration.
5. As an admin, I want upgrades to install in place and keep the config, database, reports and logs, so that history survives every upgrade.
6. As an admin, I want uninstalling ATE to never delete the database, reports or logs, so that test records are never lost as a side effect.
7. As a Linux admin, I want a `.tar.gz` with `install.sh`, a self-contained `.deb` for Debian stable, and a wheel for pipx, so that I can install ATE in the way that suits my Station.
8. As a Linux admin, I want `ate setup` to create an `ate` group owning the data directories, so that I control which users can run tests by group membership.
9. As a developer, I want source build instructions, so that I can build and run ATE from a checkout.
10. As an admin, I want the config file at a machine-wide location (`%PROGRAMDATA%\ATE\config.json`, `/etc/ate/config.json`) with a `--config` override, so that the Station's configuration is in one known place.
11. As an Operator, I want ATE to tell me the expected path and offer to create a default config or abort when the config is missing, so that a missing file is never silently ignored.
12. As an admin, I want ATE to refuse to start on invalid JSON, a wrong type, an unknown key or an unknown `format`, naming the file and key, so that typos are caught immediately.
13. As an admin, I want `station.name` to default to the hostname and `station.uuid` to be generated and written back to the config, so that every Test Run identifies its Station stably.
14. As an admin, I want ATE to refuse to start, showing the generated UUID, if it can't write `station.uuid` back, so that I can add it by hand and the identity stays stable.
15. As an admin, I want relative paths in the config to resolve against the config file's directory, absolute paths to work, and forward slashes to work on Windows, so that paths behave predictably.
16. As an admin, I want a missing Test Definition directory to be a warning rather than a startup failure, so that one bad path doesn't stop the Station.
17. As an admin, I want one application log file per launch that is never rotated or deleted, so that a multi-day Test Run is never split across files.
18. As an admin, I want settings for Device tracing, the receive buffer size, the default Project, the theme, the Run screen layout, layout switching and History-during-run, so that I can tune each Station.
19. As an admin, I want `pyvisa-py` bundled, so that LAN and USBTMC instruments work without installing NI-VISA, while NI-VISA is still used when present.

### Database lifecycle

20. As an admin, I want `ate db migrate` to create the database (after confirmation) or upgrade it (after a timestamped backup), so that schema changes are deliberate and reversible.
21. As an admin, I want `ate db status` to print the schema version, so that I can check a Station's database.
22. As an Operator, I want ATE to stop at launch with "run `ate db migrate`" when the database is missing or older than the app, so that the app never changes the schema itself.
23. As an Operator, I want ATE to stop at launch with "this ATE is too old" when the database is newer than the app, so that an old ATE can't damage newer records.
24. As an admin, I want only `sqlite` accepted as `database.type` in v1, so that an unsupported backend is caught at startup.

### Test Definitions and Media Releases

25. As a test author, I want a Test Definition to be a JSON `.atedef` file with `format`, `mediaNumber`, `name`, semver `version`, optional `depends` and `files`, so that a Media Release is described in one validated file.
26. As a test author, I want `.tslua` entries to be Test Schedules and `.lua` entries to be modules, so that a file's role is clear from its extension.
27. As a test author, I want to mark a Test Schedule `hidden`, so that it can be used as a Sub-schedule without cluttering the Operator's list.
28. As a test author, I want relative and absolute paths, with module names derived from relative paths and set explicitly with `module` where needed, so that shared helpers anywhere on disk can be `require`d.
29. As a test author, I want `depends` entries with npm-style semver ranges resolved transitively to the highest matching installed release, so that helper Media Releases can be shared and upgraded.
30. As a test author, I want `ate definition update` to recompute and fill in every `sha256`, so that I don't maintain checksums by hand.
31. As an Operator, I want ATE to scan the configured directories recursively at launch and on rescan, so that newly installed Media Releases appear without restarting.
32. As an Operator, I want broken Test Definitions (missing files, bad entries, duplicates, unresolvable or ambiguous dependencies, cycles, duplicate Media Numbers) shown with their errors and not runnable, so that I never start a run on a broken release.
33. As an Operator, I want a Test Schedule that raises while loading to be listed as broken with its error while the rest of its Media Release stays usable, so that one bad schedule doesn't block others.
34. As a quality engineer, I want every file's SHA-256 checked against the Test Definition, with a mismatch logged, flagged and recorded but the file still loaded, so that runs are traceable without blocking production.

### Writing Test Schedules

35. As a test author, I want a `.tslua` file to return a `ScheduleParams` object built with chained setters (doc title, id, revision, Pre-test and Post-test Actions, Stop on Fail default, steps), so that a schedule is declarative data plus Lua functions.
36. As a test author, I want `StepParams(name)` with Step Number, enable default, Step Inputs and an action, so that each Test Step is self-describing.
37. As a test author, I want Step Inputs of type `ENUM` (radio buttons, checkboxes, push buttons), `INTEGER`, `FLOAT`, `STRING`, `PROMPT` and `POLL`, with ranges, patterns, tooltips and orientation, so that I can ask the Operator for any value I need.
38. As a test author, I want `ATE.input(n)` to return a typed value per input type, blocking for every type except `POLL`, so that the step reads Operator input like a function call.
39. As a test author, I want `POLL` to return `nil` until a button is clicked and then that button's text until the next step starts, so that a step can loop waiting for an Operator signal.
40. As a test author, I want `ATE.report(passFail, msg, actual, expected)` where the last call wins and every call is logged, so that a step can update its result as it goes.
41. As a test author, I want a step that never reports to record `NOREPORT` with a warning, so that a forgotten report is visible.
42. As a test author, I want a Lua error in a step to record `Result.ERROR` with the error's first line as the message and the full traceback in the log, so that failures are diagnosable.
43. As a test author, I want ATE's error messages to start with my schedule's `file:line` and name the `ATE` function, so that I can find the failing call.
44. As a test author, I want `require` to resolve only modules listed in the Test Definition or its dependencies, cached once per run, so that every executed file is checksummed.
45. As a test author, I want `require("ate.devices")` to give me ATE's bundled builder module, so that I can build Device params without copying helpers.
46. As a quality engineer, I want `dofile`, `loadfile`, `load`, the `python` table and bytecode loading removed, so that no Lua source can run without passing the checksum check.
47. As a test author, I want `ATE.log(Log.X, ...)` with levels and `print` redirected to `INFO`, so that schedule output is always recorded.
48. As a test author, I want `ATE.sleep(seconds)` that an abort interrupts, so that waits never block an abort.
49. As a test author, I want `ATE.addEquipment(name, serial, connection, calDue, notes)`, so that the bench Equipment and its calibration appear on the Test Report.
50. As a test author, I want a read-only `ATE.run` with UUT serial, Operator, Station, Project, Test Schedule name and Media Release, so that a schedule can use run details.
51. As a test author, I want calls rejected where they don't belong (Device I/O at load time, `input` and `report` outside steps), so that mistakes surface as clear errors.

### Sub-schedules

52. As a test author, I want `setInclude(file, keyvals)` with `setPrefix` to pull another Test Schedule's steps in under a header row, so that I can reuse a schedule several times with different parameters.
53. As a test author, I want each include to be its own instance that receives its `keyvals` as the chunk argument, so that instances don't share state except through `require`d modules.
54. As a test author, I want prefixes to stack through nesting and duplicate Step Numbers to be detected on the expanded tree, so that every Step Number in a run is unique.
55. As a test author, I want included files resolved through the Test Definition and its dependencies, and missing, unlisted, ambiguous or cyclic includes to break only the including schedule, so that errors are local and clear.
56. As a test author, I want `setEnable` on an include step to force the default of every step beneath it, so that a whole Sub-schedule can be off by default.

### Devices

57. As a test author, I want `ATE.open(DeviceType.X, params [, onError])` for `SERIAL`, `TCP`, `UDP` and `VISA`, returning an opaque device object usable for the whole run, so that a Pre-test Action can open instruments for later steps.
58. As a test author, I want every `open` field required and type-checked, with unknown, missing or mistyped fields raising and naming the field, so that typos never silently misconfigure a Device.
59. As a test author, I want `ate.devices` builders (`Serial(port)`, `Tcp(host, port)`, `Udp(host, port)`, `Visa(resource)`, `Read()`, `Write(data)`) with defaults and chained setters, so that common setups are one line.
60. As a test author, I want `read` and `write` to take optional per-call overrides of `size`, `timeout` and terminators, so that one Device can handle different exchanges.
61. As a test author, I want data as raw bytes with terminators stripped on read and appended on write, so that both SCPI text and binary protocols work.
62. As a test author, I want uniform result tables (`{data, count, partial, overflow}` for reads, `{ok, count}` for writes), so that code is the same across Device Types.
63. As a test author, I want a timeout after some bytes to return a partial read with a warning rather than fail, so that I can still inspect what arrived.
64. As a test author, I want failures to go to an `onError(opResult, opArgs)` handler (falling back to the one given to `open`) whose non-`nil` return value lets the script continue, so that I can handle expected failures without `pcall`.
65. As a test author, I want an unhandled Device failure to raise and record `ERROR`, so that a run never continues against a dead instrument by accident.
66. As a test author, I want incoming `SERIAL`, `TCP` and `UDP` data buffered from `open` to `close`, so that a reply that arrives before I read is never lost.
67. As a test author, I want `VISA` to read on demand with no background reader, so that instruments don't log query errors.
68. As a test author, I want `ATE.flush(dev)` to discard buffered data and tell me how many bytes it dropped, so that I can clear noise before sending a command.
69. As a test author, I want the buffer capped by config, dropping the oldest data with a warning and `overflow = true` on the next read, so that a chatty Device can't exhaust memory.
70. As a test author, I want a background failure (cable unplugged, connection reset) logged immediately and reported on my next `read` or `write`, so that I see the real cause.
71. As a test author, I want to recover from a Device failure by closing and reopening the Device, with no automatic reconnect, so that flaky hardware is never hidden.
72. As a test author, I want `write` to honour `timeout` and report the bytes actually sent, so that a stuck serial port can't hang a step.
73. As an Operator, I want every Device still open at run end closed by ATE with a warning, so that the next run starts clean.
74. As a test engineer, I want optional Device tracing of every operation at `DEBUG`, so that I can diagnose an instrument conversation.

### Running a test

75. As an Operator, I want to enter Operator, Project (prefilled from the config), UUT Type and serial as required fields, so that every Test Run is attributed.
76. As an Operator, I want one flat list of every visible Test Schedule with its Media Release, broken ones shown with their errors and mismatches flagged, so that I pick the right schedule confidently.
77. As an Operator, I want to see the expanded step tree and enable or disable steps (and whole Sub-schedules through their header) before starting, so that I can run only what's needed.
78. As an Operator, I want a Stop on Fail / continue on fail choice prefilled from the schedule, so that I can run attended or unattended.
79. As an Operator, I want a confirmation only when a checksum mismatches, listing the files and offering to store their bytes with the run, so that normal runs start with one click.
80. As an Operator, I want every step listed with its inputs always visible, each input enabled only while it's needed, so that I always know what's coming and what's being asked.
81. As an Operator, I want the run to pause on a failure with Stop on Fail and offer Continue, Retry or Abort, so that I can fix a probe and retry without restarting the run.
82. As an Operator, I want Retry to rerun the step from the start with inputs cleared and every Attempt recorded, so that the record shows what happened.
83. As an Operator, I want to abort the whole run at any time and have it stop within a fraction of a second, even in a busy Lua loop, so that I can stop a test immediately.
84. As an Operator, I want Post-test Actions to run even after an abort (and a second abort to end only the current one), so that supplies are still powered down safely.
85. As an Operator, I want a Pre-test Action failure to end the run automatically, with every step `NOTRUN` and the log explaining why, so that a broken setup never produces misleading results.
86. As an Operator, I want a status bar showing Pre-test/Post-test progress, total run time and time in the current action or step, so that I can judge whether something has hung.
87. As an Operator, I want the log pane to show `INFO` and above and a debug window to show everything, so that I see what matters and can dig deeper.
88. As an Operator, I want New run to keep every field, so that testing the next unit needs only the new serial.
89. As an Operator, I want the staged layout (UUT and Operator, Test Schedule, Steps and options, then a focus view that enlarges the current step), so that I can read progress from across the bench.
90. As an Operator, I want the three-pane layout (setup and schedule list, step table with Result, Attempts, Actual, Expected and Message, and the log), so that I can see everything at once.
91. As an admin, I want to set the default layout and optionally lock the View menu switch, so that Stations behave consistently.
92. As an Operator, I want to switch layouts mid-run without affecting the run, so that I can use whichever view suits the moment.
93. As an Operator, I want the GUI to stay responsive throughout a run, so that `POLL` buttons and Abort always work.

### Recording

94. As a quality engineer, I want each Test Run keyed by a UUIDv7 and recorded with its Station UUID and name, Operator, Project, UUT Type and serial, ATE version, schedule, Stop on Fail, times, UTC offset, result and End Reason, so that every run is fully attributed.
95. As a quality engineer, I want the resolved Media Release chain and every file used with expected and computed checksums recorded, so that I can prove which code ran.
96. As a quality engineer, I want mismatched files' bytes stored with the run according to `database.storeMismatchedFiles` (`ask`, `always`, `never`), so that suspicious code can be analysed later.
97. As a quality engineer, I want the expanded tree (headers, Step Numbers and Names, default and actual enables, input declarations), every action, every Attempt with its prompt choice, every input value, every Equipment declaration and every log line recorded, so that reports regenerate from the database alone.
98. As a quality engineer, I want writes to happen incrementally and a run left unfinished by a crash to be closed as `INTERRUPTED` / `FAIL` at the next launch, so that no run is ever lost or left open.
99. As a quality engineer, I want Test Runs to be append-only with no edit, delete or purge, so that records can be trusted.
100. As an architect, I want storage behind a `RunStore` interface with dialect-neutral tables, so that a central Postgres or MariaDB database can be added later.

### Reports

101. As an Operator, I want the HTML and PDF Test Report written automatically at run end, so that every run has a report without extra steps.
102. As an Operator, I want report files named by UUT Type, serial, local start time and result, never overwritten, so that reports are easy to find and never lost.
103. As a quality engineer, I want the cover to show the result and End Reason, banners for mismatches and interrupted runs, run and schedule details, the Operator with a signature box and date line, `Result` counts and the failures, so that the outcome is clear at a glance.
104. As a quality engineer, I want the Steps table to show Step Number, Step Name with the message underneath, Expected, Actual and Result, with input values, Operator overrides, retried Attempts as sub-rows and header rows with roll-ups, so that every step's story is on the page.
105. As a quality engineer, I want Equipment, Configuration (releases, files and checksums, Sub-schedule instances) and the Run log (`INFO` and above, grouped by owner) sections, so that the report is a complete record.
106. As a quality engineer, I want every page to carry a header (organisation, logo, document and UUT) and a footer (run UUID, ATE versions, page X of Y), so that loose pages are identifiable.
107. As an Operator, I want PDF rendering to run in a separate helper process and the HTML to be written even if the PDF fails, so that the GUI never stalls and a report is always produced.

### History

108. As a quality engineer, I want a flat, sortable table of runs, newest first, filterable by UUT Type, serial, result, End Reason, date range and Project, and loaded page by page, so that I can find any run quickly.
109. As a quality engineer, I want a summary panel for the selected run with Open report, Regenerate (HTML/PDF, with or without the Run log), Export log, Show all runs of this UUT and New run for this UUT, so that I can act on a run directly.
110. As an Operator, I want to browse History during a run (unless the admin disables it), with the running run shown as "running", so that I can check previous results without stopping.
111. As a test engineer, I want to export a run's log as CSV, JSON Lines or Text, filtered by level, source and owner, so that I can analyse it in other tools.

### Delivery

112. As a maintainer, I want CI to test on Windows and Linux with Python 3.13 and 3.14, so that both platforms and versions stay working.
113. As a maintainer, I want every tagged release to build and smoke-test the Windows installer, `.tar.gz`, `.deb` and wheel, so that no broken artifact ships.
114. As a maintainer, I want ATE versioned by semver from the git tag, so that the version on every Test Run and report is meaningful.
115. As a maintainer, I want code signing as an optional build step, so that builds can be signed as soon as a certificate is available.

## Implementation Decisions

### Architecture

- Three layers: **engine** → **run session** → **layouts**. The engine and run session never import widgets; layouts render the run session and forward user actions to it, holding no run logic. This is an architectural rule.
- The engine runs each Test Run on one `QThread` worker. `ATE.input`, `ATE.sleep` and `ATE.read` are Python calls that block that thread; lupa releases the GIL while Lua runs. The worker reaches the GUI only through queued Qt signals. ATE uses no coroutines internally.

### Modules

- **Config**: loads and validates the machine-wide JSON config (`format: 1`, camelCase sections, unknown keys rejected), resolves paths, manages `station.uuid` write-back, and handles the missing-config prompt. Read once at launch.
- **Catalog** (Test Definition scanner and resolver): finds `.atedef` files recursively, validates them, resolves dependencies (npm-style semver ranges, transitive, highest match), computes checksums, maps module names, and produces the list of visible Test Schedules and broken entries with their errors. Interface: scan and rescan; look up a schedule; resolve a module or include path for a given release.
- **Lua host**: creates one fresh `LuaRuntime` per load (each Test Run, and each scan-time load), from `lupa.lua55` with `register_eval=False`, `register_builtins=False` and `unpack_returned_tuples=True`. It refuses to start unless the Lua version is 5.5. Its prelude:
  - provides the `ScheduleParams`/`StepParams` builders, the enums (`Result`, `Log`, `DeviceType`, `Parity`, `FlowControl`) and the `ATE` table;
  - replaces `require` (resolving through the Catalog, plus the reserved `ate.` namespace for bundled modules);
  - removes `dofile`, `loadfile` and `load`, and sets `python` to `nil`;
  - loads chunks with the name `@<path>` and mode `t`;
  - installs a count hook every 1000 instructions for abort, and wraps `pcall`/`xpcall` to re-raise `"aborted"`.
  
  Python halves of `ATE` functions return `(ok, value)` to a Lua shim, which raises with the caller's `file:line`, so no Python exception ever reaches Lua.
- **Schedule loader**: loads a `.tslua`, validates the returned objects, expands the Sub-schedule tree (instances, `keyvals` as the chunk argument, prefixes, duplicate and cycle checks, header forcing of enables), and produces the expanded tree used both for listing and for running.
- **Device layer**: a registry of one driver per Device Type (`SERIAL` through pyserial, opened with `serial_for_url` so `port` accepts COM/tty names and pyserial URLs; `TCP`; `UDP`; `VISA` through pyvisa with whatever backend it finds, with pyvisa-py bundled).
  - It validates params strictly.
  - It reads `SERIAL`, `TCP` and `UDP` Devices in the background into a buffer capped by `devices.receiveBufferBytes`, dropping the oldest data on overflow; `VISA` reads on demand.
  - It implements read termination, partial reads, write timeouts, flush, background-failure marking and tracing.
  - It closes leftover Devices at run end.
- **Run engine**: flow control.
  - All Pre-test Actions run up front, depth first and parent first. Post-test Actions run in reverse, always, after clearing the abort flag. Only schedules whose Pre-test Action started get a Post-test Action, and fully disabled Sub-schedules are skipped.
  - Steps run in tree order through an `xpcall(fn, debug.traceback)` wrapper. Stop on Fail opens a Continue/Retry/Abort prompt. Every Attempt is recorded, and the step's result is its last Attempt's.
  - A Pre-test failure aborts automatically. An Operator abort makes the running step `ERROR` "Aborted by Operator".
  - It sets the End Reason (`COMPLETED`, `PRETEST_FAILED`, `STOPPED_ON_FAIL`, `ABORTED`) and the run result (`FAIL` on any `FAIL` or `ERROR`).
- **RunStore**: the only storage interface. SQLAlchemy Core tables written once and dialect-neutral (36-character text UUIDs, ISO 8601 UTC text times with milliseconds, 0/1 booleans, JSON as text). SQLite in WAL mode through stdlib `sqlite3`.
  - Tables: `test_run`, `media_release`, `run_file`, `schedule_instance`, `node`, `action`, `attempt`, `input_value`, `equipment` and `log_line`.
  - Run rows are keyed by UUIDv7, and child rows by `(run_uuid, seq)`.
  - Writes are incremental, with log lines batched about once a second. Runs left unfinished are closed as `INTERRUPTED` at launch. The schema version lives in `PRAGMA user_version`.
- **Run session**: the UI-free object a layout binds to. It holds setup fields, the schedule list, the expanded tree with enables and results, Attempts, input state, prompt state, timers, status-bar progress and log lines. It exposes commands (scan or rescan, select a schedule, set fields, toggle a node, set Stop on Fail, start, confirm a mismatch with or without storing files, supply an input or click a `POLL` button, choose Continue/Retry/Abort, abort, new run) and emits Qt signals for state changes.
- **Report generator**: renders a self-contained HTML report from the RunStore alone, using a bundled Jinja2 template (inline CSS, logo as a data URI). Files are named `<UUT Type>/<UUT Type>_<serial>_<local start>_<PASS|FAIL>`, with suffixes and never overwritten. It can leave out the Run log section.
- **PDF helper**: a separate ATE process that loads the saved HTML file (never `setHtml`) into QWebEngine and runs `printToPdf`. The page furniture uses CSS `@page` margin boxes, and the `QPageLayout` margins must equal the CSS margins. Paper is A4 or Letter.
- **Log exporter**: exports one finished run's Run log as CSV (UTF-8 with BOM), JSON Lines or Text, filtered by level, source and owner, in recorded order.
- **Layouts**: staged (default) and three-pane Run screens, the History screen, and the debug window. `ui.layout` sets the default layout, and the View menu switch can be disabled with `ui.allowLayoutSwitch`. `ui.allowHistoryDuringRun` controls History during a run.
- **CLI** (`ate`):
  - `ate setup` (admin or root): creates the directories, ACLs or `ate` group and a default config, then runs migrate.
  - `ate db migrate` (backs up first; creates a missing database after confirmation) and `ate db status`.
  - `ate definition update <file.atedef>`.
  
  The GUI never migrates the schema.

### Contracts worth stating precisely

- **Device read results**: complete reads return `{data, count, partial=false}`. A timeout after some bytes returns `partial=true` and logs a warning. A timeout with no bytes returns `err="timeout"` and goes through `onError`. Any read result may carry `overflow=true`. Writes return `{ok, count}`, where `count` is the bytes actually sent. `close` returns `{ok}` and `flush` returns `{ok, count}`.
- **`onError(opResult, opArgs)`**: a non-`nil` return value becomes the call's return value and the script continues. A `nil` return, or no handler at all, raises. Errors that aren't Device failures always raise.
- **Where calls are allowed**: at load time only the builders, `require`, `ATE.log` and the enums; Pre-test and Post-test Actions can use everything except `input` and `report`; steps can use everything.
- **Mismatch handling**: a checksum mismatch warns and still loads. It is flagged on the Run screen and the Test Report, and recorded. With `storeMismatchedFiles=ask`, the run-start confirmation (shown only when there is a mismatch) has a "Store mismatched files" checkbox, ticked by default. Headless, nobody answers it: the run starts and the setting decides, so `always` stores the bytes while `ask` and `never` store nothing.

### Packaging

- **Windows (supported):** a PyInstaller one-directory build inside an Inno Setup installer, bundling CPython 3.14.
- **Linux (best effort):** a `.tar.gz` with `install.sh`; a self-contained `.deb` for Debian stable (under `/opt/ate`, with `/usr/bin/ate` linking to it); and a wheel supporting CPython 3.13 and 3.14.
- No ATE artifact contains Media Releases.
- Signing is optional; there is no certificate yet.
- ATE is versioned by semver from the git tag. Schema changes are noted in the release notes.

## Testing Decisions

- **What a good test is**: it drives ATE through a public seam and checks externally visible outcomes: run session state, the Test Run record read back through the RunStore, generated report HTML, exported files, and CLI effects. It never checks internal calls or private structures. Tests use real Lua (lupa 5.5), real SQLite in a temporary directory and real Device drivers wherever a loopback peer exists.
- **Seam 1, the headless run session (primary):** tests build a temporary config and data directory with sample Media Releases (`.atedef`, `.tslua`, `.lua`) on disk. They create the engine and run session under a non-GUI Qt event loop and drive them as a layout would: scan, select, fill fields, toggle steps, start, answer inputs, click `POLL`, choose Continue/Retry/Abort, abort, then regenerate the report or export the log. This one seam covers the Test Definition rules, the schedule format, Sub-schedules, the `ATE` table, flow control, checksums, recording, reports and log export.
  - **Devices in seam 1:**
    - `TCP` and `UDP` talk to loopback sockets the test controls.
    - `SERIAL` uses a pyserial `socket://localhost:<port>` URL as its `port`, so the test acts as the peer.
    - `VISA` is the only substituted piece: tests register a scripted fake `VISA` driver in the Device registry.
  - **Covers, among others:** abort latency in busy loops and inside `pcall`, interrupted-run recovery (kill mid-run, relaunch), buffer overflow and flush, background failures, partial reads, write timeouts, and every End Reason.
- **Seam 2, the `ate` CLI:** `ate setup`, `ate db migrate` / `ate db status` and `ate definition update`, run against temporary directories. Checks: the files, permissions and group created, the schema version, backups, refusal on a newer database, creation of a missing database after confirmation, and rewritten checksums.
- **Not seams:**
  - **Layouts:** they hold no logic. A pytest-qt smoke test builds each layout (staged, three-pane, History) on a run session and runs one short schedule end to end.
  - **The PDF helper:** one integration test renders a generated HTML report to PDF.
  - **Installers:** covered by the CI smoke tests on tagged releases. Each artifact is installed in a clean Windows runner or Debian stable container, `ate setup` is run, and ATE is launched headless far enough to scan a sample Media Release and check the database version.
- **CI matrix:** Windows and Linux × CPython 3.13 and 3.14.
- **Prior art:** none. The repository has no code yet, so these tests set the patterns. The throwaway prototypes are design references, not tests:
  - `prototype/run-screen`: the simulated run engine in HTML;
  - the Windows Lua worker prototype: abort, `pcall` and traceback checks;
  - the PDF prototype: QWebEngine rendering.

## Out of Scope

- A multi-site central database. The `RunStore` interface and config shape must not preclude it, but v1 runs only SQLite.
- Branching and looping in Test Schedules (v2).
- Parallel execution of Test Steps or UUTs.
- Reports summarising many Test Runs.
- Logins and roles.
- A GUI for editing configuration.
- Lua sandboxing (Lua is trusted; the loading restrictions exist for traceability only).
- A custom Lua build for lupa, and Lua C modules.
- Report generators scripted in Lua, and user-supplied report templates.
- Cryptographically signing Test Reports, and PDF signature fields (v2).
- Linking Equipment to the Devices a schedule opens.
- Device discovery (listing COM ports or VISA resources).
- Automatic Device reconnection.
- Pause, resume or skip during a run (only whole-run abort).
- Deleting or editing Test Runs.
- `.rpm` packages, 32-bit or ARM Windows, and free-threaded Python builds.

## Further Notes

- The detailed definitions live with the ATE v1 map, and this spec summarises them. When implementing a module, read its definition document:
  - **Test Schedule format:** `test-schedule-format`
  - **`ATE` table:** `ate-table-reference`
  - **Test Definition format:** `test-definition-format`
  - **Application config:** `app-config-format`
  - **Test Run record schema:** `test-run-record`
  - **Test Report:** `test-report-format`
  - **Run log export:** `log-export`
  - **Run screen:** `run-screen`
  - **History screen:** `history-screen`
  - **Packaging:** `packaging`

  All of these are in `.scratch/ate-v1-wayfinding/assets/`. Domain vocabulary is in the glossary.
- The decisions behind each area, including the reasons for them, are in the resolved wayfinding tickets linked from "Decisions so far" in `.scratch/ate-v1-wayfinding/map.md`. Implementation tickets for this spec go in `.scratch/ate-v1/issues/`.
- The Windows prototype found five refinements to the Lua host that are easy to miss:
  - re-raise `"aborted"` from `pcall` and `xpcall`;
  - never let a Python exception reach Lua;
  - `unpack_returned_tuples=True`;
  - set `python` to `nil`;
  - load chunks as `@<path>` in text mode.
- QWebEngine adds about 341 MB to the Windows install. PDF rendering in the GUI process stalled the GUI on Windows, which is why it runs in a helper process.
- Pin `lupa>=2.8`, and move to 2.9 when it's released for its Lua 5.5 garbage-collector fix.
