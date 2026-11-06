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

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QRect, QRectF, QSize, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QBoxLayout,
    QApplication,
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
    QStyleOptionViewItem,
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

from ..catalog import find_definitions, schedule_mismatches, schedule_rows
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


#: The note a grouped step shows while it has recorded no line (packet 40a).
NO_OUTPUT_NOTE = "No output."
#: A Test Step the worker has not started yet (packet 40a review, item 5): "No output." would read
#: as though the step ran and said nothing.
NOT_YET_NOTE = "Not run yet."
#: A Test Step this run will never execute — disabled, skipped, or left after a Stop-on-Fail end.
NOT_RUN_NOTE = "Not run."
#: The three run-level groups the grouping draws (packet 40a).
PRE_GROUP_HEADING = "── Pre-test ──"
POST_GROUP_HEADING = "── Post-test ──"
RUN_GROUP_HEADING = "── Run ──"
#: Every note a group can carry before it has a line of its own.
NOTES = (NO_OUTPUT_NOTE, NOT_YET_NOTE, NOT_RUN_NOTE)
RUN_GROUP_KEY = ("run",)
PRE_GROUP_KEY = ("pre",)
POST_GROUP_KEY = ("post",)
#: The most rows a Log pane (or the Debug window) keeps, the newest ones (packet 40a fix 2, item 1).
#: A test lowers it to exercise the window; the pane and the store read both use this value.
LOG_PANE_LIMIT = 5000
#: Rows removed below the cap at a batch trim, keeping appends cheap between trims.
LOG_PANE_SLACK = LOG_PANE_LIMIT // 10
#: Export log on the History screen provides the full persisted log.
LOG_TRIM_NOTICE = "Earlier lines are not shown. The full log is in the run record: use Export log on the History screen."


def _dropped_note(count: int) -> str:
    """The line a group shows under its heading when its earlier lines were left out of the window."""
    return f"… {count} earlier {'line' if count == 1 else 'lines'} not shown"


def _step_note(key: tuple, node: Optional[dict], run_finished: bool, started: set) -> str:
    """What a group with no line yet says (packet 40a review, item 5).

    A Pre-/Post-test Action with no line says "No output.". A Test Step says "Not run." when its
    own row is recorded NOTRUN (disabled, skipped, or never reached), "No output." when it ran or
    is running and logged nothing, and "Not run yet." while the run is still up and the step has
    not started — never a silent-looking "No output." for a step that has not been touched."""
    if key[0] != "step":
        return NO_OUTPUT_NOTE
    result = (node or {}).get("result")
    if result == "NOTRUN":
        return NOT_RUN_NOTE
    if result:
        return NO_OUTPUT_NOTE  # the step ran, and said nothing
    enabled = (node or {}).get("enabled")
    if enabled is not None and not enabled:
        # Disabled: the store keeps `:setEnable(false)` as 0, so `is False` never matches a stored row.
        # A row that says nothing about enabling (None) is not taken as disabled.
        return NOT_RUN_NOTE  # it will never run, from the start of the run (packet 40a fix 2, item 5)
    if run_finished:
        return NOT_RUN_NOTE  # the run ended without ever reaching this step
    if key[1] in started:
        return NO_OUTPUT_NOTE  # the Attempt is in flight and has not logged yet
    return NOT_YET_NOTE


def _step_heading(node: dict, position: int) -> str:
    """The Log pane's label for one scheduled Test Step: `── Step 3: Measure supply ──`.

    Built from the run's node rows, so it shows the Step Number the Operator sees in the step
    list; rows without a number count in their schedule position instead."""
    number = str(node.get("step_number") or "").strip().rstrip(".")
    name = str(node.get("step_name") or "").strip()
    return f"── Step {number or position + 1}: {name} ──"


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


