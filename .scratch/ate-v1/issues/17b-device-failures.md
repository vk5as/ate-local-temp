# 17b: Background failures and write timeouts

**Parent:** 17 (Receive buffer, Device failures and tracing) — `17-receive-buffer-and-device-tracing.md`

**What to build:** Device failures are reported promptly and writes can't hang.

**Blocked by:** 17a (Background reader, receive buffer and `ATE.flush`)

**Status:** ready-for-agent

- [ ] A background reader error logs a `WARNING` (source `device`) at once and marks the Device failed; buffered data can still be read, then `read`/`write` fail through `onError`; no automatic reconnect; close then open again gives a new device
- [ ] `write` honours `timeout` (per-call override) and reports bytes actually sent on failure
- [ ] Seam-1 tests cover each with a loopback TCP peer

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
