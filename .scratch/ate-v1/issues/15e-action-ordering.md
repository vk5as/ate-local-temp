# 15e: Pre/Post-test Action ordering across the tree

**Parent:** 15 (Sub-schedules) — `15-sub-schedules.md`

**What to build:** Pre-test and Post-test Actions run in the right order across Sub-schedules.

**Blocked by:** 15b (`setInclude` and Sub-schedule instances), 14c (Post-test always runs)

**Status:** resolved

- [x] All Pre-test Actions run up front depth first, parent first; Post-test Actions in exact reverse; a fully disabled Sub-schedule's actions are skipped; after a Pre-test failure only instances whose Pre-test started get a Post-test Action
- [x] Seam-1 tests cover the ordering cases

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.

## Comments

- Implemented in `run_engine.py`: Pre-test Actions walk `host.instances` (already depth first, parent
  first, each include in step order) and Post-test Actions walk that same list reversed; each instance's
  Actions come from the instance's own `ScheduleParams`, now kept on `_ScheduleInstance.schedule`.
  `_action_instances` drops an instance only when its subtree has Test Steps and all of them are
  disabled — a schedule with no steps at all is not a disabled one (14c's empty-schedule tests still
  pass). "Pre-test started" is read as "the Pre-test pass reached that instance": an instance with no
  Pre-test Action of its own still counts as started, so its Post-test Action runs.
- `action.instance_seq` names the instance and `run_order` is now the Action's position within its phase
  across the tree (still 0 for a lone Action, so 14a/14c rows are unchanged).
