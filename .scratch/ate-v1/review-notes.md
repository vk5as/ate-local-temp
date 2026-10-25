# Review notes for the final code review

Gaps spotted by the orchestrator when merging; the end-of-spec code review should check them.

- 08: `log_line` rows are inserted once per step, not batched "about once a second"; ATE-sourced lines are only the NOREPORT warning; `str(level)` on a Lua `Log` enum value may not yield `INFO` etc. once real Lua runs.
- 09: `_location()` derives `<file>:<line>` by text-searching the schedule source for `:setAction(`/`:setPreTestAction(` rather than reading the real call site (e.g. `debug.getinfo` from Lua); wrong for multi-line actions, helper modules and sub-schedules. Load-time gating only covers `report`/`input`, not "anything other than builders, require, ATE.log, enums".
- 13a: abort is only observed at step boundaries, so the step that was running finishes with its own result and the *next* step records `ERROR` "Aborted by Operator"; 13b/13c must make the running step itself record the abort.
- 14a: adds the `action` table without bumping `SCHEMA_VERSION` (still 2), so an existing v2 database never gets it via `ate db migrate`; settle how schema versions move before release (e.g. squash to one v1 schema).
- 12a: `HeadlessRunSession.select_schedule` reports an unreadable schedule with `print` instead of the application log, and `_pending_stop_on_fail` (set via `getattr`) is never reset between runs.
- 12b: the failure pause is keyed to `attempt_no` 1 throughout (`_pause_for_operator`, `failure_prompt.emit(node_seq, 1)`); 12c must carry the real Attempt number.
- 13c: the abort prelude registers `aborted` as a global Lua function, so schedules can see and shadow it; keep it in a local upvalue instead. The 13c tests check End Reason and NOTRUN but not the "Aborted by Operator" message.
- 14c: `test_remaining_posttest_actions_still_run_after_one_aborted` drives `_record_phase_action` with Python closures, not through `run()`, so it can't catch the flag carrying over between actions (orchestrator fixed that in run()); revisit once 15e allows several Post-test Actions.
- 06b: `run_file` rows and mismatch WARNINGs are written once at run end, not as each file loads (the record doc wants incremental writes, and an interrupted run loses them); files are matched to the Test Definition by unresolved `path / rel_path`, so `..` paths may not match.
- 21a/22a: page-furniture strings (organisation, doc fields, UUT) are interpolated into CSS `content:` inside <style> with HTML autoescape, so a quote or & shows as an entity; escape them for CSS instead.
- Screenshots (2026-10-24, `/tmp/ate-shots/png/`): `HeadlessRunSession.start` hard-codes `utc_offset="+00:00"` (`headless.py` ~394), so reports show UTC as local time on a +10:30 machine; report Cover shows Duration "—" with both times set and ATE version blank; empty doc fields render a stray "· · rev" page header; stage-1 fields have no labels (placeholder text only); the Run view's step list doesn't list a step until it completes, so a step blocked on an input isn't visible.
- UI vs prototype (user, 2026-10-24): the implemented staged Run screen looks nothing like the HTML mockup it was meant to follow — `prototypes/run-screen/run-screen-prototype.html` on branch `prototype/run-screen` (variant C = staged, variant B = three-pane), as specified in `.scratch/ate-v1-wayfinding/assets/run-screen.md` lines 20-46. Today's screen is a plain vertical stack of unlabelled fields, list widgets and buttons (see `/tmp/ate-shots/png/`). Rework the staged layout to match variant C before release, and build 25a (three-pane) against variant B; future UI packets (10a, 11b, 12d, 14d, 15d, 23b/23c, 25a/25b) should be briefed with the mockup as the visual reference.
- 23d: the History summary panel is a raw `key=value` dump of the run row in a short scroll strip (`/tmp/ate-shots/png/09-history-b-*.png`); lay it out properly with the UI rework.
- 26a: `ate setup --root DIR` writes a config whose database path is `<data>/ate.db`, which resolves to the platform data dir at runtime, not DIR/data — check `<data>` resolution matches what setup prepared (or write the resolved path) before packagers rely on --root.
- 26b: the Windows icacls path has only been checked as argument lists on Linux; verify on a real Windows runner (27c) that Users can write the DB/WAL/reports/logs and can't write config.json.

## Suite flakiness (seen during 16b, 2026-10-24)

Pre-existing, not 16b: roughly 1 full-suite run in 8 fails one Qt test, a different one each time. Seen: `test_headless_session.py::test_interrupted_run_recovery_is_logged_in_the_app_log`, `::test_stop_on_fail_records_the_schedule_default_when_unset` ("QProcess: Destroyed while process … still running", then "wrapped C/C++ object of type HeadlessRunSession has been deleted"), `test_history_session_23b.py::test_report_files_lists_written_reports_newest_first`. More frequent when several suites run at once (three reviewers). Likely PDF-helper QProcess lifetime at session teardown (22b/23b). Needs a fix packet before the final review.

