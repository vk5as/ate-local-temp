# 06: `require`, checksums and mismatch handling

**What to build:** Schedules load helper modules through ATE's replacement `require`, every file used is checksummed and recorded, and a mismatch is warned about, flagged, optionally stored, and confirmed at run start.

**Blocked by:** 05 (Test Definition catalog and `ate definition update`)

**Status:** split

**Split into:** 06a, 06b, 06c, 06d — work happens in those packets; this ticket is resolved when all of them are.

- [ ] `require` resolves only module names listed in the Test Definition (dependencies come later), never `package.path`; modules run once per Test Run and are cached
- [ ] `ate.` module names resolve to modules bundled with ATE; the bundled modules' checksums and the ATE version are recorded
- [ ] SHA-256 of raw bytes is checked for every `.tslua` and `.lua` used; a mismatch logs a warning, still loads, and is flagged on the Run screen
- [ ] `media_release` and `run_file` rows record the release and every file used with expected and computed checksums and the mismatch flag
- [ ] A run-start confirmation appears only when there is a mismatch, listing the files; with `database.storeMismatchedFiles` = `ask` it has a "Store mismatched files with this Test Run" checkbox ticked by default; `always`/`never` behave as named; stored bytes go on the `run_file` row
- [ ] Seam-1 tests cover resolution, caching, mismatch recording and each storage setting

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
