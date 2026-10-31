# 33: Appearance review, Opus

Review only. Nothing under `src/` or `tests/` changed. Rendered 2026-10-31 at 1280x800, 1920x1080 and the smallest size, on PyQt6 and Qt 6.11.0 with the `offscreen` platform (`xcb` will not load here: `libxcb-cursor0` is missing). Screenshots are in `33-opus-shots/`: `mock-*` are the mockup in `QWebEngineView`, `app-*` the real `MainWindow` driven through real runs, `concept-*` a throwaway Qt build of the proposal.

**Limits of this evidence**
- **Windows was not run.** Windows statements come from the code and from what I know of Qt, and are marked as such.
- Both renders used DejaVu Sans, because Segoe UI is not installed here.
- The app's test data copies the mockup's Test Schedule and Media Releases. Differences in content, such as the sub-schedule `Channel checks` appearing in the picker and the description text after a schedule's name, come from my fixture.
- `concept-*` uses fake data and is hand-built widgets, not the application. It shows that Qt can look like this; it does not estimate the migration.
- Colour-blind figures use the Machado 2009 full-severity matrices, entered from memory. Treat them as indicative.

## 1. Verdict

The mockup looks better because of hierarchy, not browser effects. In the same font, it gives every state one obvious loudest element, gives results a consistent shape, and lets the step list dominate. ATE has the mockup's behaviour and some of its colours, but it has no visual structure: a stock Fusion window, 12 px text throughout, a run header reading `STARTED · 00:00`, and a step list with only 234 px of the window against the mockup's 458 px, because three log panes sit under it. A passed run shows no verdict at all. Almost all of the gap can close in Qt with one theme, one reworked Run view header, and step rows with result badges; `concept-run-waiting.png` is plain widgets and one stylesheet. What stays out of reach is unimportant here: Qt stylesheets have no box shadows, and I would not copy the mockup's backdrop anyway. The reachable gain is also a usability gain, because the same changes answer "what must the Operator do next?" from a distance.

## 2. Gaps, screen by screen

