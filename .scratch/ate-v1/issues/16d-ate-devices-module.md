# 16d: `ate.devices` builders

**Parent:** 16 (TCP Devices and `ate.devices`) — `16-tcp-devices.md`

**What to build:** The bundled `ate.devices` module builds params tables.

**Blocked by:** 16b (TCP `read` and `write`), 06d (Bundled `ate.` modules)

**Status:** ready-for-agent

- [ ] `require("ate.devices")` gives `Tcp(host, port)`, `Read()`, `Write(data)` builders with defaults and chained setters; the builder is the params table
- [ ] Seam-1 tests open, write and read through the builders

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
