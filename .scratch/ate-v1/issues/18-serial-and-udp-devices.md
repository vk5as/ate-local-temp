# 18: SERIAL and UDP Device Types

**What to build:** Schedules can talk to serial Devices and UDP peers with the same API and result shapes as TCP.

**Blocked by:** 17 (Receive buffer, Device failures and tracing)

**Status:** split

**Split into:** 18a, 18b — work happens in those packets; this ticket is resolved when all of them are.

- [ ] `SERIAL` opens with pyserial `serial_for_url`, so `port` accepts COM/tty names and pyserial URLs; fields `port`, `baud`, `dataBits`, `stopBits`, `parity`, `flowControl`, `timeout`, `readTerm`, `writeTerm`
- [ ] `Parity` and `FlowControl` enums
- [ ] `UDP` with `host`, `port`, `localPort`, `timeout`, no terminators; one datagram per read; never partial except when truncated by `size`; buffer is a queue of datagrams
- [ ] `Serial(port)` and `Udp(host, port)` builders with the documented defaults
- [ ] Seam-1 tests use `socket://localhost:<port>` for SERIAL and a loopback UDP peer

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
