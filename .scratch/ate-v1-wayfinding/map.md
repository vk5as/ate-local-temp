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
  - GUI v1: Run screen and History screen (`assets/history-screen.md`); configuration is edited by hand. The Run screen has two layouts, staged and three-pane (`assets/run-screen.md`).
  - Architectural rule: the engine and the run session never import widgets, and layouts hold no run logic, so layouts are interchangeable and the engine is testable without a GUI.
  - Windows and Linux both supported; Windows primary.
- Grilling tickets: use the `grilling` and `domain-modeling` skills.

## Decisions so far

- [Settle the remaining Not yet specified items](issues/19-remaining-fog.md): History is a flat, filterable table with per-run actions and no delete (`assets/history-screen.md`); a failing Device is logged and marked failed, with no automatic reconnect; the receive buffer is capped and drops the oldest data, and `ATE.flush` empties it; a supported Windows installer plus best-effort `.tar.gz`, Debian `.deb` and wheel, all prepared by `ate setup` (`assets/packaging.md`); a custom Lua build is out of scope.
- [Run screen, input forms, log pane and debug window](issues/12-run-screen-prototype.md): two layouts on one UI-free run session, staged (default; setup stages UUT and Operator, Test Schedule, Steps and options; a focus run view) and three-pane (step table, inline failure prompt), chosen by `ui.layout` with an optional View menu switch; Operator is required; run-start confirmation only on a checksum mismatch; New run clears nothing. Defined in `assets/run-screen.md`.
- [Draft the ATE table](issues/15-ate-table-reference.md): a first draft of the `ATE` table: `open`/`read`/`write`/`close` with `onError` handlers, plus timing, `input`, logging and `report`. Its conflicts with earlier decisions went to "The ATE Lua API".
- [Draft the Lua schedule format](issues/01-lua-schedule-spec.md): `.tslua` files return a `ScheduleParams` builder (report doc fields, Pre-test and Post-test Actions, array of `StepParams` steps with Step Number, enable, Step Inputs, action, Sub-schedule include); written up in `assets/test-schedule-format.md`.
- [Survey of the Python to Lua binding candidates](issues/02-lua-binding-survey.md): lupa is the only viable binding (bundles Lua 5.1–5.5 and LuaJIT, Windows and Linux packages); pausing in `ATE.input` works through a Lua-side coroutine yield (not inside `pcall` on 5.1) or a worker thread.
- [Options for turning HTML reports into PDF on Windows and Linux](issues/03-html-pdf-options.md): Qt QTextDocument is free but has weak CSS; QWebEngine and Playwright have full CSS but a heavy footprint; WeasyPrint is strong but hard to install on Windows; xhtml2pdf is easy but unproven.

