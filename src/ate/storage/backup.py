"""The copy a migration takes before it changes a database, and the migration itself.

Spec rule 20 asks for a timestamped backup before an upgrade; the code now does it, in one
place, so every path that changes a database — ``ate db migrate``, ``ate setup``, and the
installers that call ``ate setup`` — takes the same copy. The copy is made with SQLite's
backup API (:meth:`sqlite3.Connection.backup`), not a file copy: a database in use keeps its
recent writes in ``-wal``, and the backup API folds those into the copy. Nothing here renames
or replaces an existing file; the copy is always written to a fresh name beside the database,
first as ``<name>.partial`` and only renamed to ``<name>.backup-<stamp>`` after it has been
checked, so a copy interrupted halfway never looks like a backup.

When is a copy taken? Only when this build has a table to create. A file that does not exist,
or one that holds no tables at all, gets no copy: there is no data to lose. A database already
at the current version but missing a table does get one, because real data is about to be
changed. :func:`backup_for_migration` and :func:`migrate_database` agree on exactly that.

:func:`migrate_database` is the routine both callers use. It refuses when a copy is needed
but cannot be made: a migration that starts without its copy is a migration that cannot be
rolled back, and it is better to change nothing at all than to change something quietly.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy.exc import SQLAlchemyError

from .schema import SCHEMA_VERSION, metadata
from ..errors import ATEError

#: How long the copy may take before it is given up on: a deadline in seconds, checked at every
#: step of the copy, not only SQLite's busy timeout. A copy that waits behind a lock another
#: process holds would otherwise hang ``ate db migrate`` and, through it, an installer: the
#: connect timeout alone did not bound it (packet 35 review).
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
    the same name — a counter is appended until the name is free. The name is chosen before the
    copy starts, and the copy is written under ``.partial`` on the way to it (see
    :func:`backup_for_migration`), so the free-name check covers both names."""
    stamp = _stamp()
    name = f"{path.name}.backup-{stamp}"
    count = 2
    target = path.with_name(name)
    while target.exists() or path.with_name(target.name + ".partial").exists():
        target = path.with_name(f"{name}-{count}")
        count += 1
    return target


def _copy_is_sound(source: Path, copy: Path) -> Optional[str]:
    """Why ``copy`` is not a usable copy of ``source``, or ``None`` when it is.

    Checks three things, in order: SQLite's own ``integrity_check`` says ``ok``, the copy holds
    exactly the tables the source holds, and it carries the same ``PRAGMA user_version``. A copy
    interrupted halfway passes all three of nothing: it is an empty, well-formed SQLite file
    which ``integrity_check`` calls ``ok``, which is precisely why the table set and the version
    are compared as well (packet 35 review)."""
    try:
        conn = _read_only(copy)
    except sqlite3.Error as exc:
        return f"the copy cannot be opened: {exc}"
    try:
        verdict = conn.execute("PRAGMA integrity_check").fetchone()
        if not verdict or verdict[0] != "ok":
            return f"integrity_check on the copy said {verdict[0] if verdict else 'nothing'}"
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    except sqlite3.Error as exc:
        return f"the copy cannot be read: {exc}"
    finally:
        conn.close()
    wanted_tables = tables_at(source)
    if tables != wanted_tables:
        missing = sorted(wanted_tables - tables)
        extra = sorted(tables - wanted_tables)
        detail = ", ".join(filter(None, (f"missing {missing}" if missing else "", f"extra {extra}" if extra else "")))
        return f"the copy holds {len(tables)} of the source's {len(wanted_tables)} tables ({detail})"
    wanted_version = current_version(source)
    if version != wanted_version:
        return f"the copy says schema version {version}, the source says {wanted_version}"
    return None


