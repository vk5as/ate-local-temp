"""The widgets both Run-screen layouts share (packet 25a).

The staged screen (``staged_run_screen.py``) and the three-pane screen
(```three_pane_run_screen.py``) are two arrangements of the same parts: the Operator's fields, the
schedule picker, the step rows with their Step Inputs, the Stop-on-Fail prompt, the log panes and
the status bar. Everything that does not depend on where a part sits lives here, so a layout only
arranges widgets and forwards clicks to the run session — it holds no run logic of its own.

Like the layouts, nothing here decides anything about a run: every action is one call into the
:class:`~ate.run_session.headless.HeadlessRunSession`, and everything rendered is data the store
already recorded.
"""

from __future__ import annotations

import html

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from PyQt6.QtCore import QEvent, QObject, Qt, QSize, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFontMetrics
from PyQt6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QGroupBox,
    QVBoxLayout,
    QWidget,
    QDialog,
)

from ate.engine.step_inputs import StepInput, value_error

from ..catalog import find_definitions, include_map, list_schedule_mismatches, list_schedule_rows, module_map, resolve_schedule_definition, resolve_schedule_path
from ..engine.lua_host import LuaHost
from ..run_session.headless import HeadlessRunSession, SessionState


def _format_elapsed(delta: datetime) -> str:
    """Format a ``timedelta`` as ``MM:SS`` or ``HH:MM:SS`` for the run header."""
    total = int(delta.total_seconds())
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _format_log_line(line: dict) -> str:
    """Render one recorded log line as ``LEVEL  SOURCE  text`` for a pane."""
    level = str(line.get("level", ""))
    source = str(line.get("source", ""))
    return f"{level:<7} {source:<8} {line.get('text', '')}"


