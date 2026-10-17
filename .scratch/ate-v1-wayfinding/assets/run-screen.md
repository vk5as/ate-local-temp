# The Run screen

ATE's definition of the Run screen, settled in "Run screen, input forms, log pane and debug window". v1 ships two layouts of the same screen, **staged** and **three-pane**. The throwaway prototype is `prototypes/run-screen/run-screen-prototype.html` on branch `prototype/run-screen` (variant C is staged, variant B is three-pane). Vocabulary follows `GLOSSARY.md`.

## Architecture

The UI is decoupled from the engine so that layouts are interchangeable.

- **Engine**: Test Definition scanning, schedule loading, the Lua worker, flow control and the `RunStore`. It imports no widgets. It takes commands (start, toggle a step, supply an input, Continue / Retry / Abort at a failure prompt, abort) and emits events.
- **Run session**: the single object between the engine and the screen. It holds everything a layout shows: setup fields, the scanned schedule list, the expanded step tree with enables and results, Attempts, input state, failure-prompt state, timers and log lines. It turns engine events into Qt signals and layout actions into engine commands. It imports no widgets.
- **Layouts**: widget trees that only render the run session and forward user actions to it. They hold no run logic. A layout can be swapped mid-run without affecting the run.
- The engine and run session are testable without a GUI.

## Choosing the layout

- `ui.layout` in the app config sets the Station's default: `staged` (default) or `threePane`.
- A View menu switches the layout for the current session, including mid-run.
- `ui.allowLayoutSwitch` (default `true`) can turn that View menu option off, fixing the Station to its configured layout.

## Behaviour common to both layouts

**Setup**
- Required free-text fields: Operator, Project (prefilled from `defaultProject`), UUT Type and serial.
- The Test Schedule list is one flat list of every visible schedule across all Media Releases, each showing its `ScheduleParams` name and Media Release. Broken Test Definitions and schedules appear with their errors; checksum mismatches are flagged prominently. A rescan action is available.
- Before the run: the step tree with every step's enable toggle. Toggling an include header sets every step beneath it; a header over mixed states shows a tri-state mark. Steps show their optional Step Name.
- Stop on fail / continue on fail radio button, prefilled from `setStopOnFail` (default stop on fail).
- **Run-start confirmation only on a checksum mismatch**: it lists the mismatched files and, with `database.storeMismatchedFiles` = `ask`, a "Store mismatched files with this Test Run" checkbox ticked by default. Without a mismatch, Start begins the run directly.

**During the run**
- Every step is listed, with its inputs always visible under it. A blocking input is enabled only while its `ATE.input(n)` call waits; a `POLL` only while its step runs. Include headers group their Sub-schedule's steps and may show a roll-up.
- On a failure with stop on fail, the run pauses with the failed step highlighted and a Continue / Retry / Abort prompt. Retried steps show their Attempts.
- A whole-run Abort control; no pause or skip.
- The log shows `INFO` and above. The separate debug window shows every level.
- A status bar at the bottom: progress through the Pre-test and Post-test Actions, total run time, and time in the current action or step.

**After the run**
- New run clears nothing: Project, Operator, UUT Type, serial, schedule and options all carry over.

## Staged layout (default)

- **Setup in three stages**, in this order: **1 UUT and Operator**, **2 Test Schedule** (the schedule list as a table), **3 Steps and options** (step enables, stop on fail).
- **Run view**: a large state header with the timers. The current step is enlarged and scrolled into view so it can be read from a distance.
- The failure prompt is a full-width bar at the bottom of the step list.
- Log and Summary (live `Result` counts) are tabs under the steps.

## Three-pane layout

- **Left pane**: the setup fields and a filterable schedule list, with errors and mismatches shown inline; live `Result` counts during the run.
- **Centre pane**: the steps as a table with Result, Attempts, Actual, Expected and Message columns. The failure prompt sits inline under the failed step, with a sticky banner.
- **Right pane**: the log.
