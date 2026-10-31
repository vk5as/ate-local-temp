# 25: Three-pane layout and layout switching

**What to build:** A second Run screen layout, three panes, built on the same run session, selectable per Station and switchable mid-run.

**Blocked by:** 10 (Step Inputs), 12 (Stop on Fail, the failure prompt and Retry), 15 (Sub-schedules)

**Status:** split

**Split into:** 25a, 25b — work happens in those packets; this ticket is resolved when all of them are.

- [ ] Left pane: setup fields, a filterable schedule list with errors and mismatches inline, live `Result` counts during the run
- [ ] Centre pane: a step table with Result, Attempts, Actual, Expected and Message; the failure prompt inline under the failed step with a sticky banner; inputs under steps
- [ ] Right pane: the log
- [ ] `ui.layout` (`staged` default or `threePane`) sets the default; a View menu switches layouts for the session, including mid-run, without affecting the run; `ui.allowLayoutSwitch` can disable the switch
- [ ] A pytest-qt smoke test builds the three-pane layout and switches layouts mid-run

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
