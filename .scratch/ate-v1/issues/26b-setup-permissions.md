# 26b: `ate setup`: permissions

**Parent:** 26 (`ate setup`) — `26-ate-setup.md`

**What to build:** Ordinary users can write data but not the config.

**Blocked by:** 26a (`ate setup`: directories, config and database)

**Status:** ready-for-agent

- [ ] Gives ordinary users modify rights on the database and WAL files, `reports` and `logs`, keeping `config.json` admin-only; on Linux creates an `ate` group for this (through an injectable filesystem seam so tests don't need root)
- [ ] Seam-2 tests check the requested permissions

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
