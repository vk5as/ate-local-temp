# 06c: Mismatch flag, run-start confirmation and `storeMismatchedFiles`

**Parent:** 06 (`require`, checksums and mismatch handling) — `06-require-and-checksums.md`

**What to build:** A checksum mismatch is flagged on the Run screen and confirmed at run start, optionally storing the mismatched files with the run.

**Blocked by:** 06b (SHA-256 checks and `media_release`/`run_file` rows)

**Status:** ready-for-agent

- [ ] A mismatched schedule is flagged on the Run screen
- [ ] A run-start confirmation appears only when there is a mismatch, listing the files; with `database.storeMismatchedFiles` = `ask` it has a "Store mismatched files with this Test Run" checkbox ticked by default; `always`/`never` behave as named; stored bytes go on the `run_file` row
- [ ] Seam-1 tests cover each storage setting through the run session (the dialog choice is injected, not clicked)

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
