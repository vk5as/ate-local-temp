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

1. **An abort could be lost at a pause.** The Abort button asks `pause_pending()`, sees none, and sets the flag; a step that then fails parks in `_pause_for_operator`, which never read the flag. The worker waits with `aborted=True` until someone answers the prompt that now shows. `stop_and_wait` has the same window. It is not this CI failure alone: a second `stop_and_wait` would have answered that pause. The pause now ends on an abort. See the decision below for how it is recorded.
2. **An abort's cost grew with the steps left.** `_record_abort` made one commit per NOTRUN row: 404 commits for an aborted 400-step run here. 400 steps at 25 ms a commit is past story 83's 10 s with no stall at all. They are now one statement per 80 rows, so the same abort takes fewer than 20.

**Left open, for a decision.** A commit in progress cannot be interrupted, so a stalled disk still delays FINISHED for as long as the stall lasts. `PRAGMA synchronous=NORMAL` would remove the sync from most commits, at the price of the last commits on power loss. That trades against the record being crash-safe, and is the user's to decide.

- [x] `_pause_for_operator` ends on the abort flag; an answer already given stays. `test_an_abort_that_lands_just_before_a_pause_still_ends_the_run_aborted` fails before (worker parked, `aborted=True pause_pending=True`). `test_a_real_answer_given_with_the_abort_is_not_overwritten` guards the other order.
- [x] `RunStore.insert_nodes`, used by `_record_abort`. `test_abort_costs_a_few_commits_however_many_steps_are_left` fails before (`404 commits`). `test_a_big_batch_of_nodes_stays_inside_sqlites_variable_limit` measures each statement against SQLite's lowest limit of 999 (with batching removed it fails: `36000 <= 999`).
- [x] `tests/_fixtures.py::describe_stuck_runs`: each live worker's and session's state, what is recorded, the store's write times with any write in flight, and all thread stacks (`faulthandler`). It goes into both teardown errors, and `pytest_runtest_makereport` adds it as a report section to any test that fails with a worker running, whichever wait helper it used. `test_a_stuck_worker_explains_itself` covers it, and a 400 ms write delay was seen producing the section.
- [x] No timeout widened. The test's 10 s wait stays and can still fail on a stall like this one, which then says so.
- [x] Run-level abort ends `ABORTED` at a pause, with `prompt_choice` NULL. `test_an_abort_during_a_pause_ends_it_aborted_with_no_choice_recorded`, `test_the_prompts_own_abort_still_ends_stopped_on_fail`, `test_the_report_shows_no_choice_for_a_pause_an_abort_ended`.
- [x] A choice after an abort is dropped, whatever the delay, and both screens cope. `test_a_choice_after_an_abort_is_dropped_whenever_it_comes` (0 to 100 ms), `test_the_screen_takes_a_click_and_an_abort_in_either_order`.
- [x] No per-row commits on an abort or failure path. `_record_pretest_failure` and the boundary abort's headers and disabled steps go through `_record_unrun`, and `insert_log_lines` is batched. `test_a_failed_pretest_action_costs_a_few_commits_however_many_steps_there_are`, `test_an_abort_before_many_disabled_steps_costs_a_few_commits`, `test_a_big_batch_of_log_lines_is_one_commit_a_batch_and_keeps_its_order`. Still one commit per row, bounded by the schedule's files and Actions and not by its steps: `_record_files`, `insert_action`, `_flush_equipment`.
- [x] An abort ends a Device call on every Device Type (`devices/abort.py`, `tests/test_device_abort_32b.py`). The table below lists what stays behind.
- [x] `describe_stuck_runs` and the report hook cannot replace the error they explain. `test_the_report_hook_adds_its_section_and_never_replaces_the_failure` drives the real hook in a pytest subprocess.

**Decision (orchestrator, 2026-11-01): End Reason of an abort at a pause.** A run-level abort (`request_abort`) ends `ABORTED` wherever it lands, so a 50 ms race no longer decides a value that History filters on. The failed step finished before the abort, so it keeps its `FAIL` or `ERROR`, gets no "Aborted by Operator" Attempt, and its `prompt_choice` stays NULL because nobody chose. The report prints a choice only beside the Attempts of a retried step, so it prints none here; History shows no choice at all. The prompt's own Abort still ends `STOPPED_ON_FAIL` (packet 12b). `stop_and_wait` now only sets the flag, so a session closed at a pause also ends `ABORTED`.

