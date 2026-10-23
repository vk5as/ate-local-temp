# 13: Abort

**What to build:** The Operator can abort the whole run at any moment, and it stops promptly even in a busy Lua loop or one that `pcall`s.

**Blocked by:** 09 (Lua lockdown, errors and `ATE.run`)

**Status:** split

**Split into:** 13a, 13b, 13c — work happens in those packets; this ticket is resolved when all of them are.

- [ ] A whole-run Abort control (no pause or skip)
- [ ] Blocking calls (`ATE.input`, `ATE.sleep`, and `ATE.read` once Devices exist) wait on the abort flag and raise `"aborted"`
- [ ] `ATE.sleep(seconds)` exists (fractions allowed) and is interruptible
- [ ] A count hook every 1000 instructions raises `"aborted"`; `pcall` and `xpcall` are wrapped to re-raise it, so an abort can't be caught; ATE's own step runner keeps the original `xpcall`
- [ ] The running step records `ERROR` "Aborted by Operator", remaining steps `NOTRUN`, End Reason `ABORTED`, result `FAIL`
- [ ] Seam-1 tests abort a busy loop, a `pcall`ed busy loop, a blocked input and a sleep, each within a bound comparable to the Windows prototype

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
