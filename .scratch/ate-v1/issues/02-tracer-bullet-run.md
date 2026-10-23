# 02: Tracer bullet: run a one-step Test Schedule and record it

**What to build:** The thinnest end-to-end path: given a config pointing at a database and a directory holding one Test Definition with one Test Schedule, the headless run session can start a Test Run, the Lua schedule runs on a `QThread` worker, its step reports a `Result`, and the Test Run is recorded in SQLite. The database is created and checked through the `ate db` CLI.

**Blocked by:** 01 (Project skeleton, `ate` CLI and CI)

**Status:** resolved

- [ ] `ate db migrate` creates a missing SQLite database (WAL mode) after confirmation, or upgrades an older one after a timestamped backup; `ate db status` prints the schema version (`PRAGMA user_version`)
- [ ] At launch ATE stops with "run `ate db migrate`" for a missing or older database and "this ATE is too old" for a newer one; the app never migrates
- [ ] Storage goes only through a `RunStore` interface over dialect-neutral SQLAlchemy Core tables; this ticket creates at least `test_run`, `node` and `attempt` with UUIDv7 run keys and `(run_uuid, seq)` child keys, UTC ISO 8601 ms times and the Station UTC offset
- [ ] A minimal config (`database.path`, one Test Definition directory) is read; one `.atedef` with `format`, `mediaNumber`, `name`, `version` and `files` is read (full validation comes later)
- [ ] The Lua host creates a fresh `LuaRuntime` from `lupa.lua55` per load, refuses to start unless Lua is 5.5, and provides `ScheduleParams`, `StepParams`, the `Result` enum and `ATE.report(passFail, msg, actual, expected)` (last call wins)
- [ ] The run engine runs steps in order on one `QThread` worker and sets the run result (`FAIL` on any `FAIL` or `ERROR`) and End Reason `COMPLETED`; the worker reaches the session only through queued Qt signals
- [ ] The headless run session exposes: select schedule, set Operator/Project/UUT Type/serial, start, and observable run state
- [ ] Seam-1 test: a passing and a failing schedule each run headless and are read back through `RunStore` with the right results; seam-2 tests cover `ate db migrate`/`status` and the launch-time refusals

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
