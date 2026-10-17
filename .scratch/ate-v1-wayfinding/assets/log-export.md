# Exporting the Run log

ATE's definition of a Run log export, settled in "Export a Test Run's log from History". Vocabulary follows `GLOSSARY.md`. The log lines come from `log_line` in `test-run-record.md`.

ATE has no log viewer. An export hands the Run log to external tools (Excel, scripts, text editors) for drilling down. It is also the way to keep a log out of a report and still have it, because History can regenerate a report without the Run log (`test-report-format.md`).

## Where and when

- **History screen only**, for any finished Test Run, including an `INTERRUPTED` one. A run still in progress can't be exported.
- **One Test Run** per export.

## Export dialog

Three groups of checkboxes choose which lines are exported. All are ticked each time the dialog opens:

- **Level**: `DEBUG`, `INFO`, `WARNING`, `ERROR`.
- **Source**: `schedule` (`ATE.log`, `print`), `ate` (ATE's own lines) and `device` (Device trace).
- **Owner**: a tree of Run (lines with no owner), each Pre-test and Post-test Action, then the steps in tree order. Ticking a Sub-schedule header ticks everything beneath it. A step brings all of its Attempts.

A line is exported when its level, source and owner are all ticked. Export is disabled when no line would be exported. These filters may be refined after hands-on use.

## Saving

- A standard Save As dialog. Its file-type list offers **CSV** (`.csv`, the default), **JSON Lines** (`.jsonl`) and **Text** (`.txt`).
- The default folder and name follow the run's Test Report (`test-report-format.md`), with `_log` added: `<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<local start>_<PASS|FAIL>_log.csv`.
- The Save As dialog handles overwriting with its usual confirmation.

## Rows

One per exported log line, in the order the lines were recorded (`seq`), not grouped by owner.

| Field | Content |
| --- | --- |
| `time` | Station local time with milliseconds and the offset: `2026-10-17 14:03:22.101 +10:30` |
| `level` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `source` | `schedule`, `ate` or `device` |
| `owner` | `Run`, `Pre-test`, `Post-test` or `Step` |
| `schedule` | `ScheduleParams` name of the schedule owning the action or step; blank for `Run` |
| `step_number` | Step Number, as text (`BI1.2`); blank unless `Step` |
| `step_name` | Step Name; blank unless `Step` or not set |
| `attempt` | Attempt number; blank unless `Step` |
| `text` | The line, unchanged; it may contain newlines |

## Formats

- **CSV**: UTF-8 with a BOM, so Excel opens it correctly. A header row of the field names. Text with commas, quotes or newlines is quoted, so a multi-line traceback stays in one cell. No record of the filters.
- **JSON Lines**: UTF-8, one JSON object per line with the field names above. Blanks are `null`. `step_number` is a string, and `attempt` a number. No record of the filters.
- **Text**: UTF-8, for reading and sharing. It starts with a header naming the run UUID, UUT Type and serial, the Test Schedule, the start time and the filters applied. Then one line per log line:

  ```
  2026-10-17 14:03:22.101 +10:30  WARNING  ate       Run         checksum mismatch: lib/scpi.lua …
  2026-10-17 14:05:10.334 +10:30  INFO     schedule  Step 120#2  SCPI> MEAS:VOLT? CH1
  ```

  The owner is written as `Run`, `Pre-test`, `Post-test` or `Step <Step Number>#<attempt>`. The continuation lines of a multi-line message are indented under its text.
