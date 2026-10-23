# 16b: TCP `read` and `write`

**Parent:** 16 (TCP Devices and `ate.devices`) — `16-tcp-devices.md`

**What to build:** Raw byte I/O with terminators and per-call overrides.

**Blocked by:** 16a (Device registry, `open`/`close` and TCP connect)

**Status:** ready-for-agent

- [ ] `read` (`size`, `timeout`, `readTerm`) and `write` (`data`, `writeTerm`) with per-call overrides; terminators stripped on read and appended on write; raw bytes
- [ ] Result tables per `ate-table-reference`: `{data, count, partial}`; partial read on timeout after some bytes with a warning; `err="timeout"` with no bytes; `{ok, count}` for write
- [ ] Seam-1 tests with a loopback peer cover each result shape

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
