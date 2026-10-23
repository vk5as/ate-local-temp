# 22b: PDF at run end

**Parent:** 22 (PDF via the helper process) — `22-pdf-helper.md`

**What to build:** Each report is also written as PDF without stalling the GUI.

**Blocked by:** 22a (PDF helper process)

**Status:** resolved

- [x] At run end HTML then PDF are written; if the PDF fails the HTML remains and the error is logged
- [x] Tests cover success and a failing helper

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.

## Measurement & residual flakiness (post-resolution)

- **Packet 22b itself is complete and green.** `test_run_end_writes_html_and_pdf_reports`
  (HTML then PDF, both paths over `report_written`), `test_run_end_pdf_failure_keeps_html_and_logs_error`
  (HTML survives + ERROR logged with source `ate` + empty pdf path), `test_run_end_pdf_is_not_blocking`
  (FINISHED returns before the ~1s helper finishes), and the real e2e `test_run_end_writes_real_pdf_report`
  all pass in isolation. The background worker holds the session strongly across both `report_written`
  emits and probes a weakref liveness so a late-finishing daemon never calls into a freed C++ QObject.

- **Unit tests no longer spawn real QWebEngine workers.** An autouse fixture (`_neutralize_pdf_worker`)
  stubs `run_pdf_helper` for every test except the e2e one, so finished runs in unit tests don't leave a
  lingering QWebEngine daemon thread (this Qt 6.11 build segfaults at shutdown with such threads present).
  Full-suite failures dropped from 3 to ~0 and the shutdown aborts stopped; commit `d57ab59`.

- **Residual full-suite flakiness is environmental, not packet 22b.** This Qt build accumulates Qt
  lifetime pollution monotonically within a process: parentless RunEngine QThreads escape conftest's
  `_stop_workers(app)` walk of `app.children()`, and widget trees / timers linger across the shared
  session application. That stalls cross-thread signal delivery so simple runs occasionally miss the
  `_wait_finished` deadline, and eventually segfaults at shutdown — in modules unrelated to packet 22b
  (ticket-03 StagedRunScreen widgets, lua seam-error tests). Each of those passes cleanly in isolation;
  the `OperatorInput has been deleted` SIGABRT is a pre-existing teardown-ordering bug in
  `RunEngine.run()` → `_op.reset()`, independent of packet 22b. Do not chase packet 22b code when you see
  full-suite flakes — read them as accumulated-Qt-pollution, and measure per-run as "clean under low
  pollution" vs "flaky under high in-session pollution," never as deterministic pass/fail.
