"""Seam-2 tests for ``ate setup`` (tickets 26a and 26b: directories/config/database, plus permissions).

``ate setup`` prepares a Station for every install format: it creates the data directories (with
their ``reports`` and ``logs`` subdirectories) and the config directory, writes a default
``config.json`` with a fresh ``station.uuid`` only when none exists — never overwriting one — and
runs the same migration ``ate db migrate`` performs. These tests drive the command through
:func:`ate.cli.main` against temporary directories, exactly as seam 2 requires (see
``assets/packaging.md``).
"""

import json
import os
import platform as _platform
import sqlite3
import subprocess
import sys
import types
import pytest

from ate.cli import main, _is_root
from ate import setup as ate_setup
from ate.permissions import ATE_GROUP, LinuxPermissions, RecordingPermissions, WindowsPermissions
from ate.setup import SKIP_PERMISSIONS, prepare_station
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

    # Bootstrap a real database (the current schema) before setup runs.
    RunStore(db_path)  # creates the tables and sets user_version to SCHEMA_VERSION
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


def test_prepare_station_applies_permissions_via_seam(tmp_path):
    """prepare_station drives the permissions seam: data tree group-writable, config admin-only."""
    root = tmp_path / "station"
    perms = RecordingPermissions()

    prepare_station(root / "data", root / "config", permissions=perms)

    # Linux treatment through _apply_permissions, in order.
    assert [c["op"] for c in perms.calls] == [
        "ensure_group",
        "set_group_writable",
        "set_admin_only",
        "set_admin_only",
    ]

    # The data tree (database, WAL files, reports/logs) is handed group-writable rights once.
    assert perms.group_writable_paths == [str(root / "data")]
    gw = next(c for c in perms.calls if c["op"] == "set_group_writable")
    assert gw["group"] == ATE_GROUP and gw["recursive"] is True

    # The group is ensured exactly once, named ate.
    ensure_calls = [c for c in perms.calls if c["op"] == "ensure_group"]
    assert len(ensure_calls) == 1 and ensure_calls[0]["group"] == ATE_GROUP

    # config.json AND the config dir are admin-only; config.json is never made user-writable.
    assert str(root / "config" / "config.json") in perms.admin_only_paths
    assert str(root / "config") in perms.admin_only_paths
    assert str(root / "config" / "config.json") not in perms.group_writable_paths


def test_prepare_station_second_run_is_idempotent_with_fake(tmp_path):
    """A second prepare_station run repeats the exact same permission operations."""
    root = tmp_path / "station"

    first = RecordingPermissions()
    prepare_station(root / "data", root / "config", permissions=first)

    second = RecordingPermissions()
    prepare_station(root / "data", root / "config", permissions=second)

    assert [c["op"] for c in first.calls] == [c["op"] for c in second.calls]
    assert first.group_writable_paths == second.group_writable_paths
    assert first.admin_only_paths == second.admin_only_paths


def test_prepare_station_skip_permission_sentinel_creates_but_skips_treatment(tmp_path):
    """SKIP_PERMISSIONS still creates dirs/config/db but applies no permission treatment."""
    root = tmp_path / "station"
    perms = RecordingPermissions()

    prepare_station(root / "data", root / "config", permissions=SKIP_PERMISSIONS)

    # Everything is created, but nothing was handed any group/ownership/ACL treatment.
    assert not perms.calls


def test_real_linux_ensure_group_creates_only_when_missing(monkeypatch):
    """getent reports the group missing → groupadd; once it exists, no create."""
    captured = []
    created = set()  # groups reported as existing by getent once groupadd has run

    def fake_run(argv, **kwargs):
        captured.append(list(argv))
        if argv == ["getent", "group", ATE_GROUP]:
            return types.SimpleNamespace(
                returncode=0 if ATE_GROUP in created else 1,
                stdout=f"{ATE_GROUP}\t4321\n" if ATE_GROUP in created else "",
            )
        if argv == ["groupadd", "--system", ATE_GROUP]:
            created.add(ATE_GROUP)
            return types.SimpleNamespace(returncode=0)
        return types.SimpleNamespace(returncode=0)

    perms = LinuxPermissions()
    monkeypatch.setattr(subprocess, "run", fake_run)

    # First call: group missing → getent fails (rc!=0 / empty stdout) → groupadd.
    perms.ensure_group(ATE_GROUP)
    assert any(c == ["groupadd", "--system", ATE_GROUP] for c in captured)

    # Second call: group now exists → getent reports it, so no second create (idempotent).
    perms.ensure_group(ATE_GROUP)
    assert len([c for c in captured if c == ["groupadd", "--system", ATE_GROUP]]) == 1


