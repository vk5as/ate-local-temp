# 11b: Toggle steps in stage 3

**Parent:** 11 (Enabling and disabling steps before the run) — `11-step-enabling.md`

**What to build:** The Operator can enable or disable individual Test Steps in stage 3 of the staged Run screen before Start.

**Blocked by:** 11a (Step enables in the engine and record)

**Status:** ready-for-agent

- [ ] Stage 3 shows the step tree with a checkbox per step reflecting its default; toggling calls the run session (no logic in the widget)
- [ ] Toggles are locked once the run starts
- [ ] A pytest-qt test toggles a step and checks the recorded `enabled`

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
