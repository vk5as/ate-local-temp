"""The fixed light design system (issue 33, recommendation R1; ticket 39).

One style, one palette, one stylesheet: Fusion on every platform, a fixed light
``QPalette``, the platform fonts, and the result-state badges. Both Run layouts
draw their look from the tokens here instead of the 21 inline stylesheets they
used to carry, so a Linux offscreen render is a fair proxy for Windows.

Two deliberate decisions baked into this module:

* **Light only.** ``ui.theme`` is still parsed and stored by the config (reserved
  for a future dark mode, per the accepted choices), but nothing here reads it:
  ATE paints the same fixed light palette whatever the PC's system setting is,
  which is what makes the tinted rows readable on a dark-mode Station.
* **Word, glyph and weight carry a result; hue only supports it.** Badges are
  painted with ``QPainterPath``, never as font characters — ``⛔`` and friends
  draw as missing-glyph boxes on font-poor runners (see ``BROKEN_MARKER``).
"""

from __future__ import annotations

from weakref import WeakSet

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPalette, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication

#: The palette tokens from `33-opus.md` section 3. Every colour the app paints is
#: one of these: the stylesheet below, the badge tables and the layouts' inline
#: styles are all built from this dict, so a review can check one list.
TOKENS = {
    "canvas": "#f3f5f8",  # window background
    "surface": "#ffffff",  # lists, tables, fields
    "subtle": "#eef1f5",  # header rows, disabled faces (the mockup's row tint)
    "line": "#e4e8ee",
    "line_strong": "#c9d0da",
    "field_border": "#7f8a99",
    "ink": "#1a2230",
    "ink_2": "#556070",  # secondary text and answered values: 6.38 on surface
    "ink_disabled": "#6a7482",
    "chrome": "#141c28",
    "head": "#1f2a3a",  # run header; white text is 14.48 on it
    "head_ink": "#c3ccd9",
    "primary": "#0b5cc4",
    "primary_tint": "#dbe8fb",  # the current step
    "ask": "#fff4cc",  # the input waiting on the Operator
    "ask_edge": "#f2b705",
    "ask_ink": "#3d2c00",
    "failed_tint": "#fde8e6",  # failed/paused row, with the red left bar
    "failed": "#b3261e",  # the louder red the mockup's #c62828 copies
    "danger_ink": "#8c1d18",
    "broken_err": "#b3261e",
    # The colours the first pass left inline in the layouts (ticket 39 review).
    "white": "#ffffff",  # plain row background, badge ink on a solid fill
    "pass_fill": "#e3f4e8",  # a PASSED badge
    "pass_ink": "#14532d",
    "error_fill": "#5b1010",  # the darker solid ERROR badge
    "verdict_pass": "#1b7a3d",  # the finished PASS chip, solid unlike the pale rows
    "amber_ink": "#8a5a00",  # the checksum-mismatch note
    "warn_bg": "#fff8e6",  # a picker row carrying a mismatch flag
    "prompt_ink": "#7b1113",  # the inline prompt's text on white
}


def token(name: str) -> str:
    """A palette value by token name."""
    return TOKENS[name]


#: D7: platform fonts, nothing bundled. Qt walks the fallback list.
UI_FONT = ["Segoe UI", "Noto Sans", "DejaVu Sans"]
MONO_FONT = ["Consolas", "Cascadia Mono", "DejaVu Sans Mono"]


def ui_font(px: int = 13, bold: bool = False) -> QFont:
    """The body font at a pixel size — pixels behave the same on Windows and
    Linux, unlike the 9 pt default that depends on logical DPI."""
    font = QFont(UI_FONT)
    font.setPixelSize(px)
    font.setBold(bold)
    return font


def mono_font(px: int = 12) -> QFont:
    """Step numbers, timers and logs (R4): the columns line up only in mono."""
    font = QFont(MONO_FONT)
    font.setPixelSize(px)
    return font


