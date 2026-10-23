# 07c: Dependency errors break the Test Definition

**Parent:** 07 (Media Release dependencies) — `07-media-release-dependencies.md`

**What to build:** Bad dependency graphs are reported as broken Test Definitions in the catalog.

**Blocked by:** 07b (Transitive resolution and the `require` search chain)

**Status:** ready-for-agent

- [ ] No match, a cycle, a module name found in more than one release of the chain, an ambiguous match (same name and version, different Media Numbers) or a broken dependency makes the Test Definition broken, with a message
- [ ] Seam-1 tests cover each error case

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
