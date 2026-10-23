# 15a: Step Names and header rows

**Parent:** 15 (Sub-schedules) — `15-sub-schedules.md`

**What to build:** Every step and header has an optional Step Name, and header rows exist in the tree.

**Blocked by:** 11a (Step enables in the engine and record), 06a (Replacement `require`: resolution and caching)

**Status:** ready-for-agent

- [ ] `StepParams(name)` takes an optional Step Name for every step and header
- [ ] Header rows have no `Result`; invalid setter combinations (`setInputs`/`setAction` with `setInclude`, `setPrefix` without it) are load errors
- [ ] Seam-1 tests cover names, headers and the load errors

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