def _close_quietly(conn: Optional[sqlite3.Connection]) -> None:
    """Close a connection without raising: used on the way to a failure, where the close is not
    the problem being reported. The copy must be closed before it is unlinked, because Windows
    cannot delete a file that is still open (packet 35 review)."""
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def backup_for_migration(path: str | Path) -> Optional[Path]:
    """Copy the database at ``path`` if a migration is about to change it.

    Returns the copy's path, or ``None`` when no copy is needed: the file does not exist yet,
    holds no tables at all, or is already what this build would create. Raises
    :class:`BackupError` naming the path and the reason when the copy cannot be made.

    The copy includes the WAL: SQLite's backup API reads the live database, so writes that
    are still sitting in ``ate.db-wal`` are in it. That is why a plain ``cp`` of ``ate.db`` is
    not the same thing, and why the code does not do one.

    Two rules the copy obeys, both from packet 35's review:

    * it is written to ``<name>.partial`` and only renamed to ``<name>.backup-<stamp>`` once it
      has been checked (:func:`_copy_is_sound`), so a copy killed mid-flight is never left behind
      under a name that reads like a backup;
    * every step of it — opening the database, taking the lock, writing the copy, checking it,
      renaming it — runs against a deadline of :data:`BACKUP_LOCK_TIMEOUT_S`. A database held by
      another process is refused inside that time rather than hanging the command or the
      installer, which is what the connect timeout on its own never did.
    """
    path = Path(path)
    if not path.exists():
        return None
    if not tables_at(path):
        return None  # an empty or table-less file: there is no data here to roll back to
    if not missing_tables(path):
        return None  # nothing will change: there is nothing to roll back to

    target = _copy_name(path)
    partial = target.with_name(target.name + ".partial")
    deadline = time.monotonic() + BACKUP_LOCK_TIMEOUT_S
    source: Optional[sqlite3.Connection] = None
    copy: Optional[sqlite3.Connection] = None

    def _give_up(reason: str) -> BackupError:
        """Close everything, delete the half-written copy, and raise one line naming it."""
        _close_quietly(copy)
        _close_quietly(source)
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass  # the copy could not be written at all; the message already names it
        return BackupError(f"cannot back up {path} to {target}: {reason}")

    def _remaining() -> float:
        """The seconds left on the deadline, refusing once it has passed."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _give_up(
                f"it gave up on the database lock after {BACKUP_LOCK_TIMEOUT_S} s; "
                "the database is held by another process — finish the Test Run or close the "
                "other ATE and run the migration again"
            )
        return remaining

    try:
        source = sqlite3.connect(str(path), timeout=BACKUP_LOCK_TIMEOUT_S)
        # SQLite's own busy timeout is set to what is left, so a wait for the lock cannot outlast
        # the deadline; the deadline is checked again at every step below.
        source.execute(f"PRAGMA busy_timeout={int(_remaining() * 1000)}")
        # Step 1: prove the database can be locked. A process holding a write transaction makes
        # this wait; it is the step that used to hang the installer, and it is the one that now
        # refuses inside the deadline.
        try:
            source.execute("BEGIN IMMEDIATE")
            source.execute("ROLLBACK")
        except sqlite3.Error as exc:
            raise _give_up(
                f"the database lock is held by another process ({exc}); "
                f"the copy gave up after {BACKUP_LOCK_TIMEOUT_S} s"
            ) from exc
        # Step 2: write the copy under its temporary name.
        copy = sqlite3.connect(str(partial), timeout=BACKUP_LOCK_TIMEOUT_S)
        copy.execute(f"PRAGMA busy_timeout={int(_remaining() * 1000)}")
        try:
            source.backup(copy)
        except sqlite3.Error as exc:
            raise _give_up(f"the copy was interrupted: {exc}") from exc
        # Step 3: close the copy (Windows cannot delete an open file) and check it.
        _close_quietly(copy)
        copy = None
        _remaining()
        problem = _copy_is_sound(path, partial)
        if problem is not None:
            raise _give_up(problem)
        # Step 4: rename, never overwriting.
        if target.exists():
            raise _give_up(f"{target} already exists and is not overwritten")
        os.rename(partial, target)
    except (OSError, sqlite3.Error) as exc:
        # Anything else that can go wrong with the copy: a directory that is not there, a
        # permission the process does not have, a rename that fails. It is reported as one line
        # naming the copy, never as a traceback (packet 35 review).
        _close_quietly(copy)
        _close_quietly(source)
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        raise BackupError(f"cannot write a backup of {path} to {target}: {exc}") from exc
    finally:
        _close_quietly(copy)
        _close_quietly(source)
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
