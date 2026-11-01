"""Packet 35: the gaps the admin manual had to warn about, now closed in the code.

Every test here runs against real SQLite files in a temporary directory and the real `ate`
command line — through :func:`ate.cli.main` where the exit code and the printed lines are what
matter, and in a subprocess where "no traceback" is part of the contract. Nothing on the host
is touched: no `groupadd`, no `chown`, no `icacls`, and every file is under ``tmp_path``.
What is pinned:

* ``ate db migrate`` and ``ate setup`` share one routine that copies the database beside
  itself before a migration changes it, refuses when the copy cannot be made, and leaves the
  copy in place when the migration then fails;
* that copy is written as ``<name>.partial``, checked, and only then renamed to its
  ``.backup-<stamp>`` name, and every step of it runs inside :data:`BACKUP_LOCK_TIMEOUT_S`
  rather than waiting on a lock indefinitely;
* a database is never stamped with the current version unless a migration actually created
  something for this build;
* ``ate setup`` migrates the database the Station's config names, and says which it chose;
* ``--config`` works after a subcommand, and a path that is not a database fails in one line;
* ``os.exit`` is no longer reachable from a schedule;
* ``reports.paperSize`` is validated when the config is read.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication

from ate.cli import main
from ate.config import ConfigError, read_config
from ate.engine.lua_host import LuaHost
from ate.engine.run_engine import RunEngine
from ate.storage import SCHEMA_VERSION, RunStore, migrate_database
from ate.storage.backup import BackupError, SchemaVersionError, backup_for_migration

from ._fixtures import _drive, scratch_directory

BACKUP_NAME = re.compile(r"\.backup-\d{8}-\d{6}$")
ROOT = Path(__file__).parents[1]  # this checkout, whose src the subprocess tests must run


def _version(path: Path) -> int:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _backups(directory: Path) -> list[Path]:
    return sorted(p for p in directory.iterdir() if BACKUP_NAME.search(p.name))


def _older_database(path: Path, version: int, drop: list[str]) -> None:
    """A database this ATE built, then rolled back to an older schema by dropping a table."""
    store = RunStore(path)
    store.insert_test_run({"uut_serial": "OLD-1", "schedule_name": "before the upgrade"})
    store.close()
    conn = sqlite3.connect(path)
    for table in drop:
        conn.execute(f"DROP TABLE {table}")
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit()
    conn.close()


def _cli(argv: list[str], capsys) -> tuple[int, str, str]:
    """The real CLI, in this process: returns ``(exit code, stdout, stderr)``."""
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _cli_subprocess(argv: list[str]) -> tuple[int, str, str]:
    """The real CLI as a separate process, for the contract that nothing reaches a traceback.

    ``PYTHONPATH`` points at this checkout's ``src``: the venv has an editable install of the
    project, and without it ``python -m ate`` would run a different copy of the code."""
    proc = subprocess.run(
        [sys.executable, "-m", "ate", *argv],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT),
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(ROOT / "src")},
    )
    return proc.returncode, proc.stdout, proc.stderr


# --- the name ---------------------------------------------------------------------------


def test_the_cli_calls_ate_automated_test_execution() -> None:
    """The parser's description matches GLOSSARY: ATE is Automated Test Execution, not Equipment."""
    from ate.cli import _build_parser

    help_text = _build_parser().format_help()
    assert "Automated Test Execution" in help_text, help_text
    assert "Automated Test Equipment" not in help_text, help_text


# --- the backup before a migration ------------------------------------------------------


def test_a_new_database_is_created_without_a_copy(tmp_path: Path) -> None:
    """There is nothing to back up before the first database exists."""
    path = tmp_path / "ate.db"
    assert migrate_database(path) == 0
    assert _backups(tmp_path) == [], sorted(p.name for p in tmp_path.iterdir())
    assert _version(path) == SCHEMA_VERSION


def test_migrating_an_older_database_copies_it_first(tmp_path: Path, capsys) -> None:
    """A v4 database missing ``equipment`` is copied, then upgraded, and the copy is named clearly."""
    path = tmp_path / "ate.db"
    _older_database(path, 4, ["equipment"])

    code, out, err = _cli(["db", "migrate", str(path), "--yes"], capsys)
    assert code == 0, err

    copies = _backups(tmp_path)
    assert len(copies) == 1, sorted(p.name for p in tmp_path.iterdir())
    copy = copies[0]
    assert copy.name == "ate.db" + BACKUP_NAME.search(copy.name).group(), copy.name
    assert copy.parent == path.parent
    assert str(copy) in out, out

    # The copy is the database as it was: older version, the table still missing.
    assert _version(copy) == 4
    assert "equipment" not in _tables(copy)
    # ... and the real database now has it.
    assert _version(path) == SCHEMA_VERSION
    assert "equipment" in _tables(path)


def test_a_database_that_needs_nothing_makes_no_copy(tmp_path: Path) -> None:
    """Migrating a database that is already current changes nothing, so nothing is copied."""
    path = tmp_path / "ate.db"
    _older_database(path, 4, ["equipment"])
    assert migrate_database(path) == 0
    copies = _backups(tmp_path)
    assert len(copies) == 1
    assert migrate_database(path) == 0
    assert _backups(tmp_path) == copies


