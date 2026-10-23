"""The only storage interface in ATE: :class:`RunStore`.

Everything that records a Test Run goes through this class; nothing else sees SQL.
It opens SQLite (WAL mode) at ``database.path`` and writes the three v1 tables
(``test_run``, ``node``, ``attempt``). Writes are incremental — each row is committed
as it happens, so a run left unfinished by a crash can be closed as INTERRUPTED later.

Portable types only: UUIDs as 36-character text, times as ISO-8601 UTC text with
milliseconds, booleans as 0/1 integers, structured values (inputs) as JSON text. No
SQLite-only features — the same tables and column types work on any backend a later
ticket swaps in behind this interface.
"""

from __future__ import annotations

import enum
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from sqlalchemy import create_engine, event

from ._uuid import new_uuid7
from .schema import (
    SCHEMA_VERSION,
    attempt,
    input_value,
    log_line,
    metadata,
    node,
    test_run,
)


@dataclass(frozen=True)
class DbLaunchResult:
    """The outcome of checking a database at launch (never migrates)."""

    status: DbLaunchStatus
    user_version: Optional[int] = None  # PRAGMA user_version, or None if unreadable/missing

    @property
    def message(self) -> str:
        """A human-readable refusal message for non-OK statuses; empty when OK."""
        if self.status == DbLaunchStatus.NEEDS_MIGRATE:
            return "run `ate db migrate` to create or upgrade the database"
        if self.status == DbLaunchStatus.TOO_OLD:
            return "this ATE is too old for this database; update ATE"
        return ""


class DbLaunchStatus(enum.Enum):
    """Why a database refuses launch (or why it allows it)."""

    OK = "OK"  # schema version matches; safe to open
    NEEDS_MIGRATE = "NEEDS_MIGRATE"  # missing, empty, or older than this ATE
    TOO_OLD = "TOO_OLD"  # built by a newer ATE


def _inspect_db(path: str | Path) -> tuple[bool, Optional[int]]:
    """Inspect ``path`` read-only without creating it.

    Returns ``(has_v1_table, user_version)``. A missing file yields ``(False, None)``;
    an existing but empty database (no v1 table yet) yields ``(False, 0)``. Opening in
    ``mode=ro`` never creates the file, so a refusal at launch leaves nothing behind.
    """
    path = Path(path)
    if not path.exists():
        return False, None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        has_tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('test_run','node','attempt')"
        ).fetchone() is not None
        user_version = conn.execute("PRAGMA user_version").fetchone()[0] or 0
    finally:
        conn.close()
    return has_tables, int(user_version)


def check_database_at_launch(path: str | Path) -> DbLaunchResult:
    """Decide whether ATE may launch against ``path``; never migrates.

    Compares the database's PRAGMA user_version to :data:`SCHEMA_VERSION`: a missing,
    empty or older database refuses with NEEDS_MIGRATE ("run `ate db migrate`"), and a
    newer one refuses with TOO_OLD ("this ATE is too old"). Only an exact match returns OK.
    """
    has_tables, user_version = _inspect_db(path)
    if not has_tables or user_version < SCHEMA_VERSION:
        return DbLaunchResult(DbLaunchStatus.NEEDS_MIGRATE, user_version)
    if user_version > SCHEMA_VERSION:
        return DbLaunchResult(DbLaunchStatus.TOO_OLD, user_version)
    return DbLaunchResult(DbLaunchStatus.OK, user_version)