class LogPane(QWidget):
    """A scrollable pane that renders recorded log lines under a header label.

    Both the Run band's live Log (ticket 08, made live by 25a) and the Debug
    window are instances of this: one is fed ``INFO``-and-above lines, the other
    every level. It never decides what to show — :meth:`add_lines` only renders
    whatever store rows it is handed, in seq order. Ticket 39 (R3): the lines are
    drawn by one read-only monospace ``QPlainTextEdit``, not a ``QListWidget``
    inside a ``QScrollArea`` — the old list-in-scroll-area nested two vertical
    scrollbars and drew the padded ``LEVEL  SOURCE`` columns in a proportional
    font that made them line up in name only. ``title`` is the name written above the
    lines: since packet 42 the Run band has no tab bar, so its pane says "Log" itself,
    with ``note`` ("INFO and above") beside it as the mockup's variant B does. A pane
    whose title is empty — the old tab-named pane — shows no heading at all.

    Packet 40a fix 2, item 1 and fix 3, item 1: a pane keeps its newest rows, at most
    :data:`LOG_PANE_LIMIT`. Overflow trims to limit minus slack in one batch. Older rows are counted, not drawn: the first line says so,
    and a group whose earlier lines were dropped says how many under its heading.
    """

    def __init__(self, title: str = "", note: str = "") -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        #: The heading row the band carries now that its tab bar is gone: the pane's name on
        #: the left, the level it shows muted on the right. Both are painted by the theme
        #: (`#log_title`, `#log_note`), not by an inline style.
        self._header = QWidget()
        header = QHBoxLayout(self._header)
        header.setContentsMargins(6, 2, 6, 2)
        self._title = QLabel(title)
        self._title.setObjectName("log_title")
        self._note = QLabel(note)
        self._note.setObjectName("log_note")
        header.addWidget(self._title)
        header.addStretch(1)
        header.addWidget(self._note)
        if not title and not note:
            self._header.hide()
        self._text = QPlainTextEdit()
        self._text.setObjectName("log_text")
        self._text.setReadOnly(True)
        self._text.setFont(mono_font(12))
        layout.addWidget(self._header)
        layout.addWidget(self._text, stretch=1)
        #: The highest log ``seq`` this pane has drawn, or -1 when it holds nothing.
        self._last_seq = -1
        #: The grouping the pane is drawing, in display order: [key, heading, drawn lines].
        self._groups: list[list] = []
        #: The rows the pane keeps (at most LOG_PANE_LIMIT, newest last) and the group each is drawn in.
        self._rows: list[dict] = []
        self._row_keys: list[tuple] = []
        #: Rows left out per group: older than the kept window, so not drawn (packet 40a fix 2, item 1).
        self._dropped: dict[tuple, int] = {}
        #: The note each group with no line showed when it was last drawn, so a change is noticed.
        self._notes: dict[tuple, str] = {}
        #: What the notes are read from: steps by node_seq, whether the run is over, the started steps.
        self._note_inputs: tuple[dict, bool, set] = ({}, False, set())
        #: What the row keys were computed under: the live Action and the Action rows (packet 40a fix 2, item 4).
        self._signature: Optional[tuple] = None
        #: How many times the pane replaced its whole text rather than appending (item 3).
        self._full_renders = 0
        # Document block -> stable anchor key, retained until the drawing is replaced.
        self._drawn_block_keys: dict[int, tuple] = {}
        self._wanted_block_keys: dict[int, tuple] = {}

    def clear(self) -> None:
        """Remove every rendered line."""
        self._text.clear()
        self._drawn_block_keys = {}
        self._last_seq = -1
        self._groups = []
        self._rows = []
        self._row_keys = []
        self._dropped = {}
        self._notes = {}
        self._signature = None

    def last_seq(self) -> int:
        """The highest log ``seq`` this pane has drawn, or -1 when it holds nothing."""
        return self._last_seq

    def is_drawn(self) -> bool:
        """Whether the pane has its grouping: False until the first full render, and after :meth:`clear`."""
        return bool(self._groups)

    def full_render_count(self) -> int:
        """How many times this pane has replaced its whole text (packet 40a review, item 3)."""
        return self._full_renders

    def held_rows(self) -> int:
        """How many log rows the pane is drawing (packet 40a fix 3, item 1).

        At most :data:`LOG_PANE_LIMIT`; overflow trims to limit minus slack."""
        return len(self._rows)

    def add_lines(
        self,
        lines: Iterable[dict],
        nodes: list[dict] | None = None,
        actions: list[dict] | None = None,
        live_phase: Optional[str] = None,
        run_finished: bool = False,
        started_steps: set | None = None,
        dropped: dict | None = None,
    ) -> None:
        """Render ``lines`` (log_line dicts) in seq order, grouped under step headings (packet 40a).

        With the run's tree rows (``nodes``, and its ``actions``) the lines are drawn under one
        label per scheduled Test Step, in schedule order: Pre-test first, then the steps, then the
        Post-test Actions, then the run's own lines. A step that ran and said nothing says "No
        output.", "Not run yet." while the run is still up and has not reached it, "Not run." once the
        run is over and it never ran. This is the full-render path: activation, restore and run start,
        which hand over the newest rows the store holds (see :func:`draw_log_panes`). ``dropped`` counts
        the rows older than ``lines`` per group, which the caller left out of the window. The live path
        is :meth:`append_rows`."""
        rows = sorted(lines, key=lambda ln: ln.get("seq", 0))
        actions = actions or []
        steps, step_seqs = self._steps(nodes or [])
        self._note_inputs = ({n.get("node_seq"): n for n in steps}, run_finished, started_steps or set())
        self._rows = rows
        self._row_keys = [self._row_key(row, step_seqs, actions, live_phase) for row in rows]
        self._dropped = {key: count for key, count in (dropped or {}).items() if count}
        self._drop_oldest()
        self._signature = self._signature_of(actions, live_phase)
        if rows:
            self._last_seq = max(self._last_seq, max(int(r.get("seq") or 0) for r in rows))
        self._groups = self._group_model(self._rows, self._row_keys, steps, actions, live_phase)
        self._redraw()

    def append_rows(
        self,
        rows: Iterable[dict],
        nodes: list[dict],
        actions: list[dict] | None = None,
        live_phase: Optional[str] = None,
        run_finished: bool = False,
        started_steps: set | None = None,
    ) -> None:
        """Fold in rows recorded since the pane last drew, without rebuilding its text (packet 40a review, item 3).

        A row that only extends the group at the bottom goes in with ``appendPlainText``. Anything
        else — a row ahead of the tail, a note that changed (a step started, the run ended), an Action
        that began or ended, rows that pushed older ones out of the window — rebuilds the grouping from
        the rows the pane holds, which is bounded by :data:`LOG_PANE_LIMIT`. Nothing is read from the store here."""
        rows = sorted(rows, key=lambda ln: ln.get("seq", 0))
        actions = actions or []
        if not self._groups:
            return self.add_lines(rows, nodes, actions, live_phase, run_finished, started_steps)
        steps, step_seqs = self._steps(nodes)
        self._note_inputs = ({n.get("node_seq"): n for n in steps}, run_finished, started_steps or set())
        rebuild = False
        signature = self._signature_of(actions, live_phase)
        if signature != self._signature:
            # The Action rows or the live phase moved: every key may have changed, and so may the groups.
            self._signature = signature
            self._row_keys = [self._row_key(row, step_seqs, actions, live_phase) for row in self._rows]
            rebuild = True
        index = {group[0]: i for i, group in enumerate(self._groups)}
        touched: set[int] = set()
        tail: list[str] = []
        tail_seqs: list[int] = []
        repaint = False
        for row in rows:
            seq = int(row.get("seq") or 0)
            if seq <= self._last_seq:
                continue
            self._last_seq = seq
            key = self._row_key(row, step_seqs, actions, live_phase)
            self._rows.append(row)
            self._row_keys.append(key)
            i = index.get(key)
            if i is None:
                rebuild = True  # a group nobody had drawn yet: the grouping is rebuilt below
                continue
            group = self._groups[i]
            touched.add(i)
            if not group[2]:
                repaint = True  # the note in front of this line is replaced by the line
            line = _format_log_line(row)
            group[2].append(line)
            tail.append(line)
            tail_seqs.append(seq)
        if self._drop_oldest():
            rebuild = True
        if rebuild:
            self._groups = self._group_model(self._rows, self._row_keys, steps, actions, live_phase)
            return self._redraw()
        if repaint or touched - {len(self._groups) - 1} or self._notes_moved():
            return self._redraw()
        if tail:
            self._append(tail, tail_seqs)

    def _drop_oldest(self) -> bool:
        """On overflow, trim to limit minus slack in one batch, never drawing above the cap."""
        if len(self._rows) <= LOG_PANE_LIMIT:
            return False
        slack = min(LOG_PANE_SLACK, LOG_PANE_LIMIT // 10)
        low_water = max(1, LOG_PANE_LIMIT - slack)
        excess = len(self._rows) - low_water
        gone = Counter(self._row_keys[:excess])
        del self._rows[:excess]
        del self._row_keys[:excess]
        for key, count in gone.items():
            self._dropped[key] = self._dropped.get(key, 0) + count
        return True

    @staticmethod
    def _signature_of(actions: list[dict], live_phase: Optional[str]) -> tuple:
        """What a row's group depends on besides the row: the live phase and the Action rows."""
        return (live_phase, tuple((a.get("seq"), a.get("phase"), a.get("instance_seq")) for a in actions))

    def _note_for(self, key: tuple) -> str:
        """The note a group with no line shows right now (packet 40a fix 2, item 5)."""
        if key[0] != "step":
            return NO_OUTPUT_NOTE
        by_seq, run_finished, started = self._note_inputs
        return _step_note(key, by_seq.get(key[1]), run_finished, started)

    def _notes_moved(self) -> bool:
        """Whether a group with no line now says something other than what the pane drew.

        A step that started, or a run that ended, changes the note of a step with no output even when no
        log row arrives (item 5). Checked against the groups the pane holds, never the store."""
        for group in self._groups:
            if group[2] or self._dropped.get(group[0]):
                continue
            if self._note_for(group[0]) != self._notes.get(group[0]):
                return True
        return False

    def _render(self) -> list[str]:
        """The text the pane shows, top to bottom: the notice, then each group's heading, notes and lines."""
        out: list[str] = []
        self._wanted_block_keys = {}

        def draw(text: str, anchor: tuple) -> None:
            for offset, line in enumerate(text.split("\n")):
                self._wanted_block_keys[len(out)] = (*anchor, offset)
                out.append(line)

        rows_by_group: dict[tuple, list[dict]] = {}
        for row, key in zip(self._rows, self._row_keys):
            rows_by_group.setdefault(key, []).append(row)
        if self._dropped:
            draw(LOG_TRIM_NOTICE, ("notice",))
        self._notes = {}
        for key, heading, lines in self._groups:
            draw(heading, ("heading", key))
            dropped = self._dropped.get(key, 0)
            if dropped:
                draw(_dropped_note(dropped), ("note", key))
            if lines:
                for row in rows_by_group.get(key, []):
                    draw(_format_log_line(row), ("row", key, int(row.get("seq") or 0)))
            elif not dropped:
                self._notes[key] = self._note_for(key)
                draw(self._notes[key], ("note", key))
        return out

    def _redraw(self) -> None:
        """Show the grouping the pane holds: append when the drawn text is its prefix, else repaint."""
        wanted = self._render()
        drawn = self._text.toPlainText().splitlines()
        if wanted == drawn:
            return
        if drawn and wanted[:len(drawn)] == drawn:
            self._append(wanted[len(drawn):])  # only the tail grew: the drawing position is untouched
        else:
            self._repaint(wanted)  # a group moved, or a note or the notice changed: redraw the whole text
        self._drawn_block_keys = self._wanted_block_keys.copy()

    def _repaint(self, wanted: list[str]) -> None:
        """Replace the pane's whole text, keeping the Operator's place in it (ticket 39 review)."""
        bar = self._text.verticalScrollBar()
        reading_the_tail = bar.value() >= bar.maximum()
        top_block = self._text.firstVisibleBlock().blockNumber()
        anchor = self._drawn_block_keys.get(top_block)
        self._text.setPlainText("\n".join(wanted))
        if reading_the_tail:
            bar.setValue(bar.maximum())
        else:
            blocks = {key: block for block, key in self._wanted_block_keys.items()}
            target = blocks.get(anchor)
            if target is None and anchor and anchor[0] == "row":
                # A trimmed row moves to the next kept row in its group. If the
                # group was emptied, its heading still records the omitted output.
                target = next((block for block, key in self._wanted_block_keys.items()
                               if key[0] == "row" and key[1] == anchor[1]
                               and key[2] >= anchor[2]), None)
                if target is None:
                    target = blocks.get(("heading", anchor[1], 0))
            if target is None:
                target = next((blocks[key] for block, key in self._drawn_block_keys.items()
                               if block >= top_block and key in blocks), 0)
            # QPlainTextEdit scrolls in visual lines, including wrapped document blocks.
            block = self._text.document().begin()
            visual_line = 0
            while block.isValid() and block.blockNumber() < target:
                visual_line += max(1, block.layout().lineCount())
                block = block.next()
            bar.setValue(visual_line)
        self._full_renders += 1

    def _append(self, new: list[str], seqs: list[int] | None = None) -> None:
        """Add lines at the end of the pane without disturbing the scroll (ticket 39 review)."""
        if not new:
            return
        bar = self._text.verticalScrollBar()
        reading_the_tail = bar.value() >= bar.maximum()
        where = bar.value()
        first_block = self._text.document().blockCount()
        self._text.appendPlainText("\n".join(new))
        if seqs is not None:
            for line, seq in zip(new, seqs):
                group_key = self._row_keys[-1]
                for offset, _ in enumerate(line.split("\n")):
                    self._drawn_block_keys[first_block] = ("row", group_key, seq, offset)
                    first_block += 1
        # Appending can scroll to the new text; put the view back unless the Operator was already
        # at the end, in which case the tail is what they want to see.
        bar.setValue(bar.maximum() if reading_the_tail else min(where, bar.maximum()))

    @staticmethod
    def _steps(nodes: list[dict]) -> tuple[list[dict], set]:
        """The scheduled Test Steps in schedule order, and their ``node_seq`` set."""
        steps = sorted((n for n in nodes if n.get("kind") == "step"), key=lambda n: n.get("node_seq", 0))
        return steps, {n.get("node_seq") for n in steps}

    @staticmethod
    def _row_key(row: dict, step_seqs: set, actions: list[dict], live_phase: Optional[str]) -> tuple:
        """Which group a log row belongs to (packet 40a, fix 2 item 4: decided by row order, not by time).

        A row owned by a step (`node_seq`) goes under that step. A row with an `action_seq` belongs to a
        Pre-/Post-test Action of that schedule instance. ``action_seq`` names the instance, not one
        Action: an instance has a Pre-test Action and a Post-test Action, and each may have several. The
        engine writes an Action's row when the Action ends, after every line it logged, and both the
        log rows and the Action rows number from the run's one counter (``seq``). So the Action that owns
        a line is the first Action row of its instance written after it. A line with no such row belongs
        to the Action running now, whose phase the session announced (``live_phase``). Only when no
        phase is known does a line after a Pre-test row count as Post-test. Everything else (ATE
        warnings, device-close warnings) goes in the `── Run ──` group."""
        node_seq = row.get("node_seq")
        if node_seq in step_seqs:
            return ("step", node_seq)
        action_seq = row.get("action_seq")
        if action_seq is None:
            return RUN_GROUP_KEY
        seq = int(row.get("seq") or 0)
        owned = sorted((a for a in actions if a.get("instance_seq") == action_seq), key=lambda a: int(a.get("seq") or 0))
        for action in owned:
            if int(action.get("seq") or 0) > seq:
                return (action.get("phase"),)
        if live_phase in ("pre", "post"):
            return (live_phase,)
        before = [a for a in owned if int(a.get("seq") or 0) < seq]
        if before and before[-1].get("phase") == "pre":
            return ("post",)
        return RUN_GROUP_KEY

    def _group_model(
        self, rows: list[dict], keys: list[tuple], steps: list[dict], actions: list[dict], live_phase: Optional[str]
    ) -> list[list]:
        """One group per phase and scheduled Test Step, in display order (packet 40a).

        Pre-test first, then the steps in schedule order, then the Post-test Actions, then the run's
        own lines. A group with no line carries a note instead of disappearing, so every scheduled step
        is listed even when it never ran (disabled, skipped, or never reached after a Stop-on-Fail end).
        A group whose lines have all fallen out of the window (packet 40a fix 3, item 2) keeps its
        heading and its "… N earlier lines not shown" note: the Operator still sees that the group
        logged, and how much of it is off screen."""
        groups: list[list] = []
        if self._phase_seen("pre", actions, live_phase, keys) or PRE_GROUP_KEY in self._dropped:
            groups.append([PRE_GROUP_KEY, PRE_GROUP_HEADING, []])
        for position, step in enumerate(steps):
            groups.append([("step", step.get("node_seq")), _step_heading(step, position), []])
        if self._phase_seen("post", actions, live_phase, keys) or POST_GROUP_KEY in self._dropped:
            groups.append([POST_GROUP_KEY, POST_GROUP_HEADING, []])
        index = {group[0]: i for i, group in enumerate(groups)}
        for row, key in zip(rows, keys):
            if key not in index:
                groups.append([key, RUN_GROUP_HEADING, []])
                index[key] = len(groups) - 1
            groups[index[key]][2].append(_format_log_line(row))
        for key in self._dropped:  # every line of this group is outside the window
            if key not in index:
                groups.append([key, RUN_GROUP_HEADING, []])
                index[key] = len(groups) - 1
        return groups

    @staticmethod
    def _phase_seen(phase: str, actions: list[dict], live_phase: Optional[str], keys: list[tuple]) -> bool:
        """Whether a Pre-/Post-test group deserves to be drawn (packet 40a review, item 4).

        It does when an Action row of that phase has been recorded, when the session announced that
        phase's Action is running, or when a recorded line already belongs to it."""
        return any(a.get("phase") == phase for a in actions) or live_phase == phase or (phase,) in keys

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


def trimmed_counts(store: Any, run_uuid: str, before_seq: int, min_level: Optional[str], nodes: list[dict],
                   actions: list[dict], live_phase: Optional[str] = None) -> dict[tuple, int]:
    """How many rows older than ``before_seq`` a pane's window left out, per group (packet 40a fix 2, item 1).

    Read from the store with COUNTs, never by reading the rows: a step's count is its node_seq's, the
    run's is the rows with no owner, and an Action instance's is split into its Pre-test part (the rows
    before the instance's last Pre-test Action row) and its Post-test part. Only the recovery path asks,
    when the window is full."""
    steps, step_seqs = LogPane._steps(nodes)
    counts: dict[tuple, int] = {}
    per_instance: dict[int, int] = {}
    for row in store.log_line_counts(run_uuid, before_seq, min_level=min_level):
        if row["action_seq"] is not None:
            per_instance[row["action_seq"]] = per_instance.get(row["action_seq"], 0) + row["count"]
            continue
        key = ("step", row["node_seq"]) if row["node_seq"] in step_seqs else RUN_GROUP_KEY
        counts[key] = counts.get(key, 0) + row["count"]
    for instance, total in per_instance.items():
        pre_ends = [int(a.get("seq") or 0) for a in actions if a.get("instance_seq") == instance and a.get("phase") == "pre"]
        if pre_ends:
            pre = sum(row["count"] for row in store.log_line_counts(
                run_uuid, min(before_seq, max(pre_ends)), min_level=min_level, action_seq=instance))
        else:
            pre = total if live_phase == "pre" else 0
        for key, part in ((PRE_GROUP_KEY, pre), (POST_GROUP_KEY, total - pre)):
            if part:
                counts[key] = counts.get(key, 0) + part
    return counts


def draw_log_panes(log_pane: LogPane, debug_window: LogPane, store: Any, run_uuid: str, nodes: list[dict],
                   actions: list[dict], **context: Any) -> None:
    """Full render of both log panes from the newest rows the store holds (packet 40a fix 2, item 2).

    The recovery path: activation, restore, run start and a pane with nothing drawn. Each pane reads at
    most :data:`LOG_PANE_LIMIT` rows of its own level filter — the Log tab INFO and above, the Debug window
    every level — and the rows it left out are counted per group, never read. Recovery keeps exactly
    the newest limit rows when available; the live window is a suffix of that recovery window.
    Each group counts all its stored rows minus the rows held in that pane."""
    for pane, min_level in ((log_pane, "INFO"), (debug_window, None)):
        rows = store.log_lines_tail(run_uuid, LOG_PANE_LIMIT, min_level=min_level)
        dropped: dict[tuple, int] = {}
        if rows and len(rows) >= LOG_PANE_LIMIT:
            dropped = trimmed_counts(store, run_uuid, rows[0]["seq"], min_level, nodes, actions, context.get("live_phase"))
        pane.add_lines(rows, nodes, actions, dropped=dropped, **context)


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
#: The bar's own box (packet 40b): the padding, the spacing between items, the extra gap
#: before Abort (D6) and the buttons' `min-height`. `FailurePromptBar.heightForWidth` needs
#: these to work out how much of the bar's width is left for the message.
_PROMPT_MARGIN_X = 16
_PROMPT_MARGIN_Y = 10
_PROMPT_SPACING = 12
_PROMPT_ABORT_SPACING = 16

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


def _wrap_lines(text: str, font, width: int) -> list[str]:
    """The lines word wrap would draw for `text` squeezed into `width` pixels with ``font``.

    Embedded newlines are kept as line breaks; each resulting chunk is then broken at word
    boundaries the way Qt's own word wrap does, and breaks inside a word when the word alone
    is wider than the column: a 300-character device id, path or hex dump has no spaces to
    break at, and Qt paints it as one line that runs out of the cell (packet 40b review)."""
    metrics = QFontMetrics(font)
    width = max(1, width)
    lines = []
    for chunk in (text.splitlines() or [""]):
        used = ""
        for word in chunk.split():
            if used and metrics.horizontalAdvance(f"{used} {word}") <= width:
                used = f"{used} {word}"
                continue
            if used:
                lines.append(used)
                used = ""
            # the word alone does not fit either: split it where the column ends it
            while len(word) > 1 and metrics.horizontalAdvance(word) > width:
                lo, hi = 1, len(word)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if metrics.horizontalAdvance(word[:mid]) <= width:
                        lo = mid
                    else:
                        hi = mid - 1
                lines.append(word[:lo])
                word = word[lo:]
            used = word
        lines.append(used)
    return lines


def _wrapped_height(text: str, font, width: int) -> int:
    """How tall word-wrapped `text` is when squeezed into `width` pixels with ``font``."""
    return len(_wrap_lines(text, font, width)) * QFontMetrics(font).lineSpacing()


class WrappedTextDelegate(QStyledItemDelegate):
    """Paint a step-table cell wrapped inside the cell (packet 40b).

    Qt's own cell drawing wraps only at spaces: an 80 px Actual column holding a 300-character
    hex dump paints one line that runs out of the cell, and it measures the wrap against the
    whole column width rather than the 74 px rect the style leaves for the text, so a row sized
    for 80 px clips the last line. This delegate draws the text itself with Qt's wrap — word
    boundaries only, with a token that is by itself wider than the column cut where the column
    ends it — and measures :meth:`sizeHint` with the same call at the same rect, so what is
    drawn and what the row is sized for are the same lines. The model keeps the raw text, so
    tooltips, copy and the tests still see the value the engine wrote."""

    #: The gap the style leaves between the cell edge and the text inside it: ink in an 80 px
    #: column starts 3 px in, so the text rect is the column minus 3 px on each side.
    TEXT_MARGIN = 3
    #: Wrap at word boundaries only. `TextWrapAnywhere` adds "break the line wherever it runs
    #: out", which cuts ordinary words in half: a 74 px Actual column read "c/hannel",
    #: "wi/ndow", "me/asured" (40b review, second round). A token with no space in it is cut
    #: by :meth:`_wrapped_text` before Qt ever sees it, because word wrap alone leaves such a
    #: token as one line running out of the cell.
    WRAP_FLAGS = Qt.TextFlag.TextWordWrap

    def _wrapped_text(self, text: str, font, width: int) -> str:
        """`text` broken into the exact lines this cell paints at `width` px.

        The same :func:`_wrap_lines` the row height is measured with, and the same rule the
        tests' reference render uses: break at a space, and break inside a word only when that
        word alone is wider than the column."""
        return "\n".join(_wrap_lines(text, font, max(1, width)))

    def _text_rect(self, option) -> QRect:
        """The rect inside the cell that the text is drawn in."""
        rect = option.rect
        return QRect(rect.left() + self.TEXT_MARGIN, rect.top() + self.TEXT_MARGIN,
                     max(1, rect.width() - 2 * self.TEXT_MARGIN),
                     max(1, rect.height() - 2 * self.TEXT_MARGIN))

    def _font(self, item, option):
        """The font this cell is painted in: the item's own, or the table's."""
        font = item.data(Qt.ItemDataRole.FontRole) if hasattr(item, "data") else None
        return font if font is not None else option.font

    def _ink_pen(self, option) -> QColor:
        """The colour the text is drawn in: the palette's, or the highlight's on a selected cell."""
        palette = option.palette
        if option.state & QStyle.StateFlag.State_Selected:
            return palette.highlightedText().color()
        return palette.text().color()

    def paint(self, painter, option, item) -> None:  # noqa: N802 - Qt's name
        # The style draws the background, the icon and the border; the text is drawn here, so the
        # style is handed an option with no text — left to itself it elides a long value to one
        # line. The item keeps the raw value, so the cell still reads as the engine wrote it.
        adjusted = QStyleOptionViewItem(option)
        self.initStyleOption(adjusted, item)
        text = adjusted.text or ""
        adjusted.text = ""
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, adjusted, painter, option.widget)
        painter.save()
        font = self._font(item, adjusted)
        rect = self._text_rect(adjusted)
        painter.setFont(font)
        painter.setPen(self._ink_pen(adjusted))
        painter.drawText(rect,
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | self.WRAP_FLAGS,
                         self._wrapped_text(text, font, rect.width()))
        painter.restore()

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt's name
        """The cell's height at its own width: the wrapped lines plus the style's margins."""
        hint = super().sizeHint(option, index)
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        rect = self._text_rect(option)
        font = self._font(index, option)
        needed = QFontMetrics(font).boundingRect(
            QRect(rect.left(), rect.top(), rect.width(), 10000), self.WRAP_FLAGS,
            self._wrapped_text(text, font, rect.width())).height()
        return QSize(hint.width(), max(hint.height(), needed + 2 * self.TEXT_MARGIN))


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
    declarations. No schedule is executed here — the LuaHost only loads the file. Packet 43: the
    schedule is looked up through the scan the session holds, so previewing a pick never walks
    the whole catalogue again."""
    config = session.config
    if config is None or not schedule_name:
        return []
    found = session.schedule_for(schedule_name)
    if found is None:
        return []
    return list(found[2].tree)


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


#: Each required field's name as the Operator sees it (packet 41): when Start is refused, the
#: error line names the fields that are still blank, so they must read like the labels do.
_FIELD_LABELS = {"project": "Project", "operator": "Operator", "uut_type": "UUT Type", "serial": "Serial"}


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

    #: The fields a Test Run cannot start without, in the mockup's order (packet 41). Each name
    #: is what the error line shows; `blank_fields` reads it back from the matching `<name>_edit`.
    REQUIRED_FIELDS: tuple[str, ...] = ("project", "operator", "uut_type", "serial")

    def blank_fields(self) -> list[str]:
        """The labels of the required fields that are empty, or hold only spaces.

        Packet 41: an Operator who types a space has not filled the field, so the check strips
        before it decides. The `required` placeholders on the edits are what shows which ones are
        still missing."""
        return [
            _FIELD_LABELS[name]
            for name in self.REQUIRED_FIELDS
            if not getattr(self, f"{name}_edit").text().strip()
        ]

    def fields_filled(self) -> bool:
        """True when Project, Operator, UUT Type and Serial all have something typed in."""
        return not self.blank_fields()


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
        # One click fires both signals, so the handler remembers the selection state it last acted on
        # and ignores the second signal of the same change (packet 43 review).
        self.schedule_list.itemSelectionChanged.connect(self._on_selection)
        self.schedule_list.currentItemChanged.connect(self._on_selection)
        self._handled_selection: tuple = ()
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
        definitions = find_definitions(*config.test_definition_dirs)
        # Packet 43: the scan made here is the scan the session keeps. Selecting a row from now on
        # reads these rows and the schedules they name — no walk of the Test Definition
        # directories until the Operator presses Rescan and refresh runs again.
        self.session.set_catalog_scan(definitions)
        # Packet 43 review fix: the rows and the ⚠ flags come from that same scan. Asking the catalog
        # for each of them walked the directories two more times per Rescan.
        self._rows = list(schedule_rows(definitions))
        self._mismatch_flags = schedule_mismatches(definitions)
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
        """Record the newly selected schedule on the session, or clear the picker — once per change.

        The exact name is read back from the row's user data (set when the list was built), so this
        hands the session precisely what the Operator picked; ``select_schedule`` then moves the
        session to SELECTING while idle. A broken row carries no user data, so it can never reach the
        session: the previous choice is dropped, the notes and the mismatch flag are emptied, and
        Start stays disabled (packet 05b review).

        One click changes the current item and the selection together, so Qt fires both signals and
        this handler ran twice per click (packet 43 review): the session was handed the schedule twice
        and the layout loaded its steps twice. The state last acted on — the current row, its name and
        the rows actually selected — is remembered, so the second signal of that one change does
        nothing; an arrow key moves the current item and counts as a selection like a click. A
        selection that was dropped and re-made on the same row is a different state, so picking a row
        again after deselecting it selects again (packet 43 review fix 2)."""
        item = self.schedule_list.currentItem()
        name = (item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""
        state = (id(item), name, tuple(id(row) for row in self.schedule_list.selectedItems()))
        if state == self._handled_selection:
            return  # the other signal of the same selection change
        self._handled_selection = state
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


class _SyncPromptBarHeight(QObject):
    """Keep the prompt bar as tall as the message it is showing (packet 40b fix).

    `setWordWrap` makes the label wrap but leaves the bar at the buttons' height, so the bar
    clips its own text. The height the wrapped message needs only exists once the bar has a
    width, which is exactly what the Resize event reports: each time the layout gives the
    bar a width, the bar re-measures its height against it."""

    def __init__(self, bar: "FailurePromptBar") -> None:
        super().__init__(bar)
        self._bar = bar

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt's name
        if event.type() == QEvent.Type.Resize:
            self._bar.sync_height_at(watched.width())
            self._bar.geometry_changed.emit()
        return False


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
    geometry_changed = Signal()

    def __init__(self, style: str = "") -> None:
        super().__init__()
        # A plain QWidget never paints its stylesheet background, so the bar showed up grey
        # (packet 12d review): opt it into styled backgrounds, and give the labels their own
        # colour rule because a stylesheet `color` does not cascade to child QLabels.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QGridLayout(self)
        layout.setContentsMargins(_PROMPT_MARGIN_X, _PROMPT_MARGIN_Y, _PROMPT_MARGIN_X, _PROMPT_MARGIN_Y)
        layout.setSpacing(_PROMPT_SPACING)

        self.step_label = QLabel()
        self.step_label.setObjectName("prompt_step")
        self.message_label = QLabel()
        # The message grows to fill whatever width the labels leave, like the mockup's .grow.
        # Packet 40b (issue #3): the Actual/Expected the prompt carries can be long and
        # multiline, so the label wraps inside the bar instead of running off its edge — and
        # :meth:`heightForWidth` makes the bar as tall as that wrapped text, because a
        # wrapping label in a bar sized for the buttons still clips it (40b review).
        self.message_label.setWordWrap(True)
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
        # Polish before consulting style hints: all controls keep the widest label's
        # text and padding, even when the bar must put them in a column.
        controls = (self.continue_button, self.retry_button, self.abort_button)
        for button in controls:
            button.ensurePolished()
        self._button_width = max(
            max(button.sizeHint().width(), button.minimumSizeHint().width(), 80,
                QFontMetrics(ui_font(_PROMPT_BUTTON_FONT_PX)).horizontalAdvance(button.text())
                + 2 * (_PROMPT_BUTTON_PAD_X + _PROMPT_BUTTON_BORDER))
            for button in controls
        )
        for button in controls:
            button.setFixedWidth(self._button_width)

        self.continue_button.clicked.connect(lambda: self._on_choice("CONTINUE"))
        self.retry_button.clicked.connect(lambda: self._on_choice("RETRY"))
        self.abort_button.clicked.connect(lambda: self._on_choice("ABORT"))

        self._buttons = QWidget(self)
        buttons = QHBoxLayout(self._buttons)
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(0)
        buttons.addWidget(self.continue_button)
        buttons.addSpacing(_PROMPT_SPACING)
        buttons.addWidget(self.retry_button)
        buttons.addSpacing(_PROMPT_SPACING + _PROMPT_ABORT_SPACING)
        buttons.addWidget(self.abort_button)
        buttons.addStretch()
        self._buttons_vertical = False
        self._stacked = None
        self._arrange(self.width())

        # Only shown while a prompt is pending; the layout shows it via show_prompt.
        # The bar's height must follow its wrapped message, and that height only exists once
        # the bar has a width — the Resize event is when the width is known (packet 40b fix).
        self.installEventFilter(_SyncPromptBarHeight(self))
        self.hide()

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt's name
        """The bar's height depends on its width: the message label wraps (packet 40b)."""
        return True

    def _needs_stack(self, width: int) -> bool:
        # Reserve a useful message column rather than squeezing Actual/Expected
        # into a sliver beside three fixed-width controls.
        return width < (2 * _PROMPT_MARGIN_X + self.step_label.sizeHint().width()
                        + 240 + self._horizontal_button_width() + 2 * _PROMPT_SPACING)

    def _horizontal_button_width(self) -> int:
        return 3 * self._button_width + 2 * _PROMPT_SPACING + _PROMPT_ABORT_SPACING

    def _vertical_buttons(self, width: int) -> bool:
        return width - 2 * _PROMPT_MARGIN_X < self._horizontal_button_width()

    def _arrange(self, width: int) -> None:
        vertical = self._vertical_buttons(width)
        if vertical != self._buttons_vertical:
            self._buttons_vertical = vertical
            self._buttons.layout().setDirection(
                QBoxLayout.Direction.TopToBottom if vertical else QBoxLayout.Direction.LeftToRight
            )
        stacked = self._needs_stack(width)
        if stacked == self._stacked:
            return
        self._stacked = stacked
        layout = self.layout()
        for widget in (self.step_label, self.message_label, self._buttons):
            layout.removeWidget(widget)
        if stacked:
            layout.addWidget(self.step_label, 0, 0, 1, 3)
            layout.addWidget(self.message_label, 1, 0, 1, 3)
            layout.addWidget(self._buttons, 2, 0, 1, 3)
        else:
            layout.addWidget(self.step_label, 0, 0)
            layout.addWidget(self.message_label, 0, 1)
            layout.addWidget(self._buttons, 0, 2)
        layout.setColumnStretch(1, 1)

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        """Measure the same message width and control rows used by the responsive layout."""
        self.step_label.ensurePolished()
        self.message_label.ensurePolished()
        stacked = self._needs_stack(width)
        label_width = width - 2 * _PROMPT_MARGIN_X
        if not stacked:
            label_width -= (self.step_label.sizeHint().width()
                            + self._horizontal_button_width() + 2 * _PROMPT_SPACING)
        label_height = self.message_label.heightForWidth(max(40, label_width))
        if label_height < 0:
            label_height = _wrapped_height(self.message_label.text(), self.message_label.font(),
                                           max(40, label_width))
        button_height = max(button.sizeHint().height() for button in
                            (self.continue_button, self.retry_button, self.abort_button))
        if self._vertical_buttons(width):
            button_height = 3 * button_height + 2 * _PROMPT_SPACING + _PROMPT_ABORT_SPACING
        if stacked:
            content = (self.step_label.sizeHint().height() + label_height
                       + button_height + 2 * _PROMPT_SPACING)
        else:
            content = max(button_height, label_height, self.step_label.sizeHint().height())
        return max(content + 2 * _PROMPT_MARGIN_Y,
                   self.layout().totalHeightForWidth(width) if stacked == self._stacked else 0)

    def sync_height_at(self, width: int) -> None:
        """Arrange and measure the prompt at the width it will actually receive."""
        if width > 0:
            self._arrange(width)
            self.setMinimumHeight(self.heightForWidth(width))
            self.updateGeometry()

    def sync_height(self) -> None:
        """Re-measure the bar's height against the width it currently has."""
        self.sync_height_at(self.width() if self.width() > 0 else self.sizeHint().width())

    def show_prompt(self, step_number: str, result: str, message: str, actual: str = "", expected: str = "") -> None:
        """Show the bar naming the failed step and its message, for the Operator to answer.

        R6: the bar carries the step's measurements too — Actual and Expected appear in the
        prompt whenever the Attempt recorded them, not only in the report."""
        self.step_label.setText(f"Step {step_number} {result}")
        detail = ""
        if actual or expected:
            detail = f"  Actual {actual or '—'} · Expected {expected or '—'}"
        self.message_label.setText(f"{message}{detail}")
        self.sync_height()  # the label wraps, so the bar has to grow with it (packet 40b)
        self.show()
        # The staged Run view may still be hidden when a prompt first arrives.
        self._buttons.layout().activate()

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

    #: The detail line's font: the row's own text uses the item's font, the Actual/Expected
    #: line the mono font it is painted in (packet 40b measures the wrap with the same font).
    DETAIL_FONT_PX = 11

    def _detail_font(self):
        """The mono font the detail line is painted in — shared with the row-height sizing."""
        return mono_font(self.DETAIL_FONT_PX)

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
            # The badge sits at the right edge, top-aligned with the step name; `_step_row_height`
            # has grown the row for the detail lines below it, so a wrapped row keeps the badge
            # level with the name rather than floating off the row (packet 40b).
            badge = QRect(rect.right() - badge_width - 2, rect.top() + 4, badge_width, 18)
            paint_result_badge(painter, badge, result, word=result)
        detail = item.data(STEP_DETAIL_ROLE) or ""
        if detail:
            font = self._detail_font()
            painter.setFont(font)
            painter.setPen(_detail_pen(fill))
            # Packet 40b (issue #3): the stage layout printed only the first line of a
            # multiline Actual/Expected. Pre-wrapping the detail at the width the row leaves
            # beside the badge and drawing the joined lines paints every line, and the row's
            # size hint (`_step_row_height`) grows to make room for them.
            wrapped = "\n".join(_wrap_lines(detail, font, max(0, rect.width() - left - badge_width - 6)))
            detail_rect = QRect(left, rect.top() + 22, max(0, rect.width() - left - badge_width - 6),
                                max(12, rect.height() - 22))
            painter.drawText(detail_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, wrapped)
        painter.restore()


