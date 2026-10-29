# 29c: Example: flow, Sub-schedules and modules

**Parent:** 29 (Example Media Release demonstrating every Test Schedule feature) — `29-example-media-release.md`

**What to build:** Demonstrate flow control, Sub-schedules and modules.

**Blocked by:** 29a (Example Media Release scaffold and basics), 15e (Pre/Post-test Action ordering across the tree), 06d (Bundled `ate.` modules)

**Status:** resolved

- [x] Flow: a step that fails on its first Attempt and passes on Retry; Pre-test and Post-test Actions on the top-level schedule and on included Sub-schedules
- [x] Sub-schedules: a `hidden` `.tslua` included more than once with different `keyvals` and `setPrefix`, a nested include, a header with `setEnable`
- [x] Modules: `.lua` helpers loaded with `require` (derived and explicit `module` name), shared across includes, and `require("ate.devices")` builders
- [x] The harness asserts Attempts and End Reasons (Continue on fail, Stop on Fail with Retry)

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
