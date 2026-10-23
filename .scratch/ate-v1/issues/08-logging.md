# 08: Logging, log pane and debug window

**What to build:** Schedules write log lines with `ATE.log` and `print`, ATE writes its own lines, everything is recorded with the run, the Run screen shows `INFO` and above, and a debug window shows every level.

**Blocked by:** 03 (Minimal staged Run screen)

**Status:** resolved

- [x] `ATE.log(Log.X, ...)` with `DEBUG`, `INFO`, `WARNING`, `ERROR`; arguments go through `tostring` joined with spaces; `print` maps to `INFO` — `src/ate/engine/lua_host.py`: the `Log` enum declares all four levels, `record_log` joins args via `str()`, and `_bind_print` routes the global `print` to `ATE.log(Log.INFO, ...)`.
- [x] `log_line` records time (`at`), level, source (`schedule`/`ate`/`device`), owner (run/action/Attempt) and text; lines are batched to the database about once a second — `src/ate/storage/store.py` `insert_log_lines` batches a step's lines with shared ownership (~once per step); ownership is structural via FKs (`action_seq`, `node_seq`+`attempt_no`).
- [x] The staged layout has a log pane showing `INFO` and above; a separate debug window shows every level — `src/ate/ui/staged_run_screen.py`: the Run view's `log_pane` (INFO+) and `debug_window` (every level), both filled from `session.store.log_lines()` in `refresh()`.
- [x] Seam-1 tests read back recorded lines with their levels, sources and owners — `tests/test_headless_session.py` reads schedule-sourced lines by level/source/owner; `tests/test_storage.py` round-trips a batched insert.

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
