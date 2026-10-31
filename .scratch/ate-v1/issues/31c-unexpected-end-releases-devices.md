# 31c: Pin that an unexpected run end releases Devices and leaves no Lua state

**Parent:** none. User decision, 2026-10-31: an unexpected exception on the worker keeps closing the run as `INTERRUPTED`/`FAIL` with Post-test Actions skipped, provided the next run gets a clean Lua environment and every open Device is closed.

**What to build:** Tests only. A throwaway probe (2026-10-31, pseudo-terminal on Linux, with a control that disabled teardown and did see the leak) found the condition holds today, but no test in the suite would catch it breaking.

**Blocked by:** none

**Status:** ready-for-agent

- [ ] In `tests/test_unexpected_run_end.py`: a step opens two SERIAL Devices and sets a Lua global and a field on `string`, then `RunStore.insert_attempt` raises. Assert the run closes `INTERRUPTED`/`FAIL`, and both Devices are closed with their readers stopped, at the moment FINISHED arrives.
- [ ] The same with `RunStore.update_run_end` raising, and with `RunStore.insert_log_lines` raising: the Devices are still closed.
- [ ] A Device opened in a Pre-test Action is closed the same way when a later step ends the run unexpectedly.
- [ ] One Device's close raising does not stop the next Device closing.
- [ ] The next run on the same session, reopening the same port, passes and sees `nil` for the global and the `string` field.
- [ ] On Linux only (skip elsewhere), use a real pseudo-terminal: no file descriptor still points at the port after the run, and `serial.Serial(port, exclusive=True)` opens it.
- [ ] Each test is shown to fail with `_close_open_devices` disabled, or with a Lua runtime reused between runs.

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes.