| Screen | Mockup | Application | Shots |
|---|---|---|---|
| **Frame** | Dark strip with Run, History, Station and Debug window | `View` menu only; Station only in the OS title bar; no app identity; no icon | `mock-c-stage1-schedule`, `app-view-menu`, `app-view-layout-menu` |
| **Stage rail** | `1 2 3` rail with current and done states; Back and a primary Next in a fixed footer | **No rail and no Back button** (no such control in the code), so the Operator cannot see or return to an earlier stage | `mock-c-stage2-uut`, `app-s-stage1-filled` |
| **Stage: UUT and Operator** | 320 px fields at 15 px, left-aligned | 1185 px fields at 12 px; full-width grey Next directly under them. Enter does nothing. Next with empty fields moves on with no message | `app-s-stage1-filled`, `app-s-stage1-next-empty-click` |
| **Stage: Test Schedule** | Table: name, Media Release, Status | Flat list. Good rows are 14 px tall, broken rows 76 px, so rows look unrelated. **Usable rows omit the Media Release**, which `run-screen.md` line 24 requires. The button reads "Run this schedule" but only opens the next stage | `mock-c-stage1-schedule`, `app-s-stage2-broken-mismatch`, `app-s-stage2-filter-empty` |
| **Stage: Steps and options** | Roomy rows, fieldset, primary "Start Test Run" | Rows touch, with no padding; radios are loose; grey full-width button; every future input shows as a grey block | `mock-c-stage3-steps-mismatch`, `app-s-stage3-steps-mismatch` |
| **Run, blocking input** | Dark header, state chip, UUT line, timers, red Abort; current step enlarged, pale blue with a bar | Header is `STARTED · 00:00` at 12 px with no Test Schedule, UUT or Operator; a blank 24 px band below it; **no visible enlargement of the current step** in any in-progress shot (`run-screen.md` line 42). Only the yellow input row marks the place | `mock-c-run-blocking-radio`, `app-s-run-blocking-radio` |
| **Run, `POLL`** | Same, with live buttons | Same weaknesses. "clicked: (none)" sits 650 px from its buttons at the far right | `mock-c-run-poll`, `app-s-run-poll` |
| **Log area** | One Log/Summary tab band, 160 px, plus a floating Debug window | **Three stacked panes** taking most of the window. The Log *tab* stays **empty during the run** and then holds `PASS  message` lines. An always-on Debug pane replaces the separate window in `run-screen.md` line 33. No time column. The Debug pane has **two nested vertical scrollbars** (a list inside a scroll area) | `mock-c-stop-on-fail`, `app-s-stop-on-fail`, `app-s-finished-passed` |
| **Step rows** | Badge at the right edge; message, Actual, Expected under the name | `3.2 Channel ripple → FAIL` as plain text. **Actual and Expected appear nowhere in the staged layout**, so the Operator deciding Retry cannot see 48 mV against < 20 mV without opening the report | `mock-c-stop-on-fail`, `app-s-stop-on-fail` |
| **Stop on Fail** | Red header, enlarged failed row, 15 px bar with detail | Closest match, but the header is a thin red strip reading `WAITING`, and the label and message stay 12 px because the bar's `font-size` does not reach its `QLabel`s | `mock-c-stop-on-fail`, `app-s-stop-on-fail`, `app-s-error-stop` |
| **Finished, passed** | Large green `PASS`, End Reason, Report, primary New run | `FINISHED · 00:03`. **No verdict anywhere** except inside the Summary tab. No End Reason. **No way to open the report** from the Run screen. Scroll position varies: near the bottom here, at the top on a 60-step run (`app-s-long-finished`) | `mock-c-finished-passed`, `app-s-finished-passed`, `app-s-finished-summary` |
| **Status bar** | One flat strip, full window width, bold figures | Same content, but each label is a separate grey chip, the bar is inset from the window edges, and it has no bold figures (see the bug below) | `mock-c-run-poll`, `app-s-run-poll` |
| **Finished, failed** | Large red `FAIL` | A thin red strip with the failed step name, which is usable. The failed row looks like any other row and may be off screen | `mock-c-fail-finished-failed`, `app-s-fail-finished-failed` |
| **ERROR** | Near-black badge, distinct from FAIL | Same red bar as FAIL; only the word differs. A stale "Enter a number" stays beside the accepted value 1.9 (a bug, see R16) | `app-s-error-stop`, `app-s-input-validation-error` |
| **Three-pane setup** | Tight picker, compact form, blue and red action buttons | Picker rows take twice the height, so it shows far fewer schedules; three grey buttons | `mock-b-setup-selected`, `app-t-setup-mismatch` |
| **Three-pane run** | Result badges; single wrapped log; counts as badges; inline prompt | Plain-text results; full grid; Step Names clipped ("Channel checks (c…"); **a stray blue selected cell reads like a status**; two log panes that clip with horizontal scrollbars; no result colours in the counts | `mock-b-stop-on-fail`, `app-t-stop-on-fail`, `app-t-error-stop` |
| **Three-pane finished** | PASS or FAIL banner with Report and New run | **No verdict at all.** The Operator must read the `FAIL 1` count; the failed row is not marked. Start and New run are both enabled | `mock-b-fail-finished-failed`, `app-t-fail-finished-failed` |
| **Mismatch confirmation** | Dark heading, flag box, compact right-aligned buttons, primary Start | Reasonable. Cancel and Start are the same wide button, and Start's default state is faint. The text omits the UUT Type | `mock-b-mismatch-confirm`, `app-s-mismatch-confirm`, `app-t-mismatch-confirm` |
| **History** (not in the mockup) | None | Vertical stack: seven filters and six full-width buttons fill the window, leaving the run table **about 130 px, or 3 rows**. Table uses 2/3 of its width; Start is clipped to `2026-10-31T…`; Result is plain text; the detail pane is raw `station_uuid=…, uut_serial=…` | `app-history-selected`, `app-history-8-runs` |
| **History, empty** (not in the mockup) | None | Blank table with no message; Open report, Regenerate, Show all runs and New run stay enabled with no run to act on | `app-history-empty` |
| **Export dialog** (not in the mockup) | None | 236x667 for a 9-step Test Schedule; **1867 px minimum height on a 60-step Test Schedule, taller than any common screen, so Export can be unreachable**. No Cancel; no select or clear all | `app-export-dialog`, `app-export-dialog-60-steps` |
| **1920x1080** | Same structure | Extra height goes to log panes; text stays 12 px; the fail bar's buttons are about 1400 px from its message | `mock-c-1920-run`, `mock-b-1920-run`, `app-s1920-stop-on-fail` |
| **Smallest size** | n/a | Minimum 587x725 and the window then **grows by itself** to 641 or 625 wide during a run. **Three-pane at this size hides Continue, Retry and Abort** behind the table's horizontal scroll while the banner says "choose below". By my arithmetic 725 px plus a title bar and taskbar does not fit a 1366x768 screen | `app-smin-stop-on-fail`, `app-tmin-stop-on-fail` |

