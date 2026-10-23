# 29d: Example: Devices with simulator peers

**Parent:** 29 (Example Media Release demonstrating every Test Schedule feature) — `29-example-media-release.md`

**What to build:** Demonstrate every Device Type against small simulator peers shipped with the example.

**Blocked by:** 29a (Example Media Release scaffold and basics), 18a (SERIAL Devices), 18b (UDP Devices), 19 (VISA Device Type)

**Status:** ready-for-agent

- [ ] A TCP/UDP echo or SCPI-style peer; `SERIAL` via pyserial `socket://`; `VISA` via pyvisa-py `TCPIP::<host>::<port>::SOCKET` against the SCPI-style peer
- [ ] Each Device opened, written, read (incl. a partial read and a timeout), flushed and closed; `onError` handlers that continue and that return `nil`; per-call overrides; a Device opened in a Pre-test Action and used by later steps
- [ ] The harness runs them green with no real hardware

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