def test_the_copy_includes_what_is_still_in_the_wal(tmp_path: Path) -> None:
    """SQLite's backup API folds ``-wal`` into the copy; a plain file copy would not.

    This is also the case a backup has to survive on Windows: the database is open by another
    connection when the copy is taken."""
    path = tmp_path / "ate.db"
    _older_database(path, 4, ["equipment"])
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT INTO test_run (run_uuid, uut_serial) VALUES ('wal-row', 'WAL-1')")
    conn.commit()
    assert (tmp_path / "ate.db-wal").exists(), "the write is still in the WAL, not the main file"

    copy = backup_for_migration(path)
    plain = tmp_path / "plain-copy.db"
    plain.write_bytes(path.read_bytes())  # what an admin's `cp ate.db` would have gotten
    conn.close()

    assert copy is not None
    with contextlib.closing(sqlite3.connect(copy)) as checked:
        serials = [row[0] for row in checked.execute("SELECT uut_serial FROM test_run ORDER BY uut_serial")]
    with contextlib.closing(sqlite3.connect(plain)) as checked:
        plain_serials = [row[0] for row in checked.execute("SELECT uut_serial FROM test_run ORDER BY uut_serial")]

    assert serials == ["OLD-1", "WAL-1"], serials
    assert plain_serials == ["OLD-1"], plain_serials


def test_restoring_from_the_copy_gives_the_old_data_back(tmp_path: Path) -> None:
    """The copy is a database you can roll back to: the runs it held are still in it."""
    path = tmp_path / "ate.db"
    _older_database(path, 4, ["equipment"])
    assert migrate_database(path) == 0
    copy = _backups(tmp_path)[0]

    restored = tmp_path / "restored.db"
    restored.write_bytes(copy.read_bytes())
    assert _version(restored) == 4 and "equipment" not in _tables(restored)

    assert migrate_database(restored) == 0  # the copy is a database ATE can open and upgrade
    store = RunStore(str(restored))
    rows = store.query_runs()
    store.close()

    assert [row["uut_serial"] for row in rows] == ["OLD-1"]
    assert _version(restored) == SCHEMA_VERSION


def test_two_copies_in_the_same_second_do_not_overwrite(tmp_path: Path, monkeypatch) -> None:
    """The copy never replaces a file that is already there: the next one gets a counter."""
    from ate.storage import backup as backup_module

    path = tmp_path / "ate.db"
    _older_database(path, 4, ["equipment"])
    monkeypatch.setattr(backup_module, "_stamp", lambda: "20261102-120000")
    first = backup_for_migration(path)
    assert first.name == "ate.db.backup-20261102-120000", first.name
    first.unlink()

    # A file sitting at that exact name is left alone; the next copy takes the next name.
    (tmp_path / "ate.db.backup-20261102-120000").write_text("someone's own backup", encoding="utf-8", newline="\n")
    second = backup_for_migration(path)
    assert second.name == "ate.db.backup-20261102-120000-2", second.name
    assert (tmp_path / "ate.db.backup-20261102-120000").read_text(encoding="utf-8") == "someone's own backup"
    assert _version(second) == 4


def test_an_unwritable_data_directory_refuses_the_migration(tmp_path: Path, capsys, deny_writes) -> None:
    """A Station whose data directory this user cannot write: refuse, name the path and the reason, change nothing."""
    path = tmp_path / "ate.db"
    _older_database(path, 4, ["equipment"])
    before = path.read_bytes()
    deny_writes(tmp_path)

    code, out, err = _cli(["db", "migrate", str(path), "--yes"], capsys)
    text = out + err
    assert code == 1, text
    assert str(path) in text, text
    assert "cannot open" in text.lower(), text
    assert "Traceback" not in err, text
    assert path.read_bytes() == before, "a refused migration must leave the database alone"
    assert _backups(tmp_path) == []


def test_a_copy_that_cannot_be_written_refuses_the_migration(tmp_path: Path, capsys, monkeypatch) -> None:
    """The copy is the first thing a migration does: when it cannot be written, nothing is migrated."""
    from ate.storage import backup as backup_module

    path = tmp_path / "ate.db"
    _older_database(path, 4, ["equipment"])
    before = path.read_bytes()
    # The copy has to land beside the database; point it at a directory that is not there.
    monkeypatch.setattr(backup_module, "_copy_name", lambda p: tmp_path / "no-such-directory" / p.name)

    code, out, err = _cli(["db", "migrate", str(path), "--yes"], capsys)
    text = out + err
    assert code == 1, text
    assert "refusing to migrate" in text.lower(), text
    assert str(path) in text and "no-such-directory" in text, text
    assert "Traceback" not in err, text
    assert path.read_bytes() == before, "a refused migration must leave the database alone"
    assert _version(path) == 4 and "equipment" not in _tables(path)

    with pytest.raises(BackupError) as raised:
        backup_for_migration(path)
    assert str(path) in str(raised.value)


def test_a_backup_taken_while_another_process_has_the_database_open(tmp_path: Path, capsys) -> None:
    """A Station with a Test Run in flight still gets its copy: the database is open, WAL and all.

    This is the case a plain `cp` gets wrong, and the one a Windows installer has to get right:
    the backup is taken while another process holds the database."""
    path = tmp_path / "ate.db"
    _older_database(path, 4, ["equipment"])

    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time, sqlite3;"
         "conn = sqlite3.connect(sys.argv[1]);"
         "conn.execute('PRAGMA journal_mode=WAL');"
         "conn.execute(\"INSERT INTO test_run (run_uuid, uut_serial) VALUES ('in-flight', 'IN-FLIGHT')\");"
         "conn.commit(); time.sleep(5)",
         str(path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(ROOT),
    )
    time.sleep(1.2)  # let the other process open and write

    code, out, err = _cli(["db", "migrate", str(path), "--yes"], capsys)
    holder.wait(timeout=30)
    assert code == 0, out + err

    copy = _backups(tmp_path)[0]
    with contextlib.closing(sqlite3.connect(copy)) as checked:
        serials = [row[0] for row in checked.execute("SELECT uut_serial FROM test_run ORDER BY uut_serial")]
    assert serials == ["IN-FLIGHT", "OLD-1"], serials