#: Result → (fill, ink, glyph). "The worse the result, the louder it is"
#: (33-opus.md, Result states): PASS pale, FAIL solid, ERROR darker solid,
#: NOREPORT amber, NOTRUN an outline. Contrast against each fill is ≥ 5.6.
RESULT_STYLES = {
    "PASS": (TOKENS["pass_fill"], TOKENS["pass_ink"], "check"),
    "FAIL": (TOKENS["failed"], TOKENS["white"], "cross"),
    "ERROR": (TOKENS["error_fill"], TOKENS["white"], "bang"),
    "NOREPORT": (TOKENS["ask_edge"], TOKENS["ask_ink"], "empty"),
    "NOTRUN": (TOKENS["surface"], TOKENS["ink_2"], "dash"),
    "SKIPPED": (TOKENS["subtle"], TOKENS["ink_2"], "dash"),
    "FORCED_NOTRUN": (TOKENS["subtle"], TOKENS["ink_2"], "dash"),
    "RUNNING": (TOKENS["primary"], TOKENS["white"], "play"),
    "INPUT": (TOKENS["ask_edge"], TOKENS["ask_ink"], "question"),
    "READY": (TOKENS["subtle"], TOKENS["ink_2"], "dash"),
}

#: The finished verdict chip is solid, unlike the pale row badges: a pale green
#: next to a pale red is unreadable, and the solid pair is only safe because the
#: chip carries the glyph and the word too.
VERDICT_STYLES = {
    "PASS": (TOKENS["verdict_pass"], TOKENS["white"], "check"),
    "FAIL": (TOKENS["failed"], TOKENS["white"], "cross"),
}

#: The header chip states (D4 vocabulary): never SessionState names.
CHIP_STYLES = {
    "ready": RESULT_STYLES["READY"],
    "running": RESULT_STYLES["RUNNING"],
    "input": RESULT_STYLES["INPUT"],
    "step_failed": VERDICT_STYLES["FAIL"],
    "pass": VERDICT_STYLES["PASS"],
    "fail": VERDICT_STYLES["FAIL"],
    "notrun": RESULT_STYLES["NOTRUN"],
}


def _glyph_path(glyph: str, box: QRectF) -> QPainterPath:
    """The badge glyph as a path, inside the 12 px square ``box``. No font
    characters: every shape here is drawn, so no runner needs a font that has
    ``✓`` or ``⛔``."""
    x, y, w, h = box.x(), box.y(), box.width(), box.height()
    path = QPainterPath()
    if glyph == "check":
        path.moveTo(x + w * 0.16, y + h * 0.52)
        path.lineTo(x + w * 0.42, y + h * 0.80)
        path.lineTo(x + w * 0.86, y + h * 0.22)
    elif glyph == "cross":
        path.moveTo(x + w * 0.24, y + h * 0.24)
        path.lineTo(x + w * 0.76, y + h * 0.76)
        path.moveTo(x + w * 0.76, y + h * 0.24)
        path.lineTo(x + w * 0.24, y + h * 0.76)
    elif glyph == "bang":
        path.addRect(QRectF(x + w * 0.44, y + h * 0.16, w * 0.12, h * 0.44))
        path.addRect(QRectF(x + w * 0.44, y + h * 0.68, w * 0.12, h * 0.14))
    elif glyph == "empty":  # ∅: circle with a slash
        path.addEllipse(QRectF(x + w * 0.22, y + h * 0.20, w * 0.56, h * 0.60))
        path.moveTo(x + w * 0.24, y + h * 0.78)
        path.lineTo(x + w * 0.76, y + h * 0.22)
    elif glyph == "play":  # ▶
        path.moveTo(x + w * 0.30, y + h * 0.20)
        path.lineTo(x + w * 0.78, y + h * 0.50)
        path.lineTo(x + w * 0.30, y + h * 0.80)
        path.closeSubpath()
    else:  # dash (NOTRUN, SKIPPED, READY)
        path.moveTo(x + w * 0.30, y + h * 0.50)
        path.lineTo(x + w * 0.70, y + h * 0.50)
    return path


