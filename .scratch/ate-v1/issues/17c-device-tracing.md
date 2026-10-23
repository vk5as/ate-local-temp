# 17c: Device tracing

**Parent:** 17 (Receive buffer, Device failures and tracing) — `17-receive-buffer-and-device-tracing.md`

**What to build:** Device traffic can be traced.

**Blocked by:** 17a (Background reader, receive buffer and `ATE.flush`)

**Status:** ready-for-agent

- [ ] With `deviceTrace.enabled`, every operation is logged at `DEBUG` (source `device`) with the Device, escaped bytes truncated at `deviceTrace.maxBytes`, and timing
- [ ] Seam-1 tests check traced lines

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
