"""The staged Run screen: ticket 03's Operator-facing layout.

This is the only place that binds a :class:`~ate.run_session.headless.HeadlessRunSession`
to widgets. It renders one short schedule through three ordered setup stages and then a
Run view, but it holds no run logic of its own — every action it takes is a call into an
existing interface:

* the Operator's fields go to ``session.set_operator/project/uut_type/serial``;
* picking a schedule goes to ``session.select_schedule``;
* starting goes to ``session.start()`` which returns a :class:`~ate.engine.run_engine.RunEngine`;
* results come back through ``session.store.nodes(run_uuid)`` and
  ``session.store.attempts(run_uuid)`` (ticket 03's two child-read methods).

The layout never imports the engine or run session, so the architectural guard in
``tests/test_architecture_rules.py`` keeps passing; it only ever *reads* those layers.
Because every update rides a Qt signal (or a timer), the worker thread is never blocked
and the GUI stays responsive while a schedule runs.
"""

from __future__ import annotations

import html

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from PyQt6.QtCore import QEvent, QObject, Qt, QSize, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFontMetrics
from PyQt6.QtWidgets import (
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
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QCheckBox,
    QDialog,
)

from ate.engine.run_engine import RunEngine
from ate.engine.step_inputs import StepInput, value_error

from ..catalog import find_definitions, include_map, list_schedule_mismatches, list_schedule_rows, module_map, resolve_schedule_definition, resolve_schedule_path
from ..engine.lua_host import LuaHost
from ..run_session.headless import HeadlessRunSession, SessionError, SessionState


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


class _LogPane(QWidget):
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


class UutOperatorStage(QWidget):
    """Stage one: the Operator and UUT fields.

    Holds no run logic — it only owns the mockup's compact first-stage form: four labelled
    text inputs (Project, Operator, UUT Type, Serial) and a button that advances to the Test
    Schedule stage. Every label is the buddy of its input (30a review item 14). The screen
    wires the button up; nothing here calls into the session until a run actually starts.
    """

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

        self.next_button = QPushButton("Next")
        layout.addWidget(self.next_button, len(rows), 1)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)


class TestScheduleStage(QWidget):
    """Stage two: the list of available schedules and their notes.

    ``schedule_list`` is populated from the catalog; ``rescan_button`` reloads it; ``filter_edit``
    narrows the list to the rows whose name, release or error contains what the Operator typed; and
    ``notes_label`` shows the description of whatever row is selected. Selecting a row
    calls back into the session (``session.select_schedule``) — that binding lives here,
    which is where picking a schedule belongs.
    """

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # The mockup's filter box and Rescan button sit side by side above the list.
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
        self.run_button = QPushButton("Run this schedule")

        layout.addLayout(filter_row)
        layout.addWidget(self.schedule_list)
        layout.addWidget(self.notes_label)
        layout.addWidget(self.mismatch_label)
        layout.addWidget(self.run_button)


def _row_label(number: object, name: object, depth: int) -> str:
    """A step list row's text: Step Number and name, indented two spaces per nesting level.

    Packet 15b: an expanded Sub-schedule's rows sit under their header, so the same Step Number
    in two instances still reads as two places in the tree. The mockup's styling is packet 15d's."""
    indent = "  " * depth
    number = str(number or "")
    name = str(name or "")
    return f"{indent}{number}  {name}" if number else f"{indent}{name}"


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


def _append_input_rows(list_widget: QListWidget, widgets: list["StepInputWidget"], depth: int) -> list[QListWidgetItem]:
    """Add one row per Step Input widget, indented under the step that declared it.

    A list item can carry only one widget, so every Step Input gets its own row — the mockup's
    `.inputs` block, one level deeper than its step. The item text stays empty: any text would be
    drawn *under* the widget, which already paints the input's description, and the row would read
    twice (packet 10a review). The indent comes from the widget's own left margin, the mockup's
    `.inputs { margin-left: 26px }`.
    """
    items: list[QListWidgetItem] = []
    for widget in widgets:
        item = QListWidgetItem("")
        # Not selectable: clicking the row itself means nothing; the controls inside it are the point.
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setSizeHint(widget.sizeHint())
        list_widget.addItem(item)
        list_widget.setItemWidget(item, widget)
        widget.input_item = item  # so the screen can scroll to the row an input is asked for
        items.append(item)
    return items


# The mockup's active input, `.inp.inp-on` (prototype line 54): `#fff6cc` background and an
# `#e0a800` left border. A plain QWidget paints both once it opts into styled backgrounds.
_INPUT_ACTIVE_STYLE = "QWidget#step_input { background: #fff6cc; border-left: 3px solid #e0a800; }"

#: The mockup's `.err` (line 28): the inline message an input shows without submitting anything.
_INPUT_ERROR_STYLE = "color: #b71c1c; font-size: 12px;"

#: `.inp` has no background when it is not asking, so an inert input reads as the list's own white.
_INPUT_IDLE_STYLE = ""


