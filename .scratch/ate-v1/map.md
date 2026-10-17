# Map: ATE v1

Label: wayfinder:map

## Destination

A v1 spec for ATE that `/to-spec` can turn into tickets: requirements agreed, Lua runtime chosen, Test Schedule format fixed, and the database and report design decided.

## Notes

- Domain: Python + PyQt6 desktop app that runs Lua Test Schedules, mostly against hardware UUTs. Vocabulary in `GLOSSARY.md`; use it.
- The Test Schedule format is defined in `assets/test-schedule-format.md`, the `ATE` table in `assets/ate-table-reference.md`, and the Test Definition in `assets/test-definition-format.md`.
- Settled during charting:
  - Test Step order is the position in the returned array; Python orchestrates execution, sequentially for now.
  - Each Test Step holds a Lua function that Python calls.
  - Target **Lua 5.5**, through the stock `lupa` wheels (`lupa.lua55`), on 64-bit CPython 3.13 and 3.14; CI runs Windows and Linux on both.
  - A step reports via `ATE.report(passFail, msg, actual, expected)`; `passFail` is `Result.PASS`, `Result.FAIL`, `Result.ERROR`, `Result.SKIPPED`, `Result.NOTRUN`, `Result.FORCED_NOTRUN` or `Result.NOREPORT`. `SKIPPED`, `NOTRUN` and `FORCED_NOTRUN` are used at the test writer's discretion. `actual` and `expected` are stored as text; the schedule does its own comparison and chooses `passFail`. Every call is logged, but only the last counts. A step that never reports records `NOREPORT` and logs a warning.
  - A Test Run's result is `PASS` or `FAIL`; it fails if any step is `FAIL` or `ERROR`. The Test Report's summary cover page shows a count of steps for each `Result` value. The Post-test Action receives only `passFail`.
  - Before a run starts, the Operator can enable or disable individual Test Steps; `setEnable` only sets the default. A step disabled at start records `Result.NOTRUN`.
  - Test Schedules' own use of Lua coroutines need not be supported.
  - `PROMPT` Step Inputs block until the Operator clicks continue. `POLL` Step Inputs are non-blocking: `nil` until a button is clicked, then the button's text (sticky; reset at run and step start), and clickable at any time while the step runs.
  - `ATE.input(n)` pauses the running step function while the GUI collects the value for the step's n-th declared input (text box or radio buttons).
  - `ATE.open(DeviceType.X, params [, onError])` takes a plain table of connection details and returns an opaque device object, the first argument to `read`/`write`/`close`. Driver complexity stays in Python. v1 Device Types are `SERIAL`, `TCP`, `UDP` and `VISA`; ATE buffers incoming data until `ATE.read`.
  - `ATE.log(Log.X, ...)` has levels; everything above debug shows in the main UI, and a separate debug window shows all levels.
  - Auxiliary Lua files load only through ATE's replacement `require`, restricted to files listed in a Test Definition or its dependencies; it warns on checksum mismatch. Each Test Run records the expected and computed checksum of every file used.
  - An application config JSON is read at launch; defined in `assets/app-config-format.md`.
  - v1 is one Station with a local database. Operator is a free-text name. Lua is trusted.
  - Reports cover a single Test Run, as HTML with PDF rendered from it, generated at run end or later from the database.
  - GUI v1: Run screen and History screen; configuration is edited by hand.
  - Windows and Linux both supported; Windows primary.
- Grilling tickets: use the `grilling` and `domain-modeling` skills.

## Decisions so far

- [Draft the ATE table](issues/15-ate-table-reference.md): a first draft of the `ATE` table: `open`/`read`/`write`/`close` with `onError` handlers, plus timing, `input`, logging and `report`. Its conflicts with earlier decisions went to "The ATE Lua API".
- [Draft the Lua schedule format](issues/01-lua-schedule-spec.md): `.tslua` files return a `ScheduleParams` builder (report doc fields, Pre-test and Post-test Actions, array of `StepParams` steps with Step Number, enable, Step Inputs, action, Sub-schedule include); written up in `assets/test-schedule-format.md`.
- [Survey of the Python to Lua binding candidates](issues/02-lua-binding-survey.md): lupa is the only viable binding (bundles Lua 5.1–5.5 and LuaJIT, Windows and Linux packages); pausing in `ATE.input` works through a Lua-side coroutine yield (not inside `pcall` on 5.1) or a worker thread.
- [Options for turning HTML reports into PDF on Windows and Linux](issues/03-html-pdf-options.md): Qt QTextDocument is free but has weak CSS; QWebEngine and Playwright have full CSS but a heavy footprint; WeasyPrint is strong but hard to install on Windows; xhtml2pdf is easy but unproven.

