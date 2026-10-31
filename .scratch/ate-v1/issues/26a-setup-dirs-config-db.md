# 26a: `ate setup`: directories, config and database

**Parent:** 26 (`ate setup`) — `26-ate-setup.md`

**What to build:** One admin command prepares a Station's directories, config and database.

**Blocked by:** 04 (Application config in full, and interrupted-run recovery)

**Status:** resolved

**Note:** Resume from branch `ticket-26-ate-setup` (a WIP checkpoint with `src/ate/setup.py` and tests is committed there).

- [x] Creates the data directories (`%PROGRAMDATA%\ATE`, `/var/lib/ate`) and the config directory
- [x] Creates a default `config.json` with a new `station.uuid` only if none exists
- [x] Runs `ate db migrate`
- [x] Seam-2 tests run it against temporary directories and check the results

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
