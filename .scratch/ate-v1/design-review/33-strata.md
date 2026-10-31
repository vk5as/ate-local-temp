# Packet 33 — appearance review of the Run screen (reviewer: strata)

Reviewed: the staged Run screen (`src/ate/ui/staged_run_screen.py`), the three-pane Run screen
(`src/ate/ui/three_pane_run_screen.py`), the shared widgets (`src/ate/ui/run_widgets.py`),
History (`src/ate/ui/history_screen.py`), the log export dialog, the mismatch confirmation, the
menu bar and the status bar — against the Run-screen mockup
(`prototype/run-screen:prototypes/run-screen-prototype.html`).

## How this was rendered, and what that means for the findings

Everything below was **looked at as pixels**, not inferred from code, with two exceptions stated
where they matter.

- **The application** was launched for real (`ate.app.launch`, PyQt6 6.11, fixtures from
  `tests/_fixtures`) and each state was captured with `QWidget.grab()` under
  `QT_QPA_PLATFORM=offscreen`. `xvfb-run` is unusable in this sandbox: Qt's xcb plugin dies with
  `libxcb-cursor.so.0: cannot open shared object file`, and there is no root to install it.
  Offscreen grabs do render real Fusion widget drawing (buttons, gradients, selection tints), so
  the colour and layout claims are measurements, not guesses.
- **The mockup** could not be rendered in Qt: QtWebEngine needs GL and this box has none
  (`QRhiGles2: Failed to create context`, a blank 1280×800 grab). So the mockup states were
  rendered in Firefox ESR 153 under `Xvfb :99` (one fresh profile per state), and the mockup
  frame cropped to 1132×787. Mockup-side numbers are read off those captures plus the CSS in the
  prototype; the mockup has never been seen through Qt.
- **Windows**: nothing here was rendered on Windows. The style/palette numbers are Linux
  measurements; the Windows differences are read from code only and are marked as such.

Shots are in `33-strata-shots/`, each downscaled and under 300 KB. App shots are prefixed
`app-`, mockup shots `mockup-`.

| shot | state |
|---|---|
| `app-01-staged-stage1.png` | staged layout, stage 1, 1280×800 |
| `app-02-staged-stage2-schedule.png` | staged layout, stage 2, 1280×800 |
| `app-03-staged-stage3-steps.png` | staged layout, stage 3, 1280×800 |
| `app-04-staged-running-blocking-input.png` | staged, run in progress, blocking INTEGER input |
| `app-05-staged-poll-running.png` | staged, POLL live alongside a blocking PROMPT |
| `app-06-staged-stop-on-fail.png` | staged, Stop-on-Fail pause |
| `app-07-staged-finished-mixed.png` | staged, finished run with PASS/FAIL/NOTRUN/ERROR |
| `app-08-staged-finished-1920x1080.png` | staged, finished run, 1920×1080 |
| `app-09-staged-minimum-587x725.png` | staged layout at the smallest window it allows |
| `app-10-staged-minimum-finished-636x705.png` | staged, finished 60-step run at minimum size |
| `app-11-three-pane-launch.png` | three-pane, no run, 1280×800 |
| `app-12-three-pane-poll.png` | three-pane, POLL live mid-run |
| `app-13-three-pane-stop-on-fail.png` | three-pane, Stop-on-Fail pause |
| `app-14-three-pane-finished.png` | three-pane, finished run |
| `app-15-three-pane-minimum-640x705.png` | three-pane at its minimum window |
| `app-16-mismatch-dialog.png` | the run-start mismatch confirmation |
| `app-17-history.png` | History, 1920×1080, empty store |
| `app-18-export-dialog.png` | log export dialog (129 px wide) |
| `app-19-status-bar.png` | status bar crop, 1248×39 |
| `app-20-menu-bar.png` | menu bar crop, 1280×19 |
| `mockup-01-c-stage1-schedule-table.png` | mockup variant C, stage 1 |
| `mockup-02-c-stage3-steps.png` | mockup variant C, stage 3 |
| `mockup-03-c-running-blocking-input.png` | mockup variant C, blocking input mid-run |
| `mockup-04-c-stop-on-fail.png` | mockup variant C, Stop-on-Fail prompt |
| `mockup-05-c-finished.png` | mockup variant C, finished run |
| `mockup-06-c-mismatch-modal.png` | mockup variant C, mismatch modal |
| `mockup-07-b-launch.png` | mockup variant B (three-pane), launch state |
| `mockup-08-b-finished.png` | mockup variant B (three-pane), finished run |

