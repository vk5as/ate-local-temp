# 06b: SHA-256 checks and `media_release`/`run_file` rows

**Parent:** 06 (`require`, checksums and mismatch handling) — `06-require-and-checksums.md`

**What to build:** Every `.tslua` and `.lua` a run uses is checksummed, and the release and files are recorded with expected and computed checksums.

**Blocked by:** 06a (Replacement `require`: resolution and caching)

**Status:** ready-for-agent

- [ ] SHA-256 of raw bytes is checked for every `.tslua` and `.lua` used; a mismatch logs a `WARNING` (source `ate`) and still loads
- [ ] `media_release` and `run_file` rows record the release and every file used with expected and computed checksums and the mismatch flag
- [ ] Seam-1 tests cover a clean run and a mismatched file, reading the rows back

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
