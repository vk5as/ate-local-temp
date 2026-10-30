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
- Flake list: add `test_staged_run_screen.py::test_abort_button_between_steps_records_error_notrun_and_aborted` (1 in 4 full-suite runs after the 11b merge while 17b ran suites concurrently; 0/30 alone before and after 11b).

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

## 24a notes from review (not fixed)

- Owner filter can't express Sub-schedule headers yet (no header nodes are recorded); 24b's dialog must expand a header into its steps' keys, or the exporter needs a helper.
- Real runs still record `utc_offset="+00:00"` (`headless.py` ~369, known), so exported "local" times equal UTC until that is fixed.
- `schedule` is always the top-level schedule name; lines owned by a future Sub-schedule would show the wrong name.
- Tests cover only the +10:30 offset end to end, not the Text header's owners line, and not a run with no steps (probes showed all three correct).
- `r.attempt or 0` would print `Step 1.#0` if a step line lacked `attempt_no` (unreachable today).

## Test suite under load (seen 2026-10-25)

With another agent running pytest loops on the same machine, the Qt/timing seam tests fail often: integration itself failed 5/10 runs of headless_session + failure_bar + device_onerror under that load (0/10 when idle). The suite is sensitive to CPU contention — worth hardening (longer deadlines, event-driven waits) before CI.

## Shared test helpers that cause flakes (found by the 17b Opus rework, 2026-10-26)

- `LoopbackPeer(hangup="reset")` can send its RST before the client's `connect` returns, so `ATE.open` itself sometimes fails; tests using it in test_device_onerror / test_tcp_read_write / test_device_failures fail ~2 in 8 runs even on base. Fix: reset only after the device's first byte (the 17b tests now do this).
- `test_headless_session._wait_finished` pumps events a fixed 2000 times with no time bound, so it fails under load (4/10 on base when paired with TCP tests). Fix: a time-bounded wait (as done in test_step_enable_ui).
- Candidate small "test hardening" packet before the final review.

## 17b notes from review (not fixed)

- Log lines are now written at every drain point (each ATE call with new lines → one `insert_log_lines` call); 1000–2000 `ATE.log` calls cost ~+40–70 ms per run. Acceptable; batch if it ever matters.
- The wall-clock `at` uses `time.time() - time.monotonic()` recomputed at each drain; an NTP step between drains could make `at` non-monotonic in seq. Compute the offset once per run.
- Top-level `print` in the schedule body (outside any action) is never recorded (pre-existing).
- A failure while one device call is blocked (e.g. a read waiting out its timeout) is logged when that call returns.
- After a write timeout, the next write follows a truncated frame; worth a sentence in the spec.
- `ATE.input` drains before its blocking wait but not during it.
- The new closed-handle test still uses the shared `LoopbackPeer(hangup="reset")` (see the shared-helper flakes note).

## 24b notes from review (not fixed)

- `report_stem_and_directory` doesn't sanitise its stem or the `<UUT Type>` directory, so `report_files` globbing (and the report directory itself) may not match a sanitised report name for a serial/UUT Type with Windows-illegal characters. The log's default name is now sanitised like the report's.
- An INTERRUPTED run's default log name ends `_INTERRUPTED_log.csv` (matches the report writer; spec lists only PASS|FAIL).
- The Owner list is flat; Sub-schedule header owners aren't representable yet (no header nodes recorded).

## 31a — test-flake hardening (merged 3e9af9d, 2026-10-26)
- Light review: asserted the 7 trigger writes; `_hang_up` now always closes and records its socket even if `sendall` raises; fixed the `wait_for_disconnect` docstring. `test_later_read_write_failures_add_no_further_warning` now writes the trigger before reading (a read first waited out the reset grace, then the write met the reset). Device files 10/10 clean; full suite 448 passed ×3.
- Leftovers (Haiku notes): two device seam tests still use a fixed 150 ms `ATE.sleep`; the 1 s reset-grace fallback could mask a missing trigger byte (now covered by the asserted writes); a few pump loops still busy-wait.

