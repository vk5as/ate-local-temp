# 25a: Three-pane Run screen

**Parent:** 25 (Three-pane layout and layout switching) — `25-three-pane-layout.md`

**What to build:** A second Run screen layout, three panes, on the same run session.

**Blocked by:** 10a (Step Input widgets on the Run screen), 12d (Failure bar on the Run screen), 15d (Enabling under headers)

**Status:** resolved

- [x] Left pane: setup fields, a filterable schedule list with errors and mismatches inline, live `Result` counts during the run
- [x] Centre pane: a step table with Result, Attempts, Actual, Expected and Message; the failure prompt inline under the failed step with a sticky banner; inputs under steps. Right pane: the log
- [x] A pytest-qt smoke test builds it and runs a schedule

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
