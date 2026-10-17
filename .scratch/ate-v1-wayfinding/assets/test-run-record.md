# The Test Run record

ATE's logical schema for what a Test Run stores, settled in "What a Test Run records". This is not DDL: the real definitions are SQLAlchemy Core tables written at implementation time. Vocabulary follows `GLOSSARY.md`.

## Storage

- **Engine**: SQLite through the stdlib `sqlite3` driver, WAL mode, one file at `database.path` (see `app-config-format.md`). v1 runs only SQLite.
- **Modular**: a `RunStore` interface is the only thing the rest of ATE talks to. Behind it, SQLAlchemy Core tables are written once and kept dialect-neutral, so a Postgres or MariaDB backend is mostly a connection change later. Nothing outside the store module sees SQL.
- **Portable types**: UUIDs as 36-character text, times as text, booleans as 0/1, structured values (`keyvals`, input declarations, `CHECKBOX` values) as JSON text. No SQLite-only features.
- **Identity**: each Test Run has a UUIDv7, generated at run start. Every child row is keyed by `(run_uuid, sequence)` within its run, never by a database-wide autoincrement, so runs from many Stations can later be merged by a plain copy.
- **Times**: UTC, ISO 8601 text with milliseconds. The Station's UTC offset is recorded once per run so reports can show local time.
- **Incremental writes**: the run row is inserted at run start, and every node, action, Attempt, input value and log line is committed as it happens (log lines batched about once a second).
- **Append-only**: v1 has no edit, delete or purge of Test Runs.
- **Interrupted runs**: at launch, a run with no end time gets End Reason `INTERRUPTED` and result `FAIL`, and ATE logs that it did so.

## Schema versions and the migration tool

- The schema version is SQLite's `PRAGMA user_version`. Migrations are forward-only.
- Migrations run only in a separate CLI, never in the app:
  - `ate db migrate` upgrades the configured database, after a timestamped backup copy. If no database file exists, it asks for confirmation and then creates one at the current schema version.
  - `ate db status` prints the schema version.
- At launch the app only checks the version:
  - no database file: "No database at `<path>`. Run `ate db migrate` to create it.", then exit;
  - older than the app: "run `ate db migrate`", then exit;
  - newer than the app: "this ATE is too old", then exit.
- Remote databases will get their own migration scheme when they're added.

## Tables

`PK` marks the key. `null` marks a nullable column; every other column is required.

### `test_run`: one row per Test Run

| Column | Type | Meaning |
| --- | --- | --- |
| `run_uuid` PK | uuid | UUIDv7, generated at run start |
| `station_uuid` | uuid | `station.uuid` from the app config |
| `station_name` | text | `station.name` as it was at the time |
| `operator` | text | Free-text Operator name |
| `project` | text | Project, prefilled from `defaultProject`; never empty |
| `uut_type` | text | UUT Type |
| `uut_serial` | text | UUT serial number; not unique |
| `ate_version` | text | ATE version that ran it |
| `schedule_name` | text | Top-level `ScheduleParams` name |
| `schedule_file` | text | Top-level `.tslua` path |
| `schedule_description` null | text | Top-level `ScheduleParams` description |
| `doc_title` null | text | `setDocTitle` of the top-level schedule |
| `doc_id` null | text | `setDocId` of the top-level schedule |
| `doc_rev` null | text | `setDocRev` of the top-level schedule |
| `media_number` | text | Media Number of the top-level schedule's Media Release |
| `stop_on_fail` | bool | Stop on Fail as set at run start |
| `started_at` | time | Run start |
| `ended_at` null | time | Run end; null while running |
| `utc_offset` | text | Station's UTC offset at run start, e.g. `+09:30` |
| `result` null | text | `PASS` or `FAIL`; null while running |
| `end_reason` null | text | `COMPLETED`, `PRETEST_FAILED`, `STOPPED_ON_FAIL`, `ABORTED` or `INTERRUPTED`; null while running |

History groups runs by (`uut_type`, `uut_serial`).

### `media_release`: every Media Release in the resolved chain

