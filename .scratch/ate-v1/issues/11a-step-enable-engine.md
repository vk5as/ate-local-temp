# 11a: Step enables in the engine and record

**Parent:** 11 (Enabling and disabling steps before the run) — `11-step-enabling.md`

**What to build:** Steps carry an enable default; disabled steps don't run and the enables are recorded.

**Blocked by:** 03 (Minimal staged Run screen)

**Status:** resolved

**Note:** Resume from branch `ticket-11-step-enabling` (engine/session work and a WIP checkpoint already committed).

- [x] `setEnable` sets each step's default; the run session accepts per-step overrides before Start
- [x] A step disabled at start isn't executed and records `NOTRUN` with no Attempt
- [x] Each `node` records `default_enabled` and `enabled`
- [x] Seam-1 tests cover defaults, overrides and `NOTRUN`

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
