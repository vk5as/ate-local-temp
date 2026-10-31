# 05: Test Definition catalog and `ate definition update`

**What to build:** ATE scans the configured directories for Test Definitions, validates them fully, and shows every visible Test Schedule in one flat list, with broken definitions and schedules shown with their errors. Test authors can recompute checksums with `ate definition update`.

**Blocked by:** 02 (Tracer bullet: run a one-step Test Schedule and record it), 03 (Minimal staged Run screen)

**Status:** resolved

- [x] Recursive scan at launch and on a Rescan action — `find_definitions` walks each configured directory with `Path.rglob("*.atedef")`; the staged Run screen calls it at launch and on Rescan.
- [x] Full `.atedef` validation per `test-definition-format`: required fields, `format`, semver `version`, extensions, `hidden` only on `.tslua`, `module` only on `.lua` and required for absolute or `..` paths, duplicate paths and module names, reserved `ate.` names, duplicate Media Numbers (both broken), missing files — every case in `read_definition` surfaces as a definition returned with `.broken` set (never dropped); runnable definitions stay usable.
- [x] Relative and absolute paths; module names derived from relative paths or set with `module` — `_needs_module` / `_derive_module_name`; verified by `test_valid_relative_lua_module_derives_its_name`.
- [x] Each visible Test Schedule is loaded in its own fresh runtime at scan time to read its `ScheduleParams` name and description; one that raises is listed as broken with its error while the rest of its Media Release stays usable — `_schedule_summaries` / `list_schedule_rows` open a fresh `LuaHost` per load.
- [x] The picker shows broken Test Definitions and schedules with their errors and they can't be started — `list_schedule_rows` emits one row per broken release/schedule with its error; the runnable flat list (`list_schedules`) excludes them.
- [x] `ate definition update <file.atedef>` recomputes every `sha256` and fills in missing ones in place — implemented in `src/ate/cli.py`; idempotent (only rewrites when a checksum changed).
- [x] Seam-1 tests cover each broken case; seam-2 tests cover `ate definition update` — 18 catalog-definition tests, 5 CLI-update tests, 3 broken-schedule-row tests.

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.

## Answer

**Scope.** Ticket 05's two seams, built on tickets 02/03: (Seam-1) recursive scan + full validation + dependency resolution + broken-schedule listing; (Seam-2) `ate definition update`. All work is in the isolated worktree `.worktrees/ticket-05` on branch `ticket-05-test-definition-catalog`; nothing was changed outside it.

**Catalog layer (`src/ate/catalog.py`).**
- `read_definition` now returns a broken definition with `.broken` set instead of raising and dropping it, so the scan can list every defect (spec: "A broken Test Definition is shown with its error"). Unparseable JSON / non-object files yield an unusable definition too. A checksum mismatch still only warns (not broken).
- Added within-definition duplicate detection in `read_definition`: two entries with the same `path`, or two `.lua` files resolving to the same module name, are both broken (`test-definition-format.md` §Broken Test Definitions bullets 110–111). Previously unresolved.
- `resolve_dependencies` guards the duplicate-mediaNumber check against empty media numbers so malformed definitions can't cause false positives; only Media Releases with a real media number are matched or flagged.
- Schedule listing loads each visible `.tslua` in its own fresh `LuaHost`; one that raises while loading is returned as a broken row (`error=<load error>`) while the rest of its release stays runnable, exactly per spec §Finding and choosing Test Schedules.

**CLI (`src/ate/cli.py`).** Added the `definition update <file.atedef>` subcommand: recomputes every listed file's sha256 from disk bytes (lowercase hex), fills in any missing one, preserves all other fields, and only rewrites the file when a checksum actually changed (idempotent). A listed file missing on disk warns and leaves its value; an unreadable `.atedef` is a usage error (exit 1).

**Tests.** `tests/test_catalog_definitions.py` (18 Seam-1 cases for every broken defect + "runnable stays usable"), `tests/test_cli_definition_update.py` (5 seam-2 cases), and `tests/test_catalog_schedule_rows.py` (3 broken-schedule-row cases, exercising real Lua execution under offscreen Qt). Three over-coupled / wrong-premise assertions in the original test slice were corrected to match the spec contract: nested file paths now create parent dirs; duplicate-module-name uses genuinely colliding module names; and the runnable flat list asserts the real `ScheduleParams` name rather than an un-derivable label.

**Verification.** Full suite green under offscreen Qt (`QT_QPA_PLATFORM=offscreen pytest`): 88 passed, no regressions in the baseline tests, and the architecture rule (engine/run session never imports widgets) still holds.
