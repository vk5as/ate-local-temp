# 14a: Pre-test and Post-test Actions and `action` rows

**Parent:** 14 (Pre-test and Post-test Actions and the status bar) — `14-pre-and-post-test-actions.md`

**What to build:** Schedules set up and tear down around their steps.

**Blocked by:** 13a (Whole-run Abort)

**Status:** resolved

- [x] The Pre-test Action runs before the first step and the Post-test Action after the last, called with the run's `passFail` — `test_pre_and_post_actions_record_ok_rows_around_a_passing_step`, `test_post_action_is_called_with_the_final_pass_fail`
- [x] Each call is an `action` row (instance, phase, order, times, outcome `ok`/`error`/`aborted`/`skipped`, error) — same two tests plus `test_actions_without_a_schedule_are_not_recorded`; error path in `test_erroring_pre_action_records_error_row_and_continues`
- [x] Seam-1 tests cover the normal path and the rows — `test_run_action_forwards_pass_fail_and_restores_context` (seam-1) + 4 end-to-end row assertions; full suite green (166 passed, no regressions)

Fix: run()'s pre/post loops unpacked each closure into two vars (`TypeError: iteration is only supported for tables`, `_LuaFunction` isn't iterable); enumerate the closures and pass `fn=`. Also moved `pass_fail`/`failed`/`aborted` init before the pre-test loop (it now needs the initial "PASS") and dropped a redundant engine-side `_log_seq` counter — the store already assigns unique action seqs from its own run counter; phase-action log lines are attributed by their `instance_seq`.

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
