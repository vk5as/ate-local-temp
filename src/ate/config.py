r"""Application-config reading and validation for ATE (ticket 04).

ATE reads one machine-wide ``config.json`` at launch; changes need a restart. The
file lives at the platform default — ``%PROGRAMDATA%\ATE\config.json`` on Windows,
``/etc/ate/config.json`` on Linux — or wherever ``--config <path>`` points. This module
does the full validation that ticket 02's minimal reader deferred: every documented key
must be present with its default (missing keys are filled), an unknown key, a value of
the wrong type, an unknown ``format`` and any non-``sqlite`` database backend refuse
startup with an error naming the file and key. A missing file prompts to create defaults;
a generated ``station.uuid`` is written back into the file preserving key order.

The config is read once at launch (see ``assets/app-config-format.md``). Paths are
resolved against the config file's directory, absolute paths are used as given, forward
slashes work on Windows because they are never translated here, and there is no
environment-variable or ``~`` expansion.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .errors import ATEError
from .storage._uuid import new_uuid7


class ConfigError(ATEError):
    """Raised when the config file is missing, unreadable or invalid."""


class MissingConfigError(ConfigError):
    """Raised when the config file is absent; carries the expected path.

    The launch layer decides whether to prompt for a default (see :func:`read_config`'s
    ``on_missing_prompt`` callback) before refusing with this error.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        super().__init__(f"config file not found at {self.path}")


# The documented settings, grouped by section and keyed ``section.key`` (see
# ``assets/app-config-format.md``). A key's type is enforced; a missing key takes its
# default. ``format`` is required and must equal 1, so it is handled separately.
_SCHEMA: dict[str, dict[str, str]] = {
    "station": {"name": "str", "uuid": "str"},
    "testDefinitions": {"directories": "list_of_str"},
    "database": {"type": "str", "path": "str", "storeMismatchedFiles": "str"},
    "reports": {"directory": "str", "organisation": "str", "logo": "none", "paperSize": "str"},
    "log": {"directory": "str", "level": "str"},
    "devices": {"receiveBufferBytes": "int"},
    "deviceTrace": {"enabled": "bool", "maxBytes": "int"},
    "ui": {"theme": "str", "layout": "str", "allowLayoutSwitch": "bool", "allowHistoryDuringRun": "bool"},
}

# Defaults for every key above, keyed ``section.key``. Paths carry the ``<data>``
# placeholder the format documents; :func:`Config_from_resolved` expands it to the
# platform data directory (``%PROGRAMDATA%\ATE`` on Windows, ``/var/lib/ate`` on Linux)
# so a config stays portable, while ``ate setup`` writes the paths already resolved
# against the data directory it created (packet 30c).
_DEFAULTS: dict[str, Any] = {
    "station.name": "",
    "station.uuid": "",
    "testDefinitions.directories": ["<data>/definitions"],  # the format's documented default (30c)
    "database.type": "sqlite",
    "database.path": "<data>/ate.db",
    "database.storeMismatchedFiles": "ask",
    "reports.directory": "<data>/reports",
    "reports.organisation": "",
    "reports.logo": None,
    "reports.paperSize": "A4",
    "log.directory": "<data>/logs",
    "log.level": "info",
    "devices.receiveBufferBytes": 16_777_216,
    "deviceTrace.enabled": False,
    "deviceTrace.maxBytes": 256,
    "ui.theme": "system",
    "ui.layout": "staged",
    "ui.allowLayoutSwitch": True,
    "ui.allowHistoryDuringRun": True,
}

# The platform default config path (see ``assets/app-config-format.md``). Overridden by
# ``--config`` on the command line.
_DEFAULT_PATHS = {
    "linux": "/etc/ate/config.json",
    "windows": r"%PROGRAMDATA%\ATE\config.json",
}


def _platform_default() -> str:
    """The platform default config path; overridden by ``--config``."""
    import sys

    return _DEFAULT_PATHS.get(sys.platform, "/etc/ate/config.json")


# The only placeholder the config format defines (see ``assets/app-config-format.md``):
# ``<data>`` stands for the platform data directory from the Location table.
_DATA = "<data>"
_PLACEHOLDER_RE = re.compile(r"<[A-Za-z][A-Za-z0-9_]*>")


def _platform_data_dir() -> Path:
    r"""The Station's data directory: ``%PROGRAMDATA%\ATE`` on Windows, ``/var/lib/ate`` on
    Linux (``assets/app-config-format.md``); ``ate setup`` creates it (``assets/packaging.md``)."""
    import os
    import sys

    if sys.platform == "win32" or os.name == "nt":
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "ATE"
    return Path("/var/lib/ate")