def _enable_wal(engine) -> None:
    """Set SQLite's WAL journal mode on every connection the Engine opens."""
    def _set_wal(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
        finally:
            cursor.close()

    event.listen(engine, "connect", _set_wal)


def _create_schema(path: str) -> None:
    """Create the v1 tables if missing and set the schema version.

    Dialect-neutral: ``metadata.create_all`` reflects each table's existence through
    its own backend catalog (SQLite's ``sqlite_master``, Postgres'
    ``information_schema``), so it never raises on an already-populated database, and
    needs no hand-written DDL or SQLite-only introspection. The Engine is disposed
    immediately after; the running :class:`RunStore` keeps its own stdlib connection.

    PRAGMA user_version and journal_mode are SQLite-only settings (there is no
    portable equivalent), so they are applied directly on the store's stdlib
    connection rather than through Core — that is the one place raw SQL touches the
    database, and it stays confined to pragmas here.
    """
    engine = create_engine(f"sqlite:///{path}")
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()


def _now_iso() -> str:
    """UTC now as ISO-8601 text with milliseconds (e.g. ``2026-10-23T12:34:56.789+00:00``)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _as_int(value: Any) -> Optional[int]:
    """Store booleans as 0/1 and pass through ints; coerce ``None`` to ``None``."""
    if value is None or isinstance(value, int):
        return value
    return 1 if value else 0


class RunStore:
    """Records Test Runs and their children in a single SQLite database.

    Writes go through SQLAlchemy Core statements executed on an Engine (autocommit, so
    each row commits immediately — that is what makes the record append-only and
    crash-safe; an open run simply has no ``ended_at`` yet). A stdlib connection
    (:attr:`_conn`) to the same file holds the pragmas (WAL mode, schema version) and
    is available for direct inspection.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        # A SQLAlchemy Engine over stdlib sqlite3 (the pysqlite dialect). Core
        # statements are executed through it; isolation_level="AUTOCOMMIT" commits
        # every statement on its own so the record is append-only and crash-safe —
        # an open run simply has no ``ended_at`` yet. In SQLAlchemy 2.x there is no
        # Engine.execute(), so each statement runs inside a connection context.
        self._engine = create_engine(f"sqlite:///{self.path}", isolation_level="AUTOCOMMIT")
        _enable_wal(self._engine)
        # A stdlib connection to the same file for pragmas and direct inspection.
        # check_same_thread=False lets a later QThread run worker share it; callers
        # that use it cross-thread (the run engine) serialise access themselves.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        # Per-run next log-line seq, so a batch can hold several lines (an INFO print and
        # an ERROR warning from the same step) without colliding on ``(run_uuid, seq)``.
        # The store spans one whole run, so this counter stays monotonic across every
        # step's batches and preserves emission order within and between them.
        self._log_seq: dict[str, int] = {}
        self._ensure_schema()

    def _run(self, stmt) -> Any:
        """Execute a Core statement on the Engine's connection and return its result."""
        with self._engine.connect() as conn:
            return conn.execute(stmt)

    def _ensure_schema(self) -> None:
        # Idempotent table creation via a throwaway engine (reflects existence, so
        # reopening an existing database never fails), then set the pragmas on our
        # own stdlib connection. Both persist in the database file header, so they
        # survive reopen and WAL mode stays enabled for every future connection.
        _create_schema(str(self.path))
        # Set the pragmas on our own stdlib connection. Both persist in the database
        # file header, so they survive reopen and WAL mode stays enabled for every
        # future connection.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    # -- test_run -----------------------------------------------------------

    def insert_test_run(self, data: Mapping[str, Any]) -> dict:
        """Insert a new run row (started, not yet ended) and return its values.

        ``run_uuid`` is generated here if the caller omits it, so the store can open
        a run on its own (for example when closing an interrupted run at launch)."""
        now = _now_iso()
        run_uuid = str(data["run_uuid"]) if "run_uuid" in data else new_uuid7()
        row = {
            "run_uuid": run_uuid,
            "station_uuid": data.get("station_uuid"),
            "station_name": data.get("station_name"),
            "operator": data.get("operator", ""),
            "project": data.get("project", ""),
            "uut_type": data.get("uut_type"),
            "uut_serial": data.get("uut_serial"),
            "ate_version": data.get("ate_version"),
            "schedule_name": data.get("schedule_name"),
            "schedule_file": data.get("schedule_file"),
            "schedule_description": data.get("schedule_description"),
            "doc_title": data.get("doc_title"),
            "doc_id": data.get("doc_id"),
            "doc_rev": data.get("doc_rev"),
            "media_number": data.get("media_number"),
            "stop_on_fail": _as_int(data.get("stop_on_fail")),
            "started_at": now,
            "ended_at": None,
            "utc_offset": data.get("utc_offset", "+00:00"),
            "result": None,
            "end_reason": None,
        }
        self._run(test_run.insert().values(**row))
        return dict(row)

    def update_run_end(self, run_uuid: str, result: str, end_reason: str) -> None:
        """Close a running run: stamp ``ended_at`` and its final result/reason."""
        now = _now_iso()
        self._run(
            test_run.update()
            .where(test_run.c.run_uuid == run_uuid)
            .values(ended_at=now, result=result, end_reason=end_reason)
        )

    # -- node ---------------------------------------------------------------

    def insert_node(self, run_uuid: str, seq: int, **fields: Any) -> None:
        """Insert one expanded-tree row keyed by ``(run_uuid, node_seq)``."""
        self._run(
            node.insert().values(
                run_uuid=run_uuid,
                node_seq=seq,
                parent_node_seq=fields.get("parent_node_seq"),
                instance_seq=fields.get("instance_seq"),
                kind=fields.get("kind"),
                step_number=fields.get("step_number"),
                step_name=fields.get("step_name"),
                default_enabled=_as_int(fields.get("default_enabled")),
                enabled=_as_int(fields.get("enabled")),
                inputs=json.dumps(fields.get("inputs", {})) if fields.get("inputs") is not None else None,
                result=fields.get("result"),
                attempt_count=fields.get("attempt_count", 0),
            )
        )

    # -- attempt ------------------------------------------------------------

    def insert_attempt(self, run_uuid: str, node_seq: int, attempt_no: int, **fields: Any) -> None:
        """Insert one Attempt row keyed by ``(run_uuid, node_seq, attempt_no)``."""
        self._run(
            attempt.insert().values(
                run_uuid=run_uuid,
                node_seq=node_seq,
                attempt_no=attempt_no,
                started_at=fields.get("started_at") or _now_iso(),
                ended_at=fields.get("ended_at") or _now_iso(),
                pass_fail=fields.get("pass_fail"),
                msg=fields.get("msg"),
                actual=fields.get("actual"),
                expected=fields.get("expected"),
                prompt_choice=fields.get("prompt_choice"),
            )
        )

    # -- input_value ---------------------------------------------------------

    def insert_input_values(self, run_uuid: str, values: list[dict[str, Any]]) -> None:
        """Insert every recorded Operator input value for a step's Attempt.

        ``values`` is an ordered list of dicts each carrying ``node_seq``, ``attempt_no``,
        ``input_index``, ``type``, ``value_text`` and ``at``; the run engine diffs this around
        each step's action closure so ownership lands on that Attempt (index, type, value and
        time). ``value_text`` is already serialised — a CHECKBOX selection arrives as a JSON
        array of labels. Each row gets its own unique ``seq`` from :attr:`_log_seq`'s run counter
        shared with log lines so ``(run_uuid, seq)`` stays globally unique within the run."""
        next_seq = self._log_seq.get(run_uuid, 0)
        for value in values:
            row = {
                "run_uuid": run_uuid,
                "seq": next_seq,
                "node_seq": value["node_seq"],
                "attempt_no": value["attempt_no"],
                "input_index": value["input_index"],
                "type": value["type"],
                "value": value["value_text"],
                "at": value["at"],
            }
            next_seq += 1
            self._run(input_value.insert().values(**row))
        if next_seq > 0:
            self._log_seq[run_uuid] = next_seq

    # -- log_line ------------------------------------------------------------

    @staticmethod
    def _log_line_row(run_uuid: str, seq: int, level: str, source: str, text: str, *, action_seq: Any = None, node_seq: Any = None, attempt_no: Any = None) -> dict:
        """Build a :class:`schema.log_line` row from a ``level``/``source``/``text`` line.

        Ownership is expressed structurally through the foreign keys rather than a
        separate column: ``action_seq`` ties the line to an owning Pre-test/Post-test
        Action, while ``node_seq`` + ``attempt_no`` tie it to a step Attempt; a line with
        neither belongs to the run as a whole.
        """
        return {
            "run_uuid": run_uuid,
            "seq": seq,
            "at": _now_iso(),
            "level": level.upper(),
            "source": source,
            "action_seq": action_seq,
            "node_seq": node_seq,
            "attempt_no": attempt_no,
            "text": text,
        }

    def insert_log_lines(self, run_uuid: str, seq: int, lines: list[dict[str, Any]], *, action_seq: Any = None, node_seq: Any = None, attempt_no: Any = None) -> None:
        """Insert a batch of log lines keyed by ``(run_uuid, seq)``.

        ``lines`` is an ordered list; each entry needs ``level``, ``source`` and ``text``.
        Ownership (which Action or Attempt owns the line) applies to every line in the
        batch — within one step's action all schedule lines are owned by that Attempt, so a
        single call batches them with shared ownership rather than writing one row per line.

        Each line gets its own unique ``seq``: ``(run_uuid, seq)`` is the primary key and a
        batch can hold several lines (an INFO print plus an ERROR warning from the same step),
        so they are numbered from the run's log-line counter (:attr:`_log_seq`) rather than
        sharing one value. The ``seq`` argument stays for call-site clarity; ownership is set
        through ``node_seq``/``attempt_no`` instead."""
        next_seq = self._log_seq.get(run_uuid, 0)
        rows = [self._log_line_row(run_uuid, seq, line["level"], line["source"], line["text"], action_seq=action_seq, node_seq=node_seq, attempt_no=attempt_no) for line in lines]
        for i, row in enumerate(rows):
            row["seq"] = next_seq + i  # unique within this batch
            self._run(log_line.insert().values(**row))
        self._log_seq[run_uuid] = next_seq + len(rows)  # keep later batches monotonic

    def log_lines(self, run_uuid: str) -> list[dict]:
        """Return this run's recorded log lines as dicts (empty when the run is unknown)."""
        stmt = log_line.select().where(log_line.c.run_uuid == run_uuid)
        rows = self._run(stmt).fetchall()
        return [
            {str(c.name): value for c, value in zip(log_line.columns, row)} for row in rows
        ]

    # -- reads --------------------------------------------------------------

    def get_test_run(self, run_uuid: str) -> Optional[dict]:
        """Return the run row as a dict, or ``None`` if it was never recorded."""
        stmt = test_run.select().where(test_run.c.run_uuid == run_uuid)
        row = self._run(stmt).first()
        # This build's native Row iterates values-only (no .keys(), no dict()), so
        # rebuild the mapping from the selected columns in declaration order.
        return {str(c.name): value for c, value in zip(test_run.columns, row)} if row else None

    def open_runs(self) -> list[str]:
        """Run UUIDs with no end time — runs left unfinished by a crash or abort."""
        rows = self._run(test_run.select().where(test_run.c.ended_at.is_(None))).fetchall()
        return [row[0] for row in rows]

    def nodes(self, run_uuid: str) -> list[dict]:
        """Return this run's expanded-tree rows as dicts (empty when the run is unknown).

        Dialect-neutral and value-only, mirroring :meth:`get_test_run`: each mapping is
        rebuilt from the selected columns in declaration order because this build's native
        Row iterates values without ``.keys()`` or ``dict()``."""
        stmt = node.select().where(node.c.run_uuid == run_uuid)
        rows = self._run(stmt).fetchall()
        return [
            {str(c.name): value for c, value in zip(node.columns, row)} for row in rows
        ]

    def attempts(self, run_uuid: str) -> list[dict]:
        """Return this run's Attempt rows as dicts (empty when the run is unknown)."""
        stmt = attempt.select().where(attempt.c.run_uuid == run_uuid)
        rows = self._run(stmt).fetchall()
        return [
            {str(c.name): value for c, value in zip(attempt.columns, row)} for row in rows
        ]

    # -- interrupted-run recovery -------------------------------------------

    def close_interrupted_runs(self, on_log: Optional[callable] = None) -> list[str]:
        """Close every open run as INTERRUPTED / FAIL. Returns the closed UUIDs.

        ``on_log`` is ATE's logger; when supplied it records that ATE closed each
        interrupted run (spec rule 98). Without one, only the row is updated — used
        by tests and by CLI paths that don't hold a logger yet.
        """
        rows = self._run(test_run.select().where(test_run.c.ended_at.is_(None))).fetchall()
        closed: list[str] = []
        for row in rows:
            run_uuid = row[0]
            now = _now_iso()
            self._run(
                test_run.update()
                .where(test_run.c.run_uuid == run_uuid)
                .values(ended_at=now, result="FAIL", end_reason="INTERRUPTED")
            )
            closed.append(run_uuid)
        if on_log is not None and closed:
            on_log("ERROR", "ate", f"Closed {len(closed)} interrupted run(s) at launch.")
        return closed
