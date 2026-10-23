# 03: Minimal staged Run screen

**What to build:** The first demoable app: the staged Run screen bound to the run session. The Operator enters Operator, Project, UUT Type and serial, picks a Test Schedule from the list, presses Start and watches the steps get their results.

**Blocked by:** 02 (Tracer bullet: run a one-step Test Schedule and record it)

**Status:** resolved

- [x] Stage 1 "UUT and Operator" has required Operator, Project, UUT Type and serial fields; Stage 2 "Test Schedule" lists schedules with their Media Release; Stage 3 "Steps and options" shows the steps and Start
- [x] The run view lists every step with its live `Result` and enlarges and scrolls to the current step
- [x] New run after a run keeps every field
- [x] The layout holds no run logic: it only renders the run session and forwards actions
- [x] The GUI stays responsive during a run
- [x] A pytest-qt smoke test builds the staged layout on a run session and runs one short schedule

**Answer:** Built `src/ate/ui/staged_run_screen.py` (`StagedRunScreen`, plus child widgets `UutOperatorStage`, `TestScheduleStage`, `StepsOptionsStage`, `RunView`). Three ordered setup stages live in a `QStackedWidget` (UUT & Operator fields, Test Schedule list with Media Release notes, Steps + stop-on-fail radio); the focus Run view sits below and is shown once a run starts. The layout binds only existing interfaces — selection → `session.select_schedule` (exact name via `Qt.ItemDataRole.UserRole`), fields → `set_operator/project/uut_type/serial`, start → `session.start()`, results read from two new child-read methods, `RunStore.nodes(run_uuid)` and `RunStore.attempts(run_uuid)`. It holds no run logic and never imports the engine or session, so the import-lint guard passes; every update rides `state_changed` (IDLE→SELECTING→STARTED→FINISHED) plus a 1 s timer, so it never blocks or polls the worker. The Run view enlarges the current step, shows per-step Result, Log/Summary tabs and a full-width red failure bar when any step is FAIL/ERROR. New runs clear nothing (fields are only pushed when non-empty). `set_stop_on_fail` was threaded through `start()` into `RunEngine.stop_on_fail`. Real Lua execution is not implemented — stubbed canned results from ticket 02 keep the seams testable offscreen.

**Segfault fix:** this Qt 6.11 / PyQt6 build segfaults at interpreter shutdown when a live `QCoreApplication` and a live `QApplication` coexist (exit 139). Fixed by sharing one session-scoped autouse `qapp` fixture — a single `QApplication` (which also satisfies `QCoreApplication`, so queued cross-thread signals still deliver) across every test via conftest, so no core/app coexistence can occur and the lone application finalizes cleanly. Ticket 02's `_wait_finished` now pumps an explicit instance (`QCoreApplication.instance()`, created if none). The suite is green (30 passed) under `QT_QPA_PLATFORM=offscreen`.

Built on branch `ticket-03-staged-run-screen`; commit `f23847b`.

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