#: A badge's word is painted at this size when the badge is wide enough for it, and
#: squeezed down to the minimum below before it is dropped at all. Ticket 39 review:
#: a badge sized by eye (72 px) is too narrow for "NOREPORT", so the word disappeared
#: and the badge carried only its glyph; the width a word needs is now measured from
#: the font's own metrics (`badge_width_for`) and the callers allocate it.
BADGE_WORD_PX = 12
BADGE_WORD_MIN_PX = 8
#: The badge's own padding: 4 px before the glyph, 6 px between glyph and word, 4 px
#: after the word. `badge_width_for` and `paint_result_badge` must agree on these.
BADGE_PAD_LEFT, BADGE_GLYPH_GAP, BADGE_PAD_RIGHT = 4, 6, 4


def badge_side(height: int) -> int:
    """The square the glyph is painted in inside a badge of ``height`` px."""
    return max(12, min(18, height - 8))


def badge_width_for(word: str, height: int = 18, word_px: int = BADGE_WORD_PX) -> int:
    """How wide a badge must be to show ``word`` beside its glyph at ``word_px``.

    Measured with the badge's own font, so a Result column or a step row sized from
    this never clips the word: "PASS" needs 60 px, "NOREPORT" 103 px, and
    "FORCED_NOTRUN" is the widest of the Result words."""
    if not word:
        return badge_side(height) + BADGE_PAD_LEFT + BADGE_PAD_RIGHT
    advance = QFontMetrics(ui_font(word_px, bold=True)).horizontalAdvance(word)
    return BADGE_PAD_LEFT + badge_side(height) + BADGE_GLYPH_GAP + advance + BADGE_PAD_RIGHT


def badge_word_px(word: str, width: float, word_px: int = BADGE_WORD_PX) -> int:
    """The largest bold size at which ``word`` fits in ``width``, or 0 if it fits at none.

    A badge narrower than the word needs is the only case where the word is dropped,
    and the layouts size their badges from `badge_width_for` so it does not happen."""
    for size in range(word_px, BADGE_WORD_MIN_PX - 1, -1):
        if QFontMetrics(ui_font(size, bold=True)).horizontalAdvance(word) <= width:
            return size
    return 0


#: The two inks text can be painted in: the dark body ink for a pale fill, white for a
#: dark one. `ink_on` picks between them so a delegate never paints one over the other.
INK = TOKENS["ink"]
WHITE = TOKENS["white"]


def _luminance(color: QColor) -> float:
    """The WCAG relative luminance of a colour, 0 for black and 1 for white."""
    def channel(value: int) -> float:
        value = value / 255.0
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(color.red()) + 0.7152 * channel(color.green()) + 0.0722 * channel(color.blue())


def contrast(a: str | QColor, b: str | QColor) -> float:
    """The WCAG contrast ratio between two colours, 1.0 when they are identical.

    The number the design review measured its colour pairs against (33-opus.md section 3);
    4.5 is the smallest ratio body text can be read at, 7.0 is the comfortable one."""
    la, lb = _luminance(QColor(a)), _luminance(QColor(b))
    lighter, darker = (la, lb) if la >= lb else (lb, la)
    return (lighter + 0.05) / (darker + 0.05)


def ink_on(fill: str | QColor) -> str:
    """The ink to paint text on ``fill``: white where the fill is dark, body ink where it is pale.

    Ticket 39 review: a delegate that picks its foreground from the *state* rather than from the
    pixels it is actually painting produces text nobody can read — a selected current row keeps
    the pale primary tint while the selection rule puts white ink on it (1.29:1). The rule here
    is the one the review accepted: the ink that reaches at least 4.5:1 on the fill."""
    if contrast(WHITE, fill) >= contrast(INK, fill):
        return WHITE
    return INK