class StepInputWidget(QWidget):
    """One declared Step Input, drawn under the step that declared it (ticket 10a).

    The widget is a pure view of a :class:`~ate.engine.step_inputs.StepInput` and of the run session:
    it decides nothing. Every answer is one call — ``session.supply_input`` for a blocking type,
    ``session.click_poll`` for a POLL — and whether it is live at all is the session's doing: the
    screen enables it when ``input_asked`` announces this step's n-th input and disables it again when
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


class StepsOptionsStage(QWidget):
    """Stage three: the run's steps and the stop-on-fail option.

    ``load_steps`` fills ``step_list`` with the selected schedule's expanded steps so the
    Operator can confirm what will run; ``stop_on_fail_radio`` is prefilled checked (True)
    as the default policy, paired with a "run all" alternative. The stage's Run button
    starts the run through the screen.
    """

    def __init__(self, session: Optional["HeadlessRunSession"] = None) -> None:
        super().__init__()
        self.session = session
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.step_list = QListWidget()
        # Checkboxes are per step (packet 11b): each row is checkable and a toggle is one call
        # into session.toggle_node — the widget holds no logic of its own. Packet 15d: a header row
        # has no toggle of its own; clicking it sets every step beneath it, one call per step.
        self._checkable = True  # setup state: the boxes stay checkable until a run starts
        self._loading = False  # True while load_steps re-syncs check states, so itemChanged
        # is not mistaken for an Operator toggle.
        self._states: dict[int, bool] = {}  # each step's last-seen checked state
        self._rows: list[object] = []  # the expanded tree rows, in runner seq order
        self._step_items: dict[int, QListWidgetItem] = {}  # each step index -> its list row
        self._children: dict[int, list[int]] = {}  # each row's direct children in the tree
        self._overrides: dict[int, bool] = {}  # the Operator's per-step toggles seen so far
        # Every declared Step Input of the selected schedule, under its step row (ticket 10a). They
        # are always shown here, but inert until the run asks for them.
        self.input_widgets: dict[int, list[StepInputWidget]] = {}
        self.step_list.itemChanged.connect(self._on_check_toggled)
        self.stop_on_fail_radio = QRadioButton("Stop on first failure")
        self.continue_all_radio = QRadioButton("Run every step regardless of failures")
        # Default policy: stop on the first failure; :meth:`load_steps` re-applies it from the
        # selected schedule's Stop on Fail default once a schedule has been chosen.
        self.stop_on_fail_radio.setChecked(True)
        self.run_button = QPushButton("Start run")
        # The mockup's `.err` line for a run that cannot start (packet 05b review): the screen fills
        # it when `begin_run` is refused, so the Operator sees why instead of a traceback.
        self.error_label = QLabel("")
        self.error_label.setObjectName("run_error")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {_BROKEN_ERR}; font-size: 12px;")

        layout.addWidget(self.step_list)
        layout.addWidget(self.stop_on_fail_radio)
        layout.addWidget(self.continue_all_radio)
        layout.addWidget(self.error_label)
        layout.addWidget(self.run_button)

    def load_steps(self, steps: Iterable[object]) -> None:
        """Show ``steps`` (each with ``step_no``, ``name``, ``default_enabled``) as checkable rows.

        Every row carries its step's enabled default — after any enclosing header's ``setEnable``
        forced it (packet 15d) — or, when the session carries step-enable overrides for the
        selected schedule, the override that a next run would record, so the boxes show what the
        run will record (spec 37: options carry over). A toggle is one call into
        ``session.toggle_node(row, checked)`` (packet 11b), with the row's position in the expanded
        step list as the runner seq. A header row's box is DERIVED (packet 15d): Checked when every
        step beneath it is on, Unchecked when none is, partial when mixed, and clicking it sets
        them all. Header rows also get the mockup's look: ``#eef1f5`` background and a bold name.
        Each step's declared Step Inputs sit under it as their own rows (ticket 10a), disabled until
        the run asks for them.
        Also re-applies the selected schedule's Stop on Fail default to the radio pair so it is not
        stuck at construction-time; the Operator can still toggle either choice."""
        self._loading = True
        self._clear_input_widgets()
        self.step_list.clear()
        self._states = {}
        self._rows = list(steps)
        self._step_items = {}
        self._children = _children_of_rows(self._rows, lambda row: getattr(row, "parent", None))
        overrides = self.session.node_enables if self.session is not None else None
        self._overrides = dict(overrides) if overrides else {}
        for seq, step in enumerate(self._rows):
            label = getattr(step, "name", "") or ""
            number = getattr(step, "step_no", "") or ""
            item = QListWidgetItem(_row_label(number, label, int(getattr(step, "depth", 0))))
            # Always keep the row enabled and selectable and the box drawn; only
            # ItemIsUserCheckable toggles with the run lock (a locked box still shows its
            # check state). NoItemFlags would disable the row even in setup.
            checkable = Qt.ItemFlag.ItemIsUserCheckable if self._checkable else Qt.ItemFlag.NoItemFlags
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | checkable)
            self.step_list.addItem(item)
            self._step_items[seq] = item
            if getattr(step, "is_header", False):
                item.setBackground(QColor(_HEADER_BG))
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                state = self._header_state(seq)
            else:
                state = Qt.CheckState.Checked if self._effective(seq) else Qt.CheckState.Unchecked
            self._states[seq] = state == Qt.CheckState.Checked
            item.setCheckState(state)
            declarations = getattr(step, "_validated_inputs", None) or []
            if declarations and self.session is not None:
                self.input_widgets[seq] = [
                    StepInputWidget(decl, self.session, seq) for decl in declarations
                ]
                _append_input_rows(
                    self.step_list, self.input_widgets[seq], int(getattr(step, "depth", 0)) + 1
                )
        self._loading = False
        if self.session is not None:
            self._apply_stop_on_fail(self.session.selected_stop_on_fail)

    def _effective(self, row: int) -> bool:
        """Whether row ``row``'s box is on: the Operator's override if there is one, else the default."""
        step = self._rows[row]
        default = bool(getattr(step, "default_enabled", getattr(step, "enabled", True)))
        return self._overrides.get(row, default)

    def _subtree(self, row: int) -> list[int]:
        """Every row beneath the header at ``row``, depth first — headers and Test Steps alike."""
        rows: list[int] = []
        stack = list(self._children.get(row, ()))
        while stack:
            child = stack.pop(0)
            rows.append(child)
            stack.extend(self._children.get(child, ()))
        return rows

    def _header_state(self, row: int) -> Qt.CheckState:
        """A header's box, derived from the steps beneath it (packet 15d, the mockup's enableBox).

        Checked when every one is on, Unchecked when none is, PartiallyChecked when the states are
        mixed. A header with no Test Steps at all has nothing to disable, so it reads Checked, as
        in the mockup."""
        beneath = [r for r in self._subtree(row) if not getattr(self._rows[r], "is_header", False)]
        on = sum(self._effective(r) for r in beneath)
        if on == len(beneath):
            return Qt.CheckState.Checked
        if on == 0:
            return Qt.CheckState.Unchecked
        return Qt.CheckState.PartiallyChecked

    def _header_rows_above(self, row: int) -> list[int]:
        """The header rows enclosing ``row``, innermost first, whose box must follow its children."""
        headers: list[int] = []
        parent = getattr(self._rows[row], "parent", None)
        while parent is not None:
            if getattr(self._rows[parent], "is_header", False):
                headers.append(parent)
            parent = getattr(self._rows[parent], "parent", None)
        return headers

    def _set_box(self, seq: int, state: Qt.CheckState) -> None:
        """Set one step's box without it reading as a new Operator toggle (the state is recorded first)."""
        self._states[seq] = state == Qt.CheckState.Checked
        self._step_items[seq].setCheckState(state)

    def _on_check_toggled(self, item: QListWidgetItem, *_args: int) -> None:
        """A checkbox toggle reaches the session as ``toggle_node(row, checked)`` — no logic here.

        PyQt6's ``itemChanged`` signal passes only the row, so the handler reads the row's new
        check state and compares it against the state it last saw; flag re-syncs (set_checkable),
        same-state events and the load_steps re-sync are all ignored, so only a real toggle
        reaches the session. A locked box (a run in progress) has no toggle: the handler
        returns before touching the session. Packet 15d: clicking a header is not an override of
        its own row — it calls toggle_node for every Test Step beneath it, and the header's box
        (and every header above it) then reads back from those steps. Ticket 10a: the input rows
        under a step have no box, so their itemChanged events are ignored."""
        if self._loading or self.session is None or not self._checkable:
            return
        seq = next((step for step, step_item in self._step_items.items() if step_item is item), None)
        if seq is None:
            return  # an input row (ticket 10a), not a step with a box
        if getattr(self._rows[seq], "is_header", False):
            on = item.checkState() == Qt.CheckState.Checked
            if self._states.get(seq) == on:
                return
            self._states[seq] = on
            beneath = self._subtree(seq)
            for child in beneath:
                if getattr(self._rows[child], "is_header", False):
                    continue
                self._overrides[child] = on
                self._set_box(child, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
                self.session.toggle_node(child, on)
            for child in beneath:
                if getattr(self._rows[child], "is_header", False):
                    self._set_box(child, self._header_state(child))
            for header in self._header_rows_above(seq):  # a nested header's click changes its ancestors too
                self._set_box(header, self._header_state(header))
            return
        checked = item.checkState() == Qt.CheckState.Checked
        if self._states.get(seq) == checked:
            return
        self._states[seq] = checked
        self._overrides[seq] = checked
        self.session.toggle_node(seq, checked)
        for header in self._header_rows_above(seq):
            self._set_box(header, self._header_state(header))

    def set_checkable(self, checkable: bool) -> None:
        """Lock or unlock the step checkboxes: toggle only ItemIsUserCheckable so the
        boxes stay drawn with their check state and the row stays enabled and selectable
        (the screen calls this from session state changes)."""
        self._checkable = checkable
        # Re-sync the last-seen state before the flag pass, so the flag events the pass emits
        # are read back as the same state and never reach the session as phantom toggles.
        for seq, item in self._step_items.items():
            self._states[seq] = item.checkState() == Qt.CheckState.Checked
        keep = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        flag = keep | (Qt.ItemFlag.ItemIsUserCheckable if checkable else Qt.ItemFlag.NoItemFlags)
        for item in self._step_items.values():
            item.setFlags(item.flags() & keep | flag)

    def _clear_input_widgets(self) -> None:
        """Delete the previous schedule's input widgets before the stage is rebuilt.

        ``clear()`` leaves every `setItemWidget` widget parented to the viewport (packet 05b
        review), so each reload would otherwise orphan one row per Step Input."""
        for widgets in self.input_widgets.values():
            for widget in widgets:
                widget.setParent(None)
                widget.deleteLater()
        self.input_widgets = {}

    def _apply_stop_on_fail(self, value: bool) -> None:
        """Set the radio pair to ``value`` (True = stop on first failure)."""
        self.stop_on_fail_radio.setChecked(value)
        self.continue_all_radio.setChecked(not value)


class FailurePromptBar(QWidget):
    """The Stop-on-Fail prompt bar shown while a failed step awaits the Operator's choice.

    Rendered only while a prompt is pending — the screen shows it with :meth:`show_prompt` and
    it hides itself once a choice is made — and it holds no run logic of its own: each button
    only hides the bar and emits ``choice_made`` with CONTINUE, RETRY or ABORT, which the
    screen sends on to the session. The layout follows the run-screen mockup (variant C): a
    bold ``Step <no> <result>`` label, the step's message growing across the bar, then the
    three buttons in order.
    """

    choice_made = pyqtSignal(str)

    def __init__(self) -> None:
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
            "FailurePromptBar { background: #c62828; color: white; font-size: 15px; }"
            "FailurePromptBar QLabel { color: white; }"
            "FailurePromptBar QPushButton { background: #f5f5f5; color: black; font-size: 15px; "
            "padding: 6px 18px; border: 1px solid #a9a9a9; border-radius: 3px; }"
            "FailurePromptBar QPushButton:hover { background: #e6e6e6; }"
            "FailurePromptBar QPushButton#retry { background: #0a64d6; color: white; border-color: #0850ad; }"
            "FailurePromptBar QPushButton#abort { background: #c62828; color: white; border-color: #8e1c1c; }"
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

        # Only shown while a prompt is pending; the screen shows it via show_prompt.
        self.hide()

    def show_prompt(self, step_number: str, result: str, message: str) -> None:
        """Show the bar naming the failed step and its message, for the Operator to answer."""
        self.step_label.setText(f"Step {step_number} {result}")
        self.message_label.setText(message)
        self.show()

    def _on_choice(self, choice: str) -> None:
        """A button was clicked: hide the bar first, then hand the choice to the screen."""
        if not self.isVisible():
            return  # a second click after the bar already hid itself must not answer twice
        self.hide()
        self.choice_made.emit(choice)


class RunView(QWidget):
    """The view shown while and after a run: state header, live steps, Log/Summary, status bar.

    ``state_header`` shows the current :class:`SessionState` plus a live elapsed timer;
    ``step_list`` lists each step with its per-step Result (the latest step enlarged), with every
    declared Step Input on its own row under the step that asked for it (ticket 10a);
    ``failure_bar`` is a full-width bar that surfaces any failing or errored step;
    ``tabs`` holds the attempt Log and a run Summary; and ``status_bar`` (packet 14d) is the last
    widget, the mockup's ``statusBar()``: the run's state, the Pre-test / Steps / Post-test tallies
    and the run and current-item timers. All content is refreshed from the
    store by :meth:`refresh`, never computed here.
    """

    def __init__(self, session: Optional[HeadlessRunSession] = None) -> None:
        super().__init__()
        self.session = session
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.state_header = QLabel()
        # Status bar (packet 14d), the mockup's statusBar(): the run state on the left, then the
        # Pre-test / Steps / Post-test tallies side by side, then the total run time and the time
        # in the current action or step. The screen refreshes it from its one-second timer and
        # from the engine's action/step announcements.
        self.status_bar = QWidget()
        self.status_bar.setStyleSheet("background:#e4e4e4; border-top:1px solid #c4c4c4; padding:3px 10px;")
        self.status_label = QLabel("Ready")
        self.status_pre = QLabel("Pre-test 0/0")
        self.status_steps = QLabel("Steps 0/0")
        self.status_post = QLabel("Post-test 0/0")
        self.status_total = QLabel("Run --:--")
        self.status_current = QLabel("Current --:--")
        status_layout = QHBoxLayout(self.status_bar)
        status_layout.addWidget(self.status_label)
        status_layout.insertStretch(1)  # the mockup's `.grow`: the tallies sit to the right
        for label in (self.status_pre, self.status_steps, self.status_post, self.status_total, self.status_current):
            status_layout.addWidget(label)
        # Whole-run Abort control (packets 13a): asks the session to abort the current run so it
        # stops between steps and records ERROR / NOTRUN / ABORTED. Enabled only while a run is up;
        # the screen toggles it in _on_state_changed.
        self.abort_button = QPushButton("Abort")
        self.step_list = QListWidget()
        # Ticket 10a: each step's Step Input widgets, keyed by the runner seq of the step they
        # belong to, in declaration order. They are built once per run and re-attached whenever the
        # step list is rebuilt, so the answers the Operator gave survive every refresh.
        self.input_widgets: dict[int, list[StepInputWidget]] = {}
        # Full-width failure bar: a fixed-height label that spans the view's width in the layout.
        self.failure_bar = QLabel()
        self.failure_bar.setFixedHeight(24)
        # The Stop-on-Fail prompt bar (packet 12d): hidden until the session pauses on a failure.
        self.failure_prompt_bar = FailurePromptBar()

        self.tabs = QTabWidget()
        log_tab = QLabel()
        summary_tab = QLabel()
        self.tabs.addTab(log_tab, "Log")
        self.tabs.addTab(summary_tab, "Summary")

        # Ticket 08's two log views: the Log pane shows INFO and above; the Debug window is a
        # separate pane showing every level. Both are filled from session.store.log_lines() in
        # refresh(), never computed here.
        self.log_pane = _LogPane("Log — INFO and above")
        self.debug_window = _LogPane("Debug — every level")

        layout.addWidget(self.state_header)
        layout.addWidget(self.abort_button)
        layout.addWidget(self.failure_bar)
        layout.addWidget(self.step_list, stretch=1)
        layout.addWidget(self.failure_prompt_bar)
        layout.addWidget(self.tabs)
        layout.addWidget(self.log_pane)
        layout.addWidget(self.debug_window)
        # Packet 14d review: the mockup's statusBar() closes the run view, so the bar is the last
        # widget — under the step list and the Log/Summary tabs — not a header.
        layout.addWidget(self.status_bar)

    def load_run_steps(self, steps: Iterable[object]) -> None:
        """Build the run's step rows, with each step's declared Step Inputs under it.

        Called when a run starts (ticket 10a) so every input is on screen, inert, before the worker
        ever asks for one: the spec says every step is listed with its inputs always visible. The
        widgets are made from the schedule's validated declarations, exactly as the mockup's
        ``inputsHTML`` renders them."""
        for widgets in self.input_widgets.values():
            for widget in widgets:
                widget.setParent(None)
                widget.deleteLater()
        self.input_widgets = {}
        # Nothing carries a widget yet (they were deleted just above), so clearing is safe: an item
        # takes its widget with it only when it is destroyed still holding one (packet 10a review).
        self.step_list.clear()
        rows: list[tuple] = []
        for seq, step in enumerate(steps):
            is_header = bool(getattr(step, "is_header", False))
            rows.append((
                _row_label(getattr(step, "step_no", ""), getattr(step, "name", ""), int(getattr(step, "depth", 0))),
                None,
                is_header,
                QColor(_HEADER_BG) if is_header else None,
                0,
            ))
            declarations = getattr(step, "_validated_inputs", None) or []
            if declarations and self.session is not None:
                self.input_widgets[seq] = [StepInputWidget(decl, self.session, seq) for decl in declarations]
                for widget in self.input_widgets[seq]:
                    rows.append(("", widget, False, None, 0))
        self._render_rows(rows)

    def _render_rows(self, rows: list[tuple]) -> list[QListWidgetItem]:
        """Draw ``(text, input widget or None, is_header, background, font bump)`` into the step list.

        Rows are updated in place rather than cleared (packet 10a review): a QListWidgetItem that
        carries a Step Input widget deletes that widget when the item is destroyed, so rebuilding the
        list with ``clear()`` wiped out every answer the Operator had given and left the view painting
        a deleted widget. Every rebuild of a run has the same rows — one per step plus one per
        declared input — so each row is reused and only its text, background and font change."""
        list_widget = self.step_list
        while list_widget.count() > len(rows):
            extra = list_widget.takeItem(list_widget.count() - 1)
            widget = list_widget.itemWidget(extra)  # a schedule with fewer rows: drop the orphan too
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        items: list[QListWidgetItem] = []
        for index, (text, widget, is_header, background, bump) in enumerate(rows):
            if index >= list_widget.count():
                item = QListWidgetItem(text)
                list_widget.addItem(item)
                item.setData(Qt.ItemDataRole.UserRole, item.font().pointSize())  # the un-bumped size
            else:
                item = list_widget.item(index)
                item.setText(text)
            item.setBackground(background if background is not None else QBrush())
            font = item.font()
            base = item.data(Qt.ItemDataRole.UserRole)
            font.setPointSize(int(base if base is not None else font.pointSize()) + bump)
            font.setBold(is_header or bump > 0)
            item.setFont(font)
            if widget is not None:
                # Not selectable: clicking the row itself means nothing; the controls inside it are
                # the point. The indent is the widget's own left margin (the mockup's `.inputs`).
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                item.setSizeHint(widget.sizeHint())
                if list_widget.itemWidget(item) is not widget:
                    list_widget.setItemWidget(item, widget)
                widget.input_item = item
            items.append(item)
        return items

    def input_widget(self, node_seq: int, index: int) -> Optional[StepInputWidget]:
        """The widget for the n-th (1-based) Step Input of the step at ``node_seq``, if any."""
        for widget in self.input_widgets.get(node_seq, ()):
            if widget.index == index:
                return widget
        return None

    def refresh(self, nodes: list[dict], attempts: list[dict], log_lines: list[dict]) -> None:
        """Render a finished run's steps, failure bar, and Log/Summary from the store.

        Reads only what the store recorded; it never decides pass/fail itself — a step is
        treated as failed purely when its stored result is ``FAIL`` or ``ERROR``, so this
        stays a view of data, not a run engine. The two tabs are added in order (Log, then
        Summary), so they are widgets 0 and 1 here.

        ``log_lines`` feeds the Log pane (INFO and above) and the separate Debug window
        (every level); DEBUG is filtered out of the Log pane but kept in the debug view.
        """
        log_tab = self.tabs.widget(0)
        summary_tab = self.tabs.widget(1)

        # The run is over: nothing can be asked for any more, so every input closes.
        for widgets in self.input_widgets.values():
            for widget in widgets:
                widget.deactivate()
        # Packet 15b: header rows show their Step Number and name with no result, and every row is
        # indented by its depth in the expanded tree, as on the Steps & options stage. Packet 15d:
        # header rows get the mockup's `#eef1f5` background and bold name, and a rollup of the
        # steps beneath them — `N/M done`, plus `, K failed` when some failed. Ticket 10a: each step
        # keeps its Step Input rows under it, showing the answers the Operator gave.
        depths = _node_depths(nodes)
        children = _children_of_rows(nodes, lambda node: node.get("parent_node_seq"))
        by_seq = {node["node_seq"]: node for node in nodes}
        rows: list[tuple] = []
        last_step_row = None
        for node in nodes:
            text = _row_label(node.get("step_number"), node.get("step_name"), depths[node.get("node_seq")])
            is_header = node.get("kind") != "step"
            if not is_header:
                text += f"  →  {node.get('result', '')}"
            else:
                rollup = _rollup_text(node["node_seq"], children,
                                     lambda seq: by_seq[seq].get("kind") != "step",
                                     lambda seq: by_seq[seq].get("result"))
                if rollup:
                    text += f"  {rollup}"
            rows.append((text, None, is_header, QColor(_HEADER_BG) if is_header else None, 0))
            if not is_header:
                last_step_row = len(rows) - 1
                for widget in self.input_widgets.get(node["node_seq"], ()):
                    rows.append(("", widget, False, None, 0))
        # The latest step is the current one; enlarge it so the eye lands on where we are.
        if last_step_row is not None and len(rows) > 1:
            text, widget, is_header, background, _ = rows[last_step_row]
            rows[last_step_row] = (text, widget, is_header, background, 2)
        self._render_rows(rows)

        failed = any(node.get("result") in ("FAIL", "ERROR") for node in nodes)
        if failed:
            names = [node.get("step_name") or "" for node in nodes if node.get("result") in ("FAIL", "ERROR")]
            self.failure_bar.setText(f"Run FAILED — {len(names)} step(s): {'; '.join(names)}")
            self._apply_failure_style()
        else:
            self.failure_bar.setText("")

        for attempt in attempts:
            message = attempt.get("msg") or ""
            log_tab.setText(log_tab.text() + f"{attempt.get('pass_fail', '')}  {message}\n")

        results = [node.get("result", "") for node in nodes if node.get("kind") == "step"]
        verdict = "FAIL" if failed else ("PASS" if results else "NOTRUN")
        lines = [f"Result: {verdict}"]
        for node in nodes:
            if node.get("kind") != "step":
                continue
            lines.append(f"{node.get('step_number', '')}  {node.get('step_name') or ''}  →  {node.get('result', '')}")
        summary_tab.setText("\n".join(lines))

        # The Log pane shows INFO and above; the Debug window keeps every level.
        info_and_above = [ln for ln in log_lines if str(ln.get("level")) != "DEBUG"]
        self.log_pane.clear()
        self.log_pane.add_lines(info_and_above)
        self.debug_window.clear()
        self.debug_window.add_lines(log_lines)

    def _apply_failure_style(self) -> None:
        """Give the failure bar its red look via a stylesheet."""
        self.failure_bar.setStyleSheet("background-color: #b71c1c; color: white;")


#: The mockup's ``.flag`` box (amber), used for the mismatch flag line and the confirmation's file list.
_FLAG_STYLE = "background: #fff3cd; border: 1px solid #e0b100; color: #5c4300; padding: 4px 10px;"


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


class StagedRunScreen(QWidget):
    """A staged Run screen bound to one :class:`HeadlessRunSession`.

    Three setup stages (UUT & Operator, Test Schedule, Steps & options) live in a
    :class:`QStackedWidget`; the Run view sits below it and is shown once a run starts.
    The session's ``state_changed`` signal drives every update, so starting a run never
    blocks this thread — the worker reports progress through queued signals that land here.
    """

    def __init__(self, session: HeadlessRunSession, confirm_choice: Optional[Callable[[MismatchConfirmation], None]] = None, clock: Optional[Callable[[], datetime]] = None) -> None:
        super().__init__()
        self.session = session
        self.state = SessionState.IDLE
        # Injectable wall clock (packet 14d): the status bar's timers read it, so tests can
        # advance time on their own terms instead of waiting for real seconds.
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # Injected decision-maker for the run-start confirmation (ticket 06c). Tests supply one that
        # clicks OK/Cancel on the dialog instead of a real Operator; without it the dialog is exec()'d
        # as usual. The screen never blocks on it in a test.
        self.confirm_choice = confirm_choice
        # The mismatch confirmation built by the most recent Start, or None when none was needed.
        self._confirm_dialog: Optional[MismatchConfirmation] = None
        # Mismatches per schedule name, refreshed with the list: the ⚠ prefix and the flag line read it.
        self._mismatch_flags: dict[str, list[tuple[str, Optional[str], str, str]]] = {}
        self._release_labels: dict[str, str] = {}
        # Every catalog row the picker shows, runnable and broken (packet 05b); the filter renders
        # a subset of it without rescanning the directory.
        self._schedule_rows: list[object] = []
        # Captured when STARTED fires; ``None`` before the first run or after FINISHED.
        self._started_at: Optional[datetime] = None
        # When the current action or step began (packet 14d); ``None`` between run start and the
        # first Action/Step. The status bar's "Current" timer reads this.
        self._item_started_at: Optional[datetime] = None
        #: The status bar's left label (packet 14d): "Ready" before a run, "Running" while it is up,
        # "Paused" during a Stop-on-Fail pause, "Finished" when it ends. The three tallies beside it
        # are what the mockup's statusBar() shows; they never overwrite each other.
        self._phase_text = "Ready"
        # Pre-/Post-test Action progress as (done, total), read from the engine's action_started.
        self._pre_counts: tuple[int, int] = (0, 0)
        self._post_counts: tuple[int, int] = (0, 0)
        # Steps that have started (a Retry counts once) and the enabled steps in the run.
        self._started_steps: set[int] = set()
        self._step_total = 0
        # The RunEngine worker for the current run, set by :meth:`_begin_run_from_selection` and
        # cleared on FINISHED so a new start() captures fresh fields; ``None`` otherwise. Exposed so
        # tests can drive inputs (supply/click) through the engine reached via the session.
        self._engine: Optional[RunEngine] = None
        # The step-list row carrying the pause highlight (packet 12d); ``None`` when no prompt is
        # pending. Dropped before refresh() clears the list, which would delete the item.
        self._failed_row: Optional[QListWidgetItem] = None

        self.uut_operator_stage = UutOperatorStage()
        self.test_schedule_stage = TestScheduleStage()
        self.steps_options_stage = StepsOptionsStage(self.session)
        self.run_view = RunView(self.session)
        # Disabled until a run starts; _on_state_changed enables it only while STARTED. The session's
        # initial IDLE emit precedes this connection, so set the default here explicitly.
        self.run_view.abort_button.setEnabled(False)
        self.steps_stack = QStackedWidget()
        self.steps_stack.addWidget(self.uut_operator_stage)
        self.steps_stack.addWidget(self.test_schedule_stage)
        self.steps_stack.addWidget(self.steps_options_stage)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.addWidget(self.steps_stack, stretch=1)
        root.addWidget(self.run_view)
        self.run_view.hide()

        # Prefill the Project field from config's `defaultProject` (spec 08). The Station name is
        # not a Project: it is recorded on the run row separately, and never lands in this field.
        if self.session.config is not None:
            self.uut_operator_stage.project_edit.setText(self.session.config.default_project or "")
        # A Test Run cannot start with an empty Project, so the field gates Start (packet 30a r3).
        self.uut_operator_stage.project_edit.textChanged.connect(self._update_run_button)

        # Bind to the session's observable state — the single source of run truth.
        self.session.state_changed.connect(self._on_state_changed)

        # A Stop-on-Fail pause names its failing step in the prompt bar (packet 12d); the bar's
        # buttons answer it. Both are display-only — the session's choose() is the only call out.
        self.session.failure_prompt.connect(self._on_failure_prompt)
        self.run_view.failure_prompt_bar.choice_made.connect(self._on_prompt_choice)

        # Status bar (packet 14d): every new Action or Step names the phase and restarts the
        # current-item timer.
        self.session.action_started.connect(self._on_action_started)
        self.session.step_started.connect(self._on_step_started)
        # Ticket 10a: Step Input widgets are driven by these two signals and answer through the
        # session's supply_input/click_poll. The layout decides nothing: asked → live, answered →
        # closed with the value showing.
        self.session.input_asked.connect(self._on_input_asked)
        self.session.input_answered.connect(self._on_input_answered)
        # A step's Attempt loop ending closes its POLL widgets: they are live only while it runs.
        self.session.step_finished.connect(self._on_step_finished)

        # Selecting a row records that selection on the session, so starting later resolves
        # exactly what the Operator picked; the notes label mirrors it for confirmation. Both the
        # selection and the current item are watched: a broken row cannot be selected, yet it can
        # still be made current, and then nothing stale may be left showing (packet 05b review).
        self.test_schedule_stage.schedule_list.itemSelectionChanged.connect(self._on_schedule_selected)
        self.test_schedule_stage.schedule_list.currentItemChanged.connect(self._on_schedule_selected)

        # Packet 05b: the filter box narrows the list to rows matching name, release or error, and
        # Rescan reloads it from disk — broken rows included.
        self.test_schedule_stage.filter_edit.textChanged.connect(self._render_schedule_rows)
        self.test_schedule_stage.rescan_button.clicked.connect(self._refresh_schedule_list)

        # Wire navigation and start; each button only advances or begins a run, never decides results.
        self.uut_operator_stage.next_button.clicked.connect(lambda: self._advance(1))
        self.test_schedule_stage.run_button.clicked.connect(self._begin_run_from_selection)
        self.steps_options_stage.run_button.clicked.connect(self.begin_run)

        # The Abort button asks the session to abort the current run; only meaningful while it is up.
        self.run_view.abort_button.clicked.connect(self._request_abort)

        # Populate the schedule list once at construction; rescan reloads it.
        self._refresh_schedule_list()

        # A one-second timer keeps the header's elapsed time live without polling the worker.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_header)
        self._timer.start(1000)

    # -- navigation ---------------------------------------------------------

    def _advance(self, index: int) -> None:
        """Move to another setup stage without starting a run."""
        self.steps_stack.setCurrentIndex(index)

    def _begin_run_from_selection(self) -> None:
        """Advance from the schedule stage to the steps stage for the selected schedule.

        Loads that schedule's expanded steps into the Steps & options stage so the Operator
        can see what will run, then shows the next stage. Selection itself was already sent
        to the session when the row was picked; this only previews its steps.
        """
        name = self.session.selected_schedule or ""
        # A broken row made current names no schedule, so there is nothing to preview and no run to
        # begin: the selection must still be a runnable row (packet 05b).
        if not name or self._selected_schedule_name() != name:
            return
        steps = self._load_schedule_steps(name)
        self.steps_options_stage.load_steps(steps)
        self.steps_stack.setCurrentIndex(2)

    def _on_schedule_selected(self) -> None:
        """Record the newly selected schedule on the session, or clear the picker when nothing runnable is current.

        The exact name is read back from the row's user data (set when the list was built),
        so this hands the session precisely what the Operator picked; ``select_schedule`` then
        moves the session to SELECTING while idle. A broken row carries no user data, so it can never
        reach the session: the previous choice is dropped, the notes and the mismatch flag are
        emptied, and Start stays disabled (packet 05b review)."""
        item = self.test_schedule_stage.schedule_list.currentItem()
        name = (item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""
        if name:
            self.session.select_schedule(name)
            self.test_schedule_stage.notes_label.setText(item.text())
            # Flag the mismatch under the picker as soon as the schedule is picked (ticket 06c).
            flagged = self._mismatch_flags.get(name, [])
            label = self.test_schedule_stage.mismatch_label
            label.setText(_mismatch_flag_text(flagged, self._release_labels.get(flagged[0][3], "") if flagged else ""))
            label.setStyleSheet(_FLAG_STYLE if flagged else "")
        else:
            self._clear_picker_selection()
        self._update_run_button()

    def _clear_picker_selection(self) -> None:
        """Nothing runnable is current — a broken row, or the picked row filtered away — so show nothing.

        The notes and the mismatch line describe the schedule that *was* picked; leaving them filled
        reads as if it were still the selection, and the session would still start a run from it
        (packet 05b review). A run already in progress keeps its own selection."""
        stage = self.test_schedule_stage
        stage.notes_label.setText("")
        stage.mismatch_label.setText("")
        stage.mismatch_label.setStyleSheet("")
        if self.session.selected_schedule and self.state != SessionState.STARTED:
            self.session.clear_selection()

    def _load_schedule_steps(self, schedule_name: str) -> list[object]:
        """Load the expanded steps of ``schedule_name`` for preview (no execution)."""
        config = self.session.config
        if config is None:
            return []
        definition = resolve_schedule_definition(*config.test_definition_dirs, schedule_name=schedule_name)
        if definition is None:
            return []
        modules = module_map(definition)  # packet 06a: forward the release's require'd module map to the preview host
        path = resolve_schedule_path(definition, schedule_name)
        if path is None:
            return []
        host = LuaHost(path, modules=modules, includes=include_map(definition))  # packet 15b: the Sub-schedule map
        return list(host.tree)  # the expanded tree in node order: headers followed by their children

    # -- running ------------------------------------------------------------

    def _reveal_run_view(self) -> None:
        """Show the Run view for an in-progress run and hide the setup stages.

        This screen is itself a top-level widget, so ``run_view.show()`` alone does not make
        it visible offscreen (a child only reports visible once its ancestor chain is too);
        showing this widget as well cascades visibility down to every descendant."""
        self.steps_stack.hide()
        self.run_view.show()
        self.show()

    def _confirm_store_choice(self, mismatches: list[tuple[str, Optional[str], str, str]]) -> Optional[bool]:
        """Ask the Operator what to do with mismatched files before the run starts.

        No mismatch means no confirmation at all (spec: the run-start confirmation only appears on a
        mismatch), so this returns ``None`` and Start runs straight through. Otherwise the dialog lists
        every file as `expected …, found …` and, with `database.storeMismatchedFiles` = `ask`, offers
        the "Store mismatched files with this Test Run" checkbox ticked by default; `always`/`never`
        have no checkbox, only what will happen. Returns the store decision, or ``None`` if the
        Operator cancelled — in which case no run starts. ``self.confirm_choice`` lets a test click
        OK/Cancel itself instead of a human, so no test ever blocks on ``exec()``."""
        if not mismatches:
            return None
        release = self._release_labels.get(mismatches[0][3], mismatches[0][3] or "")
        setting = self.session.config.store_mismatched_files if self.session.config is not None else "ask"
        stage = self.uut_operator_stage
        details = ", ".join(part for part in (
            f"UUT {stage.serial_edit.text().strip()}" if stage.serial_edit.text().strip() else "",
            f"Project {stage.project_edit.text().strip()}" if stage.project_edit.text().strip() else "",
            f"Operator {stage.operator_edit.text().strip()}" if stage.operator_edit.text().strip() else "",
        ) if part)
        dialog = MismatchConfirmation(self.session.selected_schedule or "", release, mismatches, setting, details)
        self._confirm_dialog = dialog
        if self.confirm_choice is not None:
            self.confirm_choice(dialog)
        else:
            dialog.exec()
        return dialog.store_choice()

    def begin_run(self) -> None:
        """Capture the Operator's fields and start a run. Clears nothing on purpose.

        Operator, UUT Type and serial are only pushed when a widget is non-empty, so a new run keeps
        every field it did not change; Project is pushed as typed, because a blank Project must
        refuse the run rather than carry an earlier one (spec 08). The run then starts through
        ``session.start()`` and the Run view is shown. This never blocks: ``start()`` queues its
        signals and returns immediately.
        """
        operator = self.uut_operator_stage.operator_edit.text().strip()
        project = self.uut_operator_stage.project_edit.text().strip()
        uut_type = self.uut_operator_stage.uut_type_edit.text().strip()
        serial = self.uut_operator_stage.serial_edit.text().strip()
        # The stop-on-fail choice is a boolean toggle, so it always applies (default True).
        self.session.set_stop_on_fail(self.steps_options_stage.stop_on_fail_radio.isChecked())

        if operator:
            self.session.set_operator(operator)
        # Project is pushed even when blank: a Test Run cannot start without one (spec 08), so the
        # field's content is exactly what the run records rather than whatever an earlier run left.
        self.session.set_project(project)
        if uut_type:
            self.session.set_uut_type(uut_type)
        if serial:
            self.session.set_uut_serial(serial)

        mismatches = self.session.pending_mismatches()
        store = self._confirm_store_choice(mismatches)
        if mismatches and store is None:
            return  # the Operator cancelled the confirmation: nothing started, nothing cleared.
        try:
            engine = self.session.start(store_mismatched=store)
        except SessionError as exc:
            # The schedule picked before this stage broke in the meantime (packet 05b review): name
            # the reason on the stage the Operator is looking at rather than raising out of the
            # button handler, and take Start away so it cannot be clicked again.
            self.steps_options_stage.error_label.setText(f"⛔ {exc}")
            self.steps_options_stage.run_button.setEnabled(False)
            return
        self._engine = engine
        self._started_at = self._clock()
        # Switch to the Run view and hide the setup stages for the duration of the run.
        self._reveal_run_view()

    def _request_abort(self) -> None:
        """The Abort button ends the current run — answering a pending prompt or stopping between steps.

        With a Stop-on-Fail prompt up the worker is blocked waiting for a choice, so the
        step-boundary flag ``request_abort`` would set is never observed and the run hangs: answer
        the pending prompt with ABORT instead (the bar's own Abort path). Otherwise the worker is
        free to stop at its next step boundary (records ERROR / NOTRUN / ABORTED); ``request_abort``
        raises if there is no run in progress (the button is disabled whenever one is not)."""
        if self.run_view.failure_prompt_bar.isVisible():
            self._on_prompt_choice("ABORT")
            return
        self.session.request_abort()

    @property
    def engine(self) -> Optional[RunEngine]:
        """The RunEngine worker for the current run, or ``None`` before it starts / after FINISHED.

        Exposed so tests can inspect the worker a run is going through; the Step Input widgets
        (ticket 10a) answer through the session's ``supply_input``/``click_poll``, never the engine."""
        return self._engine

    def _on_state_changed(self, state: SessionState) -> None:
        """React to a session state change — the only place run truth reaches the layout."""
        self.state = state
        if state == SessionState.STARTED:
            # Begin the run and reveal it. Ticket 10a: the step rows and their Step Inputs are built
            # as the run starts, so every declared input is on screen, inert, before the worker asks.
            steps = self._load_schedule_steps(self.session.selected_schedule or "")
            self.run_view.load_run_steps(steps)
            # Status bar (packet 14d): the run clock starts here and the one-second timer restarts,
            # because a previous run stopped it at FINISHED. No Action or Step has begun yet, so the
            # current-item timer is blank; the Steps tally knows its total from the rows just built.
            self._started_at = self._clock()
            self._item_started_at = None
            self._phase_text = "Running"
            self._pre_counts = (0, 0)
            self._post_counts = (0, 0)
            self._started_steps = set()
            self._step_total = self._enabled_step_count(steps)
            self._timer.start(1000)
            self._reveal_run_view()
        elif state == SessionState.FINISHED:
            # Release the finished worker so a new start() captures fresh fields; the next run owns
            # its own engine again.
            self._engine = None
            # refresh() clears the step list and deletes its items — drop the pause highlight's row
            # reference before that so we never touch a deleted item (packet 12d).
            self._failed_row = None
            # Read results from the store (never compute them here) and refresh the view.
            run_uuid = self.session.run_uuid
            nodes = self.session.store.nodes(run_uuid) if run_uuid else []
            attempts = self.session.store.attempts(run_uuid) if run_uuid else []
            log_lines = self.session.store.log_lines(run_uuid) if run_uuid else []
            self.run_view.refresh(nodes, attempts, log_lines)
            # The run is over: the status bar keeps these final timings (packet 14d).
            self._phase_text = "Finished"
            # Return to the first setup stage so a new run can be started.
            self.steps_stack.show()
            self.steps_stack.setCurrentIndex(0)
        elif state in (SessionState.IDLE, SessionState.SELECTING):
            # Back on the setup stages; show them again and hide the Run view.
            self.run_view.hide()
            self.steps_stack.show()

        # Step checkboxes (packet 11b): checkable in setup, locked once a run starts and kept
        # locked until it ends; checkable again only for the next run's setup.
        self.steps_options_stage.set_checkable(state in (SessionState.IDLE, SessionState.SELECTING))

        # The Abort button is only meaningful while a run is in progress.
        self.run_view.abort_button.setEnabled(state == SessionState.STARTED)

        self._update_header(state)
        if state == SessionState.FINISHED:
            # Final state reached: stop the one-second timer so the header and status bar keep
            # their final values on screen (packet 14d).
            self._timer.stop()

    def _update_header(self, state: Optional[SessionState] = None, now: Optional[datetime] = None) -> None:
        """Refresh the header's state label and the status bar's live timers.

        ``now`` is the injectable clock's reading (packet 14d): a test can emit the QTimer's
        ``timeout`` to tick the bar without waiting for real seconds. The status bar updates
        even while a failure prompt is pending."""
        if now is None:
            now = self._clock()
        self._update_status_bar(now)
        if self.run_view.failure_prompt_bar.isVisible():
            # A failure prompt is pending: the header waits on the Operator, whatever the state
            # (this also guards the one-second timer while paused) — packet 12d.
            self.run_view.state_header.setText("WAITING")
            return
        if state is None:
            state = self.state
        text = f"{state.value}"
        if self._started_at is not None and state in (SessionState.STARTED, SessionState.FINISHED):
            delta = now - self._started_at
            text += f"  ·  {_format_elapsed(delta)}"
        self.run_view.state_header.setText(text)

    def _update_status_bar(self, now: datetime) -> None:
        """Refresh the status bar (packet 14d): the state, the three tallies and both timers.

        Pre-test, Steps and Post-test progress are on the bar at the same time — starting an Action
        or a Step never overwrites another phase's tally. The Run and Current timers keep ticking
        through a Stop-on-Fail pause: the bar measures real elapsed time, which is how the Operator
        can tell that something has hung."""
        view = self.run_view
        view.status_label.setText(self._phase_text)
        view.status_pre.setText(f"Pre-test {self._pre_counts[0]}/{self._pre_counts[1]}")
        view.status_steps.setText(f"Steps {len(self._started_steps)}/{self._step_total}")
        view.status_post.setText(f"Post-test {self._post_counts[0]}/{self._post_counts[1]}")
        if self._started_at is None:
            view.status_total.setText("Run --:--")
            view.status_current.setText("Current --:--")
            return
        view.status_total.setText(f"Run {_format_elapsed(now - self._started_at)}")
        if self._item_started_at is None:
            view.status_current.setText("Current --:--")
        else:
            view.status_current.setText(f"Current {_format_elapsed(now - self._item_started_at)}")

    def _on_action_started(self, phase: str, done: int, total: int) -> None:
        """A Pre-/Post-test Action has begun: move its tally and restart the current-item timer."""
        now = self._clock()
        if phase == "pre":
            self._pre_counts = (done, total)
        else:
            self._post_counts = (done, total)
        self._item_started_at = now
        self._update_header(now=now)

    def _on_step_started(self, node_seq: int) -> None:
        """A Test Step Attempt — or a Retry of one — has begun.

        ``node_seq`` is the step that started: the Steps tally counts each distinct step once, so a
        Retry restarts the current-item timer without counting the step twice (packet 14d)."""
        now = self._clock()
        self._started_steps.add(node_seq)
        self._item_started_at = now
        self._update_header(now=now)

    def _enabled_step_count(self, steps: list[object]) -> int:
        """How many expanded rows are Test Steps this run will execute (the Steps tally's total).

        Header rows are not steps, and an Operator toggle (packet 11a) beats the row's :setEnable()
        default exactly as :meth:`RunEngine._enabled` decides."""
        overrides = self.session.node_enables or {}
        return sum(
            1
            for seq, step in enumerate(steps)
            if not getattr(step, "is_header", False)
            and overrides.get(seq, bool(getattr(step, "default_enabled", getattr(step, "enabled", True))))
        )

    # -- Stop-on-Fail prompt (packet 12d) -------------------------------------

    def _on_failure_prompt(self, node_seq: int, attempt_no: int) -> None:
        """A failure is awaiting the Operator's choice: name it in the bar and pause the display.

        The failing step's node row is not written yet (the engine writes it after the last
        Attempt), so the step's number and name come from the schedule's expanded steps and its
        result and message from the Attempt row the pause was raised on."""
        step = self._step_identity(node_seq)
        attempt = self._attempt_row(node_seq, attempt_no)
        result = str(attempt.get("pass_fail") or "FAIL") if attempt else "FAIL"
        message = str(attempt.get("msg") or "") if attempt else ""
        self.run_view.failure_prompt_bar.show_prompt(str(getattr(step, "step_no", "") or ""), result, message)
        self._render_paused_steps(node_seq, result)
        self._set_paused_header()

    def _step_identity(self, node_seq: int):
        """The selected schedule's expanded step at ``node_seq`` (its number and name)."""
        steps = self._load_schedule_steps(self.session.selected_schedule or "")
        if 0 <= node_seq < len(steps):
            return steps[node_seq]
        return None

    def _attempt_row(self, node_seq: int, attempt_no: int) -> Optional[dict]:
        """The Attempt row a pause was raised on — its result and message, read from the store."""
        run_uuid = self.session.run_uuid
        if run_uuid is None:
            return None
        for row in self.session.store.attempts(run_uuid):
            if row.get("node_seq") == node_seq and row.get("attempt_no") == attempt_no:
                return row
        return None

    def _render_paused_steps(self, node_seq: int, result: str) -> None:
        """Rebuild the step list mid-run: done steps from the store, the failing one marked, the rest pending.

        Rows normally only exist after FINISHED (refresh), so a pause renders its own list; the
        failing step's row gets the pause highlight and is remembered so the choice can clear it.
        Packet 10a review: the rows are updated in place (``RunView._render_rows``) rather than
        cleared and re-added — destroying an item that carries a Step Input widget deletes that
        widget, and the pause then painted a dead widget (SIGSEGV while the run is paused)."""
        run_uuid = self.session.run_uuid
        nodes = self.session.store.nodes(run_uuid) if run_uuid else []
        done = {node.get("node_seq"): node for node in nodes if node.get("kind") == "step"}
        steps = self._load_schedule_steps(self.session.selected_schedule or "")
        # Packet 15d: header rows are styled like the finished list and roll up the steps beneath
        # them, so a paused run still shows how far each Sub-schedule got.
        children = _children_of_rows(steps, lambda step: getattr(step, "parent", None))
        rows: list[tuple] = []
        failed_index = None
        for seq, step in enumerate(steps):
            text = _row_label(getattr(step, "step_no", ""), getattr(step, "name", ""), int(getattr(step, "depth", 0)))
            if getattr(step, "is_header", False):  # a header row has no result, as in the finished list
                rollup = _rollup_text(seq, children,
                                      lambda index: getattr(steps[index], "is_header", False),
                                      lambda index: done[index].get("result") if index in done else None)
                rows.append((text + (f"  {rollup}" if rollup else ""), None, True, QColor(_HEADER_BG), 0))
                continue
            if seq == node_seq:
                state = result
            elif seq in done:
                state = str(done[seq].get("result", ""))
            else:
                state = "…"
            # The pause highlight: a pale red row, cleared once the Operator chooses.
            if seq == node_seq:
                failed_index = len(rows)
            rows.append((f"{text}  →  {state}", None, False, QColor("#fff0f0") if seq == node_seq else None, 0))
            for widget in self.run_view.input_widgets.get(seq, ()):
                rows.append(("", widget, False, None, 0))
        items = self.run_view._render_rows(rows)
        self._failed_row = items[failed_index] if failed_index is not None else None

    def _set_paused_header(self) -> None:
        """While a prompt is pending the header goes red and reads WAITING (see _update_header), and
        the status bar says "Paused" (packet 14d) — its timers keep running, because the pause is
        real time the Operator is spending on the run."""
        self.run_view.state_header.setStyleSheet("background-color: #c62828; color: white;")
        self._phase_text = "Paused"
        self._update_header()

    def _on_prompt_choice(self, choice: str) -> None:
        """A prompt was answered (a bar button, or the header Abort while one is up): clear the
        pause's display, then answer the session.

        Clearing first means a run that finishes straight away can refresh() without a highlight
        still held on an item refresh() would delete; the session's worker unblocks on choose().
        The bar hides itself in _on_choice, but the header-Abort path answers without the bar, so
        hide it here too (a no-op when the bar already hid itself)."""
        self.run_view.failure_prompt_bar.hide()
        self._clear_paused_display()
        self.session.choose(choice)

    def _clear_paused_display(self) -> None:
        """Drop the pause highlight and red header once the Operator has chosen."""
        if self._failed_row is not None:
            self._failed_row.setBackground(QBrush())
            self._failed_row = None
        self.run_view.state_header.setStyleSheet("")
        self._phase_text = "Running"  # the bar is back on the run's state (packet 14d)
        self._update_header()

    # -- Step Inputs (ticket 10a) ---------------------------------------------

    def _on_input_asked(self, node_seq: int, attempt_no: int, index: int) -> None:
        """The worker is waiting on this step's n-th input, so its widget becomes live.

        A blocking input is enabled only while its ``ATE.input(n)`` call waits; a POLL opens when
        its step's inputs are announced, which is while that step runs. The mockup's `focusActive`
        puts the cursor in the input that is currently asking, which `activate` does. Nothing here
        decides whether the input is answerable — the session announced it, so the layout renders it.
        """
        widget = self.run_view.input_widget(node_seq, index)
        if widget is None:
            return
        widget.activate(attempt_no)
        # The mockup scrolls the asking row into view so the Operator sees it (packet 10a review).
        if widget.input_item is not None:
            self.run_view.step_list.scrollToItem(widget.input_item)

    def _on_step_finished(self, node_seq: int) -> None:
        """One step's Attempts are done: nothing may answer its POLL any more, so close its widgets."""
        for widget in self.run_view.input_widgets.get(node_seq, ()):
            if widget.declaration.type == "POLL":
                widget.deactivate()

    def _on_input_answered(self, node_seq: int, attempt_no: int, index: int, value: object) -> None:
        """An answer arrived through the session: show it on the widget and close it."""
        widget = self.run_view.input_widget(node_seq, index)
        if widget is not None:
            widget.answer(value)

    # -- schedule list ------------------------------------------------------

    def _refresh_schedule_list(self) -> None:
        """Populate the schedule list from the catalog and clear the notes label.

        A schedule whose Media Release has a checksum mismatch gets a ⚠ prefix in the row (ticket 06c)
        so the Operator can see it before picking it. Broken Test Definitions and Test Schedules are
        listed too, each with its error, after the usable rows (packet 05b): the picker shows every
        row the catalog found, not only the runnable ones."""
        config = self.session.config
        if config is None:
            return
        self._schedule_rows = list_schedule_rows(*config.test_definition_dirs)
        self._mismatch_flags = list_schedule_mismatches(*config.test_definition_dirs)
        definitions = find_definitions(*config.test_definition_dirs)
        # "MR-10422 · PSU-200 Tests 2.3.0", as the mockup names a release in the flag and the dialog.
        self._release_labels = {d.media_number: f"{d.media_number} · {d.name} {d.version}" for d in definitions}
        # Where each Media Release is on disk: a load error's absolute path is written relative to it.
        self._release_dirs = {d.media_number: d.path for d in definitions}
        self._render_schedule_rows()

    def _render_schedule_rows(self) -> None:
        """Rebuild the picker from :attr:`_schedule_rows`, keeping only the rows the filter matches.

        Usable rows come first in scan order and the broken rows follow them at the end of the list:
        they cannot be picked, so they sit below everything the Operator can still start from. The
        picked schedule, if the filter still shows it, is re-selected so Start stays enabled across a
        filter change; when the filter hides it, the notes and the selection are cleared."""
        term = self.test_schedule_stage.filter_edit.text().strip().lower()
        list_widget = self.test_schedule_stage.schedule_list
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
        usable = [row for row in self._schedule_rows if row.error is None]
        broken = [row for row in self._schedule_rows if row.error is not None]
        for row in usable + broken:
            if term and term not in _row_filter_text(row):
                continue
            item = self._make_schedule_item(list_widget, row)
            if chosen and row.name == chosen:
                picked = item
        if picked is not None:
            list_widget.setCurrentItem(picked)
        else:
            self._clear_picker_selection()
        self._update_run_button()

    def _make_schedule_item(self, list_widget: QListWidget, row) -> QListWidgetItem:
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

    def _selected_schedule_name(self) -> Optional[str]:
        """The exact schedule name the picker's current row stands for, or ``None`` if it can't run.

        A broken row carries no name in its user data, so it can never be the selection a run starts
        from even if something bypasses its flags and makes it current (packet 05b)."""
        item = self.test_schedule_stage.schedule_list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole) or None

    def _update_run_button(self) -> None:
        """Start is enabled only while a runnable schedule is the current selection.

        The Steps stage's Start button follows the same rule (packet 05b review): the picked
        schedule can break between stages — a Rescan finds a bad file — and `begin_run` would then
        raise out of the button's handler. A blank Project keeps Start away too (spec 08: a Test
        Run cannot start with an empty Project), with the reason on the stage's error line."""
        runnable = self._selected_schedule_name() is not None
        project = self.uut_operator_stage.project_edit.text().strip()
        self.test_schedule_stage.run_button.setEnabled(runnable and bool(project))
        self.steps_options_stage.run_button.setEnabled(runnable and bool(project))
        if runnable:
            self.steps_options_stage.error_label.setText(
                "" if project else "⛔ Project is required before a Test Run can start"
            )