---

## 1. Verdict

The Run screen is **behaviourally complete and visually unstyled**. Both layouts paint correctly
and nothing is broken, but every pixel is Qt's default Fusion look plus a handful of hardcoded
hex values copied one at a time from the mockup's CSS. There is no theme module, no type scale,
no icon set and no palette: 21 separate `setStyleSheet` calls across three files, and `ui.theme`
is parsed in `config.py` but never read by a single line of UI code.

The gap that matters most is **result state**. In the mockup a finished run is legible at a glance
because PASS/FAIL/ERROR/NOTRUN are coloured pills; in the app those same words are plain black text
in a column, and the only signal left is the shape of the word. (`app-07-staged-finished-mixed.png`
vs `mockup-05-c-finished.png`: identical data, one is a glance, the other is a read.)

The second gap is **space discipline**. In the staged run view at 1280×800 the step list gets 214 px
and the three stacked log bands get 356 px — the log is nearly twice the space of the work. The
mockup gives the step list about 64% of the frame and the log band about 19%. The setup stages are
worse: stage 1 fills 100 px of an 800 px window and leaves 700 px of grey (`app-01`).

None of this is hard to fix. The five changes that move the needle are: a shared theme module,
result colouring, a monospace log, a legible Abort button on the red bar, and a run header that
says which run is on screen.

---

## 2. Gaps, screen by screen

### 2.1 Staged layout — stage 1, UUT and Operator

`app-01-staged-stage1.png` (1280×800, mockup: `mockup-01-c-stage1-schedule-table.png`)

- Four full-width line edits and one full-width **Next** button, then ~700 px of empty grey.
  Nothing tells the Operator which stage they are in, what is still required, or what the run will
  do. The mockup's stage strip (`1 Test Schedule | 2 UUT and Operator | 3 Steps and options`,
  active stage underlined in blue, completed stages green) has no counterpart in the app.
- No heading, no station name, no context: the window title (`ATE <version> — <station>`) is the
  only place the station appears, and it is invisible in the grabs.
- Empty required fields read as the grey placeholder "required" — the same grey as disabled
  controls (see §2.4 on disabled states).

### 2.2 Staged layout — stage 2, Test Schedule picker

`app-02-staged-stage2-schedule.png`, mockup: `mockup-01-c-stage1-schedule-table.png`

- The picker is a plain `QListWidget`: `name — description` on one line, no columns. **Media
  Release is tooltip-only** in this layout (`run_widgets.py:764`, `item.setToolTip(row.media_number)`),
  so the release the Operator is about to run is invisible until they hover. The mockup shows
  Name / Media Release / Status as three columns.
- The selected row is Fusion's selection blue (sampled `#308cc6`); the mockup tints the picked row
  pale blue (`#dbe8ff`) and keeps the text black. Both are readable (5.69 vs 5.97 contrast), but in
  the app selection and the broken-row tint are the only two visual states in the list, and a
  schedule with a checksum mismatch gets no tint at all in this layout — the amber `⚠` flag appears
  only in the three-pane picker (`inline_mismatches=True`).
- The broken row is a pink band with `× name`, release, and the error in `#b71c1c` (6.14 contrast —
  that part is fine).