def paint_result_badge(painter: QPainter, rect, kind: str, word: str = "", word_px: int = BADGE_WORD_PX) -> None:
    """Paint one result badge in ``rect``: rounded fill, glyph, and the word when
    asked. ``kind`` is a key of RESULT_STYLES (or VERDICT/CHIP styles).

    Ticket 39 review, two rules the first pass broke. The glyph gets its own pen
    and brush: the badge background is drawn with NoPen, and a pen taken from the
    painter after that strokes nothing, while the still-set fill brush turns an
    open path — a tick, a cross, a dash — into a filled blob. And the word is painted
    at the largest size that fits rather than dropped as soon as the natural size
    does not: a "NOREPORT" in a 72 px badge read as a bare glyph, and a row whose
    result is only a colour and a shape is unreadable to anyone who cannot see the
    tint. The word is only lost when the badge is narrower than even the smallest
    size can draw it."""
    fill, ink, glyph = CHIP_STYLES.get(kind) or RESULT_STYLES.get(kind) or RESULT_STYLES["READY"]
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    shape = QPainterPath()
    shape.addRoundedRect(QRectF(rect), 4, 4)
    if glyph == "dash" and fill == TOKENS["surface"]:  # NOTRUN is an outline, not a fill
        painter.setPen(QPen(QColor(TOKENS["field_border"])))
        painter.setBrush(QColor(fill))
    else:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(fill))
    painter.drawPath(shape)
    side = badge_side(rect.height())
    box = QRectF(rect.x() + BADGE_PAD_LEFT, rect.y() + (rect.height() - side) / 2, side, side)
    if glyph == "question":  # "?" is plain ASCII: safe in any font
        painter.setPen(QPen(QColor(ink)))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setFont(ui_font(word_px, bold=True))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, "?")
    else:
        pen = QPen(QColor(ink))
        pen.setWidth(2)
        pen.setStyle(Qt.PenStyle.SolidLine)
        painter.setPen(pen)
        # Only the closed play triangle is filled; every other glyph is an open
        # path that must be stroked, not filled.
        painter.setBrush(QColor(ink) if glyph == "play" else Qt.BrushStyle.NoBrush)
        painter.drawPath(_glyph_path(glyph, box))
    if word:
        text_box = QRectF(
            box.right() + BADGE_GLYPH_GAP,
            rect.y(),
            max(0, rect.right() - box.right() - BADGE_PAD_RIGHT),
            rect.height(),
        )
        size = badge_word_px(word, text_box.width(), word_px)
        if size:
            painter.setFont(ui_font(size, bold=True))
            painter.drawText(text_box, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, word)
        # No size fits: the badge carries the glyph only.
    painter.restore()