def _bulk_rows(path: Path, count: int) -> None:
    """Put ``count`` 4 KB rows in a table of its own, so a copy of this file takes real pages."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS bulk(x BLOB)")
    conn.executemany("INSERT INTO bulk VALUES (?)", [(b"\x00" * 4096,) for _ in range(count)])
    conn.commit()
    conn.close()


def _locked_holder(path: Path, seconds: int) -> subprocess.Popen:
    """A separate process that opens the database, starts writing, and sits on it."""
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time, sqlite3;"
         "conn = sqlite3.connect(sys.argv[1], timeout=120);"
         "conn.execute('PRAGMA journal_mode=WAL');"
         "conn.execute('BEGIN IMMEDIATE');"
         "conn.execute(\"INSERT INTO test_run (run_uuid, uut_serial) VALUES ('in-flight', 'IN-FLIGHT')\");"
         "time.sleep(float(sys.argv[2]))",
         str(path), str(seconds)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    time.sleep(1.0)  # let it open and start its transaction
    assert proc.poll() is None, "the process meant to hold the lock died instead of holding it"
    return proc


def test_a_copy_interrupted_halfway_is_never_left_named_as_a_backup(tmp_path: Path) -> None:
    """A process killed while the copy is in flight leaves nothing that reads like a backup.

    The copy is written to ``<name>.partial`` and renamed only after it is checked, so a kill
    in the middle can only leave a ``.partial`` file: an interrupted SQLite file is well formed
    enough to pass ``integrity_check``, which is exactly why the name is not trusted."""
    path = tmp_path / "ate.db"
    _older_database(path, 4, ["equipment"])
    _bulk_rows(path, 12000)
    before = path.read_bytes()

    proc = subprocess.Popen(
        [sys.executable, "-m", "ate", "db", "migrate", str(path), "--yes"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(ROOT),
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(ROOT / "src")},
    )
    partial = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        partial = next((p for p in tmp_path.iterdir() if p.name.endswith(".partial")), None)
        if partial is not None:
            break
        time.sleep(0.02)
    assert partial is not None, "the copy never started"
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)

    assert _backups(tmp_path) == [], sorted(p.name for p in tmp_path.iterdir())
    assert path.read_bytes() == before, "killing the copy changed the database it was copying"
    assert _version(path) == 4


def test_a_copy_is_checked_before_it_is_named_a_backup(tmp_path: Path, monkeypatch) -> None:
    """The rename happens only after the copy is verified: a truncated copy is deleted, not kept."""
    from ate.storage import backup as backup_module

    path = tmp_path / "ate.db"
    _older_database(path, 4, ["equipment"])
    target = tmp_path / "ate.db.backup-20260101-000000"
    monkeypatch.setattr(backup_module, "_copy_name", lambda p: target)

    def truncated_copy(source: Path, copy: Path) -> str | None:
        """Say the copy is missing a table, as a copy stopped halfway would."""
        return "the copy holds 6 of the source's 7 tables"

    monkeypatch.setattr(backup_module, "_copy_is_sound", truncated_copy)
    with pytest.raises(BackupError) as raised:
        backup_for_migration(path)
    assert not target.exists(), "a copy that failed its check was renamed anyway"
    assert not any(p.name.endswith(".partial") for p in tmp_path.iterdir()), sorted(tmp_path.iterdir())
    assert str(target) in str(raised.value), str(raised.value)


def test_a_copy_is_one_file_and_a_database_ate_can_open(tmp_path: Path) -> None:
    """The copy is a standalone file: no `.partial`, no `-wal` left beside it, same tables, same version."""
    path = tmp_path / "ate.db"
    _older_database(path, 4, ["equipment"])
    copy = backup_for_migration(path)
    assert copy is not None
    beside = sorted(p.name for p in tmp_path.iterdir())
    assert not any("partial" in name for name in beside), beside
    assert copy.name in beside, beside
    conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert version == 4 and tables == _tables(path), beside


def test_a_copy_never_overwrites_a_file_that_is_already_there(tmp_path: Path, monkeypatch) -> None:
    """An Operator's own file keeps its contents: the migration will not write over it."""
    from ate.storage import backup as backup_module

    path = tmp_path / "ate.db"
    _older_database(path, 4, ["equipment"])
    taken = tmp_path / "ate.db.backup-today"
    taken.write_text("the Administrator's own copy", encoding="utf-8")
    monkeypatch.setattr(backup_module, "_copy_name", lambda p: taken)

    with pytest.raises(BackupError) as raised:
        backup_for_migration(path)
    assert taken.read_text(encoding="utf-8") == "the Administrator's own copy"
    assert not any(p.name.endswith(".partial") for p in tmp_path.iterdir()), sorted(tmp_path.iterdir())
    assert "already" in str(raised.value).lower(), str(raised.value)


