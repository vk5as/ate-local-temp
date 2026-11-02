"""Ticket 23c session logic: History availability while a run is in progress (UI-free)."""

from __future__ import annotations

import pytest

from ate.config import Config
from ate.history.session import HistorySession
from ate.storage.store import RunStore


def _running_row() -> dict:
    """A run still executing: no result, no end_reason."""
    return {"run_uuid": "abc", "result": None, "end_reason": None}


def _finished_row(result="PASS", end_reason="DONE") -> dict:
    return {"run_uuid": "abc", "result": result, "end_reason": end_reason}


@pytest.fixture
def store(tmp_path):
    return RunStore(tmp_path / "ate.db")


def test_allow_during_run_defaults_true(store):
    """With no config the History screen is usable during a run."""
    s = HistorySession(store)
    assert s.allow_during_run() is True
    assert s.available() is True


def test_allow_during_run_from_config(tmp_path):
    """A Config with allowHistoryDuringRun=false disables availability."""
    cfg = Config(path=tmp_path / "ate.db", ui_allow_history_during_run=False)
    s = HistorySession(RunStore(tmp_path / "x.db"), config=cfg)
    assert s.allow_during_run() is False


def test_set_run_in_progress_tracks_state(store):
    """Toggling in-progress state alone does not block an allowed screen."""
    s = HistorySession(store)
    s.set_run_in_progress(True)
    assert s.available() is True
    s.set_run_in_progress(False)
    assert s.available() is True


def test_available_false_only_when_in_progress_and_disabled(tmp_path):
    """available() is False only when a run is in progress AND the flag disables it."""
    cfg = Config(path=tmp_path / "ate.db", ui_allow_history_during_run=False)
    s = HistorySession(RunStore(tmp_path / "x.db"), config=cfg)
    # not in progress -> available even though disabled
    assert s.available() is True
    # now a run is in progress and the flag disables it -> unavailable
    s.set_run_in_progress(True)
    assert s.available() is False


def test_available_true_when_in_progress_but_allowed(store):
    """A run in progress stays available when the flag permits it."""
    s = HistorySession(store)
    s.set_run_in_progress(True)
    assert s.available() is True


def test_is_running_detects_open_runs(store):
    """is_running is true for a row with no result and no end_reason, false once finished."""
    s = HistorySession(store)
    assert s.is_running(_running_row()) is True
    assert s.is_running(_finished_row()) is False
    # empty-string fields also read as "no result / no reason" (still executing)
    assert s.is_running({"run_uuid": "x", "result": "", "end_reason": ""}) is True
