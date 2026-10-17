# 09: Lua lockdown, errors and `ATE.run`

**What to build:** The Lua environment is locked down for traceability, a Lua error in a step records `ERROR` with a usable message and traceback, a step that never reports records `NOREPORT`, calls are rejected where they don't belong, and schedules can read `ATE.run`.

**Blocked by:** 08 (Logging, log pane and debug window)

**Status:** ready-for-agent

- [ ] Runtimes use `register_eval=False`, `register_builtins=False`, `unpack_returned_tuples=True`; `dofile`, `loadfile` and `load` are removed and `python` is `nil`; chunks load with name `@<path>` and mode `t` (bytecode refused)
- [ ] Actions run through `xpcall(fn, debug.traceback)`; an error records `Result.ERROR` with its first line as the message and the full traceback logged at `ERROR`
- [ ] `ATE` functions never let a Python exception reach Lua: Python halves return `(ok, value)` and a Lua shim raises `error("ATE.x: " .. msg, 2)`, so messages start with the schedule's `file:line`; the Python traceback goes to the application log
- [ ] A step that never calls `ATE.report` records `NOREPORT` and a warning
- [ ] Load time allows only the builders, `require`, `ATE.log` and the enums; Pre-test and Post-test Actions disallow `input` and `report`; violations raise
- [ ] `ATE.run` is a read-only table (UUT serial, Operator, Station, Project, Test Schedule name, Media Release media number, name and version); writing to it raises
- [ ] Seam-1 tests cover each rule, including the error message form and traceback content

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
