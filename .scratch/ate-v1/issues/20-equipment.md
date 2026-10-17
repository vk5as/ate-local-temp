# 20: Equipment

**What to build:** Schedules declare the Equipment on the bench so its identity and calibration are recorded for the Test Report.

**Blocked by:** 09 (Lua lockdown, errors and `ATE.run`)

**Status:** ready-for-agent

- [ ] `ATE.addEquipment(name, serial, connection, calDue, notes)`: strings, `name` required, others may be `nil`; other types raise
- [ ] Allowed in Pre-test Actions, steps and Post-test Actions; a repeat with the same `name` and `serial` is ignored with a `DEBUG` line; first-call order kept
- [ ] Recorded in the `equipment` table with time and owner
- [ ] Seam-1 tests read back declared Equipment

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
