# 03: Minimal staged Run screen

**What to build:** The first demoable app: the staged Run screen bound to the run session. The Operator enters Operator, Project, UUT Type and serial, picks a Test Schedule from the list, presses Start and watches the steps get their results.

**Blocked by:** 02 (Tracer bullet: run a one-step Test Schedule and record it)

**Status:** ready-for-agent

- [ ] Stage 1 "UUT and Operator" has required Operator, Project, UUT Type and serial fields; Stage 2 "Test Schedule" lists schedules with their Media Release; Stage 3 "Steps and options" shows the steps and Start
- [ ] The run view lists every step with its live `Result` and enlarges and scrolls to the current step
- [ ] New run after a run keeps every field
- [ ] The layout holds no run logic: it only renders the run session and forwards actions
- [ ] The GUI stays responsive during a run
- [ ] A pytest-qt smoke test builds the staged layout on a run session and runs one short schedule

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
