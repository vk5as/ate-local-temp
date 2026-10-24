# 16c: `onError` handling

**Parent:** 16 (TCP Devices and `ate.devices`) — `16-tcp-devices.md`

**What to build:** Device failures go through `onError`.

**Blocked by:** 16b (TCP `read` and `write`)

**Status:** resolved

- [x] `onError(opResult, opArgs)` with fallback to `open`'s handler; a non-`nil` return continues with that value; `nil` or no handler raises
- [x] Using a closed Device fails through `onError`
- [x] Seam-1 tests cover each path

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
