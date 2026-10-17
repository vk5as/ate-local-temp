# Options for turning HTML reports into PDF on Windows and Linux

Type: research
Status: resolved
Blocked by: none

## Question

Which ways exist to render an HTML Test Report to PDF from a Python/PyQt6 app on both Windows and Linux (e.g. Qt's QTextDocument/QPrinter, QWebEngine printToPdf, WeasyPrint, others)? For each: CSS support, external dependencies, install footprint on Windows, licence.

## Answer

Findings: `research/html-pdf-options.md` on branch `research/html-pdf-options` (throwaway, never merged).

- **Qt QTextDocument + QPdfWriter**: no new dependencies, synchronous, but only limited CSS 2.1 (no flex, grid or `@page`).
- **QWebEngine printToPdf**: full Chromium CSS, but adds about 120–130 MB, is asynchronous (needs the event loop), and has Chromium sandbox issues on Linux.
- **WeasyPrint**: best paged-media CSS and BSD licence, but needs native Pango, which on Windows means MSYS2 and antivirus false positives.
- **xhtml2pdf**: pure pip install, Apache licence; its newest release claims much better CSS, but that is unverified.
- **Playwright + Chromium**: modern CSS in a separate process; Apache licence; about 281 MB browser download unless it drives an installed Chrome or Edge.
- **wkhtmltopdf**: archived; avoid.
- Open: how well each engine renders a real Test Report is best answered by a prototype.
