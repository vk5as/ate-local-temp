r"""Prepare a Station for every install format (``ate setup``, ticket 26a).

One admin command prepares a Station regardless of how it was installed: the Windows installer,
``install.sh`` and the ``.deb``'s ``postinst`` all call it; wheel users run it themselves after
``pipx install`` (see ``assets/packaging.md``). It creates the data directories (with their
``reports`` and ``logs`` subdirectories) and the config directory, writes a default
``config.json`` with a fresh ``station.uuid`` only when none exists — never overwriting one —
and runs the same migration ``ate db migrate`` performs against the configured database path.

The privileged filesystem operations (creating directories, writing files) need root on some
platforms, so :func:`prepare_station` raises :class:`SetupError` with a message rather than failing
silently; the CLI turns that into a non-zero exit. The layout (:class:`StationLayout`) keeps each
platform's directory choices in one place and resolves them against an explicit ``--root`` for
automation and tests, while :func:`prepare_station` is pure logic over those two directories — the
CLI decides where a Station lives (the platform default or an explicit root) and prints what it did.

The database migration reuses the exact code path ``ate db migrate`` uses (:class:`RunStore`,
:class:`check_database_at_launch`) so setup never creates or upgrades a database any differently than
the standalone command does.
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .errors import ATEError
from .permissions import ATE_GROUP, SKIP_PERMISSIONS, _current_platform, Permissions, real_permissions_for
from .storage import DbLaunchStatus, RunStore, check_database_at_launch


class SetupError(ATEError):
    """Raised when ``ate setup`` cannot prepare the Station (e.g. a directory is not writable)."""


@dataclass(frozen=True)
class StationLayout:
    r"""Where ATE keeps its machine-wide files on one platform.

    On Windows the config and data live together under ``%PROGRAMDATA%\ATE``; on Linux they are
    separate — ``/etc/ate`` for the admin-only config, ``/var/lib/ate`` for the database, reports
    and logs (see ``assets/app-config-format.md``).
    """

    data_dir: Path  # %PROGRAMDATA%\ATE or /var/lib/ate
    config_dir: Path  # %PROGRAMDATA%\ATE (Windows) or /etc/ate (Linux)
    platform: str = ""  # "linux" or "windows"; defaults to the running platform when unset

    @staticmethod
    def from_root(root: Optional[Path]) -> StationLayout:
        """A layout rooted at ``root`` for automation and tests (``data/`` and ``config/``)."""
        root = Path(root) if root is not None else Path(".")
        return StationLayout(data_dir=root / "data", config_dir=root / "config", platform=_current_platform())

    @property
    def database_path(self) -> Path:
        """The SQLite database file, created by ``ate db migrate``."""
        return self.data_dir / "ate.db"

    @property
    def reports_dir(self) -> Path:
        """Where Test Reports are written."""
        return self.data_dir / "reports"

    @property
    def log_dir(self) -> Path:
        """Where the application log is written."""
        return self.data_dir / "logs"

    @property
    def config_file(self) -> Path:
        """The machine-wide ``config.json`` (admin-only)."""
        return self.config_dir / "config.json"


def default_layout() -> StationLayout:
    """The platform's real directories. Overridden by ``--root`` for automation and tests."""
    import os

    if os.name == "nt":
        base = (Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "ATE").resolve()
        return StationLayout(data_dir=base, config_dir=base)
    # Linux: a separate admin-only config directory and a user-writable data directory.
    return StationLayout(data_dir=Path("/var/lib/ate"), config_dir=Path("/etc/ate"))


def _migrate_database(path: str) -> int:
    """Create or upgrade the database at ``path`` (non-interactive; shared by ``ate setup``).

    A database built by a newer ATE is refused rather than downgraded; everything else — a missing
    or older database — is opened with :class:`RunStore`, which creates the tables if missing and
    sets the schema version. Mirrors ``ate db migrate --yes`` exactly so setup never migrates any
    differently than the standalone command does.
    """
    from .storage import DbLaunchStatus, RunStore, check_database_at_launch

    result = check_database_at_launch(path)
    if result.status == DbLaunchStatus.TOO_OLD:
        print(result.message, file=sys.stderr)
        return 1

    store = RunStore(path)  # creates the tables if missing and sets the schema version
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()
    print(f"created or upgraded database at {path} (schema version {version})")
    return 0