- [The ATE Lua API](issues/05-ate-lua-api.md): `ATE.report(passFail, msg, actual, expected)` with the `Result` enum, `ATE.log(Log.X, ...)`, `ATE.sleep`, a read-only `ATE.run`, and `ATE.open(DeviceType.X, params [, onError])` with plain params tables and a shipped builder utility. A non-`nil` return from `onError` lets the script continue; otherwise the failure raises and the step records `ERROR`. Defined in `assets/ate-table-reference.md`.
- [Device connection details and v1 connection types](issues/06-device-connection-info.md): `SERIAL`, `TCP`, `UDP`, `VISA`, raw bytes, all `open` fields required with defaults supplied by builders in the bundled `ate.devices` module, optional per-call overrides, uniform `{data, count, partial}` read results with partial reads not an error, background buffering until `read`, and Device tracing as an off-by-default setting.
- [Choose the Lua runtime](issues/09-choose-lua-runtime.md): stock lupa wheels pinned to `lupa.lua55`, one `QThread` worker and a fresh `LuaRuntime` per Test Run or load, abort via blocking calls plus a count hook, `xpcall` tracebacks for `ERROR`, loading locked down (no `dofile`/`loadfile`/`load`/`python.*`), `VISA` reads on demand, CPython 3.13 and 3.14 in CI. A Windows prototype (16) checks it.
- [Flow control](issues/10-flow-control.md): stop on fail by default (`setStopOnFail`, changeable before the run), with a Continue / Retry / Abort prompt on failure; every Attempt recorded and the last one counts; Pre-test failure aborts automatically; end reasons `COMPLETED`, `PRETEST_FAILED`, `STOPPED_ON_FAIL`, `ABORTED`. All Pre-test Actions run up front depth first and Post-test Actions in reverse, always, even after an abort. Sub-schedules are in v1.
- [Test Step structure and outcome model](issues/04-test-step-structure.md): v1 steps keep only Step Number, enable, inputs and action, with every Step Input type supported. `ATE.input(n)` return values are fixed per type, and a schedule with a duplicate Step Number fails to load. A new `Result.ERROR` covers Lua errors, and a run fails on any `FAIL` or `ERROR`.
- [Test Definition format](issues/07-test-definition-format.md): a JSON `.atedef` file with `format`, `mediaNumber` (unique identity, not a version), `name`, semver `version`, and a `files` list of `.tslua` and `.lua` files with raw-byte SHA-256 checksums. `depends` takes name plus semver range and resolves transitively. Mismatches warn, while missing files, bad entries and duplicates make the release broken. There is no Project in the definition; Project is per Test Run. The Operator picks from one flat list of every visible schedule. Defined in `assets/test-definition-format.md`.
- [Application config file](issues/08-app-config-file.md): a machine-wide `config.json` (`%PROGRAMDATA%\ATE`, `/etc/ate`), read at launch. A missing file prompts to create defaults or abort; an invalid file stops startup. It holds `station.name` and a generated `station.uuid` that is written back to the file, data-directory default paths, a per-launch application log that is never rotated or deleted, Device tracing, `defaultProject` and `ui.theme`. Defined in `assets/app-config-format.md`.

## Not yet specified

- What the History screen does beyond browsing Test Runs and regenerating reports.
- How a Device failing mid-run is handled.
- The Device receive buffer: its size limit, what happens on overflow, and whether a schedule can discard buffered data before a command.
- Recording instrument identity and calibration on a Test Run.
- Packaging and installers, especially for Windows.
- A custom Lua build for lupa (patched `luaconf.h`, extra C libraries, Lua C modules); possible but means building our own wheels. See `research/lupa-custom-lua.md` on branch `research/lupa-custom-lua`.

## Out of scope

- Multi-site central database (wanted later; the v1 schema must not preclude it).
- Branching and looping in Test Schedules (v2).
- Parallel execution of Test Steps or UUTs.
- Reports summarising many Test Runs.
- Logins and roles.
- A GUI for editing configuration.
- Lua sandboxing.
