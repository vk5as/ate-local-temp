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
| `:setPreTestAction(fn)` | function | Runs once before the first Test Step (setup, calibration). With Sub-schedules, every Pre-test Action runs before the first step, depth first, parent first |
| `:setPostTestAction(fn)` | function | Runs once after the steps, in the reverse of the Pre-test order; called as `fn(passFail)` with the Test Run's overall result. Runs even after a stop, a Pre-test failure or an abort |
| `:setStopOnFail(b)` | boolean | Default for the Test Run's stop-on-fail setting, which the Operator can change before the run starts. Defaults to `true` when not set |
| `:setSchedule(list)` | array of step objects | The Test Steps, in execution order (required) |

## Step object

Created with `StepParams(name)`, where `name` is the optional Step Name shown on the Run screen and the Test Report, and configured with:

| Setter | Value | Meaning |
| --- | --- | --- |
| `:setStepNo(s)` | string | Step Number as displayed, free-form dotted (`"1."`, `"4.0.1"`); required for an ordinary step, optional for an include step |
| `:setEnable(b)` | boolean | Default enabled state; the Operator can enable or disable each step before the run starts. A step that is disabled when the run starts is not executed and records `Result.NOTRUN`. On an include step, forces the default of every step beneath it |
| `:setInputs(t)` | table keyed `1..n` | Step Input declarations; `ATE.input(n)` reads the n-th |
| `:setAction(fn)` | function | The step's logic; required for an ordinary step |
| `:setInclude(file, keyvals)` | string, table | Makes this an include step: a header row followed by the steps of the Sub-schedule loaded from `file`, parameterised by `keyvals` (see Sub-schedules) |
| `:setPrefix(s)` | string | Prepended to the Sub-schedule's Step Numbers; only with `setInclude` |

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
    StepParams("LED colour"):setStepNo("1."):setEnable(true)
      :setInputs({ [1] = { desc = "LED colour", type = "ENUM", input = "RADIOBUT",
                           enum = { "Green", "Red", "Off" }, orient = "HORIZ" } })
      :setAction(function() local c = ATE.input(1) end),
    StepParams("Rail voltage"):setStepNo("2."):setAction(measureRail),
    StepParams("Channel checks"):setStepNo("3.")
      :setInclude("channel.tslua", { channel = 1 }):setPrefix("3."),
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

- `require("lib.psu")` resolves the module name against the files listed in the Test Definition and then its dependencies, never against `package.path`. Asking for a module none of them lists is an error. Module naming and resolution order are defined in `test-definition-format.md`.
- ATE computes the checksum of each file it loads and compares it with the Test Definition. A mismatch logs a warning but still loads the file. The Test Run records the expected and computed checksum of every file used.
- As in normal Lua, a module runs once per Test Run, and later calls return the cached value.
- The `ate.` namespace is reserved: `require("ate.devices")` resolves to a module bundled with ATE, never to the Test Definition, and a Test Definition may not list files under `ate.`. The Test Run records the ATE version and the checksum of every bundled module it used.
- `dofile`, `loadfile` and `load` are removed, so nothing can run Lua source that skipped the checksum check. Lua is trusted, so this is about traceability, not security; `io` and `os` remain.

This is unrelated to the step setter `:setInclude(file, keyvals)`, which pulls in a Sub-schedule. ATE has no function named `include`.

## Flow control

Defined in "Flow control":

- **Stop on fail** (default): when a step ends `FAIL` or `ERROR`, the run pauses and the Operator chooses Continue, Retry or Abort. With continue on fail, the run never pauses and retry isn't offered.
- **Retry** runs the step's action again from the start, with `POLL` reset and blocking inputs cleared. Every Attempt is recorded; the step's result is the last Attempt's.
- **Abort** is whole-run. The running step records `ERROR` "Aborted by Operator"; steps that never ran record `NOTRUN`.
- **Pre-test Action failure** aborts the run automatically: every step records `NOTRUN`.
- **Pre-test and Post-test order**: all Pre-test Actions run before the first step, depth first through the Sub-schedule tree (parent first, each include in step order). All Post-test Actions run after the last step in exactly the reverse order. A Sub-schedule whose steps are all disabled has its actions skipped. After a Pre-test failure, only schedules whose Pre-test Action started get their Post-test Action.
- **Post-test Actions always run**. ATE clears the abort flag before them; a second abort ends only the current one, and an error in one is logged, fails the run, and the rest still run.
- **End reason**: `COMPLETED`, `PRETEST_FAILED`, `STOPPED_ON_FAIL` or `ABORTED`, recorded alongside `PASS`/`FAIL`.

## Sub-schedules

Defined in "Sub-schedules":

- **Resolving `file`**: a path resolved against the directory holding the including schedule's `.atedef`, using the same rules as `files` paths, which must match a listed `.tslua` file. If it isn't in the including release's own files, ATE searches its dependencies depth first, in declaration order, as `require` does. A path matched in more than one release is an error. The checksum is checked and recorded as for `require`.
- **Expansion**: ATE expands the whole tree every time it loads a schedule, both at scan time and at run start. A Sub-schedule that is missing, not listed, ambiguous, cyclic or raises while loading breaks the including Test Schedule, not its Media Release.
- **`keyvals`**: the chunk receives it as its argument, `local kv = ...`. ATE runs the chunk again for every include, so each include is its own instance; `require`d modules stay shared. A top-level schedule, or an include without `keyvals`, receives `{}`. `keyvals` is plain data only (strings, numbers, booleans, nested tables of those), and it is recorded per instance.
- **Step Numbers**: the prefix is prepended as it is, with no separator (`"3."` + `"1."` is `"3.1."`), and prefixes stack through nesting from the outside in. The duplicate Step Number check runs on the expanded tree.
- **Nesting**: any depth. A file that appears twice on one include chain is a cycle; the same file in sibling branches is fine.
- **Include step**: a header row, not a Test Step. It has no `Result` and is not counted in the summary. It shows its own Step Name and optional Step Number, never the Sub-schedule's `ScheduleParams` name. Its Step Number lives in the parent's numbering (outer prefixes apply, its own does not) and is checked for duplicates. `setInputs` or `setAction` with `setInclude`, or `setPrefix` without it, is a load error.
- **Enabling**: `setEnable` on an include step, if called, forces the default of every step beneath it; otherwise those steps keep their own defaults. Toggling the header before the run sets everything beneath it, and those steps can still be toggled one by one afterwards. A header over mixed states shows a partial mark.
- **The included schedule's own fields**: only its Pre-test Action, Post-test Action and steps take part. Its doc fields and `setStopOnFail` are ignored. `ATE.run` describes the top-level schedule.

```lua
-- channel.tslua: runnable alone, or included once per channel
local kv = ...
local ch = kv.channel or 1
return ScheduleParams("Channel check", "One output channel")
  :setSchedule({
    StepParams("Output on"):setStepNo("1."):setAction(function() --[[ uses ch ]] end),
    StepParams("Output off"):setStepNo("2."):setAction(function() --[[ uses ch ]] end),
  })
```

## The `ATE` table

The functions schedules call (`open`, `read`, `write`, `close`, `sleep`, `input`, `log`, `report`, `addEquipment`, `run`) are defined in `ate-table-reference.md`.