**Still `STOPPED_ON_FAIL`: the header Abort with a pause pending.** `_request_abort` answers the prompt when `pause_pending()` is true, which 25b's tests pin, so the same button gives `ABORTED` before the pause and `STOPPED_ON_FAIL` at it. With the prompt on screen that is the Operator answering it. There is a short window where the pause is pending and its queued `failure_prompt` has not been painted. It was not changed, since the brief keeps the prompt's Abort and asked only to fix the comment in `test_failure_bar`.

**A choice after an abort.** `choose` returns None once the flag is set, so the order of the two decides, not the worker's 50 ms poll. A screen's click clears its own prompt first, then the worker's `prompt_answered` (choice `""`) clears every layout.

**What an abort cannot interrupt (for the user).** The worker no longer waits for any of the Device calls below: it raises "aborted" and moves on. What is listed keeps running on a daemon thread and holds its transport until it ends. A Post-test Action on the same Device waits for it (a second abort ends that wait), a late close happens once it ends, and a new run opening the same instrument meanwhile may find it busy.

| Call | Why | The worker is free after | The call itself can last |
|---|---|---|---|
| TCP connect, DNS (`ATE.open` on TCP, UDP, SERIAL `socket://`, VISA) | in the OS | 50 ms | the `timeout` given, or the OS's own limit for DNS and connect |
| `viRead`, `write_raw` on anything but pyvisa-py's SOCKET session (NI-VISA, GPIB, USB, VXI-11) | in C; repeating shorter reads would poll the instrument, which packet 19 forbids | 50 ms | the call's `timeout` |
| pyserial write | a timeout does not say how much went out | 50 ms | the `timeout`; a real port takes `cancel_write`, `socket://` has none |
| `ATE.close` on a stuck port or session | in the OS or C (Linux can hold a tty close while output drains, by default up to 30 s) | 50 ms | as long as the OS holds it |
| Closes at an aborted run's end | as above | at most 3 s in total (`_ABORT_CLOSE_SECONDS`), with a WARNING naming the Device | as above |

Waits ATE owns stop within about 50 ms: stream and UDP reads, TCP and UDP writes, and pyvisa-py SOCKET reads and writes. `ATE.flush` does not block, and on VISA is a no-op. `ATE.input` and `ATE.sleep` already gave way.

Also still blocking, with no bound possible:

- A SQLite commit in progress (the stall this packet is about).
- One Lua C library call, such as `string.rep` or a pattern match on a huge string, since the count hook fires only between VM instructions.
- `resource.timeout = ...` and `set_visa_attribute` on the worker, which are local attribute calls and should not reach the bus.

Post-test Actions run after an abort by design (story 84), and a second abort ends each one, so a long one extends a run's end past 10 s until then.

**Also seen, not changed.** `_selftest_checks` in `cli.py` closes pyvisa's shared `ResourceManager`. Run in-process after a Device open, it breaks the cached manager in `visa._manager`, which is how a new test first turned 30 VISA tests red. The CLI exits after its self-test, so only tests can hit it; `test_device_abort_32b` keeps its manager out of the cache.

**Two test races fixed on the way, both older than this packet, each seen once in 10 runs under load.**

- Two VISA tests read `peer.received` straight after the run, but the peer records it only when it sees the connection end (`KeyError`). The TCP tests already wait with `wait_for_disconnect`, and these two now do.
- `LoopbackPeer` stopped reading once `stop()` was called, so a handler thread starved until then recorded `b''` for bytes the device had sent (`test_lua_write_sends_raw_0xFF`). It now reads to the connection's end.

**For the user: `PRAGMA synchronous`, left as it is.** See "Left open" above. `NORMAL` would remove the sync from most commits, so a stalled disk would delay runs and aborts far less. Its price is that a power cut can lose the last commits of a record that is otherwise crash-safe.

