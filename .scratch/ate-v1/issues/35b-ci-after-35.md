# 35b: CI red after packet 35

**Parent:** 35. CI run 41407051552 on `b4df8b1` failed all four jobs. Log saved at `/home/dev/ate-reviews/ci35.log`.

**What to build:** CI green on Linux and Windows. This blocks the rc3 tag. Fix product code where a failure shows a real Windows bug, and tests where the test is wrong.

**Blocked by:** none (runs after 36's implementer frees Strata)

**Status:** queued

- [ ] Linux, both Pythons: `test_a_copy_that_keeps_restarting_is_refused_within_the_bound` passed locally but the copy succeeded on the runner: the writer thread doesn't reliably force restarts there. Make the restart deterministic, for example by driving the progress callback, and keep it able to fail.
- [ ] Windows: config JSON written by tests with raw `C:\...` paths is invalid JSON (`invalid JSON in config file`, `JSONDecodeError`). Find whether only tests do this, or whether `ate setup` or any product writer does too: a product writer must use `json.dump`.
- [ ] Windows: tests asserting exact `ate setup` output fail on "running unprivileged; skipping group/ownership" lines; `test_a_bare_ate_set_up_takes_its_directories_from_the_config_seams` reaches the real `C:\ProgramData\ATE`: no test may touch a real system directory on any platform.
- [ ] Windows: `signal.SIGKILL` doesn't exist; `os.execute` holding the abort and `kill $PPID` are POSIX commands. Use platform-appropriate commands or skip with a stated reason, and say in the manuals what is POSIX-only.
- [ ] Windows: `test_an_abort_ends_a_long_write_to_a_peer_that_stopped_reading[SERIAL|TCP|VISA]` fail with an unexpected result (`NOREPORT`?). These are 32b's tests and passed before: find out whether 35 changed behaviour or the test is flaky on Windows, and fix the cause.
- [ ] Every failure in the log is accounted for, one line each.