def prepare_station(data_dir: Path, config_dir: Path, *, permissions=None) -> dict[str, Any]:
    """Create the Station's directories and default config, then migrate its database.

    Creates ``data_dir`` with ``reports``, ``logs`` and ``definitions`` under it and ``config_dir``,
    writes a default ``config.json`` (with a fresh ``station.uuid``) only when none exists — never
    overwriting one — and runs the shared migration against ``<data_dir>/ate.db``. Returns a report
    describing what was created versus already present; raises :class:`SetupError` if anything could
    not be created or written (for example, an unwritable target) or if applying permissions failed.

    ``definitions`` is created because the default config lists ``<data>/definitions`` as a Test
    Definition directory, and a directory that does not exist is a warning on the Run screen at
    every scan (packet 30c review): setup leaves the Station quiet until a definition is dropped in.

    After creating the directories and config, :func:`prepare_station` applies the filesystem/
    permissions treatment through ``permissions`` (:mod:`ate.permissions`). ``None`` uses the real
    implementation for this platform; :data:`SKIP_PERMISSIONS` skips it entirely (unprivileged runs);
    anything else is a :class:`Permissions` instance whose calls are recorded by tests.
    """
    data_dir = Path(data_dir)
    config_dir = Path(config_dir)

    # 1. Create every directory ATE needs (parents included). Idempotent: an existing directory is
    # left untouched so re-running setup never errors on an already-prepared Station.
    created_dirs: list[str] = []
    for directory in (data_dir, data_dir / "reports", data_dir / "logs", data_dir / "definitions", config_dir):
        if not directory.exists():
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise SetupError(f"cannot create {directory}: {exc}") from exc
            created_dirs.append(str(directory))

    # 2. A default config with a fresh station.uuid only when none exists — never an overwrite.
    default_written = False
    if not (config_dir / "config.json").exists():
        try:
            from .config import build_default_config, write_config

            data = build_default_config(_hostname(), data_dir=data_dir)  # resolved paths, not <data> placeholders (30c)
            write_config(str(config_dir / "config.json"), data)
            default_written = True
        except OSError as exc:
            raise SetupError(f"cannot write config to {config_dir / 'config.json'}: {exc}") from exc

    # 3. Create or upgrade the database (the same migration ``ate db migrate`` performs).
    report = _migrate_database(str(data_dir / "ate.db"))
    if report != 0:
        raise SetupError(f"database migration failed at {data_dir / 'ate.db'}")

    # 4. Apply the filesystem/permissions treatment now that everything exists — users can modify the
    # database, WAL files and reports/logs; config.json (and the config dir on Linux) stays admin-only.
    if permissions is None:
        perms = real_permissions_for(_current_platform())
    elif permissions is SKIP_PERMISSIONS:
        perms = None  # skip the privileged treatment entirely (unprivileged or --no-permissions)
    else:
        perms = permissions

    if perms is not None:
        try:
            _apply_permissions(perms, data_dir, config_dir)
        except OSError as exc:
            raise SetupError(f"cannot apply permissions to {data_dir}: {exc}") from exc

    return {
        "ok": True,
        "created_dirs": created_dirs,
        "default_config_written": default_written,
        "config_file": str(config_dir / "config.json"),
        "database_path": str(data_dir / "ate.db"),
    }


def _apply_permissions(perms: Permissions, data_dir: Path, config_dir: Path) -> None:
    """Grant ordinary users modify rights on the data tree and keep the config admin-only.

    Linux owns everything under ``data_dir`` to the group (recursively, with setgid so new files like
    ``ate.db-wal``/``-shm`` and fresh reports inherit it) and makes both ``config.json`` and
    ``config_dir`` root-owned; Windows grants Users Modify across ``data_dir`` and leaves
    ``config.json`` read-only for them.
    """
    if _current_platform() == "windows":
        perms.grant_users_modify(data_dir)
        perms.admin_only(config_dir / "config.json")
    else:  # linux — a separate data tree, with the config dir itself admin-only (dir and file)
        perms.ensure_group(ATE_GROUP)
        perms.set_group_writable(data_dir, ATE_GROUP, recursive=True)
        perms.set_admin_only(config_dir / "config.json")
        perms.set_admin_only(config_dir)


# The default config and its writer already live in :mod:`ate.config`; reuse them rather than
# reimplementing the schema here. Imported lazily so this module stays import-light.
from .config import build_default_config, write_config  # noqa: E402


def _hostname() -> str:
    """This machine's hostname, used to seed a fresh config's ``station.name``."""
    import socket

    return socket.gethostname()
