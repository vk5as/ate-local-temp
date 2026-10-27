# 15d: Enabling under headers

**Parent:** 15 (Sub-schedules) — `15-sub-schedules.md`

**What to build:** Enable state cascades through headers.

**Blocked by:** 15b (`setInclude` and Sub-schedule instances), 11b (Toggle steps in stage 3)

**Status:** resolved

- [x] `setEnable` on a header forces the default beneath it; toggling a header sets everything beneath; mixed states show a tri-state mark
- [x] Seam-1 and pytest-qt tests cover cascading and the tri-state mark

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.

## Comments

- `_StepParams` now records whether `setEnable` was CALLED (`_enable_set`), not just its value, and
  `_TreeNode` carries `default_enabled`: a Test Step takes the nearest enclosing header's `setEnable`
  when one was called, otherwise its own. `_expand` passes `forced` down, and a header's own
  `setEnable` replaces it for its subtree, so the innermost header wins. A header row is not a Test
  Step, so its own `default_enabled` is just its own `setEnable` — an outer force does not overwrite
  a nested header's own default.
- `RunEngine._enabled(seq, step)` now returns the row's forced default plus its effective state; for
  a header the effective state is "any Test Step beneath it is enabled" (operator overrides on a
  header seq are never written, so they cannot change it). `default_enabled` is recorded as "from the
  schedule, after header forcing" (`test-run-record.md`).
- StepsOptionsStage: a header's box is derived — Checked when every step beneath is on, Unchecked
  when none is, `Qt.CheckState.PartiallyChecked` when mixed (a header with no Test Steps reads
  Checked, as the mockup's `enableBox` does). Clicking one calls `toggle_node` for every Test Step
  beneath it, and every toggle of a child re-derives its header rows, so children can still be
  toggled one by one afterwards. A header row never gets an override of its own.
- Header rows are `#eef1f5` and bold in the pre-run list, the finished RunView and the paused list.
  After a run a header shows `N/M done` plus `, K failed`; mid-run `N` counts only the steps whose
  node row is written (the failing step's row is written after its last Attempt). A QListWidgetItem
  text cannot be partly coloured, so `K failed` is plain text rather than the mockup's red span.
- Two `test_sub_schedules_15b.py` rows were updated: those assertions list header row text, which
  now carries the rollup.
- Screenshots: `/tmp/15d-shots/prerun-partial.png` and `/tmp/15d-shots/runview-rollup.png`, checked
  by scanning the grab's pixels for `#eef1f5`.