### Things the application already does well

Keep these: the yellow active-input row with its focus moved into the input, tooltips, the fail bar's position, the status bar's content, and inline input validation that needs no dialog.

### Theme, confirmed

There are **21** `setStyleSheet` uses, in `run_widgets.py`, `staged_run_screen.py` and `three_pane_run_screen.py`. There is **no shared theme, no `QPalette` and no `setStyle` anywhere** in `src/ate`. `_FLAG_STYLE` is defined twice. `app.py`, `history_screen.py` and `export_log_dialog.py` carry no styling. Two consequences:

- **The status bar "chips" are a bug.** `StatusBar` sets a stylesheet with no selector (`run_widgets.py:114`), so its background, top border and padding apply to every child `QLabel`.
- **Colours are half-specified.** Fixed backgrounds are set without a text colour, so they rely on the palette being light. With a dark `QPalette`, standing in for a PC in dark mode, the active input's label and the header rows become white on pale yellow and white on pale grey, which is **unreadable**, and the status bar goes the same way: `app-dark-palette-run-blocking`, `app-dark-palette-stop-on-fail`. I could not confirm this on Windows, and `setColorScheme(Dark)` had no effect on the offscreen platform, so I set the palette directly. From what I know of Qt, with no `setStyle` call Windows gets its native style rather than Fusion, and Qt 6 follows the system dark setting, so this is likely on any dark-mode Station.

### Measured contrast (WCAG ratio; 4.5 is the normal-text target)

| Pair | Ratio | Note |
|---|---|---|
| Disabled text (sampled about `#bebebe`–`#c2c2c2`) on white, used for **answers already given** | **about 1.8** | Answers are record, not decoration |
| Same text on `#efefef` button | **about 1.6** | |
| `#777777` Media Release on selected row `#308cc6` (sampled) | **1.21** | Practically invisible: `app-t-stop-on-fail` |
| White on selection `#308cc6` | 3.69 | |
| `#777777` on white / `#fff5f5` / `#fff8e6` | 4.48 / 4.19 / 4.23 | Slightly short |
| White on `#c62828` bar | 5.62 | Fine |
| Mockup Abort fill on its red bar | **1.00** (border 1.60) | Button edge disappears |
| Mockup Retry fill on its red bar | **1.02** | Different hue, same lightness |
| Mockup disabled step `#aaaaaa` | 2.32 | |
| Mockup result badges | 5.58–13.76 | All fine |

**Colour-blind reading.** No result in the application is colour-only today, because results are words. That is safe but slow. The mockup's badges keep their words, but its PASS and FAIL tints are close to identical under deuteranopia (**1.06**), as are its green and red header chips (**1.14**). Colour must not carry the difference: see the shape-and-weight rule in section 3.

**From a distance.** At 96 dpi the header's 12 px text has a capital about 2.3 mm tall. Normal acuity at 2 m needs about 2.9 mm, so today's state text is **below the limit of reading** at 2 m. The mockup's 20 px gives 3.9 mm. The proposed 28 px gives 5.4 mm. These are estimates, and they ignore screen size.

**Keyboard (from the code; I sent no key events).** Focus moves to the active input and Enter submits a line edit. There are no shortcuts or mnemonics, nothing connects Enter to Next on the first stage, and nothing moves focus to the failure prompt. The fail bar's styled buttons have no `:focus` rule, so I expect no visible focus on them.

## 3. Design choices

Concept renders: `concept-stage1`, `concept-run-waiting`, `concept-run-stop-on-fail`, `concept-finished-pass`, `concept-finished-fail`.

### Light or dark

**Light body with a dark header strip, as in the mockup, and one fixed palette whatever the OS asks for.** A dark mode doubles the checking for little gain on a lit production floor. Alternative: both themes, later, which the token approach allows without rework.

