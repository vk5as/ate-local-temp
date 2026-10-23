# 14: Pre-test and Post-test Actions and the status bar

**What to build:** Schedules can set up and tear down around their steps; setup failures abort cleanly, teardown always runs, and the Operator sees progress and timers.

**Blocked by:** 13 (Abort)

**Status:** split

**Split into:** 14a, 14b, 14c, 14d — work happens in those packets; this ticket is resolved when all of them are.

- [ ] The Pre-test Action runs before the first step and the Post-test Action after the last, called with the run's `passFail`
- [ ] A Pre-test failure aborts automatically: every step `NOTRUN`, End Reason `PRETEST_FAILED`, and a log line naming the failure and explaining why every step is `NOTRUN`
- [ ] The Post-test Action always runs, after a stop, a Pre-test failure or an abort; ATE clears the abort flag first; a second abort ends only the current action; an error in it is logged and fails the run
- [ ] Each call is an `action` row (instance, phase, order, times, outcome `ok`/`error`/`aborted`/`skipped`, error)
- [ ] A status bar shows Pre-test/Post-test progress, total run time and time in the current action or step
- [ ] Seam-1 tests cover each outcome and End Reason

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
