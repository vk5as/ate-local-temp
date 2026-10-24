# 23d: History screen rendering fixes

**Parent:** 23 (History screen) — `23-history-screen.md`

**What to build:** Fix what the 2026-10-24 screenshots of the History screen showed (`/tmp/ate-shots/png/09-history-*.png`); the 23b tests passed because they read widget text, not what is on screen.

**Blocked by:** 23c (History during a run)

**Status:** resolved

- [x] The summary panel shows the selected run's fields, `Result` counts and mismatches on screen (built the layout on a dedicated `QWidget` container before `setWidget`, so the labels are parented)
- [x] Column names come from `_HistoryItemModel`, which serves friendly labels via `horizontalHeaderData` as a real header row; body data starts at model row 0 and selection still maps rows correctly
- [x] "Include Run log" is ticked by default (spec: `history-screen.md`, `test-report-format.md`)
- [x] "Show all runs of this UUT" also updates the filter inputs to the UUT Type and serial it applied (`set_filters` writes widget values before reloading; dropdown options only refresh on a genuinely unfiltered view)
- [x] pytest-qt tests check visibility/geometry (parent/descendant relations + model header data), not just label text; existing 23b/23c row-offset assertions updated for the row-0 layout

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes.