#: Width assumed for a step list that has not been laid out yet when the row hints are
#: computed: Qt reports a viewport that has never been sized as 0 px wide, and a wrapped row
#: cannot be measured against nothing. This is only a stand-in for the first paint —
#: `_RefitStepRows` re-measures every row as soon as the viewport has a width. (Packet 40b
#: review: the earlier claim that the Run view never makes the step list narrower than this
#: was wrong — a narrow window puts the list at 240 px, and rows sized for 500 px clipped
#: their detail.)
_STEP_LIST_FALLBACK_WIDTH = 500


def _step_row_height(result: str, detail: str, list_width: int, emphasis: str = "") -> int:
    """The height a staged step row needs: 30 px without a detail line, 38 px with a single
    wrapped line, plus one more line of wrapped detail on top (packet 40b).

    The badge and the emphasis bar take room off the row, so the detail is measured as it
    will be painted — wrapped into the width left beside the badge, with long words broken
    inside the word. A list not sized yet falls back to ``_STEP_LIST_FALLBACK_WIDTH`` until
    the viewport reports its real width."""
    if not detail:
        return 30
    list_width = list_width if list_width > 0 else _STEP_LIST_FALLBACK_WIDTH
    delegate = StepRowDelegate()
    badge = delegate._badge_width(result, QRect(0, 0, list_width, 0)) if result else delegate.BADGE_MIN_WIDTH
    width = list_width - (5 if emphasis else 4) - badge - 6
    font = delegate._detail_font()
    wrapped = "\n".join(_wrap_lines(detail, font, max(1, width)))
    # Measure the explicit lines with the same flags as paint(), rather than
    # assuming lineSpacing() is the height drawText() uses on every platform.
    height = QFontMetrics(font).boundingRect(
        QRect(0, 0, max(1, width), 10000),
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, wrapped,
    ).height()
    return max(38, 22 + height + 3)


