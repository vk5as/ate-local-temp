"""Packet 30c: the first run after ``ate setup`` works.

End to end in real subprocesses: ``ate setup --root <tmp>`` prepares the Station,
``ate --config <tmp>/config/config.json db status`` prints the schema version, and
``db migrate --yes`` from a different directory touches the configured database instead of
creating ``./ate.db`` in the working directory. A GUI launch (``exec_=False``) then opens the
window, and nowhere under the root is a literal ``<data>`` directory — the reader resolves the
placeholder to the platform data directory (``assets/app-config-format.md``), and setup writes
paths already resolved against the data dir it created (packet 30c).

Every launch refusal (missing, older or newer database; invalid config) runs as a subprocess
with ``QT_QPA_PLATFORM=offscreen`` and the dialog auto-dismissed: an abort would exit 134 with
``QWidget: Must construct a QApplication before a QWidget``, so exit 1 carrying the message
proves the dialog path runs with the QApplication kept alive for the whole launch."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ate.app import launch
from ate.config import ConfigError, read_config
from ate.storage import SCHEMA_VERSION, RunStore

_SRC = str(Path(__file__).resolve().parent.parent / "src")


def _run_ate(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run ``python -m ate ...`` in a subprocess, offscreen, with dialogs auto-dismissed."""
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["ATE_AUTO_DISMISS_DIALOG"] = "1"  # headless seam: QMessageBox closes itself (app.py)
    return subprocess.run(
        [sys.executable, "-m", "ate", *args],
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


# --- the setup -> status -> launch path ---------------------------------------------------------


def test_first_run_after_rooted_setup(tmp_path: Path) -> None:
    """``ate setup --root``, then ``db status``, then a GUI launch — the Station just works."""
    root = tmp_path / "station"
    result = _run_ate(["setup", "--root", str(root)])
    assert result.returncode == 0, result.stderr  # unprivileged: setup warns and skips ACLs

    config = root / "config" / "config.json"
    written = json.loads(config.read_text(encoding="utf-8"))
    assert "<data>" not in json.dumps(written), "setup writes resolved paths, not placeholders"

    result = _run_ate(["--config", str(config), "db", "status"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(SCHEMA_VERSION), result.stdout

    # migrate with no path, from a different cwd: it touches the configured database, not ./ate.db
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = _run_ate(["--config", str(config), "db", "migrate", "--yes"], cwd=elsewhere)
    assert result.returncode == 0, result.stderr
    assert (root / "data" / "ate.db").exists(), "the configured database was not migrated"
    assert not (elsewhere / "ate.db").exists(), "db migrate created a stray ./ate.db in the cwd"

    outcome = launch(["--config", str(config)], exec_=False)
    assert outcome.code == 0 and outcome.window is not None
    literal_data = [d for d in tmp_path.rglob("*") if d.name == "<data>" and d.is_dir()]
    assert not literal_data, f"the GUI created a literal {literal_data} directory"


# --- the <data> placeholder -------------------------------------------------------------------


def test_data_placeholder_resolves_to_the_platform_data_dir(tmp_path: Path, monkeypatch) -> None:
    """A config that keeps the documented ``<data>`` defaults is resolved on READ (30c), so it
    stays portable; the reader points every path at the platform data directory."""
    monkeypatch.setattr("ate.config._platform_data_dir", lambda: tmp_path / "station-data")
    config = tmp_path / "config" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"format": 1, "database": {"type": "sqlite", "path": "<data>/ate.db"}}),
        encoding="utf-8",
    )
    cfg = read_config(config)
    assert cfg.database_path == tmp_path / "station-data" / "ate.db"
    assert cfg.log_directory == tmp_path / "station-data" / "logs"  # filled default carries <data>
    assert cfg.reports_directory == tmp_path / "station-data" / "reports"
    assert cfg.test_definition_dirs == [tmp_path / "station-data" / "definitions"]


def test_unknown_placeholder_is_a_config_error(tmp_path: Path) -> None:
    """A placeholder the format does not define stops startup naming the file and key."""
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"format": 1, "database": {"type": "sqlite", "path": "<datx>/ate.db"}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown placeholder '<datx>'"):
        read_config(config)


# --- db commands and the configured database ----------------------------------------------------


