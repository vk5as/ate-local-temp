# 07a: Media Release `depends` ranges

**Parent:** 07 (Media Release dependencies) — `07-media-release-dependencies.md`

**What to build:** Test Definitions declare dependencies on other installed Media Releases by name and npm-style semver range.

**Blocked by:** 06a (Replacement `require`: resolution and caching)

**Status:** ready-for-agent

- [ ] `depends` entries with npm-style ranges (exact, `^`, `~`, bounds) resolve at scan time to the highest installed match; pre-releases only when the range names one
- [ ] Seam-1 tests cover each range form and the pre-release rule

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
