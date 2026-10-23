# 13c: Abort a busy Lua loop

**Parent:** 13 (Abort) — `13-abort.md`

**What to build:** Abort works even in a busy loop or one that `pcall`s.

**Blocked by:** 13a (Whole-run Abort)

**Status:** ready-for-agent

- [ ] A count hook every 1000 instructions raises `"aborted"`; `pcall` and `xpcall` are wrapped to re-raise it so an abort can't be caught; ATE's own step runner keeps the original `xpcall`
- [ ] Seam-1 tests abort a busy loop and a `pcall`ed busy loop within a bound

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
