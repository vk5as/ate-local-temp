"""Ticket 23b's seam-2 smoke test: the flat History screen bound to a real session + store.

It builds :class:`HistoryScreen` on top of a live :class:`HistorySession` whose rows come from runs
recorded through RunEngine, then drives the bindings — rendering, row→uuid mapping, "Load more" paging,
filter inputs, header sort and the action buttons — offscreen. No run logic here; every action is a call
into an existing interface (session methods), and "Open report" opens through an injected ``open_file`` so
tests never touch the platform.

Selection: on this Qt 6.11 / PyQt6 build ``QTableView.setCurrentIndex(idx)`` followed by
``qapp.processEvents()`` reliably registers a row selection offscreen (verified against the real widget),
so the action handlers that read ``selectedRunUuid()`` are exercised through genuine selections rather
than a hand-filled map. Columns are addressed by their fixed index in :data:`HISTORY_COLUMNS` — column 0
is ``started_at``, column 1 is ``uut_type``, column 2 is ``uut_serial`` — not by the header text, which
this build does not expose via ``horizontalHeaderItem``. "Load more" hides itself when no page remains; it
never toggles its enabled state, so paging is asserted on visibility, not ``isEnabled()``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ate.config import Config
from ate.engine.run_engine import RunEngine
from ate.history.session import HistorySession
from ate.storage.store import RunStore
from ate.ui.history_screen import HistoryScreen

from ._fixtures import DOC_FIELDS, _drive

# Fixed column indices into :data:`HISTORY_COLUMNS` (the header text is not exposed offscreen here).
STARTED_AT_COL = 0
UUT_TYPE_COL = 1
SERIAL_COL = 2


def _record_run(tmp_path: Path, app, uut_type="PowerSupply", serial="PS7") -> str:
    """Drive one schedule to completion and return its run_uuid."""
    captured: dict[str, str] = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    # Write the run's database and schedule directly under ``tmp_path`` so it is the same file every
    # test reads back through its own ``store = RunStore(tmp_path / "ate.db")`` (a separate ``runs/``
    # directory here would point at a different, empty database).
    store = RunStore(tmp_path / "ate.db")
    schedule_path = tmp_path / f"{uut_type}_{serial}.tslua"
    schedule_path.write_text(DOC_FIELDS, encoding="utf-8")
    engine = RunEngine(
        store,
        str(schedule_path),
        operator="Alice",
        uut_type=uut_type,
        uut_serial=serial,
        station_name="BENCH-03",
        utc_offset="+10:30",
    )
    engine.run_uuid_reported.connect(_capture)
    _drive(engine, app)
    assert captured.get("run_uuid") is not None, "engine never reported a run key"
    return captured["run_uuid"]


def _cfg(reports_dir: Path) -> Config:
    """A report config pointing at ``reports_dir`` (path is required but unused by the display)."""
    return Config(path=Path("/tmp/ate-history-test"), reports_directory=reports_dir)


def _screen(store: RunStore, *, page_size=3):
    """Return ``(screen, session)`` with a session using ``page_size`` rows per fetch."""
    s = HistorySession(store, page_size=page_size)
    return HistoryScreen(s), s


def test_history_screen_renders_rows_and_selecting_shows_summary(qapp, tmp_path: Path):
    """The table lists every run and selecting one fills the summary panel's fields."""
    store = RunStore(tmp_path / "ate.db")
    _record_run(tmp_path, qapp, uut_type="PowerSupply", serial="PS7")
    _record_run(tmp_path, qapp, uut_type="MotorDriver", serial="MD3")

    screen, session = _screen(store)
    model = screen.table_view.model()

    # Two runs -> two data rows (row 0 is the sticky header).
    assert model.rowCount() == 3
    texts = [model.item(r + 1, UUT_TYPE_COL).text() for r in range(2)]
    assert sorted(texts) == ["MotorDriver", "PowerSupply"]

    # Selecting a run refreshes the summary panel from session.summary(run_uuid).
    row = next(r for r in range(1, model.rowCount()) if model.item(r, UUT_TYPE_COL).text() == "PowerSupply")
    screen.table_view.setCurrentIndex(model.index(row, 0))
    qapp.processEvents()
    panel = "\n".join([screen._summary_fields.text(), screen._result_counts.text(), screen._mismatches.text()])
    assert "PowerSupply" in panel and "PS7" in panel


