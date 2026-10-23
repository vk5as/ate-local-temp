# 21a: HTML report skeleton, naming and Cover

**Parent:** 21 (HTML Test Report at run end) — `21-html-test-report.md`

**What to build:** At the end of every run ATE writes a self-contained HTML Test Report from the Test Run record; this packet builds the file, its naming and the Cover section.

**Blocked by:** 09 (Lua lockdown, errors and `ATE.run`)

**Status:** resolved

- [x] A bundled Jinja2 template with inline CSS and the logo as a data URI; only `reports.organisation`, `reports.logo` and `reports.paperSize` customise it
- [x] File names `<UUT Type>/<UUT Type>_<serial>_<local start>_<PASS|FAIL>.html` under `reports.directory`, Windows-invalid characters replaced, never overwritten (`-2`, `-3` suffixes); written at run end
- [x] Cover section with doc fields (recorded on `test_run`), banners, signature box and `Result` counts; page header/footer, local times with offset
- [x] Seam-1 tests generate the HTML from a recorded run and check its content

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
