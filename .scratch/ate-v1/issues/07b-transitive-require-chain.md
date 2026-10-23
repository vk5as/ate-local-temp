# 07b: Transitive resolution and the `require` search chain

**Parent:** 07 (Media Release dependencies) — `07-media-release-dependencies.md`

**What to build:** Dependencies resolve transitively and `require` finds modules through the resolved chain.

**Blocked by:** 07a (Media Release `depends` ranges)

**Status:** ready-for-agent

- [ ] Resolution is transitive; `require` searches the release's own files, then each dependency depth first in declaration order
- [ ] Seam-1 tests cover a two-level chain and the search order

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
