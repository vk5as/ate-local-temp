"""Packet 24b seam test: the run log export dialog on the flat History screen.

Seam: a HistoryScreen over a real HistorySession and a store (a run recorded through the
RunEngine) plus a dialog whose Save As is injected so tests never touch the platform. The dialog
is dumb: the owner choices and default path come from the session, the Export button checks the
exporter's row builder, and the write goes through session.export_log. Every action is a call
into an existing interface."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from ate.config import Config
from ate.engine.run_engine import RunEngine
from ate.history.session import HistorySession
from ate.report.html import _local_stamp
from ate.report.log_export import FIELDS
from ate.storage.store import RunStore
from ate.ui.export_log_dialog import ExportLogDialog
from ate.ui.history_screen import HistoryScreen

from ._fixtures import DOC_FIELDS, FAILING, LOGGING, PRE_POST_PASS, _drive

SERIAL_COL = 2


def _record_run(tmp_path: Path, app, source, uut_type="PowerSupply", serial="PS7") -> str:
    """Drive one schedule to completion and return its run_uuid."""
    captured: dict[str, str] = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    store = RunStore(tmp_path / "ate.db")
    schedule_path = tmp_path / f"{uut_type}_{serial}.tslua"
    schedule_path.write_text(source, encoding="utf-8")
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
    return Config(path=Path("/tmp/ate-24b-test"), reports_directory=reports_dir)


def _screen(store: RunStore, *, cfg: Config | None = None, file_dialog=None):
    """Return ``(screen, session)``; ``cfg`` and ``file_dialog`` are the 24b seams."""
    s = HistorySession(store, config=cfg)
    return HistoryScreen(s, file_dialog=file_dialog), s


def _select(screen: HistoryScreen, app, serial: str) -> None:
    """Select the row carrying ``serial`` (body data starts at model row 0)."""
    model = screen.table_view.model()
    row = next(r for r in range(model.rowCount()) if model.item(r, SERIAL_COL).text() == serial)
    screen.table_view.setCurrentIndex(model.index(row, 0))
    app.processEvents()


def _open_dialog(screen: HistoryScreen, app) -> ExportLogDialog:
    """Click "Export log" for the selected run and return the dialog it created."""
    screen.export_log_button.click()
    app.processEvents()
    dialog = screen._export_dialog
    assert dialog is not None, "Export log did not open a dialog"
    return dialog


# --- Item 1: the session's export interface -------------------------------------


def test_owner_choices_are_run_phases_and_steps_in_tree_order(qapp, tmp_path: Path):
    """Owner tree = Run, each Pre-/Post-test Action, then the steps in tree order; a step's
    label is its Step Number and name."""
    store = RunStore(tmp_path / "ate.db")
    uuid = _record_run(tmp_path, qapp, DOC_FIELDS, serial="PS7")
    s = HistorySession(store)

    # A one-step run: Run then the single step, labelled "1. Step name".
    assert s.export_owner_choices(uuid) == [("run", "Run"), ("step:0", "1. LED colour")]

    uuid2 = _record_run(tmp_path, qapp, PRE_POST_PASS, serial="PS8")
    # Pre-test Action before the step's Post-test... no: the Pre-test, then the Post-test,
    # then the step in tree order.
    assert s.export_owner_choices(uuid2) == [
        ("run", "Run"),
        ("pretest:0", "Pre-test"),
        ("posttest:0", "Post-test"),
        ("step:0", "1. the step"),
    ]

    uuid3 = _record_run(tmp_path, qapp, LOGGING, serial="PS9")
    choices = s.export_owner_choices(uuid3)
    assert [label for _key, label in choices] == ["Run", "1. info step", "2. warn step", "3. error step"]
    assert [key for key, _label in choices] == ["run", "step:0", "step:1", "step:2"]


def test_default_save_path_is_under_the_report_folder(qapp, tmp_path: Path):
    """The default path is the report directory + stem + ``_log.csv``; the dialog starts there
    with CSV selected."""
    store = RunStore(tmp_path / "ate.db")
    uuid = _record_run(tmp_path, qapp, DOC_FIELDS, serial="PS7")
    cfg = _cfg(tmp_path / "reports")
    s = HistorySession(store, config=cfg)

    path = s.export_log_default_path(uuid)
    assert path.parent == tmp_path / "reports" / "PowerSupply"
    assert path.suffix == ".csv" and path.name.endswith("_log.csv")

    screen, _s = _screen(store, cfg=cfg)
    _select(screen, qapp, "PS7")
    dialog = _open_dialog(screen, qapp)
    assert dialog.default_path == str(path)
    assert dialog.format_combo.currentText() == "CSV"
    assert dialog.format_combo.currentData() == "csv"


def test_default_save_name_matches_the_report_name(qapp, tmp_path: Path):
    """The default Save As name is the run's report name (``<stem>_<RESULT>``) with ``_log``
    added (spec 24b Saving) — the full file name pinned for both a PASS and a FAIL run."""
    store = RunStore(tmp_path / "ate.db")
    cfg = _cfg(tmp_path / "reports")
    s = HistorySession(store, config=cfg)

    uuid = _record_run(tmp_path, qapp, DOC_FIELDS, serial="PS7")
    row = store.get_test_run(uuid)
    stamp = _local_stamp(row.get("started_at"), row.get("utc_offset") or "+00:00")
    assert s.export_log_default_path(uuid) == (
        tmp_path / "reports" / "PowerSupply" / f"PowerSupply_PS7_{stamp}_PASS_log.csv"
    )

    uuid2 = _record_run(tmp_path, qapp, FAILING, serial="PS8")
    row2 = store.get_test_run(uuid2)
    stamp2 = _local_stamp(row2.get("started_at"), row2.get("utc_offset") or "+00:00")
    assert s.export_log_default_path(uuid2) == (
        tmp_path / "reports" / "PowerSupply" / f"PowerSupply_PS8_{stamp2}_FAIL_log.csv"
    )


def test_session_export_log_wraps_the_exporter(qapp, tmp_path: Path):
    """session.export_log writes through the exporter with the dialog's tick filters applied."""
    store = RunStore(tmp_path / "ate.db")
    uuid = _record_run(tmp_path, qapp, LOGGING, serial="PS9")
    s = HistorySession(store)
    out = tmp_path / "out.csv"
    count = s.export_log(uuid, str(out), "csv", levels={"INFO"}, owners={"step:0"})
    assert count == 2
    rows = [list(r) for r in csv.reader(io.StringIO(out.read_text(encoding="utf-8-sig").replace("\r\n", "\n")))]
    assert rows[0] == list(FIELDS)
    assert all(row[1] == "INFO" and row[5] == "1." and row[6] == "info step" for row in rows[1:])
    assert len(rows) == 1 + 2  # "step one running" + the print line


