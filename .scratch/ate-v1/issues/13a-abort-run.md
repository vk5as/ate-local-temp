# 13a: Whole-run Abort

**Parent:** 13 (Abort) — `13-abort.md`

**What to build:** The Operator can abort the whole run; recording and End Reason are right. Interrupting blocked or busy Lua comes in 13b/13c.

**Blocked by:** 09 (Lua lockdown, errors and `ATE.run`)

**Status:** ready-for-agent

- [ ] A whole-run Abort control on the Run screen (no pause or skip) that sets the run session's abort flag
- [ ] The running step records `ERROR` "Aborted by Operator", remaining steps `NOTRUN`, End Reason `ABORTED`, result `FAIL`
- [ ] Seam-1 tests abort between steps and check the record

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
