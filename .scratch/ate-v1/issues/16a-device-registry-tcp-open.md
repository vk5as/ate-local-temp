# 16a: Device registry, `open`/`close` and TCP connect

**Parent:** 16 (TCP Devices and `ate.devices`) — `16-tcp-devices.md`

**What to build:** Schedules open and close a TCP Device through a registry of drivers.

**Blocked by:** 09 (Lua lockdown, errors and `ATE.run`)

**Status:** ready-for-agent

- [ ] A Device registry with one driver per Device Type; `DeviceType` enum; a `TCP` driver that connects
- [ ] `open` params are all required and type-checked; unknown, missing or mistyped fields always raise naming the field
- [ ] Devices last for the run; ATE closes leftovers at run end with a warning; double close is a no-op; `close` returns `{ok}`
- [ ] Seam-1 tests use a loopback TCP peer controlled by the test

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