def test_real_linux_command_construction(tmp_path, monkeypatch):
    """The Linux implementation builds the right chgrp/chmod argument lists (no real system change)."""
    captured = []

    def fake_run(argv, **kwargs):
        captured.append(list(argv))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    perms = LinuxPermissions()
    data_dir = tmp_path / "data"
    (data_dir / "reports").mkdir(parents=True)
    (data_dir / "logs").mkdir(parents=True)
    (data_dir / "ate.db").write_bytes(b"x")  # a file in the tree, like the migrated database

    perms.set_group_writable(data_dir, ATE_GROUP, recursive=True)

    chgrp_calls = [c for c in captured if len(c) == 3 and c[0] == "chgrp"]
    assert sorted(c[2] for c in chgrp_calls) == sorted(
        [str(data_dir), str(data_dir / "reports"), str(data_dir / "logs"), str(data_dir / "ate.db")],
    )

    dir_modes = [c for c in captured if len(c) == 3 and c[0] == "chmod" and c[1] == "g+ws"]
    file_modes = [c for c in captured if len(c) == 3 and c[0] == "chmod" and c[1] == "g+w"]
    assert sorted(c[2] for c in dir_modes) == sorted(
        [str(data_dir), str(data_dir / "reports"), str(data_dir / "logs")],
    )
    assert sorted(c[2] for c in file_modes) == [str(data_dir / "ate.db")]

    # set_admin_only: owner root; a file 0644 (readable by all, writable only by root), a dir 0755.
    config_file = tmp_path / "config" / "config.json"
    (tmp_path / "config").mkdir(parents=True)
    config_file.write_text("{}")  # must exist for set_admin_only to act on it
    perms.set_admin_only(config_file)
    perms.set_admin_only(tmp_path / "config")

    chowns = [c for c in captured if len(c) == 3 and c[0] == "chown"]
    assert (["chown", "root:root", str(config_file)]) in chowns
    # (path, mode): config.json must stay admin-only (root-owned, writable only by root).
    chmods = [(m[2], m[1]) for m in captured if len(m) == 3 and m[0] == "chmod"]
    assert (str(config_file), "0644") in chmods
    assert (str(tmp_path / "config"), "0755") in chmods


def test_real_windows_command_construction(tmp_path, monkeypatch):
    """The Windows implementation builds the right icacls argument lists."""
    if _platform.system() == "windows":
        pytest.skip("Windows command construction")

    captured = []

    def fake_run(argv, **kwargs):
        captured.append(list(argv))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    perms = WindowsPermissions()
    data_dir = tmp_path / "data"
    config_file = tmp_path / "config.json"

    perms.grant_users_modify(data_dir)
    perms.admin_only(config_file)

    assert ["icacls", str(data_dir), "/grant", "*S-1-5-32-545:(OI)(CI)M"] in captured
    assert [
        "icacls", str(config_file), "/inheritance:r", "/grant:r",
        "*S-1-5-32-544:F", "*S-1-5-18:F", "*S-1-5-32-545:R",
    ] in captured


def _fake_ctypes(monkeypatch, admin: int) -> types.ModuleType:
    """A stand-in for ctypes.windll.shell32 so these tests never touch a real Windows API."""
    fake = types.ModuleType("ctypes")
    fake.windll = types.SimpleNamespace(shell32=types.SimpleNamespace(IsUserAnAdmin=lambda: admin))
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    return fake


