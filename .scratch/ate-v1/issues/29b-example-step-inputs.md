# 29b: Example: Step Inputs

**Parent:** 29 (Example Media Release demonstrating every Test Schedule feature) — `29-example-media-release.md`

**What to build:** Demonstrate every Step Input type and widget.

**Blocked by:** 10a (Step Input widgets on the Run screen), 29a (Example Media Release scaffold and basics)

**Status:** resolved

- [x] One step per type and widget (`ENUM` with `RADIOBUT`, `CHECKBOX` and `PUSHBUT`; `INTEGER` and `FLOAT` with ranges; `STRING` with a pattern; `PROMPT`; `POLL` read in a loop), using `tooltip` and `orient`
- [x] The harness asserts recorded input values

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
