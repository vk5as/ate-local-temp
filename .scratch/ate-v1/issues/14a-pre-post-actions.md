# 14a: Pre-test and Post-test Actions and `action` rows

**Parent:** 14 (Pre-test and Post-test Actions and the status bar) — `14-pre-and-post-test-actions.md`

**What to build:** Schedules set up and tear down around their steps.

**Blocked by:** 13a (Whole-run Abort)

**Status:** ready-for-agent

- [ ] The Pre-test Action runs before the first step and the Post-test Action after the last, called with the run's `passFail`
- [ ] Each call is an `action` row (instance, phase, order, times, outcome `ok`/`error`/`aborted`/`skipped`, error)
- [ ] Seam-1 tests cover the normal path and the rows

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
