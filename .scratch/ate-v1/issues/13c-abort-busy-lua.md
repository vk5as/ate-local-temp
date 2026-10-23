# 13c: Abort a busy Lua loop

**Parent:** 13 (Abort) — `13-abort.md`

**What to build:** Abort works even in a busy loop or one that `pcall`s.

**Blocked by:** 13a (Whole-run Abort)

**Status:** resolved

- [x] A count hook every 1000 instructions raises `"aborted"`; `pcall` and `xpcall` are wrapped to re-raise it so an abort can't be caught; ATE's own step runner keeps the original `xpcall`
- [x] Seam-1 tests abort a busy loop and a `pcall`ed busy loop within a bound

Resolved by `afe50f5`. The count hook + re-raising pcall/xpcall live in `lua_host.py`; the mid-step `"aborted"` routing into `_record_abort` is in `run_engine.py`. The seam-1 tests needed two harness fixes to go green: `_drive_busy` must call `engine.start()` (no worker thread ⇒ FINISHED never emitted), and the timing assertions had to measure elapsed wall-clock (`time.time() - t0`) instead of absolute epoch time.

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