def test_a_copy_that_runs_past_the_deadline_is_refused_in_one_line(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """A database locked by another process is refused inside the deadline, not waited out.

    The holder sits on a write transaction for 30 s. :data:`BACKUP_LOCK_TIMEOUT_S` is shortened
    to 2 s: the copy gives up inside it, in one line naming the lock, leaving the database and
    no copy behind. Before the deadline was enforced this waited for the lock instead."""
    from ate.storage import backup as backup_module

    monkeypatch.setattr(backup_module, "BACKUP_LOCK_TIMEOUT_S", 2.0)
    path = tmp_path / "ate.db"
    _older_database(path, 4, ["equipment"])
    before = path.read_bytes()
    holder = _locked_holder(path, 30)
    try:
        started = time.monotonic()
        code, out, err = _cli(["db", "migrate", str(path), "--yes"], capsys)
        elapsed = time.monotonic() - started
    finally:
        holder.kill()
        holder.wait(timeout=10)

    text = out + err
    assert elapsed < 6, f"the migration waited {elapsed} s on a locked database"
    assert code == 1, text
    assert "lock" in text.lower(), text
    assert "2.0" in text, text
    assert "Traceback" not in err, err
    assert _backups(tmp_path) == [], sorted(p.name for p in tmp_path.iterdir())
    assert not any(p.name.endswith(".partial") for p in tmp_path.iterdir()), sorted(tmp_path.iterdir())
    assert path.read_bytes() == before, "the refused copy changed the database"
    assert _version(path) == 4


def test_a_migration_that_fails_leaves_the_copy_and_names_it(tmp_path: Path) -> None:
    """The copy is taken first, so a migration that then fails can be rolled back — and says where."""
    path = tmp_path / "ate.db"
    _older_database(path, 4, ["equipment"])
    path.chmod(0o444)  # readable, so it can be copied; not writable, so the migration cannot run

    code, out, err = _cli_subprocess(["db", "migrate", str(path), "--yes"])
    text = out + err
    assert code == 1, text
    copies = _backups(tmp_path)
    assert len(copies) == 1, text
    assert str(copies[0]) in text, text
    assert "Traceback" not in err, err
    assert _version(path) == 4, "the failed migration changed a database it could not write"
    path.chmod(0o644)


# --- the version is stamped only when a migration ran ----------------------------------


def test_a_database_missing_a_table_is_stamped_after_the_migration(tmp_path: Path) -> None:
    """The honest stamp: the migration created the table, so the version is raised."""
    path = tmp_path / "ate.db"
    _older_database(path, 4, ["equipment"])
    assert migrate_database(path) == 0
    assert _version(path) == SCHEMA_VERSION and "equipment" in _tables(path)


def test_a_version_bump_with_no_migration_is_refused(tmp_path: Path) -> None:
    """A database that stores an older version but has every table is not stamped: it is refused."""
    path = tmp_path / "ate.db"
    RunStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION - 1}")
    conn.commit()
    conn.close()
    assert "equipment" in _tables(path)

    raised: Exception | None = None
    try:
        RunStore(path).close()
    except SchemaVersionError as exc:
        raised = exc
    assert raised is not None, "opening a database that needs no migration must not stamp it"
    assert str(SCHEMA_VERSION - 1) in str(raised), str(raised)

    code, out, err = _cli_subprocess(["db", "migrate", str(path), "--yes"])
    assert code == 1
    text = out + err
    assert str(SCHEMA_VERSION - 1) in text, text
    assert "Traceback" not in err, err


def test_the_refusal_leaves_the_database_as_it_was(tmp_path: Path) -> None:
    """Refusing a stamp changes nothing in the file, and makes no copy."""
    path = tmp_path / "ate.db"
    RunStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION - 1}")
    conn.commit()
    conn.close()
    before = path.read_bytes()
    assert migrate_database(path) != 0
    assert path.read_bytes() == before
    assert _backups(tmp_path) == [], "a database that needed no migration gets no copy"
    assert _version(path) == SCHEMA_VERSION - 1


# --- ate setup migrates the database the config names ----------------------------------


def test_ate_setup_migrates_the_database_the_config_names(tmp_path: Path, capsys) -> None:
    """A Station whose ``database.path`` is elsewhere gets *that* database migrated."""
    station = tmp_path / "station"
    config = station / "config" / "config.json"
    db = tmp_path / "other" / "station.db"
    config.parent.mkdir(parents=True)
    (station / "data").mkdir(parents=True)
    config.write_text(
        '{"format": 1, "station": {"name": "Station 35"}, '
        f'"database": {{"type": "sqlite", "path": "{db}"}}}}',
        encoding="utf-8",
        newline="\n",
    )

    code, out, err = _cli(["setup", "--root", str(station), "--no-permissions"], capsys)
    assert code == 0, err
    assert str(db) in out, out
    assert db.exists()
    assert _version(db) == SCHEMA_VERSION
    assert not (station / "data" / "ate.db").exists()


def test_ate_setup_falls_back_to_the_data_directory_when_there_is_no_config(tmp_path: Path, capsys) -> None:
    """No config file at all is not a fault: setup uses the data directory it prepared and says so."""
    station = tmp_path / "station"
    config = station / "config" / "config.json"
    config.parent.mkdir(parents=True)
    (station / "data").mkdir(parents=True)

    code, out, err = _cli(["setup", "--root", str(station), "--no-permissions"], capsys)
    assert code == 0, err
    default = station / "data" / "ate.db"
    assert str(default) in out, out
    assert "wrote default config" in out, out
    assert default.exists()


def test_ate_setup_refuses_a_config_it_cannot_read_instead_of_guessing(tmp_path: Path, capsys) -> None:
    """A config that is there but unreadable is refused in one line, not silently replaced.

    Falling back to ``<data>/ate.db`` migrates a database nobody opens while the real one stays
    at the old schema: the opposite of what the Administrator asked for (packet 35 review)."""
    station = tmp_path / "station"
    config = station / "config" / "config.json"
    config.parent.mkdir(parents=True)
    (station / "data").mkdir(parents=True)
    config.write_text("{ this is not json", encoding="utf-8", newline="\n")

    code, out, err = _cli(["setup", "--root", str(station), "--no-permissions"], capsys)
    text = out + err
    assert code == 1, text
    assert str(config) in text, text
    assert "cannot read the config" in text.lower(), text
    assert "Traceback" not in err, err
    assert not (station / "data" / "ate.db").exists(), "setup migrated a database it never read"


