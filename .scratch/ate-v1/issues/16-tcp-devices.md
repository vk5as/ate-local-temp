# 16: TCP Devices and `ate.devices`

**What to build:** Schedules can open a TCP Device, read and write raw bytes with per-call overrides, handle failures through `onError`, and build params with the bundled `ate.devices` module.

**Blocked by:** 09 (Lua lockdown, errors and `ATE.run`)

**Status:** split

**Split into:** 16a, 16b, 16c, 16d — work happens in those packets; this ticket is resolved when all of them are.

- [ ] A Device registry with one driver per Device Type; `DeviceType` enum; this ticket implements `TCP`
- [ ] `open` params are all required and type-checked; unknown, missing or mistyped fields always raise naming the field
- [ ] `read` (`size`, `timeout`, `readTerm`) and `write` (`data`, `writeTerm`) with per-call overrides; terminators stripped on read and appended on write; raw bytes
- [ ] Result tables per `ate-table-reference` (`{data, count, partial}`; partial read on timeout after some bytes with a warning; `err="timeout"` with no bytes; `{ok, count}` for write; `{ok}` for close)
- [ ] `onError(opResult, opArgs)` with fallback to `open`'s handler; a non-`nil` return continues with that value; `nil` or no handler raises
- [ ] Devices last for the run; ATE closes leftovers at run end with a warning; double close is a no-op; using a closed Device fails through `onError`
- [ ] `require("ate.devices")` gives `Tcp(host, port)`, `Read()`, `Write(data)` builders with defaults and chained setters; the builder is the params table
- [ ] Seam-1 tests use a loopback TCP peer controlled by the test

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
