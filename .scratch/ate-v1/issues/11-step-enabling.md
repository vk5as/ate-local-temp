# 11: Enabling and disabling steps before the run

**What to build:** Before starting, the Operator sees the step tree and can enable or disable individual Test Steps; disabled steps record `NOTRUN`, and overrides are recorded.

**Blocked by:** 03 (Minimal staged Run screen)

**Status:** ready-for-agent

- [ ] `setEnable` sets each step's default; the Operator can toggle any step in stage 3 before Start
- [ ] A step disabled at start isn't executed and records `NOTRUN` with no Attempt
- [ ] Each `node` records `default_enabled` and `enabled`
- [ ] Seam-1 tests cover defaults, overrides and `NOTRUN`

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
