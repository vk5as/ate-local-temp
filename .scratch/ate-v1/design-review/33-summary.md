# 33: Appearance review, combined summary

For the user's review. Nothing here is queued for implementation. Any work lands after 32a (PySide6). Full reports: `33-opus.md` and `33-strata.md`, with their shots. The two reviewers worked independently.

**Limits of the evidence:** neither reviewer ran Windows, or a real windowed session: the application was grabbed under Qt's `offscreen` platform on Linux. The Opus `concept-*` shots are hand-built with fake data. They show what Qt can do, not what the change costs.

## Where they agree

| Finding | Evidence |
|---|---|
| The gap is hierarchy and structure, not browser effects, and Qt can close most of it | Both verdicts; `33-opus-shots/concept-*` |
| No shared theme: 21 scattered stylesheet uses | Both counted them |
| The step list is starved by the log bands | 214–234 px against the mockup's 458–512 px at 1280x800 |
| A finished run shows no glanceable verdict, and results are plain text | Both |
| The run header carries almost no information (`FINISHED · 00:03`) | Both |
| Finished Run view has no Open report | Both |
| Staged picker omits the Media Release; setup stages have no stage indicator | Both |
| Abort on the red failure bar is the same colour as the bar, 1.00 contrast | Both; confirmed at `src/ate/ui/run_widgets.py:887,893` |
| History and the smallest window size are poor; three-pane is squeezed | Both |

Only one reviewer found these, and they are worth keeping:

- **Opus:** the export dialog needs 1867 px of height on a 60-step schedule, so Export can be unreachable. Three-pane at its smallest hides Continue, Retry and Abort. Actual and Expected appear nowhere in the staged layout. Answered inputs read at about 1.8:1. A stale input error stays beside an accepted value. A dark OS palette makes some rows unreadable (simulated; Windows not run).
- **Strata:** `ui.theme` is parsed from the config and never read. Log panes pad their columns but draw them in a proportional font, so nothing lines up.

## Where they disagree

| Question | Opus | Strata | Orchestrator's recommendation |
|---|---|---|---|
| Debug | Separate window, as `run-screen.md` says | Toggled pane in the header | **Separate window:** it is what was agreed, and it frees the most room |
| Run, History, Station strip | Add a dark strip, keep the menu | Keep the menu only; a strip duplicates chrome | **Add it:** an Operator should see where they are, and the Station name out of the title bar helps from across a bench. It is cheap to drop |
| Result glyphs | With colour, from the start | Colour first, glyphs next | **Together:** the mockup's PASS and FAIL tints are near-identical under deuteranopia (1.06, from matrices Opus entered from memory, so indicative) |
| Smallest window | About 1024x640 three-pane, 800x600 staged | About 900x700, hiding columns below it | **Opus's per-layout figures,** with the 1366x768 screen as the real test |
| Dark mode | Light only for v1 | Decide now, or remove `ui.theme` | **Light only, and remove or reserve `ui.theme`** |
| Failure buttons | Three equal, Abort set apart, no default so a scanner's Enter can't answer | Visible Abort; no view on the rest | **Opus** |

## Proposed order if accepted

1. **Theme:** `ui/theme.py`, Fusion, a fixed palette, one stylesheet. Fixes the Abort contrast and the status bar chips. Everything else builds on it.
2. **Run header and one log band:** both layouts. The largest visible gain for the least risk.
3. **Small fixes:** failure bar text with Actual and Expected, window minimums, export dialog, stale input error, monospace logs.
4. **Step rows with badges,** then **guided setup stages.**
5. **History, three-pane polish, navigation strip, icons, empty states, keyboard.**

Each packet must budget for tests that pin today's look: mockup colours in 4 test files and the `→` row text in 11, by Opus's count. Effort estimates are in `33-opus.md` section 4.

## Decisions for the user

1. **Light only for v1,** ignoring the PC's dark mode? Recommended: yes.
2. **Debug as a separate window?** It reverses what 08 and 30b built. Recommended: yes, with a button and `Ctrl+D`.
3. **During a staged run, must future steps show their inputs in full?** `run-screen.md` line 30 says yes, and they take a lot of room. Recommended (Opus): a one-line summary on future steps in the staged Run view only. This changes agreed behaviour.
4. **State words** `INPUT NEEDED` and `STEP FAILED`, replacing `STARTED` and `WAITING`? Recommended: yes.
5. **Add the Run, History and Station strip?** Recommended: yes, keeping the menu.
6. **Failure prompt:** three equal buttons, or Retry highlighted as in the mockup? Recommended: three equal, unless retry is nearly always right on your schedules.
7. **Fonts:** platform fonts, or bundle Inter for identical looks everywhere? Recommended: platform fonts.
8. **Both layouts equally, or staged first?** Recommended: staged first; the theme, header, log band and failure bar reach both anyway.
9. **`ui.theme`:** remove the key, or keep it reserved? Recommended: remove it if decision 1 is yes.