### Style

`Fusion` on every platform, plus an explicit `QPalette` and one application stylesheet. Windows and Linux then render alike, and Linux test screenshots become a fair proxy for Windows. The alternative, keeping the native Windows style, leaves mixed native and stylesheet controls and can't be checked here.

### Palette

| Token | Value | Use | Contrast measured |
|---|---|---|---|
| `canvas` | `#f3f5f8` | Window background | |
| `surface` | `#ffffff` | Lists, tables, fields | |
| `subtle` | `#eef1f5` | Header rows, disabled fields | |
| `line` | `#e4e8ee`, strong `#c9d0da` | Row rules, panel edges | |
| `field-border` | `#7f8a99` | Input and button outlines | 3.50 on surface |
| `ink` | `#1a2230` | Main text | 15.96 on surface |
| `ink-2` | `#556070` | Secondary text; answered values | 6.38 on surface, 5.63 on subtle |
| `ink-disabled` | `#6a7482` | Disabled controls | 4.74 on surface |
| `chrome` | `#141c28` | Nav strip | |
| `head` | `#1f2a3a` | Run header | White 14.48; `#c3ccd9` 8.93 |
| `primary` | `#0b5cc4` | Primary button, selection, focus ring | White 6.28 |
| `primary-tint` | `#dbe8fb` | Current step, soft selection | Ink 12.88 |
| `ask` | `#fff4cc` with edge `#f2b705` | The input waiting on the Operator | `#3d2c00` 12.23 |

### Result states

**Rule: the worse the result, the louder it is. Word, glyph and weight carry the meaning, and hue only supports it.**

| Result | Style | Glyph | Text on fill |
|---|---|---|---|
| PASS | Pale `#e3f4e8`, ink `#14532d` | ✓ | 7.97 |
| FAIL | Solid `#b3261e`, white | ✕ | 6.54 |
| ERROR | Solid `#5b1010`, white | ! | 13.76 |
| NOREPORT | Solid `#f2b705`, ink `#3d2c00` | ∅ | 7.41 |
| NOTRUN | Outline `#7f8a99` on white, ink `#556070` | – | 6.38 |
| SKIPPED, FORCED_NOTRUN | Pale `#eef1f5`, ink `#556070` | – | 5.63 |
| RUNNING | Solid `#0b5cc4`, white | ▶ | 6.28 |
| Waiting on Operator | Solid `#f2b705`, ink `#3d2c00` | ? | 7.41 |

- PASS against FAIL: fill-to-fill **5.72**, **4.82** with deuteranopia, **7.86** with protanopia, against the mockup's 1.06 under deuteranopia.
- FAIL against ERROR: 2.10, 2.28, 1.77. This is modest, which is why the glyph differs too.
- Only the verdict chip in the header uses solid green `#1b7a3d` (white 5.39). Solid green against FAIL red would be 1.02 under deuteranopia; the chip is safe only because it also carries ✓ or ✕ and the word.
- **Row tints:** failed or paused row `#fde8e6` with a 5 px `#b3261e` left bar; current row `primary-tint` with a 5 px `primary` bar. The bar is the cue; the tint is not, since the pale tints differ by 1.03 even for normal vision.

### Type

Sizes in pixels. Pixel sizes behave the same on Windows and Linux, whereas this repo's 9 pt default depends on logical DPI. The stack is Segoe UI on Windows and Noto Sans then DejaVu Sans on Linux; mono is Consolas, then Cascadia Mono, then DejaVu Sans Mono, used for Step Numbers, timers and logs.

| Role | Size and weight |
|---|---|
| Run state chip | 28 semibold to bold |
| Current step name | 18 semibold |
| Run timer | 18 mono |
| Stage page and fail bar text | 15 (16 bold for its step label) |
| Schedule title in the header | 15 semibold |
| Body | 13 |
| Secondary, table header, log, status bar | 12 |
| Badge | 12 bold, 11 is the floor |

### Spacing, radii, density

- **Spacing scale:** 4, 8, 12, 16, 24, 32. Window content 16 horizontally; stage pages 24–32.
- **Radii:** 4 for fields and badges, 6 for buttons and chips. No shadows.
- **Rows:** step row 30 px minimum with 5 px vertical padding; current row grows to fit; three-pane table 28 px. This is denser than the mockup's current row and looser than today's 14 px.

