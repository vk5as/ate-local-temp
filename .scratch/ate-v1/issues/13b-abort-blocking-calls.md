# 13b: `ATE.sleep` and abort-aware blocking calls

**Parent:** 13 (Abort) — `13-abort.md`

**What to build:** Blocking calls stop promptly on abort.

**Blocked by:** 13a (Whole-run Abort), 10 (Step Inputs)

**Status:** ready-for-agent

- [ ] `ATE.sleep(seconds)` exists (fractions allowed) and is interruptible
- [ ] Blocking calls (`ATE.input`, `ATE.sleep`) wait on the abort flag and raise `"aborted"`
- [ ] Seam-1 tests abort a blocked input and a sleep within a bound comparable to the Windows prototype

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
