# 12c: Retry and Attempt recording

**Parent:** 12 (Stop on Fail, the failure prompt and Retry) — `12-stop-on-fail-and-retry.md`

**What to build:** Retry reruns a failed step; every Attempt is recorded and the last counts.

**Blocked by:** 12b (Failure pause with Continue and Abort), 10 (Step Inputs)

**Status:** resolved

- [x] Retry reruns the action from the start with `POLL` reset and blocking inputs cleared, Devices kept open, no limit; not offered with Continue on fail
- [x] Every Attempt is an `attempt` row with its final report values and `prompt_choice`; the step's result is its last Attempt's; a retried step can make the run pass
- [x] Seam-1 tests cover retry-then-pass, retry-then-fail and Attempt rows

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
