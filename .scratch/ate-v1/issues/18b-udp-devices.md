# 18b: UDP Devices

**Parent:** 18 (SERIAL and UDP Device Types) — `18-serial-and-udp-devices.md`

**What to build:** Schedules talk to UDP peers.

**Blocked by:** 17b (Background failures and write timeouts)

**Status:** resolved

- [x] `UDP` with `host`, `port`, `localPort`, `timeout`, no terminators; one datagram per read; never partial except when truncated by `size`; buffer is a queue of datagrams
(The `Udp(host, port)` builder moved to 16d on 2026-10-26: builders live in the `ate.devices` module that 16d creates.)
- [x] Seam-1 tests use a loopback UDP peer

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
