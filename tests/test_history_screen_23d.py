"""Ticket 23d's rendering fixes for the flat History screen (seam-2 smoke test).

It builds :class:`HistoryScreen` on a live :class:`HistorySession` and asserts what is actually on
screen rather than just widget text: the summary panel must show the selected run's fields inside its
scroll container, the table header row must carry the friendly column labels (not the first body row),
"Include Run log" must be ticked by default, and "Show all runs of this UUT" must also fill the filter
inputs with that run's UUT Type and serial.

Visibility/geometry is checked through parent/descendant relations rather than ``isVisible()`` — in this
offscreen Qt build a child's ``isVisible()`` can report falsely while its parent is shown (see 23c). The
summary labels are asserted as descendants of the scroll area's widget, and the header row is read from
the view's real horizontal header items.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QWidget

from ate.engine.run_engine import RunEngine
from ate.history.session import HistorySession
from ate.storage.store import HISTORY_COLUMNS, RunStore
from ate.ui.history_screen import (
    HISTORY_HEADER_LABELS,
    HistoryScreen,
)

UUT_TYPE_COL = 1
SERIAL_COL = 2


def _record_run(tmp_path: Path, app, uut_type="PowerSupply", serial="PS7") -> str:
    """Drive one schedule to completion and return its run_uuid."""
    captured: dict[str, str] = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    store = RunStore(tmp_path / "ate.db")
    schedule_path = tmp_path / f"{uut_type}_{serial}.tslua"
    schedule_path.write_text(_SCHEDULE, encoding="utf-8")
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
    engine.start()
    assert engine.wait(10_000), "engine never finished"
    app.sendPostedEvents()
    app.processEvents()
    assert captured.get("run_uuid") is not None, "engine never reported a run key"
    return captured["run_uuid"]


def _screen(store: RunStore):
    s = HistorySession(store)
    return HistoryScreen(s), s


_SCHEDULE = """\
return ScheduleParams("Doc fields schedule", "a step with full document metadata")
  :setDocTitle("Quarterly Power Audit")
  :setSchedule({
    StepParams("LED colour"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "ok", "Green", "Green") end),
  })
"""


def _header_labels(model):
    """The view's friendly header labels read back from the model's horizontal header data."""
    n = len(HISTORY_COLUMNS)
    # This build exposes no HeaderDataRole enum, so pass the stable role value (10).
    return [model.horizontalHeaderData(col, Qt.Orientation.Horizontal, 10) for col in range(n)]


def _descendants(widget):
    """Every descendant widget of ``widget`` (depth-first, no duplicates)."""
    found = []
    for child in widget.children():
        if isinstance(child, QWidget):
            found.append(child)
            found.extend(_descendants(child))
    return found


def test_summary_container_is_the_scroll_widget_with_labels(qapp, tmp_path: Path):
    """The summary labels are parented into the scroll area's container and show the selected run."""
    store = RunStore(tmp_path / "ate.db")
    _record_run(tmp_path, qapp, uut_type="PowerSupply", serial="PS7")

    screen, session = _screen(store)
    model = screen.table_view.model()
    # Header row carries friendly labels; body data starts at row 0 (the newest run), not the column key.
    assert tuple(_header_labels(model)) == HISTORY_HEADER_LABELS
    assert model.item(0, SERIAL_COL).text() != "started_at"

    container = screen._summary_container
    # The scroll area's widget is the container we built the layout on.
    assert screen.summary_panel.widget() is container
    labels = [w for w in _descendants(container) if isinstance(w, QLabel)]
    assert labels, "no summary labels found under the container"
    # Selecting a run fills its fields; one label must carry the serial so it is on screen.
    row = next(r for r in range(model.rowCount()) if model.item(r, SERIAL_COL).text() == "PS7")
    screen.table_view.setCurrentIndex(model.index(row, 0))
    qapp.processEvents()
    texts = [w.text() for w in labels]
    assert any("PS7" in t for t in texts)


def test_include_run_log_is_checked_by_default(qapp, tmp_path: Path):
    """'Include Run log' starts ticked."""
    store = RunStore(tmp_path / "ate.db")
    _record_run(tmp_path, qapp)

    screen, _ = _screen(store)
    assert screen.include_run_log.isChecked() is True


def test_show_all_runs_fills_the_filter_inputs(qapp, tmp_path: Path):
    """'Show all runs of this UUT' sets the filter inputs to that run's UUT Type and serial."""
    store = RunStore(tmp_path / "ate.db")
    _record_run(tmp_path, qapp, uut_type="PowerSupply", serial="PS7")
    _record_run(tmp_path, qapp, uut_type="MotorDriver", serial="MD9")

    screen, session = _screen(store)
    model = screen.table_view.model()
    row = next(r for r in range(model.rowCount()) if model.item(r, UUT_TYPE_COL).text() == "PowerSupply")
    run_uuid = screen._row_to_uuid[row]
    assert session.uut_filter(run_uuid)["uut_type"] == "PowerSupply"

    screen.table_view.setCurrentIndex(model.index(row, 0))
    qapp.processEvents()
    screen.show_all_runs_button.click()
    qapp.processEvents()

    # The UUT Type combo and the Serial edit both reflect the selected run's identity.
    assert screen.uut_type_combo.currentText() == "PowerSupply"
    assert screen.serial_edit.text() == "PS7"
