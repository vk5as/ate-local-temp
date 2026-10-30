"""Packet 30c: the first run after ``ate setup`` works, and the review rework of it.

End to end in real subprocesses: ``ate setup --root <tmp>`` prepares the Station,
``ate --config <tmp>/config/config.json db status`` prints the schema version, and
``db migrate --yes`` from a different directory touches the configured database instead of
creating ``./ate.db`` in the working directory. A GUI launch (``exec_=False``) then opens the
window, and nowhere under the root is a literal ``<data>`` directory — the reader resolves the
placeholder to the platform data directory (``assets/app-config-format.md``), and setup writes
paths already resolved against the data dir it created (packet 30c).

Every launch refusal (missing, older or newer database; invalid config; a data directory this
user cannot write) runs as a subprocess with ``QT_QPA_PLATFORM=offscreen`` and the dialog
auto-dismissed: an abort would exit 134 with ``QWidget: Must construct a QApplication before a
QWidget``, and a traceback would mean the refusal never became a dialog, so exit 1 carrying one
clear line proves the dialog path runs with the QApplication kept alive for the whole launch.

Two environment seams make the platform default testable in a real subprocess (see
``docs/building-from-source.md``): ``ATE_DEFAULT_CONFIG`` replaces the platform default config
path in :func:`ate.config._platform_default`, and ``ATE_DATA_DIR`` replaces the platform data
directory in :func:`ate.config._platform_data_dir`. Nothing else reads them."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import ate.app as ate_app
from ate.app import launch
from ate.config import ConfigError, config_hint, config_path, read_config
from ate.storage import SCHEMA_VERSION, RunStore

_SRC = str(Path(__file__).resolve().parent.parent / "src")


def _run_ate(
    args: list[str],
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run ``python -m ate ...`` in a subprocess, offscreen, with dialogs auto-dismissed."""
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["ATE_AUTO_DISMISS_DIALOG"] = "1"  # headless seam: QMessageBox closes itself (app.py)
    env.update(env_extra or {})
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


# --- the platform default config: bare `ate` and `ate db` with no --config (30c review) ------


def test_the_platform_default_is_a_path_and_can_be_overridden(tmp_path: Path, monkeypatch) -> None:
    """``_platform_default()`` returns a Path (not a str), and ``ATE_DEFAULT_CONFIG`` moves it.

    The reader calls ``.resolve()`` on it, so a str here is an AttributeError for every bare
    ``ate`` and ``ate db status`` (packet 30c review)."""
    monkeypatch.setenv("ATE_DEFAULT_CONFIG", str(tmp_path / "station" / "config.json"))
    assert isinstance(config_path(None), Path), "the default config path must be a Path"
    assert config_path(None) == tmp_path / "station" / "config.json"
    assert config_path(str(tmp_path / "other.json")) == tmp_path / "other.json"
    # The override changes only the default location: a real path still wins over it.
    assert config_path(None) != config_path(str(tmp_path / "other.json"))


def test_db_status_without_a_config_names_the_missing_default_exactly(tmp_path: Path) -> None:
    """No path, no config at the default: one clear line, exit 1, nothing created, no traceback."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    default = tmp_path / "station" / "config.json"  # neither the file nor its directory exists
    expected = f"ate db: config file not found at {default}\nrun 'ate setup' or pass --config PATH first\n"

    for subcommand in (["db", "status"], ["db", "migrate", "--yes"]):
        result = _run_ate(subcommand, cwd=cwd, env_extra={"ATE_DEFAULT_CONFIG": str(default)})
        assert result.returncode == 1, result.stderr
        assert result.stderr == expected, f"got {result.stderr!r}"
        assert "Traceback" not in result.stderr, result.stderr
        assert "AttributeError" not in result.stderr, result.stderr
    assert not default.parent.exists(), "the failed read created a directory"
    assert not (cwd / "ate.db").exists(), "db status guessed a ./ate.db in the cwd"


def test_db_status_reads_a_default_config_when_no_config_is_given(tmp_path: Path) -> None:
    """With a valid config at the default location, bare ``ate db status`` reads it."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    default = tmp_path / "station" / "config.json"
    default.parent.mkdir(parents=True)
    db = tmp_path / "station" / "data" / "ate.db"
    db.parent.mkdir(parents=True)
    RunStore(db)
    default.write_text(
        json.dumps({"format": 1, "database": {"type": "sqlite", "path": str(db)}}), encoding="utf-8"
    )

    result = _run_ate(["db", "status"], cwd=cwd, env_extra={"ATE_DEFAULT_CONFIG": str(default)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(SCHEMA_VERSION), result.stdout


def test_bare_launch_without_a_default_config_refuses_without_a_traceback(tmp_path: Path) -> None:
    """Bare ``ate`` with no config: the prompt closes (the seam answers Abort) and the launch
    exits 1 with the path, instead of the AttributeError the str default path used to raise."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    default = tmp_path / "station" / "config.json"
    result = _run_ate([], cwd=cwd, env_extra={"ATE_DEFAULT_CONFIG": str(default)})
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert f"config file not found at {default}" in output, output
    assert "Traceback" not in output, output
    assert not default.exists() and not default.parent.exists(), "the refusal created a config"


def test_launch_refuses_when_the_data_directory_is_not_writable(tmp_path: Path) -> None:
    """``<data>`` defaults + an unprivileged Station: refusal with the path, exit 1, no traceback.

    The defect (30c review): the "Create default config and launch" defaults resolve to
    ``/var/lib/ate``, and :class:`ate.log.AppLog` raised ``PermissionError`` from its ``mkdir``,
    which :func:`ate.app.launch` never caught."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    data = tmp_path / "station-data"
    data.mkdir()
    data.chmod(0o555)  # readable, never writable
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "format": 1,
                "database": {"type": "sqlite", "path": "<data>/ate.db"},
                "log": {"directory": "<data>/logs"},
                "reports": {"directory": "<data>/reports"},
            }
        ),
        encoding="utf-8",
    )

    result = _run_ate(["--config", str(config)], cwd=cwd, env_extra={"ATE_DATA_DIR": str(data)})
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "Traceback" not in output, output
    assert f"cannot create {data / 'logs'}" in output, output
    assert "ate setup" in output and "writable" in output, output
    assert not (data / "logs").exists() and not (data / "reports").exists(), "the launch made the dirs anyway"
    assert not (cwd / "ate.db").exists(), "the launch opened the database before refusing"