- Below the list: ~250 px of nothing, then a full-width **Run this schedule** button. The mockup
  puts navigation in a bottom bar with Back / Next and a primary button.

### 2.3 Staged layout — stage 3, steps and options

`app-03-staged-stage3-steps.png`

- Steps and their enable checkboxes, then ~600 px of empty white, then the On-failure radios, then
  a full-width **Start run**. Two things the mockup has and the app does not: the schedule name and
  release pinned at the top of the step list, and the mismatch flag shown before the run starts.
- Sub-schedule headers are tinted `#eef1f5` (18.5 contrast), which is the only hierarchy cue in the
  list; depth is otherwise carried by indentation only.

### 2.4 Run in progress — blocking input, POLL

`app-04-staged-running-blocking-input.png`, `app-05-staged-poll-running.png`,
`app-12-three-pane-poll.png`

- The active Step Input row is genuinely good: `#fff6cc` with a 3 px `#e0a800` left rule
  (19.3 contrast). This is the one place the app matches the mockup's clarity.
- Everything else on the screen is white-on-grey and flat: the header reads `STARTED - 00:00`, and
  the two live POLL/PROMPT rows sit in an unlabelled sea of white with no step numbering context.
- A POLL's answer shows as `clicked: (none)` in muted `#777` (3.89 on the window grey) — the one
  piece of state the Operator needs while polling is the faintest thing on screen.
- Log panes are empty for most of the run and the log lines are drawn in the **proportional** font,
  so the `LEVEL   SOURCE   text` padding does not line up. The mockup uses Consolas 12 px, and the
  alignment of levels and sources is what makes a log scannable.
- `Run 00:00` / `Current 00:00` in every shot: the fixture run completes in under a second, so the
  timers' live behaviour could not be judged from these captures. The mockup's timers are frozen at
  render time, so neither side proves anything here.

### 2.5 Stop-on-Fail prompt

`app-06-staged-stop-on-fail.png`, mockup: `mockup-04-c-stop-on-fail.png`

- **The Abort button is invisible.** The bar is `#c62828`; the Abort button's fill is also `#c62828`
  (`run_widgets.py:893`). Same colour on same colour: contrast 1.00, and only the `#8e1c1c` border
  (1.60) separates them. In the shot the button is a slightly darker red rectangle — the Operator
  cannot see it is a button until they hover.
- The pause paints **two** red bands stacked: the header goes red and reads `WAITING - 00:00`, and
  the 52 px prompt bar is red again 250 px below it. When the run ends, a third red band
  (`#b71c1c`, `setFixedHeight(24)`) appears in the same place the prompt bar just vacated. The
  mockup keeps these distinct by weight — a `FAIL` pill in the header, an inline bordered box under
  the failed step — and never stacks two fills.
- The step list's own highlight for the failed step (`#fff0f0`) is a pale tint that is invisible at
  the top of the list because the prompt bar is a separate widget above it, not attached to the
  row (the three-pane layout attaches it properly; the staged one does not).

### 2.6 Finished run

`app-07-staged-finished-mixed.png`, `app-08-staged-finished-1920x1080.png`,
`app-14-three-pane-finished.png`, mockups: `mockup-05-c-finished.png`, `mockup-08-b-finished.png`

- **No result colour anywhere.** `1. Visual inspection → PASS` is black text. In the mockup PASS is
  a green pill, FAIL red, ERROR white-on-dark-red, NOTRUN grey. This is the single biggest legibility
  difference between the two screens.
- The header is a 22 px QLabel reading `FINISHED - 00:00` plus a greyed `Abort` and a `New run`
  button. No schedule, release, UUT serial, project, operator, stop-on-fail mode, or end reason —
  all of which the mockup puts in the run band, and all of which the app already has in the store.
- Disabled `Abort` and enabled `New run` are pixel-identical shapes: sampled at the same y in
  `three-01-launch-selected.png`, `Start`, `Abort` and `New run` all read `#efefef`. Fusion's
  enabled/disabled difference is about 1.13:1 — not a state the eye can pick up.
