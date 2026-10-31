# 32b: A run on Windows CI missed FINISHED, and abort did not stop its worker in 10 s

**Parent:** none. CI on `08d7e56`, the first run after packet 32a (PyQt6 to PySide6), windows-latest Python 3.14 only. Ubuntu 3.13/3.14, Windows 3.13 and the release run passed.

`tests/test_three_pane_run_screen_25a.py::test_a_sixty_step_run_finishes_through_both_layouts`: `ThreePaneRunScreen never reached FINISHED` (call 22.40 s), then at teardown `a RunEngine worker outlived its test: abort did not stop it` and `1 RunEngine worker(s) did not stop` (teardown 10.01 s, two `stop_and_wait` of 5 s each).

**What to build:** The cause if it can be found, a test for it, and a failure that explains itself next time. No widened timeouts.

**Blocked by:** none

**Status:** open until Windows CI is green

**What the log shows.**

- The worker was slow, not hung for good. The stuck engine stays in `_live_engines`, so the next test's teardown would wait 5 s on it if it still ran. No later teardown reached 2.83 s, the floor of the slowest-20 list. It ended between 10 s after the abort and the next test's teardown.
- No thread died of an exception. The failure report has no captured stderr, so no slot traceback, no `choose` raising, and no "The Test Run ended on an unexpected error" (which `_end_unexpectedly` prints) and so no "database is locked".
- The main thread was slow too. 12.4 s of the call lies outside the 10 s wait, and the next test took 4.54 s where it takes under 0.53 s here. Other tests in the module ran at about Linux speed (13.58 s against 8.65 s, 6.32 s against 5.58 s). So a stall lasting about 35 s or more, not a general slowness of the runner.
- During `stop_and_wait` the main thread sits in `QThread.wait`, which releases the GIL (it does here: the worker wrote throughout). So GUI starvation cannot explain the teardown's 10 s.

**Reproduction on Linux.** No failure from load or races: Python 3.13 (3.14 is not on this machine), 25 runs alone, 25 under 6 busy loops on 3 cores (worst 10.9 s wall), 15 under load with random delays up to 0.3 s before the pause and in `choose`. `choose` runs on the main thread with the pause already pending, so the lambda slot is fine.

Delaying each store write does give the failure. A run makes about 187 writes:

| Per write | Old code | Fixed |
|---|---|---|
| 25 ms, 50 ms | passes the wait (the 25 ms run takes about 5 s a layout) | not run |
| 100 ms | wait expires; abort 3.32 s, 32 writes | wait expires; abort 0.71 s, 6 writes |
| 200 ms | wait expires; **abort over 9.7 s, 48 writes**, first `stop_and_wait` times out | wait expires; abort 1.28 s, 6 writes |
| 400 ms | wait expires; **abort over 20 s**, every `stop_and_wait` times out | wait expires; abort 2.83 s, 7 writes |

Old-code rows ran the main checkout, which is at `08d7e56`.

**Belief, not proof.** Store commits stalled on the Windows runner for tens of seconds, the run could not reach FINISHED, and the abort then had about 50 commits of its own to make. It fits every line above and 31e's unexplained 2.2 s stalls on Windows, whose open question was this. Every autocommit is a synced commit in WAL mode at SQLite's default `synchronous=FULL`. Nothing measured the disk on the runner. The injection did not reproduce the 12.4 s outside the wait, since it delays writes only and the main thread writes little; a stall in reads or the file system generally would fit, but that is a guess. A cause in PySide6 or Python 3.14 is not excluded: the one failure came straight after the switch.

**Two product faults found, each real whatever Windows did.**

1. **An abort could be lost at a pause.** The Abort button asks `pause_pending()`, sees none, and sets the flag; a step that then fails parks in `_pause_for_operator`, which never read the flag. The worker waits with `aborted=True` until someone answers the prompt that now shows. `stop_and_wait` has the same window. It is not this CI failure alone: a second `stop_and_wait` would have answered that pause. The pause now takes an abort as the answer ABORT, the same as the button during a pause, so the run ends `STOPPED_ON_FAIL`.
2. **An abort's cost grew with the steps left.** `_record_abort` made one commit per NOTRUN row: 404 commits for an aborted 400-step run here. 400 steps at 25 ms a commit is past story 83's 10 s with no stall at all. They are now one statement per 80 rows, so the same abort takes fewer than 20.

**Left open, for a decision.** A commit in progress cannot be interrupted, so a stalled disk still delays FINISHED for as long as the stall lasts. `PRAGMA synchronous=NORMAL` would remove the sync from most commits, at the price of the last commits on power loss. That trades against the record being crash-safe, and is the user's to decide.

- [x] `_pause_for_operator` answers ABORT when the abort flag is set; an answer already given stays. `test_an_abort_that_lands_just_before_a_pause_still_ends_the_run` fails before (worker parked, `aborted=True pause_pending=True`). `test_a_real_answer_given_with_the_abort_is_not_overwritten` guards the other order.
- [x] `RunStore.insert_nodes`, used by `_record_abort`. `test_abort_costs_a_few_commits_however_many_steps_are_left` fails before (`404 commits`). `test_a_big_batch_of_nodes_stays_inside_sqlites_variable_limit` covers 3000 rows.
- [x] `tests/_fixtures.py::describe_stuck_runs`: each live worker's and session's state, what is recorded, the store's write times with any write in flight, and all thread stacks (`faulthandler`). It goes into both teardown errors, and `pytest_runtest_makereport` adds it as a report section to any test that fails with a worker running, whichever wait helper it used. `test_a_stuck_worker_explains_itself` covers it, and a 400 ms write delay was seen producing the section.
- [x] No timeout widened. The test's 10 s wait stays and can still fail on a stall like this one, which then says so.
- [x] New tests and the sixty-step test pass 10 times under load (6 busy loops on 3 cores). Full suite `1480 passed, 4 skipped`.

Unproven until Windows CI runs: that the failure stays away, and what caused the stall. If it returns, the report's `store writes` line separates the readings: long or in-flight writes mean the disk, normal ones with the worker's stack elsewhere mean a fault this packet did not find.

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes.