def _expand_data(value: str, config_file: Path, key: str) -> str:
    """Replace ``<data>`` with the platform data directory; reject any other placeholder.

    An unknown placeholder (``<whatever>``) is a config error naming the file and key, so a typo
    never quietly becomes a directory literally called ``<whatever>`` (packet 30c)."""
    text = str(value)
    for token in _PLACEHOLDER_RE.findall(text):
        if token != _DATA:
            raise ConfigError(f"config file {config_file}: '{key}' has unknown placeholder {token!r}")
    return text.replace(_DATA, str(_platform_data_dir()))


# --- type predicates --------------------------------------------------------


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_int(value: Any) -> bool:
    # ``bool`` is a subclass of ``int``; a boolean is never a valid count/size.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _check_type(path: str, full: str, value: Any, expected: str) -> None:
    """Raise :class:`ConfigError` naming the file and ``full`` if ``value`` is wrong type."""
    predicate = {
        "str": _is_str,
        "int": _is_int,
        "bool": _is_bool,
        "none": lambda v: v is None,
        "list_of_str": lambda v: isinstance(v, list) and all(_is_str(x) for x in v),
    }[expected]
    if predicate(value):
        return  # the value's type matches; nothing to raise about

    kind = {
        "str": "string",
        "int": "integer",
        "bool": "boolean",
        "none": "null",
        "list_of_str": "list of strings",
    }[expected]
    raise ConfigError(f"config file {path}: '{full}' must be a {kind}, got {value!r}")


# --- defaults ---------------------------------------------------------------


def build_default_config(hostname: str = "", data_dir: str | Path | None = None) -> dict[str, Any]:
    """Return the default config as an ordered dict with a fresh ``station.uuid``.

    Key order follows ``assets/app-config-format.md``'s example so a freshly created
    file reads naturally. ``hostname`` seeds ``station.name`` (the computer's hostname).
    With ``data_dir`` the ``<data>`` placeholder is written out already resolved — that is
    what ``ate setup`` does, so a Station rooted at ``--root`` works wherever setup put it
    (packet 30c); without it the file keeps the documented ``<data>`` placeholders and the
    reader resolves them against the platform data directory.
    """
    data = str(Path(data_dir).resolve()) if data_dir is not None else _DATA
    return {
        "format": 1,
        "station": {"name": str(hostname), "uuid": new_uuid7()},
        "defaultProject": "",
        "testDefinitions": {"directories": [f"{data}/definitions"]},
        "database": {"type": "sqlite", "path": f"{data}/ate.db", "storeMismatchedFiles": "ask"},
        "reports": {"directory": f"{data}/reports", "organisation": "", "logo": None, "paperSize": "A4"},
        "log": {"directory": f"{data}/logs", "level": "info"},
        "devices": {"receiveBufferBytes": 16_777_216},
        "deviceTrace": {"enabled": False, "maxBytes": 256},
        "ui": {"theme": "system", "layout": "staged", "allowLayoutSwitch": True, "allowHistoryDuringRun": True},
    }


def write_config(path: str | Path, data: Mapping[str, Any]) -> None:
    """Write ``data`` to ``path`` as pretty JSON (key order preserved)."""
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _hostname() -> str:
    import socket

    return socket.gethostname()


# --- validation -------------------------------------------------------------


