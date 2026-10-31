"""Application-config validation seam tests: ticket 04 expands the ticket 02 reader.

These drive :func:`ate.config.read_config` through a real JSON file on disk and check
the externally visible outcomes — what it accepts, what it refuses (and with which
message), and how it fills defaults, resolves paths and writes back ``station.uuid``.
No mocks: each test builds a temp config and reads the result back out of it.
"""

import json
import os
import socket
import sys
from pathlib import Path

import pytest

from ate import config
from ate.config import (
    Config,
    ConfigError,
    MissingConfigError,
    build_default_config,
    read_config,
)

# conftest.py replaces `_platform_data_dir` for the session so `<data>` lands under pytest's
# basetemp instead of `/var/lib/ate`. Collection happens before that fixture runs, so this is
# the real platform function the Windows tests below need.
_real_platform_data_dir = config._platform_data_dir


# --- missing file -----------------------------------------------------------


def test_missing_file_raises_naming_the_expected_path(tmp_path):
    """A missing config names the platform default path and refuses to start."""
    with pytest.raises(MissingConfigError) as exc:
        read_config(str(tmp_path / "does-not-exist.json"))
    assert str(tmp_path / "does-not-exist.json") in str(exc.value)


def test_missing_file_create_writes_defaults_and_uuid(tmp_path):
    """Choosing to create writes defaults plus a fresh station.uuid and returns a Config."""
    config_path = tmp_path / "config.json"  # parent exists; file does not

    def _create(_path: str) -> bool:
        return True

    cfg = read_config(str(config_path), on_missing_prompt=_create)

    assert config_path.exists()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    # Every documented key is present with its default.
    assert data["format"] == 1
    assert "station" in data and "uuid" in data["station"]
    assert cfg.station_uuid  # a fresh UUIDv7 was generated
    assert cfg.station_name == socket.gethostname()  # name defaulted to the hostname

    # The written file is itself valid: reloading it needs no prompt.
    reloaded = read_config(str(config_path))
    assert reloaded.station_uuid == cfg.station_uuid


def test_missing_file_abort_refuses_to_start(tmp_path):
    """Choosing Abort refuses startup with the same MissingConfigError."""
    config_path = tmp_path / "config.json"

    def _abort(_path: str) -> bool:
        return False

    with pytest.raises(MissingConfigError):
        read_config(str(config_path), on_missing_prompt=_abort)
    assert not config_path.exists()  # nothing was written


def test_create_defaults_when_file_is_unwritable_refuses_showing_path(tmp_path):
    """If the default file cannot be written, startup is refused showing the path."""
    if os.name != "posix" or os.geteuid() == 0:  # no euid on Windows; root ignores the mode bits
        pytest.skip("running as root, or on a platform where chmod is advisory")

    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o555)  # read + execute only, no write for the owner either
    config_path = str(ro / "config.json")  # does not exist yet -> triggers the create prompt

    def _create(_path: str) -> bool:
        return True

    with pytest.raises(ConfigError) as exc:
        read_config(config_path, on_missing_prompt=_create)
    assert config_path in str(exc.value)


# --- invalid file -----------------------------------------------------------


