# 22b: PDF at run end

**Parent:** 22 (PDF via the helper process) — `22-pdf-helper.md`

**What to build:** Each report is also written as PDF without stalling the GUI.

**Blocked by:** 22a (PDF helper process)

**Status:** ready-for-agent

- [ ] At run end HTML then PDF are written; if the PDF fails the HTML remains and the error is logged
- [ ] Tests cover success and a failing helper

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
