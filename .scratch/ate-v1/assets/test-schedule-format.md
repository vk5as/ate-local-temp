# Test Schedule format

ATE's definition of the Lua Test Schedule format. Vocabulary follows `GLOSSARY.md`.

## Files

- A Test Schedule is a Lua file with the extension `.tslua`.
- Loading it must **return** a schedule object. Nothing in the file runs Test Steps itself; ATE drives execution.
- The builders `ScheduleParams` and `StepParams` are globals ATE provides to the Lua runtime. Their setters return the object so calls can be chained.

## Schedule object

Created with `ScheduleParams(name, description)`; `name` is required.

| Setter | Value | Meaning |
| --- | --- | --- |
| `:setDocTitle(s)` | string | Title of the Test Report |
| `:setDocId(s)` | string | Document identifier shown on the Test Report |
| `:setDocRev(s)` | string | Revision of the Test Schedule |
| `:setPreTestAction(fn)` | function | Runs once before the first Test Step (setup, calibration) |
| `:setPostTestAction(fn)` | function | Runs once after the steps; called as `fn(passFail)` with the Test Run's overall result |
| `:setSchedule(list)` | array of step objects | The Test Steps, in execution order (required) |

## Step object

Created with `StepParams()` and configured with:

| Setter | Value | Meaning |
| --- | --- | --- |
| `:setStepNo(s)` | string | Step Number as displayed, free-form dotted (`"1."`, `"4.0.1"`); required |
| `:setEnable(b)` | boolean | Default enabled state; the Operator can enable or disable each step before the run starts. A step that is disabled when the run starts is not executed and records `Result.NOTRUN` |
| `:setInputs(t)` | table keyed `1..n` | Step Input declarations; `ATE.input(n)` reads the n-th |
| `:setAction(fn)` | function | The step's logic; required for an ordinary step |
| `:setInclude(file, keyvals)` | string, table | Makes this step a placeholder that is replaced by the steps of a Sub-schedule loaded from `file`, parameterised by `keyvals` |
| `:setPrefix(s)` | string | Prefix applied to the Sub-schedule's Step Numbers (used with `setInclude`) |

## Step Input declaration

Each entry in the `setInputs` table:

| Field | Applies to | Meaning |
| --- | --- | --- |
| `desc` | all (required) | Text describing what is asked for |
| `type` | all (required) | One of `ENUM`, `INTEGER`, `FLOAT`, `STRING`, `PROMPT`, `POLL` |
| `input` | `ENUM` | Widget: `RADIOBUT` (pick one), `CHECKBOX` (pick many), `PUSHBUT` (buttons) |
| `enum` | `ENUM` | Option labels, array of strings |
| `range` | `INTEGER`, `FLOAT` | `{min, max}`; empty means unbounded |
| `pattern` | `STRING` | Regular expression the value must match |
| `prompt` | `PROMPT`, `POLL` | Prompt text: a string or an array of strings |
| `tooltip` | all | Hover help |
| `orient` | all | Layout: `VERT` or `HORIZ` |

## Example

```lua
local function measureRail()
  -- connection fields are illustrative; see "Device connection details and v1 connection types"
  local psu = ATE.open(DeviceType.SERIAL, { port = "COM3", baud = 9600 })
  -- ...
  ATE.close(psu, {})
end

return ScheduleParams("Power board functional", "Basic rails and LEDs")
  :setDocTitle("Power Board Functional Test")
  :setDocId("TR-0001")
  :setDocRev("A")
  :setSchedule({
    StepParams():setStepNo("1."):setEnable(true)
      :setInputs({ [1] = { desc = "LED colour", type = "ENUM", input = "RADIOBUT",
                           enum = { "Green", "Red", "Off" }, orient = "HORIZ" } })
      :setAction(function() local c = ATE.input(1) end),
    StepParams():setStepNo("2."):setAction(measureRail),
  })
```

## Behaviour

- **Lua version**: ATE targets **Lua 5.5**.
- **Step results**: a step reports by calling `ATE.report(passFail, msg, actual, expected)` (full definition in `ate-table-reference.md`).
  - `passFail` is a value of the `Result` enum: `Result.PASS`, `Result.FAIL`, `Result.ERROR`, `Result.SKIPPED`, `Result.NOTRUN`, `Result.FORCED_NOTRUN`, `Result.NOREPORT`. `SKIPPED`, `NOTRUN` and `FORCED_NOTRUN` are used at the test writer's discretion.
  - `msg` and `expected` are optional. `actual` and `expected` may be a number, string or boolean, and ATE stores them as text. The schedule does its own comparison and chooses `passFail`; ATE does not check limits.
  - Every call is logged, but only the most recent one counts for the step.
  - A step that never calls it records `NOREPORT`, and ATE logs a warning.
- **Coroutines**: ATE need not support Test Schedules using Lua coroutines themselves.
- **`PROMPT`**: blocking. Execution pauses until the Operator clicks the button to continue.
- **`POLL`**: non-blocking. The input shows one or more buttons that can be clicked at any time. Reading it returns `nil` until a button is clicked, then returns that button's text.

- **Run result**: a Test Run's result is `PASS` or `FAIL`; it fails if any step's result is `Result.FAIL` or `Result.ERROR`.
- **`Result.ERROR`**: ATE records it when a step raises a Lua error, and schedules may report it too.
- **`POLL`** is sticky: after a click it keeps returning that button's text until it is reset at Test Run start and at step start.
- **`ATE.input(n)` returns**: the label for `RADIOBUT`/`PUSHBUT`, an array of labels for `CHECKBOX`, a Lua integer for `INTEGER`, a Lua float for `FLOAT`, a string for `STRING`, and the clicked button's text for `PROMPT`. See "Test Step structure and outcome model".
- **Strict values**: only `INTEGER` is accepted (`INT` is not), and `range` is `{}` or `{min, max}`.

## Loading auxiliary Lua code

Test Schedules share code through ordinary Lua modules loaded with the standard `require`. ATE replaces `require` so that a run stays traceable:

- `require("lib.psu")` resolves the module name against the files listed in the Test Definition, never against `package.path`. Asking for a module the Test Definition does not list is an error.
- ATE computes the checksum of each file it loads and compares it with the Test Definition. A mismatch logs a warning but still loads the file. The Test Run records the expected and computed checksum of every file used.
- As in normal Lua, a module runs once per Test Run, and later calls return the cached value.
- The `ate.` namespace is reserved: `require("ate.devices")` resolves to a module bundled with ATE, never to the Test Definition, and a Test Definition may not list files under `ate.`. The Test Run records the ATE version and the checksum of every bundled module it used.
- `dofile` and `loadfile` are removed, so `require` is the only intended way to run another file. Lua is trusted, so this is about traceability, not security; `io` and `load` on strings remain.

This is unrelated to the step setter `:setInclude(file, keyvals)`, which pulls in a Sub-schedule. ATE has no function named `include`.

## The `ATE` table

The functions schedules call (`open`, `read`, `write`, `close`, `sleep`, `input`, `log`, `report`, `run`) are defined in `ate-table-reference.md`.

## Still open

- **Sub-schedules**: how `file` is resolved, what `keyvals` does, how the prefix combines with Step Numbers, and whether they nest.
