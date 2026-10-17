# Test Step structure and outcome model

Type: grilling
Status: resolved
Blocked by: 01

## Question

What fields does a Test Step carry (name, function, input declarations, limits, units, …), and what outcomes can a step and a Test Run have (Pass / Fail / Error / Skipped / Aborted, measurements against limits)?

Inputs: the step fields are known from `assets/test-schedule-format.md`. Still open: how a step reports pass/fail and measurements (the draft format doesn't say), how `passFail` is derived, what disabled steps record, and `PROMPT` vs `POLL` semantics.
Decide which Step Input types and step features ATE v1 supports.
Answered since: steps report through `ATE.report(<pass/fail enum>)`. Still to settle: the enum name and values, measurements and limits, multiple calls per step, a step that never reports, and the `passFail` derivation.
Answered since: the signature is `ATE.report(passFail, result, expectedResult, message)`. Still to settle: the enum values, the types of `result` and `expectedResult`, units, multiple calls per step, and a step that never reports.
Answered since: `passFail` is one of PASS, FAIL, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT. `result` and `expectedResult` are strings, and the schedule does its own comparison. Every call is logged, but the last one wins. A step that never reports gets NOREPORT plus a warning log. Remaining: how the enum is spelled in Lua, what distinguishes SKIPPED, NOTRUN and FORCED_NOTRUN, and how the run's overall `passFail` is derived.
Answered since: the enum is `Result.PASS`, `Result.FAIL`, and so on. SKIPPED, NOTRUN and FORCED_NOTRUN are used at the test writer's discretion. A Test Run passes if no step reports FAIL. A step disabled at run start records NOTRUN (see map Notes). Remaining: which Step Input types and step features v1 keeps, what `ATE.input(n)` returns per type, and whether `POLL` resets after it is read.

## Answer

**Step fields.** A v1 step has exactly `setStepNo`, `setEnable`, `setInputs` and `setAction`. It has no step name (amended: see below), units, limits or measurement fields: the Step Number identifies the step, and units go inside the `result`/`expectedResult` strings. `setInclude`/`setPrefix` are left to "Sub-schedules".

**Step Input types.** All of them are supported: `ENUM` (`RADIOBUT`, `CHECKBOX`, and `PUSHBUT` as a blocking pick-one), `INTEGER`, `FLOAT`, `STRING`, `PROMPT`, `POLL`. `tooltip` and `orient` are honoured.

**What `ATE.input(n)` returns:**

| Type | Returns |
| --- | --- |
| `ENUM` / `RADIOBUT`, `PUSHBUT` | the chosen label (string) |
| `ENUM` / `CHECKBOX` | array of chosen labels in declaration order; empty table if none |
| `INTEGER` | Lua integer (`math.type` = `"integer"`) |
| `FLOAT` | Lua float |
| `STRING` | string |
| `PROMPT` | blocks until clicked, then returns the clicked button's text (an array `prompt` gives several buttons) |
| `POLL` | non-blocking: `nil`, or the text of the most recently clicked button. Sticky: it is not cleared by reading, only reset to nothing-clicked at Test Run start and at step start |

- Calling `ATE.input(n)` again on a blocking input asks the Operator again. Every value entered is logged.
- The GUI won't submit a value until it parses and satisfies `range`/`pattern`, so Lua never sees an invalid value.

**GUI behaviour** (input to "Run screen, input forms, log pane and debug window"):

- The Run screen shows a vertical list of every step. Each step's inputs are always visible under it.
- A blocking input is enabled only while its `ATE.input(n)` call waits. Afterwards it is disabled again, still showing the entered value.
- A `POLL` is enabled only while its own step runs.

**Strictness.** Only `INTEGER` is accepted; `INT` is an error. `range` is `{}` (unbounded) or `{min, max}`; a single number is an error.

**Load errors.** ATE refuses to load a Test Schedule for any of these:

- a Lua parse error
- a step without a Step Number
- an ordinary step without an action
- a malformed Step Input declaration
- a duplicate Step Number (results and history are keyed by it)

**`Result.ERROR`.**

- Schedules may report it via `ATE.report`.
- ATE records it when a step raises a Lua error. It overrides anything reported earlier, the error becomes the `message`, and the traceback is logged.

**Run result.**

- A Test Run's overall result is `PASS` or `FAIL` only. It fails if any step is `FAIL` or `ERROR`.
- The Post-test Action never sees `ERROR`.
- The summary cover page counts `ERROR` along with the other `Result` values.

**Pre-test and Post-test Action errors** are recorded against that action and logged, and the run fails. Whether the remaining steps still run is left to "Flow control".

**Amended by "Sub-schedules" (14):** steps now take an optional Step Name, `StepParams(name)`, shown on the Run screen and the Test Report and recorded on the Test Run.
