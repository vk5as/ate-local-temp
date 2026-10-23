# 28c: Linux release CI

**Parent:** 28 (Linux packages, wheel and source build instructions) — `28-linux-packages.md`

**What to build:** Tagged releases build and smoke-test the Linux deliverables.

**Blocked by:** 28a (Wheel and source build instructions), 28b (Linux tarball and `.deb`), 27c (Windows release CI)

**Status:** ready-for-agent

- [ ] Tagged releases build all three and smoke-test each in a Debian stable container

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
