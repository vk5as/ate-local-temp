# Map: ATE v1

Label: wayfinder:map

## Destination

A v1 spec for ATE that `/to-spec` can turn into tickets: requirements agreed, Lua runtime chosen, Test Schedule format fixed, and the database and report design decided.

## Notes

- Domain: Python + PyQt6 desktop app that runs Lua Test Schedules, mostly against hardware UUTs. Vocabulary in `GLOSSARY.md`; use it.
- The Test Schedule format is defined in `assets/test-schedule-format.md`, and the `ATE` table in `assets/ate-table-reference.md`.
- Settled during charting:
  - Test Step order is the position in the returned array; Python orchestrates execution, sequentially for now.
  - Each Test Step holds a Lua function that Python calls.
  - Target **Lua 5.5**.
  - A step reports via `ATE.report(passFail, msg, actual, expected)`; `passFail` is `Result.PASS`, `Result.FAIL`, `Result.ERROR`, `Result.SKIPPED`, `Result.NOTRUN`, `Result.FORCED_NOTRUN` or `Result.NOREPORT`. `SKIPPED`, `NOTRUN` and `FORCED_NOTRUN` are used at the test writer's discretion. `actual` and `expected` are stored as text; the schedule does its own comparison and chooses `passFail`. Every call is logged, but only the last counts. A step that never reports records `NOREPORT` and logs a warning.
  - A Test Run's result is `PASS` or `FAIL`; it fails if any step is `FAIL` or `ERROR`. The Test Report's summary cover page shows a count of steps for each `Result` value. The Post-test Action receives only `passFail`.
  - Before a run starts, the Operator can enable or disable individual Test Steps; `setEnable` only sets the default. A step disabled at start records `Result.NOTRUN`.
  - Test Schedules' own use of Lua coroutines need not be supported.
  - `PROMPT` Step Inputs block until the Operator clicks continue. `POLL` Step Inputs are non-blocking: `nil` until a button is clicked, then the button's text (sticky; reset at run and step start), and clickable at any time while the step runs.
  - `ATE.input(n)` pauses the running step function while the GUI collects the value for the step's n-th declared input (text box or radio buttons).
  - `ATE.open(DeviceType.X, params [, onError])` takes a plain table of connection details and returns an opaque device object, the first argument to `read`/`write`/`close`. Driver complexity stays in Python.
  - `ATE.log(Log.X, ...)` has levels; everything above debug shows in the main UI, and a separate debug window shows all levels.
  - Auxiliary Lua files load only through ATE's replacement `require`, restricted to files listed in a Test Definition; it warns on checksum mismatch. Each Test Run records the expected and computed checksum of every file used.
  - An application config JSON is read at launch; it includes the directories searched for Test Definitions.
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
- [Test Step structure and outcome model](issues/04-test-step-structure.md): v1 steps keep only Step Number, enable, inputs and action, with every Step Input type supported. `ATE.input(n)` return values are fixed per type, and a schedule with a duplicate Step Number fails to load. A new `Result.ERROR` covers Lua errors, and a run fails on any `FAIL` or `ERROR`.

## Not yet specified

- What the History screen does beyond browsing Test Runs and regenerating reports.
- Threading model for Lua execution vs the GUI; depends on the runtime chosen in "Choose the Lua runtime".
- How a Device failing mid-run is handled.
- Recording instrument identity and calibration on a Test Run.
- Packaging and installers, especially for Windows.
- Whether Media Release and Test Definition are distinct concepts (may settle in "Test Definition format").

## Out of scope

- Multi-site central database (wanted later; the v1 schema must not preclude it).
- Branching and looping in Test Schedules (v2).
- Parallel execution of Test Steps or UUTs.
- Reports summarising many Test Runs.
- Logins and roles.
- A GUI for editing configuration.
- Lua sandboxing.