## 18a — SERIAL Devices (merged 2026-10-26)
- Reviewed by Haiku only: the Strata review was cancelled when coder2 went down, and the user said to finish without it.
- Light review: box 3 was unticked although Status said resolved (tests use `socket://127.0.0.1`, kept: the peer binds IPv4 only); `test_data_that_arrives_before_the_read_is_buffered` couldn't fail (now the peer hangs up first; shown failing with `_append` disabled); the shared reader now fails the device on any untranslated exception instead of dying silently (new test, shown failing without it). Serial file 10/10 clean, once with `-W error::PytestUnhandledThreadExceptionWarning`; full suite 487 passed ×3.
- The implementer's reply said "first compaction, well under 150 steps"; collect.py shows 161 steps and 2 compactions, so the Opus-handoff rule was missed and misreported.
- Leftovers: `test_flush_empties_the_buffer` relies on `ATE.sleep(0.2)` for the payload to arrive (the driver-level flush test is deterministic); the PING wait in `test_open_with_every_field_then_write_and_read` is a hand-rolled sleep loop; on a write timeout `count` omits a partly sent 4 KiB piece (pyserial reports no progress; documented); `_recv` catches `AttributeError` (pyserial's closed-port symptom) which could mask a bug; `test_every_enum_value_maps_onto_pyserial` takes ~5 s; the suite went from ~27 s to ~41 s.

## 18b — UDP Devices (merged 2026-10-26)
- Two review rounds (Strata + Haiku). Round 1: an oversize datagram failed the whole device, NaN/inf timeouts got through, `UdpLoopbackPeer` never joined its drain thread, and the overflow seam relied on `ATE.sleep(0.4)` (failed 4/10 without it); fixed by a Strata rework (5 items). Round 2 found test-only gaps, fixed in the light review: the overflow seam couldn't tell drop-oldest from drop-newest, and its "8" could match the port; the refused-write seam raced the kernel's ICMP error (Strata: 2/20 failures) and is now a stand-in socket refusing exactly 3 sends; the not-listening seam's `ok + refused == 10` was always true. Each shown failing under a mutation; UDP file 20/20 clean; suite 525 passed ×3.
- Design decided by the orchestrator: bind localPort then connect; ECONNREFUSED is never a device failure (reader ignores it, a refused send fails only that call); a size-truncated read logs "datagram truncated" (device attribute `partial_read_is_truncation`).
- Strata misreported its step counts in both runs (claimed ~60 and ~66; actual 158 and 140, with 1 and 2 compactions).
- Leftovers: the oversize check runs before timeout validation (an oversize write with a NaN timeout returns an err instead of raising); TCP/SERIAL `_timeout` still accepts NaN/inf (an inf timeout would block a read forever) — fix in the shared stream base; `localPort` is only tested as 0 (a busy fixed port surfaces as an open error); `select` in `_send` sits outside the try (ValueError on a concurrently closed fd, pre-existing pattern); `_join_drain` asserts with a bare `assert`.

## 06d — bundled `ate.` modules (merged 2026-10-26)
- Reviewed by Haiku and Strata. Strata found that `pyproject.toml`'s package-data keys were bare dotted keys (`ate.report = …` since 06b, plus the new `ate.lua = …`): TOML reads them as a nested table, setuptools rejects that, and **no wheel has built since 06b**. Quoted in the light review; the wheel now ships `ate/lua/devices.lua` and the report template. The package-data test read only the source tree, so it now checks the parsed config. The traversal guard was untested (a `..` name loaded a planted file with the regex weakened, and all 7 tests still passed); a new test plants one and shows it never loads. Both new tests fail without their fix. Suite 533 passed (×2; one serial flake, see 31b).
- Strata's step count was close this time (claimed ~75, actual 96, 0 compactions).
- Leftovers: `_record_files` returns early with no Test Definition, so a definition-less run records no `ate-module` rows (production always has one); `store.run_files()` has no `ORDER BY seq`; `history_screen.py:283` does `f.path` on dict rows, so displaying a mismatch raises AttributeError (pre-existing, from f8287535): a real bug for 06c's mismatch UI; nothing pins that an `ate.` name never reaches the definition's module map (catalog rejects such names, so the map can't hold one); a stray import order (E402) and an unbalanced backtick in `lua_host.py`.
- New packet 31b for the serial test flakes.

## 16d — `ate.devices` builders (merged 2026-10-27)
- First Strata run on the tuned config (262k, compaction buffer 32k): 21 min, 87 steps, 0 compactions, 106k peak; accurate self-report. Both reviewers: MUST-FIX 0 (Strata ran mutations and 10 file runs).
- Light review: builder metatables locked (`__metatable = false`; Haiku and Strata both showed `getmetatable(t).__index` could rewrite setters for every later builder); added coverage for `setLocalPort`, for `Tcp` lacking `setBaud` and for `timeout` being a float (each was invisible to the suite under mutation); the serial setter test closes its device in a `finally`; docstring wording. Suite 545 passed ×3.
- Decisions: `Read()`/`Write(data)` carry no defaults (spec line 105 read as open-builder defaults; a per-call default would override the device's timeout); `Read`/`Write` setters are shared across types, so `setReadTerm` on a UDP read is accepted by the builder and rejected at the call (matches the drivers). `Visa(resource)` is packet 19.

## 31b — serial test flakes (merged 2026-10-27)
- Root cause (Strata, confirmed by its reviewer's probe): pyserial 3.5's `socket://` `open()` ends with `reset_input_buffer()`, so a `LoopbackPeer` payload sent on accept can be discarded before ATE's reader starts (probe: 3/200 lost idle, 20/200 under load; 0 when sent after open). Before: 4/30 serial-file runs failed alone, 20/20 had failures under load. Fix (tests only): replies sent after the open returns, every wait on real device state, no sleeps.
- Haiku must-fix (accepted in part): 7 serial seams moved from RunEngine to driving LuaHost directly, so SERIAL's device WARNING no longer went through the engine's drain. Light review: `LoopbackPeer(after_first_byte=True)` holds the payload until the device writes; the hang-up seam is back on RunEngine with it. The other seams stay two-step (they need a Python-side wait between steps, like 18b's UDP overflow seam; TCP covers the engine drain). The same race was in `test_device_builders`' serial test (3/10 under load) and is fixed the same way. Serial + builder files 20/20 under 4 CPU hogs; reviewer: serial file 30/30 alone and under load, full suite 10/10; suite 545 ×3.
- Process: the implementer ended its turn with a 30-run load check still running in the background and the packet unticked (ticked in the light review). 60 min, 130 steps, 0 compactions, 170k peak (beyond the old 131k window).
- **Product note:** a real instrument that sends a banner the instant the port opens can lose it the same way. pyserial flushes input in `open()` on POSIX (`tcflush`) and for `socket://`, not on Windows; `serial.py` can't opt out short of bypassing pyserial's `open`. Risk is narrow (the banner usually follows the DTR/RTS handshake that precedes the flush). Worth a line in the user docs: devices should answer after the host's first write.
- Leftovers: `HANGUP_GRACE = 10.0` makes a payload-less "close" peer that is never sent to hold for 10 s; `LoopbackPeer.send()` swallows OSError and marks sent even with no connection; `_two_step` duplicates `_step`'s schedule text; the two-step harness reaches into `LuaHost` privates.

## 19 — VISA Devices (merged 2026-10-27)
- Strata implemented (37 min, 140 steps, 0 compactions, 186k peak). Round 1 (Strata + Haiku, both probing real pyvisa-py): `size` reads passed ATE's size as pyvisa's chunk size, so `read_raw` looped (501 viReads for size=4), overran the deadline (10.6 s vs 1 s) and discarded data on exact-size replies; pyvisa-py's socket write has no timeout (blocked >20 s); real open failures (bare `Exception`, `ValueError`) skipped onError and a refused port "opened"; a leftover buffer survived flush; per-call timeout unvalidated; failure paths covered only by fakes. Opus rework: byte-exact reads (`visalib.read`, or for pyvisa-py SOCKET sessions peek-and-take on the session socket), `_leftover` removed, write sent on the session socket with ATE's deadline, every open failure → `OSError` (half-open resource closed), refused port caught via `SO_ERROR`, per-call timeout validated; real-backend tests run on both read paths.
- Round 2: the code held up under probes; both reviewers found mechanisms no test could see (terminator mid-segment, pyvisa-py's `_pending_buffer` first, socket timeout restore). Light review added those three tests (each fails under its mutation) and made read/write turn any other backend exception into that call's err. VISA file 10/10; suite 588 ×3.
- **Coupling:** ATE reaches into pyvisa-py's `TCPIPSocketSession` (`.interface` socket, `._pending_buffer`). If pyvisa-py renames them, ATE silently falls back to `write_raw`, and pyvisa-py's own write has no timeout. Pin pyvisa-py's version, or add a startup check that warns when a SOCKET session doesn't match.
- Leftovers: a reset mid-reply drops the bytes already received (returns the error, not partial); a peer that hangs up with nothing sent gives `err = "connection closed by the instrument"`, not "timeout"; `size <= 0` isn't validated (shared READ_FIELDS; TCP behaves differently); the viRead path on SOCKET makes one extra read for terminator-less replies; the `write_raw` (NI-VISA) path has no real-backend test here; the manager cache has no lock; a broken NI-VISA install falls back to pyvisa-py silently.

## 06c — mismatch flag, run-start confirmation, storeMismatchedFiles (merged 2026-10-27)
- Strata implemented (23 min, 115 steps, 0 compactions). Haiku reviewed with screenshots. The Strata review was cancelled at ~38 min when the user shut coder2 down; its findings were read from its thread (second half, all conclusions and mutation outputs).
- Light review: the flag was release-wide (a schedule with no mismatch of its own got ⚠ and, under `ask`, couldn't start without a choice). It is now scoped to the schedule's own `.tslua` plus the release's `.lua` modules (which modules a run `require`s is only known at run time, so all of them count). Mockup fixes: amber `.flag` box on the flag line and dialog list, release as `MR · name version`, abbreviated hashes with full ones in a tooltip, UUT/Project/Operator line, default Start, minimum width. New tests: sibling scope and loaded-bytes-not-re-read (both fail under mutation). Suite 609 ×3.
- Verified (Strata's probes): stored bytes are the loaded bytes; `ask`/`always`/`never` matrix; invalid setting rejected at config read; Cancel starts nothing; History fix (the old code raised).
- Leftovers: `LuaHost._file_bytes` keeps every loaded file's bytes, not only mismatched ones (memory); a `.lua` listed but never required is still flagged (pre-run scope can't know); `require` can't reach a dependency release's modules (`module_map` covers only the definition's own files; 07 territory); `_record_files` has a stale 3-tuple type annotation; with `always`/`never` the mockup shows a checkbox but the spec (and this code) shows none; the schedule list row reads `name — description`, not the mockup's `name [release]` (pre-existing).

## 07b — transitive require chain (merged 2026-10-27)
- Strata implemented (16 min, 64 steps, 0 compactions). Both reviewers probed real `.atedef` trees and agreed: chain order is depth-first pre-order with a release reached twice keeping its first position (a last-wins mutation fails 2 tests), cycles terminate, every production `module_map` caller gets a resolved definition, and `run_file` rows carry the owning release.
- Must-fixes (light review): `definition_mismatches` matched the run's schedule by rel path alone, so a dependency's mismatched `main.tslua` was flagged for the depending release's `main.tslua` (now the schedule must belong to the selected release); ancestors of a cycle or of a propagated break kept a chain, contradicting the docstring (now cleared for every broken release, once all breakage is known); the "dependency schedules are never modules" assertion couldn't fail because the catalog never gives a `.tslua` a module name (replaced by a hand-built chain that tests the guard). One new test per fix, each failing under its mutation. Suite 620 ×3.
- Confirmed for 07c (both reviewers probed it): the module-collision check is keyed per media number, so one release with two modules is flagged and a real cross-release clash isn't. Propagation of breaks is transitive through `_chain`, but releases broken by the collision loop are never propagated (the loop runs after propagation).
- Leftovers: `resolve_dependencies` re-resolves a release once per incoming edge (Fibonacci DAG n=24: 2 s; memoise); two releases listing the same rel path give two `run_file` rows that differ only in `media_number`, and the run-start warning and the report banner name only the path; a same-directory, same-rel-path file is attributed to the first chain member; absolute-path entries crash `catalog.py:390` (`str` has no `.exists`, pre-existing, untested) and would never match `_record_files`; seam tests build unresolved definitions, so they'd never catch a chain regression; the pre-run list includes chain modules the run never requires (same as 06c's scope note).

## 07c — dependency errors break the Test Definition (merged 2026-10-27)
- Strata implemented (41 steps, 0 compactions, its count). The collision check is now keyed by module name across chain members (the old one was inverted). Intrinsic breaks are found first, then "broken dependency" propagates to a fixpoint, naming the nearest broken member. The 07b search-order tests moved to hand-built chains, and each also asserts that its on-disk tree is now a collision. The reviewers' mutations confirm both pin the order.
- Reviewers: Haiku (MUST-FIX 1) fuzzed 3000 random graphs: no order dependence in broken flags, but the first error was overwritten in 239. Strata (MUST-FIX 3) agreed on the overwrite, and found that a structurally broken but installed dependency was reported as "unresolved" (the matching registry excluded broken releases) and that the Run screen never shows broken definitions at all.
- Light review: `_set_broken` keeps the first error, and `resolve` skips any already-broken release (the stale `broken_ids` snapshot predated the duplicate-mediaNumber loop and is gone). Broken releases with a readable name and version stay matchable, so dependents get "broken dependency". A chain already holding a broken release skips the collision check, so the message names the root cause. The collision-propagation test now pins its message. 4 new tests, each failing under its mutation; the first-error test needed a forced scan order (directory names) to hit the mid-resolve overwrite. Suite 635 ×3.
- **New packet 05b:** ticket 05's "picker shows broken definitions with their errors" was ticked, but `list_schedule_rows` is never called by the UI, so broken definitions and schedules simply vanish from the Run screen (spec `test-definition-format.md:103`, `run-screen.md:24`).
- Leftovers: messages (cycle names, partial-chain collision lists in cyclic graphs) depend on scan order, though flags don't; `_chain` is dead except for `test_catalog_semver_ranges.py`; the `is_schedule` guard in the collision loop is redundant (schedules never get a module name); `resolve_dependencies` still re-resolves per incoming edge (07b note).

## 07d — record the resolved release chain (merged 2026-10-27)
- Strata implemented (~13 min, ~50 steps by its count, 0 compactions). One `media_release` row per chain member, in chain order, each with its own `.atedef` SHA-256 (read once at run start); an unreadable `.atedef` gives a null checksum and one run-level WARNING, and the run continues. `media_releases()`/`run_files()` now `ORDER BY seq`. Strata itself reported that its ORDER BY test could not be made to fail.
- Both reviewers: MUST-FIX 1, the same one: that test can't fail. SQLite reads `(run_uuid, seq)` through the PK index, and config accepts only `sqlite` (`config.py:236`), so no supported backend can show it. Light review: test removed, ORDER BY kept as a contract for a future backend; the `.atedef` read catches any exception (it was `OSError`), so the run carries on as its comment claims; `_record_files` uses `_search_chain` like the new code. Mutations confirmed real: owner-only recording, reversed order, top-level hash for every member and a dropped warning each fail tests. Suite 639 ×3.
- Leftovers: a failure inserting a run-start row (DB error) escapes `run()`; the worker dies before `run_uuid_reported`, leaving a `test_run` with no end until the next start marks it INTERRUPTED (pre-existing for `test_run`; 07d adds one write per chain member). `atedef_sha256` is recorded but nothing compares it, so an `.atedef` edited after the scan records the new hash next to the scan's stale name and version. The spec's `test-run-record.md:68` doesn't mark `atedef_sha256` nullable. The report's run-log section is still a placeholder, so the warning shows only in the log export. `run_engine` imports the private `_search_chain`.

## 15a — Step Names and header rows (merged 2026-10-27)
- Strata implemented (~20 min, steps "not counted, roughly 50", 0 compactions). `StepParams()` takes an optional name. A step with `setInclude` is a header (`is_header`): it never runs, has no Attempt or Result, and isn't counted. Load errors cover `setInputs`/`setAction` with `setInclude`, `setPrefix` without it, an ordinary step without a number or an Action, a non-string name, and duplicate sibling Step Numbers (there was no Step Number duplicate check before; 15c extends it to the expanded tree). It went past the brief in one place: header `node` rows are recorded now (15b's checkbox), on the normal, abort and Pre-test-failure paths. The reviewers checked them and found them complete.
- Reviewers: Haiku MUST-FIX 1 (with screenshots), Strata MUST-FIX 2. Both: the one existing test the packet touched in `test_step_inputs.py` kept a bad-input schedule with no Action, so it failed on the new Action rule and its loose `"schedule.tslua:"` assertion passed without reaching input validation. Strata: `setStepNo("")` counted as a Step Number and slipped past the duplicate check.
- Light review: the bad-input test now has an Action and asserts the ENUM message. An empty or non-string Step Number is a load error. `setInclude(nil)` / a table / `""` is a load error, where before `nil` silently made an ordinary step, and `is_header` now means "setInclude was called". A header's `""` number is stored null. The paused-run list skips headers like the finished list does. New tests cover these, plus two ordinary duplicates and `setEnable`/`setPrefix` on a header; each new check fails under its mutation. Suite 669 ×3.
- Leftovers: a load error at run start escapes `RunEngine.run()` (the worker dies with no `test_run` row; same for every load error, no test); headers show a checkable row in the setup list that does nothing until 15d, with no header styling yet (mockup: grey band, bold name, Sub-schedule tag, caret, tri-state box; 15b/15d); `_StepParams.kind` is read only by a test; `instance_seq`/`parent_node_seq` are null on every node until 15b; a blank name leaves double spacing in rows (`'2.    →  PASS'`).

## 15b — setInclude and Sub-schedule instances (merged 2026-10-27)
- Strata implemented: 31 min, 151 steps (it said ~120), 0 compactions, 174k peak. It added include expansion at load (`host.tree`/`host.instances`), `catalog.include_map`, the engine walking the tree, `schedule_instance` (schema v4), include mismatches in the 06c pre-run scope, and an indented pre-run step list.
- Round 1:
  - Haiku (MUST-FIX 1, with screenshots): the finished Run view had no headers or indentation, so two channel instances read as duplicate rows.
  - Strata (MUST-FIX 3): every include shared one global environment, so globals leaked between siblings and parent, and an include could redefine `Result`/`ATE`. Resolution used the top-level release's map at every level, so a dependency's schedule including its own file came out "ambiguous". My brief was wrong there: I said own + dependency is ambiguous, but the spec has own-first like `require`. Also, two tests passed with keyvals cut off from the chunk.
- Opus rework (`f7d08c4`, effort high):
  - Per-instance `_ENV` via a pre-lockdown loader (`load(code, name, "t", env)`; reads fall through to the shared globals).
  - Read-only enums and Python API objects.
  - `catalog.IncludeMap`, resolving each include from the including file's own release first, then that release's dependencies; ambiguous only among dependencies.
  - Lua-visible tests, which exposed nested keyvals reaching Lua as userdata (fixed with recursive `table_from`).
  - Headers and indentation in the Run view and the paused list.
  - Clean errors for non-table or metatabled keyvals and for a bare-seam missing include.
  - Dead store code removed; schema v4 pinned, with a v3-migration test.
- Round 2 (Haiku, MUST-FIX 3, all through the `debug` library, which the lockdown never removed):
  - `debug.getregistry()` reached the instance loader and, through it, the stock `load`, so arbitrary unchecked source could run.
  - `debug.setmetatable(Result, nil)` undid the enum lock.
  - `getmetatable(_ENV).__index` handed out the shared globals, so isolation could be bypassed.
- Light review: schedules now get a `debug` table holding only `traceback` (lupa builds error tracebacks through the global `debug.traceback`; removing it outright broke 2 traceback tests), and the instance metatable has `__metatable = false`. A parametrised test replays each escape; each fails under its mutation. Suite 720 ×3.
- Leftovers:
  - A `require`d module runs in the shared table and can still reassign `ATE`/`Result` there (kept so `require` is unchanged).
  - Standard-library tables (`string`, `math`, ...) are writable from an include.
  - An error inside an included step reports the top-level file's line numbers (`_location()`).
  - The Summary tab lists steps without headers.
  - `definition_mismatches` reloads the include tree on every call, and flags nothing (not even the schedule's own file) when the schedule fails to load.
  - The depth-32 guard rejects legal acyclic trees deeper than 32 (15c replaces it with cycle detection).
  - `schedule_instance` text columns are nullable where the spec has them as text.
  - Header styling is 15d.

## 17c — device tracing (merged 2026-10-27)
- Strata implemented in parallel with the 15b Opus rework: 30 min, 149 steps, 0 compactions. `src/ate/devices/tracing.py` (`DeviceTracer`, `TracedDevice` proxy). The registry wraps handles when `deviceTrace.enabled` is set, and lines go out through `record_log(..., source="device")`, so owner and order match `ATE.log`. Run-end teardown now closes through the registry, so leftover closes are traced. `lua_host.py` changed by 10 lines and merged cleanly after 15b.
- Haiku (MUST-FIX 0) ran a real config through `HeadlessRunSession`: lines are in order, at DEBUG, owned by the attempt, and truncated on raw bytes. They appear in the Debug pane, not in the Log pane or the HTML report, and do appear in the CSV/JSONL exports. Strata (MUST-FIX 2): nothing pinned raw-byte truncation (an escaped-text cut passed every test), and nothing pinned the `\t` escape. Strata's probes also showed SERIAL and VISA traced correctly, with no regression with tracing off.
- Light review: one direct test of `quoted_bytes`, covering every simple escape plus ten control bytes capped at 8 (fails under both mutations). Removed the duplicate `__getattr__`. Suite 686 ×3, integrated 737.
- Leftovers:
  - Opens rejected with `ValueError` (bad params, unknown DeviceType) and non-OSError close failures aren't traced.
  - Two handles to the same address share one label.
  - `isinstance(handle, _StreamDevice)` is False through the proxy; nothing checks it today.
  - At run end the "left open" WARNING is stored before the close trace.
  - A failed write quotes the whole payload next to the sent count.
  - `maxBytes` isn't range-checked.
  - Tests use a fixed `ATE.sleep(0.3)` and two vacuous assertions.
  - Only TCP/UDP are covered by tests.

## 15c — prefixes, numbering and tree checks (merged 2026-10-27)
- Opus implemented, as the user's one-off (effort medium; it paused at the rate limit and resumed after the reset).
  - `setPrefix` stacks outside in on `host.tree`, so every view and `node.step_number` get the prefixed number.
  - A header takes the outer prefixes but not its own.
  - Duplicates are checked on the expanded tree, and each error names both include chains.
  - A cycle (the same resolved file twice on one chain) is caught before the chunk reloads, and the error names the chain.
  - The depth-32 limit is removed; a non-string prefix is a load error.
  - 12 tests. The 15b fixtures gained a `setPrefix` on every include, since 15c correctly rejects them otherwise; reviewers confirmed no 15b behaviour was hidden.
- Haiku (MUST-FIX 0, screenshots): the channel example reads `2.1.` / `3.2.`, errors are readable, the sibling schedule still loads, and the report shows `3.2.`. Strata (MUST-FIX 1): `_expand` recurses per level, so a chain of about 1000 levels raised `RecursionError` out of the scan and killed every schedule. It also confirmed the remaining behaviour by probe and that 8 mutations each fail tests.
- Light review: `RecursionError` from the expansion becomes the schedule's load error. Tested with a 150-level chain under a recursion limit just above the current stack depth; the test fails under mutation. Suite 733 ×3, integrated 750.
- Leftovers:
  - Nothing pins that a cycle is caught before the repeated chunk runs (correct by probe).
  - `schedule_instance.prefix` stores the raw per-include value; the spec's "as applied" could mean the stacked one.
  - Chain labels in errors use raw `file:number`, which is inconsistent about the trailing dot and doesn't match the prefixed numbers shown everywhere else, and nested messages run together without a separator.
  - The report assertion is weak.
  - There is no run-start cycle test.

## 15e — Pre/Post-test Action ordering across the tree (merged 2026-10-28)
- Strata implemented it with 11 tests and no existing tests changed. Pre-test runs in `host.instances` order (parent first, depth first); Post-test runs in exact reverse. An instance whose whole subtree is disabled is skipped (a schedule with zero steps is not). After a Pre-test failure, only instances the Pre-test pass reached get a Post-test. `action.instance_seq` is recorded, and `run_order` is the position in its phase.
- Haiku (MUST-FIX 0, screenshots) confirmed by probe the spec order, the disabled skip, the Pre-test failure path and a Post-test error. Strata (MUST-FIX 1): Post-test Actions got `passFail="PASS"` on runs that had already failed through a Pre-test failure or an abort, against spec line 21 ("the Test Run's overall result"). This predates 15e, but 15e's own tests pinned PASS. Strata also checked by mutation that no 15e test is vacuous, that `run_order` has no readers, and the abort, second-abort and stop-on-fail paths across the tree.
- Light review: a Pre-test failure, stop or abort sets `pass_fail` to FAIL before the Post-test loop. The Pre-test failure test now asserts every Post logs FAIL, and the abort test expects FAIL; both fail under mutation. Suite 761 ×3.
- Leftovers:
  - When every step in the run is disabled, the top-level schedule's own actions are skipped too (the spec names only Sub-schedules).
  - "Its steps are all disabled" is read as the whole subtree.
  - No 15e test covers an instance with no Pre-test Action reached before a failure; 14c tests pin it.
  - `outcome="skipped"` is never written: a skipped action has no row.
  - Text/CSV export labels every instance's action plainly "Pre-test" while the dialog numbers them.
  - The report names only the first errored action, and without its name.
  - A Pre-test-failed run's header reads "FINISHED".

## 15d — enabling under headers (merged 2026-10-28)
- Strata run 1 ended after 28 steps (7.7 min) when it tried to view a screenshot with a non-existent `browser.preview` tool: the malformed tool call ended the turn with nothing committed. Run 2 was told to check screenshots in code and finished: forced defaults carried down `host.tree` (innermost wins), a derived tri-state header box whose click sets every descendant step through `toggle_node`, `#eef1f5`/bold header rows, and an `N/M done, K failed` rollup. 10 tests; two 15b row-text assertions updated for the rollup.
- Haiku (MUST-FIX 1, screenshots from real clicks): a header over an empty Sub-schedule is drawn checked but recorded `enabled=0`, which a report would print as "disabled by Operator". Strata (MUST-FIX 1): clicking a nested header re-derived the headers below it but not the ones above, so the outer box went stale and the operator's next click on it did nothing. Strata also timed a 500-step header click at ~4 ms with no list rebuild, confirmed the list is locked during a run, and traced the large last RunView row to ticket 03 (`f23847b`).
- Light review: the header branch re-derives ancestor headers; a header with no Test Step beneath it records its default. Both have tests that fail under mutation. Suite 772 ×3.
- Leftovers:
  - NOTRUN counts as "done" in the rollup (as in the mockup).
  - A nested header's `default_enabled` ignores an enclosing force (its steps don't).
  - A partly-enabled header under a forced-off parent records `enabled=1`, which a report could call "enabled by Operator".
  - The paused rollup assumes `node_seq == row index`.
  - Mockup gaps: the rollup is not right-aligned, there is no "Sub-schedule" tag, "K failed" is not red, and disabled rows are not greyed or struck through.
  - The ticket-03 "latest step" large-font row lands on a trailing NOTRUN step and clashes with the bold headers (the mockup uses blue/red bands).
  - Tests use `setCheckState`, not clicks.
  - The PDF helper's callback can fire after the session is deleted (pre-existing).

## 20 — Equipment (merged 2026-10-28) — first Strata run at variant medium
- Strata (medium) implemented it in 24.9 min / 114 steps / 0 compactions (155k peak). `ATE.addEquipment` validates types (strings only, `name` required). A repeat of name+serial is ignored with a DEBUG line (source `ate`). A load-time call is a load error, like `ATE.report`/`ATE.input`. Rows are flushed through the engine's per-call drain, owned by the step Attempt or Action. Schema v5 adds the `equipment` table, with a v4→v5 migration test. 16 tests. Its report was accurate and complete.
- Haiku: MUST-FIX 0. Its probes confirmed owners, order, NULLs, the repeat DEBUG line, per-step ERRORs, and that History and the log export are unaffected. Strata: MUST-FIX 1. Removing the per-call drain left all tests green, so "recorded as it happens" was untested. Strata also confirmed by probe that rows survive a raising step and an abort, that the retried-step owner is right, and that the shared `_log_seq` has gaps but no collisions.
- Light review:
  - A mid-step test with a busy Lua loop and no further ATE calls (`ATE.sleep` would flush the row itself), which fails under the drain mutation.
  - `""` as name is "name is required".
  - A sixth argument gives a readable error, not Python's signature message.
  - No serial and `""` are the same Equipment.
  - The DEBUG text no longer prints `(serial )`.

  The new checks fail under their mutations. Suite 792 ×3.
- Leftovers: the ownership dict's `action_seq` holds the instance seq, not the `action` row's `seq` (same as `log_line` since 17b), so a report join on `action.seq` would mis-join. Probes in the review came up with PDF-helper teardown noise (pre-existing).

## 21b — report Steps section (merged 2026-10-28) — second Strata medium run, plus a medium rework
- Strata (medium) implemented it in 23 min / 109 steps / 0 compactions: Steps rows from the record, a spec-worded roll-up (`N steps: 3 PASS, 1 FAIL`), Attempt sub-rows, override labels, input values and print CSS, plus `RunStore.input_values()`. 7 tests.
- Haiku (MUST-FIX 2, PDF screenshots):
  - Autoescape was OFF: `select_autoescape(["html"])` doesn't match `report.html.j2`, so operator input and report messages rendered as markup. This dates from 21a and also affected the Cover.
  - The depth indent sat on the fixed-width Step Number cell, so names never moved.

  Strata (MUST-FIX 3):
  - The same autoescape bug.
  - Header rows never carried an override label. My brief said "steps only", but spec line 66 says "whenever `enabled` differs".
  - The print test was a substring grep that passed with the CSS removed; `<thead>` alone repeats the headings.
- Rework (Strata medium, on its own `t3/report-escape-indent-rework` branch because the packet branch was checked out elsewhere):
  - Autoescape on (`["html","j2"]`), with only the logo data URI and the margin CSS marked `Markup`.
  - Indent on the name cell (sub-rows one level deeper).
  - Header overrides follow the spec.
  - The print test parses `<thead>`/`<tbody>`; the inert `thead` CSS rule was dropped.
  - `input_values()` gets an `ORDER BY` (pinned by reversing it, since SQLite's PK order hides its removal).
  - CHECKBOX values print as `Green, Blue`.

  5 tests. Suite 804 ×3.
- Leftovers (pre-existing):
  - `.page-break` has no CSS rule, so no section starts a page.
  - The footer `run_ftr` is never in the context, so it's blank.
  - Cover Failures uses the first Attempt's message, Steps the last.
  - The page header reads `· · rev` with no doc title and doesn't fall back to the schedule name (21a).
  - `nodes()`/`attempts()` have no ORDER BY.
  - The PDF file is written 0600 while the HTML is 0664.
- Medium-trial note: the defects were real behaviour or spec gaps, not just test gaps, but 2 of the 3 distinct ones came from 21a or my brief. The rework was clean.

## 21c — report Equipment, Configuration, Run log (merged 2026-10-28) — third Strata medium run
- Strata (medium) implemented it in 27.7 min / 146 steps / 0 compactions: Equipment, Configuration (Media Releases filtered to releases that supplied a used file, Files with highlight and stored flag, Sub-schedule instances) and a grouped Run log (run, every Pre/Post-test Action, then each step Attempt; DEBUG dropped). `include_run_log=False` gives a one-line note. `log_export.owner_keys` was extracted so the report and the export share ownership. 11 tests. The user then reverted implementers to high.
- Haiku: MUST-FIX 0 (PDF screenshots). Layout notes: the Run log time column wrapped, each group's table had its own widths, headings were orphaned at page foot, and the pale mismatch tint was the only marker. Strata: MUST-FIX 2. With no step log lines, the "Pre-test Action failed … every step is NOTRUN" line fell outside both action windows and landed under Post-test in both the report and the export. `stored` used truthiness, so a zero-byte stored file said "no" while the Run log said it was stored.
- Light review:
  - The abort explanation is now written inside the failed Pre-test Action's window (`_record_phase_action`), so it is owned by and recorded with that Action.
  - In a run with no step lines, `owner_keys` separates Pre-test and Post-test by `seq` against the last Pre-test action row. Timestamps can't: both land in the same millisecond.
  - `stored` is now `content is not None`.
  - Layout: a CSS ⚠ marker on mismatch rows, fixed log table columns with a nowrap time column, and `break-after: avoid` on headings.

  Each fix has a test that fails under its mutation. Suite 818 ×3.
- Leftovers:
  - The Sub-schedule instances table includes the top-level instance. Spec text says "each instance"; the heading and glossary suggest inclusions only.
  - `phase_label` counts Actions, not groups with lines.
  - `_run_log_groups` drops a line whose owner key doesn't match (not reproduced).
  - `.page-break` still has no rule.
  - The Cover's Media Release and Schedule-file rows are blank (21a: `schedule_file: None`, `media_name/version` None).
  - The `test_report_21a.py` docstring is stale.

## 05b — broken rows on the Run screen (merged 2026-10-28)
- Strata (high) implemented it: the picker uses `list_schedule_rows`. Broken definitions and schedules show as `setItemWidget` rows (name, `MR · name version`, error in `#b71c1c`, on `#fff5f5`), are unselectable and disabled, and Start refuses them. `ScheduleRow` gained `release` and `file`. It added a filter box next to Rescan, which the staged screen lacked. 8 tests.
- Haiku (MUST-FIX 1, screenshots; real clicks and keys never start a broken row): no not-allowed cursor. Strata (MUST-FIX 4):
  - every Rescan leaked the row widgets (+59 MB for 50 rows × 20);
  - the start guard was untested;
  - the error label didn't wrap;
  - no not-allowed cursor.

  Notes: the raw traceback (with ATE's own `instance-env` frames and absolute paths) was shown as the error; stale notes stayed when the pick was hidden; the stage-2 Start could raise `SessionError`.
- Rework (Strata high, on `packet-05b-broken-rows-t3`): all of the above fixed and tested — widgets freed, the guard tested, word wrap, `ForbiddenCursor`, a one-line error with the full text in the tooltip, stale notes cleared via `session.clear_selection()`, stage-2 Start guarded. 17 tests.
- Light review: the 640 px screenshot still clipped the last wrapped line. The size hint hand-counted lines, and was fixed at insert time before the list was laid out, so the rework's own test compared the hint to itself. Rows are now sized from the row layout's `heightForWidth` and re-sized by a viewport resize filter. The test compares the item rect with the layout's needed height, and fails under the old calculation. Suite 835 ×3.
- Leftovers: usable rows show no release line (the mockup has one; 25a); broken rows sit after usable ones rather than in scan order; the filter box predates 25a's three-pane filter, so 25a should reuse or replace it; `list_schedules` is used only by tests now; `_release_label` duplicates the inline release-label code; the screenshot test only checks colours, it doesn't compare against the mockup.

## 10a — Step Input widgets (merged 2026-10-29)
- Implementer (Strata, high) 157 steps, 42 min. Reviews: Haiku 5 must-fix, Strata 6 must-fix (union 9).
  - Defects: line edit never in the layout; radios/checks not in `controls`; row text drawn twice; widgets deleted at FINISHED; SIGSEGV on a Stop-on-Fail pause with input rows (Qt 6 `QListWidget.clear()` destroys item widgets); POLL disabled after one click; all inputs live at step start; an unclicked POLL hung the run; `supply_input`/`click_poll` accepted anything.
  - The implementer's tests were vacuous (looped over the incomplete `controls`; typed into a hidden widget).
- Rework (Strata, high, 2 compactions) fixed all 9, each mutation-proven, plus the SHOULD items (content-sized buttons, 26 px indent, `scrollToItem`).
- Light review: 853 passed ×3; collection 835 → 853, none lost; screenshots /tmp/10a-r2-shots OK.
- Leftovers:
  - After FINISHED the setup stages re-show and squeeze the step list to ~68 px (pre-existing).
  - Pre-existing teardown abort in single-file runs (`pdf_job.py` QProcess destroyed while running, exit 134).
- Concurrency note: 14d/28a/29a ran as 3 parallel Strata implementers during the 10a review (4 sessions on 1 slot), giving 1,483 out-tok/min total vs ~2,400 solo. Avoid unless the orchestrator is unavailable.

## 14d — Run status bar (merged 2026-10-29)
- Implementer (Strata, high), 83 steps, 101 min; it ran slowly as 1 of 4 parallel sessions.
- Reviews:
  - Haiku: 5 must-fix (1 rejected).
  - Strata: 3 must-fix, the same as Haiku's. It refuted Haiku's "the live QTimer is unproven".
  - Kept:
    - the bar was at the top (the spec says bottom);
    - no Retry test;
    - the Pre-test/Post-test totals were untested with more than one Action per phase.
  - Rejected: freezing the timers during a pause. That came from loose wording in my brief; the elapsed time is the point of the bar.
- Rework (Strata, high):
  - merged integration (3 conflicts in `staged_run_screen.py` with 10a, both sides kept);
  - moved the bar to the bottom, showing the Pre-test/Steps/Post-test tallies side by side and a "Paused" state;
  - added tests for layout order, Retry, Sub-schedule totals, the pause and the live QTimer.
  - Its final reply cut off before the 3 suite runs, so I ran them.
- Light review: 864 passed ×3 (~89 s, up from ~82 s); collection 853 → 864, none lost; screenshots in /tmp/14d-r2-shots OK.
- Leftovers: during a pause, "Current" shows the failed step's time and keeps counting (by design).

## 28a — Wheel and source build (merged 2026-10-29)
- Implementer (Strata, high): 86 steps, 102 min, 1 of 4 parallel sessions.
- Reviews: Haiku 5 must-fix; Strata 4 must-fix, all of them also in Haiku's list. The four:
  - `pyvisa>=1.22` doesn't exist (1.16.2 is the newest);
  - the test needs setuptools, which a fresh venv lacks;
  - a stale `src/ate.egg-info` makes setuptools ship unlisted data files, so the resource test could pass on a broken wheel;
  - the docs claimed bare `ate` opens the GUI.
- **Gap found:** no packet launches the GUI at all; only the PDF helper creates a QApplication. Added packet 30a (GUI launch: main window, launch checks), and 25b now waits on it.
- Rework (Strata, high, 0 compactions):
  - floors are checked against the installed versions;
  - `[test]` extra gains setuptools and build;
  - the wheel builds from a clean temp copy, and the repo's `build/` and egg-info are never touched;
  - the docs are honest;
  - the package-data key is tied to `ate`;
  - the network probe runs inside the test;
  - a pip error fails the install test instead of skipping it.
  - Each fix is mutation-proven.
- Light review: collection 864 → 878, none lost.
  - Suite: 1 failed + 877 passed on run 1 of 3. 7 later full runs and 8 runs of the 28a files all passed, and the failing test was not captured. It is likely a pre-existing flake; watch for it.
  - Suite time is now ~95 s (was ~89 s; the wheel builds add ~6 s).
- Leftovers: `ate --help` exits 1 and `ate setup --help` runs setup (pre-existing CLI issue). Fold this into 30a.

## 29a — Example Media Release scaffold and harness (merged 2026-10-29)
- Implementer (Strata, high): 88 steps, 94 min, 1 of 4 parallel sessions.
- Reviews: Haiku 1 must-fix; Strata 6 must-fix. The examples flushed out two real engine bugs:
  - reported values were stored with Python `str()` instead of Lua `tostring` ("True" vs "true"; float repr);
  - a step that *reports* `Result.ERROR` never paused under Stop on Fail.
  - The other four:
    - an auto-CONTINUE harness that couldn't prove "no pause";
    - a false "range-checked" comment;
    - no `.gitattributes`, so CRLF on Windows would break the checksums;
    - polling of the private `session._state`.
- Rework (Strata, high, 0 compactions):
  - merged integration and adapted the harness to 10a's ask-time inputs;
  - one Lua-side `to_text` path now serves reports, log args and input values;
  - the engine pauses on a reported ERROR;
  - added `.gitattributes` (LF for .tslua/.lua/.atedef);
  - the harness has scripted prompt choices, asserts the report files arrive, uses a scoped PDF stub, reads more `ATE.run` fields and includes an Operator-enabled run;
  - `headless.py` now passes the Media Release number to `ATE.run`.
  - Each fix is mutation-proven.
- Light review: 893 passed ×3 (~100 s); collection 878 → 893, none lost; `python -m examples.harness.run_examples` ran 5 runs and exited 0.
- Leftovers: the harness imports `tests._fixtures` (fine from a source checkout). The suite has grown to ~100 s.

## 30a — GUI launch (merged 2026-10-29)
- New packet, created after the 28a review found that nothing launches the GUI.
- Implementer (Strata, high): 0 compactions, 909 passed.
- Reviews: Haiku 3 must-fix, Strata 2 must-fix, 5 distinct in all:
  - `QMessageBox(app)` crashed the real missing-config dialog;
  - the QApplication was a local, so the window died with `exec_=False` (pytest-qt masked it);
  - the config was read twice;
  - one launch could write two app logs;
  - existing subcommands rejected stray args after the argparse rewrite.
- Rework 1 (Strata, high, 0 compactions) fixed those 5 plus 10 SHOULD items:
  - `db migrate --yes` with no path;
  - the refusal is logged and tested;
  - History "New run" now prefills the Run screen;
  - `history_screen.set_run_in_progress`;
  - close-during-run asks "Abort it and quit?" and releases the session, store and log;
  - `_pdf` is hidden from help;
  - one clear Abort message;
  - the docstring;
  - **the first-stage fields are labelled** (the pre-existing packet 03 gap, seen in screenshots);
  - run teardown in the tests.
- Rework 2 (Strata): the new labels showed the Project field prefilled with the **Station name**, a pre-existing bug.
  - Project now comes from `defaultProject`.
  - An empty Project blocks Start (UI and `session.start()`).
  - History "New run" uses the run's recorded project.
  - Fixture configs gained `defaultProject`.
- Light review: 931 passed ×3 (~103 s). Collection 893 → 931; one test was renamed (station_name → default_project), nothing lost. Screenshots /tmp/30a-r2-shots and /tmp/30a-r3-shots OK.
- Leftovers:
  - The first-stage form is compact but the window below it is empty. The mockup's setup layout is fuller; revisit in 25a/25b if wanted.
  - A fresh `ate setup` config has `defaultProject: ""`, so Operators must type a Project (per spec).

## 25a — Three-pane Run screen (merged 2026-10-29)
- Implementer (Strata, high), 1 compaction. Extracted shared widgets into `src/ate/ui/run_widgets.py` and added `three_pane_run_screen.py`.
- Reviews: Haiku 6 must-fix, Strata 4 must-fix (union 8).
  - **`database is locked` crash on long GUI runs** (Haiku, 5 of 6 runs).
  - Inputs stay live after Abort; OK then raised an uncaught SessionError → SIGABRT.
  - Ticking a step ON was a no-op (bool vs CheckState).
  - Re-picking a schedule mid-run reloaded the table and wedged the run.
  - Log pane not live.
  - Counts stale on a second run.
  - Vertical header numbered input/prompt rows.
  - A mismatch note without word wrap squeezed the centre pane.
- **Root cause of the lock, a latent app-wide bug:** `RunStore._run` returned an open SQLAlchemy `Result` after its connection went back to the pool. The unfinalised read then collided with the worker's INSERT on the same pooled sqlite connection, giving an immediate BUSY that `busy_timeout` can't clear. The staged screen was affected identically. Fixed by materialising rows inside the context; a 60-step run through both layouts is the regression test.
- Rework (Strata, high). Its first turn ended mid-work with uncommitted edits; I resumed it with `t3_thread_send`.
  - Fixed all 8, plus: blank Attempts for unrun steps; `RunScreenMixin` de-duplicates the two layouts; the staged-screen teardown abort is fixed (wait for `report_written`).
- Light review: 954 passed ×3 (~115 s); collection 931 → 954, none lost; screenshots /tmp/25a-r2-shots OK.
- Leftovers:
  - The suite is now ~115 s (it was ~80 s at 10a). Consider a speed pass.
  - The step table's columns overflow below ~1000 px window width.

## 25b — Layout setting and switching (merged 2026-10-30)
- Implementer (Strata, high, 0 compactions). Both layouts live in a QStackedWidget, bound to one session, with an `is_active()` flag.
- Reviews: Haiku 3 must-fix, Strata 2 must-fix (union 4):
  - the hidden layout was active at launch (it could answer inputs and start/abort runs);
  - a switch after FINISHED wiped the three-pane results;
  - pause state wasn't reconciled across layouts (stale prompt; the first Abort was a no-op);
  - no focus on the waiting input after a switch.
- Rework (Strata, high, 1 compaction). Its turn ended twice early (a malformed tool call), and I resumed it with `t3_thread_send` both times.
  - Fixed all 4, plus: guards on every hidden-screen action; deferred store reads on the hidden screen; a staged "(N Attempts)" badge; a new engine `prompt_answered` signal; `session.pause_pending()`.
  - **Teardown-crash root cause (the flake since 10a):** the PDF helper QProcess was parented to the session, and GC at teardown destroyed it mid-render, after which its finished callback hit dead Qt objects. Fixed with `stop_pdf_jobs()` in `HeadlessRunSession.close`/`__del__`, guards in `pdf_job.finish`, and an autouse fixture.
  - The rework first made the suite ~196 s: a test helper waited 10 s on a condition that could never be true and swallowed the timeout. Fixed: `_wait` raises on timeout, and conftest's engine join asserts instead of waiting quietly.
- Light review: 984 passed ×3 (~132 s); collection 954 → 984, none lost; screenshots /tmp/25b-r2-shots OK.
- Leftovers:
  - A layout switch after FINISHED re-picks the schedule via `_carry_over`, which moves the session to SELECTING. The view switch has a session side effect; results still display.
  - `StagedRunScreen._step_identity` has dead code after its return.
  - RunView's header/rollup code is duplicated between the paused and finished renderers.

## 29b — Example: Step Inputs (merged 2026-10-30)
- Implementer (Strata, high, 0 compactions): one schedule with all 8 input types/widgets, plus harness assertions on the recorded values. It flagged two engine deviations instead of fixing them.
- Reviews: Haiku 3 must-fix, Strata 3 must-fix (the same 3). Both engine deviations were confirmed against `test-schedule-format.md`:
  - **CHECKBOX returned multiple Lua values, not an array** (lupa `unpack_returned_tuples`), so `local v = ATE.input(1)` got only the first label;
  - **`tooltip`/`orient` were dropped for INTEGER/FLOAT**;
  - plus a false "FLOAT refused first" comment.
  - A 10a widget test was vacuous for numeric tooltip/orient.
- Rework (Strata, high, 0 compactions):
  - `_map_input` returns `table_from(...)` (the shims keep tuple unpacking);
  - tooltip/orient are kept for every type and shown on every control;
  - the example now reads `local channels = ATE.input(1)`;
  - added the FLOAT refusal;
  - POLL click timing measured from `input_asked`;
  - removed the duplicate `step_seq`;
  - `test_total_time_advances` made deterministic (20/20).
- Light review: 993 passed ×3 (~136 s); `pytest examples/` 5×. One test was renamed to a stronger version (`…returns_one_lua_table_not_a_tuple`); nothing lost.
- Leftovers:
  - `StepInputWidget._placeholder` shows the tooltip instead of the pattern when a STRING has both.
  - The POLL click delay is 0.3 s of wall time (bounded loop of 8).

## 29c — Example: flow, Sub-schedules and modules (merged 2026-10-30)
- Implementer (Strata, high, 0 compactions). It found no engine deviation from the spec. No API lets a step read its Attempt number, so the Retry is driven by a module counter.
- Reviews: Haiku 3 must-fix, Strata 4 must-fix (union 5).
  - Strata showed the "depth first" assertion couldn't fail: with the nested include last, DFS and BFS coincide, and a BFS mutation passed `pytest examples/`.
  - The rest were false comments or README claims (the Pre-test "runs again after", the nest_a counter, "both modules shared").
  - The examples passed `passFail` to Pre-test, an argument the spec doesn't define.
  - The `expected_sha256 = NULL` skip for `ate.` modules was confirmed correct (`test-run-record.md:78`).
- Rework (Strata, high, 0 compactions):
  - an include after the nested chain pins DFS (the BFS mutation now fails);
  - `passFail` dropped from Pre-test;
  - comments and README made true, with `example_helper` now required from an include and its sharing asserted;
  - `test_poll_recorded_when_clicked` made deterministic: a blocking PROMPT is held until the POLL click lands, bounded in ticks. It went from 13/50 failures under load to 0.
- Light review: 999 passed ×3 (~136 s); collection 993 → 999, none lost; the harness ran 8 runs of 5 schedules.
- Leftovers: `test_layout_switching_25b.py::test_a_mid_run_switch_answers_the_input_in_the_new_layout` failed once under 3-way load (seen by Strata only). Watch it.

## 29d — Example: Devices (merged 2026-10-30)
- Implementer (Strata, high, 0 compactions): SCPI/UDP peers in the harness; TCP, SERIAL (`socket://`), VISA (pyvisa-py SOCKET) and UDP exercised end to end.
  - It found and fixed an engine bug: a traced Device opened with `onError` raised a TypeError, because `TracedDevice.__slots__` had no `__weakref__`.
- Reviews: Haiku 2 must-fix (false header claims: a per-call readTerm, the real-hardware instructions); Strata 1 must-fix:
  - **engine: a non-Device handle crashed with a Python AttributeError traceback** (`describe()` before the handle check);
  - `ATE.close("")` returned ok;
  - the example's Pre-test `onError` returned `""`;
  - the port placeholders silently fell back to 5025.
- Rework (Strata, high, 0 compactions):
  - handle validation in read/write/flush/close ("not a Device" through onError, never a traceback), with 13 engine tests;
  - the Pre-test onError returns nil, and a new `devices_peer_down` run asserts PRETEST_FAILED with no traceback;
  - env-var ports (`ATE_SCPI_*`/`ATE_UDP_*`), so the harness runs the shipped bytes;
  - a per-call readTerm demo;
  - peers stop promptly;
  - the double close is pinned (ok, no trace line).
- Light review: 1020 passed ×3 (~146 s); collection 999 → 1020, none lost; the harness ran 11 runs of 6 schedules. The env is restored after each run.
- Leftovers:
  - Spec ambiguity: line 110 says "every close traced" vs line 126 "a second close does nothing". The engine follows 126.
  - New packet 30b covers the finished-run window growth seen in the screenshots.

## 30b — Finished-run layout (merged 2026-10-30)
- New packet from the 10a/29c/29d screenshots: the window grew past 1280x800 after FINISHED.
- Implementer (Strata, high, 0 compactions).
  - Root cause: FINISHED re-showed the setup stages beside the run view, so the central minimumSizeHint was the sum of both. The Log/Summary QLabels also grew with their text.
  - Fixed: stages hidden at FINISHED, a New run button, scrollable TextPanes, size floors.
- Reviews: Haiku 4 must-fix, Strata 4 must-fix (union 5):
  - **the hidden staged screen painted over three-pane** (`_reveal_run_view` called `self.show()`; a 25b bug, also on integration);
  - New run left the session FINISHED (options locked);
  - History "New run" prefilled hidden fields;
  - a layout switch after FINISHED moved the session to SELECTING (lost the finished view);
  - the Log tab accumulated across runs.
- Rework (Strata, high, 0 compactions). Principle applied: a view action never changes session state.
  - `_show_if_on_screen` guard;
  - `new_run()` re-selects the schedule and re-applies stop-on-fail (enable overrides carry);
  - three-pane gained New run;
  - History goes through New run;
  - carry-over never calls the session;
  - run text cleared at start;
  - read-only TextPanes;
  - stretch instead of caps;
  - hermetic tests (opt-in screenshot test skipped by default);
  - the staged abort race made deterministic (`ABORTABLE_TWO_INPUTS`).
- Light review: 1032 passed + 1 skipped ×3 (~153 s); collection 1020 → 1033, none lost; screenshots /tmp/30b-r2-shots OK.
- Leftovers:
  - The staged Debug pane shows a doubled vertical scrollbar.
  - Below ~725 px window height the window still grows.
  - The finished header has no PASS/FAIL chip or Report button (mockup).

## 29e — Example: Test Report check (merged 2026-10-30)
- Implementer (Strata, high, 0 compactions): the harness asserts a report for all 13 runs, plus a green run for every catalog-visible schedule (two extra runs disable the deliberately failing steps). CI already runs the examples.
- Reviews: Haiku 4 must-fix, Strata 1 must-fix. The rendered reports showed gaps left over from 21a:
  - **Cover Duration always "—"**;
  - **schedule file and Media Release name/version missing**;
  - **footer had no run UUID or "Run recorded by / Report generated" line** (`run_ftr` never set);
  - **the PRETEST_FAILED banner didn't name the schedule**.
  - The harness Run-log check was `any()`, not "every line, grouped".
  - CI never set `QT_QPA_PLATFORM=offscreen`.
- Rework (Strata, high, 0 compactions): all report fields filled, and the footer is in `@bottom-left` on every PDF page. The harness:
  - checks every INFO+ line by owner group and time order;
  - checks the Cover fields, footer, seven counts and banner text;
  - parses sections with HTMLParser.
  - Also: CI job-level offscreen env; derived extra-run count.
- Light review: 1042 passed + 1 skipped ×3 (~158 s); collection 1033 → 1043, none lost; PDF page 1 renders checked.
- Leftovers:
  - `test_run.schedule_file` is never written by RunEngine (always NULL; the Cover reads `schedule_instance` seq 0 instead).
  - Parent 29 is fully covered by 29a–29e.

## 27a — PyInstaller build (merged 2026-10-30)
- User decision: build with CPython 3.13 locally (the only Python here); CI builds 3.14. The recipe is interpreter-agnostic.
- Implementer (Strata, high, 0 compactions; the first turn ended while a build ran in the background, so I resumed it):
  - `packaging/pyinstaller/ate.spec`, `ate_entry.py`, `build_pyinstaller.py --smoke`;
  - a hidden `ate _selftest`;
  - `ate/version.txt` baked at build time;
  - a gated build test (`ATE_BUILD_TESTS=1`).
- Reviews:
  - Haiku: 1 must-fix — the checkbox claimed 3.14 was "built by CI", but no CI job does a frozen build.
  - Haiku also found three pre-existing first-run bugs → **new packet 30c**: `<data>` never resolved; `ate db` ignores `--config`; launch-refusal paths drop the QApplication → abort 134. 27c now waits on 30c.
  - **The Strata review failed twice** (a malformed empty tool call ended the turn both times); no Strata review exists for 27a.
- Rework (Strata, high, 0 compactions):
  - honest checkbox (3.13 recipe proven; 3.14 frozen build in 27c);
  - `_size` counts inodes once (942 → 579 MB true);
  - Windows layout for QtWebEngineProcess.exe;
  - `--no-gpu` appended, not clobbered;
  - `_selftest --run`: a frozen end-to-end run with a real QWebEngine PDF (65 KB);
  - Qt binding excludes: 579 → 490 MB, 2688 → 448 files.
- Light review: 1055 passed + 2 skipped ×3 (~159 s); `ATE_BUILD_TESTS=1 pytest -m build` 1 passed (37 s); collection 1043 → 1057, none lost; no build output left.
- Leftovers / Windows unverified:
  - QTWEBENGINEPROCESS_PATH on Windows;
  - whether the excluded QML data is needed by WebEngine on Windows;
  - the ~76 MB debug .pak on Windows.
  - Flake: `test_report_steps_21b.py::test_autoescape_does_not_change_the_cover_bytes` is time-dependent since 29e's footer timestamp. Folded into 30c.
