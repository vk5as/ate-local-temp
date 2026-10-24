"""The flat History screen (ticket 23b): a newest-first table of past runs plus a summary panel.

This is the only place that binds :class:`~ate.history.session.HistorySession` to widgets; it holds no
run logic of its own — every action is a call into an existing interface: sorting rides ``set_sort``
(clickable headers), paging rides ``next_page`` ("Load more"), filtering rides ``set_filters`` (the
input fields), and the summary panel reads ``summary`` for one selected run. "Open report" / "Regenerate"
open files through a module-level, injectable ``open_file`` so tests never touch the platform. Nothing
here imports the engine or run session.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..history.session import HistorySession
from ..storage.store import HISTORY_COLUMNS


def _default_open_file(path: str | None) -> None:
    """Open ``path`` in the system viewer; a no-op when nothing was written (spec 23b)."""
    if path is None or not Path(path).exists():
        return
    subprocess.Popen(["xdg-open", path])


class HistoryScreen(QWidget):
    """A flat table of past runs, newest first, with a summary panel for the selected run.

    ``session`` supplies every row and action; ``open_file`` opens report files (injected so tests can
    capture it instead of launching a viewer). Selecting a run refreshes the summary panel; its actions
    are wired to :meth:`HistorySession.summary`, :meth:`report_files`, :meth:`regenerate`,
    :meth:`uut_filter` and :meth:`new_run_prefill`.
    """

    # Prefill dict for a new run of this UUT (see :meth:`HistorySession.new_run_prefill`).
    new_run_requested = pyqtSignal(dict)

    def __init__(self, session: HistorySession, *, open_file: Optional[Callable[[str | None], None]] = None) -> None:
        super().__init__()
        self.session = session
        self._open_file = open_file or _default_open_file
        # column name -> model column index (built once in __init__, reused on every reload)
        self._column_index: dict[str, int] = {}
        # absolute page currently shown; "Load more" advances it and filter/sort changes reset it to 0
        self._page_index = 0

        layout = QVBoxLayout(self)
        self.table_view = QTableView()
        model = QStandardItemModel()
        self.table_view.setModel(model)
        for column in HISTORY_COLUMNS:
            header_item = QStandardItem(column)
            header_item.setEditable(False)
            model.setItem(0, HISTORY_COLUMNS.index(column), header_item)  # row 0 holds the headers
            self._column_index[column] = HISTORY_COLUMNS.index(column)
        header = self.table_view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.sectionClicked.connect(self._on_header_clicked)
        # Refresh the summary panel whenever the selected run changes.
        self.table_view.selectionModel().selectionChanged.connect(self._refresh_summary)

        layout.addWidget(self.table_view)
        controls = QWidget()
        control_layout = QVBoxLayout(controls)
        self._build_filters(control_layout)
        control_layout.addStretch()
        layout.addWidget(controls)

        self.summary_panel = QScrollArea()
        summary_layout = QVBoxLayout(self.summary_panel.widget())
        self._summary_fields = QLabel("")
        self._result_counts = QLabel("")
        self._mismatches = QLabel("")
        summary_layout.addWidget(QLabel("Run"))
        summary_layout.addWidget(self._summary_fields)
        summary_layout.addWidget(QLabel("Result counts"))
        summary_layout.addWidget(self._result_counts)
        summary_layout.addWidget(QLabel("Checksum mismatches"))
        summary_layout.addWidget(self._mismatches)
        layout.addWidget(self.summary_panel)

        self._build_actions(control_layout)

        self._load_page(0)

    # -- filters -------------------------------------------------------------

    def _build_filters(self, parent: QVBoxLayout) -> None:
        """UUT Type, serial, result, End Reason, date range and Project — each bound to ``set_filters``."""
        # ``parent`` is the controls layout already installed in __init__; add labels/inputs directly.

        self.uut_type_combo = QComboBox()
        self.serial_edit = QLineEdit()
        self.result_combo = QComboBox()
        self.end_reason_combo = QComboBox()
        self.date_from_edit = QLineEdit()
        self.date_to_edit = QLineEdit()
        self.project_combo = QComboBox()

        for combo in (self.uut_type_combo, self.result_combo, self.end_reason_combo, self.project_combo):
            combo.setEditable(False)
            combo.addItem("", "…")  # empty option clears the filter
            combo.currentIndexChanged.connect(self._on_filter_changed)  # picking a value narrows the table (spec: Filters)

        rows = [
            ("UUT Type", self.uut_type_combo),
            ("Serial", self.serial_edit),
            ("Result", self.result_combo),
            ("End Reason", self.end_reason_combo),
            ("From", self.date_from_edit),
            ("To", self.date_to_edit),
            ("Project", self.project_combo),
        ]
        for caption, widget in rows:
            parent.addWidget(QLabel(caption))
            parent.addWidget(widget)

    def _read_filters(self) -> dict[str, str]:
        """The current filter mapping from the input widgets (empty options clear their column)."""
        filters: dict[str, str] = {}
        if self.uut_type_combo.currentText():
            filters["uut_type"] = self.uut_type_combo.currentText()
        serial = self.serial_edit.text()
        if serial:
            filters["uut_serial"] = serial.lower()
        if self.result_combo.currentText():
            filters["result"] = self.result_combo.currentText()
        if self.end_reason_combo.currentText():
            filters["end_reason"] = self.end_reason_combo.currentText()
        if self.date_from_edit.text():
            filters["date_from"] = self.date_from_edit.text()
        if self.date_to_edit.text():
            filters["date_to"] = self.date_to_edit.text()
        if self.project_combo.currentText():
            filters["project"] = self.project_combo.currentText()
        return filters

    def _refresh_filter_options(self) -> None:
        """Repopulate the dropdowns from the distinct values in the current page."""
        rows = self.session.page(0)
        for combo, key in (
            (self.uut_type_combo, "uut_type"),
            (self.result_combo, "result"),
            (self.end_reason_combo, "end_reason"),
            (self.project_combo, "project"),
        ):
            values = sorted({str(row.get(key) or "") for row in rows if str(row.get(key) or "")})
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("", "…")
            for value in values:
                combo.addItem(value, value)
            combo.blockSignals(False)

    def _on_filter_changed(self, index: int) -> None:
        """A filter dropdown changed; re-read every input and apply them (the column index is unused)."""
        self.set_filters(self._read_filters())

    # -- paging --------------------------------------------------------------

    def _load_page(self, page: int) -> None:
        """Fetch ``page`` and rebuild the table; hide 'Load more' once every row is shown."""
        rows = self.session.page(page)
        model = self.table_view.model()
        # Drop any stale data/header rows first (a filter or sort change must not accumulate them).
        for r in range(model.rowCount() - 1, -1, -1):
            model.removeRow(r)
        self._row_to_uuid = {}
        for column in HISTORY_COLUMNS:
            header_item = QStandardItem(column)
            header_item.setEditable(False)
            model.setItem(0, self._column_index[column], header_item)
        # ``session.page`` returns pure data rows (no header); the header lives at model row 0, so every
        # page's data starts at model row 1 regardless of ``page`` — shifting by ``page`` would leave a
        # gap and make later pages' early cells read back as None.
        for offset, row in enumerate(rows):
            items = [QStandardItem(str(row.get(column) or "")) for column in HISTORY_COLUMNS]
            abs_row = 1 + offset
            self._row_to_uuid[abs_row] = str(row.get("run_uuid") or "")
            for i, column in enumerate(HISTORY_COLUMNS):
                model.setItem(abs_row, self._column_index[column], items[HISTORY_COLUMNS.index(column)])
        # 'Load more' stays visible while a page has rows and hides only once a fetch returns none, so the
        # user can still try to advance until they've confirmed there is nothing further.
        load_more = self.load_more_button
        load_more.setVisible(bool(rows))
        # Populate the dropdown filters from all runs (only on a fresh, unfiltered view).
        if page == 0:
            self._refresh_filter_options()

    def next_page(self) -> None:
        """Load the next page (or stop at the end)."""
        # 'Load more' hides itself once no rows remain, so paging is gated on visibility rather than an
        # enabled flag; advance this counter and pass it to ``_load_page``.
        if not self.load_more_button.isVisible():
            return
        self._page_index += 1
        self._load_page(self._page_index)

    # -- selection & summary -------------------------------------------------

    def _selected_run_uuid(self) -> Optional[str]:
        index = self.table_view.selectedIndexes()
        if not index:
            return None
        row = index[0].row()
        if row == 0 or row not in self._row_to_uuid:  # the header row is never selectable
            return None
        return self._row_to_uuid[row]

    def _refresh_summary(self) -> None:
        run_uuid = self._selected_run_uuid()
        if not run_uuid:
            self._summary_fields.setText("")
            self._result_counts.setText("")
            self._mismatches.setText("")
            return
        summary = self.session.summary(run_uuid)
        fields = ", ".join(f"{key}={value}" for key, value in summary.items() if key not in ("run_uuid", "result_counts", "checksum_mismatches"))
        self._summary_fields.setText(fields)
        counts = ", ".join(f"{label}={count}" for label, count in summary["result_counts"].items())
        self._result_counts.setText(counts)
        mismatches = [f.path for f in summary["checksum_mismatches"]] or "none"
        self._mismatches.setText(mismatches)

    # -- actions -------------------------------------------------------------

    def _build_actions(self, parent: QVBoxLayout) -> None:
        """Open report, Regenerate (HTML / HTML+PDF + Include run log), Show all runs of this UUT, New run for this UUT."""
        bar = QWidget()
        bar_layout = QVBoxLayout(bar)
        bar_layout.setContentsMargins(0, 0, 0, 0)

        self.load_more_button = QPushButton("Load more")
        self.load_more_button.clicked.connect(self.next_page)

        self.open_report_button = QPushButton("Open report")
        self.regenerate_button = QPushButton("Regenerate")
        self.show_all_runs_button = QPushButton("Show all runs of this UUT")
        self.new_run_button = QPushButton("New run for this UUT")

        self.include_run_log = QCheckBox("Include run log")
        self.regenerate_mode = QComboBox()
        self.regenerate_mode.addItem("HTML", "html-only")
        self.regenerate_mode.addItem("HTML + PDF", "html+pdf")

        bar_layout.addWidget(self.load_more_button)
        bar_layout.addWidget(self.open_report_button)
        bar_layout.addWidget(self.regenerate_button)
        bar_layout.addWidget(self.show_all_runs_button)
        bar_layout.addWidget(self.new_run_button)
        action_row = QHBoxLayout()
        action_row.addWidget(self.include_run_log)
        action_row.addWidget(self.regenerate_mode)
        bar_layout.addLayout(action_row)
        parent.addWidget(bar)

        self.open_report_button.clicked.connect(self._on_open_report)
        self.regenerate_button.clicked.connect(self._on_regenerate)
        self.show_all_runs_button.clicked.connect(self._on_show_all_runs)
        self.new_run_button.clicked.connect(self._emit_new_run)

    def _files_for(self, run_uuid: str) -> list[str]:
        try:
            return [str(p) for p in self.session.report_files(run_uuid)]
        except ValueError:
            return []

    def _on_open_report(self) -> None:
        """Open the newest report file for the selected run."""
        run_uuid = self._selected_run_uuid()
        if not run_uuid:
            return
        files = self._files_for(run_uuid)
        if files:
            self._open_file(files[0])

    def _on_regenerate(self) -> None:
        """Regenerate the selected run's report(s); HTML always, PDF when selected."""
        run_uuid = self._selected_run_uuid()
        if not run_uuid:
            return
        mode = self.regenerate_mode.currentData()
        self.session.regenerate(
            run_uuid,
            html=True,
            pdf=mode == "html+pdf",
            include_run_log=self.include_run_log.isChecked(),
            parent=self,
        )

    def _on_show_all_runs(self) -> None:
        """Filter the table to every run of this UUT Type and serial."""
        run_uuid = self._selected_run_uuid()
        if not run_uuid:
            return
        filters = self.session.uut_filter(run_uuid)
        self.set_filters(filters)

    def set_filters(self, filters: dict[str, str]) -> None:
        """Apply a filter mapping to the inputs and reload from page zero."""
        # The store builds its SQL from the session's filters; update them first so the reload is narrowed.
        self.session.set_filters(filters)
        if "uut_type" in filters:
            self.uut_type_combo.blockSignals(True)
            self.uut_type_combo.setCurrentText(filters["uut_type"])
            self.uut_type_combo.blockSignals(False)
        if "serial" in filters:
            self.serial_edit.setText(filters["serial"])
        if "result" in filters:
            self.result_combo.blockSignals(True)
            self.result_combo.setCurrentText(filters["result"])
            self.result_combo.blockSignals(False)
        if "end_reason" in filters:
            self.end_reason_combo.blockSignals(True)
            self.end_reason_combo.setCurrentText(filters["end_reason"])
            self.end_reason_combo.blockSignals(False)
        if "project" in filters:
            self.project_combo.blockSignals(True)
            self.project_combo.setCurrentText(filters["project"])
            self.project_combo.blockSignals(False)
        self.date_from_edit.setText(filters.get("date_from", ""))
        self.date_to_edit.setText(filters.get("date_to", ""))
        # a filter change reloads from the top; reset "Load more" too so it counts pages of the new view
        self._page_index = 0
        self._load_page(0)

    def _emit_new_run(self) -> None:
        """Emit ``new_run_requested`` with the prefill for a new run of this UUT."""
        run_uuid = self._selected_run_uuid()
        if not run_uuid:
            return
        self.new_run_requested.emit(self.session.new_run_prefill(run_uuid))

    # -- header sort ---------------------------------------------------------

    def _on_header_clicked(self, section: int) -> None:
        """Sort by the clicked column; a second click flips the direction."""
        # ``section`` is the header's column index and sections are never reordered here, so it indexes
        # HISTORY_COLUMNS directly. (``_column_index`` maps name->index for building rows; reading header
        # text would be both unnecessary and unreliable in this Qt 6.11 build.)
        name = HISTORY_COLUMNS[section]
        self.session.set_sort(name, descending=not self.session.descending)
        # a sort change reloads from the top; reset "Load more" too so it counts pages of the new order
        self._page_index = 0
        self._load_page(0)
