r"""Prepare a Station for every install format (``ate setup``, ticket 26).

One admin command prepares a Station regardless of how it was installed: the Windows installer,
``install.sh`` and the ``.deb``'s ``postinst`` all call it; wheel users run it themselves after
``pipx install`` (see ``assets/packaging.md``). It creates the data and config directories, gives
ordinary users modify rights on the database and its WAL files, ``reports`` and ``logs`` while
leaving ``config.json`` admin-only — on Linux by creating an ``ate`` group for that — writes a
default ``config.json`` with a fresh ``station.uuid`` only when none exists, and runs
``ate db migrate``.

The privileged filesystem operations — making directories, changing permissions and creating the
Linux group — need root, so they are routed through :class:`Fs`. Production uses the real system
calls; tests inject fakes (or neutralise them with monkeypatching) so a headless test can assert
exactly which paths were made writable and whether the group was created without needing those
privileges. The layout (:class:`StationLayout`) keeps the platform's directory choices in one
place, and :func:`prepare_station` is pure logic over that layout — the CLI layer decides where a
Station lives (the platform default or an explicit ``--root``) and runs ``ate db migrate`` on top.
"""

from __future__ import annotations

import grp
import os as _os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .errors import ATEError


class SetupError(ATEError):
    """Raised when ``ate setup`` cannot prepare the Station."""


@dataclass(frozen=True)
class StationLayout:
    r"""Where ATE keeps its machine-wide files on one platform.

    On Windows the config and data live together under ``%PROGRAMDATA%\ATE``; on Linux they are
    separate — ``/etc/ate`` for the admin-only config, ``/var/lib/ate`` for the database, reports
    and logs that ordinary users must modify (see ``assets/app-config-format.md``).
    """

    data_dir: Path  # %PROGRAMDATA%\ATE or /var/lib/ate
    config_dir: Path  # %PROGRAMDATA%\ATE (Windows) or /etc/ate (Linux)
    platform: str = ""  # "linux" or "windows"; defaults to the running platform when unset

    @staticmethod
    def from_root(root: Path) -> StationLayout:
        """A layout rooted at ``root`` for automation and tests (``data/`` and ``config/``)."""
        return StationLayout(data_dir=Path(root) / "data", config_dir=Path(root) / "config", platform=_current_platform())

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


def _current_platform() -> str:
    """This process's platform label, used to seed :attr:`StationLayout.platform`."""
    return "windows" if _os.name == "nt" else "linux"


def default_layout() -> StationLayout:
    """The platform's real directories. Never used by tests, which build their own layout."""
    if _os.name == "nt":
        base = (Path(_os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "ATE").resolve()
        return StationLayout(data_dir=base, config_dir=base)
    # Linux: a separate admin-only config directory and a user-writable data directory.
    return StationLayout(data_dir=Path("/var/lib/ate"), config_dir=Path("/etc/ate"))


@dataclass
class Fs:
    """The privileged filesystem operations ``prepare_station`` performs.

    Production defaults do the real work; tests replace them with fakes that record what was
    requested so a headless test can assert which directories were made writable and whether the
    platform group was created, without root privileges (creating a Linux group or chowning to
    another user needs them). The methods read the ``os`` module lazily in :meth:`__init__`, so a
    test that monkeypatches ``os.chmod``/``os.chown`` before an instance is built neutralises just
    those calls.
    """

    def __init__(self) -> None:
        self.make_dirs: Callable[[Path], None] = _os.makedirs
        self.chmod: Callable[[Path, int], None] = _os.chmod
        self.chown: Callable[[Path, tuple[int, int]], None] = _os.chown
        self.add_group: Callable[[str], bool] = _real_add_group


def _real_add_group(name: str) -> bool:
    """Create ``name`` if it does not exist; return whether it was newly created."""
    try:
        grp.getgrnam(name)
        return False  # already present
    except KeyError:
        _os.groupadd(name)
        return True


def _writable_targets(layout: StationLayout) -> list[tuple[Path, bool]]:
    """The directories ordinary users must modify and whether they need the group owner.

    The database (and its WAL), ``reports`` and ``logs`` are writable; ``config.json`` is left
    admin-only on every platform. On Windows the installer runs as an admin per-machine, so
    ordinary users already have access and no ownership or group change is made.
    """
    if layout.platform != "linux":
        return []
    return [(layout.data_dir, True), (layout.reports_dir, True), (layout.log_dir, True)]


def _linux_gid_for(group: str) -> int:
    """The numeric gid of ``group``, or 0 if it cannot be looked up."""
    try:
        return int(grp.getgrnam(group).gid)
    except (KeyError, PermissionError, OSError):
        return 0


def prepare_station(layout: StationLayout, fs: Optional[Fs] = None) -> dict[str, object]:
    """Create the directories and config for one Station; leave ``ate db migrate`` to the CLI.

    Creates the data and config directories (and their ``reports``/``logs`` subdirectories), then
    — on Linux — makes the database, reports and logs writable by ordinary users via a freshly
    created ``ate`` group while leaving ``config.json`` admin-only; writes a default
    ``config.json`` with a fresh ``station.uuid`` only when none exists (never overwriting one);
    and returns a record of what was done. It does not migrate the database — that is composed by
    the CLI layer, which runs it after :func:`prepare_station` succeeds.

    Returns a summary dict describing the directories created, the writable targets, whether the
    group was created, whether a default config was written and the resolved database path.
    """
    fs = fs or Fs()

    # 1. Create every directory ATE needs (parents included). Idempotent: an existing directory is
    # left untouched so re-running setup never errors on an already-prepared Station.
    created_dirs: list[str] = []
    for directory in (layout.data_dir, layout.config_dir, layout.reports_dir, layout.log_dir):
        if not directory.exists():
            fs.make_dirs(directory)
            created_dirs.append(str(directory))

    # 2. Ordinary users modify the database and its WAL files, reports and logs; config.json stays
    # admin-only. On Linux this needs an ``ate`` group; on Windows nothing is done here.
    writable: list[tuple[Path, bool]] = _writable_targets(layout)
    group_created = False
    if layout.platform == "linux" and writable:
        # Create the ate group once (real default creates it if missing; returns whether new).
        group_created = fs.add_group("ate")
        gid = _linux_gid_for("ate")  # re-looked up after creation, so a pre-existing group is honoured too
        for target_path, _needs_group in writable:
            fs.chmod(target_path, 0o775)  # owner rwx + group rwx so the ate group can modify everything inside
            fs.chown(target_path, 0, gid)  # owned by root:ate; adding Operators to the group is a manual step

    # 3. A default config with a fresh station.uuid only when none exists — never an overwrite.
    default_written = False
    if not layout.config_file.exists():
        data = build_default_config(_hostname())
        try:
            write_config(str(layout.config_file), data)
        except OSError as exc:
            raise SetupError(f"cannot write config to {layout.config_file}: {exc}") from exc
        default_written = True

    return {
        "data_dir": str(layout.data_dir),
        "config_dir": str(layout.config_dir),
        "database_path": str(layout.database_path),
        "created_dirs": created_dirs,
        "writable_targets": [(str(path), needs_group) for path, needs_group in writable],
        "group_created": group_created,
        "default_config_written": default_written,
    }


# The default config and its writer already live in :mod:`ate.config`; reuse them rather than
# reimplementing the schema here. Imported lazily so this module stays import-light.
from .config import build_default_config, write_config  # noqa: E402


def _hostname() -> str:
    """This machine's hostname, used to seed a fresh config's ``station.name``."""
    return socket.gethostname()


if __name__ == "__main__":  # pragma: no cover - manual smoke check only
    print(prepare_station(default_layout()))
