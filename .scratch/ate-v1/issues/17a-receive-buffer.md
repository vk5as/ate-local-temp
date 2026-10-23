# 17a: Background reader, receive buffer and `ATE.flush`

**Parent:** 17 (Receive buffer, Device failures and tracing) — `17-receive-buffer-and-device-tracing.md`

**What to build:** Incoming data is buffered so nothing is lost before a read.

**Blocked by:** 16c (`onError` handling), 04 (Application config in full, and interrupted-run recovery)

**Status:** ready-for-agent

- [ ] `SERIAL`/`TCP`/`UDP` Devices are read in the background from `open` to `close`; `read` takes from the buffer first
- [ ] The buffer is capped by `devices.receiveBufferBytes`; overflow drops the oldest data, logs one `WARNING` per overflow, and the next read carries `overflow = true`
- [ ] `ATE.flush(dev)` empties the buffer and returns `{ok = true, count = n}`
- [ ] Seam-1 tests cover each with a loopback TCP peer

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
