# 22a: PDF helper process

**Parent:** 22 (PDF via the helper process) — `22-pdf-helper.md`

**What to build:** A separate ATE helper process turns a saved HTML report into PDF.

**Blocked by:** 21a (HTML report skeleton, naming and Cover)

**Status:** ready-for-agent

- [ ] A helper process loads the saved HTML file (never `setHtml`) in QWebEngine and runs `printToPdf` with `QPageLayout` margins equal to the CSS `@page` margins; A4 or Letter
- [ ] Page furniture via CSS `@page` margin boxes with "Page X of Y"
- [ ] An integration test renders a generated HTML report to PDF

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
