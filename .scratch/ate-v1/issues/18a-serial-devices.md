# 18a: SERIAL Devices

**Parent:** 18 (SERIAL and UDP Device Types) — `18-serial-and-udp-devices.md`

**What to build:** Schedules talk to serial Devices with the same API as TCP.

**Blocked by:** 17b (Background failures and write timeouts)

**Status:** resolved

- [x] `SERIAL` opens with pyserial `serial_for_url`, so `port` accepts COM/tty names and pyserial URLs; fields `port`, `baud`, `dataBits`, `stopBits`, `parity`, `flowControl`, `timeout`, `readTerm`, `writeTerm`
- [x] `parity` and `flowControl` take the `Parity` and `FlowControl` enum values (the enums already exist), `dataBits` 5–8 and `stopBits` 1, 1.5 or 2; a bad value raises and names the field

(The `Serial(port)` builder moved to 16d on 2026-10-26: builders live in the `ate.devices` module that 16d creates.)
- [x] Seam-1 tests use `socket://127.0.0.1:<port>` (not `localhost`: the loopback peer binds 127.0.0.1 only, and `localhost` may resolve to `::1`)

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
