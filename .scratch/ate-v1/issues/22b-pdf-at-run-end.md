# 22b: PDF at run end

**Parent:** 22 (PDF via the helper process) — `22-pdf-helper.md`

**What to build:** Each report is also written as PDF without stalling the GUI.

**Blocked by:** 22a (PDF helper process)

**Status:** resolved

- [x] At run end HTML then PDF are written; if the PDF fails the HTML remains and the error is logged
- [x] Tests cover success and a failing helper

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.

## Notes

- The run session starts the PDF helper with a `QProcess` it owns (`HeadlessRunSession._start_pdf`), so no worker thread touches the session; the process is killed after `PDF_TIMEOUT_MS`. An earlier version ran the blocking helper on a daemon thread and made the suite fail about 4 runs in 10.
- Tests swap `pdf_helper_command` for a tiny Python stand-in process (`conftest._fake_pdf_helper`), except `test_run_end_writes_real_pdf_report`.
- `conftest._stop_running_engines` aborts and joins any RunEngine a test leaves running; without it a dropped session's `OperatorInput` could be deleted under a live worker and abort a later test.
