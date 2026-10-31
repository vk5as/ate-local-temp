# 12: Stop on Fail, the failure prompt and Retry

**What to build:** With Stop on Fail, a failed step pauses the run so the Operator can Continue, Retry or Abort; every Attempt is recorded and the last one counts.

**Blocked by:** 09 (Lua lockdown, errors and `ATE.run`)

**Status:** split

**Split into:** 12a, 12b, 12c, 12d — work happens in those packets; this ticket is resolved when all of them are.

- [ ] `ScheduleParams:setStopOnFail(b)` sets the default (`true` when unset); stage 3 has a Stop on fail / Continue on fail radio button; the run records the setting
- [ ] On `FAIL` or `ERROR` with Stop on Fail, the run pauses with the step highlighted and a full-width Continue / Retry / Abort bar, with no timeout
- [ ] Continue keeps the result and moves on; Retry reruns the action from the start with `POLL` reset and blocking inputs cleared, Devices kept open, no limit; Abort makes remaining steps `NOTRUN`, keeps the failed step's result and ends with `STOPPED_ON_FAIL`
- [ ] With Continue on fail, failures never pause and Retry isn't offered
- [ ] Every Attempt is an `attempt` row with its final report values and `prompt_choice`; the step's result is its last Attempt's; a retried step can make the run pass
- [ ] Seam-1 tests cover each choice, Attempt recording and the End Reason

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
