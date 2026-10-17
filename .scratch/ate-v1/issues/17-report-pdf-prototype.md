# Render a sample Test Report to PDF

Type: prototype
Status: resolved
Blocked by: 13

## Question

Which engine turns the HTML Test Report into PDF: **QWebEngine `printToPdf`** or **xhtml2pdf** (with Playwright as the fallback)? Build one realistic sample report to `assets/test-report-format.md` and render it with each engine on Windows, then on Linux. Judge them on:

- Fidelity of the cover, the step table (indented header rows, Attempt sub-rows, the smaller message line), the signature box, and the embedded logo.
- Page furniture: a header and footer on every page, "Page X of Y", and step table headings that repeat after page breaks. With QWebEngine, check whether CSS `@page` margin boxes work in the bundled Chromium, or whether the header and footer must be done another way.
- Install size added on Windows, and render time for a long run (hundreds of steps, thousands of log lines).
- Whether rendering can run headless at run end without blocking the Run screen. QWebEngine is asynchronous and needs the event loop.

From "Options for turning HTML reports into PDF on Windows and Linux" (03): WeasyPrint and wkhtmltopdf are ruled out.

## Answer

**QWebEngine `printToPdf`**, run in a separate ATE helper process. Recorded in `assets/test-report-format.md`.

- **Engine**: QWebEngine (PyQt6-WebEngine, Chromium). On both Windows and Linux it gave the better-looking output, and it renders the very HTML file ATE saves, so there is one source. xhtml2pdf and Playwright are not used.
- **Helper process**: ATE starts a separate copy of itself to turn the saved HTML into PDF. In the GUI process, Windows showed GUI stalls of 0.35–0.42 s per render, and 8 s on the first cold render.
- **Page furniture**: CSS `@page` margin boxes, with `counter(page)` / `counter(pages)` and a data-URI logo. The `QPageLayout` margins must match the CSS `@page` margins, because Chromium takes them from Qt.
- **Load the saved file**, never `setHtml`: past about 1.5 MB, `setHtml` silently produces a blank page.
- **Cost**: about 341 MB of WebEngine files on Windows. Render time on Windows: 2.4 s for 40 steps, 11.6 s for 500, and 23 s for 1,000 steps with 10,000 log lines.
- **New from feedback**: reports include the Run log by default. When regenerating from History, a toggle can leave the Run log out. Exporting a run's log to another format is a new ticket, "Export a Test Run's log from History" (18).

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

### Windows run and feedback, 2026-10-18

Run by the user on Windows from `run_windows.bat`; results in their `out\results.txt`.

| Run | QWebEngine | xhtml2pdf (thread) | xhtml2pdf (process) |
| --- | --- | --- | --- |
| 40 steps | 2.4 s, 9 pages | 8.8 s | 9.9 s |
| 500 steps, 5,000 lines | 11.6 s, 124 pages | 96 s | 89 s |
| 1,000 steps, 10,000 lines | 23.3 s, 246 pages | not run | not run |
| Longest GUI stall | 8.0 s on the first (cold) render, then 0.35–0.42 s | 0.06–0.3 s | 0.05–0.17 s |
| Install size | 341 MB | 40 MB with dependencies | |

- QWebEngine looks nicer than xhtml2pdf.
- Rendering runs in a helper process (the user chose this over rendering in the GUI process), so the Run and History screens never stall.
- Reports include the log by default. When regenerating from History, a toggle can exclude the Run log section, which helps when it gets large.
- The log should be exportable to another format. That is a new ticket.