class LogPane(QWidget):
    """A scrollable pane that renders recorded log lines under a header label.

    Both the Run view's Log pane (ticket 08) and its separate Debug window are instances of
    this: one is fed ``INFO``-and-above lines, the other every level. It never decides what
    to show — :meth:`add_lines` only renders whatever store rows it is handed, in seq order.
    """

    def __init__(self, title: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._title = QLabel(title)
        self._title.setStyleSheet("font-weight: bold;")
        self._list = QListWidget()
        self._area = QScrollArea()
        self._area.setWidget(self._list)
        self._area.setWidgetResizable(True)
        layout.addWidget(self._title)
        layout.addWidget(self._area, stretch=1)

    def clear(self) -> None:
        """Remove every rendered line."""
        self._list.clear()

    def add_lines(self, lines: Iterable[dict]) -> None:
        """Render ``lines`` (each a log_line dict) as level/source/text rows in seq order."""
        for line in sorted(lines, key=lambda ln: ln.get("seq", 0)):
            self._list.addItem(QListWidgetItem(_format_log_line(line)))

    def rendered_lines(self) -> list[str]:
        """Return the pane's lines in order (for inspection and tests)."""
        return [self._list.item(i).text() for i in range(self._list.count())]


class StatusBar(QWidget):
    """The status bar every layout puts at the bottom of the screen (packet 14d).

    The mockup's ``statusBar()``: the run's state on the left, then the Pre-test / Steps /
    Post-test tallies side by side, then the total run time and the time in the current action
    or step. The bar paints nothing itself — the screen calls :meth:`set_progress` with the
    numbers it read from the session and the store.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setStyleSheet("background:#e4e4e4; border-top:1px solid #c4c4c4; padding:3px 10px;")
        self.status_label = QLabel("Ready")
        self.status_pre = QLabel("Pre-test 0/0")
        self.status_steps = QLabel("Steps 0/0")
        self.status_post = QLabel("Post-test 0/0")
        self.status_total = QLabel("Run --:--")
        self.status_current = QLabel("Current --:--")
        status_layout = QHBoxLayout(self)
        status_layout.addWidget(self.status_label)
        status_layout.insertStretch(1)  # the mockup's `.grow`: the tallies sit to the right
        for label in (self.status_pre, self.status_steps, self.status_post, self.status_total, self.status_current):
            status_layout.addWidget(label)

    def set_progress(
        self,
        phase: str,
        pre: tuple[int, int],
        steps_done: int,
        steps_total: int,
        post: tuple[int, int],
        started_at: Optional[datetime],
        item_started_at: Optional[datetime],
        now: datetime,
    ) -> None:
        """Write the state, the three tallies and both timers onto the bar."""
        self.status_label.setText(phase)
        self.status_pre.setText(f"Pre-test {pre[0]}/{pre[1]}")
        self.status_steps.setText(f"Steps {steps_done}/{steps_total}")
        self.status_post.setText(f"Post-test {post[0]}/{post[1]}")
        if started_at is None:
            self.status_total.setText("Run --:--")
            self.status_current.setText("Current --:--")
            return
        self.status_total.setText(f"Run {_format_elapsed(now - started_at)}")
        if item_started_at is None:
            self.status_current.setText("Current --:--")
        else:
            self.status_current.setText(f"Current {_format_elapsed(now - item_started_at)}")


#: The mockup's header row background (`.row.hdr` / `.b-row.hdr`), packet 15d.
_HEADER_BG = "#eef1f5"

#: A broken picker row, straight from the mockup: `.b-sched .it.broken` background and `.err` text.
_BROKEN_BG = "#fff5f5"
_BROKEN_ERR = "#b71c1c"
_MUTED_STYLE = "color: #777; font-size: 12px;"

#: `.b-sched .it.broken { cursor: not-allowed }` (mockup line 97): the pointer says "can't pick this".
_FORBIDDEN_CURSOR = Qt.CursorShape.ForbiddenCursor

#: The broken row's layout margins; the row-height calculation below has to add them back.
_ROW_MARGIN_X, _ROW_MARGIN_Y = 6, 4

#: The mockup's ``.flag`` box (amber), used for the mismatch flag line and the confirmation's file list.
_FLAG_STYLE = "background: #fff3cd; border: 1px solid #e0b100; color: #5c4300; padding: 4px 10px;"

#: The mockup's active input, `.inp.inp-on` (prototype line 54): `#fff6cc` background and an
#: `#e0a800` left border. A plain QWidget paints both once it opts into styled backgrounds.
_INPUT_ACTIVE_STYLE = "QWidget#step_input { background: #fff6cc; border-left: 3px solid #e0a800; }"

#: The mockup's `.err` (line 28): the inline message an input shows without submitting anything.
_INPUT_ERROR_STYLE = "color: #b71c1c; font-size: 12px;"

#: `.inp` has no background when it is not asking, so an inert input reads as the list's own white.
_INPUT_IDLE_STYLE = ""

#: `.b-sticky` (mockup line 108): the red banner the centre pane keeps at the top while paused.
_STICKY_STYLE = "background: #c62828; color: white; padding: 5px 10px; font-weight: 600;"

#: A paused step's row, the mockup's `.row.failed-hl` / `.b-row.failed-hl`.
_FAILED_BG = "#fff0f0"

#: The mockup's `.b-inline-prompt`: a red-outlined box hugging the failed step.
_INLINE_PROMPT_STYLE = (
    "FailurePromptBar { background: white; border: 2px solid #c62828; padding: 8px; }"
    "FailurePromptBar QLabel { color: #7b1113; }"
    "FailurePromptBar QPushButton { background: #f5f5f5; color: black; padding: 6px 16px; "
    "border: 1px solid #a9a9a9; border-radius: 3px; }"
    "FailurePromptBar QPushButton#retry { background: #0a64d6; color: white; border-color: #0850ad; }"
    "FailurePromptBar QPushButton#abort { background: #c62828; color: white; border-color: #8e1c1c; }"
)


def _row_label(number: object, name: object, depth: int) -> str:
    """A step list row's text: Step Number and name, indented two spaces per nesting level.

    Packet 15b: an expanded Sub-schedule's rows sit under their header, so the same Step Number
    in two instances still reads as two places in the tree. The mockup's styling is packet 15d's."""
    indent = "  " * depth
    number = str(number or "")
    name = str(name or "")
    return f"{indent}{number}  {name}" if number else f"{indent}{name}"


def _row_filter_text(row) -> str:
    """Everything the filter box matches a picker row against: name, release and error."""
    return f"{row.name or ''} {row.release or ''} {row.file or ''} {row.error or ''}".lower()


def _short_error(error: str, release_dir: Optional[Path]) -> str:
    """The one line a broken row shows: the message only, paths relative to their Media Release.

    A schedule that raises on load arrives as `failed to load <absolute path>: <file>:<line>: <msg>`
    followed by the Lua stack traceback, which repeats the file the row already names and prints the
    scan directory (packet 05b review). The row keeps the message; the full text is the tooltip."""
    line = (error or "").splitlines()[0]
    if line.startswith("failed to load "):
        line = line.split(": ", 1)[-1]
    if release_dir is not None:
        line = line.replace(f"{release_dir}/", "")
    return line


def _wrapped_height(text: str, font, width: int) -> int:
    """How tall word-wrapped `text` is when squeezed into `width` pixels with ``font``."""
    metrics = QFontMetrics(font)
    lines = 0
    for chunk in (text.splitlines() or [""]):
        used = ""
        for word in chunk.split():
            if used and metrics.horizontalAdvance(f"{used} {word}") > width:
                lines += 1
                used = word
            else:
                used = f"{used} {word}" if used else word
        lines += 1
    return lines * metrics.lineSpacing()


def _broken_row_size_hint(widget: QWidget, width: int) -> QSize:
    """The size a broken row needs at list width ``width``, with the `.err` line wrapped.

    `QLabel::sizeHint` knows nothing about the width the view will give the row, so a long error
    would sit in a one-line-tall row and be cut off at the viewport edge (packet 05b review). The
    wrapped height is counted here instead."""
    if width <= 0:
        return widget.sizeHint()
    # The row's own layout knows how tall its wrapped error is at this width (QLabel word wrap is
    # height-for-width); summing hand-counted lines came up short and clipped the last line.
    height = widget.heightForWidth(width) if widget.hasHeightForWidth() else -1
    return QSize(width, max(height, widget.sizeHint().height()))


class _ResizeBrokenRows(QObject):
    """Re-size the picker's broken rows whenever its viewport changes width.

    A wrapped error needs more lines on a narrower list, and an item's size hint is fixed when it is
    added, so without this a window resize (or a list laid out after its rows were built) clips the
    last lines of the error."""

    def __init__(self, list_widget: QListWidget) -> None:
        super().__init__(list_widget)
        self._list = list_widget

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt's name
        if event.type() == QEvent.Type.Resize:
            width = self._list.viewport().width()
            for row in range(self._list.count()):
                item = self._list.item(row)
                widget = self._list.itemWidget(item)
                if widget is not None:
                    item.setSizeHint(_broken_row_size_hint(widget, width))
        return False


def _broken_row_widget(name: str, release: str, error: str) -> QWidget:
    """The widget inside a broken picker row: name, Media Release, then the error in `.err` red.

    A list item cannot paint two colours on one row, so the mockup's three lines
    (`name`, `.muted` release, `.err` error) are three labels on a pale-red QWidget. The error label
    is named ``schedule_error`` so a test can read the exact colour it draws, and it word-wraps:
    the row is as wide as the list, so a long error must wrap rather than run past it."""
    widget = QWidget()
    widget.setStyleSheet(f"background: {_BROKEN_BG};")
    widget.setCursor(_FORBIDDEN_CURSOR)
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(_ROW_MARGIN_X, _ROW_MARGIN_Y, _ROW_MARGIN_X, _ROW_MARGIN_Y)
    title = QLabel(f"⛔ {name}")
    subtitle = QLabel(release)
    subtitle.setStyleSheet(_MUTED_STYLE)
    err = QLabel(error)
    err.setObjectName("schedule_error")
    err.setWordWrap(True)
    err.setStyleSheet(f"color: {_BROKEN_ERR}; font-size: 12px;")
    for label in (title, subtitle, err):
        label.setCursor(_FORBIDDEN_CURSOR)
        layout.addWidget(label)
    return widget


def _mismatch_row_widget(name: str, release: str, files: list[str]) -> QWidget:
    """A picker row for a schedule whose files mismatch: name, release, then the amber ⚠ line.

    The three-pane layout puts the mismatch note *in* the row (spec: "errors and mismatches shown
    inline"), which a single-colour list item cannot do, so the row is three labels on aQWidget
    styled like the mockup's `.flag`."""
    widget = QWidget()
    widget.setStyleSheet(f"background: #fff8e6;")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(_ROW_MARGIN_X, _ROW_MARGIN_Y, _ROW_MARGIN_X, _ROW_MARGIN_Y)
    title = QLabel(f"⚠ {name}")
    subtitle = QLabel(release)
    subtitle.setStyleSheet(_MUTED_STYLE)
    note = QLabel("⚠ checksum mismatch: " + ", ".join(files))
    note.setObjectName("schedule_mismatch")
    note.setWordWrap(True)
    note.setStyleSheet("color: #8a5a00; font-size: 12px;")
    for label in (title, subtitle, note):
        layout.addWidget(label)
    return widget


def _children_of_rows(rows: list[object], parent_of) -> dict[int, list[int]]:
    """Each row index's direct children, for both the step objects and the recorded node dicts."""
    children: dict[int, list[int]] = {}
    for index, row in enumerate(rows):
        parent = parent_of(row)
        if parent is not None:
            children.setdefault(int(parent), []).append(index)
    return children


def _rollup_text(row: int, children: dict[int, list[int]], is_header, result) -> str:
    """The mockup's header rollup: `N/M done`, plus `, K failed` when K > 0 (packet 15d).

    `M` is every Test Step beneath this header (nested headers included), `N` those with a Result,
    and `K` of those that FAILED or ERRORED. A header whose steps have not produced any Result yet
    shows nothing."""
    stack = list(children.get(row, ()))
    done = total = failed = 0
    while stack:
        seq = stack.pop()
        if is_header(seq):
            stack.extend(children.get(seq, ()))
            continue
        total += 1
        outcome = result(seq)
        if outcome:
            done += 1
            if outcome in ("FAIL", "ERROR"):
                failed += 1
    if not done:
        return ""
    return f"{done}/{total} done" + (f", {failed} failed" if failed else "")


def _node_depths(nodes: list[dict]) -> dict[object, int]:
    """Each recorded node's nesting depth, from its ``parent_node_seq`` (parents come first)."""
    depths: dict[object, int] = {}
    for node in nodes:
        parent = node.get("parent_node_seq")
        depths[node.get("node_seq")] = 0 if parent is None else depths.get(parent, 0) + 1
    return depths


def load_schedule_steps(session: HeadlessRunSession, schedule_name: str) -> list[object]:
    """The selected schedule's expanded step tree, read without running it (packets 15b, 15d).

    Both layouts preview the same tree: the header rows and the Test Steps beneath them, in runner
    seq order, each carrying its own Step Number, name, depth and validated Step Input
    declarations. No schedule is executed here — the LuaHost only loads the file."""
    config = session.config
    if config is None or not schedule_name:
        return []
    definition = resolve_schedule_definition(*config.test_definition_dirs, schedule_name=schedule_name)
    if definition is None:
        return []
    modules = module_map(definition)  # packet 06a: forward the release's require'd module map
    path = resolve_schedule_path(definition, schedule_name)
    if path is None:
        return []
    host = LuaHost(path, modules=modules, includes=include_map(definition))
    return list(host.tree)


def _short_sha(value: Optional[str]) -> str:
    """A SHA-256 abbreviated the way the mockup shows it (``3f9a0c…e1``); the full value goes in a tooltip."""
    if not value:
        return "none"
    return value if len(value) <= 9 else f"{value[:6]}…{value[-2:]}"


def _mismatch_flag_text(mismatches: list[tuple[str, Optional[str], str, str]], release: str = "") -> str:
    """The line the Run screen shows under the picker for a schedule with mismatched files.

    Names the Media Release (``MR-… · name version`` when known) and every file whose bytes differ
    from its Test Definition checksum, matching the mockup's flag: `⚠ Checksum mismatch in <release>:
    <files>. The files on disk are not the released files.` Empty when nothing mismatched."""
    if not mismatches:
        return ""
    release = release or mismatches[0][3] or "the Test Definition"
    files = ", ".join(rel_path for rel_path, _expected, _computed, _media in mismatches)
    return f"⚠ Checksum mismatch in {release}: {files}. The files on disk are not the released files."


class StepEnabler:
    """The enable state of an expanded step tree, and the header rules over it (packets 11b, 15d).

    Both layouts need the same answers: whether a step is on (the Operator's override beating the
    row's :setEnable() default), what a header's tri-state box reads, and what a header click
    changes. Only the widget differs — a list item's check box on the staged screen, a QCheckBox in
    a table cell on the three-pane one — so the rules live here and each view just paints them.
    Every toggle is one ``session.toggle_node`` call; the enabler decides nothing about the run."""

    def __init__(self, steps: Iterable[object], session: Optional[HeadlessRunSession] = None) -> None:
        self.rows = list(steps)
        self.session = session
        self.children = _children_of_rows(self.rows, lambda row: getattr(row, "parent", None))
        overrides = session.node_enables if session is not None else None
        self.overrides: dict[int, bool] = dict(overrides) if overrides else {}
        # Each row's last-seen state, so a re-sync is not read as an Operator toggle.
        self.states = {
            index: self._derived(index) for index in range(len(self.rows))
        }

    def is_header(self, row: int) -> bool:
        """Whether the row at ``row`` is an include header rather than a Test Step."""
        return bool(getattr(self.rows[row], "is_header", False))

    def effective(self, row: int) -> bool:
        """Whether row ``row``'s box is on: the Operator's override if there is one, else the default."""
        step = self.rows[row]
        default = bool(getattr(step, "default_enabled", getattr(step, "enabled", True)))
        return self.overrides.get(row, default)

    def subtree(self, row: int) -> list[int]:
        """Every row beneath the header at ``row``, depth first — headers and Test Steps alike."""
        rows: list[int] = []
        stack = list(self.children.get(row, ()))
        while stack:
            child = stack.pop(0)
            rows.append(child)
            stack.extend(self.children.get(child, ()))
        return rows

    def header_state(self, row: int) -> Qt.CheckState:
        """A header's box, derived from the steps beneath it (packet 15d, the mockup's enableBox).

        Checked when every one is on, Unchecked when none is, PartiallyChecked when the states are
        mixed. A header with no Test Steps at all has nothing to disable, so it reads Checked, as
        in the mockup."""
        beneath = [r for r in self.subtree(row) if not self.is_header(r)]
        on = sum(self.effective(r) for r in beneath)
        if on == len(beneath):
            return Qt.CheckState.Checked
        if on == 0:
            return Qt.CheckState.Unchecked
        return Qt.CheckState.PartiallyChecked

    def headers_above(self, row: int) -> list[int]:
        """The header rows enclosing ``row``, innermost first, whose box must follow its children."""
        headers: list[int] = []
        parent = getattr(self.rows[row], "parent", None)
        while parent is not None:
            if self.is_header(parent):
                headers.append(parent)
            parent = getattr(self.rows[parent], "parent", None)
        return headers

    def _derived(self, row: int) -> bool:
        """The state to remember for a row: a header reads Checked only when all its steps are on."""
        if self.is_header(row):
            return self.header_state(row) == Qt.CheckState.Checked
        return self.effective(row)

    def box_state(self, row: int) -> Qt.CheckState:
        """The state to paint on a row's box: derived for a header, the step's own state otherwise."""
        if self.is_header(row):
            return self.header_state(row)
        return Qt.CheckState.Checked if self.effective(row) else Qt.CheckState.Unchecked

    def toggle(self, row: int, checked: bool) -> list[int]:
        """Apply one Operator toggle at ``row`` and name the rows whose box must be re-painted.

        A header click is not an override of its own row: it sets every Test Step beneath it, one
        ``toggle_node`` call each (packet 15d), and the header's box — and every header above it —
        then reads back from those steps. Returns the rows the caller has to re-sync: the steps and
        nested headers beneath a clicked header, plus the headers enclosing any toggle."""
        if self.is_header(row):
            beneath = self.subtree(row)
            for child in beneath:
                if self.is_header(child):
                    continue
                self.overrides[child] = checked
                self.states[child] = checked
                if self.session is not None:
                    self.session.toggle_node(child, checked)
            for header in beneath + self.headers_above(row):
                if self.is_header(header):
                    self.states[header] = self._derived(header)
            return beneath + self.headers_above(row)
        self.overrides[row] = checked
        self.states[row] = checked
        if self.session is not None:
            self.session.toggle_node(row, checked)
        return self.headers_above(row)


class ResultCounts(QWidget):
    """The live `Result` tallies the left pane shows while a run is up (spec line 48).

    The mockup's ``counts()``: one row per Result value — PASS, FAIL, ERROR, SKIPPED, NOTRUN,
    FORCED_NOTRUN, NOREPORT — with the number of steps that recorded it. The widget only writes the
    numbers it is handed; the screen counts them from the store's node rows."""

    RESULT_KEYS = ("PASS", "FAIL", "ERROR", "SKIPPED", "NOTRUN", "FORCED_NOTRUN", "NOREPORT")

    def __init__(self) -> None:
        super().__init__()
        group = QGroupBox("Results")
        grid = QGridLayout(group)
        grid.setContentsMargins(8, 8, 8, 8)
        self.count_labels: dict[str, QLabel] = {}
        for index, key in enumerate(self.RESULT_KEYS):
            name = QLabel(key)
            name.setObjectName(f"count_{key}")
            count = QLabel("0")
            count.setObjectName(f"count_{key}_value")
            grid.addWidget(name, index, 0)
            grid.addWidget(count, index, 1)
            self.count_labels[key] = count
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(group)

    def set_counts(self, counts: dict[str, int]) -> None:
        """Write one tally per Result value; anything not counted reads 0."""
        for key in self.RESULT_KEYS:
            self.count_labels[key].setText(str(int(counts.get(key, 0))))

    def counts(self) -> dict[str, int]:
        """The tallies as they are on screen, for tests to read."""
        return {key: int(self.count_labels[key].text() or 0) for key in self.RESULT_KEYS}


class RunFieldsForm(QWidget):
    """The Operator's four required fields: Project, Operator, UUT Type and Serial.

    Both layouts draw the same form; only the surrounding stage differs. Every label is the buddy
    of its input (30a review item 14), and the edits are public so the screen can read them and
    prefill Project from the Station's ``defaultProject``."""

    def __init__(self) -> None:
        super().__init__()
        layout = QGridLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.operator_edit = QLineEdit()
        self.project_edit = QLineEdit()
        self.uut_type_edit = QLineEdit()
        self.serial_edit = QLineEdit()

        self.operator_label = QLabel("Operator")
        self.project_label = QLabel("Project")
        self.uut_type_label = QLabel("UUT Type")
        self.serial_label = QLabel("Serial")

        # Mockup order and placeholders: Project is prefilled from the station, so it carries
        # no placeholder; Operator, UUT Type and Serial are marked "required" in the mockup.
        rows = [
            (self.project_label, self.project_edit, ""),
            (self.operator_label, self.operator_edit, "required"),
            (self.uut_type_label, self.uut_type_edit, "required"),
            (self.serial_label, self.serial_edit, "required"),
        ]
        for index, (label, edit, placeholder) in enumerate(rows):
            label.setBuddy(edit)
            if placeholder:
                edit.setPlaceholderText(placeholder)
            layout.addWidget(label, index, 0)
            layout.addWidget(edit, index, 1)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)


class SchedulePicker(QWidget):
    """The filterable Test Schedule list, its errors and its mismatch notes (packets 05b, 06c).

    ``schedule_list`` is populated from the catalog; ``rescan_button`` reloads it; ``filter_edit``
    narrows the list to the rows whose name, release or error contains what the Operator typed; and
    ``notes_label`` shows the description of whatever row is selected. Selecting a row records it on
    the session and emits ``selection_changed`` so the screen can enable Start. A broken row carries
    no name in its user data, so it can never reach the session.

    ``inline_mismatches`` draws the ⚠ note inside the row (the three-pane picker, spec line 48);
    the staged picker keeps the note under the list instead, where ``mismatch_label`` shows it."""

    selection_changed = pyqtSignal(str)  # the exact schedule name, or "" when nothing runnable is current

    def __init__(self, session: HeadlessRunSession, inline_mismatches: bool = False) -> None:
        super().__init__()
        self.session = session
        self.inline_mismatches = inline_mismatches
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        filter_row = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter schedules")
        self.rescan_button = QPushButton("Rescan schedules")
        filter_row.addWidget(self.filter_edit, stretch=1)
        filter_row.addWidget(self.rescan_button)

        self.schedule_list = QListWidget()
        self.schedule_list.viewport().installEventFilter(_ResizeBrokenRows(self.schedule_list))
        self.notes_label = QLabel()
        # The ⚠ line naming the mismatched files of the selected schedule (ticket 06c); empty when
        # the schedule's checksums all match.
        self.mismatch_label = QLabel("")

        layout.addLayout(filter_row)
        layout.addWidget(self.schedule_list)
        layout.addWidget(self.notes_label)
        layout.addWidget(self.mismatch_label)

        # Every catalog row the picker shows, runnable and broken (packet 05b); the filter renders
        # a subset of it without rescanning the directory.
        self._rows: list[object] = []
        self._mismatch_flags: dict[str, list[tuple[str, Optional[str], str, str]]] = {}
        self._release_labels: dict[str, str] = {}
        self._release_dirs: dict[str, Path] = {}

        # Both the selection and the current item are watched: a broken row cannot be selected, yet
        # it can still be made current, and then nothing stale may be left showing (05b review).
        self.schedule_list.itemSelectionChanged.connect(self._on_selection)
        self.schedule_list.currentItemChanged.connect(self._on_selection)
        self.filter_edit.textChanged.connect(self.render)
        self.rescan_button.clicked.connect(self.refresh)

    @property
    def mismatch_flags(self) -> dict[str, list[tuple[str, Optional[str], str, str]]]:
        """The mismatched files of every schedule the picker listed."""
        return self._mismatch_flags

    @property
    def release_labels(self) -> dict[str, str]:
        """``media number -> "MR-10422 · name version"`` for the releases the picker showed."""
        return self._release_labels

    def selected_name(self) -> Optional[str]:
        """The exact schedule name the picker's current row stands for, or ``None`` if it can't run.

        A broken row carries no name in its user data, so it can never be the selection a run starts
        from even if something bypasses its flags and makes it current (packet 05b)."""
        item = self.schedule_list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole) or None

    def refresh(self) -> None:
        """Populate the schedule list from the catalog and clear the notes label.

        A schedule whose Media Release has a checksum mismatch gets a ⚠ prefix in the row (ticket 06c)
        so the Operator can see it before picking it. Broken Test Definitions and Test Schedules are
        listed too, each with its error, after the usable rows (packet 05b): the picker shows every
        row the catalog found, not only the runnable ones."""
        config = self.session.config
        if config is None:
            return
        self._rows = list(list_schedule_rows(*config.test_definition_dirs))
        self._mismatch_flags = list_schedule_mismatches(*config.test_definition_dirs)
        definitions = find_definitions(*config.test_definition_dirs)
        # "MR-10422 · PSU-200 Tests 2.3.0", as the mockup names a release in the flag and the dialog.
        self._release_labels = {d.media_number: f"{d.media_number} · {d.name} {d.version}" for d in definitions}
        # Where each Media Release is on disk: a load error's absolute path is written relative to it.
        self._release_dirs = {d.media_number: d.path for d in definitions}
        self.render()

    def render(self) -> None:
        """Rebuild the picker from the catalog rows, keeping only the rows the filter matches.

        Usable rows come first in scan order and the broken rows follow them at the end of the list:
        they cannot be picked, so they sit below everything the Operator can still start from. The
        picked schedule, if the filter still shows it, is re-selected so Start stays enabled across a
        filter change; when the filter hides it, the notes and the selection are cleared."""
        term = self.filter_edit.text().strip().lower()
        list_widget = self.schedule_list
        # The pick has to be read before the list is emptied: clearing it makes the current item
        # None, which the selection handler reads as "nothing runnable is current".
        chosen = self.session.selected_schedule
        # `clear()` leaves every `setItemWidget` widget behind as a child of the viewport, so each
        # rescan would add one orphan row per broken row (packet 05b review): hand them back to Qt
        # and delete them before the list is emptied.
        for row in range(list_widget.count()):
            item = list_widget.item(row)
            widget = list_widget.itemWidget(item)
            if widget is None:
                continue
            list_widget.removeItemWidget(item)
            widget.setParent(None)
            widget.deleteLater()
        list_widget.clear()
        picked: Optional[QListWidgetItem] = None
        usable = [row for row in self._rows if row.error is None]
        broken = [row for row in self._rows if row.error is not None]
        for row in usable + broken:
            if term and term not in _row_filter_text(row):
                continue
            item = self._make_item(list_widget, row)
            if chosen and row.name == chosen:
                picked = item
        if picked is not None:
            list_widget.setCurrentItem(picked)
        else:
            self._clear_selection()
        self.selection_changed.emit(self.selected_name() or "")

    def _make_item(self, list_widget: QListWidget, row) -> QListWidgetItem:
        """Add one catalog row to the picker and return its item.

        A usable row is unchanged from ticket 06c: the ⚠ flag, the schedule name and its description,
        and the exact name in the user data for the selection to read back. A broken row gets the
        mockup's `.it.broken` look and no user data, so it can never be handed to the session. Its
        error line is the short form (first line, paths relative to the release); the tooltip holds
        the whole text."""
        if row.error is None:
            flag = "⚠ " if row.name in self._mismatch_flags else ""
            item = QListWidgetItem(f"{flag}{row.name} — {row.description}")
            # Carry the exact selected name so a later selection can hand it to the session.
            item.setData(Qt.ItemDataRole.UserRole, row.name)
            item.setToolTip(row.media_number or "")
            list_widget.addItem(item)
            if self.inline_mismatches and row.name in self._mismatch_flags:
                # The three-pane picker names the mismatched files inside the row itself.
                widget = _mismatch_row_widget(
                    row.name,
                    self._release_labels.get(row.media_number, row.media_number or ""),
                    [rel_path for rel_path, _e, _c, _m in self._mismatch_flags[row.name]],
                )
                item.setText("")
                item.setSizeHint(_broken_row_size_hint(widget, list_widget.viewport().width()))
                list_widget.setItemWidget(item, widget)
            return item
        # A broken Test Schedule has no ScheduleParams name to show, so its file names the row; a
        # broken Test Definition has neither, and the mockup calls it "(Test Definition broken)".
        name = row.file or "(Test Definition broken)"
        release = row.release or row.media_number
        error = _short_error(row.error, self._release_dirs.get(row.media_number))
        item = QListWidgetItem(f"⛔ {name}\n{release}\n{error}")
        item.setBackground(QColor(_BROKEN_BG))
        # Not selectable and not enabled: a click can't pick it, and Qt won't even make it current.
        # UserRole is deliberately left empty so no path can pass a broken row to the session.
        item.setFlags(item.flags() & ~(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled))
        item.setData(Qt.ItemDataRole.UserRole + 1, row.error)
        item.setToolTip(row.error)
        widget = _broken_row_widget(name, release, error)
        # The row is as tall as its three wrapped lines need to be, not one line per label.
        item.setSizeHint(_broken_row_size_hint(widget, list_widget.viewport().width()))
        list_widget.addItem(item)
        list_widget.setItemWidget(item, widget)
        return item

    def _on_selection(self) -> None:
        """Record the newly selected schedule on the session, or clear the picker.

        The exact name is read back from the row's user data (set when the list was built), so this
        hands the session precisely what the Operator picked; ``select_schedule`` then moves the
        session to SELECTING while idle. A broken row carries no user data, so it can never reach the
        session: the previous choice is dropped, the notes and the mismatch flag are emptied, and
        Start stays disabled (packet 05b review)."""
        item = self.schedule_list.currentItem()
        name = (item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""
        if name:
            self.session.select_schedule(name)
            self.notes_label.setText(item.text())
            # Flag the mismatch under the picker as soon as the schedule is picked (ticket 06c).
            flagged = self._mismatch_flags.get(name, [])
            label = self.mismatch_label
            label.setText(_mismatch_flag_text(flagged, self._release_labels.get(flagged[0][3], "") if flagged else ""))
            label.setStyleSheet(_FLAG_STYLE if flagged else "")
        else:
            self._clear_selection()
        self.selection_changed.emit(name)

    def _clear_selection(self) -> None:
        """Nothing runnable is current — a broken row, or the picked row filtered away — so show nothing.

        The notes and the mismatch line describe the schedule that *was* picked; leaving them filled
        reads as if it were still the selection, and the session would still start a run from it
        (packet 05b review). A run already in progress keeps its own selection."""
        self.notes_label.setText("")
        self.mismatch_label.setText("")
        self.mismatch_label.setStyleSheet("")
        if self.session.selected_schedule and self.session.state != SessionState.STARTED:
            self.session.clear_selection()


class FailurePromptBar(QWidget):
    """The Stop-on-Fail prompt shown while a failed step awaits the Operator's choice.

    Rendered only while a prompt is pending — the layout shows it with :meth:`show_prompt` and it
    hides itself once a choice is made — and it holds no run logic of its own: each button only
    hides the bar and emits ``choice_made`` with CONTINUE, RETRY or ABORT, which the layout sends on
    to the session. The look follows the run-screen mockup: a bold ``Step <no> <result>`` label, the
    step's message growing across the bar, then the three buttons in order. ``style`` picks the
    mockup's full-width bar (variant C) or the box hugging the failed step (variant B)."""

    choice_made = pyqtSignal(str)

    def __init__(self, style: str = "") -> None:
        super().__init__()
        # A plain QWidget never paints its stylesheet background, so the bar showed up grey
        # (packet 12d review): opt it into styled backgrounds, and give the labels their own
        # colour rule because a stylesheet `color` does not cascade to child QLabels.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(12)

        self.step_label = QLabel()
        bold = self.step_label.font()
        bold.setBold(True)
        self.step_label.setFont(bold)
        self.message_label = QLabel()
        # The message grows to fill whatever width the labels leave, like the mockup's .grow.
        self.message_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self.continue_button = QPushButton("Continue")
        self.continue_button.setToolTip("Keep the result and move on")
        self.retry_button = QPushButton("Retry")
        self.retry_button.setToolTip("Run the step's action again")
        self.abort_button = QPushButton("Abort")
        self.abort_button.setToolTip("Run the Post-test Actions and end the run")
        # Red bar, 15px type, and the mockup's button classes: plain Continue, primary Retry,
        # danger Abort.
        self.setStyleSheet(
            style
            or (
                "FailurePromptBar { background: #c62828; color: white; font-size: 15px; }"
                "FailurePromptBar QLabel { color: white; }"
                "FailurePromptBar QPushButton { background: #f5f5f5; color: black; font-size: 15px; "
                "padding: 6px 18px; border: 1px solid #a9a9a9; border-radius: 3px; }"
                "FailurePromptBar QPushButton:hover { background: #e6e6e6; }"
                "FailurePromptBar QPushButton#retry { background: #0a64d6; color: white; border-color: #0850ad; }"
                "FailurePromptBar QPushButton#abort { background: #c62828; color: white; border-color: #8e1c1c; }"
            )
        )
        self.retry_button.setObjectName("retry")
        self.abort_button.setObjectName("abort")

        self.continue_button.clicked.connect(lambda: self._on_choice("CONTINUE"))
        self.retry_button.clicked.connect(lambda: self._on_choice("RETRY"))
        self.abort_button.clicked.connect(lambda: self._on_choice("ABORT"))

        layout.addWidget(self.step_label)
        layout.addWidget(self.message_label)
        layout.addWidget(self.continue_button)
        layout.addWidget(self.retry_button)
        layout.addWidget(self.abort_button)

        # Only shown while a prompt is pending; the layout shows it via show_prompt.
        self.hide()

    def show_prompt(self, step_number: str, result: str, message: str) -> None:
        """Show the bar naming the failed step and its message, for the Operator to answer."""
        self.step_label.setText(f"Step {step_number} {result}")
        self.message_label.setText(message)
        self.show()

    def _on_choice(self, choice: str) -> None:
        """A button was clicked: hide the bar first, then hand the choice to the layout."""
        if not self.isVisible():
            return  # a second click after the bar already hid itself must not answer twice
        self.hide()
        self.choice_made.emit(choice)


class StepInputWidget(QWidget):
    """One declared Step Input, drawn under the step that declared it (ticket 10a).

    The widget is a pure view of a :class:`~ate.engine.step_inputs.StepInput` and of the run session:
    it decides nothing. Every answer is one call — ``session.supply_input`` for a blocking type,
    ``session.click_poll`` for a POLL — and whether it is live at all is the session's doing: the
    layout enables it when ``input_asked`` announces this step's n-th input and disables it again when
    ``input_answered`` reports the answer, matching the run-screen spec ("a blocking input is enabled
    only while its ``ATE.input(n)`` call waits; a POLL only while its step runs").

    Rendering follows the mockup's ``inputHTML``: ``RADIOBUT`` is one radio per option plus OK,
    ``CHECKBOX`` a checkbox per option plus OK, ``PUSHBUT``/``PROMPT``/``POLL`` one button per
    choice, and ``INTEGER``/``FLOAT``/``STRING`` a line edit plus OK. ``orient`` stacks the controls
    vertically or lays them out in a row, ``tooltip`` is the hover text, and an answered input shows
    its value: the mockup's `chose:`/`clicked:` note for the button types, and for the others the
    state the controls already carry (a ticked box, typed text)."""

    def __init__(self, declaration: StepInput, session: HeadlessRunSession, node_seq: int) -> None:
        super().__init__()
        self.declaration = declaration
        self.session = session
        self.node_seq = node_seq
        self.index = declaration.index  # the 1-based n in ATE.input(n)
        self.blocking = declaration.type != "POLL"
        #: The Attempt this input was last asked for; a Retry re-asks the same index (packet 12c).
        self.attempt_no = 1

        #: The list row this widget is attached to, set by _append_input_rows; None while detached.
        self.input_item: Optional[QListWidgetItem] = None

        self.radios: list[QRadioButton] = []
        self.checks: list[QCheckBox] = []
        self.choice_buttons: list[QPushButton] = []
        self.line_edit: Optional[QLineEdit] = None
        self.ok_button: Optional[QPushButton] = None
        self.controls: list[QWidget] = []

        self.setObjectName("step_input")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        if declaration.tooltip:
            self.setToolTip(declaration.tooltip)

        outer = QHBoxLayout(self)
        # The mockup's `.inputs` block is indented 26 px under its step; with the row item text
        # empty, the indent has to come from the widget's own left margin (26 + the 6 px padding).
        outer.setContentsMargins(32, 2, 6, 2)
        outer.setSpacing(8)
        outer.addWidget(QLabel(f"{self.index}. {declaration.desc}"))  # the mockup's .inp-desc

        # .inp-w: the controls. orient=VERT stacks them (`.inp.vert .inp-w`), otherwise they sit in a
        # row, exactly as the mockup's flex layout does.
        self._controls_widget = QWidget()
        self._controls_widget.setObjectName("inp_w")
        controls = (
            QVBoxLayout(self._controls_widget)
            if declaration.orient == "VERT"
            else QHBoxLayout(self._controls_widget)
        )
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)

        if declaration.type == "ENUM" and declaration.widget == "RADIOBUT":
            self.radios = [QRadioButton(label) for label in declaration.enum]
            for radio in self.radios:
                controls.addWidget(radio)
                self.controls.append(radio)
            self._add_ok(lambda: self._submit(next((r.text() for r in self.radios if r.isChecked()), None)))
        elif declaration.type == "ENUM" and declaration.widget == "CHECKBOX":
            self.checks = [QCheckBox(label) for label in declaration.enum]
            for check in self.checks:
                controls.addWidget(check)
                self.controls.append(check)
            self._add_ok(lambda: self._submit([c.text() for c in self.checks if c.isChecked()]))
        elif declaration.type == "ENUM":  # PUSHBUT: clicking a choice is the answer
            for label in declaration.enum:
                self._add_button(label, lambda *_seen, label=label: self._submit(label))
        elif declaration.type in ("INTEGER", "FLOAT", "STRING"):
            # `.inp input[type=text] { width: 140px }` — the line edit is one of the controls,
            # not a decoration: it must sit in the layout (packet 10a review).
            self.line_edit = QLineEdit()
            self.line_edit.setFixedWidth(140)
            self.line_edit.setPlaceholderText(self._placeholder())
            self.line_edit.returnPressed.connect(lambda: self._submit(self.line_edit.text()))
            controls.addWidget(self.line_edit)
            self.controls.append(self.line_edit)
            self._add_ok(lambda: self._submit(self.line_edit.text()))
        else:  # PROMPT and POLL: one button per prompt text
            for text in declaration.prompt:
                if declaration.type == "POLL":
                    self._add_button(text, lambda *_seen, text=text: self._click_poll(text))
                else:
                    self._add_button(text, lambda *_seen, text=text: self._submit(text))
        # The mockup's `.inp-w` items are content-sized: let the leftover row width fall after
        # them rather than stretching each control (packet 14d made the view wider, and a
        # stretched radio only answers clicks on its own indicator and label).
        controls.addStretch()
        outer.addWidget(self._controls_widget, stretch=1)
        self.value_label = QLabel("")
        self.value_label.setObjectName("input_value")
        self.value_label.setStyleSheet(_MUTED_STYLE)
        self.error_label = QLabel("")
        self.error_label.setObjectName("input_error")
        self.error_label.setStyleSheet(_INPUT_ERROR_STYLE)
        outer.addWidget(self.value_label)
        outer.addWidget(self.error_label)
        if declaration.type == "POLL":
            # The mockup's POLL row: `clicked: ${inp.poll ?? '(none)'}` before the first click.
            self.value_label.setText("clicked: (none)")

        # Always visible, inert until asked: the session decides when this input is live.
        self.deactivate()

    # -- rendering helpers --------------------------------------------------

    def _placeholder(self) -> str:
        """The line edit's hint: its range, else its pattern, as the mockup shows it."""
        decl = self.declaration
        if decl.range:
            return f"{decl.range['min']} to {decl.range['max']}"
        if decl.pattern:
            return decl.tooltip or decl.pattern
        return ""

    def _add_ok(self, answer: Callable[[], Any]) -> None:
        """The OK button every line-edit and pick-one widget has."""
        self.ok_button = QPushButton("OK")
        # `.inp-w` gives a button its content width; it must not stretch to the row (review).
        self.ok_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.ok_button.clicked.connect(lambda *_seen: answer())
        self.controls.append(self.ok_button)
        self._controls_widget.layout().addWidget(self.ok_button)

    def _add_button(self, text: str, answer: Callable[[], None]) -> None:
        """A PUSHBUT/PROMPT/POLL button: clicking it is the whole interaction."""
        button = QPushButton(text)
        button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        button.clicked.connect(answer)
        self.choice_buttons.append(button)
        self.controls.append(button)
        self._controls_widget.layout().addWidget(button)

    # -- state, driven entirely by the session ----------------------------

    def activate(self, attempt_no: int) -> None:
        """This input is being asked for: enable it, give it the mockup's active look, focus it.

        The mockup's `focusActive` puts the cursor in whichever input is currently waiting, so the
        Operator can type without reaching for the mouse."""
        self.attempt_no = attempt_no
        self.error_label.setText("")
        for control in self.controls:
            control.setEnabled(True)
        self.setStyleSheet(_INPUT_ACTIVE_STYLE)
        # The mockup's `focusActive`: the waiting line edit takes the cursor, so Enter submits;
        # a widget without one focuses its first control.
        if self.line_edit is not None:
            self.line_edit.setFocus()
        elif self.controls:
            self.controls[0].setFocus()

    def deactivate(self) -> None:
        """Not being asked for: every control is disabled and the row has no active styling."""
        self.setStyleSheet(_INPUT_IDLE_STYLE)
        for control in self.controls:
            control.setEnabled(False)

    def answer(self, value: Any) -> None:
        """Show the recorded answer; a blocking input then closes, a POLL stays open.

        A blocking type is asked once, so it deactivates. A POLL is clickable for as long as its
        step runs: every click updates the mockup's `clicked:` note and the button stays live
        until the step ends (packet 10a review)."""
        decl = self.declaration
        if decl.type == "POLL":
            self.value_label.setText(f"clicked: {value}")
            return
        if self.radios:
            for radio in self.radios:
                radio.setChecked(radio.text() == value)
        elif self.checks:
            chosen = set(value if isinstance(value, (list, tuple)) else [value])
            for check in self.checks:
                check.setChecked(check.text() in chosen)
        elif self.line_edit is not None:
            self.line_edit.setText("" if value is None else str(value))
        if decl.type in ("PUSHBUT", "PROMPT") or (decl.type == "ENUM" and decl.widget == "PUSHBUT"):
            self.value_label.setText(f"chose: {value}")
        self.deactivate()

    # -- the only calls out ----------------------------------------------

    def _submit(self, value: Any) -> None:
        """Hand one blocking answer to the session — after step_inputs says it is acceptable.

        A value that fails the declaration's own range/pattern is shown as the mockup's inline error
        and never reaches the session, so the step keeps waiting."""
        error = value_error(self.declaration, value)
        if error:
            self.error_label.setText(error)
            return
        try:
            self.session.supply_input(self.node_seq, self.attempt_no, self.index, value)
        except ValueError as exc:
            # The session says this input is not waiting (or the value is not acceptable): show it
            # rather than crashing the click, and the widget stays as it is (10a review).
            self.error_label.setText(str(exc))

    def _click_poll(self, text: str) -> None:
        """Record a POLL click through the session; the sticky value is the button's text."""
        try:
            self.session.click_poll(self.node_seq, self.attempt_no, self.index, text)
        except ValueError as exc:
            self.error_label.setText(str(exc))


