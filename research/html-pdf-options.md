# Options for rendering an HTML Test Report to PDF (Python + PyQt6, Windows and Linux)

Research for `.scratch/ate-v1/issues/03-html-pdf-options.md`. This file records facts only. It does not recommend an option; that choice belongs to a later ticket.

Gathered 2026-10-16. Versions and sizes come from the PyPI JSON API (`https://pypi.org/pypi/<name>/json`) on that date. Wheel sizes are download sizes, not installed sizes.

Context: ATE generates a Test Report as HTML and renders the PDF from that HTML, either at the end of a Test Run or later on demand from the History screen. The app is PyQt6 and runs on Windows (primary) and Linux.

---

## 1. Qt rich text: `QTextDocument` + `QPrinter` / `QPdfWriter`

**What it is.** `QTextDocument.setHtml()` parses HTML into Qt's rich-text model. `QTextDocument.print(QPagedPaintDevice)` then writes it to a `QPdfWriter` (in Qt GUI) or to a `QPrinter` in `PdfFormat` (in Qt Print Support).

**CSS / HTML support**
- The engine supports only a subset of HTML 4. The docs say "The functionality implemented for tags listed below is a subset of the full HTML 4 specification." [Qt: Supported HTML Subset](https://doc.qt.io/qt-6/richtext-html-subset.html)
- The CSS properties are limited to colour, background, font, text, border, padding, margin and line-height, plus `-qt-*` extensions. Some properties, such as margins, apply only to block elements. (same source)
- Tables are supported, including `colspan`/`rowspan`. "If the thead tag is specified, it is used when printing tables that span multiple pages." (same source)
- `page-break-before` and `page-break-after` are supported. "The float property is only supported for tables and images." (same source)
- Selectors: "All CSS 2.1 selector classes are supported except pseudo-class selectors such as :first-child, :visited and :hover." (same source)
- The supported-subset page does not mention flexbox, grid, `@page` or `<link rel=stylesheet>`. Treat them as unsupported.
- `setDefaultStyleSheet()` accepts a CSS 2.1-syntax stylesheet. [Qt: QTextDocument](https://doc.qt.io/qt-6/qtextdocument.html)
- `print()` paginates by the device's paper rect. If the document has no `pageSize` set, Qt "adds a default 2 cm margin and prints the current page number at the bottom of each page". (same source; paraphrased by the fetch tool)

**PDF output**
- Both `QPdfWriter` and `QPrinter` can produce the formats in `QPagedPaintDevice::PdfVersion`: `PdfVersion_1_4` (the default), `PdfVersion_A1b` (PDF/A-1b), `PdfVersion_1_6` (Qt 5.12+) and `PdfVersion_X4` (PDF/X-4, Qt 6.8+). [Qt: QPagedPaintDevice](https://doc.qt.io/qt-6/qpagedpaintdevice.html), [Qt 6.8: QPdfWriter](https://doc.qt.io/qt-6.8/qpdfwriter.html)
- `QPrinter::PdfFormat`: "QPrinter will generate its output as a searchable PDF file." [Qt: QPrinter](https://doc.qt.io/qt-6/qprinter.html)

**Dependencies / footprint**
- Nothing beyond PyQt6, which the app already ships. The CMake target for `QTextDocument` and `QPdfWriter` is `Qt6::Gui`; `QPrinter` is in `Qt6::PrintSupport`. Both are inside the `PyQt6-Qt6` wheel. Reference sizes for that wheel at 6.11.2: win_amd64 78.0 MB, manylinux x86_64 86.0 MB. [PyPI: PyQt6-Qt6](https://pypi.org/project/PyQt6-Qt6/)

**Headless / no-GUI use**
- QGuiApplication "must be constructed before any paint devices". This means a `QGuiApplication` must exist, but no window or visible GUI is needed. [Qt: QGuiApplication](https://doc.qt.io/qt-6/qguiapplication.html)
- For machines with no display, the `-platform` option and `QT_QPA_PLATFORM` select a QPA plugin. `offscreen` and `minimal` are listed; `minimal` is described as usable "to run GUI applications in environments without a GUI, such as servers." (same source)
- Rendering is synchronous and runs in-process.

**Licence**
- It is part of Qt / PyQt6. PyQt6 is "dual licensed ... under the Riverbank Commercial License and the GPL v3." [Riverbank: PyQt6 introduction](https://www.riverbankcomputing.com/static/Docs/PyQt6/introduction.html) PyPI metadata for `PyQt6` 6.11.0 is `GPL-3.0-only`. The Qt binaries (`PyQt6-Qt6`) are LGPL v3. [PyPI: PyQt6](https://pypi.org/project/PyQt6/)
- This option adds no licence beyond what the app already carries.

---

## 2. Qt WebEngine: `QWebEnginePage.printToPdf`

**What it is.** A Chromium engine embedded in Qt. You load HTML into a `QWebEnginePage` with `setHtml` or `load`, then call `printToPdf`.

**API (Qt WebEngine 6.x docs)** [Qt: QWebEnginePage](https://doc-snapshots.qt.io/qtwebengine/qwebenginepage.html)
- `printToPdf(filePath, layout = A4 portrait, ranges = {})`: "issues an asynchronous request for printing the web page into a PDF and returns immediately." The result arrives through the `pdfPrintingFinished(filePath, success)` signal.
- `printToPdf(resultCallback, layout, ranges)`: passes the PDF bytes to the callback as a `QByteArray`, which is empty on failure.
- "The page size and orientation of the produced PDF document are taken from the values specified in layout". The page range comes from `ranges`.
- Both overloads are asynchronous, so a Qt event loop must be running. The page must finish loading (`loadFinished`) before printing. The second point is an inference from the API shape; the docs do not state it.
- "Qt WebEngine supports printing a web page to a PDF file ... Chromium provides the implementation." [Qt: WebEngine features](https://doc-snapshots.qt.io/qtwebengine/qtwebengine-features.html)

**CSS / HTML support**
- This is the bundled Chromium, so modern CSS is available: flexbox, grid, `@media print`, `@page` size/margins and JavaScript. The Qt docs do not give a CSS feature list or the Chromium version on the pages consulted.

**Dependencies / footprint**
- It needs the separate `PyQt6-WebEngine` binding (about 0.3 MB) plus the `PyQt6-WebEngine-Qt6` binary wheel. At 6.11.2 that wheel is **132.4 MB on win_amd64** and 116.3 MB on manylinux x86_64, in addition to `PyQt6-Qt6`. [PyPI: PyQt6-WebEngine](https://pypi.org/project/PyQt6-WebEngine/), [PyPI: PyQt6-WebEngine-Qt6](https://pypi.org/project/PyQt6-WebEngine-Qt6/)
- Wheels exist for win_amd64, win_arm64, manylinux_2_34 x86_64, manylinux_2_39 aarch64 and macOS. No compiler or external install is needed with pip.
- The `PyQt6-WebEngine` wheel version must match the PyQt6 version: it declares `PyQt6-WebEngine-Qt6 >=6.11.0,<6.12.0`. (PyPI metadata)
- Chromium runs a helper process (`QtWebEngineProcess`). The platform-notes page does not cover deployment. Packaging is out of scope for this ticket and is listed under "Not yet specified" in the map.

**Headless / no-GUI use**
- `QWebEnginePage` can be used without a `QWebEngineView`, but it needs a running Qt event loop.
- Linux: the Chromium sandbox needs seccomp-bpf and unprivileged user namespaces. On Debian, `unprivileged_userns_clone` is off by default. Ubuntu AppArmor may restrict namespaces. The sandbox can be disabled with `QTWEBENGINE_DISABLE_SANDBOX=1` or `--no-sandbox`. [Qt: WebEngine platform notes](https://doc-snapshots.qt.io/qtwebengine/qtwebengine-platform-notes.html)
- WebEngine falls back to software rendering if GPU acceleration is unavailable. You can force software rendering with `QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu`. [Qt: WebEngine features](https://doc-snapshots.qt.io/qtwebengine/qtwebengine-features.html)
- On Windows, Qt WebEngine "does not compile with MinGW", so only MSVC builds exist. This matters only if Qt is built from source; the PyPI wheels are prebuilt. (platform notes)

**Licence**
- The Qt-specific parts are available commercially or under LGPLv3 / GPLv2 / GPLv3. Chromium's bundled third-party code uses various licences, the most restrictive being LGPL 2.1. [Qt: WebEngine licensing](https://doc-snapshots.qt.io/qtwebengine/qtwebengine-licensing.html)
- The `PyQt6-WebEngine` binding is `GPL-3.0-only` (or Riverbank commercial), the same as PyQt6. The `PyQt6-WebEngine-Qt6` binaries are LGPL v3. (PyPI metadata)

---

## 3. WeasyPrint

**What it is.** A pure-Python HTML/CSS layout engine built for paged media. It is not a browser: "Rather than relying on a full browser engine like WebKit or Gecko", it uses smaller libraries, and Pango handles text. [PyPI: weasyprint](https://pypi.org/project/weasyprint/)

**Version / licence**
- 70.0, released 2026-09-08. BSD licence. `requires_python >=3.10`. The `main`-branch docs already list Python ≥ 3.11 for the next release. (PyPI; [WeasyPrint docs source on main](https://github.com/Kozea/WeasyPrint/blob/main/docs/first_steps.rst))

**CSS support** [WeasyPrint: API reference, Supported Features](https://doc.courtbouillon.org/weasyprint/stable/api_reference.html)
- CSS 2.1 "is pretty well supported", and it passes Acid2.
- Paged Media Level 3 is largely complete: `@page` selectors, margin boxes (running headers and footers), page counters, `size`, and named pages. Generated Content for Paged Media is supported, including running elements and footnotes.
- Flexbox "works for simple use cases but is not deeply tested."
- Grid "works for simple cases, but has some limitations". There is no subgrid, `auto-fill`/`auto-fit`, or fragmentation between rows.
- Multi-column layout covers basic cases only. 2D transforms are supported; 3D transforms are not.
- Bidirectional and right-to-left text are not supported.
- The Supported Features list does not mention JavaScript, and nothing indicates it is executed.

**Dependencies**
- Python dependencies, all wheels from PyPI: `pydyf`, `cffi`, `tinyhtml5`, `tinycss2`, `cssselect2`, `Pyphen`, `Pillow`, `fonttools[woff]`. The WeasyPrint wheel itself is 0.3 MB. (PyPI metadata)
- **Native: Pango ≥ 1.44.0** and its dependency chain (GLib, HarfBuzz, fontconfig, and so on), loaded at runtime via cffi. pip does not install these. [WeasyPrint: First steps](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html)

**Windows install** ([WeasyPrint v70.0 first_steps.rst](https://github.com/Kozea/WeasyPrint/blob/v70.0/docs/first_steps.rst))
- "To use WeasyPrint on Windows, the easiest way is to use the executable of the latest release." This is a standalone executable from GitHub Releases.
- To use it as a Python library: "you'll have to follow a few extra steps. Please read this chapter carefully." The steps are: install MSYS2, run `pacman -S mingw-w64-ucrt-x86_64-pango` in the UCRT64 shell, then `pip install weasyprint` in a venv.
- If the DLLs are not found, set `WEASYPRINT_DLL_DIRECTORIES` to the folder that contains them, for example `C:\msys64\ucrt64\bin`.
- Warning in the docs: "WeasyPrint is regularly marked as malware by different antivirus companies." (issues #2081, #2092)
- Missing fonts show up as squares. The fix is to install fonts or use `@font-face`.

**Linux install**
- Packaged for Debian, Ubuntu, Fedora, Arch and Gentoo. Otherwise pip works as long as the system Pango is ≥ 1.44; for older Pango, the docs suggest WeasyPrint 52.5. (first_steps)

**Headless / no-GUI use**
- Fully headless. It is a library call (`HTML(string=...).write_pdf()`) or a CLI. It does not depend on Qt or a display. Rendering is synchronous and runs in-process.

---

## 4. xhtml2pdf (ReportLab-based)

**What it is.** "Pure Python on top of ReportLab, with no browser to install or keep running." [PyPI: xhtml2pdf](https://pypi.org/project/xhtml2pdf/)

**Version / licence**
- 0.2.24, released 2026-10-15. Apache-2.0. `requires_python >=3.10`, with classifiers for 3.10 through 3.14. (PyPI)

**CSS support** (as claimed on the 0.2.24 PyPI description)
- Flexbox, "including containers that break across pages". Also `inline-block`, `position` relative/absolute/fixed with `z-index`, and `border-radius`.
- Tables whose header rows repeat on every page, with `colspan`/`rowspan`. `page-break-before`/`after` and `page-break-inside: avoid`.
- Selectors up to Level 4 (`:not`, `:is`, `:where`, `:has`, `:nth-child`), custom properties via `var()`, and `@font-face` (TTF/OTF).
- `@page` sizes and margins, named page templates, and frames for headers and footers. Headers and footers use the xhtml2pdf-specific `@frame` / `-pdf-frame-content` syntax and `<pdf:pagenumber/>` / `<pdf:pagecount/>` tags rather than CSS margin boxes.
- These claims come from the project's own README for a version released the day before this research. The feature list is much broader than historical xhtml2pdf releases. It has not been independently verified.

**Dependencies**
- All from PyPI: `reportlab>=4.4.9,<6`, `html5lib`, `Pillow`, `pypdf`, `pyHanko`, `pyhanko-certvalidator`, `svglib`, `arabic-reshaper`. (PyPI metadata)
- ReportLab 5.0.1 is a pure-Python wheel (2.0 MB, BSD). [PyPI: reportlab](https://pypi.org/project/reportlab/)
- Native: none required. Cairo is optional, only for the `pycairo` extra (vector-to-bitmap rendering).

**Windows install**
- `pip install xhtml2pdf`, with no native libraries required.

**Headless / no-GUI use**
- Fully headless. It offers a library call (`pisa.CreatePDF`) or a CLI, and runs synchronously in-process.

---

## 5. Headless Chromium via Playwright (`page.pdf()`)

**What it is.** Playwright drives a separately downloaded Chromium. `page.set_content(html)` followed by `page.pdf(...)` produces the PDF.

**Version / licence**
- `playwright` 1.64.0, released 2026-10-13. Apache-2.0. `requires_python >=3.10`. Wheels are 38.7 MB on win_amd64 and 48.3 MB on manylinux x86_64, and they bundle the Node driver. (PyPI) Chromium itself is BSD-3-Clause; this was not looked up in this session.

**PDF support**
- `page.pdf()` "generates a pdf of the page with `print` css media." [Playwright: Page.pdf (docs source)](https://github.com/microsoft/playwright/blob/main/docs/src/api/class-page.md)
- Options include `format`, `width`/`height`, `margin`, `display_header_footer`, `header_template`/`footer_template`, `prefer_css_page_size` ("Give any CSS @page size declared in the page priority over what is declared in width and height or format options"), `print_background` (defaults to false), `tagged` (accessible PDF, v1.42+) and `outline` (v1.42+). [Playwright Python: class Page](https://playwright.dev/python/docs/api/class-page)
- **Chromium only.** `CRPage` implements `pdf()`, and the Firefox backend (`ffPage.ts`) has no pdf implementation. [crPage.ts v1.64.0](https://github.com/microsoft/playwright/blob/v1.64.0/packages/playwright-core/src/server/chromium/crPage.ts), [ffPage.ts v1.64.0](https://github.com/microsoft/playwright/blob/v1.64.0/packages/playwright-core/src/server/firefox/ffPage.ts)

**CSS support**
- Current Chromium, so full modern CSS and JavaScript.

**Dependencies / footprint** [Playwright Python: Browsers](https://playwright.dev/python/docs/browsers)
- Browsers are a separate download (`playwright install chromium`). The docs say they "take a few hundred megabytes"; the sample output shows about 281 MB for Chromium.
- `playwright install --only-shell` fetches only the Chromium headless shell, for headless-only use.
- The Windows cache is `%USERPROFILE%\AppData\Local\ms-playwright`, and `PLAYWRIGHT_BROWSERS_PATH` overrides it.
- An installed Chrome or Edge can be used instead via `channel="chrome"` / `"msedge"`. Edge is preinstalled on current Windows; this is general knowledge, not from the source.
- On Linux, `playwright install-deps` installs the required OS packages.

**Headless / no-GUI use**
- Headless is the default. It runs out of process and has both sync and async APIs. The sync API cannot run inside an already-running asyncio loop; this is a general Playwright constraint and was not looked up in this session. It is independent of Qt.

### 5a. Related: pyppeteer
- Its PyPI page says "This repo is unmaintained" and "Please consider playwright-python as an alternative." Version 2.0.0 dates from 2024-02-18. [PyPI: pyppeteer](https://pypi.org/project/pyppeteer/)

### 5b. Related: Chrome's own headless mode
- "With Chrome Headless mode, you can run the browser in an unattended environment, without any visible UI." The old headless implementation became a separate `chrome-headless-shell` binary from Chrome 132. [Chrome for Developers: Headless](https://developer.chrome.com/docs/chromium/headless), [Chromium headless README](https://chromium.googlesource.com/chromium/src/+/main/headless/README.md)
- Calling a system Chrome/Edge directly via subprocess with `--print-to-pdf` is possible in practice. Neither page consulted documents that flag, so it is not verified here.

---

## 6. wkhtmltopdf (via `pdfkit`)

- It uses the "Qt WebKit rendering engine" and is licensed LGPLv3. [wkhtmltopdf.org](https://wkhtmltopdf.org/)
- **The GitHub repository is archived.** The last push was 2022-11-22. [GitHub API: wkhtmltopdf/wkhtmltopdf](https://api.github.com/repos/wkhtmltopdf/wkhtmltopdf)
- The status page says Qt 4 is unsupported, QtWebKit was removed, and there is "effectively no upstream to contribute to". It advises "Do not use wkhtmltopdf with any untrusted HTML" and suggests WeasyPrint or Prince for reports from HTML you control. [wkhtmltopdf status](https://wkhtmltopdf.org/status.html)
- `pdfkit` 1.0.0 (2021, MIT) is only a wrapper. It needs the wkhtmltopdf binary installed separately, and on Windows that means the binary installer from wkhtmltopdf.org. [PyPI: pdfkit](https://pypi.org/project/pdfkit/)
- CSS support follows an old WebKit: no reliable flexbox or grid. This comes from the engine's age, and no single spec page was consulted.

---

## Summary table

| Option | CSS level | Native / external deps | Windows install | Extra download (win_amd64) | Headless | Licence |
|---|---|---|---|---|---|---|
| QTextDocument + QPdfWriter/QPrinter | HTML 4 subset; limited CSS 2.1 props; no flex/grid/@page | None (already in PyQt6) | Nothing extra | 0 | Needs QGuiApplication; `offscreen`/`minimal` QPA for no display; sync | GPLv3/commercial (PyQt6) + LGPLv3 (Qt) |
| QWebEnginePage.printToPdf | Modern Chromium (flex, grid, @page, JS) | Qt WebEngine (Chromium) helper process | pip wheels | ~132 MB wheel (`PyQt6-WebEngine-Qt6`) | Needs Qt event loop; async; Linux sandbox caveats | GPLv3/commercial (binding) + LGPLv3 (Qt) + Chromium third-party |
| WeasyPrint 70.0 | CSS 2.1 + Paged Media (margin boxes, counters); flex/grid "simple cases" | **Pango ≥ 1.44** (GLib, HarfBuzz, fontconfig...) | MSYS2 + pacman Pango + `WEASYPRINT_DLL_DIRECTORIES`; AV false positives | ~0.3 MB wheel + Python deps + Pango/MSYS2 runtime | Fully headless; sync, in-process | BSD |
| xhtml2pdf 0.2.24 | Claims flexbox, L4 selectors, `@page` with frames (project's own claims, newly released) | None (ReportLab, pure Python) | `pip install` | ~0.3 MB + ReportLab 2 MB + deps | Fully headless; sync, in-process | Apache-2.0 (ReportLab BSD) |
| Playwright + Chromium | Modern Chromium | Chromium browser download (or system Chrome/Edge) | pip + `playwright install chromium` | 38.7 MB wheel + ~281 MB browser (or shell only) | Headless by default; out of process | Apache-2.0 (+ Chromium) |
| wkhtmltopdf / pdfkit | Old Qt WebKit | Separate wkhtmltopdf binary | Binary installer | ~tens of MB (not measured) | Headless | LGPLv3; **repo archived** |

## Open points not settled by this research

- Measured installed sizes in a frozen Windows build (PyInstaller or similar). This depends on the packaging ticket.
- Actual fidelity of each engine on a sample Test Report. A prototype would answer this, not reading.
- Whether bundling the Pango DLLs from MSYS2 into a Windows installer is practical. The WeasyPrint docs only describe developer installs.