def test_launch_refuses_when_the_default_config_cannot_be_created(tmp_path: Path) -> None:
    """Answering "create" where the file cannot be written refuses with the path (spec: a failed
    write refuses to start), and leaves no half-written config behind."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    default = tmp_path / "station" / "config.json"  # its directory does not exist
    result = _run_ate([], cwd=cwd, env_extra={"ATE_DEFAULT_CONFIG": str(default)})
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "config file not found" in output and str(default) in output, output
    assert "Traceback" not in output, output
    assert not default.exists() and not default.parent.exists(), "a partial config was left behind"


def test_a_default_config_that_cannot_be_written_refuses_in_process(tmp_path: Path) -> None:
    """The "Create default config and launch" answer, where the write fails: a refusal naming the
    path, exit 1, nothing created (``assets/app-config-format.md``)."""
    config = tmp_path / "station" / "config.json"
    alerts: list[str] = []
    outcome = launch(["--config", str(config)], exec_=False, prompt=lambda path: True, alert=alerts.append)
    assert outcome.code == 1 and outcome.window is None
    assert len(alerts) == 1 and f"cannot write default config to {config.resolve()}" in alerts[0], alerts
    assert not (tmp_path / "station").exists() and not config.exists()


def test_launch_refuses_an_unwritable_log_directory_in_process(tmp_path: Path) -> None:
    """The same refusal through the injected ``alert`` seam: message, exit 1, no window."""
    read_only = tmp_path / "station-data"
    read_only.mkdir()
    read_only.chmod(0o555)
    logs = read_only / "logs"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "format": 1,
                "database": {"type": "sqlite", "path": str(tmp_path / "ate.db")},
                "log": {"directory": str(logs)},
                "reports": {"directory": str(read_only / "reports")},
            }
        ),
        encoding="utf-8",
    )
    alerts: list[str] = []
    outcome = launch(["--config", str(config)], exec_=False, alert=alerts.append)
    assert outcome.code == 1 and outcome.window is None
    assert len(alerts) == 1 and f"cannot create {logs}" in alerts[0], alerts
    assert "ate setup" in alerts[0] and "writable" in alerts[0], alerts
    assert not logs.exists() and not (read_only / "reports").exists()


def test_creating_the_defaults_then_an_unwritable_data_dir_refuses(tmp_path: Path, monkeypatch) -> None:
    """The 30c defect exactly: "Create default config and launch" writes the ``<data>`` defaults,
    the log directory under the platform data dir is not writable, and the launch refused with a
    traceback instead of a dialog. Now it is a refusal naming the path."""
    monkeypatch.setattr("ate.config._platform_data_dir", lambda: tmp_path / "station-data")
    data = tmp_path / "station-data"
    data.mkdir()
    data.chmod(0o555)
    config = tmp_path / "config.json"
    alerts: list[str] = []
    outcome = launch(["--config", str(config)], exec_=False, prompt=lambda path: True, alert=alerts.append)
    assert outcome.code == 1 and outcome.window is None, alerts
    written = json.loads(config.read_text(encoding="utf-8"))  # the config that was created is complete
    assert written["format"] == 1 and written["station"]["uuid"], written
    assert any(f"cannot create {data / 'logs'}" in a for a in alerts), alerts
    assert not (data / "logs").exists() and not (data / "reports").exists()


def test_creating_the_default_config_leaves_a_complete_file(tmp_path: Path) -> None:
    """A config written by the missing-config prompt is valid JSON with every key: it is never
    half-written, and the spec keeps a config whose write succeeded (``assets/app-config-format.md``)."""
    config = tmp_path / "config.json"
    alerts: list[str] = []
    outcome = launch(["--config", str(config)], exec_=False, prompt=lambda path: True, alert=alerts.append)
    written = json.loads(config.read_text(encoding="utf-8"))  # raises if the file were truncated
    assert written["format"] == 1 and written["station"]["uuid"], written
    assert "<data>" in json.dumps(written), "the created config keeps the documented placeholders"
    # Nothing else about the Station exists yet, so the launch then refuses with the database message.
    db = read_config(config).database_path
    assert outcome.code == 1 and outcome.window is None
    assert any(f"to create or upgrade the database at {db}" in a for a in alerts), alerts


def test_a_failed_config_write_leaves_no_temporary_file(tmp_path: Path, monkeypatch) -> None:
    """``write_config`` stages the JSON and moves it, so a failed write leaves nothing behind."""
    config = tmp_path / "config.json"
    config.write_text('{"format": 1}\n', encoding="utf-8")

    def _boom(source, destination):
        raise OSError("disk full")

    monkeypatch.setattr("ate.config.os.replace", _boom)
    from ate.config import write_config

    with pytest.raises(OSError):
        write_config(config, {"format": 1, "station": {"name": "BENCH", "uuid": "x"}})
    assert not (tmp_path / "config.json.tmp").exists(), "a staged config was left in the config directory"
    assert json.loads(config.read_text(encoding="utf-8")) == {"format": 1}, "the original config was damaged"


# --- the migrate hint names the config and the database (30c review) --------------------------


def test_the_migrate_hint_names_the_config_and_the_database(tmp_path: Path) -> None:
    """A non-default config makes the hint ``ate db migrate --config <path>``, and the message
    always names the database it is talking about."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    config = tmp_path / "config.json"
    db = tmp_path / "station" / "data" / "ate.db"
    config.write_text(
        json.dumps({"format": 1, "database": {"type": "sqlite", "path": str(db)}}), encoding="utf-8"
    )

    result = _run_ate(["--config", str(config), "db", "status"], cwd=cwd)
    assert result.returncode == 1, result.stderr
    assert f"run `ate db migrate --config {config}` to create or upgrade the database at {db}" in result.stderr

    # The same config at the platform default needs no --config in the hint: the bare command works.
    result = _run_ate(["db", "status"], cwd=cwd, env_extra={"ATE_DEFAULT_CONFIG": str(config)})
    assert result.returncode == 1, result.stderr
    assert f"run `ate db migrate` to create or upgrade the database at {db}" in result.stderr
    assert "--config" not in result.stderr, result.stderr
    assert not db.exists(), "status created the database it refused to open"


