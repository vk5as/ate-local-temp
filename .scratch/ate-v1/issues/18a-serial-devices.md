# 18a: SERIAL Devices

**Parent:** 18 (SERIAL and UDP Device Types) — `18-serial-and-udp-devices.md`

**What to build:** Schedules talk to serial Devices with the same API as TCP.

**Blocked by:** 17b (Background failures and write timeouts)

**Status:** ready-for-agent

- [ ] `SERIAL` opens with pyserial `serial_for_url`, so `port` accepts COM/tty names and pyserial URLs; fields `port`, `baud`, `dataBits`, `stopBits`, `parity`, `flowControl`, `timeout`, `readTerm`, `writeTerm`
- [ ] `Parity` and `FlowControl` enums; `Serial(port)` builder with the documented defaults
- [ ] Seam-1 tests use `socket://localhost:<port>`

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
