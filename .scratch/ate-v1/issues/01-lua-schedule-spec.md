# Draft the Lua schedule format

Type: task
Status: resolved
Blocked by: none

## Question

Provide a first draft of the Lua Test Schedule format: the returned data structure, Test Step fields, input declarations, limits/outcomes, any API the scripts call, and the target Lua version. Human task.

## Answer

Drafted by the user and written up as `.scratch/ate-v1/assets/test-schedule-format.md`.

- A `.tslua` file returns `ScheduleParams(name, desc)` with chained setters: doc title, id and revision for the report; Pre-test and Post-test Actions (`post(passFail)`); and the step array.
- Each step is a `StepParams` with Step Number (string), enable flag, Step Inputs keyed `1..n`, an action function, or `setInclude(file, keyvals)` plus `setPrefix` to splice in a Sub-schedule.
- Step Input types: `ENUM` (`RADIOBUT`/`CHECKBOX`/`PUSHBUT`), `INTEGER`, `FLOAT` (range), `STRING` (regex), `PROMPT`, `POLL`; plus `tooltip` and `orient`.
- Not in the draft: the Lua version, how steps report results and measurements, `PROMPT` vs `POLL` semantics, and Sub-schedule details. These are listed in the asset's "open" section and carried into the tickets below.
