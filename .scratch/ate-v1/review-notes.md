# Review notes for the final code review

Gaps spotted by the orchestrator when merging; the end-of-spec code review should check them.

- 08: `log_line` rows are inserted once per step, not batched "about once a second"; ATE-sourced lines are only the NOREPORT warning; `str(level)` on a Lua `Log` enum value may not yield `INFO` etc. once real Lua runs.
- 09: `_location()` derives `<file>:<line>` by text-searching the schedule source for `:setAction(`/`:setPreTestAction(` rather than reading the real call site (e.g. `debug.getinfo` from Lua); wrong for multi-line actions, helper modules and sub-schedules. Load-time gating only covers `report`/`input`, not "anything other than builders, require, ATE.log, enums".
- 13a: abort is only observed at step boundaries, so the step that was running finishes with its own result and the *next* step records `ERROR` "Aborted by Operator"; 13b/13c must make the running step itself record the abort.
- 14a: adds the `action` table without bumping `SCHEMA_VERSION` (still 2), so an existing v2 database never gets it via `ate db migrate`; settle how schema versions move before release (e.g. squash to one v1 schema).
- 12a: `HeadlessRunSession.select_schedule` reports an unreadable schedule with `print` instead of the application log, and `_pending_stop_on_fail` (set via `getattr`) is never reset between runs.
- 12b: the failure pause is keyed to `attempt_no` 1 throughout (`_pause_for_operator`, `failure_prompt.emit(node_seq, 1)`); 12c must carry the real Attempt number.
- 13c: the abort prelude registers `aborted` as a global Lua function, so schedules can see and shadow it; keep it in a local upvalue instead. The 13c tests check End Reason and NOTRUN but not the "Aborted by Operator" message.
- 14c: `test_remaining_posttest_actions_still_run_after_one_aborted` drives `_record_phase_action` with Python closures, not through `run()`, so it can't catch the flag carrying over between actions (orchestrator fixed that in run()); revisit once 15e allows several Post-test Actions.
- 06b: `run_file` rows and mismatch WARNINGs are written once at run end, not as each file loads (the record doc wants incremental writes, and an interrupted run loses them); files are matched to the Test Definition by unresolved `path / rel_path`, so `..` paths may not match.