def test_open_report_disabled_without_a_run(qapp, tmp_path: Path):
    """With no row selected, Open report does nothing."""
    store = RunStore(tmp_path / "ate.db")
    _record_run(tmp_path, qapp)

    opened: list[str] = []
    s = HistorySession(store)
    screen = HistoryScreen(s, open_file=lambda p: opened.append(p))
    screen.open_report_button.click()
    assert opened == []


def test_summary_panel_shows_result_counts(qapp, tmp_path: Path):
    """The Result counts line lists each label with its step count."""
    store = RunStore(tmp_path / "ate.db")
    _record_run(tmp_path, qapp)  # one run -> one data row

    screen, session = _screen(store)
    model = screen.table_view.model()
    row = model.rowCount() - 1  # the single data row (header is row 0)
    screen.table_view.setCurrentIndex(model.index(row, 0))
    qapp.processEvents()

    assert "PASS=1" in screen._result_counts.text()


def test_paging_loads_the_next_page(qapp, tmp_path: Path):
    """'Load more' fetches the next page once there is one and hides itself at the end."""
    store = RunStore(tmp_path / "ate.db")
    serials = ["PS1", "PS2", "PS3", "MD1", "MD2"]  # five distinct runs -> two pages of page_size=3
    for s in serials:
        _record_run(
            tmp_path, qapp, uut_type="PowerSupply" if s.startswith("PS") else "MotorDriver", serial=s
        )

    screen, session = _screen(store, page_size=3)
    model = screen.table_view.model()
    # In this offscreen Qt build a widget must be shown before isVisible() reports truthfully; the real
    # shell shows the HistoryScreen when it displays it.
    screen.show()
    qapp.processEvents()

    def page_serials():
        # Serial lives in column 2 (uut_serial), not started_at (column 0). Read only the data rows.
        return [model.item(r + 1, SERIAL_COL).text() for r in range(model.rowCount() - 1)]

    # Page 0 shows the three newest runs; Load more is visible while a next page exists.
    first = set(page_serials())
    assert len(first) == 3
    assert screen.load_more_button.isVisible()

    screen.next_page()
    qapp.processEvents()
    second = set(page_serials())
    # Paging moves data between pages: the two pages partition the five runs.
    assert first.isdisjoint(second)
    assert first | second == {"PS1", "PS2", "PS3", "MD1", "MD2"}
    # Page 1 still has rows -> Load more stays visible.
    assert screen.load_more_button.isVisible()

    screen.next_page()
    qapp.processEvents()
    third = set(page_serials())
    # No further page -> Load more hides itself.
    assert len(third) == 0
    assert screen.load_more_button.isVisible() is False


def test_filter_inputs_repopulate_and_apply(qapp, tmp_path: Path):
    """The dropdowns are populated from the runs; picking one narrows the table."""
    store = RunStore(tmp_path / "ate.db")
    _record_run(tmp_path, qapp, uut_type="PowerSupply", serial="PS1")
    _record_run(tmp_path, qapp, uut_type="MotorDriver", serial="MD9")

    screen, session = _screen(store)
    model = screen.table_view.model()
    # The UUT Type dropdown lists both types (plus the empty "clear" option).
    assert set(screen.uut_type_combo.itemText(i) for i in range(screen.uut_type_combo.count())) == {"", "PowerSupply", "MotorDriver"}

    # Picking a value drives set_filters and narrows the table to that UUT Type.
    screen.uut_type_combo.setCurrentText("MotorDriver")
    qapp.processEvents()
    texts = [model.item(r + 1, UUT_TYPE_COL).text() for r in range(model.rowCount() - 1)]
    assert texts == ["MotorDriver"]