class MismatchConfirmation(QDialog):
    """The run-start confirmation a checksum mismatch requires (ticket 06c).

    It lists each file as ``path expected …, found …``. Only when
    ``database.storeMismatchedFiles`` is ``ask`` does it offer the "Store mismatched files with this
    Test Run" checkbox, ticked by default; `always` and `never` just state what will happen.
    ``ok_button``/``cancel_button`` are public so a test can decide without ``exec()``: clicking OK
    records the choice and :meth:`store_choice` then reports it (`None` when cancelled)."""

    def __init__(
        self,
        schedule_name: str,
        release: str,
        mismatches: list[tuple[str, Optional[str], str, str]],
        store_setting: str = "ask",
        run_details: str = "",
    ) -> None:
        super().__init__()
        self.setWindowTitle("Start Test Run")
        self.setMinimumWidth(460)  # room for "path expected …, found …" on one line
        self.schedule_name = schedule_name
        self.release = release
        self.mismatches = list(mismatches)
        self.store_setting = store_setting
        self._accepted = False

        layout = QVBoxLayout(self)
        header = QLabel(f"<b>{html.escape(schedule_name)}</b><br><span style='color:#666'>{html.escape(release)}</span>")
        layout.addWidget(header)
        if run_details:  # the mockup's "UUT …, Project …, Operator …" line
            layout.addWidget(QLabel(run_details))
        lines = "\n".join(
            f"{rel_path} expected {_short_sha(expected)}, found {_short_sha(computed)}"
            for rel_path, expected, computed, _media in self.mismatches
        )
        self.file_lines = QLabel(f"⚠ These files do not match their Test Definition checksums:\n{lines}")
        self.file_lines.setStyleSheet(_FLAG_STYLE)
        self.file_lines.setWordWrap(True)
        self.file_lines.setToolTip("\n".join(
            f"{rel_path}\n  expected {expected or 'none'}\n  found    {computed}"
            for rel_path, expected, computed, _media in self.mismatches
        ))
        layout.addWidget(self.file_lines)

        if store_setting == "ask":
            self.store_checkbox = QCheckBox("Store mismatched files with this Test Run")
            self.store_checkbox.setChecked(True)
            layout.addWidget(self.store_checkbox)
            self.store_note = None
        else:
            # Nothing to ask: the setting already decided, so the dialog only says so.
            self.store_checkbox = None
            self.store_note = QLabel(
                "Mismatched files will be stored with this Test Run."
                if store_setting == "always"
                else "Mismatched files will not be stored with this Test Run."
            )
            layout.addWidget(self.store_note)

        self.cancel_button = QPushButton("Cancel")
        self.ok_button = QPushButton("Start")
        self.ok_button.setDefault(True)  # the mockup's primary button
        buttons = QHBoxLayout()
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.ok_button)
        layout.addLayout(buttons)

        # accept()/reject() work without exec(), which is what keeps the dialog testable.
        self.accepted.connect(lambda: setattr(self, "_accepted", True))
        self.ok_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)

    def is_accepted(self) -> bool:
        """True once Start was clicked; False if Cancel was (or nothing was)."""
        return self._accepted

    def store_choice(self) -> Optional[bool]:
        """Whether this run stores the mismatched bytes, or ``None`` if the run was cancelled."""
        if not self._accepted:
            return None
        if self.store_checkbox is not None:
            return self.store_checkbox.isChecked()
        return self.store_setting == "always"
