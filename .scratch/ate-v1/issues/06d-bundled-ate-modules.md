# 06d: Bundled `ate.` modules

**Parent:** 06 (`require`, checksums and mismatch handling) — `06-require-and-checksums.md`

**What to build:** `ate.`-prefixed module names resolve to modules shipped with ATE, and their checksums and the ATE version are recorded.

**Blocked by:** 06b (SHA-256 checks and `media_release`/`run_file` rows)

**Status:** ready-for-agent

- [ ] `ate.` module names resolve to modules bundled with ATE (a package directory inside `src/ate`); unknown `ate.` names raise
- [ ] The bundled modules' checksums and the ATE version are recorded with the run
- [ ] Seam-1 tests require a bundled module and read back the recorded rows

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
