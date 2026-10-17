# Test Report content and format

Type: grilling
Status: open
Blocked by: 03, 11

## Question

What does a Test Report contain (including a summary cover page with the overall result and a count of steps for each `Result` value) and how is it laid out, as HTML with PDF rendered from it, and how is it produced at run end and regenerated later?
Also from "Flow control" (10): the run's stop-on-fail setting and end reason (`COMPLETED`, `PRETEST_FAILED`, `STOPPED_ON_FAIL`, `ABORTED`); every Attempt of a retried step (number, result, message, log lines), with the step's result being the last Attempt's; retried steps are flagged on the report.
Also from "Sub-schedules" (14): include header rows (Step Name, optional Step Number) group their Sub-schedule's steps and have no `Result`; they are not counted in the summary, but may show a roll-up. Every step shows its optional Step Name.
Also from "What a Test Run records" (11): reports are regenerated from the database alone (`assets/test-run-record.md`). The record gives the run fields (including UUT Type, Station name at the time and UTC offset for local times), the resolved Media Release chain, per-file checksums and mismatches, the expanded tree with headers and Operator enable overrides (`default_enabled` vs `enabled`), each action's outcome, every Attempt with its failure-prompt choice, every input value, and every log line with its level and source. End Reason may also be `INTERRUPTED`.