def _validate_and_fill(raw: Any, path: str, hostname: str = "") -> dict[str, Any]:
    """Validate ``raw`` against the schema and fill every missing key with its default.

    Returns a new ordered dict containing all documented keys (user values first, in
    their order; defaults appended for any that were absent). Raises :class:`ConfigError`
    naming the file and offending key on an unknown key, a wrong type, or a bad ``format``.
    A missing ``station.name`` takes ``hostname``; a non-``sqlite`` database backend is
    rejected (spec rule 24).
    """
    if not isinstance(raw, dict):
        raise ConfigError(f"config file {path} must be a JSON object")

    allowed_top = {"format", "defaultProject", *(_SCHEMA.keys())}
    for key in raw:
        if key not in allowed_top:
            raise ConfigError(f"unknown config key '{key}' in {path}")

    result: dict[str, Any] = dict(raw)  # preserve the user's key order and nesting

    # defaultProject is a documented top-level scalar (not grouped under a section).
    if "defaultProject" not in raw:
        result["defaultProject"] = ""

    # format is required and must be exactly 1.
    if "format" not in raw:
        raise ConfigError(f"config file {path}: 'format' is required (expected 1)")
    if raw["format"] != 1:
        raise ConfigError(f"config file {path}: unknown 'format' value {raw['format']!r} (expected 1)")

    for section, keys in _SCHEMA.items():
        section_obj = raw.get(section)
        if isinstance(section_obj, dict):
            # Present: validate each present key's type and fill any missing key.
            for dotted_key, expected in keys.items():
                full = f"{section}.{dotted_key}"
                if dotted_key not in section_obj:
                    result[section][dotted_key] = (
                        hostname if dotted_key == "name" else _DEFAULTS[full]
                    )  # station.name defaults to the hostname; others take their default
                else:
                    value = section_obj[dotted_key]
                    _check_type(path, full, value, expected)
        elif section_obj is None:
            # Absent section: every one of its keys is missing -> fill the defaults.
            for dotted_key, expected in keys.items():
                full = f"{section}.{dotted_key}"
                result.setdefault(section, {})[dotted_key] = (
                    hostname if dotted_key == "name" else _DEFAULTS[full]
                )
        else:
            raise ConfigError(f"'{section}' must be an object in {path}")

    # The receive buffer cap must be positive: 0 or less would disable or break the cap.
    devices = raw.get("devices")
    if isinstance(devices, dict) and devices.get("receiveBufferBytes", 1) <= 0:
        raise ConfigError(
            f"config file {path}: 'devices.receiveBufferBytes' must be a positive integer, "
            f"got {devices['receiveBufferBytes']!r}"
        )

    # Only sqlite is a supported backend in v1 (spec rule 24).
    database = raw.get("database")
    if isinstance(database, dict) and database.get("type") != "sqlite":
        raise ConfigError(f"config file {path}: 'database.type' must be 'sqlite', got {database['type']!r}")

    # Whether mismatched Lua bytes are stored with a run is one of three words (ticket 06c).
    if isinstance(database, dict) and database.get("storeMismatchedFiles", "ask") not in ("ask", "always", "never"):
        raise ConfigError(
            f"config file {path}: 'database.storeMismatchedFiles' must be 'ask', 'always' or 'never', "
            f"got {database.get('storeMismatchedFiles')!r}"
        )

    # The Run screen layout is one of the two shipped layouts (packet 25b); anything else is a typo.
    ui = raw.get("ui")
    if isinstance(ui, dict) and ui.get("layout", "staged") not in ("staged", "threePane"):
        raise ConfigError(
            f"config file {path}: 'ui.layout' must be 'staged' or 'threePane', got {ui.get('layout')!r}"
        )

    return result


def _resolve_path(value: str | Path, base: Path) -> Path:
    """Resolve ``value`` against ``base``; absolute paths are used as given.

    Relative paths resolve against the config file's directory. No environment-variable
    or ``~`` expansion happens (paths are used verbatim), and forward slashes work on
    Windows because they are never translated here.
    """
    if Path(value).is_absolute():
        return Path(value)
    return (base / value).resolve()


@dataclass
class Config:
    """The fully validated application config for one launch.

    Ticket 02's reader exposed only the fields a run needed; this expands to every
    documented setting so any later layer can read it directly. Paths are resolved to
    absolute :class:`~pathlib.Path` objects; ``data_dir`` is the config file's directory.
    """

    path: Path  # resolved config file location (as given)
    station_name: str = ""
    station_uuid: str = ""
    default_project: str = ""
    test_definition_dirs: list[Path] = field(default_factory=list)

    database_type: str = "sqlite"
    database_path: Path = field(default_factory=lambda: Path("ate.db"))
    store_mismatched_files: str = "ask"

    reports_directory: Path = field(default_factory=lambda: Path("reports"))
    reports_organisation: str = ""
    reports_logo: Optional[Path] = None
    reports_paper_size: str = "A4"

    log_directory: Path = field(default_factory=lambda: Path("logs"))
    log_level: str = "info"

    receive_buffer_bytes: int = 16_777_216
    device_trace_enabled: bool = False
    device_trace_max_bytes: int = 256

    ui_theme: str = "system"
    ui_layout: str = "staged"
    ui_allow_layout_switch: bool = True
    ui_allow_history_during_run: bool = True

    @property
    def data_dir(self) -> Path:
        """Directory the config paths are relative to (the config file's directory)."""
        return self.path.parent