#: The one application stylesheet. Rules are scoped to object names or to the
#: widgets that need them, so untouched widgets keep Fusion's own drawing. The
#: status-bar chips bug (a selector-less stylesheet painting every child label)
#: is fixed here by naming the bar and clearing its children.
STYLESHEET = f"""
QWidget {{ color: {TOKENS['ink']}; }}
QMainWindow {{ background: {TOKENS['canvas']}; }}
QLineEdit, QPlainTextEdit {{
  background: {TOKENS['surface']};
  border: 1px solid {TOKENS['field_border']};
  border-radius: 4px;
  padding: 4px 6px;
}}
QGroupBox {{
  border: 1px solid {TOKENS['line_strong']};
  border-radius: 4px;
  margin-top: 10px;
  padding-top: 8px;
}}
QPushButton {{
  border: 1px solid {TOKENS['field_border']};
  border-radius: 6px;
  background: {TOKENS['surface']};
  color: {TOKENS['ink']};
}}
/* Ticket 39 review: the 32 px minimum and the 6x14 padding belong to the Run controls, not
   to every button in the application. Applied globally they made each of History's buttons
   46 px tall, and because the Run screens and the History screen share one QStackedWidget,
   the window's minimum height rose to 887 px — taller than the 1366x768 netbook most
   Stations are on. Only the buttons carrying the Run actions get the bench-sized target;
   the rest keep Fusion's own compact size. */
QPushButton#primary, QPushButton#danger {{
  min-height: 32px;
  padding: 6px 14px;
}}
QPushButton#primary {{ background: {TOKENS['primary']}; color: {TOKENS['white']}; border-color: {TOKENS['primary']}; font-weight: 600; }}
/* Packet 41: a Start button that cannot be clicked reads as faded, not broken: the primary's own
   tint at the disabled ink, so it still looks like the run control, just not live. */
QPushButton#primary:disabled {{ background: {TOKENS['primary_tint']}; color: {TOKENS['ink_disabled']}; border-color: {TOKENS['line_strong']}; }}
QPushButton#danger {{ border: 2px solid {TOKENS['danger_ink']}; color: {TOKENS['danger_ink']}; background: {TOKENS['surface']}; }}
/* Packet 41 fix2 review: the object-name rule outranks the plain `QPushButton:disabled` one below,
   so a disabled Abort kept its red outline and looked clickable. It fades like every other button
   that cannot be pressed. */
QPushButton#danger:disabled {{ background: {TOKENS['subtle']}; color: {TOKENS['ink_disabled']}; border-color: {TOKENS['line_strong']}; }}
QPushButton:disabled {{ background: {TOKENS['subtle']}; color: {TOKENS['ink_disabled']}; border-color: {TOKENS['line_strong']}; }}
QListWidget, QTableWidget {{ background: {TOKENS['surface']}; border: 1px solid {TOKENS['line_strong']}; }}
QTableWidget {{ gridline-color: {TOKENS['line']}; }}
QTableWidget::item {{ padding: 4px 6px; }}
QHeaderView::section {{
  background: {TOKENS['subtle']};
  color: {TOKENS['ink']};
  font-size: 12px;
  font-weight: 600;
  border: none;
  border-bottom: 1px solid {TOKENS['line']};
}}
#run_header {{ background: {TOKENS['head']}; padding: 8px 16px; }}
#run_header QLabel {{ color: {TOKENS['head_ink']}; font-size: 12px; background: transparent; border: none; }}
#run_title {{ color: {TOKENS['white']}; font-size: 15px; font-weight: 600; }}
#run_timer {{ font-size: 12px; }}
#debug_button {{ background: {TOKENS['chrome']}; color: {TOKENS['white']}; border: 1px solid {TOKENS['line_strong']}; min-height: 28px; }}
#status_bar {{ background: {TOKENS['subtle']}; border-top: 1px solid {TOKENS['line_strong']}; padding: 3px 10px; }}
#status_bar QLabel {{ background: transparent; border: none; padding: 0; color: {TOKENS['ink']}; font-size: 12px; }}
#log_text, #debug_text {{ font-size: 12px; }}
#log_title {{ color: {TOKENS['ink']}; font-size: 12px; font-weight: 600; }}
#log_note {{ color: {TOKENS['ink_2']}; font-size: 11px; }}
#run_error, #schedule_error, #input_error {{ color: {TOKENS['broken_err']}; font-size: 12px; }}
#schedule_mismatch {{ color: {TOKENS['amber_ink']}; font-size: 12px; }}
#input_value, #muted {{ color: {TOKENS['ink_2']}; font-size: 12px; }}
"""

_APPLIED: "WeakSet[QApplication]" = WeakSet()


def apply_theme(app: QApplication) -> None:
    """Fusion, the fixed light palette, the platform font and one stylesheet —
    called once per QApplication (MainWindow's constructor and ``launch`` both
    call it; the guard keeps it idempotent).

    Ticket 39 review: the guard holds weak references to the applications it has
    styled, not their ``id()``. CPython reuses an ``id`` when an object is gone,
    so a later QApplication that happened to land on a freed one's address would
    have been told it was already styled.
    """
    if app is None or app in _APPLIED:
        return
    _APPLIED.add(app)
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(TOKENS["canvas"]))
    palette.setColor(QPalette.WindowText, QColor(TOKENS["ink"]))
    palette.setColor(QPalette.Base, QColor(TOKENS["surface"]))
    palette.setColor(QPalette.AlternateBase, QColor(TOKENS["subtle"]))
    palette.setColor(QPalette.Text, QColor(TOKENS["ink"]))
    palette.setColor(QPalette.Button, QColor(TOKENS["subtle"]))
    palette.setColor(QPalette.ButtonText, QColor(TOKENS["ink"]))
    palette.setColor(QPalette.Highlight, QColor(TOKENS["primary"]))
    palette.setColor(QPalette.HighlightedText, QColor(TOKENS["white"]))
    app.setPalette(palette)
    app.setFont(ui_font(13))
    app.setStyleSheet(STYLESHEET)