def test_invalid_json_names_the_file(tmp_path):
    """Invalid JSON refuses startup, naming the file."""
    bad = tmp_path / "config.json"
    bad.write_text("{ this is not json ", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        read_config(str(bad))
    assert str(bad) in str(exc.value)


def test_not_an_object_names_the_file(tmp_path):
    """A JSON array (not an object) refuses startup, naming the file."""
    bad = tmp_path / "config.json"
    bad.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        read_config(str(bad))
    assert str(bad) in str(exc.value)


def test_wrong_type_names_the_file_and_key(tmp_path):
    """A value of the wrong type refuses startup, naming the file and key."""
    bad = tmp_path / "config.json"
    bad.write_text(json.dumps({"format": 1, "ui": {"theme": 42}}), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        read_config(str(bad))
    assert str(bad) in str(exc.value)
    assert "'ui.theme'" in str(exc.value)


def test_ui_layout_is_read_as_written(tmp_path):
    """`ui.layout` is either `staged` or `threePane`, and either is accepted as given (packet 25b)."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"format": 1, "ui": {"layout": "threePane"}}), encoding="utf-8")
    assert read_config(str(cfg)).ui_layout == "threePane"
    cfg.write_text(json.dumps({"format": 1, "ui": {"layout": "staged"}}), encoding="utf-8")
    assert read_config(str(cfg)).ui_layout == "staged"


def test_unknown_ui_layout_is_rejected(tmp_path):
    """A layout word that is not `staged` or `threePane` refuses startup, naming the key."""
    bad = tmp_path / "config.json"
    bad.write_text(json.dumps({"format": 1, "ui": {"layout": "panes"}}), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        read_config(str(bad))
    assert "'ui.layout'" in str(exc.value)
    assert "staged" in str(exc.value) and "threePane" in str(exc.value)


def test_unknown_key_names_the_file_and_key(tmp_path):
    """An unknown key refuses startup, naming the file and key."""
    bad = tmp_path / "config.json"
    bad.write_text(json.dumps({"format": 1, "station": {"name": "X", "uuid": "u"}, "bogus": True}), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        read_config(str(bad))
    assert str(bad) in str(exc.value)
    assert "'bogus'" in str(exc.value)


def test_unknown_format_names_the_key(tmp_path):
    """An unknown format refuses startup, naming the 'format' key."""
    bad = tmp_path / "config.json"
    bad.write_text(json.dumps({"format": 2}), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        read_config(str(bad))
    assert "'format'" in str(exc.value)


def test_database_type_other_than_sqlite_is_rejected(tmp_path):
    """A non-sqlite database backend is rejected, naming the key."""
    bad = tmp_path / "config.json"
    bad.write_text(json.dumps({"format": 1, "database": {"type": "postgres", "path": "x.db"}}), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        read_config(str(bad))
    assert "'database.type'" in str(exc.value)


def test_store_mismatched_files_must_be_ask_always_or_never(tmp_path):
    """`database.storeMismatchedFiles` accepts only the three documented words (ticket 06c)."""
    bad = tmp_path / "config.json"
    bad.write_text(json.dumps({"format": 1, "database": {"type": "sqlite", "path": "x.db", "storeMismatchedFiles": "sometimes"}}), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        read_config(str(bad))
    assert "'database.storeMismatchedFiles'" in str(exc.value)

    for word in ("ask", "always", "never"):
        good = tmp_path / f"config-{word}.json"
        good.write_text(json.dumps({"format": 1, "database": {"type": "sqlite", "path": "x.db", "storeMismatchedFiles": word}}), encoding="utf-8")
        assert read_config(str(good)).store_mismatched_files == word


# --- station identity -------------------------------------------------------


def test_station_name_defaults_to_hostname_when_missing(tmp_path):
    """A config without station.name uses the computer's hostname."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"format": 1, "station": {"uuid": "u"}}), encoding="utf-8")
    assert read_config(str(cfg)).station_name == socket.gethostname()


def test_missing_uuid_is_generated_and_written_back_preserving_order(tmp_path):
    """A missing station.uuid is generated and written back keeping key order."""
    cfg = tmp_path / "config.json"
    # A deliberately ordered file: name before uuid, with other sections after.
    cfg.write_text(
        json.dumps({
            "format": 1,
            "station": {"name": "BENCH-03"},   # no uuid here
            "defaultProject": "",
            "ui": {"theme": "dark"},
        }),
        encoding="utf-8",
    )

    loaded = read_config(str(cfg))
    assert loaded.station_uuid  # generated

    written = json.loads(cfg.read_text(encoding="utf-8"))
    # Key order preserved: name still first, uuid appended right after it.
    assert list(written["station"].keys()) == ["name", "uuid"]
    assert list(written.keys())[0] == "format"


def test_uuid_is_not_written_back_when_present(tmp_path):
    """An existing station.uuid is left untouched (never copied between Stations)."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"format": 1, "station": {"name": "X", "uuid": "keep-me"}}), encoding="utf-8")
    read_config(str(cfg))
    assert json.loads(cfg.read_text(encoding="utf-8"))["station"]["uuid"] == "keep-me"


# --- paths ------------------------------------------------------------------


def test_relative_path_resolves_against_config_dir(tmp_path):
    """A relative path resolves against the config file's directory."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"format": 1, "database": {"type": "sqlite", "path": "ate.db"}}), encoding="utf-8")
    assert read_config(str(cfg)).database_path == (tmp_path / "ate.db").resolve()


def test_absolute_path_is_used_as_given(tmp_path):
    """An absolute path is used as given, not resolved against the config dir."""
    cfg = tmp_path / "config.json"
    target = tmp_path / "elsewhere" / "db.sqlite"
    cfg.write_text(json.dumps({"format": 1, "database": {"type": "sqlite", "path": str(target)}}), encoding="utf-8")
    assert read_config(str(cfg)).database_path == Path(target)


def test_no_env_or_tilde_expansion(tmp_path):
    """Paths are never expanded for environment variables or ~."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"format": 1, "log": {"directory": "~/logs", "level": "info"}}), encoding="utf-8")
    resolved = read_config(str(cfg)).log_directory
    assert "~" in str(resolved)  # not turned into the home directory


def test_missing_test_definition_dir_is_not_a_startup_failure(tmp_path):
    """A configured Test Definition directory that does not exist still loads."""
    cfg = tmp_path / "config.json"
    defs = tmp_path / "definitions"  # never created
    cfg.write_text(json.dumps({"format": 1, "testDefinitions": {"directories": [str(defs)]}}), encoding="utf-8")
    loaded = read_config(str(cfg))
    assert loaded.test_definition_dirs == [defs.resolve()]


# --- every documented key exists with its default ---------------------------


def test_every_documented_key_has_its_default(tmp_path):
    """A config that omits the optional sections still yields a fully populated Config."""
    cfg = tmp_path / "config.json"
    # Only format + station present; everything else is missing.
    cfg.write_text(json.dumps({"format": 1, "station": {"name": "X", "uuid": "u"}}), encoding="utf-8")

    loaded = read_config(str(cfg))
    assert isinstance(loaded, Config)
    # Paths default to the data directory; scalars take their documented defaults.
    assert loaded.database_type == "sqlite"
    assert loaded.store_mismatched_files == "ask"
    assert loaded.reports_paper_size == "A4"
    assert loaded.log_level == "info"
    assert loaded.receive_buffer_bytes == 16_777_216
    assert loaded.device_trace_enabled is False
    assert loaded.ui_layout == "staged"
    # data_dir derives from the config file's directory.
    assert loaded.data_dir == cfg.parent


def test_custom_config_path_overrides_the_default(tmp_path):
    """--config <path> overrides the platform default location."""
    cfg = tmp_path / "custom.json"
    cfg.write_text(json.dumps({"format": 1, "station": {"name": "X", "uuid": "u"}}), encoding="utf-8")
    loaded = read_config(cfg)
    assert loaded.path == cfg.resolve()


def _pretend_windows(monkeypatch, programdata: str | None = r"C:\ProgramData") -> None:
    """Make this Linux process look like a Windows one: `sys.platform` is `win32` on every
    real Windows, and `PROGRAMDATA` is set there. Only the platform branches change.

    `conftest.py` redirects `_platform_data_dir` to pytest's basetemp for the session so no
    test writes under `/var/lib`; the real function is captured at import time, before that
    fixture ran, and put back here."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(config, "_platform_data_dir", _real_platform_data_dir)
    if programdata is None:
        monkeypatch.delenv("PROGRAMDATA", raising=False)
    else:
        monkeypatch.setenv("PROGRAMDATA", programdata)


def test_the_windows_default_config_is_programdata_not_etc(monkeypatch):
    r"""The platform default config on Windows is `%PROGRAMDATA%\ATE\config.json`.

    `sys.platform` is `win32`, never `windows`, so a table keyed on the doc's label falls
    through to `/etc/ate/config.json`: bare `ate` and `ate db status` would look in
    `C:\etc\ate\` while `ate setup` wrote in ProgramData (packet 27c review)."""
    _pretend_windows(monkeypatch)

    assert str(config._platform_default()) == r"C:\ProgramData\ATE\config.json"
    # A bare `ate db status` on Linux still reads /etc/ate, and nothing writes to ProgramData.
    monkeypatch.undo()
    assert str(config._platform_default()) == "/etc/ate/config.json"


def test_programdata_is_expanded_and_falls_back_when_unset(monkeypatch):
    """`%PROGRAMDATA%` is expanded; a stripped environment still gives the documented path."""
    _pretend_windows(monkeypatch, programdata=r"D:\StationData")
    assert str(config._windows_data_dir()) == r"D:\StationData\ATE"
    # A Windows env value can itself hold `%VAR%`: REG_EXPAND_SZ is stored unexpanded.
    monkeypatch.setenv("PROGRAMDATA", r"%USERPROFILE%\AppData")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Operator")
    assert str(config._windows_data_dir()) == r"C:\Users\Operator\AppData\ATE"
    _pretend_windows(monkeypatch, programdata=None)
    assert str(config._windows_data_dir()) == r"C:\ProgramData\ATE"
    # Not Windows after the monkeypatch is undone: the Linux default is untouched, and nothing
    # on a Linux machine points at ProgramData.
    monkeypatch.undo()
    assert not config._is_windows()
    assert str(config._platform_default()) == "/etc/ate/config.json"


def test_config_default_data_dir_and_ate_setup_all_agree_on_windows(monkeypatch):
    r"""`ate`, `ate db status` and `ate setup` must point at the same %PROGRAMDATA%\ATE.

    Three places decide the location (the config default, the `<data>` placeholder and
    setup's layout); if one of them keys on a different platform test, the Station is
    prepared in one directory and read from another."""
    _pretend_windows(monkeypatch)
    from ate.setup import default_layout

    data = config._platform_data_dir()
    layout = default_layout()
    assert str(data) == r"C:\ProgramData\ATE"
    assert layout.data_dir == data, "setup prepares a different directory than the config expects"
    assert layout.config_dir == data, "setup writes config.json where nothing reads it"
    assert layout.config_file == config._platform_default()
    assert str(layout.config_file) == r"C:\ProgramData\ATE\config.json"
    assert str(layout.database_path) == r"C:\ProgramData\ATE\ate.db"
    assert layout.platform == "windows"
    assert config._expand_data("<data>/ate.db", Path("config.json"), "database.path").replace("\\", "/") == "C:/ProgramData/ATE/ate.db"


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_receive_buffer_bytes_is_rejected(tmp_path, value):
    """devices.receiveBufferBytes caps the receive buffer, so it must be positive: 0 or less
    refuses startup, naming the file and key (0 must not silently disable the cap)."""
    bad = tmp_path / "config.json"
    bad.write_text(json.dumps({"format": 1, "devices": {"receiveBufferBytes": value}}), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        read_config(str(bad))
    assert str(bad) in str(exc.value)
    assert "'devices.receiveBufferBytes'" in str(exc.value)
