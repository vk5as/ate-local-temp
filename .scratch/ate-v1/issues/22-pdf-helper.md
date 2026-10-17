# 22: PDF via the helper process

**What to build:** Each report is also written as PDF by a separate ATE helper process, so the GUI never stalls.

**Blocked by:** 21 (HTML Test Report at run end)

**Status:** ready-for-agent

- [ ] A helper process loads the saved HTML file (never `setHtml`) in QWebEngine and runs `printToPdf` with `QPageLayout` margins equal to the CSS `@page` margins; A4 or Letter
- [ ] Page furniture via CSS `@page` margin boxes with "Page X of Y"
- [ ] At run end HTML then PDF are written; if the PDF fails the HTML remains and the error is logged
- [ ] An integration test renders a generated HTML report to PDF

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