| Column | Type | Meaning |
| --- | --- | --- |
| `run_uuid`, `seq` PK | | `seq` is the release's position in resolution order, `0` for the top-level release |
| `media_number` | text | Media Number (the release's identity) |
| `name` | text | Descriptive name |
| `version` | text | Semver version |
| `atedef_sha256` | text | SHA-256 of the `.atedef` file |

### `run_file`: every file the run used

| Column | Type | Meaning |
| --- | --- | --- |
| `run_uuid`, `seq` PK | | |
| `path` | text | Path as listed in the Test Definition, or the `ate.` module name |
| `media_number` null | text | Owning Media Release; null for bundled `ate.` modules |
| `kind` | text | `tslua`, `lua` or `ate-module` |
| `expected_sha256` null | text | From the Test Definition; null for `ate.` modules |
| `computed_sha256` | text | Computed from the raw bytes at load |
| `mismatch` | bool | Expected and computed differ |
| `content` null | blob | Raw bytes of a mismatched file, when stored (see below) |

### `schedule_instance`: the top-level schedule and every Sub-schedule include

| Column | Type | Meaning |
| --- | --- | --- |
| `run_uuid`, `instance_seq` PK | | Instances in tree order; `0` is the top-level schedule |
| `parent_instance_seq` null | int | Including instance; null for the top level |
| `header_node_seq` null | int | The include header node; null for the top level |
| `schedule_name` | text | The instance's `ScheduleParams` name |
| `file` | text | `.tslua` path |
| `media_number` | text | Media Release the file came from |
| `prefix` | text | `setPrefix` value as applied, `""` if none |
| `keyvals` | json | `keyvals` passed to the instance, `{}` if none |

### `node`: the expanded step tree

One row per Test Step and per include header row, in tree order. Disabled steps are stored too.

| Column | Type | Meaning |
| --- | --- | --- |
| `run_uuid`, `node_seq` PK | | Tree order |
| `parent_node_seq` null | int | Enclosing header node; null at the top level |
| `instance_seq` | int | Schedule instance the node belongs to |
| `kind` | text | `step` or `header` |
| `step_number` null | text | Fully prefixed Step Number; null for a header without one |
| `step_name` null | text | Step Name from `StepParams(name)` |
| `default_enabled` | bool | From the schedule, after header forcing |
| `enabled` | bool | As the Operator left it at run start |
| `inputs` null | json | Step Input declarations (index, type, label or prompt text, options, range or pattern); steps only |
| `result` null | text | Steps only: the last Attempt's `Result`, or `NOTRUN` when there was no Attempt. Null for headers, which have no `Result` |
| `attempt_count` | int | `0` for headers and steps that never ran |

### `action`: Pre-test and Post-test Action calls

| Column | Type | Meaning |
| --- | --- | --- |
| `run_uuid`, `seq` PK | | |
| `instance_seq` | int | Schedule instance whose action it is |
| `phase` | text | `pre` or `post` |
| `run_order` | int | Position in that phase's order |
| `started_at` null, `ended_at` null | time | Null when skipped |
| `outcome` | text | `ok`, `error`, `aborted` or `skipped` |
| `error` null | text | Error message; the traceback is in the log |

This shows which Pre-test Action caused `PRETEST_FAILED`.

### `attempt`: each execution of a step's action

| Column | Type | Meaning |
| --- | --- | --- |
| `run_uuid`, `node_seq`, `attempt_no` PK | | `attempt_no` from 1 |
| `started_at`, `ended_at` null | time | |
| `pass_fail` | text | Final `Result` of this Attempt |
| `msg` null | text | Final `ATE.report` message, or the error message |
| `actual` null, `expected` null | text | Final `ATE.report` values as text |
| `prompt_choice` null | text | `CONTINUE`, `RETRY` or `ABORT` if the Attempt ended at a failure prompt |

Earlier `ATE.report` calls within an Attempt exist only as log lines.

### `input_value`: every value the Operator entered

| Column | Type | Meaning |
| --- | --- | --- |
| `run_uuid`, `seq` PK | | |
| `node_seq`, `attempt_no` | int | The Attempt that asked |
| `input_index` | int | `n` in `ATE.input(n)` |
| `type` | text | Step Input type |
| `value` | text | Value as text; `CHECKBOX` as a JSON array |
| `at` | time | When it was entered |

Repeated asks on a blocking input give one row each. A `POLL` click is recorded when it happens, not when Lua reads it.

### `equipment`: Equipment declared with `ATE.addEquipment`

| Column | Type | Meaning |
| --- | --- | --- |
| `run_uuid`, `seq` PK | | Declaration order |
| `name` | text | |
| `serial` null, `connection` null, `cal_due` null, `notes` null | text | As given; `cal_due` is free text |
| `at` | time | When it was declared |
| `action_seq` null | int | Declaring Pre-test or Post-test Action call |
| `node_seq` null, `attempt_no` null | int | Declaring step Attempt |

A repeat with the same `name` and `serial` adds no row.

### `log_line`: everything logged during the run

| Column | Type | Meaning |
| --- | --- | --- |
| `run_uuid`, `seq` PK | | |
| `at` | time | |
| `level` | text | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `source` | text | `schedule` (`ATE.log`, `print`), `ate` (ATE's own lines, e.g. checksum warnings, the Pre-test failure explanation, `NOREPORT`) or `device` (Device trace) |
| `action_seq` null | int | Owning Pre-test or Post-test Action call |
| `node_seq` null, `attempt_no` null | int | Owning step Attempt |
| `text` | text | The line |

A line with no owner belongs to the run as a whole. Every level is stored, including `DEBUG` and Device trace lines.

## Lua source

Lua files aren't copied into the record when their checksums match: the Media Number plus the checksum identifies the exact bytes. When a file mismatches, its raw bytes can be stored in `run_file.content`, controlled by `database.storeMismatchedFiles` in the app config:

- `ask` (default): the run-start confirmation lists the mismatched files with a checkbox, "Store mismatched files with this Test Run", ticked by default.
- `always`: stored without asking.
- `never`: never stored.

Each run stores its own copy; there is no deduplication.
