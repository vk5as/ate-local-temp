# Test Report content and format

Type: grilling
Status: open
Blocked by: 03, 11

## Question

What does a Test Report contain (including a summary cover page with the overall result and a count of steps for each `Result` value) and how is it laid out, as HTML with PDF rendered from it, and how is it produced at run end and regenerated later?
Also from "Flow control" (10): the run's stop-on-fail setting and end reason (`COMPLETED`, `PRETEST_FAILED`, `STOPPED_ON_FAIL`, `ABORTED`); every Attempt of a retried step (number, result, message, log lines), with the step's result being the last Attempt's; retried steps are flagged on the report.
Also from "Sub-schedules" (14): include header rows (Step Name, optional Step Number) group their Sub-schedule's steps and have no `Result`; they are not counted in the summary, but may show a roll-up. Every step shows its optional Step Name.
