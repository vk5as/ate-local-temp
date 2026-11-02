r"""Prepare a Station for every install format (``ate setup``, ticket 26a).

One admin command prepares a Station regardless of how it was installed: the Windows installer,
``install.sh`` and the ``.deb``'s ``postinst`` all call it; wheel users run it themselves after
``pipx install`` (see ``assets/packaging.md``). It creates the data directories (with their
``reports`` and ``logs`` subdirectories) and the config directory, writes a default
``config.json`` with a fresh ``station.uuid`` only when none exists — never overwriting one —
and runs the same migration ``ate db migrate`` performs against the database the config names --
its ``database.path``, or the data directory setup prepared when the config names none.

The privileged filesystem operations (creating directories, writing files) need root on some
platforms, so :func:`prepare_station` raises :class:`SetupError` with a message rather than failing
silently; the CLI turns that into a non-zero exit. The layout (:class:`StationLayout`) keeps each
platform's directory choices in one place and resolves them against an explicit ``--root`` for
automation and tests, while :func:`prepare_station` is pure logic over those two directories — the
CLI decides where a Station lives (the platform default or an explicit root) and prints what it did.

The database migration is the one routine ``ate db migrate`` also calls
(:func:`ate.storage.migrate_database`), so setup never creates or upgrades a database any
differently than the standalone command does — including the timestamped copy it takes before
a migration runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .errors import ATEError
from .permissions import ATE_GROUP, SKIP_PERMISSIONS, _current_platform, Permissions, real_permissions_for


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

    def _child(self, base: Path, *parts: str) -> Path:
        r"""A path under ``base``, joined with the separator this platform uses.

        ``Path / name`` uses the host's separator, so a Windows layout evaluated on a Linux
        host (the test suite) would read ``C:\ProgramData\ATE/reports`` — a path no directory
        on Windows matches."""
        sep = "\\" if self.platform == "windows" else "/"
        return Path(sep.join((str(base), *parts)))

    @property
    def database_path(self) -> Path:
        """The SQLite database file, created by ``ate db migrate``."""
        return self._child(self.data_dir, "ate.db")

    @property
    def reports_dir(self) -> Path:
        """Where Test Reports are written."""
        return self._child(self.data_dir, "reports")

    @property
    def log_dir(self) -> Path:
        """Where the application log is written."""
        return self._child(self.data_dir, "logs")

    @property
    def config_file(self) -> Path:
        """The machine-wide ``config.json`` (admin-only)."""
        return self._child(self.config_dir, "config.json")


def default_layout() -> StationLayout:
    r"""The platform's real directories. Overridden by ``--root`` for automation and tests.

    The platform test is ``ate.config._is_windows``, not ``os.name == "nt"`` alone: the config
    reader, the ``<data>`` placeholder and setup have to agree on ``%PROGRAMDATA%\ATE``, and
    ``sys.platform`` is the value a Windows machine actually reports (``win32``)."""
    from .config import _is_windows, _windows_data_dir

    if _is_windows():
        base = _windows_data_dir()
        return StationLayout(data_dir=base, config_dir=base, platform="windows")
    # Linux: a separate admin-only config directory and a user-writable data directory.
    return StationLayout(data_dir=Path("/var/lib/ate"), config_dir=Path("/etc/ate"), platform="linux")


def _station_database_path(config_file: Path, data_dir: Path) -> tuple[Path, str]:
    """Which database setup migrates, and why that one.

    The config is the Station's own statement of where its database is. An installer running
    ``ate setup`` on a Station that already exists must migrate the database that config names;
    migrating ``<data_dir>/ate.db`` instead would upgrade a file the Operator never opens and
    leave the real one at the old schema, which the next launch refuses (packet 35).

    A config that names no ``database.path`` is not a statement about where a database lives:
    the default is ``<data>/ate.db``, and ``<data>`` is the platform data directory, which is not
    the data directory setup was asked to prepare (``--root DIR``). In that case the Station's
    own data directory is used. A config that is *there* but cannot be read is different: silently
    falling back to ``<data>/ate.db`` migrates a database the Operator never opens and leaves the
    real one at the old schema, so it is refused in one line (packet 35 review). The missing file
    is not that case: a Station may not have a config yet, and an installer running ``ate setup``
    on a Station it owns cannot always read ``/etc/ate/config.json``. Either way the caller prints
    the chosen path and the reason, so an Administrator is never left guessing which file was
    touched."""
    config_file = Path(config_file)
    if not config_file.exists():
        return data_dir / "ate.db", f"no config at {config_file}"

    try:
        from .config import ConfigError, database_path_from_config

        database = database_path_from_config(config_file)
    except (OSError, ValueError, TypeError, ConfigError) as exc:
        # A file that is there but cannot be read at all — bad JSON, a bad encoding, a
        # ``{"database": 5}`` that is not an object. Reading it is the whole job here, so there
        # is no partial answer to fall back to: refuse, name the config, exit non-zero.
        raise SetupError(
            f"cannot read the config {config_file} ({exc}); fix it or remove it and run 'ate setup' again"
        ) from exc
    if database is None:
        return data_dir / "ate.db", f"{config_file} names no database.path"
    return database, f"database.path in {config_file}"


def _migrate_database(path: str) -> int:
    """Create or upgrade the database at ``path`` (non-interactive; shared by ``ate setup``).

    This is :func:`ate.storage.migrate_database` — the one routine every path that changes a
    database goes through, including ``ate db migrate``: a database built by a newer ATE is
    refused rather than downgraded, a copy is taken beside the file first when a migration will
    run, and the migration itself is :class:`RunStore` opening it. setup never migrates
    differently from the standalone command because it is the same call.
    """
    from .storage import migrate_database

    return migrate_database(path)


def prepare_station(
    data_dir: Path,
    config_dir: Path,
    *,
    permissions=None,
    config_file: Optional[Path] = None,
) -> dict[str, Any]:
    """Create the Station's directories and default config, then migrate its database.

    Creates ``data_dir`` with ``reports``, ``logs`` and ``definitions`` under it and ``config_dir``,
    writes a default ``config.json`` (with a fresh ``station.uuid``) only when none exists — never
    overwriting one — and runs the shared migration against the database the config names, or
    ``<data_dir>/ate.db`` when there is no config or it names no ``database.path``. A config that
    exists but cannot be read is refused instead (packet 35). It prints which database it migrated
    and why. Returns a report describing what was created versus already present; raises
    :class:`SetupError` if anything could not be created or written (for example, an unwritable
    target), if the config named by ``config_file`` cannot be read, if a backup could not be
    written, or if applying permissions failed.

    ``config_file`` is the file ``--config FILE`` names; without it the machine-wide
    ``<config_dir>/config.json`` is used, which is what every install format writes.

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
    config_file = Path(config_file) if config_file is not None else config_dir / "config.json"

    # 1. Create every directory ATE needs (parents included). Idempotent: an existing directory is
    # left untouched so re-running setup never errors on an already-prepared Station.
    created_dirs: list[str] = []
    for directory in (data_dir, data_dir / "reports", data_dir / "logs", data_dir / "definitions", config_dir, config_file.parent):
        if not directory.exists():
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise SetupError(f"cannot create {directory}: {exc}") from exc
            created_dirs.append(str(directory))

    # 2. A default config with a fresh station.uuid only when none exists — never an overwrite.
    default_written = False
    if not config_file.exists():
        try:
            from .config import build_default_config, write_config

            data = build_default_config(_hostname(), data_dir=data_dir)  # resolved paths, not <data> placeholders (30c)
            write_config(str(config_file), data)
            default_written = True
        except OSError as exc:
            raise SetupError(f"cannot write config to {config_file}: {exc}") from exc

    # 3. Create or upgrade the database. The config, if there is one, says which database the
    # Station uses; setup migrates that one and prints the choice and the reason (packet 35).
    db_path, why = _station_database_path(config_file, data_dir)
    if not db_path.parent.exists():
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SetupError(f"cannot create the directory for the database {db_path}: {exc}") from exc
    print(f"database to migrate: {db_path} ({why})")
    report = _migrate_database(str(db_path))
    if report != 0:
        # create_all runs table by table, so a migration that fails may have created some of
        # them: "nothing was changed" would be a lie. The copy taken before it is the way back
        # (packet 35 review).
        raise SetupError(
            f"database migration failed at {db_path}; some tables may already have been created, "
            f"and the copy taken beside it (if one was) is the way back. Run 'ate setup' again once "
            f"the problem it reported is fixed"
        )

    # 4. Apply the filesystem/permissions treatment now that everything exists — users can modify the
    # database, WAL files and reports/logs; config.json (and the config dir on Linux) stays admin-only.
    if permissions is None:
        perms = real_permissions_for(_current_platform())
    elif permissions is SKIP_PERMISSIONS:
        perms = None  # skip the privileged treatment entirely (unprivileged or --no-permissions)
    else:
        perms = permissions

    if perms is not None:
        # What ran is printed only when it ran: an unprivileged `ate setup`, or one given
        # --no-permissions, changes no group, mode or ACL and says nothing about a pass that
        # did not happen (packet 35 review, round 2).
        if db_path.parent != data_dir:
            who = "Users" if _current_platform() == "windows" else f"the {ATE_GROUP} group"
            print(f"the permission pass names the database {db_path} and the files written beside "
                  f"it; {db_path.parent} is not touched. SQLite writes its journal next to the "
                  f"database, so the Administrator has to make that directory group-writable for "
                  f"{who} before ordinary users can run a Test Run on it")
        else:
            print(f"no separate permission pass: the database is inside the data directory {data_dir}")
        try:
            _apply_permissions(perms, data_dir, config_dir, config_file, db_path)
        except OSError as exc:
            raise SetupError(f"cannot apply permissions to {data_dir}: {exc}") from exc

    return {
        "ok": True,
        "created_dirs": created_dirs,
        "default_config_written": default_written,
        "config_file": str(config_file),
        "database_path": str(db_path),
        "database_dir": str(db_path.parent),
    }


