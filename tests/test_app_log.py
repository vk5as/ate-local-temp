"""Application log: ticket 04's one-file-per-launch logger.

These drive :class:`ate.log.AppLog` against a real directory on disk and check the
externally visible behaviour — that each launch gets its own file named by start time,
that entries below ``log.level`` are dropped before they reach it, and that ATE never
rotates or deletes its log for a launch. No Qt, no mocks: the logger is plain file I/O.
"""

import glob
import os
import re
from pathlib import Path

import pytest

from ate.log import AppLog, LogError, open_app_log
from ._fixtures import chmod_can_deny_access


def _basename(path: str) -> str:
    return Path(path).name


# --- one file per launch ----------------------------------------------------


def test_one_file_per_launch_named_by_start_time(tmp_path):
    """Each launch opens its own file; the name carries a start-time stamp."""
    first = AppLog(tmp_path, level="info", name="2026-01-01T08-00-00")
    second = AppLog(tmp_path, level="info", name="2026-06-15T19-30-45")

    assert first.path != second.path  # distinct launches -> distinct files
    assert re.fullmatch(r"ate-app-\d{4}-\d{2}-\d{2}[T ]\d{2}-\d{2}-\d{2}\.log$", _basename(first.path))
    assert re.fullmatch(r"ate-app-\d{4}-\d{2}-\d{2}[T ]\d{2}-\d{2}-\d{2}\.log$", _basename(second.path))

    first.log("info", "ate", "launch one")
    second.log("info", "ate", "launch two")
    first.close()
    second.close()

    files = sorted(glob.glob(str(tmp_path / "ate-app-*.log")))
    assert len(files) == 2  # neither overwrote the other


def test_open_app_log_helper_returns_a_logger(tmp_path):
    """The convenience helper opens a logger at the given level."""
    log = open_app_log(tmp_path, level="warning")
    try:
        assert isinstance(log, AppLog)
        assert log.level() == "warning"
    finally:
        log.close()


# --- level filtering --------------------------------------------------------


def test_entries_below_level_are_dropped(tmp_path):
    """An entry below ``log.level`` never reaches the file."""
    log = AppLog(tmp_path, level="error")  # floor at ERROR
    for lvl in ("debug", "info", "warning", "error"):
        log.log(lvl, "ate", f"{lvl}-message")
    log.close()

    lines = [ln.strip() for ln in log.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    assert lines[0].endswith("ERROR ate: error-message")


def test_entries_at_and_above_level_are_written_in_order(tmp_path):
    """Entries at or above ``log.level`` are written, in the order logged."""
    log = AppLog(tmp_path, level="info")  # default floor
    for lvl in ("debug", "info", "warning", "error"):
        log.log(lvl, "ate", f"{lvl}-message")
    log.close()

    levels = [ln.split()[1] for ln in log.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert levels == ["INFO", "WARNING", "ERROR"]  # debug dropped below the info floor


@pytest.mark.parametrize(
    "level,kept",
    [
        ("debug", ["debug", "info", "warning", "error"]),
        ("info", ["info", "warning", "error"]),
        ("warning", ["warning", "error"]),
        ("error", ["error"]),
    ],
)
def test_level_threshold_selects_which_entries(tmp_path, level, kept):
    """The configured level keeps only entries at or above it."""
    log = AppLog(tmp_path, level=level)
    for name in kept:
        log.log(name, "ate", f"{name}-msg")
    log.close()

    levels = [ln.split()[1] for ln in log.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert levels == [name.upper() for name in kept]


# --- never rotated or deleted -----------------------------------------------


def test_never_rotated_or_deleted_across_a_launch(tmp_path):
    """All entries accumulate in one file and survive close(); a new launch is separate."""
    log = AppLog(tmp_path, level="info", name="2026-01-01T08-00-00")
    for i in range(5):
        log.log("info", "ate", f"event {i}")
    original = log.path.read_text(encoding="utf-8")

    # Closing flushes and closes but must not delete the launch's own copy.
    log.close()
    assert Path(log.path).exists()
    assert Path(log.path).read_text(encoding="utf-8") == original  # untouched after close

    # A later launch in the same directory gets its own file, leaving this one intact.
    AppLog(tmp_path, level="info", name="2026-01-02T09-00-00").log("info", "ate", "later")
    assert Path(log.path).read_text(encoding="utf-8") == original  # still intact


def test_close_is_idempotent_and_keeps_the_file(tmp_path):
    """Closing twice does not raise and the file remains."""
    log = AppLog(tmp_path, level="info", name="2026-01-01T08-00-00")
    log.log("info", "ate", "one")
    log.close()
    log.close()  # must not raise on an already-closed handle
    assert Path(log.path).exists()


def test_context_manager_closes_and_keeps_the_file(tmp_path):
    """The context manager closes the logger without deleting its file."""
    with AppLog(tmp_path, level="info", name="2026-01-01T08-00-00") as log:
        log.log("info", "ate", "inside")
    assert Path(log.path).exists()


# --- failure paths ----------------------------------------------------------


def test_unwritable_directory_refuses_to_open(tmp_path):
    """A directory ATE cannot write to refuses to open the log (not a silent no-op)."""
    if not chmod_can_deny_access():
        pytest.skip("running as root, or on a platform where chmod is advisory")

    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o555)  # read + execute only, no write for the owner either
    with pytest.raises(LogError):
        AppLog(str(ro))


def test_a_log_directory_that_cannot_be_created_is_a_log_error(tmp_path):
    """Creating the log directory fails the same way: a :class:`LogError`, not the raw
    ``PermissionError`` the bare ``mkdir`` used to raise (packet 30c review). The launch layer
    catches :class:`LogError` and shows a dialog, so anything else is a traceback."""
    if not chmod_can_deny_access():
        pytest.skip("cannot make a directory unreadable for this process")

    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o555)
    with pytest.raises(LogError, match="cannot create the application log directory"):
        AppLog(ro / "logs")
    assert not (ro / "logs").exists()

