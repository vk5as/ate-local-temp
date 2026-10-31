# 24b: Run log export dialog

**Parent:** 24 (Run log export) — `24-run-log-export.md`

**What to build:** From History, the Operator exports a run's log.

**Blocked by:** 24a (Run log exporter), 23b (History screen)

**Status:** resolved

- [x] An export dialog with Level, Source and Owner checkboxes (all ticked each time); Export disabled when nothing would be exported
- [x] Save As offering CSV (default), JSON Lines and Text, defaulting to the report folder with `_log` added
- [x] A pytest-qt test covers the dialog

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
