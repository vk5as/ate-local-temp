# 35: Gaps found while writing the admin manual

**Parent:** none. Found by 34b, 2026-11-01. The name and the backup are user decisions.

**What to build:** Small fixes to what the manuals had to warn about. Starts after 34's reviews merge, so it can correct the manuals at the same time.

**Blocked by:** 34

**Status:** queued

- [ ] **Name:** ATE stands for "Automatic Test Execution" (user, 2026-11-01). Change `GLOSSARY.md` ("Automated Test Execution"), `src/ate/cli.py` ("Automated Test Equipment"), the manuals, and anywhere else `git grep -n -i -E 'automated test|automatic test'` finds, except history under `.scratch`.
- [ ] **Backup on migrate:** `ate db migrate` makes a timestamped copy of the database beside it before changing anything, as the wayfinding asset says, and prints the copy's path. A new database, or one already at the current schema, makes no copy. If the copy fails, nothing is migrated: it refuses, naming the path and reason, with a non-zero exit. A migration that fails leaves the copy in place and says where it is. Include SQLite's WAL if the database uses one, using SQLite's backup API rather than a plain file copy. Tests: each case above, and restoring from the copy gives the old data. The admin manuals say what happens instead of telling the admin to make the copy, and say old copies are not removed automatically.
- [ ] **`--config` after a subcommand:** `ate db status --config X` treats `X` as the database path and ends in an unhandled `sqlite3.DatabaseError`. Accept `--config` after the subcommand, for `db status`, `db migrate` and any other subcommand that reads the config. A path that isn't a database gives a one-line error and exit 1, never a traceback.
- [ ] **Settings:** `reports.paperSize` is validated when the config is read, naming the key and the allowed values. `ui.theme` and `reports.logo` wait for the user's appearance decisions (33); note them only.

Definitions live in `.scratch/ate-v1-wayfinding/assets/` (`app-config-format.md`, `packaging.md`); the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes.