- The three-pane tally prints internal enum names: `FORCED_NOTRUN`, `NOREPORT`. A Station Operator
  reading a results panel sees a database key.
- At 1920×1080 the staged view wastes the extra height the same way it does at 1280×800: the log
  bands grow, the step list does not.

### 2.7 Mismatch confirmation

`app-16-mismatch-dialog.png`, mockup: `mockup-06-c-mismatch-modal.png`

- Structurally sound: bold schedule name, muted release, the run details line, the amber `⚠` flag
  box (8.39 contrast), the store checkbox, Cancel/Start. This is the best-looking thing in the app.
- The flag box has no padding beyond the QSS `padding: 4px 10px` and no rounding, so it reads as a
  stretched band rather than a notice. The `⚠` glyph is DejaVu's; fine on Linux, but it is a glyph
  the app relies on with no fallback.

### 2.8 History

`app-17-history.png` (1920×1080)

- Filters are **below** the table; the mockup and every operator habit put them above. With no runs
  in the store, the table is a 258 px white void and the filters are the only visible content.
- "Runs 1 - 0 of 0" is a nonsense string for an empty result set.
- The detail rows (`Run`, `Result counts`, `Checksum mismatches`) are plain text lines with no
  hierarchy or colour, and they are the same size as the table headers.

### 2.9 Export dialog

`app-18-export-dialog.png` — the whole dialog is **129 px wide** (measured: sizeHint and minimumSize
both 129×348). Level / Source / Owner stack vertically in group boxes, and the Export button is
disabled grey. It is the narrowest window in the application and it has no minimum width set.

### 2.10 Menu bar and status bar

`app-20-menu-bar.png`, `app-19-status-bar.png`

- The menu bar is 19 px, one menu, `View ▸ Run / History / Layout ▸ Staged / Three-pane`. The layout
  switch is a checkable exclusive group, which is correct, but there is no indication of which
  layout is active anywhere else on the screen. `app-20-menu-bar.png` shows the top-level item only:
  offscreen grabs cannot capture a popup menu, so the submenu's rendering is read from
  `app.py:122-134`, not seen.
- The status bar is 39 px tall, `#e4e4e4` with a `#c4c4c4` top rule: `Finished` on the left, tallies
  and timers on the right. It is the only consistently styled element in the app, and it is the
  same style in both layouts — the good precedent to build the rest of the theme on.

### 2.11 The smallest window the app allows

- Both layouts: **587×725** at launch (`sizeHint` 850×911).
- After a 60-step run: staged **636×705**, three-pane **640×705**.
- `app-09-staged-minimum-587x725.png`, `app-10-staged-minimum-finished-636x705.png`,
  `app-15-three-pane-minimum-640x705.png`.
- At the three-pane minimum the centre table is **207 px wide**: only Enable and Step are visible,
  a horizontal scrollbar appears, and the On-failure radio label is cut mid-word
  ("Run every step regardless of f"). The left pane's 240 px minimum is doing this — it is a floor
  with no counterweight, so the table pays for it.

### 2.12 Windows, from code only

Nothing pins a style or palette. On this Linux box Qt resolves to `fusion`; on Windows PyQt6 picks
the native Windows style, so every default in §3 (button faces, selection tint, border widths) is a
Linux measurement that will not hold there. The mockup's CSS values are the only cross-platform
constants the app has, and they are applied per-widget rather than as a style sheet, so the same
widget renders differently on the two platforms wherever the app does not override it.

---

## 3. The design choices the code already makes