## 16b notes from review (not fixed)

- A read or write after `close()` raises a raw `ValueError` from `select` on fd -1; surfaces as an isolated ATE error, not a result table. Packet 16c's "using a closed Device fails through onError" covers it.
- `LoopbackPeer(hangup="reset")` with a payload can lose unsent bytes to the RST; no test combines them.
- `test_write_that_cannot_drain_times_out_with_count_sent` assumes kernel socket buffers < 64 MB.
- `_hex_field` looks up `string.format` at call time, so a schedule that reassigns it breaks ATE.read/write param handling (self-inflicted).

## 16c notes from review (not fixed)

- **must-fix later, shared:** `_location` (`lua_host.py` ~819-850) reports the step's `:setAction(` line, not the line of the ATE call. The spec wants the call's own `file:line` (Errors section, `final_atp.tslua:42`). Affects every ATE error, predates 16c. The 16c tests now match `tslua:\d+:` with a comment; tighten them once fixed. Candidate follow-up packet.
- `ATE.read(nil, …)` (wrong device argument) is caught by `_isolate_device` and returns `false, "'NoneType' object has no attribute …"`, so the step can PASS. Inherited from 16a. A type check raising a clean "device must be an open Device" would fit the spec's "always raises".
- TCP `close` swallows `OSError`, so the close-failure-through-onError path is unreachable and untested.
- Bad params take the ticket 09/16a graceful `false, msg` path with an ERROR log, but spec line 60 says they "always raise". Decide at the final review.
- `test_failed_device_returns_buffered_data_before_failing_through_handler` relies on the reset error text containing "reset".

## 12c notes from review (not fixed)

- During a Stop-on-Fail pause the failing step has its attempt row but no node row yet (the node is written after the last Attempt). The run screen's "failed step highlighted during the pause" will need to read attempts, not nodes.
- A step calling `ATE.report` several times now records only the last report per Attempt (the old loop inserted one row per report, which would have hit the attempt primary key). Matches "final report values"; untested for multi-report steps.
- `wait_for_asks()` has no timeout: a retried Attempt that skips an input it declared hangs the worker (pre-existing for a single Attempt; Retry widens it).
- A POLL click made during the pause is cleared by the next Attempt's reset and never recorded.
- `_record_inputs` rebinds `attempt_no` from the GUI's click tuple; POLL rows trust the GUI's attempt number.
- `OperatorInput._clicks_log` / `poll_clicks()` annotations say 4-tuples; they are 5-tuples.
- Spec "the run is FAIL on any FAIL or ERROR" vs packet 12c "a retried step can make the run pass": implemented per the packet (the last Attempt counts). Confirm at final review.
- `test_retry_not_offered_when_stop_on_fail_off` sets the engine flag directly; the schedule's `:setStopOnFail(false)` path isn't exercised, and a regression would raise inside a Qt slot.
- Flake list: add `test_headless_session.py::test_run_end_writes_html_report` (seen once in 3 runs during the 12c light review).

## 12d notes from review (not fixed)

