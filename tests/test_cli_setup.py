"""Seam-2 tests for ``ate setup`` (ticket 26).

``ate setup`` prepares a Station for every install format: it creates the data and config
directories, gives ordinary users modify rights on the database and its WAL files, ``reports``
and ``logs`` while leaving ``config.json`` admin-only (on Linux by creating an ``ate`` group),
writes a default ``config.json`` with a fresh ``station.uuid`` only when none exists, and runs
``ate db migrate``.

These tests drive the command through :func:`ate.cli.main` against temporary directories, exactly
as seam 2 requires (see ``assets/packaging.md``). The privileged operations — making directories,
changing permissions and creating a Linux group — need root, so they are routed through an
injectable :class:`ate.setup.Fs`; the CLI tests neutralise those three calls with monkeypatching
and assert on the externally visible outcomes (the files created, the config written, the database
migrated), while ``test_prepare_station_permission_contract`` asserts the permission contract itself
against a recording fake.
"""

import json
import sqlite3

import pytest

from ate.cli import main
from ate.setup import Fs, StationLayout, prepare_station


class _RecordingFs(Fs):
    """A :class:`Fs` that records every privileged call instead of performing it.

    It does not call ``super().__init__`` (which binds the real system calls as instance
    attributes, shadowing these overrides), so its permission methods record and no-op while its
    directory method still creates them for real — :func:`prepare_station` needs the directories to
    exist before it writes ``config.json``.
    """

    def __init__(self) -> None:
        import os as _os

        self.made_dirs: list[str] = []
        self.chmods: list[tuple[str, int]] = []
        self.chowns: list[tuple[str, int, int]] = []
        self.add_groups: list[str] = []
        self.make_dirs = _os.makedirs
        self.chmod = lambda path, mode: self.chmods.append((str(path), int(mode)))
        self.chown = lambda path, uid, gid: self.chowns.append((str(path), int(uid), int(gid)))
        self.add_group = lambda name: (self.add_groups.append(name) or True)  # type: ignore[assignment]


def _linux_layout(tmp_path):
    """A Linux Station rooted at ``tmp_path`` (data and config are separate directories)."""
    return StationLayout(data_dir=tmp_path / "data", config_dir=tmp_path / "config", platform="linux")


def test_prepare_station_creates_dirs_and_default_config_when_missing(tmp_path, rec_fs):
    """A fresh Station gets its four directories plus a default ``config.json`` with a uuid."""
    layout = _linux_layout(tmp_path)

    summary = prepare_station(layout, fs=rec_fs)

    # The data and config directories (and their reports/logs subdirectories) were all created.
    assert {str(p) for p in (layout.data_dir, layout.config_dir, layout.reports_dir, layout.log_dir)} <= {
        str(d) for d in summary["created_dirs"]
    }
    # A default config is written only because none existed, and it carries a fresh uuid.
    assert tmp_path / "config" / "config.json".exists()
    data = json.loads((tmp_path / "config" / "config.json").read_text(encoding="utf-8"))
    assert data["format"] == 1
    assert isinstance(data["station"]["uuid"], str) and data["station"]["uuid"]


def test_prepare_station_does_not_overwrite_an_existing_config(tmp_path, rec_fs):
    """An existing ``config.json`` is never rewritten — its uuid survives."""
    layout = _linux_layout(tmp_path)
    existing = {"format": 1, "station": {"name": "BENCH-03", "uuid": "deadbeef"}}
    (layout.config_file).parent.mkdir(parents=True, exist_ok=True)
    (layout.config_file).write_text(json.dumps(existing), encoding="utf-8")

    prepare_station(layout, fs=rec_fs)

    written = json.loads((tmp_path / "config" / "config.json").read_text(encoding="utf-8"))
    assert written["station"]["uuid"] == "deadbeef"  # untouched


def test_prepare_station_permission_contract(tmp_path, rec_fs):
    """On Linux ordinary users may modify the db/reports/logs but not ``config.json``.

    The database (and its WAL) live in the data directory; ``reports`` and ``logs`` are made
    writable too, each owned by the freshly created ``ate`` group. The config directory is left
    admin-only: no chmod or chown touches it.
    """
    layout = _linux_layout(tmp_path)
    prepare_station(layout, fs=rec_fs)

    # The ate group was requested exactly once.
    assert rec_fs.add_groups == ["ate"]

    writable = {str(p) for p in (layout.data_dir, layout.reports_dir, layout.log_dir)}
    touched = {path for path, _mode in rec_fs.chmods} | {path for path, _, _gid in rec_fs.chowns}
    assert writable <= touched  # every writable target was chmod'd and chowned

    config_touched = touched & {str(layout.config_file), str(layout.config_dir)}
    assert not config_touched  # config.json stays admin-only


def test_prepare_station_windows_makes_no_permission_changes(tmp_path, rec_fs):
    """On Windows the installer runs as an admin per-machine; no group or ownership is needed."""
    layout = StationLayout(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        platform="windows",
    )
    prepare_station(layout, fs=rec_fs)

    assert rec_fs.add_groups == []
    assert not rec_fs.chmods and not rec_fs.chowns


def test_cli_setup_creates_dirs_writes_config_and_migrates(tmp_path, capsys, monkeypatch):
    """``ate setup --root`` prepares the whole Station on disk: dirs, config and a migrated db."""
    # Neutralise the three privileged operations so this runs as an ordinary user.
    monkeypatch.setattr("os.chmod", lambda *a, **k: None)
    monkeypatch.setattr("os.chown", lambda *a, **k: None)
    monkeypatch.setattr("ate.setup._real_add_group", lambda name: False)

    root = tmp_path / "station"
    rc = main(["setup", "--root", str(root)])
    assert rc == 0

    # Directories and a default config with a fresh uuid.
    assert (root / "data").is_dir() and (root / "config").is_dir()
    config = json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))
    assert config["format"] == 1
    assert isinstance(config["station"]["uuid"], str) and config["station"]["uuid"]

    # The database was created by ``ate db migrate`` (schema version 1).
    db_path = root / "data" / "ate.db"
    assert db_path.exists()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 1
    finally:
        conn.close()


def test_cli_setup_does_not_overwrite_an_existing_config(tmp_path, capsys, monkeypatch):
    """``ate setup`` never rewrites a config that already exists."""
    monkeypatch.setattr("os.chmod", lambda *a, **k: None)
    monkeypatch.setattr("os.chown", lambda *a, **k: None)
    monkeypatch.setattr("ate.setup._real_add_group", lambda name: False)

    root = tmp_path / "station"
    (root / "config").mkdir(parents=True)
    (root / "config" / "config.json").write_text(
        json.dumps({"format": 1, "station": {"name": "OLD", "uuid": "keep-me"}}),
        encoding="utf-8",
    )

    main(["setup", "--root", str(root)])

    written = json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))
    assert written["station"]["uuid"] == "keep-me"


def test_cli_setup_runs_migrate_even_when_database_already_exists(tmp_path, capsys, monkeypatch):
    """Re-running ``ate setup`` on an already-prepared Station re-migrates without error."""
    monkeypatch.setattr("os.chmod", lambda *a, **k: None)
    monkeypatch.setattr("os.chown", lambda *a, **k: None)
    monkeypatch.setattr("ate.setup._real_add_group", lambda name: False)

    root = tmp_path / "station"
    main(["setup", "--root", str(root)])  # first run prepares everything
    capsys.readouterr()

    rc = main(["setup", "--root", str(root)])  # second run must not fail
    assert rc == 0


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def rec_fs():
    """A recording :class:`Fs` for tests that inject one directly."""
    return _RecordingFs()