@pytest.mark.parametrize("body", [
    b'{"format": 1, "database": 5}',
    b'{"format": 1, "database": {"path": "\xff\xfe"}}',
    b'{"format": 1, "defnitions": {"directories": ["x"]}}',
    b'{"format": 2, "database": {"path": "/tmp/x.db"}}',
])
def test_a_config_that_cannot_be_read_at_all_is_refused(tmp_path: Path, capsys, body: bytes) -> None:
    """A wrong type, a bad encoding, a typo and a wrong format all refuse; none of them guesses.

    Each of these used to fall back to ``<data>/ate.db`` with exit 0, migrating a database the
    Station does not use (packet 35 review)."""
    station = tmp_path / "station"
    config = station / "config" / "config.json"
    config.parent.mkdir(parents=True)
    (station / "data").mkdir(parents=True)
    config.write_bytes(body)

    code, out, err = _cli(["setup", "--root", str(station), "--no-permissions"], capsys)
    text = out + err
    assert code == 1, text
    assert str(config) in text, text
    assert "cannot read the config" in text.lower(), text
    assert "Traceback" not in err, err
    assert not (station / "data" / "ate.db").exists(), "setup migrated a database it never read"


def test_ate_setup_backs_up_an_older_database_it_upgrades(tmp_path: Path, capsys) -> None:
    """The installers' path — ``ate setup`` — takes the same copy ``ate db migrate`` takes."""
    station = tmp_path / "station"
    data = station / "data"
    data.mkdir(parents=True)
    path = data / "ate.db"
    _older_database(path, 4, ["equipment"])

    code, out, err = _cli(["setup", "--root", str(station), "--no-permissions"], capsys)
    assert code == 0, err
    copies = _backups(data)
    assert len(copies) == 1, out
    assert str(copies[0]) in out, out
    assert _version(path) == SCHEMA_VERSION


# --- --config after the subcommand ------------------------------------------------------


def test_a_config_that_names_no_database_migrates_the_station_it_prepared(tmp_path: Path, capsys) -> None:
    """A config with no ``database.path`` is not a statement about where a database is.

    The default is ``<data>/ate.db``, and ``<data>`` is the platform data directory, not the data
    directory ``--root`` pointed setup at. Migrating the platform one would create a database no
    one opens and leave the Station's own unprepared, so setup migrates the directory it made."""
    from ate.config import _platform_data_dir

    station = tmp_path / "station"
    config = station / "config" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        '{"format": 1, "station": {"name": "Station 35", "uuid": "keep-me"}}',
        encoding="utf-8",
        newline="\n",
    )

    code, out, err = _cli(["setup", "--root", str(station), "--no-permissions"], capsys)
    assert code == 0, err
    own = station / "data" / "ate.db"
    assert str(own) in out, out
    assert own.exists() and _version(own) == SCHEMA_VERSION
    assert not (_platform_data_dir() / "ate.db").exists(), "setup wrote a database in the platform data directory"


def test_config_after_a_subcommand_selects_the_config(tmp_path: Path, capsys) -> None:
    """`ate db status --config X` reads X as the config, not as a database path."""
    station = tmp_path / "station"
    config = station / "config" / "config.json"
    config.parent.mkdir(parents=True)
    _cli(["setup", "--root", str(station), "--no-permissions"], capsys)
    capsys.readouterr()

    code, out, err = _cli(["db", "status", "--config", str(config)], capsys)
    assert code == 0, err
    assert out.strip() == str(SCHEMA_VERSION), out

    # Before the subcommand still works, and an explicit database path still wins.
    code, out, err = _cli(["db", "--config", str(config), "status"], capsys)
    assert code == 0 and out.strip() == str(SCHEMA_VERSION), out
    db = station / "data" / "ate.db"
    code, out, err = _cli(["db", "migrate", str(db), "--yes", "--config", str(config)], capsys)
    assert code == 0 and str(db) in out, out


def test_config_flag_with_no_value_is_a_one_line_error(tmp_path: Path, capsys) -> None:
    code, out, err = _cli(["db", "status", "--config"], capsys)
    assert code == 1
    assert "needs a path" in err, err
    assert "Traceback" not in err


def test_a_path_that_is_not_a_database_fails_in_one_line(tmp_path: Path) -> None:
    """Not a traceback and not an unhandled sqlite3.DatabaseError: one line, exit 1."""
    not_a_db = tmp_path / "not-a-database.db"
    not_a_db.write_text("this is a text file, not a database", encoding="utf-8", newline="\n")

    code, out, err = _cli_subprocess(["db", "status", str(not_a_db)])
    assert code == 1
    assert "not a SQLite database" in err, err
    assert "Traceback" not in err, err
    assert err.count("\n") <= 1, err

    code, out, err = _cli_subprocess(["db", "migrate", str(not_a_db), "--yes"])
    assert code == 1 and "Traceback" not in err, err


# --- a database nothing can migrate -----------------------------------------------------


def _no_migration_message(path: Path) -> str:
    """The whole line this ATE prints for a database at an older version with every table."""
    return (
        f"the database at {path} stores schema version {SCHEMA_VERSION - 1} and already holds every "
        f"table this ATE declares: this ATE has no migration from version {SCHEMA_VERSION - 1} to "
        f"{SCHEMA_VERSION}, so it is not stamped as {SCHEMA_VERSION}"
    )


def _database_at_an_older_version_with_every_table(path: Path) -> None:
    """A database this ATE built, then stamped one version behind without dropping anything."""
    RunStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION - 1}")
    conn.commit()
    conn.close()


def test_a_database_with_every_table_and_an_old_version_is_refused_by_name(tmp_path: Path, capsys) -> None:
    """It is its own status, not NEEDS_MIGRATE: telling the Operator to migrate is a dead loop.

    The whole message is checked, because the manual quotes it word for word."""
    from ate.storage import DbLaunchStatus, check_database_at_launch

    path = tmp_path / "ate.db"
    _database_at_an_older_version_with_every_table(path)
    expected = _no_migration_message(path)

    result = check_database_at_launch(path)
    assert result.status is DbLaunchStatus.NO_MIGRATION, result
    assert result.user_version == SCHEMA_VERSION - 1

    code, out, err = _cli(["db", "status", str(path)], capsys)
    assert code == 1, out + err
    assert err.strip() == expected, repr(err)

    code, out, err = _cli_subprocess(["db", "migrate", str(path), "--yes"])
    assert code == 1, out + err
    assert err.strip() == expected, repr(err)
    assert "Traceback" not in err, err


