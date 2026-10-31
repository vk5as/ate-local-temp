# 29a: Example Media Release scaffold and basics

**Parent:** 29 (Example Media Release demonstrating every Test Schedule feature) — `29-example-media-release.md`

**What to build:** Start the example Media Release and the CI harness that runs every visible example schedule headless.

**Blocked by:** 10 (Step Inputs), 12c (Retry and Attempt recording), 13b (`ATE.sleep` and abort-aware blocking calls), 20 (Equipment)

**Status:** resolved

- [x] One `.atedef` lists every example file with checksums produced by `ate definition update`; the catalog scans it with no broken entries and no mismatches
- [x] Schedule features (`ScheduleParams` name, description, doc title, id, revision, `setStopOnFail`; Step Numbers, `setEnable` defaults incl. a step disabled by default), every `Result` via `ATE.report` (numbers, strings, booleans, updated reports), `NOREPORT`, a Lua `ERROR`, and other calls (`ATE.log` at every level, `print`, `ATE.sleep`, `ATE.addEquipment`, reading `ATE.run`)
- [x] Each example file has short comments explaining what it demonstrates
- [x] A seam-1 harness runs every visible example schedule headless with scripted inputs and prompt choices and asserts the record; lives in the repo, runnable from a source checkout, not installed by default

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
