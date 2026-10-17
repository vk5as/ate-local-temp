# PROTOTYPE: Test Report to PDF (throwaway, never merged)

Answers "Render a sample Test Report to PDF" (`.scratch/ate-v1/issues/17-report-pdf-prototype.md`):
QWebEngine `printToPdf` or xhtml2pdf?

- `sample.py`: fake Test Run data shaped like `assets/test-report-format.md`, rendered with `report.html.j2`.
- `report.html.j2`: one template. `furniture` picks how the page header and footer are drawn:
  `css` (CSS `@page` margin boxes, for Chromium) or `xhtml2pdf` (`@frame` plus `<pdf:pagenumber/>`).
- `render.py`: renders inside a running Qt app with a 10 ms heartbeat and prints the render time,
  page count, PDF size and the longest GUI-thread stall.
- `fonts/`: DejaVu Sans, which xhtml2pdf needs for Ω, →, ↻ and ⚠ (it only knows the 14 base PDF fonts).

## Run

```
python -m venv .venv
.venv/bin/pip install PyQt6 PyQt6-WebEngine xhtml2pdf jinja2 pypdf      # Windows: .venv\Scripts\pip
python render.py --engine qwe                 # QWebEngine
python render.py --engine x2p-thread          # xhtml2pdf in a QThread
python render.py --engine x2p-process         # xhtml2pdf in a worker process
python render.py --engine qwe --steps 1000 --logs 10000
```

Output goes to `out/`. On Windows, `run_windows.bat` does all of the above and records the timings in `out\results.txt`.
On headless Linux: `QT_QPA_PLATFORM=offscreen QTWEBENGINE_DISABLE_SANDBOX=1`.

## Linux results (Debian 13, CPython 3.13, PyQt6 6.11 / Chromium 140, xhtml2pdf 0.2.26)

| Run | QWebEngine | xhtml2pdf (thread) | xhtml2pdf (process) |
| --- | --- | --- | --- |
| 40 steps, 200 log lines | 0.3 s, 8 pages | 1.6 s, 9 pages | 1.7 s |
| 500 steps, 5,000 lines | 3.1 s, 104 pages | 35 s, 131 pages | 35 s |
| 1,000 steps, 10,000 lines | 6.5 s, 205 pages | 97 s, 260 pages | 95 s |
| 2,000 steps, 20,000 lines | 14.5 s, 408 pages | not run | not run |
| Longest GUI stall | ~80 ms (warm page) | 280–550 ms (GIL) | ~15 ms |
| Installed size (Linux venv) | ~264 MB of WebEngine files | ~37 MB with deps | |
