# 31b: Serial device test flakes

**Parent:** none (test hygiene, like 31a)

**What to build:** `tests/test_serial_devices.py` passes every time, alone and in the full suite.

**Blocked by:** none

**Status:** resolved

Evidence (2026-10-26):
- Strata's 06d review: 10 runs of the file gave 3 failures, in `test_read_term_size_and_timeout_can_be_overridden_per_call` and `test_flush_discards_the_buffer_and_reports_the_count`, e.g. `assert '0 true nothing' == '5 true nothing'`. Every test passes in isolation.
- The orchestrator's 06d final review, on an idle machine: 1 failure in 3 full-suite runs, and 2 in 10 runs of the file (`test_read_honours_per_call_read_term_size_and_timeout`, `test_read_term_size_and_timeout_can_be_overridden_per_call`). A later 20 runs were clean, so it's rare and load-dependent.
- Known suspects from the 18a review notes: `test_flush_empties_the_buffer` waits for the payload with `ATE.sleep(0.2)`, and `test_open_with_every_field_then_write_and_read` uses a hand-rolled PING wait. The serial reader reads one byte per pyserial call with a 50 ms poll, so a 10-byte payload can take several polls to land.

Reproduced (2026-10-27, 3-core box, 4 background `while True: pass` hogs as the load): 4 failing runs out of 60 alone, and 27 failing runs out of 30 under load (2-5 tests each); the detailed loop (12 loaded runs) caught these:

| test | failures in 12 loaded runs | assertion output |
| --- | --- | --- |
| `test_a_read_that_times_out_after_some_bytes_is_partial_and_warns` | 5 | `assert 'ERROR' == 'PASS'` - the step errored concatenating a nil `data` |
| `test_a_peer_that_hangs_up_fails_the_device_once_and_later_calls_error` | 5 | same: `assert 'ERROR' == 'PASS'` |
| `test_read_term_size_and_timeout_can_be_overridden_per_call` | 4 | same |
| `test_a_small_receive_buffer_overflows_once_with_a_warning` | 4 | same (`#r.data` on a nil `data`) |
| `test_flush_empties_the_buffer` | 3 | `assert '0 true nothing' == '5 true nothing'` |
| `test_data_that_arrives_before_the_read_is_buffered` | 3 | `assert 'ERROR' == 'PASS'` |
| `test_read_honours_per_call_read_term_size_and_timeout` | 2 | `AssertionError: device never reached the expected state` - `_wait_for(len(d._buffer) >= 10, 2.0)` |
| `test_open_with_every_field_then_write_and_read` | 2 | `assert 'ERROR' == 'PASS'` |
| `test_peer_hang_up_fails_the_device_once_and_later_calls_report_it` | 1 | `assert (None, 0, False, '...disconnected') == (b'buffered', 8, False, None)` - "buffered bytes survive the failure" |
| `test_flush_discards_the_buffer_and_reports_the_count` | 1 | `assert dev.flush() == (True, 5, None)` got `(True, 0, None)` |
| `test_bytes_are_buffered_before_the_read_and_a_write_appends_its_terminator` | 1 | `device never reached the expected state` |

Nothing fails when a single test is run by itself.

Cause (one race, not several): pyserial 3.5's `socket://` handler calls `reset_input_buffer()` inside `open()`, which drains whatever the peer has already sent. `LoopbackPeer` hands its payload to the connection on accept, so on a loaded machine the peer thread gets to run inside that window and the reply is discarded before the Device's reader ever starts. Instrumented proof: `protocol_socket.Serial.reset_input_buffer` logged `flushed_at_open = b'0123456789'` in the failing runs, and the reader's first `read(1)` then returned EOF. Every failure follows from that empty buffer: reads time out, Lua concatenates `nil` and the step reports ERROR, `flush` counts 0, and a hang-up peer fails the device with nothing buffered. `src/ate/devices/serial.py` and `stream.py` are not at fault - the driver cannot recover bytes pyserial throws away before the reader exists - so no product code changed.

Fix: no timing guesses. `LoopbackPeer.send()` hands over the reply after `open()` has returned (provably after that flush), `_wait_for` waits on the Device's own buffer/flags under the lock (deadline raised to 10 s as a safety net), the Seam-1 payload tests use a two-step schedule driven through the host with the wait in the gap (18b's overflow seam), `_wait_for(dev, lambda d: d._overflow_dropped == 36)` replaces `_overflow_pending` (the flag can trip on a partial chunk and leave the wrong 64 bytes in the buffer), `wait_for_disconnect(peer)` replaces the hand-rolled PING poll, and `ATE.sleep(0.2)`/`ATE.sleep(0.3)` are gone. Assertions unchanged, except: the overflow tests now wait for "36 bytes dropped" rather than "an overflow flag", and `test_close_then_open_again` waits for both connections to close before reading `peer.received` - both are stronger, not weaker.

Verified after the change: the file passes 30/30 alone, 30/30 with the 4 CPU hogs running, and the full suite 10/10 (`545 passed`).

- [x] Reproduce each failure (loop the file under load, e.g. alongside a CPU hog) and capture the assertion output
- [x] Replace every timing guess with a wait on real state (`_wait_for` on the buffer under the lock, or a peer-side deadline poll), for Seam-1 tests too (e.g. a two-step schedule with a Python-side wait between steps, as 18b's overflow seam does)
- [x] `tests/test_serial_devices.py` passes 30/30 alone and 30/30 under load; the full suite passes 10/10
