# 15: Sub-schedules

**What to build:** A Test Schedule can include other Test Schedules as Sub-schedules under header rows, each include its own instance, with Pre-test and Post-test Actions ordered across the whole tree.

**Blocked by:** 06 (`require`, checksums and mismatch handling), 11 (Enabling and disabling steps before the run), 14 (Pre-test and Post-test Actions and the status bar)

**Status:** split

**Split into:** 15a, 15b, 15c, 15d, 15e — work happens in those packets; this ticket is resolved when all of them are.

- [ ] `StepParams(name)` takes an optional Step Name for every step and header
- [ ] `setInclude(file, keyvals)` and `setPrefix` per `test-schedule-format`: resolution through the Test Definition (and later dependencies), checksum checked and recorded, chunk re-run per include with `keyvals` (plain data only) as its argument
- [ ] Prefixes stack outside in; duplicate Step Numbers and cycles are checked on the expanded tree at scan time and run start; a bad include breaks only the including schedule
- [ ] Include rows are headers with no `Result`; invalid setter combinations are load errors; an included schedule's doc fields and `setStopOnFail` are ignored
- [ ] `setEnable` on a header forces the default beneath it; toggling a header sets everything beneath; mixed states show a tri-state mark
- [ ] All Pre-test Actions run up front depth first, parent first; Post-test Actions in exact reverse; a fully disabled Sub-schedule's actions are skipped; after a Pre-test failure only instances whose Pre-test started get a Post-test Action
- [ ] `schedule_instance` rows and header `node` rows are recorded
- [ ] Seam-1 tests cover expansion, numbering, enabling and action ordering

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
