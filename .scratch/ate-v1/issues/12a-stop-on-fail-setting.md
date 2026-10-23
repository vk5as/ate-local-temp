# 12a: Stop on Fail setting

**Parent:** 12 (Stop on Fail, the failure prompt and Retry) — `12-stop-on-fail-and-retry.md`

**What to build:** Schedules set a Stop on Fail default and the Operator can override it before Start.

**Blocked by:** 09 (Lua lockdown, errors and `ATE.run`)

**Status:** ready-for-agent

- [ ] `ScheduleParams:setStopOnFail(b)` sets the default (`true` when unset); stage 3 has a Stop on fail / Continue on fail radio button; the run records the setting
- [ ] Seam-1 tests cover the default, the override and the recorded value

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
