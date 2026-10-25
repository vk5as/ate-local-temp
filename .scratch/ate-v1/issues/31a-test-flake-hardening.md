# 31a: Harden the flaky shared test helpers

**Parent:** none (found during review of 17a/17b/24a/24b) — see `.scratch/ate-v1/review-notes.md`, "Shared test helpers that cause flakes" and "Test suite under load".

**What to build:** The test suite passes reliably, including under CPU load, by removing race-prone waits in shared helpers.

**Blocked by:** none

**Status:** ready-for-agent

- [ ] `LoopbackPeer(hangup="reset")` (tests/_fixtures.py) can no longer send its RST before the client's `connect` returns: reset only after the device's first byte (or after accept is observed), and every test using it still checks the same behaviour
- [ ] Event-loop waits that pump a fixed number of times (e.g. `_wait_finished` in tests/test_headless_session.py, and any similar helper) wait on a time bound instead
- [ ] The full suite passes 10/10 runs, and 10/10 with another full suite running in parallel