def test_is_root_asks_windows_for_admin(monkeypatch):
    """Windows has no euid: elevation is IsUserAnAdmin, not geteuid == 0.

    Patching only os.name and ctypes keeps this true on a Linux runner as well as on Windows.
    """
    monkeypatch.delenv("ATE_ELEVATED", raising=False)  # conftest pins it; this test drives the probe
    monkeypatch.setattr(os, "name", "nt")

    _fake_ctypes(monkeypatch, 1)
    assert _is_root() is True, "an elevated Windows process is root for setup's purposes"

    _fake_ctypes(monkeypatch, 0)
    assert _is_root() is False, "a plain Windows double-click is not elevated"


def test_is_root_is_false_when_the_windows_check_fails(monkeypatch):
    """No ctypes, no windll: never claim elevation we cannot prove."""
    monkeypatch.delenv("ATE_ELEVATED", raising=False)
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setitem(sys.modules, "ctypes", types.ModuleType("ctypes"))
    assert _is_root() is False


def test_is_root_still_reads_the_euid_on_posix(monkeypatch):
    monkeypatch.delenv("ATE_ELEVATED", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    assert _is_root() is True
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    assert _is_root() is False


def test_an_elevated_windows_ate_applies_the_acl_grants(tmp_path, monkeypatch, capsys):
    """The installer runs `ate setup` elevated, and that is what grants Users Modify.

    Before the IsUserAnAdmin fix, _is_root() returned False on Windows and every install
    skipped the ACLs; this drives the real command path with icacls stubbed out.
    """
    captured = []

    def fake_run(argv, **kwargs):
        captured.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.delenv("ATE_ELEVATED", raising=False)  # conftest pins privilege; this test fakes it
    monkeypatch.setattr(sys, "platform", "win32")  # pretend this run is on Windows
    _fake_ctypes(monkeypatch, 1)  # elevated
    monkeypatch.setattr(ate_setup, "_current_platform", lambda: "windows")

    root = tmp_path / "station"
    assert main(["setup", "--root", str(root)]) == 0
    assert "skipping" not in capsys.readouterr().err.lower(), "an elevated setup must not skip the ACLs"
    grants = [argv for argv in captured if argv and argv[0] == "icacls"]
    assert grants, f"elevated setup ran no icacls at all: {captured}"
    modify = [argv for argv in grants if argv[2:4] == ["/grant", "*S-1-5-32-545:(OI)(CI)M"]]
    assert modify, f"the data directory never got the Users Modify grant: {grants}"
    assert modify[0][1] == str(root / "data"), f"the grant landed on the wrong directory: {modify}"
    admin_only = [argv for argv in grants if "/inheritance:r" in argv]
    assert admin_only, f"config.json was not locked to Administrators: {grants}"


def test_unprivileged_ate_still_prepares_without_the_acl_grants(tmp_path, monkeypatch, capsys):
    """The other half: not elevated means warn and skip, never fail."""
    captured = []

    def fake_run(argv, **kwargs):
        captured.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.delenv("ATE_ELEVATED", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    _fake_ctypes(monkeypatch, 0)  # not elevated
    monkeypatch.setattr(ate_setup, "_current_platform", lambda: "windows")

    root = tmp_path / "station"
    assert main(["setup", "--root", str(root)]) == 0
    assert "skipping" in capsys.readouterr().err.lower()
    assert not [argv for argv in captured if argv and argv[0] == "icacls"], "no ACLs without elevation"


def test_setup_no_permissions_skips_privileged_ops(tmp_path, capsys):
    """``ate setup --no-permissions`` still prepares the Station but skips privileged treatment."""
    root = tmp_path / "station"

    rc, captured = main(["setup", "--root", str(root), "--no-permissions"]), capsys.readouterr()
    assert rc == 0
    for path in (root / "data", root / "config"):
        assert path.is_dir()
    assert "skipping" in captured.err.lower()


def test_setup_warns_when_unprivileged(tmp_path, capsys):
    """Without --no-permissions and not running as root, setup warns instead of failing."""
    if _is_root():
        pytest.skip("running as root; cannot exercise the unprivileged warning")

    root = tmp_path / "station"

    rc, captured = main(["setup", "--root", str(root)]), capsys.readouterr()
    assert rc == 0
    for path in (root / "data", root / "config"):
        assert path.is_dir()
    assert "skipping" in captured.err.lower()
