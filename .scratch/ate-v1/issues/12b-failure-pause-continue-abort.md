# 12b: Failure pause with Continue and Abort

**Parent:** 12 (Stop on Fail, the failure prompt and Retry) — `12-stop-on-fail-and-retry.md`

**What to build:** With Stop on Fail a failed step pauses the run until the Operator chooses; this packet does Continue and Abort (Retry is 12c).

**Blocked by:** 12a (Stop on Fail setting)

**Status:** resolved

- [x] On `FAIL` or `ERROR` with Stop on Fail the engine pauses (no timeout) and the run session exposes the pending choice
- [x] Continue keeps the result and moves on; Abort makes remaining steps `NOTRUN`, keeps the failed step's result and ends with `STOPPED_ON_FAIL`
- [x] With Continue on fail, failures never pause
- [x] Each Attempt's `prompt_choice` is recorded (stamped onto the failing Attempt when the operator answers)
- [x] Seam-1 tests cover Continue, Abort, Continue on fail and the End Reason (`tests/test_stop_on_fail_continue_abort.py`, 5 tests)

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
