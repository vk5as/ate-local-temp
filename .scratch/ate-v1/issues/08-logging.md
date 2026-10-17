# 08: Logging, log pane and debug window

**What to build:** Schedules write log lines with `ATE.log` and `print`, ATE writes its own lines, everything is recorded with the run, the Run screen shows `INFO` and above, and a debug window shows every level.

**Blocked by:** 03 (Minimal staged Run screen)

**Status:** ready-for-agent

- [ ] `ATE.log(Log.X, ...)` with `DEBUG`, `INFO`, `WARNING`, `ERROR`; arguments go through `tostring` joined with spaces; `print` maps to `INFO`
- [ ] `log_line` records time, level, source (`schedule`, `ate`, `device`), owner (run, action or Attempt) and text; lines are batched to the database about once a second
- [ ] The staged layout has a log pane showing `INFO` and above; a separate debug window shows every level
- [ ] Seam-1 tests read back recorded lines with their levels, sources and owners

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
