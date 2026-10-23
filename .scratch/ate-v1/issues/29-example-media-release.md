# 29: Example Media Release demonstrating every Test Schedule feature

**What to build:** One example Media Release, described by a single `.atedef`, whose `.tslua` Test Schedules and `.lua` modules together demonstrate everything a Test Schedule can do. It serves as living documentation for test authors and as a regression suite: every schedule runs green in CI through the headless run session, with scripted Operator answers, and needs no real hardware. Device demos talk to small simulator peers shipped with the example: a TCP/UDP echo or SCPI-style peer, the same peer reached by `SERIAL` through a pyserial `socket://` URL, and `VISA` through pyvisa-py's `TCPIP::<host>::<port>::SOCKET` resource against the SCPI-style peer.

**Blocked by:** 10 (Step Inputs), 12 (Stop on Fail, the failure prompt and Retry), 14 (Pre-test and Post-test Actions and the status bar), 15 (Sub-schedules), 17 (Receive buffer, Device failures and tracing), 18 (SERIAL and UDP Device Types), 19 (VISA Device Type), 20 (Equipment), 21 (HTML Test Report at run end)

**Status:** split

**Split into:** 29a, 29b, 29c, 29d, 29e — work happens in those packets; this ticket is resolved when all of them are.

- [ ] One `.atedef` lists every example file with checksums produced by `ate definition update`, and the catalog scans it with no broken entries and no mismatches
- [ ] Schedule features: `ScheduleParams` with name, description, doc title, id and revision, `setStopOnFail`; `StepParams(name)` with Step Numbers, `setEnable` defaults (including a step disabled by default)
- [ ] Step Inputs: one step per type and widget (`ENUM` with `RADIOBUT`, `CHECKBOX` and `PUSHBUT`; `INTEGER` and `FLOAT` with ranges; `STRING` with a pattern; `PROMPT`; `POLL` read in a loop), using `tooltip` and `orient`
- [ ] Results: steps reporting each `Result` value through `ATE.report` (with `msg`, `actual`, `expected` as numbers, strings and booleans, and a step updating its report more than once), a step that never reports (`NOREPORT`), and a step that raises a Lua error (`ERROR`)
- [ ] Flow: a step that fails on its first Attempt and passes on Retry; Pre-test and Post-test Actions on the top-level schedule and on included Sub-schedules
- [ ] Sub-schedules: a `hidden` `.tslua` included more than once with different `keyvals` and `setPrefix`, a nested include, a header with `setEnable`
- [ ] Modules: `.lua` helpers loaded with `require` (derived module name and an explicit `module` name), shared across includes, and `require("ate.devices")` builders
- [ ] Devices: `TCP`, `UDP`, `SERIAL` and `VISA` each opened, written, read (including a partial read and a timeout), flushed and closed; `onError` handlers that continue with a value and that return `nil`; per-call overrides; a Device opened in a Pre-test Action and used by later steps
- [ ] Other `ATE` calls: `ATE.log` at every level and `print`, `ATE.sleep`, `ATE.addEquipment`, reading `ATE.run`
- [ ] Each example file has short comments explaining what it demonstrates, so a test author can learn from it
- [ ] Seam-1 tests run every visible example schedule headless with scripted inputs and failure-prompt choices (including Continue on fail and Stop on Fail with Retry) and assert the recorded results, Attempts, input values, Equipment, End Reason and that a Test Report is generated
- [ ] The example lives in the repository and is runnable from a source checkout by pointing a config's Test Definition directory at it; it isn't installed by default

Not covered here: Media Release dependencies (they need a second `.atedef`) and checksum mismatches (tests can demonstrate these by copying and altering the example).

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
