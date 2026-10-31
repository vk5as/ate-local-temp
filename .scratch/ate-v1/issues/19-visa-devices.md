# 19: VISA Device Type

**What to build:** Schedules can talk to VISA instruments, reading on demand, with pyvisa-py bundled so LAN and USBTMC instruments work without NI-VISA.

**Blocked by:** 16 (TCP Devices and `ate.devices`)

**Status:** resolved

- [x] `VISA` with `resource`, `timeout`, `readTerm`, `writeTerm` through pyvisa using whatever backend it finds (NI-VISA preferred, pyvisa-py bundled)
- [x] No background reader: `read` reads on demand; `flush` is a no-op returning `count = 0`; same result shapes
- [x] `Visa(resource)` builder
- [x] Tests register a scripted fake VISA driver in the Device registry

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
