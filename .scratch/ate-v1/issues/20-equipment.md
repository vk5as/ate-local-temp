# 20: Equipment

**What to build:** Schedules declare the Equipment on the bench so its identity and calibration are recorded for the Test Report.

**Blocked by:** 09 (Lua lockdown, errors and `ATE.run`)

**Status:** resolved

- [x] `ATE.addEquipment(name, serial, connection, calDue, notes)`: strings, `name` required, others may be `nil`; other types raise
- [x] Allowed in Pre-test Actions, steps and Post-test Actions; a repeat with the same `name` and `serial` is ignored with a `DEBUG` line; first-call order kept
- [x] Recorded in the `equipment` table with time and owner
- [x] Seam-1 tests read back declared Equipment

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.

## Comments

- `ATE.addEquipment` is a method on `_AteTable` (like `report`/`log`), so a bad argument raises a real
  Lua error rather than the isolated `(false, message)` halves. A call while the `.tslua` is still
  loading is refused the same way `ATE.report` is: the spec allows Equipment only in a Pre-test Action,
  a step or a Post-test Action, and the top level runs before a Test Run exists, so the schedule fails
  to load and is never offered to run.
- Rows are written by the run engine's drain (`_flush_equipment`), which every ATE call triggers, so
  each declaration is recorded as it happens, owned by the running step Attempt (`node_seq`/`attempt_no`)
  or Action (`action_seq`), exactly like that action's log lines. Its `seq` comes from the run's shared
  row counter, as `action` and `input_value` rows do.
- The repeat's `DEBUG` line is ATE's own, so it is logged with `source = "ate"`.
- Schema v5: `SCHEMA_VERSION = 4` → `5`, with `equipment` created by `metadata.create_all`; `ate db
  migrate` upgrades a v4 database (test pinning it in `tests/test_cli_db.py`).
