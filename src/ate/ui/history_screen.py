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


# Friendly labels shown as the table's real header row, one per :data:`HISTORY_COLUMNS` cell in order.
HISTORY_HEADER_LABELS = ("Start", "UUT Type", "Serial", "Result", "End Reason", "Test Schedule", "Project", "Operator")


class _HistoryItemModel(QStandardItemModel):
    """A QStandardItemModel that renders friendly column labels as its header row (spec 23d).

    ``QStandardItemModel`` has no way to set horizontal header text, so the labels are served through
    :meth:`horizontalHeaderData`. The view then draws them as a real header and excludes that row from the
    body, so data starts at model row 0 — matching how :attr:`HistoryScreen._row_to_uuid` keys rows. This
    keeps ``table_view.model()`` usable (selection reads back through it) unlike a bare ``QTableWidget``,
    whose offscreen selection does not register on this Qt build.

    This PyQt6 build exposes no ``ItemDataRole`` enum, so the header roles are matched on their stable
    integer values: ``DisplayRole = 9`` and ``HeaderDataRole = 10``. Any Horizontal query returns the label.
    """

    def __init__(self, labels: tuple[str, ...]) -> None:
        super().__init__()
        self._header_labels = list(labels)

    def horizontalHeaderData(self, column: int, orientation: Qt.Orientation, role: int) -> str | None:
        if orientation == Qt.Orientation.Horizontal and role in (9, 10):
            return self._header_labels[column]
        return None


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
        # A banner shown while a run is in progress and History is disabled (spec 23c); empty/hidden otherwise.
        self._status_label = QLabel("")
        layout.addWidget(self._status_label)
        self.table_view = QTableView()
        # Friendly labels shown as the real header row; body data therefore starts at model row 0, so
        # selection and the running-row map address rows from 0 (no separate header row to skip).
        model = _HistoryItemModel(HISTORY_HEADER_LABELS)
        self.table_view.setModel(model)
        for column in HISTORY_COLUMNS:
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
        # Build the summary layout on a dedicated container first; setWidget after so the labels are
        # parented into it (building before setWidget leaves them unparented and never shown).
        self._summary_container = QWidget()
        summary_layout = QVBoxLayout(self._summary_container)
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
        self.summary_panel.setWidget(self._summary_container)
        self.summary_panel.setWidgetResizable(True)

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
        # Drop any stale data rows first (a filter or sort change must not accumulate them).
        for r in range(model.rowCount() - 1, -1, -1):
            model.removeRow(r)
        self._row_to_uuid = {}
        # Headers are the model's horizontal header labels (set once in __init__), so body data starts at row 0.
        # ``session.page`` returns pure data rows; a running row (no result / end_reason) is flagged here so its
        # "running" status drives the result cell and disables report actions for it while selected.
        self._row_running = {}
        for offset, row in enumerate(rows):
            abs_row = offset
            self._row_to_uuid[abs_row] = str(row.get("run_uuid") or "")
            running = self.session.is_running(row)
            self._row_running[abs_row] = running
            items = [QStandardItem(str(row.get(column) or "")) for column in HISTORY_COLUMNS]
            if running:
                # an open run has no result yet; show "running" in the result column instead.
                items[self._column_index["result"]] = QStandardItem("running")
            for i, column in enumerate(HISTORY_COLUMNS):
                model.setItem(abs_row, self._column_index[column], items[HISTORY_COLUMNS.index(column)])
        # 'Load more' stays visible while a page has rows and hides only once a fetch returns none, so the
        # user can still try to advance until they've confirmed there is nothing further.
        load_more = self.load_more_button
        load_more.setVisible(bool(rows))
        # Populate the dropdown filters from all runs (only on a fresh, unfiltered view). When a filter is
        # already active we must not clear and repopulate the inputs here — set_filters has just written
        # their values, and clearing them would drop the selection back to empty.
        if page == 0 and not self._read_filters():
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
        if row < 0 or row not in self._row_to_uuid:  # there is no header row; any valid data row maps to a run
            return None
        return self._row_to_uuid[row]

    def _refresh_summary(self) -> None:
        index = self.table_view.selectedIndexes()
        row = index[0].row() if index else 0
        running = row in self._row_to_uuid and self._row_running.get(row, False)
        run_uuid = self._selected_run_uuid()
        # A selected run still executing has no result yet: show "running" instead of a summary.
        if not run_uuid or running:
            self._summary_fields.setText("running" if running else "")
            self._result_counts.setText("running" if running else "")
            self._mismatches.setText("")
            # report actions for an open run are disabled regardless of the global flag below.
            self._update_actions_for_selected()
            return
        summary = self.session.summary(run_uuid)
        fields = ", ".join(f"{key}={value}" for key, value in summary.items() if key not in ("run_uuid", "result_counts", "checksum_mismatches"))
        self._summary_fields.setText(fields)
        counts = ", ".join(f"{label}={count}" for label, count in summary["result_counts"].items())
        self._result_counts.setText(counts)
        mismatches = [f.path for f in summary["checksum_mismatches"]] or "none"
        self._mismatches.setText(mismatches)

    def _update_actions_for_selected(self) -> None:
        """Disable report actions for a selected run still executing (spec 23c)."""
        if not self.session.available():
            return
        index = self.table_view.selectedIndexes()
        row = index[0].row() if index else 0
        running = row in self._row_to_uuid and self._row_running.get(row, False)
        if not running:
            return
        # An open run has no report to open or regenerate yet. "Show all runs" / "New run" are left enabled.
        self.open_report_button.setEnabled(False)
        self.regenerate_button.setEnabled(False)
        export_log = getattr(self, "export_log_button", None)
        if export_log is not None:
            export_log.setEnabled(False)

    def set_run_in_progress(self, active: bool) -> None:
        """Tell the session a run is (no longer) in progress and refresh the display.

        When History is disabled during a run the table and action bar are disabled and "A Test Run is in
        progress" is shown; when it becomes available again they are restored, and any still-selected open
        run has its report actions re-disabled by :meth:`_update_actions_for_selected`."""
        self.session.set_run_in_progress(active)
        if not self.session.available():
            self.table_view.setEnabled(False)
            self._actions_bar.setEnabled(False)
            self._status_label.setText("A Test Run is in progress")
            self._status_label.setVisible(True)
        else:
            self.table_view.setEnabled(True)
            self._actions_bar.setEnabled(True)
            self._status_label.setText("")
            self._status_label.setVisible(False)
            self._update_actions_for_selected()

    # -- actions -------------------------------------------------------------

    def _build_actions(self, parent: QVBoxLayout) -> None:
        """Open report, Regenerate (HTML / HTML+PDF + Include run log), Show all runs of this UUT, New run for this UUT."""
        bar = QWidget()
        self._actions_bar = bar
        bar_layout = QVBoxLayout(bar)
        bar_layout.setContentsMargins(0, 0, 0, 0)

        self.load_more_button = QPushButton("Load more")
        self.load_more_button.clicked.connect(self.next_page)

        self.open_report_button = QPushButton("Open report")
        self.regenerate_button = QPushButton("Regenerate")
        self.show_all_runs_button = QPushButton("Show all runs of this UUT")
        self.new_run_button = QPushButton("New run for this UUT")

        # Spec (history-screen.md, test-report-format.md): regenerating includes the run log by default.
        self.include_run_log = QCheckBox("Include run log")
        self.include_run_log.setChecked(True)
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
        if "uut_serial" in filters:
            self.serial_edit.setText(filters["uut_serial"])
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
