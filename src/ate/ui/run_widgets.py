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

from PySide6.QtCore import QEvent, QObject, Qt, QRect, QRectF, QSize, Signal
from PySide6.QtGui import QBrush, QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QStackedWidget,
    QStyledItemDelegate,
    QStyle,
    QGroupBox,
    QVBoxLayout,
    QWidget,
    QDialog,
)

from .theme import (
    CHIP_STYLES,
    MONO_FONT,
    RESULT_STYLES,
    TOKENS,
    badge_width_for,
    contrast,
    ink_on,
    mono_font,
    paint_result_badge,
    ui_font,
)

from ate.dialogs import dismiss_if_headless
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
    """Render one recorded log line as ``HH:MM:SS LEVEL  SOURCE  text`` for a pane.

    Ticket 39 (R3): the band's log is drawn in the mono font, so the padded
    columns line up, and each line carries the time it was recorded."""
    at = str(line.get("at") or "")
    when = at[11:19] if len(at) >= 19 else ""
    level = str(line.get("level", ""))
    source = str(line.get("source", ""))
    return f"{when:<9} {level:<7} {source:<8} {line.get('text', '')}"


def _no_auto_default(*buttons: QPushButton) -> None:
    """Take away a button's Enter-key default (ticket 39 review, D6).

    Qt marks every button in a window `autoDefault`, so a stray Enter — a barcode
    scanner's newline, an Operator pressing Enter to move on — fires the button Qt
    picks as the window's default. That is fine for the OK of a dialog the Operator
    opened and dangerous for Debug, Abort and New run, none of which anyone should
    trigger by accident. `setDefault(False)` as well, so no other widget in the
    window can make it the default."""
    for button in buttons:
        button.setAutoDefault(False)
        button.setDefault(False)


class TextPane(QPlainTextEdit):
    """A scrollable text pane with the ``text``/``setText`` shape the screens and tests use.

    Plain text scrolls, so the Log and Summary tabs keep a small fixed minimum (packet 30b); the
    QLabel they replaced grew their minimumSizeHint with every line the run wrote. The Operator reads
    them; the run writes them, so they are read-only (packet 30b review)."""

    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)

    def text(self) -> str:
        return self.toPlainText()

    def setText(self, value: str) -> None:
        self.setPlainText(value)

    def clear(self) -> None:
        """Empty the pane, so a new run's Log and Summary hold only their own lines (packet 30b)."""
        self.setPlainText("")