class _IncrementalRowFit(QObject):
    """Fit visible rows on scroll; drain width-invalidated rows eight per timer turn.

    A new width discards the previous generation's backlog. Both the timer and the
    resize filter belong to the view, so neither can outlive it. The margin readies
    adjacent rows before a small scroll; the scroll hook also handles arbitrary jumps.
    """

    CHUNK = 8
    MARGIN = 2

    def __init__(self, view, fit_row):
        super().__init__(view)
        self.view = view
        self.fit_row = fit_row
        self._cursor = 0
        self._generation = 0
        self._done = set()
        self._busy = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self.tick)
        view.viewport().installEventFilter(self)
        view.verticalScrollBar().valueChanged.connect(self.fit_visible)

    def restart(self):
        self._generation += 1
        self._cursor = 0
        self._done.clear()
        self._timer.start()
        self.view.viewport().update()

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() == QEvent.Type.Resize:
            self.restart()
        elif event.type() == QEvent.Type.Paint:
            # A queued zero-delay timer can run after the first resized paint.
            self.fit_visible()
        return False

    def _fit(self, row):
        if row not in self._done:
            self._done.add(row)
            self.fit_row(row)

    def fit_visible(self):
        if self._busy:
            return
        self._busy = True
        try:
            model = self.view.model()
            count = model.rowCount()
            first = max(0, self.view.indexAt(QPoint(1, 0)).row())
            # Re-evaluate after fitting: shrinking a row can expose another row.
            row = max(0, first - self.MARGIN)
            while row < count:
                rect = self.view.visualRect(model.index(row, 0))
                if rect.top() > self.view.viewport().height():
                    for extra in range(row, min(count, row + self.MARGIN)):
                        self._fit(extra)
                    break
                self._fit(row)
                row += 1
        finally:
            self._busy = False

    def tick(self):
        generation = self._generation
        self.fit_visible()
        if generation != self._generation:
            return
        count = self.view.model().rowCount()
        end = min(count, self._cursor + self.CHUNK)
        for row in range(self._cursor, end):
            self._fit(row)
        if generation != self._generation:
            return
        self._cursor = end
        if end < count:
            self._timer.start()


