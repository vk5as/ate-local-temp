# 14c: Post-test always runs

**Parent:** 14 (Pre-test and Post-test Actions and the status bar) — `14-pre-and-post-test-actions.md`

**What to build:** Teardown always runs.

**Blocked by:** 14b (Pre-test failure), 13c (Abort a busy Lua loop), 12b (Failure pause with Continue and Abort)

**Status:** resolved

- [x] The Post-test Action always runs, after a stop, a Pre-test failure or an abort; ATE clears the abort flag first; a second abort ends only the current action; an error in it is logged and fails the run
- [x] Seam-1 tests cover each outcome and End Reason

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
