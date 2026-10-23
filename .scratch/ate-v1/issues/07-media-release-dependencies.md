# 07: Media Release dependencies

**What to build:** Test Definitions can depend on other installed Media Releases by name and semver range, and `require` finds modules through the resolved chain.

**Blocked by:** 06 (`require`, checksums and mismatch handling)

**Status:** split

**Split into:** 07a, 07b, 07c, 07d — work happens in those packets; this ticket is resolved when all of them are.

- [ ] `depends` entries with npm-style ranges (exact, `^`, `~`, bounds) resolve at scan time to the highest installed match; pre-releases only when the range names one
- [ ] Resolution is transitive; `require` searches the release's own files, then each dependency depth first in declaration order
- [ ] No match, a cycle, a module name found in more than one release of the chain, an ambiguous match (same name and version, different Media Numbers) or a broken dependency makes the Test Definition broken
- [ ] Every resolved release in the chain is recorded in `media_release` with its `.atedef` SHA-256
- [ ] Seam-1 tests cover each resolution and error case

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
