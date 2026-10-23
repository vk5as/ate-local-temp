# 09: Lua lockdown, errors and `ATE.run`

**What to build:** The Lua environment is locked down for traceability, a Lua error in a step records `ERROR` with a usable message and traceback, a step that never reports records `NOREPORT`, calls are rejected where they don't belong, and schedules can read `ATE.run`.

**Blocked by:** 08 (Logging, log pane and debug window)

**Status:** in progress — criteria a–d committed on `ticket-09-r2`; e+ next. Do not merge into integration/ate-v1 (orchestrator merges).

- [x] Runtimes use `register_eval=False`, `register_builtins=False`, `unpack_returned_tuples=True`; `dofile`, `loadfile` and `load` are removed and `python` is `nil`; chunks load with name `@<path>` and mode `t` (bytecode refused) — branch `ticket-09-lua-lockdown-and-errors`, commit `79eccb2`
- [x] Actions run through `xpcall(fn, debug.traceback)`; an error records `Result.ERROR` with its first line as the message and the full traceback logged at `ERROR` — commit `d86d31d`
- [x] `ATE` functions never let a Python exception reach Lua: `_AteTable._isolate` (routed through `__getattr__`) logs the Python traceback at ERROR prefixed `<file>:<line>: ATE.<fn>:` and returns graceful halves `(false, "<file>:<line>: ATE.<fn>: <msg>")`; messages start with the schedule's `file:line`, the Python traceback goes to the application log — commit `a0f1737`
- [x] A step that never calls `ATE.report` records `NOREPORT` and a warning — commit `836e053` (test `test_step_that_never_reports_records_noreport_and_warns`)
- [x] Load time allows only the builders, `require`, `ATE.log` and the enums; Pre-test and Post-test Actions disallow `input` and `report`; violations raise — commit `f55d356` (tests in `test_lua_lockdown_calls.py`)
- [x] `ATE.run` is a read-only table (UUT serial, Operator, Station, Project, Test Schedule name, Media Release media number, name and version); writing to it raises — commit `d0f9908` (tests in `test_lua_run_readonly.py`)
- [x] Seam-1 tests cover each rule, including the error message form and traceback content — commit `4d69a3e` (`tests/test_lua_seam1_errors.py`)

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
