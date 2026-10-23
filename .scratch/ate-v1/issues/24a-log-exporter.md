# 24a: Run log exporter

**Parent:** 24 (Run log export) — `24-run-log-export.md`

**What to build:** A finished run's Run log can be exported in three formats.

**Blocked by:** 09 (Lua lockdown, errors and `ATE.run`)

**Status:** ready-for-agent

- [ ] CSV (UTF-8 with BOM), JSON Lines and Text writers; rows in recorded order with the fields and formats in `log-export` (local time with ms and offset, owner, schedule, Step Number, Step Name, Attempt, text); Level, Source and Owner filters
- [ ] Seam-1 tests export each format with filters and check the output

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