- `FailurePromptBar._on_choice` and the screen's `_request_abort` decide "prompt pending" with `failure_prompt_bar.isVisible()`. Works today (the screen is shown on STARTED), but if an ancestor were hidden during a prompt, header Abort would fall back to `request_abort()` and hang the paused worker. A `_prompt_pending` flag set in `show_prompt` and cleared on choice would not depend on widget state.
- A second header-Abort click after it answered a prompt calls `session.request_abort()` on the live engine; untested.
- `RunEngine.choose()` silently ignores a choice when nothing is pending; its docstring (and the session's) says it raises.
- Mockup polish still missing: the highlighted row's red left edge (`box-shadow: inset 4px 0 #c62828`), the amber `FAIL?` result chip while paused, the bar sits below the step list rather than under the header, and the old finished-run `failure_bar` QLabel leaves an empty 24px gap. Part of the general "UI doesn't match the mockup" rework.
- Flake list: add `test_tcp_read_write.py::test_read_other_failure_through_lua` (1 in 3 full-suite runs during the 12d light review; 0/30 alone). Likely reset-vs-data timing under load.

## 13b notes from review (not fixed)

- **Engine waits that ignore abort (follow-up):** `OperatorInput.wait_for_asks()` (operator_input.py ~73) and the Stop-on-Fail pause `ch.get()` in `_pause_for_operator` (run_engine.py ~186) block with no abort check. An Operator abort during a POLL step whose button is never clicked, or during a failure prompt, leaves the worker blocked (story 83 "at any time"). 12d routes the header Abort to `choose("ABORT")` while a prompt shows, but `RunEngine.request_abort()` itself still can't end a pause. Candidate small packet.
- `ATE.sleep` errors aren't wrapped by `_isolate`: they reach Lua as userdata without the `file:line` prefix ATE.report/ATE.input carry (16-lua-worker-windows refinement 2).
- `ATE.sleep` has no context check, so it's callable at load time; the spec says it "pauses the step".
- The sleep-abort test has no handshake (input has `input_asked`); with `pre_abort_s=0.1` the abort could land at the step boundary and pass without exercising the sleep.
- No test of `ATE.sleep` inside a Post-test Action (probes: works; a second abort ends only that action). Builder's `setPostTestAction` keeps one action per phase.
- `tests/test_abort_blocking.py` reads results through `store._conn` with raw SQL rather than `RunStore.nodes()/attempts()/actions()`.
- `run_engine.py` ~133 comment says blocking interruption "comes in packets 13b/13c" — stale.
- Pre-existing: an abort requested between one Post-test Action ending and the next `clear()` is dropped (run_engine.py ~715).
- The 0.5 s bound is loose against the prototype's 28–43 ms (Windows) / 1–12 ms (Linux); measured aborts take ~0.17–0.19 s.

## 17a notes from review (not fixed)

- "One WARNING per overflow" is implemented as one WARNING per `ATE.read` whose `take_overflow()` reports an overflow, with all bytes dropped since the previous read summed; an overflow with no later read logs nothing. Spec says "one WARNING per overflow" — confirm the reading.
- `flush` on a failed device returns the failure and keeps buffered bytes, while `read` returns them first; spec line 19 says flush "discards everything". Decide.
- `close()` doesn't notify `_cond`, so a read blocked in another thread waits out its deadline (only relevant if close runs concurrently with a read).
- `_teardown` / `_stop` are touched without the lock (ordered, GIL-atomic; no observed race in 300–400 cycle stress probes).
- `write` select on a closed fd can still raise a raw ValueError at device level; unreachable through the ATE API because `_do_write` checks `registry.is_open` and `close_device` removes the entry first.
- No test proves `devices.receiveBufferBytes` reaches a device through the headless session (config → session → engine → host → device).
- `Driver.accepts_receive_buffer_bytes` is a special-case opt-in; the default limit is hard-coded in four places.
- The reader polls `select` every 50 ms even when idle.
- Tests: the overflow-twice seam test assumes 160 bytes arrive in one `recv`; the join-timeout path is tested only with a stub; `test_take_overflow_holds_the_lock`'s in-lock check can pass vacuously.
- Suite now ~20 s (+~5 s for the receive-buffer tests). One light-review run had 3 Qt/timing failures (`test_failure_bar::test_retry_button…`, `test_headless_session::test_toggle_node_rejects_bad_types`, +1) while two saluki reviewers ran suites concurrently; 7/8 runs clean. Watch whether background reader threads make Qt tests flakier under load.

## 07a notes from review (not fixed)

- Range forms the spec doesn't name are unsupported or differ from npm: `1.x`/`^1.x`, `*`, `x`, `||` unions, hyphen ranges `1.2.3 - 2.3.4`, a `v` prefix, bare partial `1.2` (treated as exact 1.2.0; npm means `>=1.2.0 <1.3.0-0`), and `^0.0` (ours `<1.0.0`, npm `<0.1.0`). `<=1.2` / `>1.2` partial bounds: reviewers disagreed on npm parity (one test asserts `3.2.1` doesn't satisfy `<=3.2`, which differs from npm's `<3.3.0-0`). Decide whether the spec wants full npm syntax.
- Ambiguity is checked on version strings, so `1.0.0+a` and `1.0.0+b` (equal precedence) aren't flagged ambiguous.
- `_chain` holds direct dependencies only (pre-existing); module-collision checks don't see transitive ones.

## 11b notes from review (not fixed)

- Re-entering stage 3 calls `_apply_stop_on_fail(schedule default)`, overwriting the Operator's stop-on-fail choice, while step overrides now carry over; spec run-screen.md line 37 says options carry over. Pre-existing; make stop-on-fail carry over too.
- Selecting a different schedule clears step overrides (same schedule keeps them). Judgement call against "New run clears nothing" — confirm.
- After a run, selecting a schedule moves FINISHED → SELECTING and hides the Run view (results, Log, Summary); before, results stayed until the next Start.
- `select_schedule` during a run updates `_selected_name` without announcing state (API-only edge; the UI hides the list during a run).
- Header rows get checkboxes that do nothing; header cascade / tri-state is packet 15d.
- Locked boxes during a run were not screenshotted.