**Rework round (2026-11-01), eight items.** Each fix has a test that failed before it, where a test can show it.

- [x] `devices/abort.py`: `_Call._run` catches `BaseException`, so a helper killed by anything else hands it to the worker, and `run_abortable` leaves the wait when the helper thread is dead with nothing recorded. Before: `run_abortable never returned: the helper died on a BaseException with no outcome` (the leaked spinner also slowed that test run to 148 s for two tests). `test_a_helper_thread_dying_on_a_base_exception_reaches_the_worker`, `test_a_helper_thread_that_records_no_outcome_is_not_waited_on`.
- [x] `storage/store.py`: `insert_log_lines` advances the run's log counter with each chunk that commits. Before, the next write after a half-written batch failed with `UNIQUE constraint failed: log_line.run_uuid, log_line.seq`. `test_a_log_batch_failing_part_way_through_keeps_the_log_counter_true`.
- [x] `devices/visa.py`: `read` waits for a call left behind before it sets the terminator, as `_visa_read` and `write` already do. Before, the attribute calls came during the abandoned read: `['attr', 'attr', 'attr', 'read', 'attr', 'attr']`. `test_a_read_after_an_abandoned_read_waits_for_it`.
- [x] `spec.md` line 209 now separates the two aborts at a pause: a run-level `request_abort` ends `ABORTED` with no `prompt_choice`; an Abort answering the prompt, header button included, is a choice and ends `STOPPED_ON_FAIL`. Spec sentence only; `tests/test_failure_bar.py` pins both (7 passed).
- [x] `engine/run_engine.py`: `_close_open_devices` and `_end_unexpectedly` say plainly that only an unbounded end closes every Device before FINISHED, and that an aborted run can leave a close running past `_ABORT_CLOSE_SECONDS`. Such a Device now logs one WARNING, not two. `test_an_aborted_run_names_a_device_still_closing_once_and_closes_it_afterwards` failed before with both lines.
- [x] `devices/registry.py`: an open or a close the abort left behind is traced, `open <device> aborted (ms)` / `close <device> aborted (ms)`, added to `DeviceTracer.aborted`. Before, the trace list was empty. `test_an_open_or_close_left_behind_by_an_abort_leaves_a_trace_line`.
- [x] `tests/test_device_abort_32b.py` measures every wait with `wait_for_aborted_run`: fails past 10 s, warns past 0.5 s, prints the timeline. The second-abort test asserted `not actions or ...`, which passed on no rows; it now requires a Post-test Action row with outcome `aborted`. Four tests warn past 0.5 s here, three at about 0.55 s and the simulated stuck close at 3.0 s, which is the close bound, and the timeline says so.
- [x] Small: the stale `_request_abort` comment in `staged_run_screen.py` (the paused worker no longer hangs, packet 32b); `_rows_per_statement(table)` computes the chunk size from the column count instead of the 80 chosen for `node`'s twelve columns (`test_a_row_batch_is_sized_to_the_table_it_writes`); `close_after`'s daemon-thread `join()` carries no bound and says why.

Helper-thread cost, measured by a reviewer at 146 µs per serial write, is accepted.

- [x] The five files this round touched (`test_device_abort_32b`, `test_windows_run_hang_32b`, `test_unexpected_run_end`, `test_failure_bar`, `test_device_tracing_17c`) pass 10 times under load (6 busy loops on 3 cores): `80 passed, 6 warnings` every run, 56–65 s. Full suite three times: `1519 passed, 4 skipped, 4 warnings` (295–301 s), against `1512 passed, 4 skipped` before the round.

- [x] New tests, the Device tests and the sixty-step test pass 10 times under load (6 busy loops on 3 cores); the slowest abort there, excluding the simulated stuck close, took 1.7 s, on a 200 MB write, against 0.5 s unloaded. Full suite `1512 passed, 4 skipped`.

Unproven until Windows CI runs: that the failure stays away, and what caused the stall. If it returns, the report's `store writes` line separates the readings: long or in-flight writes mean the disk, normal ones with the worker's stack elsewhere mean a fault this packet did not find.

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes.
