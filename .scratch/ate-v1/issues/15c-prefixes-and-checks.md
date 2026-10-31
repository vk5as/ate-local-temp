# 15c: Prefixes, numbering and tree checks

**Parent:** 15 (Sub-schedules) — `15-sub-schedules.md`

**What to build:** Step Numbers on the expanded tree are prefixed and validated.

**Blocked by:** 15b (`setInclude` and Sub-schedule instances)

**Status:** resolved

- [x] `setPrefix`: prefixes stack outside in
- [x] Duplicate Step Numbers and cycles are checked on the expanded tree at scan time and run start; a bad include breaks only the including schedule
- [x] Seam-1 tests cover numbering, duplicates and cycles

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