# --- Item 2: the export dialog --------------------------------------------------


def test_dialog_opens_with_every_box_ticked_and_export_enabled(qapp, tmp_path: Path):
    """All Level, Source and Owner boxes are ticked each time the dialog opens; Export starts
    enabled; the Owner group lists the run's owner nodes in tree order."""
    store = RunStore(tmp_path / "ate.db")
    uuid = _record_run(tmp_path, qapp, LOGGING, serial="PS9")
    cfg = _cfg(tmp_path / "reports")
    screen, _s = _screen(store, cfg=cfg)
    _select(screen, qapp, "PS9")
    assert screen.export_log_button.isEnabled() is True

    dialog = _open_dialog(screen, qapp)
    assert dialog.windowTitle() == "Export run log"

    all_boxes = [
        *dialog._level_boxes.values(),
        *dialog._source_boxes.values(),
        *dialog._owner_boxes.values(),
    ]
    assert all(box.isChecked() for box in all_boxes)
    # The Owner group carries the session's owner choices, in tree order, keyed by owner key.
    assert list(dialog._owner_boxes) == ["run", "step:0", "step:1", "step:2"]
    assert [box.text() for box in dialog._owner_boxes.values()] == [
        "Run", "1. info step", "2. warn step", "3. error step"
    ]
    assert dialog.export_button.isEnabled() is True


def test_unticking_every_level_disables_export_and_reenables(qapp, tmp_path: Path):
    """Export is disabled when the ticks would export no line: every level, or every source, or
    every owner unticked disables it; restoring the ticks re-enables it."""
    store = RunStore(tmp_path / "ate.db")
    uuid = _record_run(tmp_path, qapp, LOGGING, serial="PS9")
    cfg = _cfg(tmp_path / "reports")
    screen, _s = _screen(store, cfg=cfg)
    _select(screen, qapp, "PS9")
    dialog = _open_dialog(screen, qapp)

    for box in dialog._level_boxes.values():
        box.setChecked(False)
    assert dialog.export_button.isEnabled() is False
    for box in dialog._level_boxes.values():
        box.setChecked(True)
    assert dialog.export_button.isEnabled() is True

    for box in dialog._source_boxes.values():
        box.setChecked(False)
    assert dialog.export_button.isEnabled() is False
    for box in dialog._source_boxes.values():
        box.setChecked(True)
    assert dialog.export_button.isEnabled() is True

    for box in dialog._owner_boxes.values():
        box.setChecked(False)
    assert dialog.export_button.isEnabled() is False
    for box in dialog._owner_boxes.values():
        box.setChecked(True)
    assert dialog.export_button.isEnabled() is True


