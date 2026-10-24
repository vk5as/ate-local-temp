"""Ticket 23c seam test: the History screen while a run is in progress (both flag settings).

A run still executing has no result and no end_reason, so it is inserted straight into the store as a
fresh ``test_run`` row (never closed through ``update_run_end``) — that is what makes the session read it
as running. The screen then either keeps working with such a row shown as "running" (allowHistoryDuringRun
true), or hides itself behind "A Test Run is in progress" and restores on completion (false)."""

from __future__ import annotations

import pytest

from ate.config import Config
from ate.history.session import HistorySession
from ate.storage.store import RunStore
from ate.ui.history_screen import HistoryScreen


def _running_row(store: RunStore, uut_type="PowerSupply", serial="PS-running") -> str:
    """Insert an open run (no result / end_reason) and return its run_uuid."""
    data = {
        "run_uuid": None,  # the store generates one
        "uut_type": uut_type,
        "uut_serial": serial,
        "station_name": "BENCH-03",
    }
    return store.insert_test_run(data)["run_uuid"]


def test_running_row_shows_running_and_disables_report_actions(qapp, tmp_path):
    """With allowHistoryDuringRun true a running row shows 'running' and report actions are disabled."""
    store = RunStore(tmp_path / "ate.db")
    _running_row(store)  # one open run -> one data row

    s = HistorySession(store)
    s.set_run_in_progress(True)  # a run is in progress, but the flag still allows browsing
    screen = HistoryScreen(s)
    model = screen.table_view.model()

    # The table still works: body data starts at model row 0 (the real header row is not a data row).
    assert model.rowCount() == 1
    # Result column (index 3 of HISTORY_COLUMNS) shows "running" for the open run.
    assert model.item(0, 3).text() == "running"

    # Selecting it disables Open report and Regenerate but leaves the row selectable.
    screen.table_view.setCurrentIndex(model.index(0, 0))
    qapp.processEvents()
    assert screen.open_report_button.isEnabled() is False
    assert screen.regenerate_button.isEnabled() is False


def test_history_blocked_while_run_in_progress(qapp, tmp_path):
    """With allowHistoryDuringRun false a run in progress hides History; it restores when finished."""
    store = RunStore(tmp_path / "ate.db")
    _running_row(store)

    cfg = Config(path=tmp_path / "ate.db", ui_allow_history_during_run=False)
    s = HistorySession(store, config=cfg)
    screen = HistoryScreen(s)

    # Available by default: the table is enabled and no banner shows. (This offscreen build reports a
    # child's isVisible() from its shown ancestor rather than its own hidden flag, so the banner is checked
    # on its text; the table/actions are checked on isEnabled().)
    assert screen.table_view.isEnabled() is True
    assert "A Test Run is in progress" not in screen._status_label.text()

    # A run starts and the flag disables History during it.
    screen.set_run_in_progress(True)
    qapp.processEvents()
    assert "A Test Run is in progress" in screen._status_label.text()
    assert screen.table_view.isEnabled() is False

    # The run finishes: History comes back.
    screen.set_run_in_progress(False)
    qapp.processEvents()
    assert "A Test Run is in progress" not in screen._status_label.text()
    assert screen.table_view.isEnabled() is True