**Palette.** Two unrelated palettes are stacked on top of each other. Fusion's defaults — window
`#efefef`, text `#000000`, buttons `#fdfdfd → #efefef`, selection `#308cc6` (sampled) — and a set of
hardcoded hex literals copied from the mockup: `#eef1f5` header tint, `#fff5f5`/`#fff0f0`/`#f0f0f0`
tints, `#b71c1c`/`#c62828`/`#8e1c1c` reds, `#fff3cd`/`#fff8e6`/`#fff6cc` ambers, `#e0a800`/`#e0b100`
rules, `#777` muted, `#0a64d6` primary, `#e4e4e4`/`#c4c4c4` status bar. No name, no single source,
no dark variant. `ui.theme` exists in config (`"system"`) and is read into `Config.ui_theme` —
then never used.

**Type.** One size: Qt's default 9 pt (DejaVu Sans on this box, reported by Qt as "Sans Serif").
`font-size: 12px` in `_MUTED_STYLE`, `_INPUT_ERROR_STYLE` and the error labels is *exactly* 9 pt at
96 DPI, so those rules change nothing — they are not a small step, they are a no-op that reads as
one. The only real step up is 15 px in the prompt bar (11.25 pt). Bold comes from `QFont.setBold()`.
There is no heading scale, no monospace anywhere.

**Spacing.** `setContentsMargins(8, 8, 8, 8)` on the run view and panes, Qt's default layout spacing
elsewhere, 16/10 margins and 12 spacing inside the prompt bar, 6/4 row margins in picker rows, a
24 px fixed failure bar. Vertical bands are divided by stretch 3/2/2/1 with floors 150/60/60/40.
Consistent, but arbitrary — nothing relates spacing to the 9 pt type size.

**Radii and borders.** The only explicit radius in the whole UI is `border-radius: 3px` on the
prompt bar buttons. Everything else is Fusion's own drawing. The amber flag box and the red bars
have no radius at all.

**Buttons.** Default Fusion for every button except the three in the prompt bar. No primary/danger
distinction anywhere else, and no state difference between enabled and disabled (§2.6).

**Tables and lists.** `QTableWidget` with default grid lines and the default header; row tints set
per item with `QColor`. `QListWidget` for the step list, header rows tinted, sub-steps indented by
text only. `QTableView` in History with no styling.

**Icons.** None. There is no `QIcon` anywhere in `src/ate`. The only symbol vocabulary is the two
glyphs `×` (U+00D7, deliberately chosen because `⛔` is not in DejaVu Sans) and `⚠`.

**Light/dark.** There is no dark mode and no palette manipulation at all: the app inherits whatever
the platform gives it. On Linux that is light Fusion; on Windows it is whatever the user's theme is,
with no attempt to make the app's hardcoded tints agree with it.

---

## 4. Recommendations, ranked

Ranked by impact per unit of work. All are PySide6/Qt 6.11 on Windows and Linux, and none need a
runtime dependency.

### 1. Put Test Results on screen in colour

- **Problem:** PASS/FAIL/ERROR/NOTRUN/SKIPPED/NOREPORT are plain text (§2.6). The finished run view
  is a list of words; the mockup's is a table of states.
- **Change:** one dict of result → (background, foreground), applied to the step-list item, the
  three-pane Result column, and the Results tally. Suggested values, chosen against Fusion's
  `#efefef`/white surfaces: PASS `#14532d` on `#e7f3ec` (8.00), FAIL `#8a1c1c` on `#fdeaea` (8.01),
  ERROR white on `#7b1a1a` (10.47), NOTRUN `#5a6068` on `#eef1f4` (5.60), RUNNING `#0f4a80` on
  `#e3f0fb` (7.84). The mockup's own pills are 5.58–5.95, which is only AA at 11–12 px; the
  darker set above is AA at any size.
- **Impact:** high — this is the change that makes the Run screen readable without reading.
- **Effort:** M.
- **Qt:** `QListWidgetItem.setBackground()` / `.setForeground()` and `QTableWidgetItem`
  equivalents already exist in the code paths (`_render_rows`, `_set_cell`); the values come from
  one module rather than three literals. No delegate, no new widget.
