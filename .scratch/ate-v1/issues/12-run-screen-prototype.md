# Run screen, input forms, log pane and debug window

Type: prototype
Status: claimed
Blocked by: 04, 10

## Question

How should the Run screen look and behave: entering Project, UUT name and serial, choosing a Test Schedule, enabling and disabling individual Test Steps before the run starts, showing live step progress, step input forms (text boxes, radio buttons), the log pane, and the separate debug window?

Inputs: "Test Step structure and outcome model" (04) fixes the layout. The screen is a vertical list of every step, with each step's inputs always visible under it. A blocking input is enabled only while its `ATE.input(n)` call waits, and a `POLL` only while its step runs.

Also from "Test Definition format" (07):
- Project is a free-text field prefilled from the application config default.
- The Test Schedule picker is one flat list of every visible schedule across all Media Releases, each row showing the `ScheduleParams` name and the Media Release. Broken Test Definitions and schedules appear with their errors.
- A rescan action is available.
- A checksum mismatch is flagged prominently.

Also from "Flow control" (10):
- Pre-run configuration has a stop on fail / continue on fail radio button, prefilled from `setStopOnFail` (default stop on fail).
- On a failure with stop on fail, the run pauses with the failed step highlighted and a Continue / Retry / Abort prompt. Retried steps show their Attempts.
- A whole-run Abort control; no pause or skip.
- A status bar at the bottom: progress through the Pre-test and Post-test Actions, total run time, and time in the current action or step.

Also from "Sub-schedules" (14):
- An include step shows as a header row with its own Step Name and optional Step Number, followed by the Sub-schedule's steps. It has no result of its own; it may show a roll-up of the steps beneath it.
- Toggling a header before the run sets every step beneath it, nested ones included. Steps beneath can still be toggled one by one, and a header over mixed states shows a tri-state mark.
- Every step shows its optional Step Name.

Also from "What a Test Run records" (11): UUT Type and serial are required free text on the Run screen. With `database.storeMismatchedFiles` = `ask`, the run-start confirmation lists checksum-mismatched files with a "Store mismatched files with this Test Run" checkbox, ticked by default.

## Comments

### Prototype, 2026-10-17

Asset: `prototypes/run-screen/run-screen-prototype.html` on branch `prototype/run-screen` (throwaway, never merged). Open it in a browser and switch variants with `?variant=A|B|C`, the floating bar, or the arrow keys. A simulated Test Run covers every Step Input type, a Sub-schedule header, a failing step that passes on Retry, a Lua `ERROR` (gain > 1.8), a disabled step and a `NOREPORT` step.

- **A, classic single column**: setup fields across the top, the schedule as a combo box, steps in the middle, the log docked below, and the failure prompt and run-start confirmation as modal dialogs.
- **B, three panes**: setup and a filterable schedule list on the left (errors and mismatches shown inline), steps as a table with Result, Attempts, Actual, Expected and Message columns, and the log on the right. The failure prompt sits inline under the failed step with a sticky banner, and live `Result` counts appear on the left.
- **C, staged setup then focus view**: a three-stage setup (schedule table, UUT and Operator, steps and options with the mismatch checkbox inline), then a run view with a large state header and timers. The current step is enlarged and scrolled into view, the failure prompt is a full-width bar, and the Log and Summary are tabs.

Gap the prototype exposed: the ticket doesn't list an **Operator** field, but every Test Run records a free-text Operator, so all three variants add a required one.
