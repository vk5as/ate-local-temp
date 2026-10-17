# 21: HTML Test Report at run end

**What to build:** At the end of every run ATE writes a self-contained HTML Test Report generated from the Test Run record alone.

**Blocked by:** 07 (Media Release dependencies), 10 (Step Inputs), 12 (Stop on Fail, the failure prompt and Retry), 14 (Pre-test and Post-test Actions and the status bar), 15 (Sub-schedules), 20 (Equipment)

**Status:** ready-for-agent

- [ ] A bundled Jinja2 template with inline CSS and the logo as a data URI; only `reports.organisation`, `reports.logo` and `reports.paperSize` customise it
- [ ] File names `<UUT Type>/<UUT Type>_<serial>_<local start>_<PASS|FAIL>.html` under `reports.directory`, Windows-invalid characters replaced, never overwritten (`-2`, `-3` suffixes)
- [ ] Sections Cover, Steps, Equipment, Configuration, Run log exactly as `test-report-format` specifies, including doc fields (recorded on `test_run`), banners, signature box, `Result` counts, failures, Attempt sub-rows, header roll-ups, Operator overrides, input values, files used and Sub-schedule instances
- [ ] Page header and footer content, local times with offset, repeating step table headings (CSS for print)
- [ ] The Run log section can be left out (used by History regeneration)
- [ ] Seam-1 tests regenerate the HTML from the record and check its content for representative runs

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
