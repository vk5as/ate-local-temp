# 06a: Replacement `require`: resolution and caching

**Parent:** 06 (`require`, checksums and mismatch handling) — `06-require-and-checksums.md`

**What to build:** Schedules load helper modules through ATE's replacement `require`, which only knows the modules listed in the Test Definition.

**Blocked by:** 05 (Test Definition catalog and `ate definition update`)

**Status:** ready-for-agent

- [ ] `require` resolves only module names listed in the Test Definition (dependencies come later), never `package.path`; unknown names raise naming the module
- [ ] Modules run once per Test Run and are cached; a second `require` of the same name returns the cached value
- [ ] Seam-1 tests cover resolution, unknown names and caching

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
