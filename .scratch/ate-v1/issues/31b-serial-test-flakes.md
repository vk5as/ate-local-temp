# 31b: Serial device test flakes

**Parent:** none (test hygiene, like 31a)

**What to build:** `tests/test_serial_devices.py` passes every time, alone and in the full suite.

**Blocked by:** none

**Status:** ready-for-agent

Evidence (2026-10-26):
- Strata's 06d review: 10 runs of the file gave 3 failures, in `test_read_term_size_and_timeout_can_be_overridden_per_call` and `test_flush_discards_the_buffer_and_reports_the_count`, e.g. `assert '0 true nothing' == '5 true nothing'`. Every test passes in isolation.
- The orchestrator's 06d final review, on an idle machine: 1 failure in 3 full-suite runs, and 2 in 10 runs of the file (`test_read_honours_per_call_read_term_size_and_timeout`, `test_read_term_size_and_timeout_can_be_overridden_per_call`). A later 20 runs were clean, so it's rare and load-dependent.
- Known suspects from the 18a review notes: `test_flush_empties_the_buffer` waits for the payload with `ATE.sleep(0.2)`, and `test_open_with_every_field_then_write_and_read` uses a hand-rolled PING wait. The serial reader reads one byte per pyserial call with a 50 ms poll, so a 10-byte payload can take several polls to land.

- [ ] Reproduce each failure (loop the file under load, e.g. alongside a CPU hog) and capture the assertion output
- [ ] Replace every timing guess with a wait on real state (`_wait_for` on the buffer under the lock, or a peer-side deadline poll), for Seam-1 tests too (e.g. a two-step schedule with a Python-side wait between steps, as 18b's overflow seam does)
- [ ] `tests/test_serial_devices.py` passes 30/30 alone and 30/30 under load; the full suite passes 10/10
