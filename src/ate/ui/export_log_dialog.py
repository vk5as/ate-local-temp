"""The run log export dialog (packet 24b): Level / Source / Owner checkbox groups and Save As.

Save As' own file-type list (CSV, JSON Lines, Text) picks the format; there is no separate format
picker. The name Save As returns is used as typed, and the chosen filter's extension is added only
when the Operator typed none.

The dialog is dumb: the owner choices and default path come from the HistorySession (via the
screen), the Export button is enabled only when the exporter's row builder yields a line under
the current ticks, and the actual write happens when the screen receives
:attr:`save_chosen`. The Save As dialog is injected so tests never touch the platform.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..report.log_export import LEVELS, SOURCES, build_export_rows


# (display name, format value, file extension), in spec order — CSV first, so it is the initial
# filter of the Save As list, and that list is what decides the format.
_FORMATS = (("CSV", "csv", "csv"), ("JSON Lines", "jsonl", "jsonl"), ("Text", "text", "txt"))
_SAVE_FILTERS = ";;".join(f"{name} (*.{ext})" for name, _value, ext in _FORMATS)
_FORMAT_BY_EXT = {ext: value for _name, value, ext in _FORMATS}
_EXT_BY_FORMAT = {value: f".{ext}" for _name, value, ext in _FORMATS}


def _format_of_filter(selected: str) -> Optional[str]:
    """The format value behind a Save As filter string such as ``"JSON Lines (*.jsonl)"``."""
    return _FORMAT_BY_EXT.get(selected.rpartition("(*.")[2].rstrip(")").lower())


class ExportLogDialog(QDialog):
    """Level / Source / Owner checkboxes (all ticked each time the dialog opens) and an Export
    button that drives Save As. Save As' file-type list is the format choice.

    ``store`` and ``run_uuid`` let the button check the exporter's row builder; ``default_path``
    is the dialog's Save As start point (the run's ``…_log.csv``). ``file_dialog`` is a callable
    ``(title, default_path, filters) -> (path, selectedFilter) or None`` standing in for
    ``QFileDialog.getSaveFileName``."""

    #: A Save As result was accepted: (path, format, filters-as-exporter-keyword-args).
    save_chosen = pyqtSignal(str, str, dict)

    def __init__(
        self,
        parent: QWidget,
        store: Any,
        run_uuid: str,
        *,
        default_path: str,
        file_dialog: Optional[Callable[[str, str, str], Optional[str]]] = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.run_uuid = run_uuid
        self.default_path = default_path
        self._file_dialog = file_dialog or self._default_save_dialog

        self._level_boxes: dict[str, QCheckBox] = {}
        self._source_boxes: dict[str, QCheckBox] = {}
        self._owner_boxes: dict[str, QCheckBox] = {}

        layout = QVBoxLayout(self)
        for title, labels in (("Level", LEVELS), ("Source", SOURCES)):
            group = QGroupBox(title)
            group_layout = QVBoxLayout(group)
            boxes = self._level_boxes if title == "Level" else self._source_boxes
            for label in labels:
                box = QCheckBox(label)
                box.setChecked(True)
                box.stateChanged.connect(self._update_export_state)
                boxes[label] = box
                group_layout.addWidget(box)
            layout.addWidget(group)

        owner_group = QGroupBox("Owner")
        self._owner_layout = QVBoxLayout(owner_group)
        owner_group.setLayout(self._owner_layout)
        layout.addWidget(owner_group)

        self.export_button = QPushButton("Export")
        layout.addWidget(self.export_button)

        self.setWindowTitle("Export run log")
        self.export_button.clicked.connect(self._on_export)
        # The owner group is filled by the screen (then reset) before show; this keeps the button
        # state sane in case the dialog is shown from somewhere that skips both.
        self._update_export_state()

    def set_owner_choices(self, choices: list[tuple[str, str]]) -> None:
        """One checked checkbox per (owner key, label), in the session's tree order."""
        for key, label in choices:
            box = QCheckBox(label)
            box.setChecked(True)
            box.stateChanged.connect(self._update_export_state)
            self._owner_boxes[key] = box
            self._owner_layout.addWidget(box)
        self._update_export_state()

    def reset(self) -> None:
        """Tick every box — the dialog's state each time it opens — and refresh the Export button."""
        for boxes in (self._level_boxes, self._source_boxes, self._owner_boxes):
            for box in boxes.values():
                box.setChecked(True)
        self._update_export_state()

    def filters(self) -> dict[str, Optional[set]]:
        """The ticked groups as exporter filters: a fully ticked group is None (the exporter's "all")."""

        def _group(boxes: dict[str, QCheckBox]) -> Optional[set]:
            ticked = {key for key, box in boxes.items() if box.isChecked()}
            return None if ticked == set(boxes) else ticked

        return {
            "levels": _group(self._level_boxes),
            "sources": _group(self._source_boxes),
            "owners": _group(self._owner_boxes),
        }

    def _update_export_state(self, *_ignored: Any) -> None:
        """Enable Export only when the ticks would export at least one line (row builder check)."""
        try:
            self.export_button.setEnabled(bool(build_export_rows(self.store, self.run_uuid, **self.filters())))
        except ValueError:  # unknown run: nothing could be exported
            self.export_button.setEnabled(False)

    def _on_export(self) -> None:
        """Export: run Save As, then hand the chosen path / format / ticks to the screen.

        The format is the filter Save As reports back (spec 24b Saving); its extension is added
        only when the Operator typed none. Cancelling leaves this dialog open, ticks and all."""
        if not self.export_button.isEnabled():
            return
        choice = self._file_dialog("Export run log", self.default_path, _SAVE_FILTERS)
        path, selected = choice if isinstance(choice, tuple) else (choice, "")
        if not path:
            return  # Save As cancelled: the export dialog stays open, ticks and all
        target = Path(path)
        fmt = _format_of_filter(str(selected)) or _FORMAT_BY_EXT.get(target.suffix.lstrip(".").lower(), "csv")
        if not target.suffix:
            target = target.with_suffix(_EXT_BY_FORMAT[fmt])
        self.save_chosen.emit(str(target), fmt, self.filters())

    def _default_save_dialog(self, title: str, default_path: str, filters: str) -> Optional[tuple]:
        """The platform Save As, defaulting to the run's ``…_log.csv``; it returns
        ``(path, selectedFilter)`` or ``("", "")`` when cancelled."""
        return QFileDialog.getSaveFileName(self, title, default_path, filters)
