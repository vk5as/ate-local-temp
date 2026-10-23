# Review notes for the final code review

Gaps spotted by the orchestrator when merging; the end-of-spec code review should check them.

- 08: `log_line` rows are inserted once per step, not batched "about once a second"; ATE-sourced lines are only the NOREPORT warning; `str(level)` on a Lua `Log` enum value may not yield `INFO` etc. once real Lua runs.
- 09: `_location()` derives `<file>:<line>` by text-searching the schedule source for `:setAction(`/`:setPreTestAction(` rather than reading the real call site (e.g. `debug.getinfo` from Lua); wrong for multi-line actions, helper modules and sub-schedules. Load-time gating only covers `report`/`input`, not "anything other than builders, require, ATE.log, enums".