class _RefitStepRows(_IncrementalRowFit):
    """Bounded width refits for the staged list, using its laid-out viewport width."""

    def __init__(self, run_view):
        self._sizes = {}
        super().__init__(run_view.step_list, self._fit_row)

    def _fit_row(self, index):
        widget = self.view
        item = widget.item(index)
        if widget.itemWidget(item) is not None:
            return
        detail = item.data(STEP_DETAIL_ROLE) or ""
        result = item.data(STEP_RESULT_ROLE) or ""
        signature = (detail, result, widget.viewport().width(), item.data(STEP_EMPHASIS_ROLE) or "")
        if self._sizes.get(index) == signature:
            return
        self._sizes[index] = signature
        if detail:
            item.setSizeHint(QSize(0, _step_row_height(result, detail, signature[2], signature[3])))


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

    #: The refusal the session gave the last Start clicked here, "" when there is none. A refusal is
    #: about the inputs it was refused with: it keeps Start off until one of them moves (packet 41
    #: fix2 review), and every refresh that runs because an input did drops it.
    _refusal = ""

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

    def _runnable_selection(self) -> bool:
        """Whether a Test Schedule is selected and can run (packet 41).

        The picker's current row is the Operator's pick, and a broken row carries no name, so a
        selection made there is dropped before it ever reaches the session (packet 05b). When the
        layout never picked through the picker — a session handed a schedule by History, or a test
        that selects one directly — the session's own selection is what `start()` uses, so it counts.
        """
        name = self.session.selected_schedule
        if not name:
            return False
        picked = self.picker.selected_name()
        return picked is None or picked == name

    def can_start(self) -> bool:
        """Whether a Test Run can start from this layout right now (packet 41).

        Three things have to hold at once: every required field — Project, Operator, UUT Type,
        Serial — has something in it after `strip()`; a Test Schedule is selected and it is not a
        broken row (packet 05b); and no run is in progress. A checksum mismatch is not settled
        here: the confirmation dialog in :meth:`begin_run` is the select/confirm flow that already
        exists, and a cancelled one starts nothing. A run that has FINISHED is not in progress, so
        the next one may begin as soon as the fields and the schedule are good again — which they
        are, because starting a run never clears a field (spec 37). A session refusal is a
        message, not a lock: retrying asks the session to validate the schedule again."""
        session = getattr(self, "session", None)
        if session is None:
            return False
        if getattr(self, "state", SessionState.IDLE) == SessionState.STARTED or session.state == SessionState.STARTED:
            return False
        if self.fields.blank_fields():
            return False
        return self._runnable_selection()

    def _missing_field_phrase(self, missing: list[str]) -> str:
        """The blank fields named in the order the form shows them: one name, or "A, B and C".

        Packet 41 fix2 review: the two messages a layout shows — why Start is off, and why the fields
        stage cannot move on — repeat this list, so its wording lives in one place."""
        if len(missing) == 1:
            return missing[0]
        return f"{', '.join(missing[:-1])} and {missing[-1]}"

    def _start_problem(self) -> str:
        """What the Start line says in the words the Operator sees; "" when there is nothing to say.

        Packet 41: every reason a run cannot start names itself, so a greyed Start is never just
        grey. One blank field reads as the Project message the layouts have carried since packet
        30a r3; several blanks name them in the order the form shows them; a schedule that cannot
        run, or none at all, says so. A refusal the session gave the Start just clicked is said
        until an input moves (packet 41 fix2 review): it is a message about an attempt, not a
        lock — Start can be enabled and still carry it. A run already going is not written
        here: it is not an error, and the faded Start already says it (packet 41 fix2 review)."""
        if self._refusal:
            return self._refusal
        missing = self.fields.blank_fields()
        if missing:
            verb = "is" if len(missing) == 1 else "are"
            return f"{BROKEN_MARKER} {self._missing_field_phrase(missing)} {verb} required before a Test Run can start"
        if not self._runnable_selection():
            return f"{BROKEN_MARKER} No runnable Test Schedule is selected"
        return ""

    def _fields_problem(self) -> str:
        """Why the fields stage cannot move on, in the words the Operator sees (packet 41 review).

        The fields stage has no Start to fade: its forward control is Next, and a greyed Next on its
        own says nothing. This is the same list of blanks `_start_problem` names, worded for the
        stage the Operator is actually on — the field to fill, not the run to start."""
        missing = self.fields.blank_fields()
        if not missing:
            return ""
        return f"{BROKEN_MARKER} Fill in {self._missing_field_phrase(missing)} to continue"

    def _start_inputs_changed(self) -> None:
        """One of the inputs a Start depends on moved, so the last refusal explains nothing.

        Packet 41 fix2 review: the session's "schedule 'Passing schedule' is not available" sat on the
        error line and kept Start disabled forever — only a run that actually started cleared it — so
        after one keystroke Start was live under a message about a schedule the Operator had already
        moved on from, a blank field was still blamed on that schedule, and a Rescan that brought the
        file back changed nothing. A field typed, a schedule picked, a Rescan or a state change is the
        Operator moving the very inputs the refusal answered, so the refusal goes and the layout's
        normal refresh shows what those inputs now say the start problem is, which may be nothing."""
        if self._refusal:
            self._refusal = ""
            # The line holds another path's message; only that path can write over it, so clear it
            # here before the refresh writes the current reason.
            if self.error_label.text() and self.error_label.property("error_owner") != "start":
                self._show_error("", "refused")
        self._refresh_start_state()

    def _refresh_start_state(self) -> None:
        """Re-run this layout's Start refresh, under one name (packet 41 fix2).

        The three-pane layout calls it `_update_start_button` and the staged one `_update_run_button`;
        code that does not know which layout is in front — a layout switch, for one — needs a single
        name for the same action. Every layout using this mixin supplies its own."""
        raise NotImplementedError("a run layout must implement its own Start refresh")

    def _show_error(self, message: str, owner: str) -> None:
        """Write ``message`` on the pane's error line, unless another path's message is there.

        Packet 41 review: the layout refreshes this line on every keystroke and every state change,
        and it used to write its own reason unconditionally — erasing a message another path put
        there, such as the session's refusal when the picked schedule broke (packet 05b). The line
        remembers who wrote it, so a refresh only overwrites text it wrote itself and clears only
        that. A message from another path stays until that path replaces it, a run starts, or the
        inputs change (packet 41 fix2 review): see :meth:`_start_inputs_changed`."""
        label = self.error_label
        if label.text() and label.property("error_owner") != owner:
            return
        label.setProperty("error_owner", owner if message else "")
        label.setText(message)

    def new_run(self) -> bool:
        """The Operator's "New run": unlock the session for the next run, clearing nothing.

        A session stays FINISHED after a run, and a FINISHED session has its step boxes locked and
        its options shut (packet 30b review). The only Operator action that unlocks it is picking a
        schedule again (spec 37), so New run re-selects the schedule already picked: that moves
        FINISHED → SELECTING and keeps the step-enable overrides, because ``select_schedule`` drops
        overrides only for a *different* schedule. Nothing here writes the setup fields, so Project,
        Operator, UUT Type, serial and schedule all carry over. Re-selecting does re-read the
        schedule's own :setStopOnFail default, so the choice the Operator made is put back after it.

        The message a refused Start left on the error line survives all of that (packet 41 review).
        It is replaced only when something an Operator could act on moves — a setup field, the
        schedule, the On-failure choice, or an attempted Start — never by New run, which changes no
        input and picks no schedule nobody already had. Re-checking Start reads the current inputs
        without erasing that message."""
        if not self.is_active() or self.session.state == SessionState.STARTED or self.state == SessionState.STARTED:
            return False
        schedule = self.session.selected_schedule
        if schedule:
            stop_on_fail = self.stop_on_fail_radio.isChecked()
            self.session.select_schedule(schedule)
            self._set_stop_on_fail(stop_on_fail)
        # Packet 41 review: New run changes no field and picks no schedule the Operator did not
        # already have, so it is not an input change — the refusal the last Start wrote stays on
        # the line until an input moves or a Start is attempted. Re-checking Start shows what the
        # inputs say now without erasing that message.
        self._refresh_start_state()
        return True

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
        `session.start()`, which queues its signals and returns immediately.

        Packet 41: `can_start()` guards the whole path before anything is written. A blank field, no
        runnable schedule, or a run already going means no run starts — whether the call came from
        the button, a shortcut, or a test calling the slot directly."""
        if not self.is_active():
            return  # the layout that is not showing must not start a run (packet 25b)
        if not self.can_start():
            return  # packet 41: nothing is written and nothing starts while a requirement is missing
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

        try:
            # `pending_mismatches` re-reads the selected release, so a Test Definition broken since
            # the scan is refused before the confirmation is asked (packet 43 review fix 3).
            mismatches = self.session.pending_mismatches()
            store = self._confirm_store_choice(mismatches)
            if mismatches and store is None:
                return  # the Operator cancelled the confirmation: nothing started, nothing cleared.
            self._engine = self.session.start(store_mismatched=store)
            self._refusal = ""
            self._show_error("", "refused")  # a run started: whatever refusal was on the line is over
        except SessionError as exc:
            # The schedule picked before this Start broke in the meantime (packet 05b review): name
            # the reason on the pane the Operator is looking at rather than raising out of the
            # button handler. Keep the current reason until inputs change or another attempt
            # replaces it; Start remains available to retry if the real conditions still hold.
            self._refusal = f"{BROKEN_MARKER} {exc}"
            self._show_error(self._refusal, "refused")
            self._refresh_start_state()

    def _on_action_started(self, phase: str, done: int, total: int) -> None:
        """A Pre-/Post-test Action has begun: move its tally, restart the current-item timer, and
        pick up the lines the worker has logged so far (packet 25a: the log pane is live)."""
        now = self._clock()
        if phase == "pre":
            self._pre_counts = (done, total)
        else:
            self._post_counts = (done, total)
            self._post_test_started = True  # the header chip reads POST-TEST (D4)
        # Packet 40a review, item 4: the Action's own row is only written when it ends, so the
        # phase the session just announced is what puts its lines in the right group as they land.
        self._live_phase = phase
        self._live_run_order = done - 1  # the engine numbers each phase's Actions from 0: `done` is one past it
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
        self._arm_log_refresh()  # its note changes from "Not run yet." even if it logs nothing (item 5)
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

    #: Packet 40a review, item 3: how long a layout waits before reading the log it was told
    #: about, so a burst of lines becomes one refresh per interval instead of one per line.
    LOG_COALESCE_MS = 100

    #: The Action the worker is inside (packet 40a review, item 4): the phase the session announced with
    #: `action_started`. A log row carrying an instance's `action_seq` that no Action row has claimed
    #: yet belongs to it. Cleared when that Action's own row lands, and at the start and end of a run.
    _live_phase: Optional[str] = None
    #: That Action's run order within its phase (the `run_order` its row will carry).
    _live_run_order: Optional[int] = None

    def _on_log_appended(self, run_uuid: str) -> None:
        """The worker just recorded log rows (packet 40a): the visible layout reads them soon.

        The notification is explicit — the panes are told a line landed rather than guessing from a
        step finishing or polling the store. A layout that is not on screen defers the read, exactly
        as it defers every other store refresh (packet 25b review). The read is throttled (packet 40a
        fix 2, item 3): the timer is armed by the first notification of an interval and not restarted by
        the rest, so a steady stream of lines refreshes about every :attr:`LOG_COALESCE_MS` instead of
        never, and a burst still lands in one read. A finished run refreshes at once."""
        if not self.is_active():
            self.mark_store_dirty()
            return
        if getattr(self, "state", None) == SessionState.FINISHED:
            self._refresh_log()
            return
        self._arm_log_refresh()

    def _arm_log_refresh(self) -> None:
        """Read the log within :attr:`LOG_COALESCE_MS`, unless a read is already on its way."""
        timer = getattr(self, "_log_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(self.LOG_COALESCE_MS)
            timer.timeout.connect(self._refresh_log)
            self._log_timer = timer
        if not timer.isActive():
            timer.start()

    def _stop_log_timer(self) -> None:
        """Cancel a coalesced read that is still waiting (packet 40a review, item 3).

        The timer fires :meth:`_refresh_log` some time after the notification that started it. If a
        new run has begun in the meantime, its panes were emptied (packet 30b review) and a deferred
        read would paint the *previous* run's lines back into them. Only the visible layout's timer
        is running, so stopping it is enough for both layouts."""
        timer = getattr(self, "_log_timer", None)
        if timer is not None:
            timer.stop()

    def _reset_live_log_state(self) -> None:
        """Forget the Action and the phase of the run before this one (packet 40a fix 2, item 5).

        Called when a run starts and when it ends: a phase left behind would draw a Post-test group the
        next run never had."""
        self._live_phase = None
        self._live_run_order = None

    def _settle_live_phase(self, actions: list[dict]) -> None:
        """Drop the running Action once its own row has landed (packet 40a fix 2, item 4).

        Matched on its phase and run order, not on the phase alone: a second Action of the same phase
        is still running after the first one's row lands."""
        if self._live_phase is None:
            return
        if any(a.get("phase") == self._live_phase and a.get("run_order") == self._live_run_order for a in actions):
            self._live_phase = None
            self._live_run_order = None

    def _loaded_schedule_steps(self) -> list[object]:
        """The expanded schedule rows this layout loaded for the run, in runner order.

        The store only gets a node row once the worker has walked that tree row (packet 15b), so a
        live Log pane grouping purely by `store.nodes()` would list only the steps it has reached.
        Each layout already keeps the loaded tree it painted its step list from, and that is what
        lets the Log name every scheduled step, including the ones not run yet."""
        return []

    def _log_group_rows(self) -> list[dict]:
        """The tree rows the Log pane groups by, in schedule order (packet 40a).

        Recorded rows are used as they are; a scheduled step the worker has not reached yet gets a
        row built from the loaded schedule so it still appears, with a note ("Not run yet." while
        the run is up, packet 40a review item 5) under its label. A layout whose step list nobody
        filled — a run started straight on the session — reads the same expanded tree the picker
        reads, once, and caches it."""
        run_uuid = self.session.run_uuid
        recorded = {node.get("node_seq"): node for node in (self.session.store.nodes(run_uuid) if run_uuid else [])}
        steps = self._loaded_schedule_steps()
        if not steps:
            name = self.session.selected_schedule or ""
            if name and getattr(self, "_log_steps_name", None) != name:
                self._log_steps_name = name
                self._log_steps_cache = load_schedule_steps(self.session, name)
            steps = getattr(self, "_log_steps_cache", [])
        overrides = self.session.node_enables or {}
        rows: list[dict] = []
        for seq, step in enumerate(steps):
            node = recorded.get(seq)
            if node is not None:
                rows.append(node)
                continue
            rows.append({
                "node_seq": seq,
                "kind": "header" if getattr(step, "is_header", False) else "step",
                "step_number": getattr(step, "step_no", "") or "",
                "step_name": getattr(step, "name", "") or "",
                # An Operator toggle beats the row's :setEnable() default, as RunEngine._enabled decides.
                "enabled": bool(overrides.get(seq, getattr(step, "default_enabled", getattr(step, "enabled", True)))),
            })
        return rows or list(recorded.values())

    def _log_panes_context(self, nodes: list[dict], actions: list[dict]) -> dict[str, Any]:
        """What the log grouping needs to know about the run right now (packet 40a, items 4 and 5)."""
        return {
            "nodes": nodes,
            "actions": actions,
            "live_phase": self._live_phase,
            "run_finished": getattr(self, "state", None) == SessionState.FINISHED,
            "started_steps": getattr(self, "_started_steps", set()),
        }

    def _refresh_log(self, incremental: bool = True) -> None:
        """Bring both log panes up to date with the store (packet 25a: the log panes are live).

        Two paths. The live path reads only the rows recorded after the Debug window's last drawn
        seq (`log_lines_after`), and each pane takes its share of them: the Log tab keeps the INFO-and-
        above ones. The recovery path — a pane with nothing drawn yet, activation, a restored run, a
        run start, ``incremental=False`` — draws the newest :data:`LOG_PANE_LIMIT` rows of each level
        through :func:`draw_log_panes`. Neither path reads the whole log (packet 40a fix 2, item 2).
        A layout that is not showing defers the read (packet 25b review)."""
        if not self.is_active():
            self.mark_store_dirty()
            return
        if not self._store_open():
            return  # ticket 39 review: the window released the session, so there is nothing to read
        run_uuid = self.session.run_uuid
        if not run_uuid:
            return
        store = self.session.store
        nodes = self._log_group_rows()
        actions = store.actions(run_uuid)
        self._settle_live_phase(actions)
        context = self._log_panes_context(nodes, actions)
        after = self.debug_window.last_seq()
        if not incremental or after < 0 or not (self.log_pane.is_drawn() and self.debug_window.is_drawn()):
            draw_log_panes(self.log_pane, self.debug_window, store, run_uuid, nodes, actions,
                           live_phase=context["live_phase"], run_finished=context["run_finished"],
                           started_steps=context["started_steps"])
            return
        # Each pane has its own cursor: the Debug window is shared by both layouts, so the other layout may
        # have advanced it while this one was hidden. The Log tab's read filters DEBUG in the store.
        info_rows = store.log_lines_after(run_uuid, self.log_pane.last_seq(), min_level="INFO")
        self.log_pane.append_rows(info_rows, nodes, actions, live_phase=context["live_phase"],
                                  run_finished=context["run_finished"], started_steps=context["started_steps"])
        self.debug_window.append_rows(store.log_lines_after(run_uuid, after), nodes, actions,
                                      live_phase=context["live_phase"], run_finished=context["run_finished"],
                                      started_steps=context["started_steps"])


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
