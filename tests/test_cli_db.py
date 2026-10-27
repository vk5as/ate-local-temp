"""CLI database subcommand tests: ``ate db migrate`` / ``status`` and launch refusals.

Ticket 02's seam-2 slice, driven through :func:`ate.cli.main` (no subprocess) against real
SQLite files in a temp dir. It pins the refusal contract at launch — the app never migrates
on its own: a missing or older database refuses with NEEDS_MIGRATE and must be migrated first;
a newer one refuses with TOO_OLD and is left untouched. ``--yes`` skips the interactive prompt
so automation (and tests) can drive migrate non-interactively.
"""

import sqlite3

from ate.cli import main
from ate.storage import DbLaunchStatus, RunStore, SCHEMA_VERSION, check_database_at_launch


def _missing_db(tmp_path):
    """A path that does not exist yet — exactly what launch must refuse."""
    return str(tmp_path / "ate.db")


def test_status_refuses_missing_database_and_migrate_creates_it(tmp_path, capsys):
    """A missing database refuses launch; ``db migrate --yes`` creates it (schema v1)."""
    path = _missing_db(tmp_path)

    # Launch refusal: status reports the message and a non-zero exit.
    rc, captured = main(["db", "status", path]), capsys.readouterr()
    assert rc == 1
    assert "run `ate db migrate`" in captured.err

    # Migrate creates it without an interactive prompt.
    rc, captured = main(["db", "migrate", path, "--yes"]), capsys.readouterr()
    assert rc == 0
    assert captured.out.strip().startswith("created or upgraded database at")

    # Now launchable: status prints the schema version and exits cleanly.
    rc, captured = main(["db", "status", path]), capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == str(SCHEMA_VERSION)


def test_migrate_is_idempotent(tmp_path, capsys):
    """Migrating an already-migrated database succeeds again (no error)."""
    path = _missing_db(tmp_path)
    main(["db", "migrate", path, "--yes"])
    rc, captured = main(["db", "migrate", path, "--yes"]), capsys.readouterr()
    assert rc == 0
    assert captured.out.strip().startswith("created or upgraded database at")


def test_status_reports_schema_version_of_migrated_db(tmp_path, capsys):
    """After migrate, ``db status`` prints the schema version and exits cleanly."""
    path = _missing_db(tmp_path)
    main(["db", "migrate", path, "--yes"])  # create it; drain its stdout so it doesn't leak
    capsys.readouterr()

    rc, captured = main(["db", "status", path]), capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == str(SCHEMA_VERSION)


def test_migrate_refuses_a_database_from_newer_ate_and_leaves_it(tmp_path, capsys):
    """A database built by a newer ATE (user_version > schema) refuses migrate/status.

    The file is left exactly as it was — migrate never downgrades or creates tables here.
    """
    path = _missing_db(tmp_path)
    # Bootstrap a real current database, then simulate a newer ATE by bumping its version one above schema.
    RunStore(path)  # creates the tables and sets user_version to SCHEMA_VERSION
    conn = sqlite3.connect(f"file:{path}?mode=rwc", uri=True)
    try:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    finally:
        conn.close()

    rc, captured = main(["db", "status", path]), capsys.readouterr()
    assert rc == 1
    assert "too old" in captured.err.lower()

    # migrate refuses too and must not touch the file.
    rc, captured = main(["db", "migrate", path, "--yes"]), capsys.readouterr()
    assert rc == 1
    assert "too old" in captured.err.lower()

    # The database is still at the newer version it started with (untouched).
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()
    assert version == SCHEMA_VERSION + 1


def test_migrate_without_yes_prompts_for_confirmation(tmp_path, capsys, monkeypatch):
    """Without --yes, migrate asks for confirmation and aborts on a non-affirmative."""
    path = _missing_db(tmp_path)
    monkeypatch.setattr("builtins.input", lambda *_args: "n")

    rc, captured = main(["db", "migrate", path]), capsys.readouterr()
    assert rc == 1
    # The aborted migrate must not create the database.
    assert not (tmp_path / "ate.db").exists()


def test_schema_version_is_4() -> None:
    """Packet 15b's ``schedule_instance`` table bumped the schema to 4; this pins the bump."""
    assert SCHEMA_VERSION == 4


def test_a_v3_database_needs_migration_and_gains_schedule_instance(tmp_path, capsys):
    """A v3 database (no ``schedule_instance``) refuses launch; migrating adds the table and sets v4."""
    path = _missing_db(tmp_path)
    RunStore(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE schedule_instance")
        conn.execute("PRAGMA user_version=3")
        conn.commit()
    finally:
        conn.close()

    result = check_database_at_launch(path)
    assert (result.status, result.user_version) == (DbLaunchStatus.NEEDS_MIGRATE, 3)
    rc, captured = main(["db", "status", path]), capsys.readouterr()
    assert rc == 1 and "run `ate db migrate`" in captured.err

    rc, captured = main(["db", "migrate", path, "--yes"]), capsys.readouterr()
    assert rc == 0
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()
    assert "schedule_instance" in tables and version == 4
    assert check_database_at_launch(path).status == DbLaunchStatus.OK
