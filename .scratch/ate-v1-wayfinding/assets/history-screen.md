# The History screen

ATE's definition of the History screen, settled in "Settle the remaining Not yet specified items". Vocabulary follows `GLOSSARY.md`. It reads the Test Run record (`test-run-record.md`) and never changes it: Test Runs are append-only, so there is no delete.

## Finding runs

- One flat table of Test Runs, newest first.
- Sortable columns: start time, UUT Type, serial, result, End Reason, Test Schedule, Project and Operator.
- Filters: UUT Type, serial (substring match), result, End Reason, date range and Project.
- Rows load page by page, so a large database stays fast.

## A selected run

Selecting a run shows a summary panel: the run fields, a count for each `Result`, and any checksum mismatches. Its actions:

- **Open report**: opens the newest report file for the run in the system viewer.
- **Regenerate**: HTML and/or PDF, with the Include Run log toggle (`test-report-format.md`).
- **Export log**: see `log-export.md`.
- **Show all runs of this UUT**: filters the table to the same UUT Type and serial.
- **New run for this UUT**: switches to the Run screen with UUT Type, serial, Project and Test Schedule filled in.

## During a run

- History can be browsed while a Test Run is in progress. The running run appears with status "running" and no result; Open report, Regenerate and Export log are disabled for it.
- `ui.allowHistoryDuringRun` (default `true`) can turn this off. Then History is disabled during a run and shows "A Test Run is in progress".