class LogPane(QWidget):
    """A scrollable pane that renders recorded log lines under a header label.

    Both the Run view's live Log tab (ticket 08, made live by 25a) and the Debug
    window are instances of this: one is fed ``INFO``-and-above lines, the other
    every level. It never decides what to show — :meth:`add_lines` only renders
    whatever store rows it is handed, in seq order. Ticket 39 (R3): the lines are
    drawn by one read-only monospace ``QPlainTextEdit``, not a ``QListWidget``
    inside a ``QScrollArea`` — the old list-in-scroll-area nested two vertical
    scrollbars and drew the padded ``LEVEL  SOURCE`` columns in a proportional
    font that made them line up in name only. ``title`` is empty for a pane
    whose name is on the tab it sits in.
    """

    def __init__(self, title: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._title = QLabel(title)
        self._title.setStyleSheet("font-weight: bold;")
        if not title:
            self._title.hide()
        self._text = QPlainTextEdit()
        self._text.setObjectName("log_text")
        self._text.setReadOnly(True)
        self._text.setFont(mono_font(12))
        layout.addWidget(self._title)
        layout.addWidget(self._text, stretch=1)
        #: The highest log ``seq`` this pane has drawn, or -1 when it holds nothing.
        #: A live refresh is handed the whole run log, so this is what says which of
        #: those rows are new (ticket 39 review).
        self._last_seq = -1

    def clear(self) -> None:
        """Remove every rendered line."""
        self._text.clear()
        self._last_seq = -1

    def add_lines(self, lines: Iterable[dict]) -> None:
        """Render ``lines`` (each a log_line dict) as level/source/text rows in seq order.

        Ticket 39 review: the Log tab is live, and every refresh is passed the whole
        log the store has recorded. Only the rows after the highest seq already drawn
        are added, and they are appended rather than the pane rewritten — rewriting
        it reset the view to the top of the log every time a line was logged. So the
        Operator's place in the log survives a refresh: reading the middle stays where
        it was, and reading the end follows the end.
        """
        new: list[str] = []
        highest = self._last_seq
        for line in sorted(lines, key=lambda ln: ln.get("seq", 0)):
            seq = int(line.get("seq") or 0)
            if seq <= self._last_seq:
                continue
            new.append(_format_log_line(line))
            highest = max(highest, seq)
        if not new:
            return
        bar = self._text.verticalScrollBar()
        reading_the_tail = bar.value() >= bar.maximum()
        where = bar.value()
        self._text.appendPlainText("\n".join(new))
        # Appending can scroll to the new text; put the view back unless the Operator
        # was already at the end, in which case the tail is what they want to see.
        bar.setValue(bar.maximum() if reading_the_tail else min(where, bar.maximum()))
        self._last_seq = highest

    def rendered_lines(self) -> list[str]:
        """Return the pane's lines in order (for inspection and tests)."""
        return self._text.toPlainText().splitlines()

    def text_widget(self) -> QPlainTextEdit:
        """The read-only mono widget that paints the lines (ticket 39 review).

        Ticket 25a's scrollbar checks reached the pane through its ``QScrollArea``, which the mono
        ``QPlainTextEdit`` replaced: the accessor is what lets a test ask the pane for the widget
        that actually scrolls, rather than digging for a child by object name."""
        return self._text


class DebugWindow(LogPane):
    """Every level of the run log, in its own window (ticket 39, R3/D2).

    `run-screen.md` line 33 always asked for a separate Debug window; packets 08
    and 30b built it as a permanent band inside the Run view instead, which is the
    single biggest space the step list never got. The header's Debug button and
    ``Ctrl+D`` open and close it; it keeps its contents across toggles so the
    Operator can flip it while reading the steps.

    ``parent`` (ticket 39 review) makes it a top-level window that belongs to the
    main window: one Debug window is shared by both layouts, so switching layouts
    cannot leave two open, and closing ATE closes it instead of leaving an orphan
    window holding the application open."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Debug — every level")
        self.setObjectName("debug_window")
        self.setWindowTitle("Debug log — every level")
        self._text.setObjectName("debug_text")
        if parent is not None:
            # A child QWidget is normally painted inside its parent; the Window flag
            # keeps it a window of its own while the parent still owns its lifetime.
            self.setParent(parent)
            self.setWindowFlag(Qt.WindowType.Window)

    def toggle(self) -> None:
        """Open if closed, close if open."""
        if self.isVisible():
            self.hide()
        else:
            self.show()


class StatusBar(QWidget):
    """The status bar every layout puts at the bottom of the screen (packet 14d).

    The mockup's ``statusBar()``: the run's state on the left, then the Pre-test / Steps /
    Post-test tallies side by side, then the total run time and the time in the current action
    or step. The bar paints nothing itself — the screen calls :meth:`set_progress` with the
    numbers it read from the session and the store.
    """

    def __init__(self) -> None:
        super().__init__()
        # Ticket 39 (R1): the look is the theme stylesheet's `#status_bar` rules.
        # The old selector-less stylesheet painted the bar's background, border
        # and padding onto every child label — the grey "chips" the review found.
        self.setObjectName("status_bar")
        # A bare QWidget paints no stylesheet background: without this the
        # `#status_bar` rules have no effect and the bar sits on the window's
        # canvas with nothing to separate it from the panes (39 review).
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
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


#: The mockup's header row background (`.row.hdr` / `.b-row.hdr`), packet 15d —
#: the palette's `subtle` token, so the header rows keep their tint under the theme.
_HEADER_BG = TOKENS["subtle"]

#: A broken picker row: the design system's failed-row tint with the error in the
#: theme's red. (The mockup's `#fff5f5`/`#b71c1c` pair came from the prototype CSS;
#: ticket 39 moves them to the tokens so every red in the app is the same red.)
_BROKEN_BG = TOKENS["failed_tint"]
_BROKEN_ERR = TOKENS["broken_err"]

#: The marker on a row that cannot run. `×` (U+00D7), not `⛔` (U+26D4): the "no entry" sign is not
#: in DejaVu Sans, the only font a headless Linux runner has, so Qt drew its missing-glyph box and
#: every broken row in the picker was an empty rectangle instead of a warning. U+00D7 is Latin-1,
#: so every font Qt can find draws it. ``tests/test_broken_rows_05b.py`` renders the marker and
#: compares its ink with the box Qt draws for a character no font has.
BROKEN_MARKER = "×"
_MUTED_STYLE = f"color: {TOKENS['ink_2']}; font-size: 12px;"

#: `.b-sched .it.broken { cursor: not-allowed }` (mockup line 97): the pointer says "can't pick this".
_FORBIDDEN_CURSOR = Qt.CursorShape.ForbiddenCursor

#: The broken row's layout margins; the row-height calculation below has to add them back.
_ROW_MARGIN_X, _ROW_MARGIN_Y = 6, 4

#: The mockup's ``.flag`` box (amber), used for the mismatch flag line and the confirmation's file list.
_FLAG_STYLE = (
    f"background: {TOKENS['ask']}; border: 1px solid {TOKENS['ask_edge']}; "
    f"color: {TOKENS['ask_ink']}; padding: 4px 10px; border-radius: 4px;"
)

#: The mockup's active input, `.inp.inp-on` (prototype line 54): the palette's `ask`
#: background and its amber left rule. A plain QWidget paints both once it opts into
#: styled backgrounds.
_INPUT_ACTIVE_STYLE = (
    f"QWidget#step_input {{ background: {TOKENS['ask']}; border-left: 3px solid {TOKENS['ask_edge']}; }}"
)

#: The mockup's `.err` (line 28): the inline message an input shows without submitting anything.
_INPUT_ERROR_STYLE = f"color: {TOKENS['broken_err']}; font-size: 12px;"

#: `.inp` has no background when it is not asking, so an inert input reads as the list's own white.
_INPUT_IDLE_STYLE = ""

#: `.b-sticky` (mockup line 108): the red banner the centre pane keeps at the top while paused.
_STICKY_STYLE = (
    f"background: {TOKENS['failed']}; color: {TOKENS['white']}; padding: 5px 10px; font-weight: 600; border-radius: 4px;"
)

#: A paused step's row: the design system's failed-row tint, with the left bar painted
#: by the row delegate (R4).
_FAILED_BG = TOKENS["failed_tint"]

#: The current step's row: soft primary tint with the primary left bar (R4).
_CURRENT_BG = TOKENS["primary_tint"]

#: The ordinary table row. A cell's background must be set explicitly to clear a tint: an unset
#: QColor paints it black.
_ROW_BG = TOKENS["surface"]

#: The prompt buttons' own metrics (R6, D6): 15 px type, 18 px side padding, 1 px border. Both bar
#: styles and the equal-width rule in `FailurePromptBar` are built from these, so the widths the
#: buttons are fixed to are the widths the stylesheet actually paints.
_PROMPT_BUTTON_FONT_PX = 15
_PROMPT_BUTTON_PAD_X = 18
_PROMPT_BUTTON_PAD_Y = 6
_PROMPT_BUTTON_BORDER = 1

#: The mockup's `.b-inline-prompt`: a red-outlined box hugging the failed step. Three equal
#: buttons (D6): Retry is not the primary, and Abort's separation is spacing, not colour.
_INLINE_PROMPT_STYLE = (
    f"FailurePromptBar {{ background: {TOKENS['white']}; border: 2px solid {TOKENS['failed']}; padding: 8px; border-radius: 6px; }}"
    f"FailurePromptBar QLabel {{ color: {TOKENS['prompt_ink']}; background: transparent; }}"
    f"FailurePromptBar QPushButton {{ background: {TOKENS['white']}; color: {TOKENS['ink']}; "
    f"font-size: {_PROMPT_BUTTON_FONT_PX}px; padding: {_PROMPT_BUTTON_PAD_Y}px {_PROMPT_BUTTON_PAD_X}px; "
    f"border: {_PROMPT_BUTTON_BORDER}px solid {TOKENS['line_strong']}; border-radius: 6px; min-height: 32px; }}"
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
    title = QLabel(f"{BROKEN_MARKER} {name}")
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


def _schedule_row_widget(name: str, release: str, files: list[str]) -> QWidget:
    """A usable picker row in the three-pane list: name, Media Release, then the amber ⚠ line.

    The three-pane layout puts the mismatch note *in* the row (spec: "errors and mismatches shown
    inline"), which a single-colour list item cannot do, so the row is labels on a QWidget styled like
    the mockup's `.it` / `.flag`. A row with nothing to flag is the same widget without the amber line
    and without the tint: the pane is only 320 px wide, so a plain item's wrapped text would sit in a
    one-line-tall row and run over the rows below it."""
    widget = QWidget()
    if files:
        widget.setStyleSheet(f"background: {TOKENS['warn_bg']};")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(_ROW_MARGIN_X, _ROW_MARGIN_Y, _ROW_MARGIN_X, _ROW_MARGIN_Y)
    title = QLabel(f"⚠ {name}" if files else name)
    title.setWordWrap(True)
    subtitle = QLabel(release)
    subtitle.setWordWrap(True)
    subtitle.setStyleSheet(_MUTED_STYLE)
    layout.addWidget(title)
    layout.addWidget(subtitle)
    if files:
        note = QLabel("⚠ checksum mismatch: " + ", ".join(files))
        note.setObjectName("schedule_mismatch")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {TOKENS['amber_ink']}; font-size: 12px;")
        layout.addWidget(note)
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

    The mockup's ``counts()``: one tally per Result value — PASS, FAIL, ERROR, SKIPPED, NOTRUN,
    FORCED_NOTRUN, NOREPORT — with the number of steps that recorded it. The widget only writes the
    numbers it is handed; the screen counts them from the store's node rows.

    Ticket 39 CI (Actions run 41754197618): seven stacked tally rows made the setup pane's natural
    minimum 661 px, and that pane is the tallest thing in the three-pane window — its floor was
    771 px, and the window's 792 px with a run finished, which is over 800 px on Windows' taller
    Segoe UI metrics (the runners resized the window to 1280x821). Two tallies per row keeps every
    Result on screen and gives the layout the room it was short of."""

    RESULT_KEYS = ("PASS", "FAIL", "ERROR", "SKIPPED", "NOTRUN", "FORCED_NOTRUN", "NOREPORT")

    def __init__(self) -> None:
        super().__init__()
        group = QGroupBox("Results")
        grid = QGridLayout(group)
        grid.setContentsMargins(8, 8, 8, 8)
        self.count_labels: dict[str, QLabel] = {}
        for index, key in enumerate(self.RESULT_KEYS):
            row, pair = divmod(index, 2)
            name = QLabel(key)
            name.setObjectName(f"count_{key}")
            count = QLabel("0")
            count.setObjectName(f"count_{key}_value")
            grid.addWidget(name, row, pair * 2)
            grid.addWidget(count, row, pair * 2 + 1)
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

    selection_changed = Signal(str)  # the exact schedule name, or "" when nothing runnable is current

    #: Set while MainWindow is copying the Operator's setup across a layout switch: re-rendering
    #: this picker as part of that is not the Operator choosing a schedule, so the picker paints
    #: the row but never writes the selection to the session (packets 25b, 30b review).
    _carrying_over = False

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
        self.notes_label.setWordWrap(True)
        # The ⚠ line naming the mismatched files of the selected schedule (ticket 06c); empty when
        # the schedule's checksums all match. The three-pane picker puts the note *in* the row, so
        # this label stays blank there instead of naming the same files twice (packet 25a review);
        # the staged picker has no inline note and uses it.
        self.mismatch_label = QLabel("")
        self.mismatch_label.setWordWrap(True)

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
            # In an inline-mismatch list the item text is empty (the row is a widget), so the notes
            # line reads the description from here rather than from the item.
            item.setData(Qt.ItemDataRole.UserRole + 2, row.description or "")
            item.setToolTip(row.media_number or "")
            list_widget.addItem(item)
            if self.inline_mismatches:
                # In the three-pane list every usable row is a widget: its name and Media Release
                # wrap inside the 320 px pane, and a mismatched schedule names its files in the row.
                widget = _schedule_row_widget(
                    row.name,
                    self._release_labels.get(row.media_number, row.media_number or ""),
                    [rel_path for rel_path, _e, _c, _m in self._mismatch_flags.get(row.name, ())],
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
        item = QListWidgetItem(f"{BROKEN_MARKER} {name}\n{release}\n{error}")
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
            self._set_notes(item.text() or (item.data(Qt.ItemDataRole.UserRole + 2) or ""))
            # Flag the mismatch under the picker as soon as the schedule is picked (ticket 06c) —
            # unless the list already draws it inside the row, in which case naming it again below
            # would show the same files twice (packet 25a review).
            flagged = self._mismatch_flags.get(name, [])
            release = self._release_labels.get(flagged[0][3], "") if flagged else ""
            self._set_mismatch("" if self.inline_mismatches else _mismatch_flag_text(flagged, release))
            if not self._carrying_over:
                self.session.select_schedule(name)
        else:
            self._clear_selection()
        self.selection_changed.emit(name)

    def _set_notes(self, text: str) -> None:
        """Show the selected row's description, or nothing at all when there is nothing to show."""
        self.notes_label.setText(text)
        self.notes_label.setVisible(bool(text))

    def _set_mismatch(self, text: str) -> None:
        """Show the mismatch flag line, or empty it and hide the label rather than leaving a bare box."""
        self.mismatch_label.setText(text)
        self.mismatch_label.setStyleSheet(_FLAG_STYLE if text else "")
        self.mismatch_label.setVisible(bool(text))

    def _clear_selection(self) -> None:
        """Nothing runnable is current — a broken row, or the picked row filtered away — so show nothing.

        The notes and the mismatch line describe the schedule that *was* picked; leaving them filled
        reads as if it were still the selection, and the session would still start a run from it
        (packet 05b review). A run already in progress keeps its own selection."""
        self._set_notes("")
        self._set_mismatch("")
        if self._carrying_over:
            return  # a layout switch that lost the row is not the Operator dropping the schedule (30b)
        if self.session.selected_schedule and self.session.state != SessionState.STARTED:
            self.session.clear_selection()


class FailurePromptBar(QWidget):
    """The Stop-on-Fail prompt shown while a failed step awaits the Operator's choice.

    Rendered only while a prompt is pending — the layout shows it with :meth:`show_prompt` and it
    hides itself once a choice is made — and it holds no run logic of its own: each button only
    hides the bar and emits ``choice_made`` with CONTINUE, RETRY or ABORT, which the layout sends on
    to the session. Ticket 39 (R6, D6): three *equal* buttons — Retry is not highlighted as the
    primary, because pushing the Operator toward one answer on hardware where retrying may not be
    right is worse than a neutral prompt — with Abort set 16 px apart, and no button a stray Enter
    from a barcode scanner can trigger. ``style`` picks the full-width red bar (staged) or the box
    hugging the failed step (three-pane inline)."""

    choice_made = Signal(str)

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
        self.step_label.setObjectName("prompt_step")
        self.message_label = QLabel()
        # The message grows to fill whatever width the labels leave, like the mockup's .grow.
        self.message_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self.continue_button = QPushButton("Continue")
        self.continue_button.setToolTip("Keep the result and move on")
        self.retry_button = QPushButton("Retry")
        self.retry_button.setToolTip("Run the step's action again")
        self.abort_button = QPushButton("Abort")
        self.abort_button.setToolTip("Run the Post-test Actions and end the run")
        # R6: no button is the default, so a scanner's Enter or an untouched dialog answers
        # nothing; focus lands on the bar, not on a button.
        _no_auto_default(self.continue_button, self.retry_button, self.abort_button)
        # Red bar, 15 px type (16 px for the step), three equal white buttons with Abort set
        # apart — deliberately not the mockup's red-on-red Abort (contrast 1.00, R6).
        self.setStyleSheet(
            style
            or (
                f"FailurePromptBar {{ background: {TOKENS['failed']}; color: {TOKENS['white']}; padding: 10px 16px; border-radius: 6px; }}"
                f"FailurePromptBar QLabel {{ color: {TOKENS['white']}; font-size: 15px; background: transparent; }}"
                "FailurePromptBar QLabel#prompt_step { font-size: 16px; font-weight: 700; }"
                f"FailurePromptBar QPushButton {{ background: {TOKENS['white']}; color: {TOKENS['ink']}; "
                f"font-size: {_PROMPT_BUTTON_FONT_PX}px; padding: {_PROMPT_BUTTON_PAD_Y}px {_PROMPT_BUTTON_PAD_X}px; "
                f"border: {_PROMPT_BUTTON_BORDER}px solid {TOKENS['line_strong']}; border-radius: 6px; min-height: 32px; }}"
                f"FailurePromptBar QPushButton:hover {{ background: {TOKENS['canvas']}; }}"
            )
        )
        self.retry_button.setObjectName("retry")
        self.abort_button.setObjectName("abort")
        # R6/D6, second pass: the three buttons are the same width. Left to their own size hints
        # they measured 106 px ("Continue"), 78 px ("Retry") and 79 px ("Abort"), so the bar read as
        # one wide button and two narrow ones. The width is measured from the bar's own 15 px font
        # and its padding rather than taken from `sizeHint()`, which is still the unstyled 61 px
        # before the widget is first polished.
        button_width = max(
            QFontMetrics(ui_font(_PROMPT_BUTTON_FONT_PX)).horizontalAdvance(button.text())
            + 2 * (_PROMPT_BUTTON_PAD_X + _PROMPT_BUTTON_BORDER)
            for button in (self.continue_button, self.retry_button, self.abort_button)
        )
        for button in (self.continue_button, self.retry_button, self.abort_button):
            button.setFixedWidth(button_width)

        self.continue_button.clicked.connect(lambda: self._on_choice("CONTINUE"))
        self.retry_button.clicked.connect(lambda: self._on_choice("RETRY"))
        self.abort_button.clicked.connect(lambda: self._on_choice("ABORT"))

        layout.addWidget(self.step_label)
        layout.addWidget(self.message_label)
        layout.addWidget(self.continue_button)
        layout.addWidget(self.retry_button)
        layout.addSpacing(16)  # Abort is set apart from the two Continue/Retry peers (D6)
        layout.addWidget(self.abort_button)

        # Only shown while a prompt is pending; the layout shows it via show_prompt.
        self.hide()

    def show_prompt(self, step_number: str, result: str, message: str, actual: str = "", expected: str = "") -> None:
        """Show the bar naming the failed step and its message, for the Operator to answer.

        R6: the bar carries the step's measurements too — Actual and Expected appear in the
        prompt whenever the Attempt recorded them, not only in the report."""
        self.step_label.setText(f"Step {step_number} {result}")
        detail = ""
        if actual or expected:
            detail = f"  Actual {actual or '—'} · Expected {expected or '—'}"
        self.message_label.setText(f"{message}{detail}")
        self.show()

    def _on_choice(self, choice: str) -> None:
        """A button was clicked: hide the bar first, then hand the choice to the layout."""
        if not self.isVisible():
            return  # a second click after the bar already hid itself must not answer twice
        self.hide()
        self.choice_made.emit(choice)


class RunStateChip(QWidget):
    """The run header's state chip (R2, D4): a painted word plus glyph.

    The chip is the loudest element in the header and is sized so the state reads
    from across a bench (2 m at 96 dpi). The word and glyph carry the meaning; the
    hue only supports it, which is why the chip never relies on green-versus-red
    alone (PASS and FAIL are near-identical under deuteranopia)."""

    WORD_PX = 22

    def __init__(self) -> None:
        super().__init__()
        self._kind = "ready"
        self._word = "READY"

    def set_state(self, kind: str, word: str) -> None:
        """Change the chip's state; the widget repaints from the new kind.

        Ticket 39 review, second pass: `update()` repaints, and only the paint changes — the
        layout never learns that the chip now needs a different width. `sizeHint` is measured from
        the word ("READY" 140 px, "INPUT NEEDED" 223 px), so a chip that started READY stayed 140
        px wide while the run needed 223, and the badge drew its 22 px word squeezed down. The
        geometry is invalidated with the paint so both layouts re-space the header."""
        self._kind = kind
        self._word = word
        self.update()
        self.updateGeometry()

    def state_word(self) -> str:
        """The word currently on the chip."""
        return self._word

    def state_kind(self) -> str:
        """The state the chip is painted in: the key of `CHIP_STYLES`."""
        return self._kind

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt's name
        metrics = QFontMetrics(ui_font(self.WORD_PX, bold=True))
        return QSize(max(140, metrics.horizontalAdvance(self._word) + 40), 42)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt's name
        painter = QPainter(self)
        paint_result_badge(
            painter,
            QRect(0, 0, self.width(), self.height()),
            self._kind,
            word=self._word,
            word_px=self.WORD_PX,
        )


class RunHeader(QWidget):
    """The shared run header both layouts put above the work (R2).

    It answers "which run is this, and what does the Operator need to do next?" from
    a glance: the state chip (RUNNING / INPUT NEEDED / STEP FAILED / POST-TEST, then
    PASS or FAIL with the End Reason), the Test Schedule and its Media Release, the
    UUT, Project, Operator and Stop-on-Fail choices, the run timer, and the Debug
    button. Like every other widget here it decides nothing: each value is read from
    the session or the store by the layout that owns the header."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("run_header")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.state_chip = RunStateChip()
        self.schedule_label = QLabel("")
        self.schedule_label.setObjectName("run_title")
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("run_meta")
        self.meta_label.setWordWrap(True)
        self.timer_label = QLabel("Run --:--")
        self.timer_label.setObjectName("run_timer")
        self.timer_label.setFont(mono_font(12))
        self.debug_button = QPushButton("Debug")
        self.debug_button.setObjectName("debug_button")
        self.debug_button.setToolTip("Open or close the Debug window (Ctrl+D)")
        # Ticket 39 review: the header's Debug button must not become the window's
        # default. An Enter pressed while the Operator is typing a Serial opens the
        # Debug window; a button that answers nothing is the safe one.
        _no_auto_default(self.debug_button)

        context = QVBoxLayout()
        context.addWidget(self.schedule_label)
        context.addWidget(self.meta_label)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.addWidget(self.state_chip)
        layout.addLayout(context, stretch=1)
        layout.addWidget(self.timer_label)
        layout.addWidget(self.debug_button)

    def set_state(self, kind: str, word: str) -> None:
        self.state_chip.set_state(kind, word)

    def set_run_info(self, schedule: str, release: str = "", meta: str = "") -> None:
        """Name the run: schedule — Media Release, and the identity line under it."""
        self.schedule_label.setText(f"{schedule} — {release}" if release else schedule)
        self.meta_label.setText(meta)

    def set_timer(self, elapsed: str) -> None:
        self.timer_label.setText(f"Run {elapsed}")


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

    def __init__(
        self, declaration: StepInput, session: HeadlessRunSession, node_seq: int, owner: Optional[object] = None
    ) -> None:
        super().__init__()
        self.declaration = declaration
        self.session = session
        self.node_seq = node_seq
        #: The layout this widget belongs to (packet 25b): a click on the hidden layout's input is
        # not the Operator's answer, so the widget asks its owner before it reaches the session.
        self.owner = owner
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
        self._desc_label = QLabel(f"{self.index}. {declaration.desc}")  # the mockup's .inp-desc
        outer.addWidget(self._desc_label)
        #: Ticket 39 (D3): in the staged Run view a future step shows this one-line
        # summary instead of its controls, so the step list is not drowned in inert
        # inputs. The summary never appears on stage 3 or in the three-pane layout.
        # Ticket 39 review: the label was built but never put in the widget's layout,
        # so it had no parent — Qt then treated it as its own top-level window, and
        # `setVisible(True)` on a collapsed future input popped a separate window
        # beside the Run screen. It is the first item of `outer`, which carries the
        # mockup's `.inputs` indent, and it wraps so a long description stays inside
        # the row instead of running past it.
        self.summary_label = QLabel(f"{self.index} input: {declaration.desc}")
        self.summary_label.setObjectName("muted")
        self.summary_label.setWordWrap(True)
        self.summary_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        outer.addWidget(self.summary_label)
        self.summary_label.hide()

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
        # Qt shows the tooltip of the widget the cursor is on, so the hover text goes on every
        # control as well as the row: a line edit with no tooltip of its own would show none (29b).
        if declaration.tooltip:
            for control in self.controls:
                control.setToolTip(declaration.tooltip)
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

    def set_summary(self, on: bool) -> None:
        """Collapse or expand this input (ticket 39, D3).

        In the staged Run view a step the run has not reached shows a one-line
        muted summary of its input instead of the full controls; the current,
        asking and finished steps always show them. Stage 3 and the three-pane
        layout never collapse, and a finished run's view is fully expanded."""
        self._desc_label.setVisible(not on)
        self._controls_widget.setVisible(not on)
        self.value_label.setVisible(not on)
        self.error_label.setVisible(not on)
        self.summary_label.setVisible(on)
        #: Ticket 39 review: collapsing and expanding changes what the widget needs,
        # and the row keeps the size hint it was given. A row left at the summary's
        # one line clips the radios, checkboxes or buttons the run is asking for, so
        # the item is resized with the widget here (and in `_render_rows`, which sets
        # the hint every time the list is rebuilt).
        if self.input_item is not None:
            self.input_item.setSizeHint(self.sizeHint())

    def activate(self, attempt_no: int) -> None:
        """This input is being asked for: enable it, give it the mockup's active look, focus it.

        The mockup's `focusActive` puts the cursor in whichever input is currently waiting, so the
        Operator can type without reaching for the mouse."""
        self.attempt_no = attempt_no
        self.set_summary(False)  # an asking input is never shown as a summary line
        self.error_label.setText("")
        for control in self.controls:
            control.setEnabled(True)
        self.setStyleSheet(_INPUT_ACTIVE_STYLE)
        # The mockup's `focusActive`: the waiting line edit takes the cursor, so Enter submits;
        # a widget without one focuses its first control. Only the layout that is showing takes
        # the focus, so the hidden one cannot steal it (packet 25b).
        if self.owner is None or self.owner.is_active():
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
        if self.owner is not None and not self.owner.is_active():
            return  # this layout is not the one showing (packet 25b): its input stays inert
        error = value_error(self.declaration, value)
        if error:
            self.error_label.setText(error)
            return
        try:
            self.session.supply_input(self.node_seq, self.attempt_no, self.index, value)
        except (ValueError, SessionError) as exc:
            # The session says this input is not waiting (or the value is not acceptable): show it
            # rather than crashing the click, and the widget stays as it is (10a review). A run that
            # has already ended raises SessionError, not ValueError (25a review).
            self.error_label.setText(str(exc))

    def _click_poll(self, text: str) -> None:
        """Record a POLL click through the session; the sticky value is the button's text."""
        if self.owner is not None and not self.owner.is_active():
            return  # the hidden layout never clicks on the run's behalf (packet 25b)
        try:
            self.session.click_poll(self.node_seq, self.attempt_no, self.index, text)
        except (ValueError, SessionError) as exc:
            self.error_label.setText(str(exc))


#: Item roles a painted step row reads (R4). `UserRole` itself still carries the
#: row's un-bumped font size, and the picker uses UserRole + 1/+ 2.
STEP_RESULT_ROLE = Qt.ItemDataRole.UserRole + 10
STEP_DETAIL_ROLE = Qt.ItemDataRole.UserRole + 11
STEP_EMPHASIS_ROLE = Qt.ItemDataRole.UserRole + 12


def _row_fill(item) -> QBrush:
    """The background a painted row should use: the item's own brush, or the list's
    white when the item carries no background.

    Ticket 39 review: the delegate asked a `QBrush` for `brushStyle()`, which PySide6
    does not have — the correct method is `style()`. The exception was raised inside
    `paint`, Qt swallowed it and the row was left unpainted, so every tinted row and
    every ordinary row in both layouts drew nothing at all."""
    background = item.data(Qt.ItemDataRole.BackgroundRole)
    if background is None or background.style() == Qt.BrushStyle.NoBrush:
        return QBrush(QColor(TOKENS["surface"]))
    return background


def _detail_pen(fill: QColor) -> QColor:
    """The colour of a row's secondary line on ``fill``.

    Ticket 39 review: the muted ink reads 6.38:1 on the pale tints, but on the solid primary
    fill a selected row takes it drops to 1.01:1 — the Actual/Expected line simply vanished.
    The muted tone is kept wherever it still reaches 4.5:1, and the body ink or white is used
    where it does not."""
    muted = TOKENS["ink_2"]
    return QColor(muted) if contrast(muted, fill) >= 4.5 else QColor(ink_on(fill))


class StepRowDelegate(QStyledItemDelegate):
    """Paint a Run-view step row: name, a result badge at the right edge, the
    Actual/Expected detail line, and the 5 px left bar on the current or failed
    step (R4). The item keeps its text, so the row stays readable to tests,
    tooltips and screen readers; only the drawing changes."""

    #: The narrowest a badge is ever drawn: enough for PASS, FAIL or ERROR with their
    #: glyph. Longer Result words widen it (ticket 39 review, `badge_width_for`).
    BADGE_MIN_WIDTH = 64
    #: The step name keeps at least this much of the row beside the badge, so a wide
    #: badge on a narrow list never pushes the step's name out of view.
    NAME_MIN_WIDTH = 40

    def _badge_width(self, word: str, rect) -> int:
        """How much of the row the badge for ``word`` may take.

        The word is painted, not clipped, so the badge is measured from the font's own
        metrics; on a list too narrow for both the badge and the name, the badge keeps
        its minimum and the name is what loses the room."""
        needed = badge_width_for(word)
        return min(max(self.BADGE_MIN_WIDTH, needed), max(self.BADGE_MIN_WIDTH, rect.width() - self.NAME_MIN_WIDTH))

    def paint(self, painter, option, item) -> None:  # noqa: N802 - Qt's name
        rect = option.rect
        painter.save()
        # The delegate's third argument is the model index, not the QListWidgetItem: read the
        # roles (and the roles this screen sets) through it, so both layouts' delegates work.
        text = item.data(Qt.ItemDataRole.DisplayRole) or ""
        emphasis = item.data(STEP_EMPHASIS_ROLE) or ""
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        left = rect.left() + (5 if emphasis else 4)
        if emphasis:
            fill, bar = (
                (TOKENS["failed_tint"], TOKENS["failed"])
                if emphasis == "failed"
                else (_CURRENT_BG, TOKENS["primary"])
            )
            painter.fillRect(rect, QColor(fill))
            painter.fillRect(QRect(rect.left(), rect.top(), 5, rect.height()), QColor(bar))
        elif selected:
            fill = TOKENS["primary"]
            painter.fillRect(rect, QColor(fill))
        else:
            brush = _row_fill(item)
            fill = brush.color()
            painter.fillRect(rect, brush)
        font = item.data(Qt.ItemDataRole.FontRole)
        painter.setFont(font if font is not None else painter.font())
        # Ticket 39 review, second pass: the ink is decided by the background this row is actually
        # painted, not by the row being selected. A selected current or failed row keeps its pale
        # tint — white ink on primary_tint is 1.23:1 and on failed_tint 1.24:1, so the step's name
        # was invisible — while a plain selected row is filled solid primary, where white is the
        # only ink that reads (6.28:1 against 2.50:1).
        painter.setPen(QColor(ink_on(fill)))
        result = item.data(STEP_RESULT_ROLE) or ""
        badge_width = self._badge_width(result, rect) if result else self.BADGE_MIN_WIDTH
        text_rect = QRect(left, rect.top() + 4, max(0, rect.width() - left - badge_width - 6), rect.height())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, text)
        if result:
            badge = QRect(rect.right() - badge_width - 2, rect.top() + 4, badge_width, 18)
            paint_result_badge(painter, badge, result, word=result)
        detail = item.data(STEP_DETAIL_ROLE) or ""
        if detail:
            painter.setFont(mono_font(11))
            painter.setPen(_detail_pen(fill))
            detail_rect = QRect(left, rect.top() + 22, max(0, rect.width() - left - badge_width - 6), 12)
            painter.drawText(detail_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, detail)
        painter.restore()


class ResultBadgeDelegate(QStyledItemDelegate):
    """The three-pane Result column, painted as a badge (R4). The cell text stays
    what it was — tests, tooltips and the report read it — but the pixels are the
    badge's fill, glyph and word rather than plain black type."""

    def paint(self, painter, option, item) -> None:  # noqa: N802 - Qt's name
        text = (item.data(Qt.ItemDataRole.DisplayRole) or "").strip()
        kind = text.upper()
        if kind not in RESULT_STYLES:
            # The delegate is set on the whole table; cells whose text is not a result
            # (Step, Actual, Message) keep Fusion's ordinary drawing.
            super().paint(painter, option, item)
            return
        painter.save()
        painter.fillRect(option.rect, _row_fill(item))
        height = min(18, option.rect.height() - 4)
        # Ticket 39 review: the badge takes the width the word needs (see
        # `badge_width_for`) rather than a fixed 72 px, which squeezed "NOREPORT"
        # down to its glyph alone. The column is widened to match in `StepTable`.
        width = max(16, min(option.rect.width() - 8, badge_width_for(text, height)))
        badge = QRect(
            option.rect.left() + 4,
            option.rect.top() + max(2, (option.rect.height() - height) // 2),
            width,
            height,
        )
        paint_result_badge(painter, badge, kind, word=text)
        painter.restore()


def build_summary(nodes: list[dict], attempts: list[dict], run: Optional[dict] = None, running: bool = False) -> str:
    """The Summary tab's text: the run's verdict, then one line per Test Step.

    Ticket 39 review: the verdict is the one the engine recorded — the ``result`` and
    ``end_reason`` on the run row written by ``update_run_end`` — never a recomputation
    from the step rows. A run that ended PRETEST_FAILED, STOPPED_ON_FAIL or ABORTED is
    FAIL whatever the steps say, and a step that ran without reporting (``NOREPORT``)
    does not by itself decide the run: the engine does, and the Summary says what the
    engine wrote. While the run is still up there is no final result to show, so the
    tab lists the steps without a verdict line."""
    verdict = str((run or {}).get("result") or "")
    if not verdict:
        # No run row with a result: the run is still going, or never started.
        verdict = "RUNNING" if running else "NOTRUN"
    reason = str((run or {}).get("end_reason") or "")
    lines = [f"Result: {verdict}" + (f" ({reason})" if reason else "")]
    for node in nodes:
        if node.get("kind") != "step":
            continue
        lines.append(f"{node.get('step_number', '')}  {node.get('step_name') or ''}  →  {node.get('result', '')}")
    return "\n".join(lines)


class RunScreenMixin:
    """The run-side behaviour both layouts repeat, in one place (packet 25a review).

    The staged and the three-pane screens bind the same session signals and read the same store
    rows; only where the widgets sit differs. A layout supplies the parts these methods use —
    `session`, `fields`, `picker`, `stop_on_fail_radio`, `error_label`, `start_button`, `log_pane`,
    `debug_window` and `step_input_widgets()` — and decides nothing about a run itself: every call
    here goes to the session or to the store.
    """

    #: Which layout the window is showing (packet 25b). A screen starts inert until MainWindow makes
    #: it the one on screen: the hidden layout mirrors the session but never acts on it, so a control
    #: on it can never answer a run twice. The default is False so a screen built without a window,
    #: or a window that never named its layout, cannot act on a run by accident.
    _active = False

    #: A layout that is not showing defers its store reads (packet 25b review): it marks itself dirty
    #: instead of reading, and :meth:`set_active` draws the deferred rows when it comes back.
    _store_dirty = False

    #: Set while MainWindow is copying the Operator's setup across a layout switch: the picker is
    #: re-rendered as part of that, which is not the Operator choosing a schedule (packet 25b).
    _carrying_over = False

    #: A finished run whose rows this layout did not read while it was hidden, drawn the moment it
    #: is showing again (packet 25b review).
    _deferred_run_uuid: Optional[str] = None

    #: True once the window has released this layout (ticket 39 review). The one-second header
    #: timer is stopped before the session's store is closed, and a tick that still arrives finds
    #: this flag and does nothing: reading a closed store raises `sqlite3.ProgrammingError`, which
    #: Qt swallows into an event-loop exception at teardown.
    _released = False

    #: The run row the header last read, and the run it belongs to (ticket 39 review). The header
    #: timer fires once a second, and the run row's schedule, UUT and Stop-on-Fail do not change
    #: while the run is up, so it is read once per run and re-read when the run's row changes.
    _run_row_cache: Optional[dict] = None
    _run_row_cache_uuid: Optional[str] = None

    def stop_run_timers(self) -> None:
        """Stop this layout's one-second header timer and mark it released.

        MainWindow calls this from `_release_resources` before it closes the session, so the timer
        cannot tick after the store is closed (ticket 39 review)."""
        self._released = True
        timer = getattr(self, "_timer", None)
        if timer is not None:
            timer.stop()

    def _store_open(self) -> bool:
        """Whether this layout's session still has a store it can read.

        Ticket 39 review: the header timer ticks once a second. Once the window has released the
        session — or a test has closed it — every read raises `sqlite3.ProgrammingError: Cannot
        operate on a closed database` inside the Qt event loop, which Qt swallows and pytest-qt
        reports as a teardown error. Stopping the timer at release is the fix; this is the guard
        for every path that reads the store."""
        session = getattr(self, "session", None)
        if session is None or getattr(session, "_closed", False):
            return False
        store = getattr(session, "store", None)
        return store is not None and not getattr(store, "_closed", False)

    def _run_row_for_header(self) -> Optional[dict]:
        """The current run's row, read from the store at most once per run phase.

        The header's identity line and the finished verdict come from the same row every tick, so
        it is cached rather than re-queried every second (ticket 39 review). `invalidate_run_row`
        is called when the row changes — a run finishing, a new run starting — and the next read
        fetches it again."""
        if self._released or not self._store_open() or self.session.run_uuid is None:
            return self._run_row_cache
        run_uuid = self.session.run_uuid
        if self._run_row_cache is None or self._run_row_cache_uuid != run_uuid:
            self._run_row_cache = self.session.store.get_test_run(run_uuid)
            self._run_row_cache_uuid = run_uuid
        return self._run_row_cache

    def invalidate_run_row(self) -> None:
        """Forget the cached run row: the next header read fetches the store's current one."""
        self._run_row_cache = None
        self._run_row_cache_uuid = None

    def set_active(self, active: bool) -> None:
        """Make this the layout that acts, or the one that only mirrors the session.

        Coming back to the visible layout is also when its deferred store reads are made, so the
        results, the prompt state and the log it skipped while hidden are on screen the moment the
        Operator switches to it (packet 25b)."""
        self._active = bool(active)
        if active and self._store_dirty:
            self._store_dirty = False
            self._sync_from_store()

    def is_active(self) -> bool:
        """True when this screen is the layout the window is showing."""
        return bool(getattr(self, "_active", False))

    def _show_if_on_screen(self) -> None:
        """Reveal this layout only when the window's stack is already on it (packet 30b review).

        The window keeps both layouts as pages of one :class:`QStackedWidget`, and the stack hides
        the page the Operator is not looking at. A layout that reveals itself with ``show()``
        un-hides that page and Qt paints it over the layout on screen: the hidden staged screen's
        header, step list and setup fields sat on top of the three-pane view while a run was up
        and after it finished. Which page is visible is the window's decision, never a layout's."""
        stack = self.parent()
        if isinstance(stack, QStackedWidget) and stack.currentWidget() is not self:
            return
        self.show()

    def mark_store_dirty(self) -> None:
        """Something the hidden layout did not read needs reading when it is showing again."""
        self._store_dirty = True

    def defer_finished_run(self, run_uuid: Optional[str]) -> None:
        """Remember a finished run this layout will draw when the Operator switches to it.

        The rows come from the store when the layout comes back, never from a read while it is
        hidden: a hidden layout has nothing on screen to draw them into (packet 25b review)."""
        self._deferred_run_uuid = run_uuid or None
        self.mark_store_dirty()

    def _clear_deferred_run(self) -> None:
        """Forget a finished run this layout never drew (ticket 39 review, second pass).

        A deferred read belongs to the run that finished, not to the session: if the Operator starts
        another run while this layout is hidden, the old uuid is stale, and drawing it when the
        layout comes back shows the previous run's results over the run that is actually waiting
        for an answer. A new run clears it, and :meth:`_sync_from_store` checks it against the
        session's current run as well."""
        self._deferred_run_uuid = None

    def _deferred_run_is_current(self) -> Optional[str]:
        """The deferred run uuid, but only while it is still the run the session is on.

        Ticket 39 review, second pass: the guard that makes the deferral safe. A layout hidden
        through a whole run defers that run; if a new run has started since, the deferred uuid is
        not what the Operator should see, and drawing it would paint the finished run's rows and
        close the Step Inputs the current run is waiting on."""
        run_uuid = self._deferred_run_uuid
        if run_uuid is None:
            return None
        if run_uuid != self.session.run_uuid:
            self._deferred_run_uuid = None
            return None
        return run_uuid

    def _sync_from_store(self) -> None:
        """Redraw this layout from the store it mirrors.

        The staged layout only needs the log lines its Run view shows; the three-pane centre pane
        overrides this with its table, tallies and log."""
        self._deferred_run_uuid = None
        self._refresh_log()

    def focus_waiting(self) -> bool:
        """Hand the keyboard to whatever this layout is waiting on (packet 25b).

        A waiting line edit gets the cursor, so Enter answers it; a Stop-on-Fail prompt answers with
        its Continue button. Only the layout on screen can take the focus: the hidden one is not what
        the Operator is looking at."""
        if not self.is_active():
            return False
        paused = self._paused_prompt_widget()
        if paused is not None:
            paused.setFocus()
            return True
        for widgets in self.step_input_widgets().values():
            for widget in widgets:
                if widget.blocking and widget.line_edit is not None and widget.line_edit.isEnabled():
                    widget.line_edit.setFocus()
                    return True
        return False

    def _paused_prompt_widget(self) -> Optional[QWidget]:
        """The button that answers this layout's pending prompt, or None if there is none."""
        return None

    def _confirm_store_choice(self, mismatches: list[tuple[str, Optional[str], str, str]]) -> Optional[bool]:
        """Ask the Operator what to do with mismatched files before the run starts (ticket 06c).

        No mismatch means no confirmation at all, so this returns ``None`` and Start runs straight
        through. Otherwise the dialog lists every file as `expected …, found …` and, with
        `database.storeMismatchedFiles` = `ask`, offers the "Store mismatched files with this Test
        Run" checkbox ticked by default; `always`/`never` have no checkbox, only what will happen.
        Returns the store decision, or ``None`` if the Operator cancelled — in which case no run
        starts. `self.confirm_choice` lets a test click OK/Cancel itself, so none blocks on `exec()`."""
        if not mismatches:
            return None
        labels = self.picker.release_labels
        release = labels.get(mismatches[0][3], mismatches[0][3] or "")
        setting = self.session.config.store_mismatched_files if self.session.config is not None else "ask"
        details = ", ".join(part for part in (
            f"UUT {self.fields.serial_edit.text().strip()}" if self.fields.serial_edit.text().strip() else "",
            f"Project {self.fields.project_edit.text().strip()}" if self.fields.project_edit.text().strip() else "",
            f"Operator {self.fields.operator_edit.text().strip()}" if self.fields.operator_edit.text().strip() else "",
        ) if part)
        dialog = MismatchConfirmation(self.session.selected_schedule or "", release, mismatches, setting, details)
        self._confirm_dialog = dialog
        if self.confirm_choice is not None:
            self.confirm_choice(dialog)
        else:
            # Nobody is looking at a screen to click Start: arm the answer the config gives (start the
            # run, store the mismatched bytes only if `database.storeMismatchedFiles` says `always`)
            # before `exec()`, which on a headless runner would otherwise wait for a click that never
            # comes. See :meth:`MismatchConfirmation.answer_from_setting`.
            dismiss_if_headless(dialog, MismatchConfirmation.answer_from_setting)
            dialog.exec()
        return dialog.store_choice()

    def _set_stop_on_fail(self, value: bool) -> None:
        """Set the "On failure" pair on whichever layout this is (packet 30b)."""
        self.stop_on_fail_radio.setChecked(value)
        self.continue_all_radio.setChecked(not value)

    def new_run(self) -> Optional[str]:
        """The Operator's "New run": unlock the session for the next run, clearing nothing.

        A session stays FINISHED after a run, and a FINISHED session has its step boxes locked and
        its options shut (packet 30b review). The only Operator action that unlocks it is picking a
        schedule again (spec 37), so New run re-selects the schedule already picked: that moves
        FINISHED → SELECTING and keeps the step-enable overrides, because ``select_schedule`` drops
        overrides only for a *different* schedule. Nothing here writes the setup fields, so Project,
        Operator, UUT Type, serial and schedule all carry over. Re-selecting does re-read the
        schedule's own :setStopOnFail default, so the choice the Operator made is put back after it."""
        if not self.is_active():
            return None  # the layout not showing cannot start the next run (packet 25b)
        schedule = self.session.selected_schedule
        if not schedule:
            return None
        stop_on_fail = self.stop_on_fail_radio.isChecked()
        self.session.select_schedule(schedule)
        self._set_stop_on_fail(stop_on_fail)
        return schedule

    def start_new_run(self) -> None:
        """The layout's "New run" control (packet 30b review): unlock the session for the next run.

        The staged layout overrides this to take the Operator back to stage 1 as well; the three-pane
        setup pane is always on screen, so unlocking the session is the whole action there."""
        self.new_run()

    def begin_run(self) -> None:
        """Capture the Operator's fields and start a run. Clears nothing on purpose.

        Operator, UUT Type and Serial are only pushed when the field is non-empty, so a new run keeps
        every field it did not change; Project is pushed as typed, because a blank Project must
        refuse the run rather than carry an earlier one (spec 08). The run then starts through
        `session.start()`, which queues its signals and returns immediately."""
        if not self.is_active():
            return  # the layout that is not showing must not start a run (packet 25b)
        operator = self.fields.operator_edit.text().strip()
        project = self.fields.project_edit.text().strip()
        uut_type = self.fields.uut_type_edit.text().strip()
        serial = self.fields.serial_edit.text().strip()
        # The stop-on-fail choice is a boolean toggle, so it always applies (default True).
        self.session.set_stop_on_fail(self.stop_on_fail_radio.isChecked())
        if operator:
            self.session.set_operator(operator)
        # Project is pushed even when blank: a Test Run cannot start without one (spec 08).
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
            self._engine = self.session.start(store_mismatched=store)
        except SessionError as exc:
            # The schedule picked before this Start broke in the meantime (packet 05b review): name
            # the reason on the pane the Operator is looking at rather than raising out of the
            # button handler, and take Start away so it cannot be clicked again.
            self.error_label.setText(f"{BROKEN_MARKER} {exc}")
            self.start_button.setEnabled(False)

    def _on_action_started(self, phase: str, done: int, total: int) -> None:
        """A Pre-/Post-test Action has begun: move its tally, restart the current-item timer, and
        pick up the lines the worker has logged so far (packet 25a: the log pane is live)."""
        now = self._clock()
        if phase == "pre":
            self._pre_counts = (done, total)
        else:
            self._post_counts = (done, total)
            self._post_test_started = True  # the header chip reads POST-TEST (D4)
        self._item_started_at = now
        self._refresh_log()
        self._update_header(now=now)

    def _on_step_started(self, node_seq: int) -> None:
        """A Test Step Attempt — or a Retry of one — has begun.

        `node_seq` is the step that started: the Steps tally counts each distinct step once, so a
        Retry restarts the current-item timer without counting the step twice (packet 14d). Ticket
        39 (R4): the current step is the one the Operator must look at, so its row gets the primary
        emphasis and, in the staged Run view, its collapsed Step Input rows expand to their full
        controls."""
        now = self._clock()
        self._started_steps.add(node_seq)
        self._current_seq = node_seq
        # The status bar's "Current" timer measures the Attempt that just began — a
        # Retry restarts it without counting the step twice (packet 14d). Without this
        # the bar kept the previous Action's start time and the pause's timer read wrong.
        self._item_started_at = now
        self._mark_current_step(node_seq)
        for widget in self.step_input_widgets().get(node_seq, ()):
            widget.set_summary(False)
        self._refresh_log()
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

    def _attempt_row(self, node_seq: int, attempt_no: int) -> Optional[dict]:
        """The Attempt row a pause was raised on — its result and message, read from the store."""
        run_uuid = self.session.run_uuid
        if run_uuid is None or not self._store_open():
            return None
        for row in self.session.store.attempts(run_uuid):
            if row.get("node_seq") == node_seq and row.get("attempt_no") == attempt_no:
                return row
        return None

    # -- the shared run header (ticket 39, R2/D4) --------------------------

    def _run_header_state(self) -> tuple[str, str]:
        """The chip kind and word for the header (D4 vocabulary): never the internal
        `STARTED`/`WAITING`/`FINISHED`. INPUT NEEDED while a blocking Step Input waits,
        STEP FAILED while a Stop-on-Fail pause is pending, PASS/FAIL once the run is
        finished (the verdict comes from the run row the store wrote, not from a guess)."""
        if self.state == SessionState.FINISHED:
            verdict = str((self._run_row_for_header() or {}).get("result") or "")
            if verdict == "PASS":
                return "pass", "PASS"
            if verdict == "FAIL":
                return "fail", "FAIL"
            return "notrun", "NOTRUN"
        if self.state == SessionState.STARTED:
            if self.session.pause_pending():
                return "step_failed", "STEP FAILED"
            if getattr(self, "_waiting_inputs", None):
                return "input", "INPUT NEEDED"
            if getattr(self, "_post_test_started", False):
                return "running", "POST-TEST"
            return "running", "RUNNING"
        return "ready", "READY"

    def _refresh_run_header(self, now: Optional[datetime] = None) -> None:
        """Write the shared header: state chip, Test Schedule and Media Release, the run's
        identity line (UUT, Project, Operator, Stop-on-Fail, End Reason) and the run timer.
        Everything is read from the session and the store; the header decides nothing.

        Ticket 39 review: a released layout has nothing left to read — the window closed the
        store — so a tick that arrives after teardown writes nothing instead of raising against
        a closed database. The run row is cached (`_run_row_for_header`) rather than queried
        every second."""
        header = getattr(self, "run_header", None)
        if header is None or self._released or not self._store_open():
            return
        if now is None:
            now = self._clock()
        kind, word = self._run_header_state()
        header.set_state(kind, word)
        run = self._run_row_for_header()
        if run:
            meta = [
                f"UUT {run.get('uut_type') or ''} {run.get('uut_serial') or ''}".strip() or "UUT —",
                f"Project {run['project']}" if run.get("project") else "",
                f"Operator {run['operator']}" if run.get("operator") else "",
                "Stop on Fail" if run.get("stop_on_fail") else "Continue on fail",
            ]
            if run.get("end_reason"):
                meta.append(f"End reason: {run['end_reason']}")
            header.set_run_info(run.get("schedule_name") or "", run.get("media_number") or "", " · ".join(bit for bit in meta if bit))
        if self._started_at is not None:
            header.set_timer(_format_elapsed(now - self._started_at))

    def toggle_debug(self) -> None:
        """Open or close the Debug window (D2): the header button or `Ctrl+D`. The layout
        that is not on screen does nothing, so the shortcut cannot fight the visible one.

        Ticket 39 review: nothing opens before there is a log to read. During setup the
        Debug window is an empty window taking bench space, so the shortcut and the
        button do nothing until the run has recorded a line; a window that is already
        open always closes."""
        window = getattr(self, "debug_window", None)
        if not self.is_active() or window is None:
            return
        if not window.isVisible() and not window.rendered_lines():
            return
        window.toggle()

    def _update_summary(self, nodes: list[dict], attempts: list[dict]) -> None:
        """The Summary tab of the Log band (R3), shared by both layouts.

        Ticket 39 review: the verdict line is the run's stored result, not a guess from
        the step rows, and while the run is still up the tab has no final verdict to
        print."""
        pane = getattr(self, "summary_pane", None)
        if pane is None or not self._store_open():
            return
        run = self._run_row_for_header()  # the cached run row, not a query per refresh
        pane.setText(build_summary(nodes, attempts, run=run, running=self.state == SessionState.STARTED))

    def _mark_current_step(self, node_seq: int) -> None:
        """Layout hook: emphasise the step that just started (R4). Layouts without a
        step list that can be emphasised do nothing."""
        return None

    def step_input_widgets(self) -> dict[int, list[StepInputWidget]]:
        """The layout's Step Input widgets, keyed by the seq of the step that declared each one."""
        raise NotImplementedError

    def input_widget(self, node_seq: int, index: int) -> Optional[StepInputWidget]:
        """The widget for the n-th (1-based) Step Input of the step at ``node_seq``, if any."""
        for widget in self.step_input_widgets().get(node_seq, ()):
            if widget.index == index:
                return widget
        return None

    def _on_input_answered(self, node_seq: int, attempt_no: int, index: int, value: object) -> None:
        """An answer arrived through the session: show it on the widget and close it."""
        widget = self.input_widget(node_seq, index)
        if widget is not None:
            widget.answer(value)
        getattr(self, "_waiting_inputs", set()).discard((node_seq, index))
        self._refresh_run_header()  # the chip leaves INPUT NEEDED the moment the answer lands

    def _show_log(self, log_lines: list[dict]) -> None:
        """The log pane shows INFO and above; the debug window keeps every level (ticket 08).

        Ticket 39 review: both panes are live, and every refresh is handed the whole log
        the store has. They are not cleared here — :meth:`LogPane.add_lines` renders only
        the rows after the highest seq it already drew — because clearing on every line
        logged threw the Operator back to the top of the log and reset the tail they were
        reading. A run's own lines start from the clears at its start."""
        info_and_above = [ln for ln in log_lines if str(ln.get("level")) != "DEBUG"]
        self.log_pane.add_lines(info_and_above)
        self.debug_window.add_lines(log_lines)

    def _refresh_log(self) -> None:
        """Render whatever the store has recorded so far (packet 25a: the log panes are live).

        The worker flushes every ATE log call into the store as it happens, so a line logged before
        a step blocks on an input is already readable when the ask is announced. The panes hold no
        buffer of their own: they show only these rows. A layout that is not showing defers the read
        (packet 25b review) and picks it up when it becomes the visible one."""
        if not self.is_active():
            self.mark_store_dirty()
            return
        if not self._store_open():
            return  # ticket 39 review: the window released the session, so there is nothing to read
        run_uuid = self.session.run_uuid
        self._show_log(self.session.store.log_lines(run_uuid) if run_uuid else [])


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
        # Set only by :meth:`answer_from_setting`: the answer an unattended run is given, taken from
        # `database.storeMismatchedFiles` rather than from the checkbox, so a default nobody chose
        # cannot count as a decision.
        self._stored_override: Optional[bool] = None

        layout = QVBoxLayout(self)
        header = QLabel(
            f"<b>{html.escape(schedule_name)}</b><br><span style='color:{TOKENS['ink_2']}'>{html.escape(release)}</span>"
        )
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

    def answer_from_setting(self) -> None:
        """Close this dialog the way a run nobody is watching has to be answered: Start, and store by the config.

        `database.storeMismatchedFiles` is the only authority (ticket 31d): `always` stores the
        mismatched bytes, `never` stores none, and `ask` stores none either, because the box is
        ticked by default and a default nobody chose is not an answer. Cancel would answer `None` and
        start no run at all, so the run starts in all three cases. This is what `dismiss_if_headless`
        answers on a runner with no display, where `exec()` otherwise waits for a click that never
        comes; an Operator at a screen still answers for themselves."""
        self._stored_override = self.store_setting == "always"
        if self.store_checkbox is not None:  # only `ask` has one
            self.store_checkbox.setChecked(False)
        self.accept()

    def store_choice(self) -> Optional[bool]:
        """Whether this run stores the mismatched bytes, or ``None`` if the run was cancelled."""
        if not self._accepted:
            return None
        if self._stored_override is not None:
            return self._stored_override
        if self.store_checkbox is not None:
            return self.store_checkbox.isChecked()
        return self.store_setting == "always"
