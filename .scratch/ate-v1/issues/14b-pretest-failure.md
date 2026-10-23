# 14b: Pre-test failure

**Parent:** 14 (Pre-test and Post-test Actions and the status bar) — `14-pre-and-post-test-actions.md`

**What to build:** A failing Pre-test Action aborts the run cleanly.

**Blocked by:** 14a (Pre-test and Post-test Actions and `action` rows)

**Status:** ready-for-agent

- [ ] A Pre-test failure aborts automatically: every step `NOTRUN`, End Reason `PRETEST_FAILED`, and a log line naming the failure and explaining why every step is `NOTRUN`
- [ ] Seam-1 tests cover it

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
