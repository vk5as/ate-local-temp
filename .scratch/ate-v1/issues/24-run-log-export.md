# 24: Run log export

**What to build:** From History, a finished run's Run log can be exported for analysis in other tools.

**Blocked by:** 23 (History screen)

**Status:** ready-for-agent

- [ ] An export dialog with Level, Source and Owner checkboxes (all ticked each time); Export disabled when nothing would be exported
- [ ] Save As offering CSV (default, UTF-8 with BOM), JSON Lines and Text, defaulting to the report folder with `_log` added
- [ ] Rows in recorded order with the fields and formats in `log-export` (local time with ms and offset, owner, schedule, Step Number, Step Name, Attempt, text)
- [ ] Seam-1 tests export each format with filters and check the output

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