- [The ATE Lua API](issues/05-ate-lua-api.md): `ATE.report(passFail, msg, actual, expected)` with the `Result` enum, `ATE.log(Log.X, ...)`, `ATE.sleep`, a read-only `ATE.run`, and `ATE.open(DeviceType.X, params [, onError])` with plain params tables and a shipped builder utility. A non-`nil` return from `onError` lets the script continue; otherwise the failure raises and the step records `ERROR`. Defined in `assets/ate-table-reference.md`.
- [Device connection details and v1 connection types](issues/06-device-connection-info.md): `SERIAL`, `TCP`, `UDP`, `VISA`, raw bytes, all `open` fields required with defaults supplied by builders in the bundled `ate.devices` module, optional per-call overrides, uniform `{data, count, partial}` read results with partial reads not an error, background buffering until `read`, and Device tracing as an off-by-default setting.
- [Choose the Lua runtime](issues/09-choose-lua-runtime.md): stock lupa wheels pinned to `lupa.lua55`, one `QThread` worker and a fresh `LuaRuntime` per Test Run or load, abort via blocking calls plus a count hook, `xpcall` tracebacks for `ERROR`, loading locked down (no `dofile`/`loadfile`/`load`/`python.*`), `VISA` reads on demand, CPython 3.13 and 3.14 in CI. Confirmed on Windows by "Lua worker thread on Windows".
- [Flow control](issues/10-flow-control.md): stop on fail by default (`setStopOnFail`, changeable before the run), with a Continue / Retry / Abort prompt on failure; every Attempt recorded and the last one counts; Pre-test failure aborts automatically; end reasons `COMPLETED`, `PRETEST_FAILED`, `STOPPED_ON_FAIL`, `ABORTED`. All Pre-test Actions run up front depth first and Post-test Actions in reverse, always, even after an abort. Sub-schedules are in v1.
- [What a Test Run records](issues/11-test-run-record.md): SQLite behind a `RunStore` interface with dialect-neutral SQLAlchemy Core tables; UUIDv7 run keys with `(run_uuid, seq)` child keys; incremental, append-only writes, with unfinished runs closed as `INTERRUPTED`; the expanded tree, actions, Attempts, inputs and every log line stored so reports regenerate from the database alone; mismatched files optionally stored; migrations only through the `ate db` CLI. Defined in `assets/test-run-record.md`.
- [Test Step structure and outcome model](issues/04-test-step-structure.md): v1 steps keep only Step Number, enable, inputs and action, with every Step Input type supported. `ATE.input(n)` return values are fixed per type, and a schedule with a duplicate Step Number fails to load. A new `Result.ERROR` covers Lua errors, and a run fails on any `FAIL` or `ERROR`.
- [Test Definition format](issues/07-test-definition-format.md): a JSON `.atedef` file with `format`, `mediaNumber` (unique identity, not a version), `name`, semver `version`, and a `files` list of `.tslua` and `.lua` files with raw-byte SHA-256 checksums. `depends` takes name plus semver range and resolves transitively. Mismatches warn, while missing files, bad entries and duplicates make the release broken. There is no Project in the definition; Project is per Test Run. The Operator picks from one flat list of every visible schedule. Defined in `assets/test-definition-format.md`.
- [Application config file](issues/08-app-config-file.md): a machine-wide `config.json` (`%PROGRAMDATA%\ATE`, `/etc/ate`), read at launch. A missing file prompts to create defaults or abort; an invalid file stops startup. It holds `station.name` and a generated `station.uuid` that is written back to the file, data-directory default paths, a per-launch application log that is never rotated or deleted, Device tracing, `defaultProject` and `ui.theme`. Defined in `assets/app-config-format.md`.
- [Sub-schedules](issues/14-sub-schedules.md): `setInclude` resolves like a `files` path, searching dependency releases as `require` does. The tree is expanded at scan and at run start, and a bad include breaks only the including schedule. Each include runs the chunk again with plain-data `keyvals` as `...`. Prefixes are prepended as they are and stack through nesting, with no cycles allowed. The include step is a header row with no `Result` that forces the enable state beneath it. Every step gains an optional Step Name, `StepParams(name)`.
- [Test Report content and format](issues/13-test-report-format.md): HTML and PDF are written automatically at run end and can be regenerated from History with the current template; files have readable names and are never overwritten. Sections: Cover (result, End Reason, Operator signature box, a count for each `Result`, failures), Steps, Equipment (from a new `ATE.addEquipment`), Configuration (only the files actually used) and Run log (`INFO` and above). The PDF engine was settled by "Render a sample Test Report to PDF". Defined in `assets/test-report-format.md`.
- [Render a sample Test Report to PDF](issues/17-report-pdf-prototype.md): QWebEngine `printToPdf` (better looking, 3–15x faster than xhtml2pdf, about 341 MB on Windows), run in a separate ATE helper process so screens never stall. CSS `@page` margin boxes draw the page furniture, with the Qt margins matching the CSS. History can regenerate a report without the Run log.
- [Lua worker thread on Windows](issues/16-lua-worker-windows.md): the runtime design holds on Windows. The GUI stalls at most about 30 ms, and Abort takes 28–43 ms. Refinements: `pcall`/`xpcall` re-raise after an abort, `ATE` errors are strings raised at the caller's line, `unpack_returned_tuples=True`, no `python` table, and chunks load by file name as text only.
- [Export a Test Run's log from History](issues/18-log-export.md): History exports one finished run's Run log as CSV (default), JSON Lines or Text. Checkboxes pick the levels, sources and owners. There is no log viewer in ATE, and only the Text header records the filters. Defined in `assets/log-export.md`. New term: Run log.
- [Project skeleton, `ate` CLI and CI](issues/01-project-skeleton.md): an installable src-layout package with an `ate` entry point whose `--version` prints a semver from the git tag at build time (safe dev fallback when untagged/git-less), deps + pytest/pytest-qt test extra, and a push CI matrix of Windows and Linux × CPython 3.13/3.14. The engine → run session → ui layers are separated so "engine and run session never import widgets" is enforced by an import-lint test; version lives in one place (`src/ate/_version.py`, read via setuptools dynamic versioning). No Lua runtime yet — deferred. Built on branch `ticket-01-project-skeleton` (commits `31f95ca`–`f4e4c33`).
- [Tracer bullet run](issues/02-tracer-bullet-run.md): the thinnest end-to-end path — a headless run session starts a one-step Test Run on a single `QThread` worker and records it through the only storage interface, `RunStore`; the engine reaches the session only via queued Qt signals (it never imports widgets). The `ate db` CLI gains `migrate`/`status`; at launch ATE refuses a missing or older database ("run `ate db migrate`") and a newer one ("this ATE is too old"), never migrating itself. Tables are `test_run`, `node`, `attempt` with UUIDv7 run keys; config and `.atedef` reading stay minimal, full validation deferred. Real Lua execution is stubbed (canned results from params) so the seams are testable offscreen.

## Not yet specified

Nothing. Every item was settled in "Settle the remaining Not yet specified items".

## Out of scope

- Multi-site central database (wanted later; the v1 schema must not preclude it).
- Branching and looping in Test Schedules (v2).
- Parallel execution of Test Steps or UUTs.
- Reports summarising many Test Runs.
- Logins and roles.
- A GUI for editing configuration.
- Lua sandboxing.
- A custom Lua build for lupa (patched `luaconf.h`, extra C libraries, Lua C modules); possible but means building our own wheels. See `research/lupa-custom-lua.md` on branch `research/lupa-custom-lua`.
- Report generators scripted in Lua, and user-supplied report templates.
- Cryptographically signing Test Reports to authenticate them, including PDF signature fields (v2).
- Linking Equipment to the Devices a schedule opens (for example by VISA `*IDN?`).