def test_db_status_and_migrate_use_the_configured_database(tmp_path: Path) -> None:
    """With no path argument, ``db status``/``db migrate`` use the database named by ``--config``."""
    db = tmp_path / "station" / "data" / "ate.db"
    db.parent.mkdir(parents=True)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"format": 1, "database": {"type": "sqlite", "path": str(db)}}),
        encoding="utf-8",
    )
    cwd = tmp_path / "cwd"
    cwd.mkdir()

    result = _run_ate(["--config", str(config), "db", "status"], cwd=cwd)
    assert result.returncode == 1 and "migrate" in result.stderr, result.stderr

    result = _run_ate(["--config", str(config), "db", "migrate", "--yes"], cwd=cwd)
    assert result.returncode == 0, result.stderr
    assert db.exists() and not (cwd / "ate.db").exists(), "db migrate ignored the configured path"

    result = _run_ate(["--config", str(config), "db", "status"], cwd=cwd)
    assert result.returncode == 0 and result.stdout.strip() == str(SCHEMA_VERSION)


def test_explicit_db_path_still_overrides_the_config(tmp_path: Path) -> None:
    """The path argument keeps its old meaning: explicit wins over the config."""
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"format": 1, "database": {"type": "sqlite", "path": str(tmp_path / "configured.db")}}),
        encoding="utf-8",
    )
    explicit = tmp_path / "explicit.db"
    RunStore(explicit)
    result = _run_ate(["--config", str(config), "db", "status", str(explicit)])
    assert result.returncode == 0 and result.stdout.strip() == str(SCHEMA_VERSION)
    assert not (tmp_path / "configured.db").exists(), "status must not touch the configured db"


@pytest.mark.skipif(Path("/etc/ate/config.json").exists(), reason="this machine has a real default config")
def test_db_without_a_config_says_so(tmp_path: Path) -> None:
    """No path and no config at the platform default: a clear error, no stray ./ate.db."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    result = _run_ate(["db", "status"], cwd=cwd)
    assert result.returncode == 1 and "config" in result.stderr.lower(), result.stderr
    assert not (cwd / "ate.db").exists()


# --- launch refusals in real subprocesses -------------------------------------------------------


def _station_config(tmp_path: Path) -> Path:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "format": 1,
                "database": {"type": "sqlite", "path": str(tmp_path / "ate.db")},
                "log": {"directory": str(tmp_path / "logs")},
            }
        ),
        encoding="utf-8",
    )
    return config


def _db_with_version(db: Path, version: int) -> Path:
    RunStore(db)
    conn = sqlite3.connect(db)
    try:
        if version < SCHEMA_VERSION:
            conn.execute("DROP TABLE equipment")
        conn.execute(f"PRAGMA user_version={version}")
        conn.commit()
    finally:
        conn.close()
    return db


@pytest.mark.parametrize(
    "prepare,expected",
    [
        ("missing", "ate db migrate"),
        ("older", "ate db migrate"),
        ("newer", "too old"),
    ],
)
def test_launch_refusals_exit_one_with_their_message(tmp_path: Path, prepare: str, expected: str) -> None:
    """A missing, older or newer database shows its dialog and exits 1 — never aborts with 134."""
    config = _station_config(tmp_path)
    db = tmp_path / "ate.db"
    if prepare != "missing":
        _db_with_version(db, SCHEMA_VERSION - 1 if prepare == "older" else SCHEMA_VERSION + 1)

    result = _run_ate(["--config", str(config)], cwd=tmp_path)
    output = result.stdout + result.stderr
    assert "Must construct a QApplication" not in output, output
    assert result.returncode == 1, f"exit {result.returncode}: {output}"
    assert expected in output.lower(), output

    logs = list((tmp_path / "logs").glob("ate-app-*.log"))
    assert logs and "launch refused" in logs[0].read_text(), "the refusal never reached the app log"


def test_invalid_config_refusal_exits_one_with_its_message(tmp_path: Path) -> None:
    """An invalid config is refused in a subprocess: message printed, exit 1, no abort."""
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"format": 1, "unknownKey": {"x": 1}}), encoding="utf-8")
    result = _run_ate(["--config", str(config)], cwd=tmp_path)
    output = result.stdout + result.stderr
    assert "Must construct a QApplication" not in output, output
    assert result.returncode == 1, f"exit {result.returncode}: {output}"
    assert "unknown config key" in output, output
