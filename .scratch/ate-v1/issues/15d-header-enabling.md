# 15d: Enabling under headers

**Parent:** 15 (Sub-schedules) — `15-sub-schedules.md`

**What to build:** Enable state cascades through headers.

**Blocked by:** 15b (`setInclude` and Sub-schedule instances), 11b (Toggle steps in stage 3)

**Status:** ready-for-agent

- [ ] `setEnable` on a header forces the default beneath it; toggling a header sets everything beneath; mixed states show a tri-state mark
- [ ] Seam-1 and pytest-qt tests cover cascading and the tri-state mark

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