### Buttons

| Kind | Style | Use |
|---|---|---|
| Primary | `primary` fill, white, semibold | One per view: Next, Start run, New run |
| Secondary | White, `field-border` outline | Back, Open report, Rescan, input OK when not waiting |
| Danger | White, 2 px `#8c1d18` outline and text (9.11) | Header Abort; deliberately not a large solid red |
| On red bar | Solid white, ink, 2 px white border, 3 px dark ring on focus | Continue, Retry, Abort (white 6.54 against the bar), with Abort set 16 px apart |

Every button: 32 px minimum height, 40 px for "big"; content width, never full window width; a 2 px focus ring.

### Tables and step lists

No vertical grid lines; 1 px `line` between rows; semibold 12 px header on `subtle`; no selection in the step table; numbers right-aligned or mono; truncated cells get a tooltip; `primary` selection with white text (and secondary text in a selected row switches to white or `#dbe8fb`, which fixes the 1.21).

### Icons

**Lucide (ISC licence), about 12 SVGs vendored into the package:** check, x, alert-triangle, octagon-x, play, refresh-cw, file-text, history, play-circle, bug, search, download.

- Render with `QSvgRenderer` into a tinted pixmap at the device pixel ratio. QtSvg ships with Qt, so **no new runtime dependency**. Check Lucide's `LICENSE` when vendoring and include it; I took the licence from memory.
- **Badges paint their glyph with `QPainterPath`** rather than as text. The comment at `run_widgets.py` on `BROKEN_MARKER` records `⛔` drawing as a missing-glyph box on a font-poor runner, and `⚠` carries the same risk.
- Alternatives: Phosphor (MIT), or Material Symbols (Apache 2.0), which is heavier-looking.

### Run state vocabulary

| Situation | Chip | Colour |
|---|---|---|
| Pre-test Actions and steps executing | `RUNNING` | Primary |
| A blocking Step Input waits | **`INPUT NEEDED`** | Amber |
| A failed step pauses the run | **`STEP FAILED`** | Red |
| Post-test Actions | `POST-TEST` | Primary |
| Finished | `✓ PASS` or `✕ FAIL`, End Reason alongside | Green or red |

The mockup shows `RUNNING` while the run blocks on the Operator, and that is the moment they most need to know. Nothing here is the internal `STARTED`, `FINISHED` or `SELECTING`.

## 4. Recommendations, ranked

Everything below works on PySide6 with Qt 6.11 on Windows and Linux with **no new runtime dependency**. Nothing here is something Qt cannot do; the two limits are noted under R1 (stylesheets) and R4 (row-widget performance). Effort assumes the 32a port is done. Many tests pin today's look: mockup colours appear in 4 test files, and the `→` row text in 11, including `test_finished_run_layout_30b.py`, which pins the three-pane band stretch. Each packet must budget for updating them.

