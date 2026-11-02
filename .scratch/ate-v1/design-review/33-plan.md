# 33: Appearance implementation plan

Draft for the user's approval, 2026-11-02. Nothing starts until approved, and all of it follows packet 36. Details and values are in `33-opus.md`; `R` numbers refer to its section 4.

## User decisions

1. One fixed light theme for v1; `ui.theme` is removed.
2. Debug moves to a separate window, opened by a button and `Ctrl+D`. This reverses what 08 and 30b built.
3. Future steps stay fully visible during a staged run, as `run-screen.md` says. Opus's one-line summary is rejected, so the step list gains less room than its concept shots show.
4. State chip words: `RUNNING`, `INPUT NEEDED`, `STEP FAILED`, `POST-TEST`, then `PASS` or `FAIL`.
5. Add the dark top strip (ATE, Run, History, Station, Debug) and keep the `View` menu.
6. Continue, Retry and Abort look equal, with Abort set apart and no default button.
7. Platform fonts.
8. Both layouts are reworked equally: every packet covers staged and three-pane.

## Packets, in order

| Packet | What the Operator gets | Covers | Size |
|---|---|---|---|
| 37a Theme | One look on Windows and Linux; readable rows on a dark-mode PC; status bar without grey chips; Abort visible on the red bar | R1, D1, D7. `ui/theme.py`, Fusion, fixed palette, one stylesheet replacing the 21 inline uses; `ui.theme` removed | M |
| 37b Run header | A large state chip readable across the bench, Test Schedule, UUT, Operator, End Reason, timers; a verdict on every finished run; Open report | R2, D4, both layouts | M |
| 37c Log band and Debug window | More height for steps; one Log and Summary band, live during the run, with a time column in monospace; no nested scrollbar | R3, D2, both layouts. Reverses 08 and 30b | M |
| 37d Failure bar and small fixes | Larger failure text with Actual and Expected; three equal buttons, focus moved to the bar, no stray Enter; legible answered inputs; Export reachable on long schedules, with Cancel; prompt never hidden at small sizes; stale input error gone | R6, R7, R8, R12, R16, D6 | M |
| 37e Step rows | Result badges with word, symbol and weight for colour-blind Operators; Actual and Expected on rows; a larger current or failed row with a left bar | R4, R10's table parts, both layouts. 500-step speed checked | L |
| 37f Setup | Stage rail and Back; fields of a sensible width; a primary Next, Enter advances, validation at Next; a schedule picker showing Media Release | R5, R11, and the three-pane setup equivalents | L |
| 37g History and empty states | More than 3 visible runs; compact filters; result badges; labelled details; clear messages when empty or busy | R9, R17 | M |
| 37h Strip, icons, keyboard | Top strip; Lucide icons and a window icon; mnemonics, visible focus, F5, tab order | R13, R14, R15, D5. The icon licence is read when vendored | M |

## How it runs

- Strata implements one packet at a time; Strata and Haiku review each. No Opus unless the user asks.
- Each packet follows `33-opus.md`'s values and the mockup's structure, and is checked by screenshots at 1280x800, 1920x1080 and the minimum size, for both layouts. Screenshots go in the report to the user. They are offscreen grabs, since no real desktop session is available.
- Tests pinning today's look are updated in the same packet: mockup colours in 4 test files, `→` row text in 11.
- Manuals change only where they describe a screen. A packet that changes one updates it.

## Not included

- Dark mode, bundled fonts, and the one-line summary for future steps.
- A real Windows desktop check: someone needs to run a release on a Windows PC. This is the main risk to the look, and should happen after 37a and again at the end.
- A Help menu for the manuals. It would be cheap to add to 37h if wanted.

## Rough time

Each M about 2-4 hours and each L about 4-8 hours, including reviews, with no pauses: about 20-35 hours in total. Packets merge one at a time, so stopping early still leaves a working, improved application. 37a to 37d give most of the visible change.