def test_refusing_a_database_that_cannot_be_migrated_touches_nothing(tmp_path: Path) -> None:
    """No copy, no rewrite, no new WAL: the refusal reads the file and stops."""
    path = tmp_path / "ate.db"
    _database_at_an_older_version_with_every_table(path)
    before = path.read_bytes()
    files = {p.name for p in tmp_path.iterdir()}

    assert migrate_database(path) == 1
    assert path.read_bytes() == before
    # SQLite creates the -wal/-shm companions just by reading a WAL database; nothing else
    # may appear, and the file itself is unchanged.
    added = {p.name for p in tmp_path.iterdir()} - files
    assert added <= {"ate.db-wal", "ate.db-shm"}, sorted(added)
    assert _backups(tmp_path) == [], sorted(tmp_path.iterdir())
    assert _version(path) == SCHEMA_VERSION - 1


def test_the_launch_dialog_says_the_same_one_line(tmp_path: Path) -> None:
    """The GUI never migrates, and it refuses with the same sentence `db status` prints."""
    from ate.app import launch

    station = tmp_path / "station"
    station.mkdir()
    config = station / "config.json"
    path = station / "ate.db"
    _database_at_an_older_version_with_every_table(path)
    config.write_text(
        '{"format": 1, "station": {"name": "Station 35", "uuid": "u-35"}, '
        f'"database": {{"type": "sqlite", "path": "{path}"}}}}',
        encoding="utf-8",
        newline="\n",
    )

    seen: list[str] = []
    outcome = launch(["--config", str(config)], prompt=lambda p: True, alert=seen.append, exec_=False)
    assert outcome.code == 1, outcome
    assert len(seen) == 1, seen
    assert seen[0] == _no_migration_message(path), repr(seen[0])


def test_a_migration_that_fails_with_a_permission_error_names_the_copy(tmp_path: Path, capsys, monkeypatch) -> None:
    """Every failure after the copy names it, in one line, with a non-zero exit and no traceback."""
    from ate.storage import store as store_module

    path = tmp_path / "ate.db"
    _older_database(path, 4, ["equipment"])

    class StoreThatCannotWrite:
        def __init__(self, db_path):
            self.path = db_path

        def close(self) -> None:
            raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(store_module, "RunStore", StoreThatCannotWrite)

    code, out, err = _cli(["db", "migrate", str(path), "--yes"], capsys)
    copies = _backups(tmp_path)
    assert code == 1, out + err
    assert len(copies) == 1, sorted(tmp_path.iterdir())
    assert str(copies[0]) in err, err
    assert "Permission denied" in err, err
    lines = [line for line in err.strip().splitlines() if line]
    assert len(lines) == 1, lines
    assert "Traceback" not in err, err
    assert _version(path) == 4


def test_the_copy_keeps_the_database_mode_and_group(tmp_path: Path) -> None:
    """A copy of a 0600 database is not 0644: the run records do not leak to the machine."""
    if os.name != "posix":
        pytest.skip("file modes are a Unix notion; on Windows the directory's ACL governs the copy")
    path = tmp_path / "ate.db"
    _older_database(path, 4, ["equipment"])
    path.chmod(0o600)
    copy = backup_for_migration(path)
    assert copy is not None
    assert copy.stat().st_mode & 0o777 == 0o600, oct(copy.stat().st_mode)
    assert copy.stat().st_gid == path.stat().st_gid
    path.chmod(0o644)


def test_an_empty_or_table_less_database_gets_no_copy(tmp_path: Path) -> None:
    """The copy rule, stated exactly once: a table has to be about to be created.

    A zero-byte file, or a file with no tables, has nothing to lose. A database at this build's
    version that is missing a table does get a copy, because that migration changes real data."""
    from ate.storage.backup import current_version

    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    assert backup_for_migration(empty) is None
    assert _backups(tmp_path) == [], sorted(p.name for p in tmp_path.iterdir())

    bare = tmp_path / "bare.db"
    conn = sqlite3.connect(bare)
    conn.execute("PRAGMA user_version=4")
    conn.commit()
    conn.close()
    assert backup_for_migration(bare) is None
    assert _backups(tmp_path) == [], sorted(p.name for p in tmp_path.iterdir())

    current = tmp_path / "current.db"
    RunStore(current).close()
    conn = sqlite3.connect(current)
    conn.execute("DROP TABLE equipment")
    conn.commit()
    conn.close()
    assert current_version(current) == SCHEMA_VERSION, "the version stayed current while a table is missing"
    copy = backup_for_migration(current)
    assert copy is not None, sorted(tmp_path.iterdir())
    assert _tables(copy) == _tables(current)


# --- the command line reads its options exactly -----------------------------------------


def test_a_config_for_ate_is_not_read_as_config_dir(tmp_path: Path, capsys) -> None:
    """`ate setup --config FILE` used to be the prefix of `--config-dir`: it now means the file.

    Setup reads a config (it decides which database to migrate from it), so the flag means the
    same thing here as everywhere else: the config file to use."""
    station = tmp_path / "station"
    data = station / "data"
    config = tmp_path / "elsewhere" / "station-config.json"
    db = tmp_path / "other" / "station.db"
    data.mkdir(parents=True)
    config.parent.mkdir(parents=True)
    config.write_text(
        '{"format": 1, "station": {"name": "Station 35", "uuid": "u-35"}, '
        f'"database": {{"type": "sqlite", "path": "{db}"}}}}',
        encoding="utf-8",
        newline="\n",
    )

    code, out, err = _cli(["setup", "--root", str(station), "--config", str(config), "--no-permissions"], capsys)
    assert code == 0, out + err
    assert str(db) in out, out
    assert db.exists()
    assert not (station / "config" / "config.json").exists(), "setup wrote a config in a directory it was never told to use"