| # | Problem | Change | Impact | Effort | How in Qt |
|---|---|---|---|---|---|
| **R1** | No theme; 21 scattered stylesheets; chips bug; dark-mode breakage; 1.21 selection text | New `ui/theme.py`: tokens, `Fusion`, a fixed `QPalette`, one application stylesheet. Remove inline styles; state via dynamic properties (`[state="active"]`) | High, and everything below depends on it | M | `app.setStyle`, `app.setPalette`, `app.setStyleSheet`; `unpolish`/`polish` on property change. Limit: no box shadows in stylesheets, and `::item` background rules override `item.setBackground`, so choose one per list |
| **R2** | No verdict, no context, 12 px state, no report access | Dark Run header: state chip, Test Schedule, UUT Type and serial, Project, Operator, Stop on Fail, End Reason, current action, timers, Abort, or Open report and New run. Three-pane gets the same strip across its centre and right panes. Remove the blank 24 px band | **Highest for Operators** | M | A `RunHeader` `QWidget` shared by both layouts; labels with object names |
| **R3** | Step list has 29% of the window | One bottom band: Log and Summary tabs, with the Log live during the run. Move Debug to a real separate window (`run-screen.md` line 33), opened from the nav strip or `Ctrl+D`. Band fixed at about 140 px, in a `QSplitter` so it can be resized; window size remembered | High | S–M | `QTabWidget`, a top-level `QWidget` for Debug, `QPlainTextEdit` for logs with level colours via `appendHtml` or `QSyntaxHighlighter`, replacing the list-in-scroll-area |
| **R4** | Results are plain text; no Actual or Expected; current step not enlarged | Step rows: mono number, name, Attempts tag, right-aligned badge, detail line, enlarged current or failed row with left bar; scrolled into view instantly | High | L | Reusable row `QWidget`s via `setItemWidget` (input rows already do this), painted `ResultBadge`. Update rows in place rather than resetting all of them. Check 500 steps stay smooth; if not, move non-input rows to a `QStyledItemDelegate`. Three-pane Result column: delegate |
| **R5** | Setup stages unguided; no Back; huge fields; validation arrives late | Stage rail, fixed footer with Back and primary Next or Start run; 360 px, 15 px fields; rename "Run this schedule" to Next; Enter advances; say what's missing at Next; empty-state text | High | M | Row of labels with properties; `QFormLayout` with fixed field width; `returnPressed`. Keep the spec's stage order |
| **R6** | Fail bar lacks measurements and its text is small | 15–16 px text with Actual and Expected; three white buttons, Abort set apart; move focus to the bar; `Alt+C`, `Alt+R`, `Alt+A`. **No default button, no single-letter keys**, because a barcode scanner's stray Enter or letters must not answer a failure | High | S | Fix the label selector; mnemonics via `&Retry`; `setAutoDefault(False)` |
| **R7** | Three-pane hides the prompt at small sizes | Set a real minimum for each layout, about 1024x640 for three-pane and 800x600 for staged, and stop the window growing unasked. Keep the prompt's buttons pinned inside the visible width, or place the prompt under the table | High on small screens | S–M | `setMinimumSize`; put the prompt in the sticky banner area rather than `setCellWidget` |
| **R8** | Export dialog can exceed the screen; no Cancel | Owner boxes in a scroll area capped at about 280 px, with All and None; Level and Source side by side; `QDialogButtonBox` | High on long schedules, cheap | S | `QScrollArea`, `QDialogButtonBox` |
| **R9** | History is filters and buttons with 3 visible rows | Filters in one compact wrapped row or a collapsible section; table takes the height; toolbar with one primary action; result badges; friendly date; right-hand or lower detail pane with labelled fields; stretch last column | High for anyone using History | M | `QTableView` with delegate; `QSplitter`; `QHeaderView.setStretchLastSection` |
| **R10** | Three-pane is grid-heavy, clipped and has a stray blue cell | `setSelectionMode(NoSelection)`, no grid, badges, tooltips, counts as badges, one wrapped log, Debug window as R3, picker rows tightened, and Start hidden once finished. Allow the right pane to collapse | Medium–high | M | `setShowGrid(False)`; `QSplitter`; delegates |
| **R11** | Staged picker is a list; Media Release missing from usable rows | Table: Test Schedule, Media Release, Status, with one row height and the error wrapped within Status. This restores `run-screen.md` lines 24 and 41 | Medium–high | M | `QTableWidget` or a model; selection by row. Broken rows selectable for reading but not for starting |
| **R12** | Answers look disabled at about 1.8:1 | Answered inputs shown as read-only text in `ink-2` (5.63), not faded controls; unasked future inputs use `ink-disabled` | Medium–high | S–M | `setReadOnly` where possible, a value label otherwise |
| **R13** | Navigation hidden in a menu; Station out of sight | Slim dark strip: ATE, Run, History, Station name, Debug window. Keep the `View` menu, native title bar and `ui.allowLayoutSwitch` | Medium | S | `QWidget` with checkable `QToolButton`s |
| **R14** | Font-dependent markers; no icons | Icons from section 3; window icon | Medium | S–M | `QSvgRenderer`. Confirm `QtSvg` is collected in `packaging/pyinstaller/ate.spec` |
| **R15** | Keyboard gaps | Mnemonics in the menus and action buttons; visible 2 px focus; tab order reviewed; `F5` for Rescan; `Ctrl+D` for Debug; accessible names on badges | Medium | S | `setTabOrder`, `QShortcut`, `setAccessibleName` |
| **R16** | Stale "Enter a number" stays after a valid value (`app-s-error-stop`); `clicked:` notes sit far from their buttons | Clear the error when an answer is accepted (a bug, independent of looks); move the notes next to the controls | Small but real | S | One `setText("")`; layout order |
| **R17** | No empty or busy states (a filter with no match is a blank list, an empty History a blank table, and the code has no report-writing indicator on the Run screen) | "No Test Schedules found: check `testDefinitions.directories`", "No Test Schedule matches this filter", "No runs yet", "Writing report…" beside Open report. No log placeholder needed once logs are single-pane | Medium | S | Placeholder `QLabel`, `QStackedLayout` |

