"""The copy a migration takes before it changes a database, and the migration itself.

Spec rule 20 asks for a timestamped backup before an upgrade; the code now does it, in one
place, so every path that changes a database — ``ate db migrate``, ``ate setup``, and the
installers that call ``ate setup`` — takes the same copy. The copy is made with SQLite's
backup API (:meth:`sqlite3.Connection.backup`), not a file copy: a database in use keeps its
recent writes in ``-wal``, and the backup API folds those into the copy. Nothing here renames
or replaces an existing file; the copy is always written to a fresh name beside the database.

:func:`migrate_database` is the routine both callers use. It refuses when a copy is needed
but cannot be made: a migration that starts without its copy is a migration that cannot be
rolled back, and it is better to change nothing at all than to change something quietly.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy.exc import SQLAlchemyError

from .schema import SCHEMA_VERSION, metadata
from ..errors import ATEError

#: How long SQLite waits for a lock held by another process before the backup gives up.
#: A Station that is merely busy (a run writing) backs up inside this; a database genuinely
#: stuck elsewhere fails with a clear message rather than hanging the installer.
BACKUP_LOCK_TIMEOUT_S = 10.0


class BackupError(ATEError):
    """A copy was needed and could not be made; the migration is refused."""


class SchemaVersionError(ATEError):
    """A database stores a version this migration cannot honestly raise to the current one."""


def _read_only(path: Path) -> sqlite3.Connection:
    """Open ``path`` read-only without creating it."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def tables_at(path: str | Path) -> set[str]:
    """Every table present in ``path``; an unreadable or missing file gives an empty set.

    Never creates the file, so asking about a database that is not there leaves nothing
    behind."""
    path = Path(path)
    if not path.exists():
        return set()
    try:
        conn = _read_only(path)
    except sqlite3.Error:
        return set()
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def missing_tables(path: str | Path) -> list[str]:
    """The tables this build declares that ``path`` does not have — the migration's work list.

    A migration "runs" when this is non-empty; that is the only thing that makes stamping
    ``PRAGMA user_version`` honest (see :meth:`RunStore._ensure_schema`)."""
    present = tables_at(path)
    return [name for name in metadata.tables if name not in present]


def _stamp() -> str:
    """The backup's timestamp, UTC: ``YYYYMMDD-HHMMSS``."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _copy_name(path: Path) -> Path:
    """``<db>.backup-<YYYYMMDD-HHMMSS>`` beside the database, never overwriting anything.

    The stamp is UTC so a copy made on a Station in any timezone sorts the same way. If that
    name is already taken — two migrations in the same second, or an Operator's own copy with
    the same name — a counter is appended until the name is free."""
    stamp = _stamp()
    name = f"{path.name}.backup-{stamp}"
    count = 2
    target = path.with_name(name)
    while target.exists():
        target = path.with_name(f"{name}-{count}")
        count += 1
    return target


def backup_for_migration(path: str | Path) -> Optional[Path]:
    """Copy the database at ``path`` if a migration is about to change it.

    Returns the copy's path, or ``None`` when no copy is needed: the file does not exist yet,
    holds no tables at all, or is already what this build would create. Raises
    :class:`BackupError` naming the path and the reason when the copy cannot be made.

    The copy includes the WAL: SQLite's backup API reads the live database, so writes that
    are still sitting in ``ate.db-wal`` are in it. That is why a plain ``cp`` of ``ate.db`` is
    not the same thing, and why the code does not do one."""
    path = Path(path)
    if not path.exists():
        return None
    missing = missing_tables(path)
    if not missing:
        return None  # nothing will change: there is nothing to roll back to

    target = _copy_name(path)
    try:
        source = sqlite3.connect(str(path), timeout=BACKUP_LOCK_TIMEOUT_S)
    except sqlite3.Error as exc:
        raise BackupError(f"cannot open the database {path} to copy it: {exc}") from exc
    try:
        try:
            copy = sqlite3.connect(str(target), timeout=BACKUP_LOCK_TIMEOUT_S)
        except sqlite3.Error as exc:
            raise BackupError(f"cannot write a backup of {path} to {target}: {exc}") from exc
        try:
            source.backup(copy)
        except sqlite3.Error as exc:
            target.unlink(missing_ok=True)  # a half-written copy is worse than none
            raise BackupError(f"cannot back up {path} to {target}: {exc}") from exc
        finally:
            copy.close()
    finally:
        source.close()
    return target


def current_version(path: str | Path) -> int:
    """``PRAGMA user_version`` of ``path``; 0 when it is missing, empty or unreadable."""
    path = Path(path)
    if not path.exists():
        return 0
    try:
        conn = _read_only(path)
    except sqlite3.Error:
        return 0
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def migrate_database(path: str | Path, *, config_file: Path | None = None) -> int:
    """Back up, then create or upgrade the database at ``path``. Returns 0 or 1.

    The single routine behind ``ate db migrate`` and ``ate setup``: a database a newer ATE
    built is refused, a copy is taken when a migration will run, and the migration itself is
    :class:`RunStore` opening the file. Lines are printed here because both callers print the
    same thing: the copy's path, then the version reached."""
    from .store import DbLaunchStatus, RunStore, check_database_at_launch

    result = check_database_at_launch(path, config_file=config_file)
    if result.status is not DbLaunchStatus.OK and result.status is not DbLaunchStatus.NEEDS_MIGRATE:
        print(result.message, file=sys.stderr)
        return 1

    try:
        copy = backup_for_migration(path)
    except BackupError as exc:
        print(f"refusing to migrate: {exc}", file=sys.stderr)
        print("nothing in the database was changed", file=sys.stderr)
        return 1
    if copy is not None:
        print(f"backed up {path} to {copy}")

    try:
        store = RunStore(path)
        store.close()  # creates the tables this build declares and stamps the version
    except (ATEError, sqlite3.Error, SQLAlchemyError) as exc:
        # Every way a database can fail to be opened or written. The Operator gets one line and
        # the path of the copy that was already taken; a traceback is not a migration message.
        print(f"migration failed: {exc}", file=sys.stderr)
        if copy is not None:
            print(f"the copy taken before it is at {copy}", file=sys.stderr)
        return 1

    print(f"created or upgraded database at {path} (schema version {current_version(path)})")
    return 0