def _database_and_its_copies(db_path: Path) -> list[Path]:
    """The database and every file SQLite or a backup writes beside it, by exact name.

    The names are the ones the code and SQLite actually use: ``<name>`` itself, the
    ``-wal``/``-shm`` companions, the ``.backup-<stamp>`` copies (``-2``, ``-3`` when the name
    was taken), the ``.partial`` a copy is written under and the ``.partial-journal`` that copy
    journals through. ``<name>*`` caught all of those and also whatever else shared the
    database's name: another application's ``ate.db2.cfg`` beside a custom ``database.path``
    had its group changed by a pass that had no business touching it.

    The directory the database lives in is deliberately not on the list: ``database.path`` can
    point at a directory shared with other applications, and a permission pass is not licence
    to change the group of everything in it (packet 35 review, round 3). See
    :func:`_apply_permissions` for what setup says about that directory instead.
    """
    paths = [db_path]
    for suffix in ("-wal", "-shm"):
        companion = db_path.with_name(f"{db_path.name}{suffix}")
        if companion.exists():
            paths.append(companion)
    copies = re.compile(
        rf"^{re.escape(db_path.name)}\.backup-\d{{8}}-\d{{6}}(-\d+)?(\.partial(-journal)?)?$"
    )
    if db_path.parent.is_dir():
        paths.extend(p for p in sorted(db_path.parent.iterdir()) if copies.match(p.name))
    return paths


