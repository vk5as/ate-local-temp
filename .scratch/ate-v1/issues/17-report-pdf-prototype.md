# Render a sample Test Report to PDF

Type: prototype
Status: open
Blocked by: 13

## Question

Which engine turns the HTML Test Report into PDF: **QWebEngine `printToPdf`** or **xhtml2pdf** (with Playwright as the fallback)? Build one realistic sample report to `assets/test-report-format.md` and render it with each engine on Windows, then on Linux. Judge them on:

- Fidelity of the cover, the step table (indented header rows, Attempt sub-rows, the smaller message line), the signature box, and the embedded logo.
- Page furniture: a header and footer on every page, "Page X of Y", and step table headings that repeat after page breaks. With QWebEngine, check whether CSS `@page` margin boxes work in the bundled Chromium, or whether the header and footer must be done another way.
- Install size added on Windows, and render time for a long run (hundreds of steps, thousands of log lines).
- Whether rendering can run headless at run end without blocking the Run screen. QWebEngine is asynchronous and needs the event loop.

From "Options for turning HTML reports into PDF on Windows and Linux" (03): WeasyPrint and wkhtmltopdf are ruled out.
