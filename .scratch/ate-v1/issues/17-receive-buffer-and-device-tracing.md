# 17: Receive buffer, Device failures and tracing

**What to build:** Incoming data is buffered so nothing is lost before a read, the buffer is bounded and can be flushed, background failures are reported, writes can't hang, and Device traffic can be traced.

**Blocked by:** 04 (Application config in full, and interrupted-run recovery), 16 (TCP Devices and `ate.devices`)

**Status:** ready-for-agent

- [ ] `SERIAL`/`TCP`/`UDP` Devices are read in the background from `open` to `close`; `read` takes from the buffer first
- [ ] The buffer is capped by `devices.receiveBufferBytes`; overflow drops the oldest data, logs one `WARNING` per overflow, and the next read carries `overflow = true`
- [ ] `ATE.flush(dev)` empties the buffer and returns `{ok = true, count = n}`
- [ ] A background reader error logs a `WARNING` (source `device`) at once and marks the Device failed; buffered data can still be read, then `read`/`write` fail through `onError`; no automatic reconnect; close then open again gives a new device
- [ ] `write` honours `timeout` (per-call override) and reports bytes actually sent on failure
- [ ] With `deviceTrace.enabled`, every operation is logged at `DEBUG` (source `device`) with the Device, escaped bytes truncated at `deviceTrace.maxBytes`, and timing
- [ ] Seam-1 tests cover each behaviour with a loopback TCP peer

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
