# 23d: History screen rendering fixes

**Parent:** 23 (History screen) — `23-history-screen.md`

**What to build:** Fix what the 2026-10-24 screenshots of the History screen showed (`/tmp/ate-shots/png/09-history-*.png`); the 23b tests passed because they read widget text, not what is on screen.

**Blocked by:** 23c (History during a run)

**Status:** ready-for-agent

- [ ] The summary panel shows the selected run's fields, `Result` counts and mismatches on screen (today `QVBoxLayout(self.summary_panel.widget())` runs before `setWidget`, so the labels are never parented and the panel is blank)
- [ ] Column names are the table's horizontal header labels, not its first body row (the header shows 1–8 today); the running/selection logic still maps rows correctly
- [ ] "Include Run log" is ticked by default (spec: `history-screen.md`, `test-report-format.md`)
- [ ] "Show all runs of this UUT" also updates the filter inputs to the UUT Type and serial it applied
- [ ] pytest-qt tests check visibility/geometry (`isVisible()`, header labels via `horizontalHeaderItem`/model header data), not just label text

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes.