- **Limitation:** item backgrounds do not survive a repaint by the style's selection highlight —
  the row under the mouse cursor will read as Fusion blue. Keep the pill in its own column, or
  style the selection, so colour and selection never fight.

### 2. One theme module, applied once

- **Problem:** 21 `setStyleSheet` calls across three files, no shared source, `ui.theme` parsed and
  unused (§3). On Windows the app silently reverts to native defaults.
- **Change:** `src/ate/ui/theme.py` holding the palette, the type scale and one QSS string; the
  window applies it once. Kill the per-widget hex literals.
- **Impact:** high — this is the precondition for 1, 3, 4 and 10 being maintainable, and the only
  way the two layouts and both platforms agree.
- **Effort:** M.
- **Qt:** a single `window.setStyleSheet(...)` with rules scoped to `objectName`s (the app already
  sets them: `step_input`, `schedule_error`, `count_PASS`). Prefer scoped rules over a global
  `QPushButton { ... }`.
- **Limitation:** a whole-window stylesheet makes Qt stop using the platform style for the
  elements it covers. On Windows that means the app stops looking native — acceptable for the Run
  screen, but scope the rules so untouched widgets keep the native look. A `QPalette` variant for
  dark mode is possible with no dependency, but Qt's Fusion does not follow system dark mode; that
  has to be driven from `ui.theme`.

### 3. Make the Abort button on the red bar actually visible

- **Problem:** `#c62828` button on a `#c62828` bar (§2.5). Contrast 1.00. The most consequential
  button in a failure is the one the eye cannot find.
- **Change:** white/light button with red text on the red bar, or a clearly different fill; the
  mockup's `danger` class is the source of the bug and should not be copied.
- **Impact:** high, tiny cost.
- **Effort:** S.
- **Qt:** the prompt bar already has a per-button QSS block; change three values.

### 4. Monospace the log panes

- **Problem:** log lines are drawn in the proportional UI font, so the `LEVEL  SOURCE` columns do
  not align and the log reads as ragged text (§2.4).
- **Change:** `QFont("DejaVu Sans Mono")` (with a fallback chain) for `LogPane` and the tab panes.
- **Impact:** high for anyone debugging a run.
- **Effort:** S.
- **Qt:** `QListWidget` items inherit the widget font; set it once in `LogPane.__init__`. Windows
  has Consolas; Linux has DejaVu Sans Mono — both ship with the OS, so no bundling.

### 5. Give the run header the run's identity

- **Problem:** `FINISHED - 00:00` with nothing else (§2.6), while the store already knows the
  schedule, release, UUT, project, operator, stop-on-fail mode and end reason.
- **Change:** a 2-line header band: line 1 `schedule — release`, line 2 `UUT type s/n — project —
  operator — stop on fail — end reason`, with the state as a coloured pill at the left.
- **Impact:** high — it is the difference between "which run am I looking at" and not asking.
- **Effort:** M.
- **Qt:** a `QLabel` with rich text, or a small QWidget with three labels; both layouts share the
  same `RunView` so one change covers both.

### 6. Balance the run view's bands

- **Problem:** at 1280×800 the step list gets 214 px and the log bands 356 px (§2.1); at 1920×1080
  the log grows and the list does not.
- **Change:** raise the step list's share (stretch 4 instead of 3, or fold the Debug pane into a
  toggle) and make the Debug window a toggle button in the header rather than a permanent band.
- **Impact:** high.
- **Effort:** M.
- **Qt:** stretch factors already exist (`_STEPS_STRETCH` etc.); the toggle is a `QPushButton`
  showing/hiding the pane. No Qt limitation.

### 7. Show Media Release in the staged picker

- **Problem:** tooltip-only (§2.2).
- **Change:** render the staged picker rows as widgets the way the three-pane picker already does
  (name / release / status), or use two list columns.
