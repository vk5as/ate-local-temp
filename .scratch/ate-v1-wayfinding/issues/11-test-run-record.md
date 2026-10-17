# What a Test Run records

Type: grilling
Status: resolved
Blocked by: 04, 07, 10, 14

## Question

What does a Test Run store: Project, UUT name and serial, Operator, Station, Media Release, per-file expected and computed checksums, step outcomes, measurements, inputs, log lines? What SQLite schema holds it without precluding a later central multi-site database?
Also from "Device connection details and v1 connection types" (06): the record holds the ATE version and the checksum of each bundled `ate.` module used, and Device trace lines when tracing is on.
Also from "Test Definition format" (07): the Project is per Test Run, entered by the Operator. The record holds the media number, name, version and `.atedef` SHA-256 of every Media Release in the resolved dependency chain, and flags checksum mismatches for the Test Report.
Also from "Flow control" (10): the run's stop-on-fail setting and end reason (`COMPLETED`, `PRETEST_FAILED`, `STOPPED_ON_FAIL`, `ABORTED`); every Attempt of a retried step (number, result, message, log lines), with the step's result being the last Attempt's; retried steps are flagged on the report.
Also from "Sub-schedules" (14): each Sub-schedule instance (its `ScheduleParams` name, file, prefix and `keyvals` as plain data) and its include header row (Step Name, optional Step Number, its place in the tree), so the tree can be rebuilt; the header has no `Result`. Every step's optional Step Name.

## Answer

Full logical schema: `assets/test-run-record.md`.

- **Storage**: SQLite (stdlib `sqlite3`, WAL) behind a `RunStore` interface, with dialect-neutral SQLAlchemy Core tables so Postgres or MariaDB can be added later. v1 runs only SQLite; the config is `"database": {"type": "sqlite", "path": ...}`.
- **Identity**: a UUIDv7 per Test Run; child rows keyed `(run_uuid, sequence)`; the run records `station.uuid` and the Station name at the time. Times are UTC ISO 8601 with milliseconds, plus the Station's UTC offset.
- **Writes**: incremental from run start. An unfinished run found at launch is closed as `INTERRUPTED` / `FAIL`. Runs are append-only.
- **Tables**: `test_run` (Operator, Project, UUT Type and serial, ATE version, top-level schedule, stop on fail, times, result, End Reason); `media_release` (the resolved chain with `.atedef` checksums); `run_file` (expected and computed SHA-256, mismatch, optional stored bytes); `schedule_instance` (each include with prefix and `keyvals`); `node` (the expanded tree with headers, Step Numbers and Names, default and actual enable, input declarations, final result); `action` (each Pre-test and Post-test call and its outcome); `attempt` (final report values and the failure-prompt choice); `input_value` (every value entered, `POLL` clicks when clicked); `log_line` (every level and source, owned by the run, an action or an Attempt).
- **Lua source** isn't stored when checksums match. Mismatched files can be stored, controlled by `database.storeMismatchedFiles` (`ask` by default, offered at run start).
- **Schema versions**: `PRAGMA user_version` with forward-only migrations, applied only by the separate `ate db migrate` CLI after a backup; `ate db status` prints the version. The app only checks: a missing, older or newer database stops startup with the matching message. `ate db migrate` creates a missing database after confirmation.
- **New terms**: UUT Type, and the End Reason `INTERRUPTED`.

## Comments

### Round 1, 2026-10-17