def test_an_unreadable_config_named_by_a_flag_does_not_change_the_answer(tmp_path: Path, capsys) -> None:
    """Setup reads only the database path from a config: it never writes a station.uuid back.

    A config directory this user cannot write used to make setup fall back to the default
    database, because `read_config` generates a uuid and writes the file."""
    from ate.config import database_path_from_config

    station = tmp_path / "station"
    config_dir = station / "config"
    config_dir.mkdir(parents=True)
    config = config_dir / "config.json"
    config.write_text(
        '{"format": 1, "station": {"name": "Station 35"}, '
        f'"database": {{"type": "sqlite", "path": "{tmp_path / 'elsewhere.db'}"}}}}',
        encoding="utf-8",
        newline="\n",
    )
    os.chmod(config_dir, 0o555)  # readable, not writable: setup cannot add a uuid

    assert database_path_from_config(config) == tmp_path / "elsewhere.db"
    assert (config_dir / "config.json").read_text(encoding="utf-8") == config.read_text(encoding="utf-8")
    assert not any(p.name.endswith(".tmp") for p in config_dir.iterdir()), sorted(config_dir.iterdir())

    code, out, err = _cli(["setup", "--root", str(station), "--no-permissions"], capsys)
    assert code == 0, out + err
    assert str(tmp_path / "elsewhere.db") in out, out
    os.chmod(config_dir, 0o755)


def test_a_config_option_with_no_value_is_a_usage_error(tmp_path: Path, capsys) -> None:
    """`ate db status --config=` names nothing: one line, exit 1, no silent default."""
    code, out, err = _cli(["db", "status", "--config="], capsys)
    assert code == 1, out + err
    assert "--config needs a path" in err, err
    assert "Traceback" not in err, err

    code, out, err = _cli(["db", "status", "--config"], capsys)
    assert code == 1, out + err
    assert "--config needs a path" in err, err

    code, out, err = _cli(["--config=", "db", "status", str(tmp_path / "nope.db")], capsys)
    assert code == 1, out + err
    assert "--config needs a path" in err, err


def test_two_configs_that_disagree_are_a_usage_error(tmp_path: Path, capsys) -> None:
    """`ate --config A db status --config B` has no right answer, so it gives none."""
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    for name, db in ((first, tmp_path / "one.db"), (second, tmp_path / "two.db")):
        name.write_text(
            '{"format": 1, "station": {"name": "S", "uuid": "u"}, '
            f'"database": {{"type": "sqlite", "path": "{db}"}}}}',
            encoding="utf-8",
            newline="\n",
        )

    code, out, err = _cli(["--config", str(first), "db", "status", "--config", str(second)], capsys)
    assert code == 1, out + err
    assert "two --config values" in err, err
    assert str(first) in err and str(second) in err, err

    code, out, err = _cli(["--config", str(first), "db", "status", "--config", str(first)], capsys)
    assert code == 1, out + err
    assert "two --config values" not in err, err
    assert "create or upgrade the database" in err, err


def test_a_flag_ate_does_not_have_is_a_usage_error_not_a_silent_misread(tmp_path: Path, capsys) -> None:
    """`--conf`, `--con` and `--roo` are no longer prefixes of real options."""
    for flag in ("--conf", "--con", "--roo"):
        code, out, err = _cli(["setup", "--root", str(tmp_path / "station"), flag, str(tmp_path), "--no-permissions"], capsys)
        assert code == 1, f"{flag}: {out + err}"
        assert "unknown argument" in err, f"{flag}: {err}"


# --- os.exit in the sandbox -------------------------------------------------------------


EXIT_SCHEDULE = """-- exit.tslua: a step that tries to leave the process.
return ScheduleParams("os.exit probe")
  :setSchedule({
    StepParams("os.exit"):setStepNo("1.")
      :setAction(function() os.exit(7) end),
    StepParams("Afterwards"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "the next step ran") end),
  })
"""


def test_os_exit_is_nil_in_the_sandbox_while_os_execute_and_getenv_stay() -> None:
    """The lockdown removes ``os.exit`` the way it removes ``dofile``, ``loadfile`` and ``load``.

    lupa's ``os`` is a small table --- ``clock``, ``date``, ``difftime``, ``execute``, ``exit``,
    ``getenv``, ``remove``, ``rename``, ``setlocale``, ``time``, ``tmpname`` --- and ``abort`` is not
    in it, which is why the manual could not say a schedule would "never need" it (packet 35)."""
    with scratch_directory() as td:
        path = Path(td) / "schedule.tslua"
        path.write_text('return ScheduleParams("probe") :setSchedule({})', encoding="utf-8", newline="\n")
        host = LuaHost(str(path))
        env = host._runtime.globals()
        os_table = env["os"]
        assert os_table["exit"] is None, "os.exit must be nil in a schedule"
        assert os_table["execute"] is not None, "os.execute stays available"
        assert os_table["getenv"] is not None, "os.getenv stays available"
        assert os_table["abort"] is None, "lupa has no os.abort to remove"
        assert env["dofile"] is None and env["load"] is None


def test_os_exit_in_a_step_ends_the_step_with_an_error_not_the_process(app: QCoreApplication) -> None:
    """``os.exit(7)`` is a plain Lua error now: the step is ERROR, the run is recorded, ATE survives."""
    with scratch_directory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule = Path(td) / "exit.tslua"
        schedule.write_text(EXIT_SCHEDULE, encoding="utf-8", newline="\n")
        engine = RunEngine(store, str(schedule))
        uuid: dict[str, str] = {}
        engine.run_uuid_reported.connect(lambda run_uuid: uuid.setdefault("run_uuid", run_uuid))
        _drive(engine, app)
        run = store.get_test_run(uuid["run_uuid"])
        steps = {n["step_number"]: n["result"] for n in store.nodes(uuid["run_uuid"]) if n["kind"] == "step"}
        store.close()

    assert run["result"] == "FAIL" and run["ended_at"], run
    assert steps == {"1.": "ERROR", "2.": "PASS"}, steps


