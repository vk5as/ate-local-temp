# 07d: Record the resolved release chain

**Parent:** 07 (Media Release dependencies) — `07-media-release-dependencies.md`

**What to build:** Every Media Release in the resolved chain is recorded with the run.

**Blocked by:** 07b (Transitive resolution and the `require` search chain), 06b (SHA-256 checks and `media_release`/`run_file` rows)

**Status:** ready-for-agent

- [ ] Every resolved release in the chain is recorded in `media_release` with its `.atedef` SHA-256
- [ ] Seam-1 tests read the chain back

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
