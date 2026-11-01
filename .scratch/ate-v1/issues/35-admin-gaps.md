# 35: Gaps found while writing the admin manual

**Parent:** none. Found by 34b, 2026-11-01. The name and the backup are user decisions.

**What to build:** Small fixes to what the manuals had to warn about. Starts after 34's reviews merge, so it can correct the manuals at the same time.

**Blocked by:** 34

**Status:** queued

- [ ] **Name:** ATE stands for "Automated Test Execution" (user, 2026-11-01), as `GLOSSARY.md` already says. Change `src/ate/cli.py` ("Automated Test Equipment"), the manuals, and anywhere else `git grep -n -i -E 'automatic test|test equipment'` finds, except history under `.scratch`.
- [ ] **Backup on migrate:** `ate db migrate` makes a timestamped copy of the database beside it before changing anything, as the wayfinding asset says, and prints the copy's path. A new database, or one already at the current schema, makes no copy. If the copy fails, nothing is migrated: it refuses, naming the path and reason, with a non-zero exit. A migration that fails leaves the copy in place and says where it is. Include SQLite's WAL if the database uses one, using SQLite's backup API rather than a plain file copy. Tests: each case above, and restoring from the copy gives the old data. The admin manuals say what happens instead of telling the admin to make the copy, and say old copies are not removed automatically.
- [ ] **Backup on every upgrade path:** the installers (`deb/postinst:20`, `install.sh:106`, `ate.iss:247`) run `ate setup`, which migrates through `setup.py` `_migrate_database` calling `RunStore` directly, so a backup only in `ate db migrate` misses every real upgrade. Share one routine. It migrates `<data_dir>/ate.db` rather than `database.path`: on a Station with a custom path, either migrate the configured database or say plainly that it didn't. `_ensure_schema` stamps `user_version` regardless (`store.py:268`), so a version bump with no migration makes an old database claim to be current: make that impossible, with a test.
- [ ] **`os.exit`:** `os.exit(7)` in a step ends the whole of ATE with nothing recorded (Opus measured, 2026-11-02). Remove `os.exit` from the sandbox, with a test.
- [ ] **Abort exceptions in the manuals (user, 2026-11-02):** `os.execute` stays available, and a command it runs, and a busy loop inside a coroutine, are the documented exceptions to the must-stop-within-10-s rule. Don't change the code for these. All the manuals present them as allowed exceptions a test writer should know about, not as things never to do.
- [ ] **`--config` after a subcommand:** `ate db status --config X` treats `X` as the database path and ends in an unhandled `sqlite3.DatabaseError`. Accept `--config` after the subcommand, for `db status`, `db migrate` and any other subcommand that reads the config. A path that isn't a database gives a one-line error and exit 1, never a traceback.
- [ ] **Settings:** `reports.paperSize` is validated when the config is read, naming the key and the allowed values. `ui.theme` and `reports.logo` wait for the user's appearance decisions (33); note them only.

Definitions live in `.scratch/ate-v1-wayfinding/assets/` (`app-config-format.md`, `packaging.md`); the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes.