**Suggested order:** R1, then R2 and R3 together (the largest visible change for the least risk), then R6, R7, R8, R16. Then R4, R5, then the rest. R1–R3 alone would close roughly half of the visual gap, by my judgement.

## 5. Mockup ideas not to copy

- **Fake window frame, grey backdrop, shadows, variant switcher, simulated title bar.** Prototype scaffolding. Keep the native title bar: a frameless window costs snapping and accessibility on Windows and gains nothing.
- **Red Abort on the red bar (1.00, border 1.60) and blue Retry on it (1.02).** The buttons lose their edges. Use white buttons.
- **Retry as the highlighted primary.** It pushes the Operator towards one answer on hardware where retrying may not be right. All three are peers here unless the user prefers otherwise (D6).
- **Pale green and pale red tints as the only difference** (1.06 under deuteranopia), and a green and red chip pair (1.14).
- **`RUNNING` while blocked on an input, and `WAITING` on a failure.** See the vocabulary in section 3.
- **`#aaaaaa` struck-through disabled steps (2.32).** Hard to read, and it looks deleted. Use `ink-disabled` and a NOTRUN badge.
- **Smooth animated scroll to the current step.** On a fast Test Schedule it never settles. Jump immediately.
- **17 px as "enlarged".** Better than nothing but small from a distance; 18 px for the name is fine only because the 28 px chip carries the state.
- **Faded (`opacity: .8`) inputs and the 2.47:1 disabled button colour.** Answers have to stay legible.
- **Eight columns in a 618 px centre pane at 1280 wide.** The mockup clips "Messag…" too. Let the log pane collapse.
- **The mockup's stage order (Test Schedule first).** `run-screen.md` settled UUT and Operator first; keep that.
- **Keeping the mismatch flag and store checkbox on stage 3 as well as in the dialog.** Say it once at Start, with the amber flag on stage 2. This is what the application does now.
- **Dark heading bars on dialogs.** Use the native dialog frame.

## 6. Decisions for the user

| # | Question | Recommendation |
|---|---|---|
| **D1** | Light only for v1, ignoring the PC's dark mode? | **Yes.** One fixed light palette. This also removes the unreadable dark-mode rows. |
| **D2** | Debug as a separate window, as `run-screen.md` says, replacing today's always-on pane? It is the biggest space gain, and it reverses what 08 and 30b built. | **Yes.** Opened by a button and `Ctrl+D`; remembered per session. |
| **D3** | During a run, must future steps keep their inputs fully visible? `run-screen.md` line 30 says always visible, and they take a lot of room. | **Change it for the staged Run view only:** a one-line muted summary (`1 input: LED colour`) on future steps, full controls on current and finished ones. Stage 3 and three-pane keep the full view. If you prefer the agreed behaviour, the rest of this report still stands. |
| **D4** | Are `INPUT NEEDED` and `STEP FAILED` the right chip words? | **Yes.** Operators should not have to learn `STARTED` or `WAITING`. |
| **D5** | Add a Run, History, Station strip, or keep only the `View` menu? | **Add the strip** and keep the menu. |
| **D6** | Failure prompt: three equal buttons, or Retry highlighted as in the mockup? | **Three equal**, with Abort set apart. Choose highlighted Retry if retry is nearly always right on your Test Schedules. |
| **D7** | Font: each platform's own, or bundle one? | **Platform fonts** (Segoe UI, Noto Sans or DejaVu Sans). No extra files, and Segoe UI is hinted for 100% scaling. Alternative: bundle Inter (OFL 1.1) for identical metrics everywhere. |
| **D8** | Rework both layouts equally, or staged first? Staged is the default. | **Staged first:** R1–R3 and R6–R8 cover both; R4 and R11 reach three-pane afterwards. |