def Config_from_resolved(config_file: Path, data: dict[str, Any]) -> Config:
    """Build a :class:`Config` from validated ``data``, resolving every path.

    ``<data>`` is expanded to the platform data directory first; remaining relative paths
    resolve against the config file's directory (``assets/app-config-format.md``)."""
    base = config_file.parent

    def _p(value: str | None, key: str) -> Optional[Path]:
        return _resolve_path(_expand_data(value, config_file, key), base) if value else None

    station = data.get("station") or {}
    database = data.get("database") or {}
    reports = data.get("reports") or {}
    log = data.get("log") or {}
    test_definitions = data.get("testDefinitions") or {}
    devices = data.get("devices") or {}
    device_trace = data.get("deviceTrace") or {}
    ui = data.get("ui") or {}

    return Config(
        path=config_file,
        station_name=station.get("name", ""),
        station_uuid=station.get("uuid", ""),
        default_project=data.get("defaultProject", ""),
        test_definition_dirs=[
            _resolve_path(_expand_data(d, config_file, "testDefinitions.directories"), base)
            for d in (test_definitions.get("directories") or [])
        ],
        database_type=database.get("type", "sqlite"),
        database_path=_p(database.get("path", _DATA + "/ate.db"), "database.path"),
        store_mismatched_files=database.get("storeMismatchedFiles", "ask"),
        reports_directory=_p(reports.get("directory", _DATA + "/reports"), "reports.directory"),
        reports_organisation=reports.get("organisation", ""),
        reports_logo=_p(reports.get("logo"), "reports.logo"),
        reports_paper_size=reports.get("paperSize", "A4"),
        log_directory=_p(log.get("directory", _DATA + "/logs"), "log.directory"),
        log_level=log.get("level", "info"),
        receive_buffer_bytes=devices.get("receiveBufferBytes", 16_777_216),
        device_trace_enabled=device_trace.get("enabled", False),
        device_trace_max_bytes=device_trace.get("maxBytes", 256),
        ui_theme=ui.get("theme", "system"),
        ui_layout=ui.get("layout", "staged"),
        ui_allow_layout_switch=ui.get("allowLayoutSwitch", True),
        ui_allow_history_during_run=ui.get("allowHistoryDuringRun", True),
    )


def read_config(config_path: str | Path | None = None, on_missing_prompt: Optional[Callable[[str], bool]] = None) -> Config:
    """Read and fully validate the application config for one launch.

    ``config_path`` overrides the platform default (the ``--config <path>`` location).
    A missing file raises :class:`MissingConfigError`; if ``on_missing_prompt`` is given,
    it is called with the expected path and returning truthy creates a default config
    (writing defaults plus a fresh ``station.uuid``) before loading. An invalid file —
    bad JSON, wrong type, unknown key or unknown ``format``, or a non-``sqlite`` database
    backend — raises :class:`ConfigError` naming the file and key. A missing ``station.uuid``
    is generated and written back preserving key order; if that write fails, startup is
    refused showing the generated UUID for an admin to add by hand.

    ``on_missing_prompt`` keeps this seam testable headless: the real launch layer wires
    its own dialog here (mirroring how :meth:`RunStore.close_interrupted_runs` takes an
    injectable logger).
    """
    raw_path = Path(config_path) if config_path is not None else _platform_default()
    config_file = raw_path.resolve()

    try:
        text = config_file.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        # Missing file: offer to create defaults (see ``assets/app-config-format.md``).
        if on_missing_prompt is not None and on_missing_prompt(str(config_file)):
            data = build_default_config(_hostname())  # fresh station.uuid + hostname name
            try:
                write_config(str(config_file), data)
            except OSError as exc:
                raise ConfigError(f"cannot write default config to {config_file}: {exc}") from exc
            return Config_from_resolved(config_file, data)
        raise MissingConfigError(str(config_file)) from exc
    except OSError as exc:  # unreadable (a directory, permissions, ...) -> refuse to start
        raise ConfigError(f"cannot read config file {config_file}: {exc}") from exc

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in config file {config_file}: {exc}") from exc

    data = _validate_and_fill(raw, str(config_file), hostname=_hostname())

    # A missing station.uuid is generated and written back into the existing file,
    # keeping every other key and its order. Never copy a uuid between Stations.
    if not (data.get("station") or {}).get("uuid"):
        new_uuid = new_uuid7()
        try:
            data.setdefault("station", {})["uuid"] = new_uuid  # preserves position within station
            write_config(str(config_file), data)
        except OSError as exc:
            raise ConfigError(
                f"cannot write station.uuid to {config_file}; add '{new_uuid}' by hand and restart"
            ) from exc

    return Config_from_resolved(config_file, data)
