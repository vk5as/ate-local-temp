# 10a: Step Input widgets on the Run screen

**Parent:** 10 (Step Inputs) — `10-step-inputs.md`

**What to build:** The staged Run screen shows each step's Step Inputs under it and lets the Operator answer them; the engine, recording and run-session side already exist (ticket 10, `83467fc`).

**Blocked by:** 10 (Step Inputs)

**Status:** ready-for-agent

- [ ] Inputs show under their step at all times, one widget per declared type (`ENUM` as `RADIOBUT`/`CHECKBOX`/`PUSHBUT`, `INTEGER`, `FLOAT`, `STRING`, `PROMPT`, `POLL`) honouring `tooltip` and `orient`; widgets only call the run session (no logic in the widget)
- [ ] A blocking input is enabled only while its `ATE.input` call waits, then disabled showing the value; `POLL` buttons are clickable while their step runs
- [ ] The staged pytest-qt smoke test answers one blocking input by interacting with the widget, not the session

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes.