- **Engine**: SQLite through stdlib `sqlite3`, WAL, one file at `database.path`, portable SQL only. It must be modular enough to drop in Postgres or MariaDB later, with local or remote chosen in the application config (details in round 2).
- **Identity**: each Test Run has a UUIDv7 primary key. Child rows are keyed `(run_uuid, sequence)`, never by a global autoincrement. The run records `station.uuid`.
- **UUT Type** (new term) and serial number: both required free text. The serial isn't unique. History groups by (UUT Type, serial).
- **Incremental writes**: the run row is inserted at start, everything else committed as it happens, and log lines batched about once a second. A run found without an end at launch gets the new End Reason `INTERRUPTED` and result `FAIL`.
- **Log lines**: every level, Device trace and `print` included, each with timestamp, level, text, owner (run, action instance or Attempt) and source (`schedule`/`ate`/`device`).
- **Tree snapshot**: one row per node (steps and include headers) in tree order, with kind, prefixed Step Number, Step Name, parent, instance and enabled at start; disabled steps included. One row per Sub-schedule instance (name, file, release, prefix, `keyvals` JSON). Reports are regenerated from the database alone.
- **Attempts**: number, times, and final `ATE.report` values. The step result is the last Attempt's and is denormalised onto the step. Earlier report calls exist only as log lines. Steps with no Attempts carry `NOTRUN` on the step row.
- **Step Input values**: one row per value entered (index, type, text value, `CHECKBOX` as JSON, timestamp). `POLL` clicks are recorded when clicked.
- **Action rows**: per instance and phase, with order, times, outcome (`ok`/`error`/`aborted`/`skipped`) and error message.
- **Run row**: UUID, Station UUID and name at the time, Operator, Project, UUT Type and serial, ATE version, top-level schedule, stop-on-fail, start and end, result and End Reason. Plus a Media Release table and a per-run file table (expected and computed SHA-256, mismatch flag; `ate.` modules have computed only).
- **Times**: UTC ISO 8601 with ms, plus the Station's UTC offset per run.
- **Append-only**: no edit, delete or purge in v1.
- **Source**: not stored when checksums match. On a mismatch, ATE offers to store the mismatched file for later analysis (details in round 2).
- **Schema versioning**: `PRAGMA user_version`, forward-only migrations with a backup first. Migrations live in a separate CLI tool, not in the app. Remote databases will get their own scheme later.

### Round 2, 2026-10-17

- **Modular storage**: a `RunStore` interface is the only thing the rest of ATE talks to. Behind it, SQLAlchemy Core tables are written once, dialect-neutral (UUIDs as 36-char text, ISO text times, 0/1 booleans, JSON as text). Nothing outside the store module sees SQL.
- **Remote databases**: v1 runs only SQLite. The config becomes `"database": {"type": "sqlite", "path": "ate.db"}`, and any other `type` stops startup naming the key. Postgres and MariaDB stay out of scope; the interface and config shape keep them possible.
- **Migration CLI**: `ate db migrate` upgrades the configured database after a timestamped backup; `ate db status` prints the schema version. The app only checks the version at launch: older stops with "run `ate db migrate`", newer stops with "this ATE is too old". The app never migrates.
- **Missing database file**: the app prompts the user to run the migration tool, which creates the database when it isn't present, after a confirmation (details in round 3).
- **Mismatched-file offer**: partly agreed, possibly overtaken by other answers (settled in round 3).
- **Step Input declarations** are stored as JSON on each step node (index, type, label or prompt, options, range or pattern).
- **Operator choices**: each Attempt that ended at a failure prompt records `CONTINUE`, `RETRY` or `ABORT`. Each node records `defaultEnabled` (from the schedule, after header forcing) and `enabled` (as the Operator left it).
- **Schema document**: a logical schema in a new `assets/test-run-record.md` (tables, columns, types, keys, nullability, meaning), not DDL.

### Round 3, 2026-10-17

- **Missing database**: the app shows "No database at `<path>`. Run `ate db migrate` to create it." and exits. `ate db migrate` on a missing file asks for confirmation and creates the database at the current schema version. Creating and migrating are admin jobs, like editing the config.
- **Mismatched files**: as proposed in round 2: `database.storeMismatchedFiles` = `ask` (default), `always` or `never`; with `ask`, the run-start confirmation lists the mismatched files with a "Store mismatched files with this Test Run" checkbox ticked by default. Stored as raw bytes on the run's file row, one copy per run.
