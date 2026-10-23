"""The v1 Test Run record schema, as SQLAlchemy Core tables.

This is ticket 02's slice of the storage layer: three dialect-neutral tables —
``test_run``, ``node`` and ``attempt`` — written once here so only :class:`RunStore`
ever sees SQL. Later tickets add more tables (media_release, run_file, ...) to the
same metadata; they never touch anything outside this module.

Portable types (see `assets/test-run-record.md`): UUIDs as 36-character text,
times as ISO-8601 UTC text with milliseconds, booleans as 0/1, structured values
as JSON text. No SQLite-only features — every column is a plain SQLAlchemy type so
a Postgres or MariaDB backend is mostly a connection change later.
"""

from __future__ import annotations

from sqlalchemy import Column, Integer, MetaData, Table, Text


# The schema version lives in PRAGMA user_version. v1 introduces this first real
# schema (ticket 01 had no tables), so it starts at 1; adding the ``input_value`` table
# is a forward migration, so it becomes 2 and older databases must be migrated first.
SCHEMA_VERSION = 2

metadata = MetaData()


test_run = Table(
    "test_run",
    metadata,
    Column("run_uuid", Text, primary_key=True),
    Column("station_uuid", Text, nullable=True),
    Column("station_name", Text, nullable=True),
    Column("operator", Text, nullable=True),
    Column("project", Text, nullable=True),
    Column("uut_type", Text, nullable=True),
    Column("uut_serial", Text, nullable=True),
    Column("ate_version", Text, nullable=True),
    Column("schedule_name", Text, nullable=True),
    Column("schedule_file", Text, nullable=True),
    Column("schedule_description", Text, nullable=True),
    Column("doc_title", Text, nullable=True),
    Column("doc_id", Text, nullable=True),
    Column("doc_rev", Text, nullable=True),
    Column("media_number", Text, nullable=True),
    Column("stop_on_fail", Integer, nullable=True),  # 0/1
    Column("started_at", Text, nullable=True),  # ISO-8601 UTC with ms
    Column("ended_at", Text, nullable=True),  # null while running; set on close
    Column("utc_offset", Text, nullable=True),  # e.g. "+09:30" at run start
    Column("result", Text, nullable=True),  # PASS or FAIL
    Column("end_reason", Text, nullable=True),  # COMPLETED / PRETEST_FAILED / STOPPED_ON_FAIL / ABORTED / INTERRUPTED
)


node = Table(
    "node",
    metadata,
    Column("run_uuid", Text, primary_key=True),
    Column("node_seq", Integer, primary_key=True),  # composite (run_uuid, node_seq) key
    Column("parent_node_seq", Integer, nullable=True),
    Column("instance_seq", Integer, nullable=True),
    Column("kind", Text, nullable=True),  # "step" or "header"
    Column("step_number", Text, nullable=True),
    Column("step_name", Text, nullable=True),
    Column("default_enabled", Integer, nullable=True),  # 0/1 from the schedule
    Column("enabled", Integer, nullable=True),  # 0/1 as the Operator left it at run start
    Column("inputs", Text, nullable=True),  # JSON text; steps only
    Column("result", Text, nullable=True),  # last Attempt's Result, or NOTRUN when none
    Column("attempt_count", Integer, nullable=True),  # 0 for headers and never-run steps
)


attempt = Table(
    "attempt",
    metadata,
    Column("run_uuid", Text, primary_key=True),
    Column("node_seq", Integer, primary_key=True),
    Column("attempt_no", Integer, primary_key=True),  # from 1
    Column("started_at", Text, nullable=True),
    Column("ended_at", Text, nullable=True),
    Column("pass_fail", Text, nullable=True),  # final Result of this Attempt
    Column("msg", Text, nullable=True),  # final ATE.report message, or the error text
    Column("actual", Text, nullable=True),  # final ATE.report actual as text
    Column("expected", Text, nullable=True),  # final ATE.report expected as text
    Column("prompt_choice", Text, nullable=True),  # CONTINUE / RETRY / ABORT if it ended at a prompt
)

action = Table(
    "action",
    metadata,
    Column("run_uuid", Text, primary_key=True),
    Column("seq", Integer, primary_key=True),  # composite (run_uuid, seq) key
    Column("instance_seq", Integer, nullable=True),  # schedule instance whose action it is
    Column("phase", Text, nullable=True),  # "pre" or "post"
    Column("run_order", Integer, nullable=True),  # position within that phase's order
    Column("started_at", Text, nullable=True),  # ISO-8601 UTC with ms; null when skipped
    Column("ended_at", Text, nullable=True),  # null while running; set on close
    Column("outcome", Text, nullable=True),  # ok / error / aborted / skipped
    Column("error", Text, nullable=True),  # error message; the traceback is in the log
)


log_line = Table(
    "log_line",
    metadata,
    Column("run_uuid", Text, primary_key=True),
    Column("seq", Integer, primary_key=True),  # composite (run_uuid, seq) key
    Column("at", Text, nullable=True),  # ISO-8601 UTC with ms
    Column("level", Text, nullable=True),  # DEBUG / INFO / WARNING / ERROR
    Column("source", Text, nullable=True),  # schedule / ate / device
    Column("action_seq", Integer, nullable=True),  # owning Pre-test/Post-test Action call; null for run-level lines
    Column("node_seq", Integer, nullable=True),  # owning step node; null when not tied to a step
    Column("attempt_no", Integer, nullable=True),  # the Attempt's number within that node; null otherwise
    Column("text", Text, nullable=True),  # the line
)


input_value = Table(
    "input_value",
    metadata,
    Column("run_uuid", Text, primary_key=True),
    Column("seq", Integer, primary_key=True),  # composite (run_uuid, seq) key
    Column("node_seq", Integer, nullable=True),  # FK to node; the step that asked
    Column("attempt_no", Integer, nullable=True),  # the Attempt that asked
    Column("input_index", Integer, nullable=True),  # n in ATE.input(n)
    Column("type", Text, nullable=True),  # Step Input type (ENUM/INTEGER/FLOAT/STRING/PROMPT/POLL)
    Column("value", Text, nullable=True),  # value as text; CHECKBOX as a JSON array of labels
    Column("at", Text, nullable=True),  # ISO-8601 UTC with ms when it was entered
)
