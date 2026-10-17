# 26: `ate setup`

**What to build:** One admin command prepares a Station for every install format.

**Blocked by:** 04 (Application config in full, and interrupted-run recovery)

**Status:** ready-for-agent

- [ ] Creates the data directories (`%PROGRAMDATA%\ATE`, `/var/lib/ate`) and the config directory
- [ ] Gives ordinary users modify rights on the database and WAL files, `reports` and `logs`, keeping `config.json` admin-only; on Linux creates an `ate` group for this
- [ ] Creates a default `config.json` with a new `station.uuid` only if none exists
- [ ] Runs `ate db migrate`
- [ ] Seam-2 tests run it against temporary directories and check the results

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
