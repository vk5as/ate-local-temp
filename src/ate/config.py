"""Minimal application-config reading for ticket 02.

Only what a one-step run needs: the Station identity and the database location from
the machine-wide JSON config, plus the Test Definition directories to scan. Full
config validation (every key, every section) is deferred; this reader extracts and
lightly checks the fields a run depends on so the rest of ticket 02 can build against
a stable shape.

The config is read once at launch and changes need a restart — see
``assets/app-config-format.md``. ``--config <path>`` overrides the file location;
without it ATE looks up the platform default (``/etc/ate/config.json`` on Linux).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ATEError


class ConfigError(ATEError):
    """Raised when the config file is missing or invalid."""


# Platform default config path (see assets/app-config-format.md). Overridden by
# ``--config`` on the command line.
_DEFAULT_PATHS = {
    "linux": "/etc/ate/config.json",
    "windows": r"%PROGRAMDATA%\ATE\config.json",
}


def _platform_default() -> str:
    import sys

    return _DEFAULT_PATHS.get(sys.platform, "/etc/ate/config.json")


@dataclass
class Config:
    """The subset of the application config a run needs."""

    path: Path  # resolved config file location (as given)
    station_name: str = ""
    station_uuid: str = ""
    database_type: str = "sqlite"
    database_path: Path = field(default_factory=lambda: Path("ate.db"))
    test_definition_dirs: list[Path] = field(default_factory=list)

    @property
    def data_dir(self) -> Path:
        """Directory the config paths are relative to (the config file's directory)."""
        return self.path.parent


def _resolve(path: Path, base: Path) -> Path:
    """Resolve ``path`` against ``base``; absolute paths win, else resolve relative."""
    if path.is_absolute():
        return path
    return (base / path).resolve()


def read_config(config_path: str | Path | None = None) -> Config:
    """Read and lightly validate the application config.

    Raises :class:`ConfigError` for a missing file, invalid JSON, an unknown format,
    or a value of the wrong type for a field we depend on. Paths are resolved against
    the config file's directory; absolute paths are used as given.
    """
    path = Path(config_path) if config_path is not None else Path(_platform_default())
    path = path.resolve()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in config file {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"config file {path} must be a JSON object")

    # format is required and must be 1.
    fmt = raw.get("format")
    if fmt != 1:
        raise ConfigError(f"unknown config format: {fmt!r}")

    station = raw.get("station", {})
    if not isinstance(station, dict):
        raise ConfigError("'station' must be an object")
    name = station.get("name", "")
    uuid = station.get("uuid", "")
    if not isinstance(name, str) or not isinstance(uuid, str):
        raise ConfigError("'station.name' and 'station.uuid' must be strings")

    database = raw.get("database", {})
    if not isinstance(database, dict):
        raise ConfigError("'database' must be an object")
    db_type = database.get("type", "sqlite")
    db_path_raw = database.get("path", Path("ate.db"))
    if db_type != "sqlite":
        raise ConfigError(f"unsupported database type: {db_type!r}")
    if not isinstance(db_path_raw, str):
        raise ConfigError("'database.path' must be a string")

    test_defs = raw.get("testDefinitions", {})
    dirs_raw = test_defs.get("directories", [])
    if not isinstance(dirs_raw, list) or not all(isinstance(d, str) for d in dirs_raw):
        raise ConfigError("'testDefinitions.directories' must be a list of strings")

    base = path.parent
    return Config(
        path=path,
        station_name=name,
        station_uuid=uuid,
        database_type=db_type,
        database_path=_resolve(Path(db_path_raw), base),
        test_definition_dirs=[_resolve(Path(d), base) for d in dirs_raw],
    )
