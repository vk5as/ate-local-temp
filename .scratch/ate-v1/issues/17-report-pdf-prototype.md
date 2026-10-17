# Render a sample Test Report to PDF

Type: prototype
Status: claimed
Blocked by: 13

## Question

Which engine turns the HTML Test Report into PDF: **QWebEngine `printToPdf`** or **xhtml2pdf** (with Playwright as the fallback)? Build one realistic sample report to `assets/test-report-format.md` and render it with each engine on Windows, then on Linux. Judge them on:

- Fidelity of the cover, the step table (indented header rows, Attempt sub-rows, the smaller message line), the signature box, and the embedded logo.
- Page furniture: a header and footer on every page, "Page X of Y", and step table headings that repeat after page breaks. With QWebEngine, check whether CSS `@page` margin boxes work in the bundled Chromium, or whether the header and footer must be done another way.
- Install size added on Windows, and render time for a long run (hundreds of steps, thousands of log lines).
- Whether rendering can run headless at run end without blocking the Run screen. QWebEngine is asynchronous and needs the event loop.

From "Options for turning HTML reports into PDF on Windows and Linux" (03): WeasyPrint and wkhtmltopdf are ruled out.

## Comments

### Prototype, 2026-10-18

Asset: `prototypes/report-pdf/` on branch `prototype/report-pdf` (throwaway, never merged). `README.md` there explains how to run it. `samples/` holds the 40-step PDF from each engine and the HTML they were rendered from. `run_windows.bat` runs the whole comparison on Windows and writes `out\results.txt`.

One Jinja2 template follows `assets/test-report-format.md`: the cover with banner, verdict, counts, failures and signature box; steps with Sub-schedule header rows, retry sub-rows, inputs and overrides; Equipment; Configuration with a mismatch row; and the Run log. Each engine was run inside a live Qt app with a 10 ms heartbeat to measure GUI stalls. Linux only so far (Debian 13, CPython 3.13, PyQt6 6.11 with Chromium 140, xhtml2pdf 0.2.26).

- **Fidelity**: both render every element of the cover and step table acceptably, and the output is close. xhtml2pdf has looser spacing, ignores `border-collapse`, and wraps the Expected column earlier. Text is searchable in both.
- **Page furniture**: both draw a header (with logo) and a footer with "Page X of Y" on every page, and both repeat the step table headings after page breaks. In QWebEngine, CSS `@page` margin boxes work, including `counter(pages)` and a data-URI logo, **but** Chromium takes the page margins from the `QPageLayout`, not from CSS; they must be passed to match, or the header and footer collapse. xhtml2pdf needs its own `@frame` and `<pdf:pagecount/>` markup, so its HTML differs from the HTML file ATE saves.
- **Fonts**: xhtml2pdf knows only the 14 base PDF fonts, so Ω, →, ↻ and ⚠ need a bundled TTF through `@font-face` (DejaVu, about 1.4 MB). Chromium uses system fonts.
- **Speed** (render only): 40 steps take 0.3 s with QWebEngine and 1.7 s with xhtml2pdf. 500 steps with 5,000 log lines take 3.1 s and 35 s. 1,000 steps with 10,000 lines take 6.5 s and 95 s. QWebEngine renders 2,000 steps with 20,000 lines in 14.5 s.
- **Not blocking the Run screen**: QWebEngine renders out of process, with about 80 ms of GUI stall once a page has been created at startup (the first page adds about 40 ms more). xhtml2pdf in a `QThread` stalls the GUI 280–550 ms through the GIL; in a worker process the stall is about 15 ms.
- **Gotcha**: `QWebEnginePage.setHtml` silently produces a blank one-page PDF once the report passes about 1.5 MB (2 MB once percent-encoded). Loading the written HTML file avoids this.
- **Install size** (Linux venv): about 264 MB for WebEngine files, against about 37 MB for xhtml2pdf with its dependencies. Windows not measured yet.
- **Playwright** was not built. It is the same Chromium as QWebEngine, so it is only needed if QWebEngine fails on Windows.
- **Still open**: the Windows run (human), and the verdict.