def test_header_click_sorts_by_the_column(qapp, tmp_path: Path):
    """Clicking a header sorts by that column; the session's sort state follows.

    The click is delivered through ``_on_header_clicked`` (a real QTableView section-click cannot be
    simulated offscreen in this build); what is asserted here is that clicking column 0 sorts by
    ``started_at`` and reorders the rows along it."""
    store = RunStore(tmp_path / "ate.db")
    _record_run(tmp_path, qapp, uut_type="PowerSupply", serial="PS1")
    _record_run(tmp_path, qapp, uut_type="MotorDriver", serial="MD9")

    screen, session = _screen(store)
    model = screen.table_view.model()
    # Column 0 is started_at; map the clicked section straight through the table (header text is not exposed).
    assert screen._column_index["started_at"] == 0
    screen._on_header_clicked(0)
    qapp.processEvents()

    assert session.sort_key == "started_at"
    started_at = [model.item(r + 1, STARTED_AT_COL).text() for r in range(model.rowCount() - 1)]
    # Rows are ordered along the clicked column (ties make them equal, which is still sorted).
    assert started_at == sorted(started_at)


def test_new_run_signal_emits_the_prefill(qapp, tmp_path: Path):
    """'New run for this UUT' emits new_run_requested with the prefill dict."""
    store = RunStore(tmp_path / "ate.db")
    _record_run(tmp_path, qapp, uut_type="PowerSupply", serial="PS7")

    received: list[dict] = []
    s = HistorySession(store)
    screen = HistoryScreen(s, open_file=lambda p: None)
    screen.new_run_requested.connect(lambda d: received.append(d))

    # Select the row (maps to its run_uuid), then click New run.
    model = screen.table_view.model()
    row = next(r for r in range(1, model.rowCount()) if model.item(r, SERIAL_COL).text() == "PS7")
    screen.table_view.setCurrentIndex(model.index(row, 0))
    qapp.processEvents()
    screen.new_run_button.click()

    assert len(received) == 1
    prefill = received[0]
    assert prefill["uut_type"] == "PowerSupply"
    assert prefill["uut_serial"] == "PS7"


def test_show_all_runs_filters_to_the_selected_uut(qapp, tmp_path: Path):
    """'Show all runs of this UUT' filters the table to that run's UUT Type and serial."""
    store = RunStore(tmp_path / "ate.db")
    _record_run(tmp_path, qapp, uut_type="PowerSupply", serial="PS7")
    _record_run(tmp_path, qapp, uut_type="MotorDriver", serial="MD9")

    screen, session = _screen(store)
    model = screen.table_view.model()

    # Select the PowerSupply row (not merely the newest), then click Show all runs of this UUT.
    row = next(r for r in range(1, model.rowCount()) if model.item(r, UUT_TYPE_COL).text() == "PowerSupply")
    screen.table_view.setCurrentIndex(model.index(row, 0))
    qapp.processEvents()
    screen.show_all_runs_button.click()

    texts = [model.item(r + 1, UUT_TYPE_COL).text() for r in range(model.rowCount() - 1)]
    assert texts == ["PowerSupply"]


def test_row_to_uuid_map_tracks_rows(qapp, tmp_path: Path):
    """Each displayed data row maps to its run_uuid (the map selection relies on)."""
    store = RunStore(tmp_path / "ate.db")
    ru1 = _record_run(tmp_path, qapp, uut_type="PowerSupply", serial="PS1")
    ru2 = _record_run(tmp_path, qapp, uut_type="MotorDriver", serial="MD9")

    screen, session = _screen(store)
    rows = {r["run_uuid"]: r for r in session.page(0)}
    # Every data row (index 1..n, header is index 0) maps to a recorded run.
    assert set(screen._row_to_uuid.values()) == {ru1, ru2}
    # A fresh fetch agrees with the map: every mapped run_uuid is a real fetched row.
    assert all(uuid in rows for uuid in screen._row_to_uuid.values())