def test_filtered_export_writes_a_csv_honouring_the_ticks(qapp, tmp_path: Path):
    """With some boxes unticked, the written CSV carries only the ticked lines; the extension
    follows the chosen (default) format even though Save As picked a .txt name."""
    store = RunStore(tmp_path / "ate.db")
    uuid = _record_run(tmp_path, qapp, LOGGING, serial="PS9")
    cfg = _cfg(tmp_path / "reports")
    screen, _s = _screen(store, cfg=cfg, file_dialog=lambda _title, _default, _filters: tmp_path / "exported.txt")
    _select(screen, qapp, "PS9")
    dialog = _open_dialog(screen, qapp)

    # Keep DEBUG/INFO/WARNING and only the first step's owner; untick the other two steps.
    dialog._level_boxes["ERROR"].setChecked(False)
    dialog._owner_boxes["step:1"].setChecked(False)
    dialog._owner_boxes["step:2"].setChecked(False)
    dialog.export_button.click()
    qapp.processEvents()

    path = tmp_path / "exported.csv"
    assert path.exists()
    text = path.read_text(encoding="utf-8-sig")
    rows = [list(r) for r in csv.reader(io.StringIO(text.replace("\r\n", "\n")))]
    assert rows[0] == list(FIELDS)
    # The run's four lines: the two INFO lines of step 1 survive, nothing else does.
    assert len(rows) == 3
    for row in rows[1:]:
        assert row[1] in ("DEBUG", "INFO", "WARNING")
        assert row[5] in ("", "1.")
        assert row[6] in ("", "info step")
    assert "step one running" in text
    assert "printed here" in text
    assert "warn step" not in text and "error step" not in text


def test_everything_ticked_exports_every_line(qapp, tmp_path: Path):
    """With all boxes ticked, every recorded line is exported (count matches the store)."""
    store = RunStore(tmp_path / "ate.db")
    uuid = _record_run(tmp_path, qapp, LOGGING, serial="PS9")
    cfg = _cfg(tmp_path / "reports")
    screen, _s = _screen(store, cfg=cfg, file_dialog=lambda _title, _default, _filters: tmp_path / "all.jsonl")
    _select(screen, qapp, "PS9")
    dialog = _open_dialog(screen, qapp)
    jsonl_index = next(i for i in range(dialog.format_combo.count()) if dialog.format_combo.itemData(i) == "jsonl")
    dialog.format_combo.setCurrentIndex(jsonl_index)
    dialog.export_button.click()
    qapp.processEvents()

    path = tmp_path / "all.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(store.log_lines(uuid))
    objs = [json.loads(line) for line in lines]
    assert all(tuple(o) == FIELDS for o in objs)
    assert {o["level"] for o in objs} == {"INFO", "WARNING", "ERROR"}
    assert {o["step_number"] for o in objs} == {"1.", "2.", "3."}


# --- Item 3: the History screen button ------------------------------------------


def test_export_button_enabled_for_finished_and_interrupted_runs(qapp, tmp_path: Path):
    """'Export log' is enabled for a selected finished run — including an INTERRUPTED one — and
    disabled with nothing selected."""
    store = RunStore(tmp_path / "ate.db")
    uuid = _record_run(tmp_path, qapp, DOC_FIELDS, serial="PS7")
    # An INTERRUPTED row: inserted open, then closed as the store closes interrupted runs.
    interrupted = store.insert_test_run(
        {"run_uuid": None, "uut_type": "PowerSupply", "uut_serial": "PS-int", "station_name": "BENCH-03"}
    )["run_uuid"]
    store.update_run_end(interrupted, "FAIL", "INTERRUPTED")

    screen, _s = _screen(store)
    assert screen.export_log_button.isEnabled() is False  # nothing selected yet
    _select(screen, qapp, "PS-int")
    assert screen.export_log_button.isEnabled() is True  # INTERRUPTED is finished
    _select(screen, qapp, "PS7")
    assert screen.export_log_button.isEnabled() is True
    screen.table_view.selectionModel().clearSelection()
    qapp.processEvents()
    assert screen.export_log_button.isEnabled() is False


def test_export_button_disabled_for_a_running_run(qapp, tmp_path: Path):
    """A running run (no result, no end_reason) disables 'Export log' and the click opens no dialog."""
    store = RunStore(tmp_path / "ate.db")
    data = {
        "run_uuid": None,
        "uut_type": "PowerSupply",
        "uut_serial": "PS-running",
        "station_name": "BENCH-03",
    }
    store.insert_test_run(data)

    screen, _s = _screen(store)
    _s.set_run_in_progress(True)
    _select(screen, qapp, "PS-running")
    assert screen.export_log_button.isEnabled() is False

    screen.export_log_button.click()
    qapp.processEvents()
    assert screen._export_dialog is None
    screen._on_export_log()  # the handler itself refuses a running selection
    assert screen._export_dialog is None
