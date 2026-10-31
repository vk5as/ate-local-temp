# 04: Application config in full, and interrupted-run recovery

**What to build:** The machine-wide config behaves as specified (see `app-config-format`), and a Test Run left unfinished by a crash is closed out at the next launch.

**Blocked by:** 02 (Tracer bullet: run a one-step Test Schedule and record it)

**Status:** resolved

- [x] Config is found at `%PROGRAMDATA%\ATE\config.json` / `/etc/ate/config.json`, or `--config <path>`, and read once at launch (`read_config`)
- [x] A missing config prompts with the path: "Create default config and launch" or "Abort"; creating writes defaults and a fresh `station.uuid`; a failed write refuses to start (injectable `on_missing_prompt` callback)
- [x] Invalid JSON, a wrong type, an unknown key or an unknown `format` refuses to start, naming the file and key; `database.type` other than `sqlite` is rejected (`_check_type`, `_validate_and_fill`)
- [x] `station.name` defaults to the hostname; a missing `station.uuid` is generated and written back preserving key order, or startup is refused showing the UUID if the file can't be written (never copied between Stations)
- [x] Relative paths resolve against the config file's directory, absolute paths work, forward slashes work on Windows, no variable or `~` expansion; a missing Test Definition directory is a warning (`_resolve_path`)
- [x] Every documented key exists with its default (including `defaultProject`, `ui.*`, `devices.receiveBufferBytes`, `deviceTrace.*`, `reports.*`, `database.storeMismatchedFiles`)
- [x] The application log is one file per launch named by start time, at `log.level`, never rotated or deleted (`src/ate/log.py`)
- [x] At launch any Test Run with no end time is closed with End Reason `INTERRUPTED` and result `FAIL`, and this is logged (ticket 02's `close_interrupted_runs`, wired through the session's app-log logger)
- [x] Tests cover each refusal, the prompt path, UUID write-back and interrupted-run recovery (`tests/test_config.py`, `tests/test_app_log.py`, `tests/test_headless_session.py`)

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.

## Answer

**What was built.** Ticket 02's minimal config reader was expanded into full, schema-driven
validation and a launch-time application log was added; interrupted-run recovery at launch was
confirmed to use the real store API. The issue is closed on branch `ticket-04-app-config` (merged
linearly back into `integration/ate-v1`).

**Config validation — `src/ate/config.py`.** A documented schema (`_SCHEMA`, keyed
`section.key`) plus per-key type predicates enforce every value; a missing key takes its default.
`format` must be exactly 1, an unknown top-level key or wrong-typed value refuses startup naming
the file and `full` (e.g. `'ui.theme'`), and a non-`sqlite` backend is rejected (spec rule 24).
`defaultProject` is the one documented scalar that lives outside a section. A missing file prompts
via an injectable `on_missing_prompt(path) -> bool` callback; choosing *Create* writes defaults plus
a fresh `station.uuid`, and a write failure refuses startup naming the path. A missing
`station.name` takes the hostname, and a missing `station.uuid` is generated and written back into
the existing file preserving key order (never copied between Stations); an unwritable file shows the
generated UUID for an admin to add by hand. Paths resolve against the config directory with no
environment-variable or `~` expansion; a missing Test Definition directory is not a failure. Every
documented key exists on the returned `Config`, and paths are resolved to absolute objects.

**Application log — `src/ate/log.py`.** One append-only file per launch, named by start time
(`ate-app-<start>.log`) in `log.directory`, filtered at `log.level` (dropping anything below it),
never rotated or deleted (`close` keeps the file). It is separate from the engine's per-run logs.
The module has no Qt dependency so its tests run headless.

**Interrupted-run recovery — `src/ate/run_session/headless.py`.** The session opens that launch
application log on construction and uses its logger as `on_log`, so ATE-level events (config load,
and the interrupted-run close) are recorded in it; a broken log directory leaves `on_log` unset
rather than blocking launch. On `start()` any run with no end time is closed `INTERRUPTED`/`FAIL`
through the real `RunStore.close_interrupted_runs`, which logs via that same seam (ticket 02's
wiring, unchanged).

**Tests.** `tests/test_config.py` covers every refusal path, the prompt/create (+write-failure),
UUID write-back preserving order / no-write-when-present, relative/absolute/no-expansion paths, a
missing defs dir not failing, and that every key has its default. `tests/test_app_log.py` covers one
file per launch named by start time, level filtering at each threshold, never rotated/deleted across
a launch, the context manager, and an unwritable directory refusing to open. `tests/
test_headless_session.py` gained a test that a crashed run is closed as INTERRUPTED/FAIL via the real
store API and recorded in the application log (no mocks). Full suite: 53 passed.