def test_config_hint_is_none_for_the_platform_default(tmp_path: Path, monkeypatch) -> None:
    """`--config` is only worth naming when the config is not the one the platform finds by default."""
    monkeypatch.setenv("ATE_DEFAULT_CONFIG", str(tmp_path / "default.json"))
    assert config_hint(tmp_path / "default.json") is None
    assert config_hint(str(tmp_path / "other.json")) == (tmp_path / "other.json").resolve()
    assert config_hint(None) is None


# --- ate setup creates the Test Definition directory (30c review) -----------------------------


def test_rooted_setup_creates_the_definitions_directory(tmp_path: Path) -> None:
    """The default config lists ``<data>/definitions`; setup creates it so a fresh Station does not
    warn on every scan (packet 30c review)."""
    root = tmp_path / "station"
    result = _run_ate(["setup", "--root", str(root)])
    assert result.returncode == 0, result.stderr
    definitions = root / "data" / "definitions"
    assert definitions.is_dir(), f"setup created no definitions directory: {sorted(root.glob('*'))}"
    written = json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))
    assert written["testDefinitions"]["directories"] == [str(definitions)], written


# --- the auto-dismiss seam (30c review) ---------------------------------------------------------


@pytest.mark.parametrize("value,dismissed", [("1", True), ("true", True), ("yes", True), ("", False), ("0", False), ("false", False), ("no", False), ("off", False)])
def test_auto_dismiss_seam_treats_falsy_values_as_unset(qapp, monkeypatch, value: str, dismissed: bool) -> None:
    """``ATE_AUTO_DISMISS_DIALOG=""``/``"0"``/``"false"`` means unset, so the real dialog stays."""
    scheduled: list[int] = []
    monkeypatch.setattr(ate_app.QTimer, "singleShot", lambda delay, slot: scheduled.append(delay))
    monkeypatch.setenv("ATE_AUTO_DISMISS_DIALOG", value)
    ate_app._dismiss_if_headless(ate_app.QMessageBox())
    assert bool(scheduled) is dismissed


# --- launch refusals in real subprocesses -------------------------------------------------------


def _station_config(tmp_path: Path) -> Path:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "format": 1,
                "database": {"type": "sqlite", "path": str(tmp_path / "ate.db")},
                "log": {"directory": str(tmp_path / "logs")},
                "reports": {"directory": str(tmp_path / "reports")},
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
    assert str(db) in output, f"the refusal never named the database: {output}"

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
