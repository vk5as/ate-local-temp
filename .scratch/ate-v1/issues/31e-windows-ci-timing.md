# 31e: Two timing tests fail intermittently on Windows CI

**Parent:** none. CI failure on `integration/ate-v1`, 2026-10-31. Ubuntu passed every time; Windows failed on one Python version at a time:

- Notes-only commit, windows-latest 3.13: `tests/test_abort_blocking.py::test_posttest_actions_run_after_abort`, `abort took 2.234 s; must stop within 0.5 s`, at [0%].
- Later commit, windows-latest 3.14: the same test, `abort took 2.218 s`, and `tests/test_device_failures.py::test_seam_log_lines_and_device_warning_are_recorded_in_emission_order`, `'second' is stamped +0.302s off the time it was emitted`, at [15%].
- Windows 3.13 passed the second run and 3.14 the first.

**What to build:** Tests that measure what the product promises, and a broken abort or stamp still does.

**Blocked by:** none

**Status:** open until Windows CI is green

**User decision, 2026-11-01 (spec story 83):** an abort should stop within 0.5 s and must stop within 10 s. A test fails only past 10 s; past 0.5 s it warns, so it stays visible in CI's summary. Where Windows' 2.2 s went is still not established.

**Root cause, the abort test.** Not a first-abort cold start. [0%] is rounding: the test is the seventh of the run. The first two tests make the process's first `LuaRuntime` and first aborts, and passed the same bound in both failing runs. On Linux, the window after `request_abort()` in this test imports nothing and opens no file (audit hook), and the store statements take 1 to 2 ms each, the first `log_line` and `action` inserts included. No code path in the window waits on a timer near 2 s.

The old clock had two faults. It started on `time.sleep(0.1)` after the run key, with no proof the action had started, so any stall of the worker around then counted as abort latency: a 2.2 s stall injected after the run key and before the action gives `abort took 2.164 s`. It also stopped after a `get_test_run` read on the main thread.

Where the 2.2 s went on Windows is **not established**. The CI logs need a token this packet did not have, and Windows cannot run here. Two readings remain: the stall was before the action (a test artefact, now fixed), or inside the window, most likely a store write (below). Nothing in the code separates them, and two failures of the same size are not proof of either. A failure of the new test separates them: its message prints the worker's store calls.

**Root cause, the stamp test.** The stamp is not batched. `record_log` takes `time.monotonic()` as it records the line, and the drain only maps that to wall-clock, so `at` promises when the worker logged the line. It does not promise the worker got there on time. The old test compared `at` with `reset_at + 0.6`, the time the sleep was due to end, so `+0.302s` means the worker really reached `ATE.log` 0.3 s late and the stamp told the truth. Why it was late on the runner is not established: a stalled store write in the `ATE.sleep` or `ATE.log` drain, or scheduling. A 0.3 s stall before the stamp injected here gives `+0.299s` on the old test.

**Open product question, not a demonstrated bug.** The abort flag wakes the worker at once, but FINISHED follows synchronous SQLite commits on the worker, and an ATE call's drain commits before it reaches its wait. So a commit stall on a station (antivirus, slow disk) would delay the end of an aborted run for as long as it lasts. If the new test still reports over 0.5 s on Windows, that is what its timeline will show, and it is a product finding against story 83's 0.5 s aim. Since the user's decision that is a warning with the timeline, not a failure. `test_abort_during_a_stalled_store_call_ends_the_run_and_warns` now shows the shape here: a 2 s stall inside a commit makes the abort take about 2 s, and the run still closes ABORTED with the line recorded.

- [x] `_drive_abort` starts the clock on an event: the worker inside ATE.sleep's wait on the abort flag, or `input_asked` for a blocked input. The `time.sleep` and the unused `pre_abort_s` are gone. The clock stops at FINISHED, before any read. The bound is asserted in one place, and a breach prints the worker's timeline (now per the user's decision below).
- [x] The stamp test and `test_seam_log_lines_without_a_device_keep_their_order_and_own_times` (same flaw, not yet seen failing) bracket each `record_log` on the worker and require `at` within that bracket, give or take 0.1 s (millisecond cut, and the drain's offset comes from two clock reads the scheduler can separate). The stamp test keeps `seq` order, adds stamp order, and runs on 0.6 s after `second`, so an end-of-step stamp is caught.
- [x] The WARNING in the stamp test keeps a lag bound on `reset_at`, now the `-0.05 < lag < 0.3` its two sibling tests already use: it is stamped when the reader thread saw the failure, and that thread can be late under load.
- [x] Shown to fail: ATE.sleep ignoring the abort, ATE.input ignoring the abort, a 1 s stall after the abort (`abort took 1.011 s`, with the timeline), a stamp 0.5 s off, and stamps taken at drain time with lines batched to step end.
- [x] Shown to pass where the old test failed: the injected stalls before the action and before the stamp above.
- [x] Each changed test passes 30 runs alone and 30 under CPU load (6 busy loops on 3 cores). Full suite passes.

Second round, after the user's decision and review:

- [x] `tests/_fixtures.py::wait_for_aborted_run` holds story 83's bound: `SlowAbortWarning` past 0.5 s and a failure past 10 s, each naming the elapsed time and the worker's timeline. `test_abort_blocking`, `test_lua_seam13c_abort` and `test_lua_seam14c_posttest_always_runs` all use it, timed from `request_abort()`. The 13c and 14c tests had a 5 s bound, 13c's and one of 14c's timed from the start of the run: none was a requirement, so all are now 10 s.
- [x] The 10 s bound can fail. `for i=1,1e9` takes 5.6 s here under the count hook, so it would have ended by itself inside 10 s: the busy sources are now 1e10. `test_a_stall_past_the_must_stop_bound_fails_and_the_bound_is_the_requirement` injects a 1 s stall against a 0.3 s bound. Shown to fail with ATE.sleep ignoring the abort (`abort took 10.045 s; must stop within 10.0 s`).
- [x] `test_abort_during_a_stalled_store_call_ends_the_run_and_warns`: the abort lands inside a store call stalled 2 s, the run ends ABORTED, within 10 s, and the warning fires. `test_abort_interrupts_a_busy_loop_with_no_ate_calls_within_bound`: the clock starts after a thousand reads of the flag by the count hook, so inside the loop. Shown to fail with the count hook's check disabled.
- [x] `_emissions` keeps what `record_log` recorded (`host.log_times`) and the text it recorded, so a logged Lua boolean cannot KeyError (`test_a_logged_lua_boolean_is_stamped_under_the_text_lua_gave_it`, which shows the KeyError with the old key). The worker holds each line 0.4 s before its drain, because the old bracket could not see `_DRAIN_SLICE`'s 0.05 s. Shown to fail with a drain-time stamp: `'first' is stamped +0.400s off the time record_log recorded`. No lateness of the worker moves this comparison: only a coarse clock or a preemption between two adjacent clock reads can, against a 0.2 s tolerance.

Unproven until Windows CI runs: that these tests stop failing there. `test_seam_device_warning_is_stamped_with_the_failure_time` and `test_seam_failure_during_a_pause_is_logged_at_once_with_its_own_time` keep their wall-clock lag bound on a reader thread and could still fail on a very slow runner.

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes.
