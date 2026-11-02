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

#: How long the copy may go without moving before it is given up on: seconds of no progress,
#: not seconds in total. A copy waiting behind a lock another process holds would otherwise run
#: forever --- CPython's backup loop keeps retrying while SQLite says the source is busy, and the
#: connect timeout alone never bounded it (packet 35 review, round 2).
BACKUP_LOCK_TIMEOUT_S = 10.0

#: Pages copied per step of the copy, so the progress callback sees the copy move often enough
#: to notice when it stops.
BACKUP_PAGES = 64

#: The pause CPython takes between steps that SQLite refused with BUSY or LOCKED.
BACKUP_RETRY_SLEEP_S = 0.05


class BackupError(ATEError):
    """A copy was needed and could not be made; the migration is refused."""


class SchemaVersionError(ATEError):
    """A database stores a version this migration cannot honestly raise to the current one."""


class _CopyStalled(Exception):
    """Internal: raised from the progress callback when the copy stops moving."""


def _read_only(path: Path) -> sqlite3.Connection:
    """Open ``path`` read-only without creating it.

    The busy timeout is the same bound the copy uses: a database held by another process answers
    in that time instead of waiting out SQLite's five seconds and then saying "no tables"."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=BACKUP_LOCK_TIMEOUT_S)


def tables_at(path: str | Path) -> set[str]:
    """Every table present in ``path``; an empty set only when the file is not there.

    Never creates the file, so asking about a database that is not there leaves nothing
    behind. A file that is there but cannot be read raises :class:`BackupError` rather than
    answering "no tables" (packet 35 review, round 2): a database held by another process
    answers nothing, and answering "no tables" to that made the copy look unnecessary and let
    the migration run with no way back. An empty set is only ever the truth about a file that
    does not exist."""
    path = Path(path)
    if not path.exists():
        return set()
    try:
        conn = _read_only(path)
    except sqlite3.Error as exc:
        raise BackupError(f"{path} exists but cannot be read to see what is in it: {exc}") from exc
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error as exc:
        raise BackupError(f"{path} exists but cannot be read to see what is in it: {exc}") from exc
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


def _create_partial_like_source(source: Path, partial: Path) -> None:
    """Make the half-written copy's file with the database's mode, before anything is written.

    SQLite creates a new database at the process default, and the mode was matched only when the
    copy was renamed, so between those two moments a half-written copy of a 0600 database sat
    beside it readable by everyone on the machine (packet 35 review, round 2). Creating the file
    with the source's mode first closes that window. The owner's write bit is added if the
    database itself is not writable: a copy the copy cannot write is no copy at all. Best effort:
    a filesystem that will not take the mode still gets a valid copy.
    """
    try:
        mode = source.stat().st_mode & 0o7777
    except OSError:
        return
    if not mode & 0o200:
        mode |= 0o200  # a copy the copying process cannot write is not a copy
    try:
        handle = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    except OSError:
        return  # the name is taken, or this filesystem answers os.open differently
    os.close(handle)


def _match_source_permissions(source: Path, copy: Path) -> None:
    """Give the copy the database's mode, and its group where the platform has one.

    A copy that is 0644 while the database is 0600 leaks the run records to anyone on the
    machine; a copy nobody but the root can read is not a way back for the Administrator who
    needs it. The source's own mode and group are the answer. Windows has no Unix mode or
    group: there the copy inherits the directory's ACL, and :mod:`ate.permissions` is what
    governs it, so this is a no-op there. Best effort: a filesystem that refuses the change
    still leaves a valid copy, and the migration goes on."""
    try:
        stat = source.stat()
        os.chmod(copy, stat.st_mode & 0o7777)
        if os.name == "posix":
            os.chown(copy, stat.st_uid, stat.st_gid)
    except OSError:
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
    * the ``.partial`` is created with the database's own mode, so the half-written copy is never
      more readable than the records it is copying (:func:`_create_partial_like_source`);
    * it is given up on when it stops moving for :data:`BACKUP_LOCK_TIMEOUT_S` seconds. The bound
      is on lack of progress, not on total time: a big database that keeps copying is never
      refused however long it takes, while one held by another process is refused inside the
      bound rather than hanging ``ate db migrate`` or an installer.
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
    source: Optional[sqlite3.Connection] = None
    copy: Optional[sqlite3.Connection] = None

    def _discard_partial() -> None:
        """Delete the half-written copy and the WAL companions it may have grown."""
        for doomed in (partial, partial.with_name(partial.name + "-wal"), partial.with_name(partial.name + "-shm")):
            try:
                doomed.unlink(missing_ok=True)
            except OSError:
                pass

    def _give_up(reason: str) -> BackupError:
        """Close everything, delete the half-written copy, and raise one line naming it."""
        _close_quietly(copy)
        _close_quietly(source)
        _discard_partial()
        return BackupError(f"cannot back up {path} to {target}: {reason}")

    moved_at = time.monotonic()
    last_remaining: Optional[int] = None

    def _progress(rc: int, remaining: int, pagecount: int) -> None:
        """Called after every step of the copy; raises when the copy has stopped moving.

        ``remaining`` is the pages still to copy. While another process holds the database
        SQLite refuses each step with BUSY and CPython retries it, leaving ``remaining``
        unchanged: that is the stall, and it is bounded here. A step that returns BUSY but
        still advances ``remaining`` is a copy that is moving.

        Raising here aborts the backup: CPython finalises the backup handle and re-raises,
        which is what makes the abort safe to catch (packet 35 review, round 2)."""
        nonlocal moved_at, last_remaining
        if remaining != last_remaining:
            last_remaining = remaining
            moved_at = time.monotonic()
            return
        if time.monotonic() - moved_at >= BACKUP_LOCK_TIMEOUT_S:
            raise _CopyStalled(
                f"it made no progress for {BACKUP_LOCK_TIMEOUT_S} s; the database is held by "
                "another process — finish the Test Run or close the other ATE and run the "
                "migration again"
            )

    try:
        source = sqlite3.connect(str(path), timeout=BACKUP_LOCK_TIMEOUT_S)
        # SQLite's own busy timeout bounds a single wait for the lock; the progress callback
        # bounds the retries after it. Neither is a limit on how long the copy may take.
        source.execute(f"PRAGMA busy_timeout={int(BACKUP_LOCK_TIMEOUT_S * 1000)}")
        # Step 1: prove the database can be locked. A process holding a write transaction makes
        # this wait; it is the step that used to hang the installer, and it is the one that now
        # refuses inside the bound.
        try:
            source.execute("BEGIN IMMEDIATE")
            source.execute("ROLLBACK")
        except sqlite3.Error as exc:
            raise _give_up(
                f"the database lock is held by another process ({exc}); "
                f"the copy gave up after {BACKUP_LOCK_TIMEOUT_S} s"
            ) from exc
        # Step 2: write the copy under its temporary name, in the database's own mode.
        _create_partial_like_source(path, partial)
        copy = sqlite3.connect(str(partial), timeout=BACKUP_LOCK_TIMEOUT_S)
        copy.execute(f"PRAGMA busy_timeout={int(BACKUP_LOCK_TIMEOUT_S * 1000)}")
        try:
            source.backup(copy, pages=BACKUP_PAGES, progress=_progress, sleep=BACKUP_RETRY_SLEEP_S)
            # A copy of a WAL database carries the WAL header, so the fresh file picks up its own
            # -wal/-shm companions. Folding them back into the file leaves the backup as one file:
            # a copy whose data is still sitting in a side file is not a copy you can take away.
            copy.execute("PRAGMA journal_mode=DELETE")
            copy.commit()
        except _CopyStalled as exc:
            raise _give_up(str(exc)) from exc
        except sqlite3.Error as exc:
            raise _give_up(f"the copy was interrupted: {exc}") from exc
        # Step 3: close the copy (Windows cannot delete an open file) and check it.
        _close_quietly(copy)
        copy = None
        problem = _copy_is_sound(path, partial)
        if problem is not None:
            raise _give_up(problem)
        # Step 4: rename, never overwriting.
        if target.exists():
            raise _give_up(f"{target} already exists and is not overwritten")
        os.rename(partial, target)
        _discard_partial()  # any WAL companions the copy grew, before the mode is copied
        _match_source_permissions(path, target)
    except (OSError, sqlite3.Error) as exc:
        # Anything else that can go wrong with the copy: a directory that is not there, a
        # permission the process does not have, a rename that fails. It is reported as one line
        # naming the copy, never as a traceback (packet 35 review).
        _close_quietly(copy)
        _close_quietly(source)
        _discard_partial()
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
    except (ATEError, sqlite3.Error, SQLAlchemyError, OSError) as exc:
        # Every way a database can fail to be opened or written, a PermissionError included:
        # one line naming the copy that was already taken, never a traceback (packet 35 review).
        # A SQLAlchemy error carries the whole statement it failed on, so only the first line of
        # the cause is printed: an Operator reading it wants the reason, not the DDL.
        reason = (str(exc).splitlines() or [""])[0]
        if copy is not None:
            print(f"migration failed: {reason}; the copy taken before it is at {copy}", file=sys.stderr)
        else:
            print(f"migration failed: {reason}", file=sys.stderr)
        return 1

    print(f"created or upgraded database at {path} (schema version {current_version(path)})")
    return 0