def _apply_permissions(perms: Permissions, data_dir: Path, config_dir: Path, config_file: Path, db_path: Path) -> None:
    """Grant ordinary users modify rights on the data tree and keep the config admin-only.

    Linux owns everything under ``data_dir`` to the group (recursively, with setgid so new files like
    ``ate.db-wal``/``-shm`` and fresh reports inherit it) and makes both ``config.json`` and
    ``config_dir`` root-owned; Windows grants Users Modify across ``data_dir`` and leaves
    ``config.json`` read-only for them.

    ``db_path`` is where the database actually lives. When ``database.path`` points outside
    ``data_dir`` only the database and the files written beside it get the pass, each by name and
    none of it recursively: the pass over ``data_dir`` cannot reach them, and the directory they
    are in is left alone because it may be shared — on a Station where ``/var/lib/ate/ate.db``
    is named, walking that parent would reach everything under ``/var`` (packet 35 review,
    round 3). The pass cannot make that directory setgid or give it the group, so files SQLite
    creates there later inherit nothing: setup prints that the Administrator has to make the
    directory itself writable, because SQLite writes its journal beside the database.
    """
    if _current_platform() == "windows":
        perms.grant_users_modify(data_dir)
        if db_path.parent != data_dir:
            for entry in _database_and_its_copies(db_path):
                perms.grant_users_modify(entry)
        perms.admin_only(config_file)
    else:  # linux — a separate data tree, with the config dir itself admin-only (dir and file)
        perms.ensure_group(ATE_GROUP)
        perms.set_group_writable(data_dir, ATE_GROUP, recursive=True)
        if db_path.parent != data_dir:
            for entry in _database_and_its_copies(db_path):
                perms.set_group_writable(entry, ATE_GROUP, recursive=False)
        perms.set_admin_only(config_file)
        perms.set_admin_only(config_dir)


# The default config and its writer already live in :mod:`ate.config`; reuse them rather than
# reimplementing the schema here. Imported lazily so this module stays import-light.
from .config import build_default_config, write_config  # noqa: E402


def _hostname() -> str:
    """This machine's hostname, used to seed a fresh config's ``station.name``."""
    import socket

    return socket.gethostname()
