# 15b: `setInclude` and Sub-schedule instances

**Parent:** 15 (Sub-schedules) — `15-sub-schedules.md`

**What to build:** A Test Schedule includes other Test Schedules, each include its own instance.

**Blocked by:** 15a (Step Names and header rows), 06b (SHA-256 checks and `media_release`/`run_file` rows)

**Status:** resolved

- [x] `setInclude(file, keyvals)` per `test-schedule-format`: resolution through the Test Definition, checksum checked and recorded, chunk re-run per include with `keyvals` (plain data only) as its argument
- [x] An included schedule's doc fields and `setStopOnFail` are ignored
- [x] `schedule_instance` rows and header `node` rows are recorded
- [x] Seam-1 tests cover expansion and the recorded rows

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