- **Impact:** medium-high — the release is the thing the Operator is confirming.
- **Effort:** S–M.
- **Qt:** `setItemWidget` is already used for broken rows; the helper exists (`_schedule_row_widget`).

### 8. Make disabled and enabled controls look different

- **Problem:** sampled button faces identical (§2.6); the mockup uses `background: #f0f0f0; color:
  #9a9a9a` for disabled, which is itself only 2.47 contrast.
- **Change:** disabled → grey text plus a dimmer face and no hover; enabled primary → blue fill.
- **Impact:** medium — it removes "is this clickable?" from every screen.
- **Effort:** S.
- **Qt:** `QPushButton:disabled` in the shared stylesheet. **Limitation:** on Windows the native
  style already dims disabled buttons, so scope the rule so it does not double-dim.

### 9. A stage indicator in the staged layout

- **Problem:** stage 1/2/3 are invisible (§2.1).
- **Change:** a 26 px strip above the stage content: `1 UUT and Operator · 2 Test Schedule · 3 Steps
  and options`, current bold, completed with a green tick. (Order per the asset spec: UUT/Operator
  first — the mockup's variant C puts Test Schedule first, which contradicts `run-screen.md`.)
- **Impact:** medium-high for a first-time Operator.
- **Effort:** S.
- **Qt:** three QLabels in a QHBoxLayout; the app already tracks the stage in `QStackedWidget`.

### 10. Define a type scale and stop using 12px as a "small"

- **Problem:** 9 pt everywhere; `font-size: 12px` is the same as 9 pt (§3).
- **Change:** base 10 pt, small 8 pt, log 9 pt mono, header 13 pt bold. Set them in one place.
- **Impact:** medium — hierarchy is currently carried only by bold.
- **Effort:** M (needs re-checking every fixed height: the 22 px header, 24 px failure bar, 52 px
  prompt bar are all sized for 9 pt).
- **Qt:** `QFont` on the widgets plus the theme stylesheet. **Limitation:** fixed row heights in
  `QTableWidget`/`QListWidget` have to be recomputed; the picker already has
  `_broken_row_size_hint` for exactly this problem.

### 11. Fix the three-pane minimum width

- **Problem:** at 640×705 the step table is 207 px wide with a horizontal scrollbar and truncated
  labels (§2.11).
- **Change:** lower the left pane's minimum (240 → 200), drop the Attempts/Actual/Expected columns
  below a width threshold with `hideColumn`, and let the Message column wrap.
- **Impact:** medium — this is the "smallest window the app allows" case, and it is currently a
  broken layout.
- **Effort:** M.
- **Qt:** `QTableWidget.hideColumn()`/`setColumnWidth()`; `QSplitter` sizes. No limitation.

### 12. Icons

- **Problem:** none anywhere (§3); `×` and `⚠` are the whole vocabulary.
- **Change:** small 16 px PNGs for PASS/FAIL/ERROR/NOTRUN/pause/mismatch, in
  `src/ate/ui/assets/`, used with `QIcon`.
- **Impact:** medium — colour plus glyph is faster to read than colour alone, and colour-blind
  operators need the second cue.
- **Effort:** L (asset work, plus a test that the icons load).
- **Qt:** `QIcon` is in QtGui, no dependency. **Limitation:** `QIcon.fromTheme` is unreliable on
  Windows (no icon theme) — ship the files rather than using a theme.

### 13. History: filters above, empty states spelled out

- **Problem:** filters below the table, "Runs 1 - 0 of 0" (§2.8).
- **Change:** filter row at the top; "No Test Runs recorded" when empty; the detail rows styled as
  a sub-table.
- **Impact:** medium. **Effort:** S–M. **Qt:** reordering a layout; a QLabel placeholder.

### 14. Fill the setup stages

- **Problem:** stage 1 and 3 leave 600–700 px empty (§2.1, §2.3).
- **Change:** stage 1 gets a two-column form (fields left, a "what will run" panel right); stage 3
  gets the schedule summary and the step count at the top.
- **Impact:** medium — the app looks unfinished on the screens an Operator sees first.
- **Effort:** M. **Qt:** nothing exotic; both screens already have the data.

### 15. Say it in operator words

- **Problem:** `FORCED_NOTRUN`, `NOREPORT`, `Pre-test 0/0` (§2.6).
- **Change:** display strings per result key: "Not run (Station forced)", "No report", "Pre-test
  actions 0 of 0". Keep the enum keys in the data.
- **Impact:** medium. **Effort:** S. **Qt:** trivial; the tally widget already has one label per key.

---

## 5. Mockup ideas not to copy

1. **The `danger` Abort button.** `background: #c62828` on a `#c62828` bar — invisible (1.00). The
   mockup's `button.danger` is a CSS habit, not a design decision; the app inherited it literally.
2. **Gradient buttons.** `linear-gradient(#fdfdfd, #e6e6e6)` — Fusion already draws a gradient;
   adding a QSS gradient overrides the platform style and looks wrong on Windows.
3. **Box shadows and translucent overlays.** `.frame { box-shadow: 0 6px 24px #0005 }`,
   `.modal-back { background: #0006 }`. Qt widgets do not cast shadows, and a modal overlay needs a
   full-window overlay widget. The app's real `QDialog` is the right answer; do not imitate the
   mockup's floating panels.
4. **The mockup's status-bar timers.** They are computed once at render and never tick (the
   prototype's `statusBar()` reads `Date.now()` only when it re-renders). The app's live timers are
   correct; do not "simplify" them to match the mockup.
5. **`⛔` as the broken-row marker.** Not in DejaVu Sans; Qt draws a missing-glyph box. The app
   already settled this (`BROKEN_MARKER = "×"`, with a test that compares the ink). Keep `×`.
6. **Variant C's stage order.** The mockup puts Test Schedule before UUT/Operator; the asset spec
   has UUT/Operator first. Follow the spec, not the mockup.
7. **The dark titlebar with Run/History tabs.** A fake chrome inside the window duplicates the OS
   titlebar and Qt's menu. Keep the 19 px Qt menu bar; put the station name in the window title
   where it already is.
8. **Colour values straight from the CSS.** The mockup's pills are 11 px text at 5.6–6.0 contrast —
   just under AA for small text. Use the darker set in §4.1.

---

## 6. Decisions for you

These are the forks I could not call on your behalf. My recommendation is the first in each.

1. **Where does the theme live?** A single `theme.py` + one window stylesheet (recommended) vs
   per-widget styles as today vs a `QPalette` on the QApplication. *Recommendation:* module +
   scoped QSS; palette only for light/dark.
2. **Is the Debug pane permanent or a toggle?** The mockup makes it a floating window; the app makes
   it a 78 px band. *Recommendation:* a toggle in the run header, default off — it buys back the
   band the step list needs.
3. **Dark mode: now or never?** `ui.theme` is parsed and unused. *Recommendation:* decide the scope
   now (light only, or light + a Fusion dark palette) and either implement it or remove the config
   key so it does not read as a promise.
4. **Do both layouts get the same treatment?** The staged layout is the default and the three-pane
   is the operator-density variant. *Recommendation:* fix the shared widgets first (colour, fonts,
   buttons) so both improve at once; then the layout-specific banding separately.
5. **Result colour vs result glyph first?** Colour is the fast win; the glyph is the accessible one.
   *Recommendation:* colour in this packet, glyph assets in the next.
6. **How small should the window be allowed to go?** The current 587×725 floor produces a 207 px
   table. *Recommendation:* raise the practical floor to ~900×700 and hide columns below it,
   rather than pretending a 587 px window is usable.
7. **Should the finished Run view offer "Open report"?** History has it; the mockup's finished band
   has it; the app's does not. *Recommendation:* add it — it is one button and the run just ended.
