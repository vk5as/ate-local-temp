# 10: Step Inputs

**What to build:** Test Steps can ask the Operator for values through `ATE.input(n)` for every Step Input type, the inputs are visible under their step, enabled only while needed, and every value is recorded.

**Blocked by:** 08 (Logging, log pane and debug window)

**Status:** ready-for-agent

- [ ] Declarations validated per `test-schedule-format` (`ENUM` with `RADIOBUT`/`CHECKBOX`/`PUSHBUT`, `INTEGER`, `FLOAT`, `STRING`, `PROMPT`, `POLL`; strict `INTEGER`, `range` `{}` or `{min, max}`, `pattern`, `tooltip`, `orient`); bad declarations are load errors
- [ ] `ATE.input(n)` blocks the worker for every type except `POLL` and returns the documented typed value; an undeclared `n` raises
- [ ] `POLL` returns `nil` until a button is clicked, then that text; sticky until reset at Test Run start and step start; clickable while its step runs
- [ ] Inputs show under their step at all times; a blocking input is enabled only while its call waits, then disabled showing the value
- [ ] Every value is an `input_value` row (index, type, text value, `CHECKBOX` as JSON, time); `POLL` clicks are recorded when clicked; declarations are stored as JSON on the step's `node`
- [ ] Seam-1 tests supply every input type through the run session; the staged smoke test exercises one blocking input

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
