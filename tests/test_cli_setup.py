"""Seam-2 tests for ``ate setup`` (ticket 26a).

``ate setup`` prepares a Station for every install format: it creates the data directories (with
their ``reports`` and ``logs`` subdirectories) and the config directory, writes a default
``config.json`` with a fresh ``station.uuid`` only when none exists — never overwriting one — and
runs the same migration ``ate db migrate`` performs. These tests drive the command through
:func:`ate.cli.main` against temporary directories, exactly as seam 2 requires (see
``assets/packaging.md``).
"""

import json
import sqlite3

from ate.cli import main
from ate.storage import RunStore


def test_setup_creates_dirs_writes_config_and_migrates_database(tmp_path, capsys):
    """A fresh ``ate setup --root`` creates the dirs, a default config and a migrated database."""
    root = tmp_path / "station"

    rc, captured = main(["setup", "--root", str(root)]), capsys.readouterr()
    assert rc == 0

    # The data directory (with reports/logs) and the config directory were all created.
    for path in (root / "data", root / "config", root / "data" / "reports", root / "data" / "logs"):
        assert path.is_dir()

    # A default config with a fresh station.uuid was written.
    config = json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))
    assert config["format"] == 1
    assert isinstance(config["station"]["uuid"], str) and config["station"]["uuid"]

    # The database was migrated (schema version is set, not a missing/empty file).
    db_path = root / "data" / "ate.db"
    assert db_path.exists()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) > 0
    finally:
        conn.close()

    # The summary printed what was created.
    assert "created:" in captured.out


def test_setup_db_status_reports_current_after_migrate(tmp_path, capsys):
    """After ``ate setup``, ``ate db status`` reports the migrated database as current."""
    root = tmp_path / "station"
    main(["setup", "--root", str(root)])  # prepare dirs, config and migrate the database
    capsys.readouterr()  # drain setup's summary so it doesn't leak into the status capture

    rc, captured = main(["db", "status", str(root / "data" / "ate.db")]), capsys.readouterr()
    assert rc == 0
    assert captured.out.strip().isdigit()


def test_setup_second_run_keeps_config_and_is_a_noop(tmp_path, capsys):
    """Re-running ``ate setup`` keeps the same config.json (same UUID and bytes) and does nothing new."""
    root = tmp_path / "station"

    main(["setup", "--root", str(root)])  # first run prepares everything
    capsys.readouterr()

    first_bytes = (root / "config" / "config.json").read_bytes()
    uuid1 = json.loads(first_bytes)["station"]["uuid"]

    rc, captured = main(["setup", "--root", str(root)]), capsys.readouterr()
    assert rc == 0

    # The config was never rewritten: identical bytes and the same UUID.
    second_bytes = (root / "config" / "config.json").read_bytes()
    assert first_bytes == second_bytes
    assert json.loads(second_bytes)["station"]["uuid"] == uuid1

    # Nothing new was created on the no-op run.
    assert "created:" not in captured.out


def test_setup_never_overwrites_an_existing_config(tmp_path):
    """An existing ``config.json`` is left untouched — its UUID and bytes survive."""
    root = tmp_path / "station"
    (root / "config").mkdir(parents=True)
    existing = {"format": 1, "station": {"name": "BENCH-03", "uuid": "keep-me"}}
    (root / "config" / "config.json").write_text(json.dumps(existing), encoding="utf-8")

    main(["setup", "--root", str(root)])

    written = json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))
    assert written["station"]["uuid"] == "keep-me"


def test_setup_migrates_an_existing_database_without_recreating_it(tmp_path):
    """An existing database is migrated, not recreated — its identity and version are preserved."""
    root = tmp_path / "station"
    db_path = root / "data" / "ate.db"

    (root / "data").mkdir(parents=True)  # a real install already has its data directory

    # Bootstrap a real database (schema v3) before setup runs.
    RunStore(db_path)  # creates the tables and sets user_version to 3
    inode_before = db_path.stat().st_ino
    version_before = int(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True).execute("PRAGMA user_version").fetchone()[0])

    main(["setup", "--root", str(root)])

    # The file was not recreated (same inode) and its schema version is unchanged.
    assert db_path.stat().st_ino == inode_before
    version_after = int(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True).execute("PRAGMA user_version").fetchone()[0])
    assert version_after == version_before


def test_setup_unwritable_target_fails_with_nonzero_exit(tmp_path, capsys):
    """An unwritable target (a path blocked by a regular file) fails with a non-zero exit."""
    blocker = tmp_path / "blocker"  # a regular file blocks the data/config subtree below it
    blocker.write_text("x", encoding="utf-8")

    rc, captured = main(
        ["setup", "--data-dir", str(blocker / "data"), "--config-dir", str(blocker / "config")],
    ), capsys.readouterr()
    assert rc != 0
    assert "failed" in captured.err.lower()
