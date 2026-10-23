# 15e: Pre/Post-test Action ordering across the tree

**Parent:** 15 (Sub-schedules) — `15-sub-schedules.md`

**What to build:** Pre-test and Post-test Actions run in the right order across Sub-schedules.

**Blocked by:** 15b (`setInclude` and Sub-schedule instances), 14c (Post-test always runs)

**Status:** ready-for-agent

- [ ] All Pre-test Actions run up front depth first, parent first; Post-test Actions in exact reverse; a fully disabled Sub-schedule's actions are skipped; after a Pre-test failure only instances whose Pre-test started get a Post-test Action
- [ ] Seam-1 tests cover the ordering cases

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
