# Run screen, input forms, log pane and debug window

Type: prototype
Status: open
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