# --- reports.paperSize -----------------------------------------------------------------


def _config_written(tmp_path: Path, body: str) -> Path:
    config = tmp_path / "config.json"
    config.write_text(body, encoding="utf-8", newline="\n")
    return config


def test_paper_size_is_validated_when_the_config_is_read(tmp_path: Path) -> None:
    """A bad `reports.paperSize` is named at read time, with the values that are allowed."""
    try:
        read_config(_config_written(tmp_path, '{"format": 1, "reports": {"paperSize": "Legal"}}'))
    except ConfigError as exc:
        message = str(exc)
    else:
        raise AssertionError("a bad reports.paperSize must not read")

    assert "reports.paperSize" in message, message
    assert "A4" in message and "Letter" in message, message
    assert "Legal" in message, message


def test_the_two_shipped_paper_sizes_read(tmp_path: Path) -> None:
    for size in ("A4", "Letter"):
        config = _config_written(tmp_path, f'{{"format": 1, "reports": {{"paperSize": "{size}"}}}}')
        assert read_config(config).reports_paper_size == size
    assert read_config(_config_written(tmp_path, '{"format": 1}')).reports_paper_size == "A4"


def test_the_registry_closes_the_handle_and_never_calls_a_driver_close() -> None:
    """``Driver.close`` says it is never reached. This is the proof, not the assertion.

    The docstring named a driver that overrides it as the exception; the sentence was written so a
    reader could not tell which path really runs. A driver that records every call its ``close``
    gets settles it (packet 35 review)."""
    from ate.devices.registry import DeviceRegistry, Driver

    calls: list[str] = []

    class _RecordingDriver(Driver):
        name = "RECORD"

        def open(self, params: dict) -> object:
            class _Handle:
                def close(self) -> None:
                    calls.append("handle")

            return _Handle()

        def close(self, device: object) -> None:
            calls.append("driver")

    registry = DeviceRegistry()
    registry.register("RECORD", _RecordingDriver)
    handle = registry.open_device("RECORD", {})
    assert registry.close_device(handle) is True
    assert calls == ["handle"], f"the registry tore down the driver too: {calls}"

    doc = Driver.close.__doc__ or ""
    assert "No shipped driver overrides it" not in doc, (
        "the docstring still says no driver overrides close while naming one that does"
    )
    assert "never" in doc and "handle" in doc, "the docstring stopped naming the teardown it is not"


# --- the harness closes what it opens, before the temporary directory is removed (packet 35) ---

#: Windows cannot delete a file it still has open: `WinError 32`. Every headless run the harness
#: starts opens its own application log inside the scratch directory, so a session left open makes
#: `tempfile.TemporaryDirectory` fail on the way out - which is what the CI runners did. The spy
#: records the sessions `run_schedule` creates, since the record it returns does not carry them.


def _spy_sessions(run_examples) -> list:
    """Replace the harness's session class with one that records every session it opens."""
    opened: list = []
    real = run_examples.HeadlessRunSession

    class _Spy(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            opened.append(self)

    run_examples.HeadlessRunSession = _Spy
    return opened, real


def _logs_are_closed(work: Path, sessions: list) -> None:
    """Every application log a session opened in ``work`` is closed, not merely written."""
    logs = sorted((work / "logs").glob("ate-app-*.log"))
    assert logs, "the run wrote no application log"
    for session in sessions:
        log = getattr(session, "_app_log", None)
        assert log is not None, "the session ran without its own application log"
        assert log._fh.closed, f"{log.path} is still open when the record came back"


def test_a_headless_run_closes_its_session_before_returning(tmp_path: Path) -> None:
    """The run the manuals' script drives releases its session, log handle included."""
    from examples.harness import run_examples

    opened, real = _spy_sessions(run_examples)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            config = run_examples.write_config(work)
            peers = run_examples.Peers()
            peers.start()
            try:
                with run_examples.bench_env(peers):
                    record = run_examples.run_schedule(config, "Example basics", inputs={"Operator input": 1.5})
            finally:
                peers.stop()
            assert record["run"]["result"] == "PASS", record["run"]
            _logs_are_closed(work, opened)
    finally:
        run_examples.HeadlessRunSession = real


def test_a_run_that_times_out_still_closes_its_session(tmp_path: Path) -> None:
    """The refusal to wait forever is a raise, and a raise must not leave the log open."""
    from examples.harness import run_examples

    opened, real = _spy_sessions(run_examples)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            config = run_examples.write_config(scratch, scratch)
            schedule = scratch / "hang.tslua"
            body = (
                'return ScheduleParams("Scratch: hang", "a step nobody answers")\n'
                "  :setSchedule({StepParams('Wait'):setStepNo('1.')\n"
                "    :setInputs({[1] = {desc = 'value', type = 'FLOAT'}})\n"
                "    :setAction(function() ATE.input(1) end)})\n"
            )
            schedule.write_text(body, encoding="utf-8", newline="\n")
            (scratch / "hang.atedef").write_text(
                json.dumps(
                    {
                        "format": 1,
                        "mediaNumber": "MR-HANG",
                        "name": "A run that never ends",
                        "version": "1.0.0",
                        "files": [{"path": "hang.tslua", "sha256": hashlib.sha256(body.encode()).hexdigest()}],
                    },
                    indent=2,
                ),
                encoding="utf-8",
                newline="\n",
            )
            with pytest.raises(TimeoutError):
                run_examples.run_schedule(config, "Scratch: hang", release_dir=Path(tmp))
            _logs_are_closed(Path(tmp), opened)
    finally:
        run_examples.HeadlessRunSession = real