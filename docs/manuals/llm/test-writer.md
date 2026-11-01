# Writing Test Schedules for ATE

What an agent needs to write, check and ship a Test Schedule. Every name here is checked
against the code (`src/ate/engine/lua_host.py`, `src/ate/devices/`, `src/ate/catalog.py`), and
every example is a file under `docs/manuals/examples/test-writer/` that the test suite runs
headless (`tests/test_manuals.py`). Facts that the wayfinding assets state differently from the
code are listed at the end.

## The job, in order

1. Write a `.tslua` file that returns a schedule.
2. Put it in a Media Release with a `.atedef` that lists every file and its checksum.
3. Run it headless or in the Run screen and read the Test Run record back.
4. Fix what the record shows.

You cannot run a schedule that is not in a Test Definition. Do step 2 before step 3.

## Files and vocabulary

- **Test Schedule** - a Lua file ending `.tslua`. Lua 5.5 (`TARGET_LUA_VERSION = (5, 5)`).
- **Test Definition** - a JSON file ending `.atedef`: `format`, `mediaNumber`, `name`,
  `version`, optional `depends`, and `files`.
- **Test Run** - one execution of one schedule against one UUT. Its End Reason is
  `COMPLETED`, `PRETEST_FAILED`, `STOPPED_ON_FAIL`, `ABORTED` or `INTERRUPTED`.
- **Device** - a connection ATE opens (TCP, SERIAL, UDP, VISA). **Equipment** - a bench record
  the schedule declares for the report. They are not the same thing and ATE never links them.
  GLOSSARY.md fixes the name for the handle `ATE.open` returns: it is a Device, not an instrument,
  a resource or a port. "Instrument" below means the physical equipment the connection reaches.

## How much Lua this manual assumes

Basic Lua: `local`, functions, tables, `if`, `for`, `while`, `..` for strings, `nil`. Three
idioms are ATE-specific: every setter returns its object so calls chain (`:setStepNo("1.")
:setAction(...)`); a params table is a Lua table written `key = value`; a step's action is an
anonymous function, and a Sub-schedule's parameters arrive as `local kv = ...`. A step's action
runs synchronously on the worker thread. Coroutines are not removed from the sandbox and using one
is allowed, but the abort hook does not reach a coroutine, so a busy loop inside one is a documented
exception to the 10 s abort rule (see Abort).

## The schedule format

The top level of the file runs before any Test Run exists. Plain Lua is fine there: `require`,
`ATE.log`, the builders, the enums, and any of the standard library (`os.getenv` in
`tcp.tslua` reads its host and port at load time). What is not legal is an `ATE` call that needs
a run: `ATE.open`, `ATE.close`, `ATE.read`, `ATE.write`, `ATE.flush`, `ATE.report`, `ATE.input`
and `ATE.addEquipment` each raise a load error naming the function. "Load error" is this manual's
name for anything the loader rejects: one of those `ATE` calls at the top level, or a schedule that
fails validation (a duplicate Step Number, a step with no action). The file must return a
`ScheduleParams`.

Setters stash values and return the object, so they chain; the order does not matter, and every
step is validated when the file finishes loading.

`ScheduleParams(name, description)` - `name` required.
`:setDocTitle(s)`, `:setDocId(s)`, `:setDocRev(s)`, `:setPreTestAction(fn)`,
`:setPostTestAction(fn)`, `:setStopOnFail(b)` (default `true`), `:setSchedule({steps})`.

`StepParams(name)` - `name` optional.
`:setStepNo(s)` - required for an ordinary step; free-form text such as `"4.0.1"`, not a
position. Duplicates among siblings are a load error.
`:setEnable(b)` - the default; the Operator can change it before the run.
`:setInputs(t)` - declarations keyed 1..n.
`:setAction(fn)` - required for an ordinary step.
`:setInclude(file, keyvals)` - makes the step a header row instead.
`:setPrefix(s)` - only with `setInclude`.

An ordinary step with no `setStepNo` or no `setAction` is a load error. `setInputs` or
`setAction` on an include step is a load error, as is `setPrefix` without `setInclude`.

```lua:docs/manuals/examples/test-writer/first.tslua
-- first.tslua: the smallest Test Schedule that runs.
--
-- A Test Schedule is a Lua file that RETURNS a schedule object. Nothing in the file
-- runs by itself: ATE reads the steps and executes them. Save it next to a Test
-- Definition that lists it (see manual.atedef in this directory).
return ScheduleParams("Manual: first schedule", "one Test Step that reports PASS")
  :setSchedule({
    StepParams("Say hello"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "the schedule ran")
      end),
  })
```

## Steps and Results

`ATE.report(passFail, msg, actual, expected)` records the step. The last call in an Attempt
wins; every call is logged; no call means `NOREPORT` and ATE logs a WARNING. `actual` and
`expected` are stored as text and ATE compares nothing - the schedule decides.

```lua:docs/manuals/examples/test-writer/steps.tslua
-- steps.tslua: Step Numbers, Step Names, enabled defaults, and reporting.
--
-- Demonstrates:
--   * every ordinary step needs setStepNo and setAction;
--   * setEnable(false) starts a step disabled, and a step disabled at run start
--     records Result.NOTRUN without ever executing;
--   * a step that never calls ATE.report records Result.NOREPORT and logs a WARNING;
--   * several ATE.report calls in one Attempt: every one is logged, the last one counts;
--   * Actual and Expected are stored as text, and ATE does no comparing: the schedule
--     decides whether the value passes.
return ScheduleParams("Manual: steps", "step numbers, names and the enabled default")
  :setDocTitle("Manual examples - Steps")
  :setDocId("MR-MANUAL-STEPS")
  :setDocRev("1.0")
  :setSchedule({
    StepParams("First check"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "rail is within tolerance", 5.0, 5.0)
      end),

    -- Two reports in one Attempt: the log shows both, the step's result is the second.
    StepParams("Report twice"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.FAIL, "first reading looked low", 4.9, 5.0)
        ATE.report(Result.PASS, "second reading is fine", 5.0, 5.0)
      end),

    -- Disabled from the start: the Operator can turn it on before the run.
    StepParams("Optional check"):setStepNo("3.")
      :setEnable(false)
      :setAction(function()
        ATE.report(Result.PASS, "only runs when the Operator enables it")
      end),

    -- A step that does work but never reports: ATE records NOREPORT and warns.
    StepParams("Forgot to report"):setStepNo("4.")
      :setAction(function()
        ATE.log(Log.INFO, "measured something, but did not report it")
      end),
  })
```

The seven `Result` values, and what each does to the run:

```
PASS            ran, passed        - no
FAIL            ran, failed        - yes
ERROR           raised, aborted, or reported as an error - yes
SKIPPED         ran, chose not to test - no
NOTRUN          never executed     - no
FORCED_NOTRUN   does not apply to this UUT - no
NOREPORT        ran, never reported - no (but it warns)
```

The run is `FAIL` if any step is `FAIL` or `ERROR`.

```lua:docs/manuals/examples/test-writer/results.tslua
-- results.tslua: every value the Result enum can record.
--
-- Stop on Fail is turned off here so the run reaches every step. A Test Run is FAIL if
-- any step records FAIL or ERROR; the other Result values do not fail the run by
-- themselves. A step that reports nothing at all records NOREPORT, which ATE also logs
-- as a WARNING.
return ScheduleParams("Manual: every Result", "one step for each Result value")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A pass"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "within limits", 5.0, 5.0) end),

    StepParams("A fail"):setStepNo("2.")
      :setAction(function() ATE.report(Result.FAIL, "rail low", 4.1, 5.0) end),

    -- Reporting ERROR is a legitimate way to say "this step could not be completed";
    -- an uncaught Lua error in a step records ERROR too.
    StepParams("An error"):setStepNo("3.")
      :setAction(function() ATE.report(Result.ERROR, "the probe was not connected") end),

    -- SKIPPED is the schedule's own decision: the step ran, chose not to test anything.
    StepParams("Skipped on purpose"):setStepNo("4.")
      :setAction(function() ATE.report(Result.SKIPPED, "no test for this variant") end),

    -- NOTRUN can also be reported by the schedule; ATE records it for a step that never
    -- ran because the run stopped or was aborted.
    StepParams("Not run"):setStepNo("5.")
      :setAction(function() ATE.report(Result.NOTRUN, "skipped by the schedule") end),

    -- FORCED_NOTRUN: the schedule decided the step does not apply to this UUT.
    StepParams("Not fitted"):setStepNo("6.")
      :setAction(function() ATE.report(Result.FORCED_NOTRUN, "this board has no second rail") end),

    -- NOREPORT: this step runs and reports nothing, so ATE records NOREPORT and warns.
    StepParams("Reports nothing"):setStepNo("7.")
      :setAction(function() ATE.log(Log.INFO, "looked, but reported nothing") end),
  })
```

## Step Inputs

Every declaration needs `desc` and `type`. `type` is one of `ENUM`, `INTEGER`, `FLOAT`,
`STRING`, `PROMPT`, `POLL`.

| field | applies to | meaning |
| --- | --- | --- |
| `input` | `ENUM` | `RADIOBUT`, `CHECKBOX` or `PUSHBUT` |
| `enum` | `ENUM` | labels, at least one |
| `range` | `INTEGER`, `FLOAT` | `{min=, max=}`; empty is unbounded |
| `pattern` | `STRING` | regex the value must match |
| `prompt` | `PROMPT`, `POLL` | a string, or an array of button texts |
| `tooltip` | all | hover help |
| `orient` | all | `VERT` or `HORIZ` |

A field on the wrong type is a load error. A field the format does not know is ignored.

`ATE.input(n)` returns: the label for `RADIOBUT`/`PUSHBUT`; a Lua table of labels for
`CHECKBOX` (empty when none are ticked); an integer for `INTEGER`; a float for `FLOAT`; the
text for `STRING` and `PROMPT`; the clicked button's text for `POLL`. `POLL` never blocks: it
returns `nil` until clicked and is sticky until the Attempt starts again. `n` out of range is
an error.

```lua:docs/manuals/examples/test-writer/inputs.tslua
-- inputs.tslua: every Step Input type, and what ATE.input(n) returns for each.
--
-- Demonstrates:
--   * ENUM with each widget: RADIOBUT picks one label, CHECKBOX returns a table of
--     every label ticked (empty when none are), PUSHBUT is answered by pressing one.
--   * INTEGER and FLOAT with a range: the harness first answers out of range, which the
--     session refuses before the valid value is recorded.
--   * STRING with a pattern, refused the same way.
--   * PROMPT blocks the step until the Operator answers; POLL does not block, so the
--     step reads it in a loop. A POLL is sticky: once clicked it keeps returning that
--     button's text until the Attempt starts again.
--   * tooltip and orient apply to every input type.
return ScheduleParams("Manual: step inputs", "one step per Step Input type")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Pick one"):setStepNo("1.")
      :setInputs({ [1] = { desc = "LED colour", type = "ENUM", input = "RADIOBUT",
                          enum = { "Green", "Red", "Off" }, orient = "HORIZ",
                          tooltip = "Which LED the board lights" } })
      :setAction(function()
        local colour = ATE.input(1)
        ATE.report(Result.PASS, "the LED is " .. colour, colour, "Green")
      end),

    StepParams("Tick several"):setStepNo("2.")
      :setInputs({ [1] = { desc = "channels to measure", type = "ENUM", input = "CHECKBOX",
                          enum = { "A", "B", "C" }, orient = "VERT" } })
      :setAction(function()
        local channels = ATE.input(1)          -- a Lua table of labels, [] when none ticked
        ATE.report(Result.PASS, "measuring " .. table.concat(channels, ", "),
                   table.concat(channels, ","), "A,B")
      end),

    StepParams("Press a button"):setStepNo("3.")
      :setInputs({ [1] = { desc = "what to do next", type = "ENUM", input = "PUSHBUT",
                          enum = { "RETEST", "SKIP" } } })
      :setAction(function()
        local choice = ATE.input(1)
        ATE.report(Result.PASS, "the Operator pressed " .. choice, choice, "RETEST")
      end),

    StepParams("Whole number"):setStepNo("4.")
      :setInputs({ [1] = { desc = "test current, mA", type = "INTEGER",
                          range = { min = 1, max = 200 } } })
      :setAction(function()
        local current = ATE.input(1)           -- a Lua integer
        ATE.report(Result.PASS, "current is " .. current .. " mA", current, 42)
      end),

    StepParams("Decimal"):setStepNo("5.")
      :setInputs({ [1] = { desc = "rail voltage, V", type = "FLOAT",
                          range = { min = 0.5, max = 2.5 } } })
      :setAction(function()
        local volts = ATE.input(1)             -- a Lua float
        ATE.report(Result.PASS, "rail is " .. volts .. " V", volts, 1.25)
      end),

    StepParams("Text with a pattern"):setStepNo("6.")
      :setInputs({ [1] = { desc = "probe part number", type = "STRING",
                          pattern = "^[A-Z]{2}-[0-9]{4}$" } })
      :setAction(function()
        local part = ATE.input(1)
        ATE.report(Result.PASS, "probe " .. part, part, "AB-1234")
      end),

    StepParams("Blocking prompt"):setStepNo("7.")
      :setInputs({ [1] = { desc = "is the probe connected?", type = "PROMPT",
                          prompt = { "CONTINUE" } } })
      :setAction(function()
        local answer = ATE.input(1)            -- blocks until the Operator presses it
        ATE.report(Result.PASS, "probe confirmed: " .. answer, answer, "CONTINUE")
      end),

    StepParams("POLL in a loop"):setStepNo("8.")
      :setInputs({ [1] = { desc = "stop this step?", type = "POLL",
                          prompt = { "KEEP GOING", "STOP" } } })
      :setAction(function()
        local clicked, reads = nil, 0
        while reads < 8 do
          reads = reads + 1
          clicked = ATE.input(1)               -- nil until a button is clicked
          if clicked then break end
          ATE.sleep(0.1)                       -- a POLL never blocks the step
        end
        if clicked then
          ATE.report(Result.PASS, "clicked " .. clicked .. " after " .. reads .. " reads",
                     clicked, "STOP")
        else
          ATE.report(Result.FAIL, "no click after " .. reads .. " reads")
        end
      end),
  })
```

## Actions and Equipment

A Pre-test Action runs before the first step; a Post-test Action runs after the last one, in
reverse Pre-test order. Both are called with the run's standing result as their first argument
- always `"PASS"` for a Pre-test Action, because nothing has failed yet. A Pre-test Action that
raises aborts the run with `PRETEST_FAILED` and every step `NOTRUN`, and the Post-test Actions
still run: every schedule instance the Pre-test pass reached gets its teardown, in reverse
order, including the instance whose own Pre-test Action raised; an instance the pass never got
to has none. Post-test Actions run even after an abort: ATE clears the abort flag before them,
an abort during one ends that Action only, and an error in one fails the run while the rest
still run.

`ATE.addEquipment(name, serial, connection, calDue, notes)` - all strings, `name` required.
A repeat of the same `name` and `serial` adds no row and logs a DEBUG line.

```lua:docs/manuals/examples/test-writer/actions.tslua
-- actions.tslua: Pre-test and Post-test Actions, and Equipment.
--
-- Demonstrates:
--   * a Pre-test Action runs before the first step; a Post-test Action runs after the
--     last one. Both are called with the run's current result as their first argument:
--     a Pre-test Action always sees "PASS", the Post-test one sees the final PASS or FAIL.
--   * ATE.addEquipment is legal in an Action, a step, or both; the report's Equipment
--     section comes from it. Declaring the same name and serial twice adds no second row.
--   * ATE.report and ATE.input are NOT legal in an Action: only Test Steps have a result
--     and an Operator prompt.
return ScheduleParams("Manual: Actions", "Pre-test and Post-test Actions")
  :setPreTestAction(function(passFail)
    ATE.log(Log.INFO, "pre-test action, run stands at " .. passFail)
    ATE.addEquipment("Bench power supply", "PSU-123", "GPIB::1", "2026-12-01", "0-60 V")
  end)
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action, run result was " .. passFail)
  end)
  :setSchedule({
    StepParams("Measure the rail"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "rail at 5.0 V", 5.0, 5.0)
      end),
  })
```

`ATE.log(level, ...)` writes to the run log; `print` is redirected to
`ATE.log(Log.INFO, ...)`. `ATE.run` is the read-only table of the current run:

```lua:docs/manuals/examples/test-writer/logging.tslua
-- logging.tslua: logging inside a Test Run, and reading the run's own context.
--
-- Demonstrates:
--   * ATE.log(level, ...) with the Log enum; every argument is turned to text and the
--     parts are joined with spaces;
--   * print is redirected to ATE.log(Log.INFO, ...), so a stray print is still recorded;
--   * the main screen shows INFO and above; the debug window shows every level;
--   * ATE.run is a read-only table of the current Test Run. Writing to it is an error.
--   * DEBUG device tracing is off unless the application config turns it on with
--     deviceTrace.enabled; every line a schedule writes is stored with the Test Run.
return ScheduleParams("Manual: logging", "log levels, print and ATE.run")
  :setSchedule({
    StepParams("One line at each level"):setStepNo("1.")
      :setAction(function()
        ATE.log(Log.DEBUG, "a detail only the debug window shows")
        ATE.log(Log.INFO, "the rail settled", 5.0, "V")
        ATE.log(Log.WARNING, "the probe is loose")
        ATE.log(Log.ERROR, "the supply did not come up")
        print("print lands at INFO")
        ATE.report(Result.PASS, "logged at every level")
      end),

    StepParams("Who is running"):setStepNo("2.")
      :setAction(function()
        ATE.log(Log.INFO, "station " .. ATE.run.Station
                             .. ", project " .. ATE.run.Project
                             .. ", UUT " .. ATE.run.UUT_serial
                             .. ", operator " .. ATE.run.Operator
                             .. ", schedule " .. ATE.run.Test_Schedule_Name
                             .. ", release " .. ATE.run.Media_Release_Media_Number)
        ATE.report(Result.PASS, "read the run context", ATE.run.Operator, "Ada Lovelace")
      end),
  })
```

## Sub-schedules

`:setInclude(file, keyvals)` expands the file's steps beneath the header row. The included file
gets `keyvals` as the chunk argument (`local kv = ...`), and ATE runs it again per include, so
each include is its own instance. `keyvals` must be plain data (strings, numbers, booleans,
nested tables of those). A top-level schedule, or an include with no keyvals, gets `{}`.

The file is resolved against the release that owns the including schedule, then its
dependencies depth first - the same order as `require`. Prefixes are prepended exactly as
written and stack outward-in. A header row is not a Test Step: it has no Result and is not
counted in the summary. `setEnable(false)` on a header forces the default of every step
beneath it, and an instance whose whole subtree is disabled has both its Actions skipped. The
included schedule's doc fields and its `setStopOnFail` are ignored; only its Actions and steps
take part.

```lua:docs/manuals/examples/test-writer/main.tslua
-- main.tslua: the top-level schedule that includes Sub-schedules.
--
-- Demonstrates:
--   * :setInclude(file, keyvals) makes a step a header row, not a Test Step: the steps of
--     the included schedule are pulled in beneath it.
--   * The same file can be included more than once; each include is its own instance with
--     its own keyvals.
--   * :setPrefix is prepended to the included steps' Step Numbers, as written, with no
--     separator, and prefixes stack as the includes nest.
--   * :setEnable on a header forces the default of every step beneath it.
return ScheduleParams("Manual: sub-schedules", "includes, keyvals, prefixes and nesting")
  :setDocTitle("Manual examples - Sub-schedules")
  :setDocId("MR-MANUAL-SUBS")
  :setDocRev("1.0")
  :setSchedule({
    StepParams("Rail present"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "the rail is fitted") end),

    -- 2.1. and 2.2. come from channel.tslua, included once for channel 1.
    StepParams("Channel 1"):setStepNo("2.")
      :setInclude("channel.tslua", { channel = 1, label = "left rail" }):setPrefix("2."),

    -- A disabled header: every step beneath it records NOTRUN, and its Actions are skipped.
    StepParams("Channel 2, not fitted"):setStepNo("3.")
      :setEnable(false)
      :setInclude("channel.tslua", { channel = 2, label = "right rail" }):setPrefix("3."),
  })
```

```lua:docs/manuals/examples/test-writer/channel.tslua
-- channel.tslua: a Sub-schedule. It is complete enough to run on its own, and is listed
-- as hidden in manual.atedef so the Operator never picks it directly.
--
-- Each include runs this file again, with the keyvals passed as the chunk's argument.
-- Its own Actions take part in the run's Pre-test and Post-test order.
local kv = ...
local channel = kv.channel or 1
local label = kv.label or "a channel"

return ScheduleParams("Channel check", "one output channel")
  :setPreTestAction(function()
    ATE.log(Log.INFO, "channel " .. channel .. " (" .. label .. ") pre-test")
  end)
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "channel " .. channel .. " post-test, run stands at " .. passFail)
  end)
  :setSchedule({
    StepParams("Switch the channel on"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "channel " .. channel .. " switches on")
      end),

    -- A Sub-schedule inside a Sub-schedule: step numbers become 2.2.1. and so on.
    StepParams("Its LEDs"):setStepNo("2.")
      :setInclude("led.tslua", { channel = channel }):setPrefix("2."),
  })
```

```lua:docs/manuals/examples/test-writer/led.tslua
-- led.tslua: the innermost Sub-schedule, included by channel.tslua.
local kv = ...
local channel = kv.channel or 1

return ScheduleParams("LED check", "the indicator LEDs of one channel")
  :setSchedule({
    StepParams("The LED lights"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "channel " .. channel .. " LED is lit")
      end),
  })
```

## Modules and the `.atedef`

`require` is ATE's own. It resolves the name against the release's files, then its
dependencies, never against `package.path`, which ATE empties. An unlisted name is an error. A
module runs once per Test Run and its return value is cached. `ate.` is reserved for modules
bundled with ATE (`ate.devices`).

Checksums are SHA-256 of the raw bytes, lowercase hex. A mismatch is a WARNING, the file still
loads, and both checksums are recorded with the run. `ate definition update <file.atedef>`
computes each listed file's SHA-256 and writes it back over the old value, filling in any
`sha256` that is absent; the file is only rewritten if a checksum actually changed, so a
correct `.atedef` comes back byte for byte the same. A listed file it cannot read keeps its
current value and warns.

Every field a `.atedef` can carry:

| Field | Meaning |
| --- | --- |
| `format` | must be `1`; anything else makes the definition broken |
| `mediaNumber` | the release's identity; not a version |
| `name`, `version` | `version` must be valid semver 2.0.0; other releases name it in `depends` |
| `depends` | a list of `{"name": ..., "version": ...}`; resolved depth first, in declaration order |
| `files[]` | one entry per Lua file: `path`, `sha256` |
| `files[].path` | relative to the `.atedef` (or absolute), forward slashes on both platforms |
| `files[].hidden` | only on a `.tslua`: keeps it off the Run screen while still allowing it as a Sub-schedule |
| `files[].module` | only on a `.lua`: the name `require` uses. Otherwise the path derives it: `lib/rails.lua` is `lib.rails`. An absolute path, or one containing `..`, must state it |

Here is the definition the manual's own examples ship, exactly as it sits on disk - note
`channel.tslua` and `led.tslua` marked `hidden`, and `modules/limits.lua` given the module name
`board_limits`:

```json:docs/manuals/examples/test-writer/manual.atedef
{
  "format": 1,
  "mediaNumber": "MR-MANUAL-TW",
  "name": "Test writer manual examples",
  "version": "1.0.0",
  "files": [
    {
      "path": "first.tslua",
      "sha256": "a260d2b923819ff6f71804370d4afce9ac77269ec9d9cf9c4b0dae35b85e48c8"
    },
    {
      "path": "steps.tslua",
      "sha256": "2203cddc55bf0daf0e557d3ecf1b847e77bf8fa873385cea775e32f240c5af97"
    },
    {
      "path": "inputs.tslua",
      "sha256": "6a746ebad482542f81d5b846b65a57adb467942e5289d9fc7e76b9bdaabd22fc"
    },
    {
      "path": "actions.tslua",
      "sha256": "a15e9a9fa060f0e829cc2e047d2c0313a1a2203c7c33356e792f699d1b02c526"
    },
    {
      "path": "retry.tslua",
      "sha256": "5375d9870ad11af1553eeaa1d5fd27ce586c2a5cce78af6b4a493a029fee58c9"
    },
    {
      "path": "results.tslua",
      "sha256": "49f80374f637c13e34281140912335fcb583fa87865674def089b18188130ce7"
    },
    {
      "path": "main.tslua",
      "sha256": "fc768ae110764fb3a7a9a4a29fdd93df3beafc09b8fff6ea7fa71efa825964d4"
    },
    {
      "path": "channel.tslua",
      "hidden": true,
      "sha256": "1cecd8b3566e4783c86375ebcdac6b69eaae52f1ea74e32c495babf7009b6e63"
    },
    {
      "path": "led.tslua",
      "hidden": true,
      "sha256": "3477e6a5a1578108bd76687e13e211ca1c48b5bac3343160a4e86319f87f46f8"
    },
    {
      "path": "modules.tslua",
      "sha256": "f34234441c454b9e0559e778fb86c6f10086f2edd4248207953ce67f22fae386"
    },
    {
      "path": "lib/rails.lua",
      "sha256": "392e385ab81dfb814a7ddec0cb1c0799c97d714b3d97c2cfb3306e6ba6cde029"
    },
    {
      "path": "modules/limits.lua",
      "module": "board_limits",
      "sha256": "ea8a21a8a7bef16b0abaa9efcc07c8f08416b4a07b712ac35b9ffdbf8c31fd99"
    },
    {
      "path": "tcp.tslua",
      "sha256": "8439beda05cc96f5c6bd69629951fc2318a7526eb085fb2bb322fc711c2cb8c7"
    },
    {
      "path": "udp.tslua",
      "sha256": "324ce6659ab74bcea159fbe20d063925e5b5a88992b2ad1755e4d68625f461b4"
    },
    {
      "path": "serial.tslua",
      "sha256": "dd717a441db74b490b84b999f82a54b7e0d2b3b3185906ae83150562c7dcb768"
    },
    {
      "path": "visa.tslua",
      "sha256": "5c9d69646277814d6d0e134380ba32e6402ce4d9d3f4006dbe4f622bb107afb8"
    },
    {
      "path": "errors.tslua",
      "sha256": "66151279fc9e62462e80a75e273785fac0bb1c8f2a7bf0aeec62f312a1fb0fe2"
    },
    {
      "path": "left_open.tslua",
      "sha256": "a53f424fe15813bafdcdf74f0c7eb4e69b37bbbb5fa27d833b58eb7d1923b279"
    },
    {
      "path": "logging.tslua",
      "sha256": "ee6b7ee6f1a3b1ed15522544d93645c694dfd29d27d53188416c8e956170dd02"
    },
    {
      "path": "abort.tslua",
      "sha256": "b164d0057d61fa7d5f4092a2b47154dc4ca16dfd685640ef09338f4b862eb308"
    },
    {
      "path": "sandbox.tslua",
      "sha256": "e40b7d8eafd167b5d52f844957962b505f53009d791a38bb2729413462aaee99"
    },
    {
      "path": "forbidden.tslua",
      "sha256": "eae7b5e968792a6abdcf883ccde1e956fdf4f7a6c14e2d8aab8ff02c1fc8dd97"
    }
  ]
}
```

```lua:docs/manuals/examples/test-writer/modules.tslua
-- modules.tslua: sharing code with require, including ATE's own bundled modules.
--
-- Demonstrates:
--   * require finds only the files the Test Definition lists (and its dependencies'),
--     never anything on package.path;
--   * the module name is derived from a plain relative path, or given explicitly with the
--     "module" field;
--   * the reserved "ate." namespace is ATE's own: require("ate.devices") is always ATE's
--     Device params builder, never a file in the Media Release.
local rails = require("lib.rails")      -- derived name: the path without .lua, / as .
local limits = require("board_limits")  -- explicit "module" name in manual.atedef
local devices = require("ate.devices")  -- bundled with ATE

return ScheduleParams("Manual: modules", "require and the ate. namespace")
  :setSchedule({
    StepParams("Check every rail"):setStepNo("1.")
      :setAction(function()
        local ok = true
        for channel = 1, 3 do
          local volts = rails.nominalVolts(channel)
          ok = ok and volts >= limits.low and volts <= limits.high
          ATE.log(Log.INFO, "channel " .. channel .. " nominal " .. volts .. " V")
        end
        ATE.report(ok and Result.PASS or Result.FAIL,
                   "every rail is inside " .. limits.low .. " to " .. limits.high .. " V")
      end),

    -- A builder is just a params table: nothing is opened, so no Device is involved.
    StepParams("Build params without opening"):setStepNo("2.")
      :setAction(function()
        local params = devices.Tcp("127.0.0.1", 5025):setTimeout(2.5)
        ATE.report(Result.PASS, "timeout set to " .. params.timeout, params.timeout, 2.5)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/lib/rails.lua
-- lib/rails.lua: an ordinary Lua module. Its module name is derived from its path:
-- "lib/rails.lua" becomes "lib.rails". require runs a module once per Test Run and
-- caches what it returns; later requires of the same name get the cached value.
local rails = {}

function rails.nominalVolts(channel)
  return 1.5 * channel
end

return rails
```

```lua:docs/manuals/examples/test-writer/modules/limits.lua
-- modules/limits.lua: a module whose name is given explicitly in manual.atedef with the
-- "module" field, because a name derived from the path would be "modules.limits".
return { low = 1.0, high = 5.0 }
```

## Devices

`ATE.open(deviceType, params [, onError])` returns an opaque handle. Every field `open` needs
is required - there are no defaults in `ATE.open`; the builders supply them.

| Device Type | `open` fields | builder (defaults) |
| --- | --- | --- |
| `DeviceType.TCP` | `host`, `port`, `timeout`, `readTerm`, `writeTerm` | `devices.Tcp(host, port)`: timeout 1.0, terminators `"\n"` |
| `DeviceType.SERIAL` | `port`, `baud`, `dataBits`, `stopBits`, `parity`, `flowControl`, `timeout`, `readTerm`, `writeTerm` | `devices.Serial(port)`: 9600, 8, 1, `Parity.NONE`, `FlowControl.NONE`, 1.0, terminators `"\n"` |
| `DeviceType.UDP` | `host`, `port`, `localPort`, `timeout` | `devices.Udp(host, port)`: `localPort` 0, timeout 1.0 |
| `DeviceType.VISA` | `resource`, `timeout`, `readTerm`, `writeTerm` | `devices.Visa(resource)`: timeout 1.0, terminators `"\n"` |

Per-call fields, all optional, falling back to the open-time configuration: `read` takes
`size`, `timeout`, `readTerm`; `write` takes `data` (required), `writeTerm`, `timeout`;
`close` takes no fields of its own but accepts an optional params table and an optional
`onError` — `ATE.close(dev [, params] [, onError])` — which is used if the close itself fails.
UDP has no terminators: its `read` takes `size` and `timeout`, and its `write` takes `data`
(required) and `timeout`. Passing a `readTerm` to a UDP Device is an error naming the field.
Builder setters: `setHost`, `setPort`, `setLocalPort`, `setResource`, `setBaud`, `setDataBits`,
`setStopBits`, `setParity`, `setFlowControl`, `setTimeout`, `setReadTerm`, `setWriteTerm`,
`setSize`, and `devices.Write(data)`.

Reads and writes are raw bytes. The terminator is stripped from `data`; `count` is what is
left. A write's `count` includes the terminator.

TCP, SERIAL and UDP are read in the background from `open` to `close` and buffered, capped at
the Station config key `devices.receiveBufferBytes` (16777216, 16 MiB); on overflow ATE drops
the oldest bytes, logs one WARNING, and the next read carries `overflow = true`. `ATE.flush(dev)`
empties the buffer and returns the count discarded. VISA has no background reader and no buffer,
so `flush` discards nothing.

A VISA resource string names the instrument, and the shape matters:
`GPIB0::5::INSTR` is a GPIB instrument, `TCPIP0::192.168.1.10::inst0::INSTR` is a VXI-11
instrument, and `TCPIP0::127.0.0.1::5025::SOCKET` is a plain TCP socket wrapped in a VISA
session - the same stream a `DeviceType.TCP` Device would open, with pyvisa in the way. ATE
uses whichever pyvisa backend it finds; only pyvisa-py is shipped, which reaches TCPIP and
USBTMC. A GPIB instrument needs a real VISA driver on the Station (NI-VISA or linux-GPIB): ask
the schedule author which resource string their instrument needs before writing the schedule,
because nothing installed with ATE can open a GPIB address.

```lua:docs/manuals/examples/test-writer/tcp.tslua
-- tcp.tslua: a TCP Device, from open to close.
--
-- The host and port come from the environment, which the test harness sets to a
-- simulator peer listening on 127.0.0.1. Point them at a real instrument to use this
-- schedule as it is.
--
-- Demonstrates:
--   * the ate.devices builders, whose setters chain, and how a Device opened in the
--     Pre-test Action is still open in the steps, because a device object lives for the
--     whole Test Run;
--   * per-call overrides (timeout, readTerm, size) that do not change the open-time
--     configuration;
--   * a read that times out after some bytes is a partial read, not a Device failure;
--   * a read that times out with nothing at all is a Device failure and goes through
--     onError, which is given the failure table and the params table and may return a
--     value for the call to carry on with;
--   * flush discards whatever the receive buffer already holds.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

local instrument = nil

return ScheduleParams("Manual: TCP", "open, write, read, flush and close a TCP Device")
  :setStopOnFail(false)
  :setPreTestAction(function()
    -- devices.Tcp(host, port) fills in every field TCP needs: timeout 1.0 s,
    -- readTerm and writeTerm "\n". open has no defaults of its own, so a plain table
    -- would have to name them all.
    instrument = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(2.0))
    ATE.addEquipment("SCPI simulator", "SIM-TCP", "TCP " .. host .. ":" .. port, "2026-01-01")
  end)
  :setPostTestAction(function(passFail)
    ATE.close(instrument)   -- closing a second time does nothing and still reports ok
  end)
  :setSchedule({
    StepParams("Identify the instrument"):setStepNo("1.")
      :setAction(function()
        local sent = ATE.write(instrument, devices.Write("*IDN?"))
        local reply = ATE.read(instrument, devices.Read():setTimeout(1.0))
        ATE.report((sent.ok and reply.count > 0) and Result.PASS or Result.FAIL,
                   "reply: " .. (reply.data or "nothing"),
                   reply.count, 26)
      end),

    -- The peer answers "MEAS:VOLT?" with "1.234\n". readTerm "3" stops the read at the "3",
    -- so the first read takes "1.2" and the second takes the rest of the reply.
    StepParams("Stop a read at a terminator"):setStepNo("2.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MEAS:VOLT?"))
        local first = ATE.read(instrument, devices.Read():setReadTerm("3"))
        local rest = ATE.read(instrument)
        ATE.report((first.data == "1.2" and rest.data == "4") and Result.PASS or Result.FAIL,
                   "first read " .. (first.data or "nothing") .. ", second " .. (rest.data or "nothing"),
                   first.count + rest.count, 4)
      end),

    -- "PARTIAL?" is answered with bytes and no terminator: the read times out having taken
    -- some of them. That is partial = true, a WARNING in the log, and not a Device failure.
    StepParams("A read that times out partway"):setStepNo("3.")
      :setAction(function()
        ATE.write(instrument, devices.Write("PARTIAL?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3))
        ATE.report((r.partial and r.count > 0) and Result.PASS or Result.FAIL,
                   "got " .. r.count .. " bytes before the timeout",
                   tostring(r.partial), "true")
      end),

    -- "NEVER?" is answered with silence: the read times out with no bytes at all, which IS
    -- a Device failure, so onError runs. Returning a value makes the call continue with it.
    StepParams("Handle a read that times out"):setStepNo("4.")
      :setAction(function()
        ATE.write(instrument, devices.Write("NEVER?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3), function(opResult, opArgs)
          ATE.log(Log.WARNING, "no reply from the instrument: " .. opResult.err)
          return { data = nil, count = 0, partial = false }
        end)
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "the timeout was handled, so the step carried on")
      end),

    -- The peer sends three lines; the schedule reads the first and throws away the rest.
    StepParams("Discard the receive buffer"):setStepNo("5.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MULTI?"))
        ATE.read(instrument, devices.Read():setSize(7))
        local f = ATE.flush(instrument)
        ATE.report((f.ok and f.count > 0) and Result.PASS or Result.FAIL, "discarded " .. f.count .. " buffered bytes")
      end),
  })
```

```lua:docs/manuals/examples/test-writer/udp.tslua
-- udp.tslua: a UDP Device, where one datagram is one message.
--
-- Demonstrates:
--   * UDP has no terminators, so readTerm and writeTerm are not fields this Device Type
--     knows; passing one is a schedule bug that raises naming the field;
--   * a datagram longer than the size asked for is truncated and returned with partial =
--     true, which the log calls a truncation rather than a timeout;
--   * the receive buffer is a queue of whole datagrams, so a reply that arrives before the
--     schedule reads it is not lost.
local devices = require("ate.devices")

local host = os.getenv("ATE_UDP_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_UDP_PORT")) or 5026

return ScheduleParams("Manual: UDP", "send and read datagrams")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Echo one datagram"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("PING"))
        local reply = ATE.read(dev)
        ATE.report(reply.data == "ECHO:PING" and Result.PASS or Result.FAIL,
                   "the peer echoed " .. (reply.data or "nothing"),
                   reply.count, 9)
        ATE.close(dev)
      end),

    -- Ask for 10 bytes of a 40-byte datagram: ATE returns the first 10 and says partial.
    StepParams("Truncate a datagram"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("a datagram longer than the size asked for"))
        local r = ATE.read(dev, devices.Read():setSize(10))
        ATE.report((r.count == 10 and r.partial) and Result.PASS or Result.FAIL,
                   "read " .. r.count .. " of a longer datagram",
                   r.count, 10)
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/serial.tslua
-- serial.tslua: a SERIAL Device through pyserial.
--
-- The port here is pyserial's socket:// URL, which opens a TCP connection to the
-- simulator peer; a real station names a COM or tty port instead.
--
-- Demonstrates:
--   * open has no defaults, so every field is named;
--   * the Parity and FlowControl enums;
--   * the same connection read with the Device's own terminator.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: SERIAL", "open a serial port and read a reply")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Open the port"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.SERIAL, {
          port = "socket://" .. host .. ":" .. port,
          baud = 115200,
          dataBits = 8,
          stopBits = 1,
          parity = Parity.NONE,
          flowControl = FlowControl.NONE,
          timeout = 2.0,
          readTerm = "\n",
          writeTerm = "\n",
        })
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "serial reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/visa.tslua
-- visa.tslua: a VISA instrument through pyvisa.
--
-- VISA has no background reader: ATE reads the instrument only when the schedule asks,
-- because polling an SCPI instrument with reads that have nothing pending makes it log
-- "Query UNTERMINATED" (-420) and ties up the bus. ATE ships pyvisa-py; a VISA resource
-- string names the instrument, here the simulator peer as a plain TCPIP socket.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: VISA", "read an instrument through pyvisa")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Ask the instrument who it is"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.VISA,
          devices.Visa("TCPIP0::" .. host .. "::" .. port .. "::SOCKET"):setTimeout(2.0))
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        local f = ATE.flush(dev)   -- VISA has no receive buffer: this discards nothing
        ATE.log(Log.INFO, "flush discarded " .. f.count .. " bytes")
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "VISA reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

A handle lives for the whole run. Closing a closed Device does nothing and reports success;
using one is a failure through `onError`. At the end of every run ATE closes whatever is still
open and logs a WARNING for each.

```lua:docs/manuals/examples/test-writer/left_open.tslua
-- left_open.tslua: a Device the schedule never closes.
--
-- A Test Run closes every Device still open when it ends, and logs one WARNING naming
-- each one. That is why a Device opened in a Pre-test Action does not have to be closed
-- by hand - it is worth closing anyway, so the log stays clean.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: a Device left open", "ATE closes what the schedule forgets")
  :setSchedule({
    StepParams("Open and forget"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.report(Result.PASS, "opened a Device and left it open")
      end),
  })
```

## Device failures, Stop on Fail, Retry, Abort

`onError(opResult, opArgs)` is called when the operation fails. If it returns anything but
`nil`, the call returns that value and the schedule continues. If it returns `nil`, or there is
no handler, the call raises a Lua error and the step records `Result.ERROR`. A read that times
out having received some bytes is not a failure: it returns `partial = true` and logs a
WARNING. ATE never reconnects: close the Device and open it again with the same params.

A field the Device Type does not know, a missing required field, or a wrong-typed value is not a
Device failure. It never reaches `onError`; the call returns `false` with the message as its
second result and the traceback is logged at ERROR.

```lua:docs/manuals/examples/test-writer/errors.tslua
-- errors.tslua: how Device failures reach the schedule, and what does not.
--
-- Demonstrates:
--   * a Device failure with no onError raises a Lua error, which ends the step as
--     Result.ERROR;
--   * an onError that returns a value makes the call continue with that value;
--   * a bad argument (a misspelled field) is a schedule bug, not a Device failure: it
--     never reaches onError, it is logged at ERROR, and the call returns false with the
--     message as its second result;
--   * the handler given to open is the fallback for that Device's read, write and close.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025
local deadPort = tonumber(os.getenv("ATE_DEAD_PORT")) or 1

return ScheduleParams("Manual: Device errors", "onError, a closed Device and a bad field")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A misspelled field"):setStepNo("1.")
      :setAction(function()
        local opened, why = ATE.open(DeviceType.TCP, { host = host, port = port, timeout = 1.0,
                                                     readTerm = "\n", writeTerm = "\n", buad = 9600 })
        ATE.report(opened == false and Result.PASS or Result.FAIL, "the open was refused: " .. why)
      end),

    -- Nothing is listening on deadPort: the open fails. This handler logs and returns nil,
    -- which means "no recovery", so ATE.open raises and the step records Result.ERROR.
    StepParams("A port nothing listens on"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, deadPort):setTimeout(0.5),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "nothing is listening on " .. opArgs.host .. ":" .. opArgs.port)
            return nil
          end)
        ATE.report(Result.PASS, "this line is never reached")
      end),

    -- A Device that is closed cannot be read. With no handler the call raises.
    StepParams("Read a closed Device"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.close(dev)
        ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(Result.PASS, "this line is never reached either")
      end),

    -- The same failure, handled: the step carries on with what onError returns.
    StepParams("Handle the closed Device"):setStepNo("4.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "the Device is gone: " .. opResult.err)
            return { data = nil, count = 0, partial = false }
          end)
        ATE.close(dev)
        local r = ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "recovered from reading a closed Device")
      end),
  })
```

Stop on Fail pauses the run at a `FAIL` or `ERROR` step so the Operator can choose `CONTINUE`,
`RETRY` or `ABORT`. Retry runs the action again from the start with POLL reset and blocking
inputs cleared; each Attempt is recorded and the step's result is the last Attempt's. There is
no Attempt number in Lua - keep your own count.

```lua:docs/manuals/examples/test-writer/retry.tslua
-- retry.tslua: Stop on Fail, and what a Retry does.
--
-- Demonstrates:
--   * setStopOnFail(true) is the default: a step ending FAIL or ERROR pauses the run and
--     the Operator chooses Continue, Retry or Abort.
--   * Retry runs the step's action again from the beginning. Each Attempt is recorded;
--     the step's result is its last Attempt's, and the Operator's choice is recorded too.
--   * ATE gives a step no way to read its own Attempt number, so the schedule keeps the
--     count itself in a local the action closes over.
local attempts = 0

return ScheduleParams("Manual: retry", "the first Attempt fails, the Retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Retry this one"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt " .. attempts .. ": the joint is cold", 0.2, 2.4)
        else
          ATE.report(Result.PASS, "attempt " .. attempts .. ": the joint is warm", 2.4, 2.4)
        end
      end),

    -- Only reached after the step passed, or after the Operator chose to continue.
    StepParams("Next check"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "the rail is steady", 5.0, 5.0)
      end),
  })
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

## Steps and Results

`ATE.report(passFail, msg, actual, expected)` records the step. The last call in an Attempt
wins; every call is logged; no call means `NOREPORT` and ATE logs a WARNING. `actual` and
`expected` are stored as text and ATE compares nothing - the schedule decides.

```lua:docs/manuals/examples/test-writer/steps.tslua
-- steps.tslua: Step Numbers, Step Names, enabled defaults, and reporting.
--
-- Demonstrates:
--   * every ordinary step needs setStepNo and setAction;
--   * setEnable(false) starts a step disabled, and a step disabled at run start
--     records Result.NOTRUN without ever executing;
--   * a step that never calls ATE.report records Result.NOREPORT and logs a WARNING;
--   * several ATE.report calls in one Attempt: every one is logged, the last one counts;
--   * Actual and Expected are stored as text, and ATE does no comparing: the schedule
--     decides whether the value passes.
return ScheduleParams("Manual: steps", "step numbers, names and the enabled default")
  :setDocTitle("Manual examples - Steps")
  :setDocId("MR-MANUAL-STEPS")
  :setDocRev("1.0")
  :setSchedule({
    StepParams("First check"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "rail is within tolerance", 5.0, 5.0)
      end),

    -- Two reports in one Attempt: the log shows both, the step's result is the second.
    StepParams("Report twice"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.FAIL, "first reading looked low", 4.9, 5.0)
        ATE.report(Result.PASS, "second reading is fine", 5.0, 5.0)
      end),

    -- Disabled from the start: the Operator can turn it on before the run.
    StepParams("Optional check"):setStepNo("3.")
      :setEnable(false)
      :setAction(function()
        ATE.report(Result.PASS, "only runs when the Operator enables it")
      end),

    -- A step that does work but never reports: ATE records NOREPORT and warns.
    StepParams("Forgot to report"):setStepNo("4.")
      :setAction(function()
        ATE.log(Log.INFO, "measured something, but did not report it")
      end),
  })
```

The seven `Result` values, and what each does to the run:

```
PASS            ran, passed        - no
FAIL            ran, failed        - yes
ERROR           raised, aborted, or reported as an error - yes
SKIPPED         ran, chose not to test - no
NOTRUN          never executed     - no
FORCED_NOTRUN   does not apply to this UUT - no
NOREPORT        ran, never reported - no (but it warns)
```

The run is `FAIL` if any step is `FAIL` or `ERROR`.

```lua:docs/manuals/examples/test-writer/results.tslua
-- results.tslua: every value the Result enum can record.
--
-- Stop on Fail is turned off here so the run reaches every step. A Test Run is FAIL if
-- any step records FAIL or ERROR; the other Result values do not fail the run by
-- themselves. A step that reports nothing at all records NOREPORT, which ATE also logs
-- as a WARNING.
return ScheduleParams("Manual: every Result", "one step for each Result value")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A pass"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "within limits", 5.0, 5.0) end),

    StepParams("A fail"):setStepNo("2.")
      :setAction(function() ATE.report(Result.FAIL, "rail low", 4.1, 5.0) end),

    -- Reporting ERROR is a legitimate way to say "this step could not be completed";
    -- an uncaught Lua error in a step records ERROR too.
    StepParams("An error"):setStepNo("3.")
      :setAction(function() ATE.report(Result.ERROR, "the probe was not connected") end),

    -- SKIPPED is the schedule's own decision: the step ran, chose not to test anything.
    StepParams("Skipped on purpose"):setStepNo("4.")
      :setAction(function() ATE.report(Result.SKIPPED, "no test for this variant") end),

    -- NOTRUN can also be reported by the schedule; ATE records it for a step that never
    -- ran because the run stopped or was aborted.
    StepParams("Not run"):setStepNo("5.")
      :setAction(function() ATE.report(Result.NOTRUN, "skipped by the schedule") end),

    -- FORCED_NOTRUN: the schedule decided the step does not apply to this UUT.
    StepParams("Not fitted"):setStepNo("6.")
      :setAction(function() ATE.report(Result.FORCED_NOTRUN, "this board has no second rail") end),

    -- NOREPORT: this step runs and reports nothing, so ATE records NOREPORT and warns.
    StepParams("Reports nothing"):setStepNo("7.")
      :setAction(function() ATE.log(Log.INFO, "looked, but reported nothing") end),
  })
```

## Step Inputs

Every declaration needs `desc` and `type`. `type` is one of `ENUM`, `INTEGER`, `FLOAT`,
`STRING`, `PROMPT`, `POLL`.

| field | applies to | meaning |
| --- | --- | --- |
| `input` | `ENUM` | `RADIOBUT`, `CHECKBOX` or `PUSHBUT` |
| `enum` | `ENUM` | labels, at least one |
| `range` | `INTEGER`, `FLOAT` | `{min=, max=}`; empty is unbounded |
| `pattern` | `STRING` | regex the value must match |
| `prompt` | `PROMPT`, `POLL` | a string, or an array of button texts |
| `tooltip` | all | hover help |
| `orient` | all | `VERT` or `HORIZ` |

A field on the wrong type is a load error. A field the format does not know is ignored.

`ATE.input(n)` returns: the label for `RADIOBUT`/`PUSHBUT`; a Lua table of labels for
`CHECKBOX` (empty when none are ticked); an integer for `INTEGER`; a float for `FLOAT`; the
text for `STRING` and `PROMPT`; the clicked button's text for `POLL`. `POLL` never blocks: it
returns `nil` until clicked and is sticky until the Attempt starts again. `n` out of range is
an error.

```lua:docs/manuals/examples/test-writer/inputs.tslua
-- inputs.tslua: every Step Input type, and what ATE.input(n) returns for each.
--
-- Demonstrates:
--   * ENUM with each widget: RADIOBUT picks one label, CHECKBOX returns a table of
--     every label ticked (empty when none are), PUSHBUT is answered by pressing one.
--   * INTEGER and FLOAT with a range: the harness first answers out of range, which the
--     session refuses before the valid value is recorded.
--   * STRING with a pattern, refused the same way.
--   * PROMPT blocks the step until the Operator answers; POLL does not block, so the
--     step reads it in a loop. A POLL is sticky: once clicked it keeps returning that
--     button's text until the Attempt starts again.
--   * tooltip and orient apply to every input type.
return ScheduleParams("Manual: step inputs", "one step per Step Input type")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Pick one"):setStepNo("1.")
      :setInputs({ [1] = { desc = "LED colour", type = "ENUM", input = "RADIOBUT",
                          enum = { "Green", "Red", "Off" }, orient = "HORIZ",
                          tooltip = "Which LED the board lights" } })
      :setAction(function()
        local colour = ATE.input(1)
        ATE.report(Result.PASS, "the LED is " .. colour, colour, "Green")
      end),

    StepParams("Tick several"):setStepNo("2.")
      :setInputs({ [1] = { desc = "channels to measure", type = "ENUM", input = "CHECKBOX",
                          enum = { "A", "B", "C" }, orient = "VERT" } })
      :setAction(function()
        local channels = ATE.input(1)          -- a Lua table of labels, [] when none ticked
        ATE.report(Result.PASS, "measuring " .. table.concat(channels, ", "),
                   table.concat(channels, ","), "A,B")
      end),

    StepParams("Press a button"):setStepNo("3.")
      :setInputs({ [1] = { desc = "what to do next", type = "ENUM", input = "PUSHBUT",
                          enum = { "RETEST", "SKIP" } } })
      :setAction(function()
        local choice = ATE.input(1)
        ATE.report(Result.PASS, "the Operator pressed " .. choice, choice, "RETEST")
      end),

    StepParams("Whole number"):setStepNo("4.")
      :setInputs({ [1] = { desc = "test current, mA", type = "INTEGER",
                          range = { min = 1, max = 200 } } })
      :setAction(function()
        local current = ATE.input(1)           -- a Lua integer
        ATE.report(Result.PASS, "current is " .. current .. " mA", current, 42)
      end),

    StepParams("Decimal"):setStepNo("5.")
      :setInputs({ [1] = { desc = "rail voltage, V", type = "FLOAT",
                          range = { min = 0.5, max = 2.5 } } })
      :setAction(function()
        local volts = ATE.input(1)             -- a Lua float
        ATE.report(Result.PASS, "rail is " .. volts .. " V", volts, 1.25)
      end),

    StepParams("Text with a pattern"):setStepNo("6.")
      :setInputs({ [1] = { desc = "probe part number", type = "STRING",
                          pattern = "^[A-Z]{2}-[0-9]{4}$" } })
      :setAction(function()
        local part = ATE.input(1)
        ATE.report(Result.PASS, "probe " .. part, part, "AB-1234")
      end),

    StepParams("Blocking prompt"):setStepNo("7.")
      :setInputs({ [1] = { desc = "is the probe connected?", type = "PROMPT",
                          prompt = { "CONTINUE" } } })
      :setAction(function()
        local answer = ATE.input(1)            -- blocks until the Operator presses it
        ATE.report(Result.PASS, "probe confirmed: " .. answer, answer, "CONTINUE")
      end),

    StepParams("POLL in a loop"):setStepNo("8.")
      :setInputs({ [1] = { desc = "stop this step?", type = "POLL",
                          prompt = { "KEEP GOING", "STOP" } } })
      :setAction(function()
        local clicked, reads = nil, 0
        while reads < 8 do
          reads = reads + 1
          clicked = ATE.input(1)               -- nil until a button is clicked
          if clicked then break end
          ATE.sleep(0.1)                       -- a POLL never blocks the step
        end
        if clicked then
          ATE.report(Result.PASS, "clicked " .. clicked .. " after " .. reads .. " reads",
                     clicked, "STOP")
        else
          ATE.report(Result.FAIL, "no click after " .. reads .. " reads")
        end
      end),
  })
```

## Actions and Equipment

A Pre-test Action runs before the first step; a Post-test Action runs after the last one, in
reverse Pre-test order. Both are called with the run's standing result as their first argument
- always `"PASS"` for a Pre-test Action, because nothing has failed yet. A Pre-test Action that
raises aborts the run with `PRETEST_FAILED` and every step `NOTRUN`, and the Post-test Actions
still run: every schedule instance the Pre-test pass reached gets its teardown, in reverse
order, including the instance whose own Pre-test Action raised; an instance the pass never got
to has none. Post-test Actions run even after an abort: ATE clears the abort flag before them,
an abort during one ends that Action only, and an error in one fails the run while the rest
still run.

`ATE.addEquipment(name, serial, connection, calDue, notes)` - all strings, `name` required.
A repeat of the same `name` and `serial` adds no row and logs a DEBUG line.

```lua:docs/manuals/examples/test-writer/actions.tslua
-- actions.tslua: Pre-test and Post-test Actions, and Equipment.
--
-- Demonstrates:
--   * a Pre-test Action runs before the first step; a Post-test Action runs after the
--     last one. Both are called with the run's current result as their first argument:
--     a Pre-test Action always sees "PASS", the Post-test one sees the final PASS or FAIL.
--   * ATE.addEquipment is legal in an Action, a step, or both; the report's Equipment
--     section comes from it. Declaring the same name and serial twice adds no second row.
--   * ATE.report and ATE.input are NOT legal in an Action: only Test Steps have a result
--     and an Operator prompt.
return ScheduleParams("Manual: Actions", "Pre-test and Post-test Actions")
  :setPreTestAction(function(passFail)
    ATE.log(Log.INFO, "pre-test action, run stands at " .. passFail)
    ATE.addEquipment("Bench power supply", "PSU-123", "GPIB::1", "2026-12-01", "0-60 V")
  end)
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action, run result was " .. passFail)
  end)
  :setSchedule({
    StepParams("Measure the rail"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "rail at 5.0 V", 5.0, 5.0)
      end),
  })
```

`ATE.log(level, ...)` writes to the run log; `print` is redirected to
`ATE.log(Log.INFO, ...)`. `ATE.run` is the read-only table of the current run:

```lua:docs/manuals/examples/test-writer/logging.tslua
-- logging.tslua: logging inside a Test Run, and reading the run's own context.
--
-- Demonstrates:
--   * ATE.log(level, ...) with the Log enum; every argument is turned to text and the
--     parts are joined with spaces;
--   * print is redirected to ATE.log(Log.INFO, ...), so a stray print is still recorded;
--   * the main screen shows INFO and above; the debug window shows every level;
--   * ATE.run is a read-only table of the current Test Run. Writing to it is an error.
--   * DEBUG device tracing is off unless the application config turns it on with
--     deviceTrace.enabled; every line a schedule writes is stored with the Test Run.
return ScheduleParams("Manual: logging", "log levels, print and ATE.run")
  :setSchedule({
    StepParams("One line at each level"):setStepNo("1.")
      :setAction(function()
        ATE.log(Log.DEBUG, "a detail only the debug window shows")
        ATE.log(Log.INFO, "the rail settled", 5.0, "V")
        ATE.log(Log.WARNING, "the probe is loose")
        ATE.log(Log.ERROR, "the supply did not come up")
        print("print lands at INFO")
        ATE.report(Result.PASS, "logged at every level")
      end),

    StepParams("Who is running"):setStepNo("2.")
      :setAction(function()
        ATE.log(Log.INFO, "station " .. ATE.run.Station
                             .. ", project " .. ATE.run.Project
                             .. ", UUT " .. ATE.run.UUT_serial
                             .. ", operator " .. ATE.run.Operator
                             .. ", schedule " .. ATE.run.Test_Schedule_Name
                             .. ", release " .. ATE.run.Media_Release_Media_Number)
        ATE.report(Result.PASS, "read the run context", ATE.run.Operator, "Ada Lovelace")
      end),
  })
```

## Sub-schedules

`:setInclude(file, keyvals)` expands the file's steps beneath the header row. The included file
gets `keyvals` as the chunk argument (`local kv = ...`), and ATE runs it again per include, so
each include is its own instance. `keyvals` must be plain data (strings, numbers, booleans,
nested tables of those). A top-level schedule, or an include with no keyvals, gets `{}`.

The file is resolved against the release that owns the including schedule, then its
dependencies depth first - the same order as `require`. Prefixes are prepended exactly as
written and stack outward-in. A header row is not a Test Step: it has no Result and is not
counted in the summary. `setEnable(false)` on a header forces the default of every step
beneath it, and an instance whose whole subtree is disabled has both its Actions skipped. The
included schedule's doc fields and its `setStopOnFail` are ignored; only its Actions and steps
take part.

```lua:docs/manuals/examples/test-writer/main.tslua
-- main.tslua: the top-level schedule that includes Sub-schedules.
--
-- Demonstrates:
--   * :setInclude(file, keyvals) makes a step a header row, not a Test Step: the steps of
--     the included schedule are pulled in beneath it.
--   * The same file can be included more than once; each include is its own instance with
--     its own keyvals.
--   * :setPrefix is prepended to the included steps' Step Numbers, as written, with no
--     separator, and prefixes stack as the includes nest.
--   * :setEnable on a header forces the default of every step beneath it.
return ScheduleParams("Manual: sub-schedules", "includes, keyvals, prefixes and nesting")
  :setDocTitle("Manual examples - Sub-schedules")
  :setDocId("MR-MANUAL-SUBS")
  :setDocRev("1.0")
  :setSchedule({
    StepParams("Rail present"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "the rail is fitted") end),

    -- 2.1. and 2.2. come from channel.tslua, included once for channel 1.
    StepParams("Channel 1"):setStepNo("2.")
      :setInclude("channel.tslua", { channel = 1, label = "left rail" }):setPrefix("2."),

    -- A disabled header: every step beneath it records NOTRUN, and its Actions are skipped.
    StepParams("Channel 2, not fitted"):setStepNo("3.")
      :setEnable(false)
      :setInclude("channel.tslua", { channel = 2, label = "right rail" }):setPrefix("3."),
  })
```

```lua:docs/manuals/examples/test-writer/channel.tslua
-- channel.tslua: a Sub-schedule. It is complete enough to run on its own, and is listed
-- as hidden in manual.atedef so the Operator never picks it directly.
--
-- Each include runs this file again, with the keyvals passed as the chunk's argument.
-- Its own Actions take part in the run's Pre-test and Post-test order.
local kv = ...
local channel = kv.channel or 1
local label = kv.label or "a channel"

return ScheduleParams("Channel check", "one output channel")
  :setPreTestAction(function()
    ATE.log(Log.INFO, "channel " .. channel .. " (" .. label .. ") pre-test")
  end)
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "channel " .. channel .. " post-test, run stands at " .. passFail)
  end)
  :setSchedule({
    StepParams("Switch the channel on"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "channel " .. channel .. " switches on")
      end),

    -- A Sub-schedule inside a Sub-schedule: step numbers become 2.2.1. and so on.
    StepParams("Its LEDs"):setStepNo("2.")
      :setInclude("led.tslua", { channel = channel }):setPrefix("2."),
  })
```

```lua:docs/manuals/examples/test-writer/led.tslua
-- led.tslua: the innermost Sub-schedule, included by channel.tslua.
local kv = ...
local channel = kv.channel or 1

return ScheduleParams("LED check", "the indicator LEDs of one channel")
  :setSchedule({
    StepParams("The LED lights"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "channel " .. channel .. " LED is lit")
      end),
  })
```

## Modules and the `.atedef`

`require` is ATE's own. It resolves the name against the release's files, then its
dependencies, never against `package.path`, which ATE empties. An unlisted name is an error. A
module runs once per Test Run and its return value is cached. `ate.` is reserved for modules
bundled with ATE (`ate.devices`).

Checksums are SHA-256 of the raw bytes, lowercase hex. A mismatch is a WARNING, the file still
loads, and both checksums are recorded with the run. `ate definition update <file.atedef>`
computes each listed file's SHA-256 and writes it back over the old value, filling in any
`sha256` that is absent; the file is only rewritten if a checksum actually changed, so a
correct `.atedef` comes back byte for byte the same. A listed file it cannot read keeps its
current value and warns.

Every field a `.atedef` can carry:

| Field | Meaning |
| --- | --- |
| `format` | must be `1`; anything else makes the definition broken |
| `mediaNumber` | the release's identity; not a version |
| `name`, `version` | `version` must be valid semver 2.0.0; other releases name it in `depends` |
| `depends` | a list of `{"name": ..., "version": ...}`; resolved depth first, in declaration order |
| `files[]` | one entry per Lua file: `path`, `sha256` |
| `files[].path` | relative to the `.atedef` (or absolute), forward slashes on both platforms |
| `files[].hidden` | only on a `.tslua`: keeps it off the Run screen while still allowing it as a Sub-schedule |
| `files[].module` | only on a `.lua`: the name `require` uses. Otherwise the path derives it: `lib/rails.lua` is `lib.rails`. An absolute path, or one containing `..`, must state it |

Here is the definition the manual's own examples ship, exactly as it sits on disk - note
`channel.tslua` and `led.tslua` marked `hidden`, and `modules/limits.lua` given the module name
`board_limits`:

```json:docs/manuals/examples/test-writer/manual.atedef
{
  "format": 1,
  "mediaNumber": "MR-MANUAL-TW",
  "name": "Test writer manual examples",
  "version": "1.0.0",
  "files": [
    {
      "path": "first.tslua",
      "sha256": "a260d2b923819ff6f71804370d4afce9ac77269ec9d9cf9c4b0dae35b85e48c8"
    },
    {
      "path": "steps.tslua",
      "sha256": "2203cddc55bf0daf0e557d3ecf1b847e77bf8fa873385cea775e32f240c5af97"
    },
    {
      "path": "inputs.tslua",
      "sha256": "6a746ebad482542f81d5b846b65a57adb467942e5289d9fc7e76b9bdaabd22fc"
    },
    {
      "path": "actions.tslua",
      "sha256": "a15e9a9fa060f0e829cc2e047d2c0313a1a2203c7c33356e792f699d1b02c526"
    },
    {
      "path": "retry.tslua",
      "sha256": "5375d9870ad11af1553eeaa1d5fd27ce586c2a5cce78af6b4a493a029fee58c9"
    },
    {
      "path": "results.tslua",
      "sha256": "49f80374f637c13e34281140912335fcb583fa87865674def089b18188130ce7"
    },
    {
      "path": "main.tslua",
      "sha256": "fc768ae110764fb3a7a9a4a29fdd93df3beafc09b8fff6ea7fa71efa825964d4"
    },
    {
      "path": "channel.tslua",
      "hidden": true,
      "sha256": "1cecd8b3566e4783c86375ebcdac6b69eaae52f1ea74e32c495babf7009b6e63"
    },
    {
      "path": "led.tslua",
      "hidden": true,
      "sha256": "3477e6a5a1578108bd76687e13e211ca1c48b5bac3343160a4e86319f87f46f8"
    },
    {
      "path": "modules.tslua",
      "sha256": "f34234441c454b9e0559e778fb86c6f10086f2edd4248207953ce67f22fae386"
    },
    {
      "path": "lib/rails.lua",
      "sha256": "392e385ab81dfb814a7ddec0cb1c0799c97d714b3d97c2cfb3306e6ba6cde029"
    },
    {
      "path": "modules/limits.lua",
      "module": "board_limits",
      "sha256": "ea8a21a8a7bef16b0abaa9efcc07c8f08416b4a07b712ac35b9ffdbf8c31fd99"
    },
    {
      "path": "tcp.tslua",
      "sha256": "8439beda05cc96f5c6bd69629951fc2318a7526eb085fb2bb322fc711c2cb8c7"
    },
    {
      "path": "udp.tslua",
      "sha256": "324ce6659ab74bcea159fbe20d063925e5b5a88992b2ad1755e4d68625f461b4"
    },
    {
      "path": "serial.tslua",
      "sha256": "dd717a441db74b490b84b999f82a54b7e0d2b3b3185906ae83150562c7dcb768"
    },
    {
      "path": "visa.tslua",
      "sha256": "5c9d69646277814d6d0e134380ba32e6402ce4d9d3f4006dbe4f622bb107afb8"
    },
    {
      "path": "errors.tslua",
      "sha256": "66151279fc9e62462e80a75e273785fac0bb1c8f2a7bf0aeec62f312a1fb0fe2"
    },
    {
      "path": "left_open.tslua",
      "sha256": "a53f424fe15813bafdcdf74f0c7eb4e69b37bbbb5fa27d833b58eb7d1923b279"
    },
    {
      "path": "logging.tslua",
      "sha256": "ee6b7ee6f1a3b1ed15522544d93645c694dfd29d27d53188416c8e956170dd02"
    },
    {
      "path": "abort.tslua",
      "sha256": "b164d0057d61fa7d5f4092a2b47154dc4ca16dfd685640ef09338f4b862eb308"
    },
    {
      "path": "sandbox.tslua",
      "sha256": "e40b7d8eafd167b5d52f844957962b505f53009d791a38bb2729413462aaee99"
    },
    {
      "path": "forbidden.tslua",
      "sha256": "eae7b5e968792a6abdcf883ccde1e956fdf4f7a6c14e2d8aab8ff02c1fc8dd97"
    }
  ]
}
```

```lua:docs/manuals/examples/test-writer/modules.tslua
-- modules.tslua: sharing code with require, including ATE's own bundled modules.
--
-- Demonstrates:
--   * require finds only the files the Test Definition lists (and its dependencies'),
--     never anything on package.path;
--   * the module name is derived from a plain relative path, or given explicitly with the
--     "module" field;
--   * the reserved "ate." namespace is ATE's own: require("ate.devices") is always ATE's
--     Device params builder, never a file in the Media Release.
local rails = require("lib.rails")      -- derived name: the path without .lua, / as .
local limits = require("board_limits")  -- explicit "module" name in manual.atedef
local devices = require("ate.devices")  -- bundled with ATE

return ScheduleParams("Manual: modules", "require and the ate. namespace")
  :setSchedule({
    StepParams("Check every rail"):setStepNo("1.")
      :setAction(function()
        local ok = true
        for channel = 1, 3 do
          local volts = rails.nominalVolts(channel)
          ok = ok and volts >= limits.low and volts <= limits.high
          ATE.log(Log.INFO, "channel " .. channel .. " nominal " .. volts .. " V")
        end
        ATE.report(ok and Result.PASS or Result.FAIL,
                   "every rail is inside " .. limits.low .. " to " .. limits.high .. " V")
      end),

    -- A builder is just a params table: nothing is opened, so no Device is involved.
    StepParams("Build params without opening"):setStepNo("2.")
      :setAction(function()
        local params = devices.Tcp("127.0.0.1", 5025):setTimeout(2.5)
        ATE.report(Result.PASS, "timeout set to " .. params.timeout, params.timeout, 2.5)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/lib/rails.lua
-- lib/rails.lua: an ordinary Lua module. Its module name is derived from its path:
-- "lib/rails.lua" becomes "lib.rails". require runs a module once per Test Run and
-- caches what it returns; later requires of the same name get the cached value.
local rails = {}

function rails.nominalVolts(channel)
  return 1.5 * channel
end

return rails
```

```lua:docs/manuals/examples/test-writer/modules/limits.lua
-- modules/limits.lua: a module whose name is given explicitly in manual.atedef with the
-- "module" field, because a name derived from the path would be "modules.limits".
return { low = 1.0, high = 5.0 }
```

## Devices

`ATE.open(deviceType, params [, onError])` returns an opaque handle. Every field `open` needs
is required - there are no defaults in `ATE.open`; the builders supply them.

| Device Type | `open` fields | builder (defaults) |
| --- | --- | --- |
| `DeviceType.TCP` | `host`, `port`, `timeout`, `readTerm`, `writeTerm` | `devices.Tcp(host, port)`: timeout 1.0, terminators `"\n"` |
| `DeviceType.SERIAL` | `port`, `baud`, `dataBits`, `stopBits`, `parity`, `flowControl`, `timeout`, `readTerm`, `writeTerm` | `devices.Serial(port)`: 9600, 8, 1, `Parity.NONE`, `FlowControl.NONE`, 1.0, terminators `"\n"` |
| `DeviceType.UDP` | `host`, `port`, `localPort`, `timeout` | `devices.Udp(host, port)`: `localPort` 0, timeout 1.0 |
| `DeviceType.VISA` | `resource`, `timeout`, `readTerm`, `writeTerm` | `devices.Visa(resource)`: timeout 1.0, terminators `"\n"` |

Per-call fields, all optional, falling back to the open-time configuration: `read` takes
`size`, `timeout`, `readTerm`; `write` takes `data` (required), `writeTerm`, `timeout`;
`close` takes no fields of its own but accepts an optional params table and an optional
`onError` — `ATE.close(dev [, params] [, onError])` — which is used if the close itself fails.
UDP has no terminators: its `read` takes `size` and `timeout`, and its `write` takes `data`
(required) and `timeout`. Passing a `readTerm` to a UDP Device is an error naming the field.
Builder setters: `setHost`, `setPort`, `setLocalPort`, `setResource`, `setBaud`, `setDataBits`,
`setStopBits`, `setParity`, `setFlowControl`, `setTimeout`, `setReadTerm`, `setWriteTerm`,
`setSize`, and `devices.Write(data)`.

Reads and writes are raw bytes. The terminator is stripped from `data`; `count` is what is
left. A write's `count` includes the terminator.

TCP, SERIAL and UDP are read in the background from `open` to `close` and buffered, capped at
the Station config key `devices.receiveBufferBytes` (16777216, 16 MiB); on overflow ATE drops
the oldest bytes, logs one WARNING, and the next read carries `overflow = true`. `ATE.flush(dev)`
empties the buffer and returns the count discarded. VISA has no background reader and no buffer,
so `flush` discards nothing.

A VISA resource string names the instrument, and the shape matters:
`GPIB0::5::INSTR` is a GPIB instrument, `TCPIP0::192.168.1.10::inst0::INSTR` is a VXI-11
instrument, and `TCPIP0::127.0.0.1::5025::SOCKET` is a plain TCP socket wrapped in a VISA
session - the same stream a `DeviceType.TCP` Device would open, with pyvisa in the way. ATE
uses whichever pyvisa backend it finds; only pyvisa-py is shipped, which reaches TCPIP and
USBTMC. A GPIB instrument needs a real VISA driver on the Station (NI-VISA or linux-GPIB): ask
the schedule author which resource string their instrument needs before writing the schedule,
because nothing installed with ATE can open a GPIB address.

```lua:docs/manuals/examples/test-writer/tcp.tslua
-- tcp.tslua: a TCP Device, from open to close.
--
-- The host and port come from the environment, which the test harness sets to a
-- simulator peer listening on 127.0.0.1. Point them at a real instrument to use this
-- schedule as it is.
--
-- Demonstrates:
--   * the ate.devices builders, whose setters chain, and how a Device opened in the
--     Pre-test Action is still open in the steps, because a device object lives for the
--     whole Test Run;
--   * per-call overrides (timeout, readTerm, size) that do not change the open-time
--     configuration;
--   * a read that times out after some bytes is a partial read, not a Device failure;
--   * a read that times out with nothing at all is a Device failure and goes through
--     onError, which is given the failure table and the params table and may return a
--     value for the call to carry on with;
--   * flush discards whatever the receive buffer already holds.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

local instrument = nil

return ScheduleParams("Manual: TCP", "open, write, read, flush and close a TCP Device")
  :setStopOnFail(false)
  :setPreTestAction(function()
    -- devices.Tcp(host, port) fills in every field TCP needs: timeout 1.0 s,
    -- readTerm and writeTerm "\n". open has no defaults of its own, so a plain table
    -- would have to name them all.
    instrument = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(2.0))
    ATE.addEquipment("SCPI simulator", "SIM-TCP", "TCP " .. host .. ":" .. port, "2026-01-01")
  end)
  :setPostTestAction(function(passFail)
    ATE.close(instrument)   -- closing a second time does nothing and still reports ok
  end)
  :setSchedule({
    StepParams("Identify the instrument"):setStepNo("1.")
      :setAction(function()
        local sent = ATE.write(instrument, devices.Write("*IDN?"))
        local reply = ATE.read(instrument, devices.Read():setTimeout(1.0))
        ATE.report((sent.ok and reply.count > 0) and Result.PASS or Result.FAIL,
                   "reply: " .. (reply.data or "nothing"),
                   reply.count, 26)
      end),

    -- The peer answers "MEAS:VOLT?" with "1.234\n". readTerm "3" stops the read at the "3",
    -- so the first read takes "1.2" and the second takes the rest of the reply.
    StepParams("Stop a read at a terminator"):setStepNo("2.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MEAS:VOLT?"))
        local first = ATE.read(instrument, devices.Read():setReadTerm("3"))
        local rest = ATE.read(instrument)
        ATE.report((first.data == "1.2" and rest.data == "4") and Result.PASS or Result.FAIL,
                   "first read " .. (first.data or "nothing") .. ", second " .. (rest.data or "nothing"),
                   first.count + rest.count, 4)
      end),

    -- "PARTIAL?" is answered with bytes and no terminator: the read times out having taken
    -- some of them. That is partial = true, a WARNING in the log, and not a Device failure.
    StepParams("A read that times out partway"):setStepNo("3.")
      :setAction(function()
        ATE.write(instrument, devices.Write("PARTIAL?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3))
        ATE.report((r.partial and r.count > 0) and Result.PASS or Result.FAIL,
                   "got " .. r.count .. " bytes before the timeout",
                   tostring(r.partial), "true")
      end),

    -- "NEVER?" is answered with silence: the read times out with no bytes at all, which IS
    -- a Device failure, so onError runs. Returning a value makes the call continue with it.
    StepParams("Handle a read that times out"):setStepNo("4.")
      :setAction(function()
        ATE.write(instrument, devices.Write("NEVER?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3), function(opResult, opArgs)
          ATE.log(Log.WARNING, "no reply from the instrument: " .. opResult.err)
          return { data = nil, count = 0, partial = false }
        end)
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "the timeout was handled, so the step carried on")
      end),

    -- The peer sends three lines; the schedule reads the first and throws away the rest.
    StepParams("Discard the receive buffer"):setStepNo("5.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MULTI?"))
        ATE.read(instrument, devices.Read():setSize(7))
        local f = ATE.flush(instrument)
        ATE.report((f.ok and f.count > 0) and Result.PASS or Result.FAIL, "discarded " .. f.count .. " buffered bytes")
      end),
  })
```

```lua:docs/manuals/examples/test-writer/udp.tslua
-- udp.tslua: a UDP Device, where one datagram is one message.
--
-- Demonstrates:
--   * UDP has no terminators, so readTerm and writeTerm are not fields this Device Type
--     knows; passing one is a schedule bug that raises naming the field;
--   * a datagram longer than the size asked for is truncated and returned with partial =
--     true, which the log calls a truncation rather than a timeout;
--   * the receive buffer is a queue of whole datagrams, so a reply that arrives before the
--     schedule reads it is not lost.
local devices = require("ate.devices")

local host = os.getenv("ATE_UDP_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_UDP_PORT")) or 5026

return ScheduleParams("Manual: UDP", "send and read datagrams")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Echo one datagram"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("PING"))
        local reply = ATE.read(dev)
        ATE.report(reply.data == "ECHO:PING" and Result.PASS or Result.FAIL,
                   "the peer echoed " .. (reply.data or "nothing"),
                   reply.count, 9)
        ATE.close(dev)
      end),

    -- Ask for 10 bytes of a 40-byte datagram: ATE returns the first 10 and says partial.
    StepParams("Truncate a datagram"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("a datagram longer than the size asked for"))
        local r = ATE.read(dev, devices.Read():setSize(10))
        ATE.report((r.count == 10 and r.partial) and Result.PASS or Result.FAIL,
                   "read " .. r.count .. " of a longer datagram",
                   r.count, 10)
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/serial.tslua
-- serial.tslua: a SERIAL Device through pyserial.
--
-- The port here is pyserial's socket:// URL, which opens a TCP connection to the
-- simulator peer; a real station names a COM or tty port instead.
--
-- Demonstrates:
--   * open has no defaults, so every field is named;
--   * the Parity and FlowControl enums;
--   * the same connection read with the Device's own terminator.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: SERIAL", "open a serial port and read a reply")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Open the port"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.SERIAL, {
          port = "socket://" .. host .. ":" .. port,
          baud = 115200,
          dataBits = 8,
          stopBits = 1,
          parity = Parity.NONE,
          flowControl = FlowControl.NONE,
          timeout = 2.0,
          readTerm = "\n",
          writeTerm = "\n",
        })
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "serial reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/visa.tslua
-- visa.tslua: a VISA instrument through pyvisa.
--
-- VISA has no background reader: ATE reads the instrument only when the schedule asks,
-- because polling an SCPI instrument with reads that have nothing pending makes it log
-- "Query UNTERMINATED" (-420) and ties up the bus. ATE ships pyvisa-py; a VISA resource
-- string names the instrument, here the simulator peer as a plain TCPIP socket.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: VISA", "read an instrument through pyvisa")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Ask the instrument who it is"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.VISA,
          devices.Visa("TCPIP0::" .. host .. "::" .. port .. "::SOCKET"):setTimeout(2.0))
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        local f = ATE.flush(dev)   -- VISA has no receive buffer: this discards nothing
        ATE.log(Log.INFO, "flush discarded " .. f.count .. " bytes")
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "VISA reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

A handle lives for the whole run. Closing a closed Device does nothing and reports success;
using one is a failure through `onError`. At the end of every run ATE closes whatever is still
open and logs a WARNING for each.

```lua:docs/manuals/examples/test-writer/left_open.tslua
-- left_open.tslua: a Device the schedule never closes.
--
-- A Test Run closes every Device still open when it ends, and logs one WARNING naming
-- each one. That is why a Device opened in a Pre-test Action does not have to be closed
-- by hand - it is worth closing anyway, so the log stays clean.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: a Device left open", "ATE closes what the schedule forgets")
  :setSchedule({
    StepParams("Open and forget"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.report(Result.PASS, "opened a Device and left it open")
      end),
  })
```

## Device failures, Stop on Fail, Retry, Abort

`onError(opResult, opArgs)` is called when the operation fails. If it returns anything but
`nil`, the call returns that value and the schedule continues. If it returns `nil`, or there is
no handler, the call raises a Lua error and the step records `Result.ERROR`. A read that times
out having received some bytes is not a failure: it returns `partial = true` and logs a
WARNING. ATE never reconnects: close the Device and open it again with the same params.

A field the Device Type does not know, a missing required field, or a wrong-typed value is not a
Device failure. It never reaches `onError`; the call returns `false` with the message as its
second result and the traceback is logged at ERROR.

```lua:docs/manuals/examples/test-writer/errors.tslua
-- errors.tslua: how Device failures reach the schedule, and what does not.
--
-- Demonstrates:
--   * a Device failure with no onError raises a Lua error, which ends the step as
--     Result.ERROR;
--   * an onError that returns a value makes the call continue with that value;
--   * a bad argument (a misspelled field) is a schedule bug, not a Device failure: it
--     never reaches onError, it is logged at ERROR, and the call returns false with the
--     message as its second result;
--   * the handler given to open is the fallback for that Device's read, write and close.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025
local deadPort = tonumber(os.getenv("ATE_DEAD_PORT")) or 1

return ScheduleParams("Manual: Device errors", "onError, a closed Device and a bad field")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A misspelled field"):setStepNo("1.")
      :setAction(function()
        local opened, why = ATE.open(DeviceType.TCP, { host = host, port = port, timeout = 1.0,
                                                     readTerm = "\n", writeTerm = "\n", buad = 9600 })
        ATE.report(opened == false and Result.PASS or Result.FAIL, "the open was refused: " .. why)
      end),

    -- Nothing is listening on deadPort: the open fails. This handler logs and returns nil,
    -- which means "no recovery", so ATE.open raises and the step records Result.ERROR.
    StepParams("A port nothing listens on"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, deadPort):setTimeout(0.5),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "nothing is listening on " .. opArgs.host .. ":" .. opArgs.port)
            return nil
          end)
        ATE.report(Result.PASS, "this line is never reached")
      end),

    -- A Device that is closed cannot be read. With no handler the call raises.
    StepParams("Read a closed Device"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.close(dev)
        ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(Result.PASS, "this line is never reached either")
      end),

    -- The same failure, handled: the step carries on with what onError returns.
    StepParams("Handle the closed Device"):setStepNo("4.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "the Device is gone: " .. opResult.err)
            return { data = nil, count = 0, partial = false }
          end)
        ATE.close(dev)
        local r = ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "recovered from reading a closed Device")
      end),
  })
```

Stop on Fail pauses the run at a `FAIL` or `ERROR` step so the Operator can choose `CONTINUE`,
`RETRY` or `ABORT`. Retry runs the action again from the start with POLL reset and blocking
inputs cleared; each Attempt is recorded and the step's result is the last Attempt's. There is
no Attempt number in Lua - keep your own count.

```lua:docs/manuals/examples/test-writer/retry.tslua
-- retry.tslua: Stop on Fail, and what a Retry does.
--
-- Demonstrates:
--   * setStopOnFail(true) is the default: a step ending FAIL or ERROR pauses the run and
--     the Operator chooses Continue, Retry or Abort.
--   * Retry runs the step's action again from the beginning. Each Attempt is recorded;
--     the step's result is its last Attempt's, and the Operator's choice is recorded too.
--   * ATE gives a step no way to read its own Attempt number, so the schedule keeps the
--     count itself in a local the action closes over.
local attempts = 0

return ScheduleParams("Manual: retry", "the first Attempt fails, the Retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Retry this one"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt " .. attempts .. ": the joint is cold", 0.2, 2.4)
        else
          ATE.report(Result.PASS, "attempt " .. attempts .. ": the joint is warm", 2.4, 2.4)
        end
      end),

    -- Only reached after the step passed, or after the Operator chose to continue.
    StepParams("Next check"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "the rail is steady", 5.0, 5.0)
      end),
  })
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

The seven `Result` values, and what each does to the run:

```
PASS            ran, passed        - no
FAIL            ran, failed        - yes
ERROR           raised, aborted, or reported as an error - yes
SKIPPED         ran, chose not to test - no
NOTRUN          never executed     - no
FORCED_NOTRUN   does not apply to this UUT - no
NOREPORT        ran, never reported - no (but it warns)
```

The run is `FAIL` if any step is `FAIL` or `ERROR`.

```lua:docs/manuals/examples/test-writer/results.tslua
-- results.tslua: every value the Result enum can record.
--
-- Stop on Fail is turned off here so the run reaches every step. A Test Run is FAIL if
-- any step records FAIL or ERROR; the other Result values do not fail the run by
-- themselves. A step that reports nothing at all records NOREPORT, which ATE also logs
-- as a WARNING.
return ScheduleParams("Manual: every Result", "one step for each Result value")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A pass"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "within limits", 5.0, 5.0) end),

    StepParams("A fail"):setStepNo("2.")
      :setAction(function() ATE.report(Result.FAIL, "rail low", 4.1, 5.0) end),

    -- Reporting ERROR is a legitimate way to say "this step could not be completed";
    -- an uncaught Lua error in a step records ERROR too.
    StepParams("An error"):setStepNo("3.")
      :setAction(function() ATE.report(Result.ERROR, "the probe was not connected") end),

    -- SKIPPED is the schedule's own decision: the step ran, chose not to test anything.
    StepParams("Skipped on purpose"):setStepNo("4.")
      :setAction(function() ATE.report(Result.SKIPPED, "no test for this variant") end),

    -- NOTRUN can also be reported by the schedule; ATE records it for a step that never
    -- ran because the run stopped or was aborted.
    StepParams("Not run"):setStepNo("5.")
      :setAction(function() ATE.report(Result.NOTRUN, "skipped by the schedule") end),

    -- FORCED_NOTRUN: the schedule decided the step does not apply to this UUT.
    StepParams("Not fitted"):setStepNo("6.")
      :setAction(function() ATE.report(Result.FORCED_NOTRUN, "this board has no second rail") end),

    -- NOREPORT: this step runs and reports nothing, so ATE records NOREPORT and warns.
    StepParams("Reports nothing"):setStepNo("7.")
      :setAction(function() ATE.log(Log.INFO, "looked, but reported nothing") end),
  })
```

## Step Inputs

Every declaration needs `desc` and `type`. `type` is one of `ENUM`, `INTEGER`, `FLOAT`,
`STRING`, `PROMPT`, `POLL`.

| field | applies to | meaning |
| --- | --- | --- |
| `input` | `ENUM` | `RADIOBUT`, `CHECKBOX` or `PUSHBUT` |
| `enum` | `ENUM` | labels, at least one |
| `range` | `INTEGER`, `FLOAT` | `{min=, max=}`; empty is unbounded |
| `pattern` | `STRING` | regex the value must match |
| `prompt` | `PROMPT`, `POLL` | a string, or an array of button texts |
| `tooltip` | all | hover help |
| `orient` | all | `VERT` or `HORIZ` |

A field on the wrong type is a load error. A field the format does not know is ignored.

`ATE.input(n)` returns: the label for `RADIOBUT`/`PUSHBUT`; a Lua table of labels for
`CHECKBOX` (empty when none are ticked); an integer for `INTEGER`; a float for `FLOAT`; the
text for `STRING` and `PROMPT`; the clicked button's text for `POLL`. `POLL` never blocks: it
returns `nil` until clicked and is sticky until the Attempt starts again. `n` out of range is
an error.

```lua:docs/manuals/examples/test-writer/inputs.tslua
-- inputs.tslua: every Step Input type, and what ATE.input(n) returns for each.
--
-- Demonstrates:
--   * ENUM with each widget: RADIOBUT picks one label, CHECKBOX returns a table of
--     every label ticked (empty when none are), PUSHBUT is answered by pressing one.
--   * INTEGER and FLOAT with a range: the harness first answers out of range, which the
--     session refuses before the valid value is recorded.
--   * STRING with a pattern, refused the same way.
--   * PROMPT blocks the step until the Operator answers; POLL does not block, so the
--     step reads it in a loop. A POLL is sticky: once clicked it keeps returning that
--     button's text until the Attempt starts again.
--   * tooltip and orient apply to every input type.
return ScheduleParams("Manual: step inputs", "one step per Step Input type")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Pick one"):setStepNo("1.")
      :setInputs({ [1] = { desc = "LED colour", type = "ENUM", input = "RADIOBUT",
                          enum = { "Green", "Red", "Off" }, orient = "HORIZ",
                          tooltip = "Which LED the board lights" } })
      :setAction(function()
        local colour = ATE.input(1)
        ATE.report(Result.PASS, "the LED is " .. colour, colour, "Green")
      end),

    StepParams("Tick several"):setStepNo("2.")
      :setInputs({ [1] = { desc = "channels to measure", type = "ENUM", input = "CHECKBOX",
                          enum = { "A", "B", "C" }, orient = "VERT" } })
      :setAction(function()
        local channels = ATE.input(1)          -- a Lua table of labels, [] when none ticked
        ATE.report(Result.PASS, "measuring " .. table.concat(channels, ", "),
                   table.concat(channels, ","), "A,B")
      end),

    StepParams("Press a button"):setStepNo("3.")
      :setInputs({ [1] = { desc = "what to do next", type = "ENUM", input = "PUSHBUT",
                          enum = { "RETEST", "SKIP" } } })
      :setAction(function()
        local choice = ATE.input(1)
        ATE.report(Result.PASS, "the Operator pressed " .. choice, choice, "RETEST")
      end),

    StepParams("Whole number"):setStepNo("4.")
      :setInputs({ [1] = { desc = "test current, mA", type = "INTEGER",
                          range = { min = 1, max = 200 } } })
      :setAction(function()
        local current = ATE.input(1)           -- a Lua integer
        ATE.report(Result.PASS, "current is " .. current .. " mA", current, 42)
      end),

    StepParams("Decimal"):setStepNo("5.")
      :setInputs({ [1] = { desc = "rail voltage, V", type = "FLOAT",
                          range = { min = 0.5, max = 2.5 } } })
      :setAction(function()
        local volts = ATE.input(1)             -- a Lua float
        ATE.report(Result.PASS, "rail is " .. volts .. " V", volts, 1.25)
      end),

    StepParams("Text with a pattern"):setStepNo("6.")
      :setInputs({ [1] = { desc = "probe part number", type = "STRING",
                          pattern = "^[A-Z]{2}-[0-9]{4}$" } })
      :setAction(function()
        local part = ATE.input(1)
        ATE.report(Result.PASS, "probe " .. part, part, "AB-1234")
      end),

    StepParams("Blocking prompt"):setStepNo("7.")
      :setInputs({ [1] = { desc = "is the probe connected?", type = "PROMPT",
                          prompt = { "CONTINUE" } } })
      :setAction(function()
        local answer = ATE.input(1)            -- blocks until the Operator presses it
        ATE.report(Result.PASS, "probe confirmed: " .. answer, answer, "CONTINUE")
      end),

    StepParams("POLL in a loop"):setStepNo("8.")
      :setInputs({ [1] = { desc = "stop this step?", type = "POLL",
                          prompt = { "KEEP GOING", "STOP" } } })
      :setAction(function()
        local clicked, reads = nil, 0
        while reads < 8 do
          reads = reads + 1
          clicked = ATE.input(1)               -- nil until a button is clicked
          if clicked then break end
          ATE.sleep(0.1)                       -- a POLL never blocks the step
        end
        if clicked then
          ATE.report(Result.PASS, "clicked " .. clicked .. " after " .. reads .. " reads",
                     clicked, "STOP")
        else
          ATE.report(Result.FAIL, "no click after " .. reads .. " reads")
        end
      end),
  })
```

## Actions and Equipment

A Pre-test Action runs before the first step; a Post-test Action runs after the last one, in
reverse Pre-test order. Both are called with the run's standing result as their first argument
- always `"PASS"` for a Pre-test Action, because nothing has failed yet. A Pre-test Action that
raises aborts the run with `PRETEST_FAILED` and every step `NOTRUN`, and the Post-test Actions
still run: every schedule instance the Pre-test pass reached gets its teardown, in reverse
order, including the instance whose own Pre-test Action raised; an instance the pass never got
to has none. Post-test Actions run even after an abort: ATE clears the abort flag before them,
an abort during one ends that Action only, and an error in one fails the run while the rest
still run.

`ATE.addEquipment(name, serial, connection, calDue, notes)` - all strings, `name` required.
A repeat of the same `name` and `serial` adds no row and logs a DEBUG line.

```lua:docs/manuals/examples/test-writer/actions.tslua
-- actions.tslua: Pre-test and Post-test Actions, and Equipment.
--
-- Demonstrates:
--   * a Pre-test Action runs before the first step; a Post-test Action runs after the
--     last one. Both are called with the run's current result as their first argument:
--     a Pre-test Action always sees "PASS", the Post-test one sees the final PASS or FAIL.
--   * ATE.addEquipment is legal in an Action, a step, or both; the report's Equipment
--     section comes from it. Declaring the same name and serial twice adds no second row.
--   * ATE.report and ATE.input are NOT legal in an Action: only Test Steps have a result
--     and an Operator prompt.
return ScheduleParams("Manual: Actions", "Pre-test and Post-test Actions")
  :setPreTestAction(function(passFail)
    ATE.log(Log.INFO, "pre-test action, run stands at " .. passFail)
    ATE.addEquipment("Bench power supply", "PSU-123", "GPIB::1", "2026-12-01", "0-60 V")
  end)
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action, run result was " .. passFail)
  end)
  :setSchedule({
    StepParams("Measure the rail"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "rail at 5.0 V", 5.0, 5.0)
      end),
  })
```

`ATE.log(level, ...)` writes to the run log; `print` is redirected to
`ATE.log(Log.INFO, ...)`. `ATE.run` is the read-only table of the current run:

```lua:docs/manuals/examples/test-writer/logging.tslua
-- logging.tslua: logging inside a Test Run, and reading the run's own context.
--
-- Demonstrates:
--   * ATE.log(level, ...) with the Log enum; every argument is turned to text and the
--     parts are joined with spaces;
--   * print is redirected to ATE.log(Log.INFO, ...), so a stray print is still recorded;
--   * the main screen shows INFO and above; the debug window shows every level;
--   * ATE.run is a read-only table of the current Test Run. Writing to it is an error.
--   * DEBUG device tracing is off unless the application config turns it on with
--     deviceTrace.enabled; every line a schedule writes is stored with the Test Run.
return ScheduleParams("Manual: logging", "log levels, print and ATE.run")
  :setSchedule({
    StepParams("One line at each level"):setStepNo("1.")
      :setAction(function()
        ATE.log(Log.DEBUG, "a detail only the debug window shows")
        ATE.log(Log.INFO, "the rail settled", 5.0, "V")
        ATE.log(Log.WARNING, "the probe is loose")
        ATE.log(Log.ERROR, "the supply did not come up")
        print("print lands at INFO")
        ATE.report(Result.PASS, "logged at every level")
      end),

    StepParams("Who is running"):setStepNo("2.")
      :setAction(function()
        ATE.log(Log.INFO, "station " .. ATE.run.Station
                             .. ", project " .. ATE.run.Project
                             .. ", UUT " .. ATE.run.UUT_serial
                             .. ", operator " .. ATE.run.Operator
                             .. ", schedule " .. ATE.run.Test_Schedule_Name
                             .. ", release " .. ATE.run.Media_Release_Media_Number)
        ATE.report(Result.PASS, "read the run context", ATE.run.Operator, "Ada Lovelace")
      end),
  })
```

## Sub-schedules

`:setInclude(file, keyvals)` expands the file's steps beneath the header row. The included file
gets `keyvals` as the chunk argument (`local kv = ...`), and ATE runs it again per include, so
each include is its own instance. `keyvals` must be plain data (strings, numbers, booleans,
nested tables of those). A top-level schedule, or an include with no keyvals, gets `{}`.

The file is resolved against the release that owns the including schedule, then its
dependencies depth first - the same order as `require`. Prefixes are prepended exactly as
written and stack outward-in. A header row is not a Test Step: it has no Result and is not
counted in the summary. `setEnable(false)` on a header forces the default of every step
beneath it, and an instance whose whole subtree is disabled has both its Actions skipped. The
included schedule's doc fields and its `setStopOnFail` are ignored; only its Actions and steps
take part.

```lua:docs/manuals/examples/test-writer/main.tslua
-- main.tslua: the top-level schedule that includes Sub-schedules.
--
-- Demonstrates:
--   * :setInclude(file, keyvals) makes a step a header row, not a Test Step: the steps of
--     the included schedule are pulled in beneath it.
--   * The same file can be included more than once; each include is its own instance with
--     its own keyvals.
--   * :setPrefix is prepended to the included steps' Step Numbers, as written, with no
--     separator, and prefixes stack as the includes nest.
--   * :setEnable on a header forces the default of every step beneath it.
return ScheduleParams("Manual: sub-schedules", "includes, keyvals, prefixes and nesting")
  :setDocTitle("Manual examples - Sub-schedules")
  :setDocId("MR-MANUAL-SUBS")
  :setDocRev("1.0")
  :setSchedule({
    StepParams("Rail present"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "the rail is fitted") end),

    -- 2.1. and 2.2. come from channel.tslua, included once for channel 1.
    StepParams("Channel 1"):setStepNo("2.")
      :setInclude("channel.tslua", { channel = 1, label = "left rail" }):setPrefix("2."),

    -- A disabled header: every step beneath it records NOTRUN, and its Actions are skipped.
    StepParams("Channel 2, not fitted"):setStepNo("3.")
      :setEnable(false)
      :setInclude("channel.tslua", { channel = 2, label = "right rail" }):setPrefix("3."),
  })
```

```lua:docs/manuals/examples/test-writer/channel.tslua
-- channel.tslua: a Sub-schedule. It is complete enough to run on its own, and is listed
-- as hidden in manual.atedef so the Operator never picks it directly.
--
-- Each include runs this file again, with the keyvals passed as the chunk's argument.
-- Its own Actions take part in the run's Pre-test and Post-test order.
local kv = ...
local channel = kv.channel or 1
local label = kv.label or "a channel"

return ScheduleParams("Channel check", "one output channel")
  :setPreTestAction(function()
    ATE.log(Log.INFO, "channel " .. channel .. " (" .. label .. ") pre-test")
  end)
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "channel " .. channel .. " post-test, run stands at " .. passFail)
  end)
  :setSchedule({
    StepParams("Switch the channel on"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "channel " .. channel .. " switches on")
      end),

    -- A Sub-schedule inside a Sub-schedule: step numbers become 2.2.1. and so on.
    StepParams("Its LEDs"):setStepNo("2.")
      :setInclude("led.tslua", { channel = channel }):setPrefix("2."),
  })
```

```lua:docs/manuals/examples/test-writer/led.tslua
-- led.tslua: the innermost Sub-schedule, included by channel.tslua.
local kv = ...
local channel = kv.channel or 1

return ScheduleParams("LED check", "the indicator LEDs of one channel")
  :setSchedule({
    StepParams("The LED lights"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "channel " .. channel .. " LED is lit")
      end),
  })
```

## Modules and the `.atedef`

`require` is ATE's own. It resolves the name against the release's files, then its
dependencies, never against `package.path`, which ATE empties. An unlisted name is an error. A
module runs once per Test Run and its return value is cached. `ate.` is reserved for modules
bundled with ATE (`ate.devices`).

Checksums are SHA-256 of the raw bytes, lowercase hex. A mismatch is a WARNING, the file still
loads, and both checksums are recorded with the run. `ate definition update <file.atedef>`
computes each listed file's SHA-256 and writes it back over the old value, filling in any
`sha256` that is absent; the file is only rewritten if a checksum actually changed, so a
correct `.atedef` comes back byte for byte the same. A listed file it cannot read keeps its
current value and warns.

Every field a `.atedef` can carry:

| Field | Meaning |
| --- | --- |
| `format` | must be `1`; anything else makes the definition broken |
| `mediaNumber` | the release's identity; not a version |
| `name`, `version` | `version` must be valid semver 2.0.0; other releases name it in `depends` |
| `depends` | a list of `{"name": ..., "version": ...}`; resolved depth first, in declaration order |
| `files[]` | one entry per Lua file: `path`, `sha256` |
| `files[].path` | relative to the `.atedef` (or absolute), forward slashes on both platforms |
| `files[].hidden` | only on a `.tslua`: keeps it off the Run screen while still allowing it as a Sub-schedule |
| `files[].module` | only on a `.lua`: the name `require` uses. Otherwise the path derives it: `lib/rails.lua` is `lib.rails`. An absolute path, or one containing `..`, must state it |

Here is the definition the manual's own examples ship, exactly as it sits on disk - note
`channel.tslua` and `led.tslua` marked `hidden`, and `modules/limits.lua` given the module name
`board_limits`:

```json:docs/manuals/examples/test-writer/manual.atedef
{
  "format": 1,
  "mediaNumber": "MR-MANUAL-TW",
  "name": "Test writer manual examples",
  "version": "1.0.0",
  "files": [
    {
      "path": "first.tslua",
      "sha256": "a260d2b923819ff6f71804370d4afce9ac77269ec9d9cf9c4b0dae35b85e48c8"
    },
    {
      "path": "steps.tslua",
      "sha256": "2203cddc55bf0daf0e557d3ecf1b847e77bf8fa873385cea775e32f240c5af97"
    },
    {
      "path": "inputs.tslua",
      "sha256": "6a746ebad482542f81d5b846b65a57adb467942e5289d9fc7e76b9bdaabd22fc"
    },
    {
      "path": "actions.tslua",
      "sha256": "a15e9a9fa060f0e829cc2e047d2c0313a1a2203c7c33356e792f699d1b02c526"
    },
    {
      "path": "retry.tslua",
      "sha256": "5375d9870ad11af1553eeaa1d5fd27ce586c2a5cce78af6b4a493a029fee58c9"
    },
    {
      "path": "results.tslua",
      "sha256": "49f80374f637c13e34281140912335fcb583fa87865674def089b18188130ce7"
    },
    {
      "path": "main.tslua",
      "sha256": "fc768ae110764fb3a7a9a4a29fdd93df3beafc09b8fff6ea7fa71efa825964d4"
    },
    {
      "path": "channel.tslua",
      "hidden": true,
      "sha256": "1cecd8b3566e4783c86375ebcdac6b69eaae52f1ea74e32c495babf7009b6e63"
    },
    {
      "path": "led.tslua",
      "hidden": true,
      "sha256": "3477e6a5a1578108bd76687e13e211ca1c48b5bac3343160a4e86319f87f46f8"
    },
    {
      "path": "modules.tslua",
      "sha256": "f34234441c454b9e0559e778fb86c6f10086f2edd4248207953ce67f22fae386"
    },
    {
      "path": "lib/rails.lua",
      "sha256": "392e385ab81dfb814a7ddec0cb1c0799c97d714b3d97c2cfb3306e6ba6cde029"
    },
    {
      "path": "modules/limits.lua",
      "module": "board_limits",
      "sha256": "ea8a21a8a7bef16b0abaa9efcc07c8f08416b4a07b712ac35b9ffdbf8c31fd99"
    },
    {
      "path": "tcp.tslua",
      "sha256": "8439beda05cc96f5c6bd69629951fc2318a7526eb085fb2bb322fc711c2cb8c7"
    },
    {
      "path": "udp.tslua",
      "sha256": "324ce6659ab74bcea159fbe20d063925e5b5a88992b2ad1755e4d68625f461b4"
    },
    {
      "path": "serial.tslua",
      "sha256": "dd717a441db74b490b84b999f82a54b7e0d2b3b3185906ae83150562c7dcb768"
    },
    {
      "path": "visa.tslua",
      "sha256": "5c9d69646277814d6d0e134380ba32e6402ce4d9d3f4006dbe4f622bb107afb8"
    },
    {
      "path": "errors.tslua",
      "sha256": "66151279fc9e62462e80a75e273785fac0bb1c8f2a7bf0aeec62f312a1fb0fe2"
    },
    {
      "path": "left_open.tslua",
      "sha256": "a53f424fe15813bafdcdf74f0c7eb4e69b37bbbb5fa27d833b58eb7d1923b279"
    },
    {
      "path": "logging.tslua",
      "sha256": "ee6b7ee6f1a3b1ed15522544d93645c694dfd29d27d53188416c8e956170dd02"
    },
    {
      "path": "abort.tslua",
      "sha256": "b164d0057d61fa7d5f4092a2b47154dc4ca16dfd685640ef09338f4b862eb308"
    },
    {
      "path": "sandbox.tslua",
      "sha256": "e40b7d8eafd167b5d52f844957962b505f53009d791a38bb2729413462aaee99"
    },
    {
      "path": "forbidden.tslua",
      "sha256": "eae7b5e968792a6abdcf883ccde1e956fdf4f7a6c14e2d8aab8ff02c1fc8dd97"
    }
  ]
}
```

```lua:docs/manuals/examples/test-writer/modules.tslua
-- modules.tslua: sharing code with require, including ATE's own bundled modules.
--
-- Demonstrates:
--   * require finds only the files the Test Definition lists (and its dependencies'),
--     never anything on package.path;
--   * the module name is derived from a plain relative path, or given explicitly with the
--     "module" field;
--   * the reserved "ate." namespace is ATE's own: require("ate.devices") is always ATE's
--     Device params builder, never a file in the Media Release.
local rails = require("lib.rails")      -- derived name: the path without .lua, / as .
local limits = require("board_limits")  -- explicit "module" name in manual.atedef
local devices = require("ate.devices")  -- bundled with ATE

return ScheduleParams("Manual: modules", "require and the ate. namespace")
  :setSchedule({
    StepParams("Check every rail"):setStepNo("1.")
      :setAction(function()
        local ok = true
        for channel = 1, 3 do
          local volts = rails.nominalVolts(channel)
          ok = ok and volts >= limits.low and volts <= limits.high
          ATE.log(Log.INFO, "channel " .. channel .. " nominal " .. volts .. " V")
        end
        ATE.report(ok and Result.PASS or Result.FAIL,
                   "every rail is inside " .. limits.low .. " to " .. limits.high .. " V")
      end),

    -- A builder is just a params table: nothing is opened, so no Device is involved.
    StepParams("Build params without opening"):setStepNo("2.")
      :setAction(function()
        local params = devices.Tcp("127.0.0.1", 5025):setTimeout(2.5)
        ATE.report(Result.PASS, "timeout set to " .. params.timeout, params.timeout, 2.5)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/lib/rails.lua
-- lib/rails.lua: an ordinary Lua module. Its module name is derived from its path:
-- "lib/rails.lua" becomes "lib.rails". require runs a module once per Test Run and
-- caches what it returns; later requires of the same name get the cached value.
local rails = {}

function rails.nominalVolts(channel)
  return 1.5 * channel
end

return rails
```

```lua:docs/manuals/examples/test-writer/modules/limits.lua
-- modules/limits.lua: a module whose name is given explicitly in manual.atedef with the
-- "module" field, because a name derived from the path would be "modules.limits".
return { low = 1.0, high = 5.0 }
```

## Devices

`ATE.open(deviceType, params [, onError])` returns an opaque handle. Every field `open` needs
is required - there are no defaults in `ATE.open`; the builders supply them.

| Device Type | `open` fields | builder (defaults) |
| --- | --- | --- |
| `DeviceType.TCP` | `host`, `port`, `timeout`, `readTerm`, `writeTerm` | `devices.Tcp(host, port)`: timeout 1.0, terminators `"\n"` |
| `DeviceType.SERIAL` | `port`, `baud`, `dataBits`, `stopBits`, `parity`, `flowControl`, `timeout`, `readTerm`, `writeTerm` | `devices.Serial(port)`: 9600, 8, 1, `Parity.NONE`, `FlowControl.NONE`, 1.0, terminators `"\n"` |
| `DeviceType.UDP` | `host`, `port`, `localPort`, `timeout` | `devices.Udp(host, port)`: `localPort` 0, timeout 1.0 |
| `DeviceType.VISA` | `resource`, `timeout`, `readTerm`, `writeTerm` | `devices.Visa(resource)`: timeout 1.0, terminators `"\n"` |

Per-call fields, all optional, falling back to the open-time configuration: `read` takes
`size`, `timeout`, `readTerm`; `write` takes `data` (required), `writeTerm`, `timeout`;
`close` takes no fields of its own but accepts an optional params table and an optional
`onError` — `ATE.close(dev [, params] [, onError])` — which is used if the close itself fails.
UDP has no terminators: its `read` takes `size` and `timeout`, and its `write` takes `data`
(required) and `timeout`. Passing a `readTerm` to a UDP Device is an error naming the field.
Builder setters: `setHost`, `setPort`, `setLocalPort`, `setResource`, `setBaud`, `setDataBits`,
`setStopBits`, `setParity`, `setFlowControl`, `setTimeout`, `setReadTerm`, `setWriteTerm`,
`setSize`, and `devices.Write(data)`.

Reads and writes are raw bytes. The terminator is stripped from `data`; `count` is what is
left. A write's `count` includes the terminator.

TCP, SERIAL and UDP are read in the background from `open` to `close` and buffered, capped at
the Station config key `devices.receiveBufferBytes` (16777216, 16 MiB); on overflow ATE drops
the oldest bytes, logs one WARNING, and the next read carries `overflow = true`. `ATE.flush(dev)`
empties the buffer and returns the count discarded. VISA has no background reader and no buffer,
so `flush` discards nothing.

A VISA resource string names the instrument, and the shape matters:
`GPIB0::5::INSTR` is a GPIB instrument, `TCPIP0::192.168.1.10::inst0::INSTR` is a VXI-11
instrument, and `TCPIP0::127.0.0.1::5025::SOCKET` is a plain TCP socket wrapped in a VISA
session - the same stream a `DeviceType.TCP` Device would open, with pyvisa in the way. ATE
uses whichever pyvisa backend it finds; only pyvisa-py is shipped, which reaches TCPIP and
USBTMC. A GPIB instrument needs a real VISA driver on the Station (NI-VISA or linux-GPIB): ask
the schedule author which resource string their instrument needs before writing the schedule,
because nothing installed with ATE can open a GPIB address.

```lua:docs/manuals/examples/test-writer/tcp.tslua
-- tcp.tslua: a TCP Device, from open to close.
--
-- The host and port come from the environment, which the test harness sets to a
-- simulator peer listening on 127.0.0.1. Point them at a real instrument to use this
-- schedule as it is.
--
-- Demonstrates:
--   * the ate.devices builders, whose setters chain, and how a Device opened in the
--     Pre-test Action is still open in the steps, because a device object lives for the
--     whole Test Run;
--   * per-call overrides (timeout, readTerm, size) that do not change the open-time
--     configuration;
--   * a read that times out after some bytes is a partial read, not a Device failure;
--   * a read that times out with nothing at all is a Device failure and goes through
--     onError, which is given the failure table and the params table and may return a
--     value for the call to carry on with;
--   * flush discards whatever the receive buffer already holds.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

local instrument = nil

return ScheduleParams("Manual: TCP", "open, write, read, flush and close a TCP Device")
  :setStopOnFail(false)
  :setPreTestAction(function()
    -- devices.Tcp(host, port) fills in every field TCP needs: timeout 1.0 s,
    -- readTerm and writeTerm "\n". open has no defaults of its own, so a plain table
    -- would have to name them all.
    instrument = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(2.0))
    ATE.addEquipment("SCPI simulator", "SIM-TCP", "TCP " .. host .. ":" .. port, "2026-01-01")
  end)
  :setPostTestAction(function(passFail)
    ATE.close(instrument)   -- closing a second time does nothing and still reports ok
  end)
  :setSchedule({
    StepParams("Identify the instrument"):setStepNo("1.")
      :setAction(function()
        local sent = ATE.write(instrument, devices.Write("*IDN?"))
        local reply = ATE.read(instrument, devices.Read():setTimeout(1.0))
        ATE.report((sent.ok and reply.count > 0) and Result.PASS or Result.FAIL,
                   "reply: " .. (reply.data or "nothing"),
                   reply.count, 26)
      end),

    -- The peer answers "MEAS:VOLT?" with "1.234\n". readTerm "3" stops the read at the "3",
    -- so the first read takes "1.2" and the second takes the rest of the reply.
    StepParams("Stop a read at a terminator"):setStepNo("2.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MEAS:VOLT?"))
        local first = ATE.read(instrument, devices.Read():setReadTerm("3"))
        local rest = ATE.read(instrument)
        ATE.report((first.data == "1.2" and rest.data == "4") and Result.PASS or Result.FAIL,
                   "first read " .. (first.data or "nothing") .. ", second " .. (rest.data or "nothing"),
                   first.count + rest.count, 4)
      end),

    -- "PARTIAL?" is answered with bytes and no terminator: the read times out having taken
    -- some of them. That is partial = true, a WARNING in the log, and not a Device failure.
    StepParams("A read that times out partway"):setStepNo("3.")
      :setAction(function()
        ATE.write(instrument, devices.Write("PARTIAL?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3))
        ATE.report((r.partial and r.count > 0) and Result.PASS or Result.FAIL,
                   "got " .. r.count .. " bytes before the timeout",
                   tostring(r.partial), "true")
      end),

    -- "NEVER?" is answered with silence: the read times out with no bytes at all, which IS
    -- a Device failure, so onError runs. Returning a value makes the call continue with it.
    StepParams("Handle a read that times out"):setStepNo("4.")
      :setAction(function()
        ATE.write(instrument, devices.Write("NEVER?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3), function(opResult, opArgs)
          ATE.log(Log.WARNING, "no reply from the instrument: " .. opResult.err)
          return { data = nil, count = 0, partial = false }
        end)
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "the timeout was handled, so the step carried on")
      end),

    -- The peer sends three lines; the schedule reads the first and throws away the rest.
    StepParams("Discard the receive buffer"):setStepNo("5.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MULTI?"))
        ATE.read(instrument, devices.Read():setSize(7))
        local f = ATE.flush(instrument)
        ATE.report((f.ok and f.count > 0) and Result.PASS or Result.FAIL, "discarded " .. f.count .. " buffered bytes")
      end),
  })
```

```lua:docs/manuals/examples/test-writer/udp.tslua
-- udp.tslua: a UDP Device, where one datagram is one message.
--
-- Demonstrates:
--   * UDP has no terminators, so readTerm and writeTerm are not fields this Device Type
--     knows; passing one is a schedule bug that raises naming the field;
--   * a datagram longer than the size asked for is truncated and returned with partial =
--     true, which the log calls a truncation rather than a timeout;
--   * the receive buffer is a queue of whole datagrams, so a reply that arrives before the
--     schedule reads it is not lost.
local devices = require("ate.devices")

local host = os.getenv("ATE_UDP_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_UDP_PORT")) or 5026

return ScheduleParams("Manual: UDP", "send and read datagrams")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Echo one datagram"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("PING"))
        local reply = ATE.read(dev)
        ATE.report(reply.data == "ECHO:PING" and Result.PASS or Result.FAIL,
                   "the peer echoed " .. (reply.data or "nothing"),
                   reply.count, 9)
        ATE.close(dev)
      end),

    -- Ask for 10 bytes of a 40-byte datagram: ATE returns the first 10 and says partial.
    StepParams("Truncate a datagram"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("a datagram longer than the size asked for"))
        local r = ATE.read(dev, devices.Read():setSize(10))
        ATE.report((r.count == 10 and r.partial) and Result.PASS or Result.FAIL,
                   "read " .. r.count .. " of a longer datagram",
                   r.count, 10)
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/serial.tslua
-- serial.tslua: a SERIAL Device through pyserial.
--
-- The port here is pyserial's socket:// URL, which opens a TCP connection to the
-- simulator peer; a real station names a COM or tty port instead.
--
-- Demonstrates:
--   * open has no defaults, so every field is named;
--   * the Parity and FlowControl enums;
--   * the same connection read with the Device's own terminator.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: SERIAL", "open a serial port and read a reply")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Open the port"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.SERIAL, {
          port = "socket://" .. host .. ":" .. port,
          baud = 115200,
          dataBits = 8,
          stopBits = 1,
          parity = Parity.NONE,
          flowControl = FlowControl.NONE,
          timeout = 2.0,
          readTerm = "\n",
          writeTerm = "\n",
        })
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "serial reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/visa.tslua
-- visa.tslua: a VISA instrument through pyvisa.
--
-- VISA has no background reader: ATE reads the instrument only when the schedule asks,
-- because polling an SCPI instrument with reads that have nothing pending makes it log
-- "Query UNTERMINATED" (-420) and ties up the bus. ATE ships pyvisa-py; a VISA resource
-- string names the instrument, here the simulator peer as a plain TCPIP socket.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: VISA", "read an instrument through pyvisa")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Ask the instrument who it is"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.VISA,
          devices.Visa("TCPIP0::" .. host .. "::" .. port .. "::SOCKET"):setTimeout(2.0))
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        local f = ATE.flush(dev)   -- VISA has no receive buffer: this discards nothing
        ATE.log(Log.INFO, "flush discarded " .. f.count .. " bytes")
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "VISA reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

A handle lives for the whole run. Closing a closed Device does nothing and reports success;
using one is a failure through `onError`. At the end of every run ATE closes whatever is still
open and logs a WARNING for each.

```lua:docs/manuals/examples/test-writer/left_open.tslua
-- left_open.tslua: a Device the schedule never closes.
--
-- A Test Run closes every Device still open when it ends, and logs one WARNING naming
-- each one. That is why a Device opened in a Pre-test Action does not have to be closed
-- by hand - it is worth closing anyway, so the log stays clean.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: a Device left open", "ATE closes what the schedule forgets")
  :setSchedule({
    StepParams("Open and forget"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.report(Result.PASS, "opened a Device and left it open")
      end),
  })
```

## Device failures, Stop on Fail, Retry, Abort

`onError(opResult, opArgs)` is called when the operation fails. If it returns anything but
`nil`, the call returns that value and the schedule continues. If it returns `nil`, or there is
no handler, the call raises a Lua error and the step records `Result.ERROR`. A read that times
out having received some bytes is not a failure: it returns `partial = true` and logs a
WARNING. ATE never reconnects: close the Device and open it again with the same params.

A field the Device Type does not know, a missing required field, or a wrong-typed value is not a
Device failure. It never reaches `onError`; the call returns `false` with the message as its
second result and the traceback is logged at ERROR.

```lua:docs/manuals/examples/test-writer/errors.tslua
-- errors.tslua: how Device failures reach the schedule, and what does not.
--
-- Demonstrates:
--   * a Device failure with no onError raises a Lua error, which ends the step as
--     Result.ERROR;
--   * an onError that returns a value makes the call continue with that value;
--   * a bad argument (a misspelled field) is a schedule bug, not a Device failure: it
--     never reaches onError, it is logged at ERROR, and the call returns false with the
--     message as its second result;
--   * the handler given to open is the fallback for that Device's read, write and close.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025
local deadPort = tonumber(os.getenv("ATE_DEAD_PORT")) or 1

return ScheduleParams("Manual: Device errors", "onError, a closed Device and a bad field")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A misspelled field"):setStepNo("1.")
      :setAction(function()
        local opened, why = ATE.open(DeviceType.TCP, { host = host, port = port, timeout = 1.0,
                                                     readTerm = "\n", writeTerm = "\n", buad = 9600 })
        ATE.report(opened == false and Result.PASS or Result.FAIL, "the open was refused: " .. why)
      end),

    -- Nothing is listening on deadPort: the open fails. This handler logs and returns nil,
    -- which means "no recovery", so ATE.open raises and the step records Result.ERROR.
    StepParams("A port nothing listens on"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, deadPort):setTimeout(0.5),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "nothing is listening on " .. opArgs.host .. ":" .. opArgs.port)
            return nil
          end)
        ATE.report(Result.PASS, "this line is never reached")
      end),

    -- A Device that is closed cannot be read. With no handler the call raises.
    StepParams("Read a closed Device"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.close(dev)
        ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(Result.PASS, "this line is never reached either")
      end),

    -- The same failure, handled: the step carries on with what onError returns.
    StepParams("Handle the closed Device"):setStepNo("4.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "the Device is gone: " .. opResult.err)
            return { data = nil, count = 0, partial = false }
          end)
        ATE.close(dev)
        local r = ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "recovered from reading a closed Device")
      end),
  })
```

Stop on Fail pauses the run at a `FAIL` or `ERROR` step so the Operator can choose `CONTINUE`,
`RETRY` or `ABORT`. Retry runs the action again from the start with POLL reset and blocking
inputs cleared; each Attempt is recorded and the step's result is the last Attempt's. There is
no Attempt number in Lua - keep your own count.

```lua:docs/manuals/examples/test-writer/retry.tslua
-- retry.tslua: Stop on Fail, and what a Retry does.
--
-- Demonstrates:
--   * setStopOnFail(true) is the default: a step ending FAIL or ERROR pauses the run and
--     the Operator chooses Continue, Retry or Abort.
--   * Retry runs the step's action again from the beginning. Each Attempt is recorded;
--     the step's result is its last Attempt's, and the Operator's choice is recorded too.
--   * ATE gives a step no way to read its own Attempt number, so the schedule keeps the
--     count itself in a local the action closes over.
local attempts = 0

return ScheduleParams("Manual: retry", "the first Attempt fails, the Retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Retry this one"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt " .. attempts .. ": the joint is cold", 0.2, 2.4)
        else
          ATE.report(Result.PASS, "attempt " .. attempts .. ": the joint is warm", 2.4, 2.4)
        end
      end),

    -- Only reached after the step passed, or after the Operator chose to continue.
    StepParams("Next check"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "the rail is steady", 5.0, 5.0)
      end),
  })
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

## Step Inputs

Every declaration needs `desc` and `type`. `type` is one of `ENUM`, `INTEGER`, `FLOAT`,
`STRING`, `PROMPT`, `POLL`.

| field | applies to | meaning |
| --- | --- | --- |
| `input` | `ENUM` | `RADIOBUT`, `CHECKBOX` or `PUSHBUT` |
| `enum` | `ENUM` | labels, at least one |
| `range` | `INTEGER`, `FLOAT` | `{min=, max=}`; empty is unbounded |
| `pattern` | `STRING` | regex the value must match |
| `prompt` | `PROMPT`, `POLL` | a string, or an array of button texts |
| `tooltip` | all | hover help |
| `orient` | all | `VERT` or `HORIZ` |

A field on the wrong type is a load error. A field the format does not know is ignored.

`ATE.input(n)` returns: the label for `RADIOBUT`/`PUSHBUT`; a Lua table of labels for
`CHECKBOX` (empty when none are ticked); an integer for `INTEGER`; a float for `FLOAT`; the
text for `STRING` and `PROMPT`; the clicked button's text for `POLL`. `POLL` never blocks: it
returns `nil` until clicked and is sticky until the Attempt starts again. `n` out of range is
an error.

```lua:docs/manuals/examples/test-writer/inputs.tslua
-- inputs.tslua: every Step Input type, and what ATE.input(n) returns for each.
--
-- Demonstrates:
--   * ENUM with each widget: RADIOBUT picks one label, CHECKBOX returns a table of
--     every label ticked (empty when none are), PUSHBUT is answered by pressing one.
--   * INTEGER and FLOAT with a range: the harness first answers out of range, which the
--     session refuses before the valid value is recorded.
--   * STRING with a pattern, refused the same way.
--   * PROMPT blocks the step until the Operator answers; POLL does not block, so the
--     step reads it in a loop. A POLL is sticky: once clicked it keeps returning that
--     button's text until the Attempt starts again.
--   * tooltip and orient apply to every input type.
return ScheduleParams("Manual: step inputs", "one step per Step Input type")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Pick one"):setStepNo("1.")
      :setInputs({ [1] = { desc = "LED colour", type = "ENUM", input = "RADIOBUT",
                          enum = { "Green", "Red", "Off" }, orient = "HORIZ",
                          tooltip = "Which LED the board lights" } })
      :setAction(function()
        local colour = ATE.input(1)
        ATE.report(Result.PASS, "the LED is " .. colour, colour, "Green")
      end),

    StepParams("Tick several"):setStepNo("2.")
      :setInputs({ [1] = { desc = "channels to measure", type = "ENUM", input = "CHECKBOX",
                          enum = { "A", "B", "C" }, orient = "VERT" } })
      :setAction(function()
        local channels = ATE.input(1)          -- a Lua table of labels, [] when none ticked
        ATE.report(Result.PASS, "measuring " .. table.concat(channels, ", "),
                   table.concat(channels, ","), "A,B")
      end),

    StepParams("Press a button"):setStepNo("3.")
      :setInputs({ [1] = { desc = "what to do next", type = "ENUM", input = "PUSHBUT",
                          enum = { "RETEST", "SKIP" } } })
      :setAction(function()
        local choice = ATE.input(1)
        ATE.report(Result.PASS, "the Operator pressed " .. choice, choice, "RETEST")
      end),

    StepParams("Whole number"):setStepNo("4.")
      :setInputs({ [1] = { desc = "test current, mA", type = "INTEGER",
                          range = { min = 1, max = 200 } } })
      :setAction(function()
        local current = ATE.input(1)           -- a Lua integer
        ATE.report(Result.PASS, "current is " .. current .. " mA", current, 42)
      end),

    StepParams("Decimal"):setStepNo("5.")
      :setInputs({ [1] = { desc = "rail voltage, V", type = "FLOAT",
                          range = { min = 0.5, max = 2.5 } } })
      :setAction(function()
        local volts = ATE.input(1)             -- a Lua float
        ATE.report(Result.PASS, "rail is " .. volts .. " V", volts, 1.25)
      end),

    StepParams("Text with a pattern"):setStepNo("6.")
      :setInputs({ [1] = { desc = "probe part number", type = "STRING",
                          pattern = "^[A-Z]{2}-[0-9]{4}$" } })
      :setAction(function()
        local part = ATE.input(1)
        ATE.report(Result.PASS, "probe " .. part, part, "AB-1234")
      end),

    StepParams("Blocking prompt"):setStepNo("7.")
      :setInputs({ [1] = { desc = "is the probe connected?", type = "PROMPT",
                          prompt = { "CONTINUE" } } })
      :setAction(function()
        local answer = ATE.input(1)            -- blocks until the Operator presses it
        ATE.report(Result.PASS, "probe confirmed: " .. answer, answer, "CONTINUE")
      end),

    StepParams("POLL in a loop"):setStepNo("8.")
      :setInputs({ [1] = { desc = "stop this step?", type = "POLL",
                          prompt = { "KEEP GOING", "STOP" } } })
      :setAction(function()
        local clicked, reads = nil, 0
        while reads < 8 do
          reads = reads + 1
          clicked = ATE.input(1)               -- nil until a button is clicked
          if clicked then break end
          ATE.sleep(0.1)                       -- a POLL never blocks the step
        end
        if clicked then
          ATE.report(Result.PASS, "clicked " .. clicked .. " after " .. reads .. " reads",
                     clicked, "STOP")
        else
          ATE.report(Result.FAIL, "no click after " .. reads .. " reads")
        end
      end),
  })
```

## Actions and Equipment

A Pre-test Action runs before the first step; a Post-test Action runs after the last one, in
reverse Pre-test order. Both are called with the run's standing result as their first argument
- always `"PASS"` for a Pre-test Action, because nothing has failed yet. A Pre-test Action that
raises aborts the run with `PRETEST_FAILED` and every step `NOTRUN`, and the Post-test Actions
still run: every schedule instance the Pre-test pass reached gets its teardown, in reverse
order, including the instance whose own Pre-test Action raised; an instance the pass never got
to has none. Post-test Actions run even after an abort: ATE clears the abort flag before them,
an abort during one ends that Action only, and an error in one fails the run while the rest
still run.

`ATE.addEquipment(name, serial, connection, calDue, notes)` - all strings, `name` required.
A repeat of the same `name` and `serial` adds no row and logs a DEBUG line.

```lua:docs/manuals/examples/test-writer/actions.tslua
-- actions.tslua: Pre-test and Post-test Actions, and Equipment.
--
-- Demonstrates:
--   * a Pre-test Action runs before the first step; a Post-test Action runs after the
--     last one. Both are called with the run's current result as their first argument:
--     a Pre-test Action always sees "PASS", the Post-test one sees the final PASS or FAIL.
--   * ATE.addEquipment is legal in an Action, a step, or both; the report's Equipment
--     section comes from it. Declaring the same name and serial twice adds no second row.
--   * ATE.report and ATE.input are NOT legal in an Action: only Test Steps have a result
--     and an Operator prompt.
return ScheduleParams("Manual: Actions", "Pre-test and Post-test Actions")
  :setPreTestAction(function(passFail)
    ATE.log(Log.INFO, "pre-test action, run stands at " .. passFail)
    ATE.addEquipment("Bench power supply", "PSU-123", "GPIB::1", "2026-12-01", "0-60 V")
  end)
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action, run result was " .. passFail)
  end)
  :setSchedule({
    StepParams("Measure the rail"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "rail at 5.0 V", 5.0, 5.0)
      end),
  })
```

`ATE.log(level, ...)` writes to the run log; `print` is redirected to
`ATE.log(Log.INFO, ...)`. `ATE.run` is the read-only table of the current run:

```lua:docs/manuals/examples/test-writer/logging.tslua
-- logging.tslua: logging inside a Test Run, and reading the run's own context.
--
-- Demonstrates:
--   * ATE.log(level, ...) with the Log enum; every argument is turned to text and the
--     parts are joined with spaces;
--   * print is redirected to ATE.log(Log.INFO, ...), so a stray print is still recorded;
--   * the main screen shows INFO and above; the debug window shows every level;
--   * ATE.run is a read-only table of the current Test Run. Writing to it is an error.
--   * DEBUG device tracing is off unless the application config turns it on with
--     deviceTrace.enabled; every line a schedule writes is stored with the Test Run.
return ScheduleParams("Manual: logging", "log levels, print and ATE.run")
  :setSchedule({
    StepParams("One line at each level"):setStepNo("1.")
      :setAction(function()
        ATE.log(Log.DEBUG, "a detail only the debug window shows")
        ATE.log(Log.INFO, "the rail settled", 5.0, "V")
        ATE.log(Log.WARNING, "the probe is loose")
        ATE.log(Log.ERROR, "the supply did not come up")
        print("print lands at INFO")
        ATE.report(Result.PASS, "logged at every level")
      end),

    StepParams("Who is running"):setStepNo("2.")
      :setAction(function()
        ATE.log(Log.INFO, "station " .. ATE.run.Station
                             .. ", project " .. ATE.run.Project
                             .. ", UUT " .. ATE.run.UUT_serial
                             .. ", operator " .. ATE.run.Operator
                             .. ", schedule " .. ATE.run.Test_Schedule_Name
                             .. ", release " .. ATE.run.Media_Release_Media_Number)
        ATE.report(Result.PASS, "read the run context", ATE.run.Operator, "Ada Lovelace")
      end),
  })
```

## Sub-schedules

`:setInclude(file, keyvals)` expands the file's steps beneath the header row. The included file
gets `keyvals` as the chunk argument (`local kv = ...`), and ATE runs it again per include, so
each include is its own instance. `keyvals` must be plain data (strings, numbers, booleans,
nested tables of those). A top-level schedule, or an include with no keyvals, gets `{}`.

The file is resolved against the release that owns the including schedule, then its
dependencies depth first - the same order as `require`. Prefixes are prepended exactly as
written and stack outward-in. A header row is not a Test Step: it has no Result and is not
counted in the summary. `setEnable(false)` on a header forces the default of every step
beneath it, and an instance whose whole subtree is disabled has both its Actions skipped. The
included schedule's doc fields and its `setStopOnFail` are ignored; only its Actions and steps
take part.

```lua:docs/manuals/examples/test-writer/main.tslua
-- main.tslua: the top-level schedule that includes Sub-schedules.
--
-- Demonstrates:
--   * :setInclude(file, keyvals) makes a step a header row, not a Test Step: the steps of
--     the included schedule are pulled in beneath it.
--   * The same file can be included more than once; each include is its own instance with
--     its own keyvals.
--   * :setPrefix is prepended to the included steps' Step Numbers, as written, with no
--     separator, and prefixes stack as the includes nest.
--   * :setEnable on a header forces the default of every step beneath it.
return ScheduleParams("Manual: sub-schedules", "includes, keyvals, prefixes and nesting")
  :setDocTitle("Manual examples - Sub-schedules")
  :setDocId("MR-MANUAL-SUBS")
  :setDocRev("1.0")
  :setSchedule({
    StepParams("Rail present"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "the rail is fitted") end),

    -- 2.1. and 2.2. come from channel.tslua, included once for channel 1.
    StepParams("Channel 1"):setStepNo("2.")
      :setInclude("channel.tslua", { channel = 1, label = "left rail" }):setPrefix("2."),

    -- A disabled header: every step beneath it records NOTRUN, and its Actions are skipped.
    StepParams("Channel 2, not fitted"):setStepNo("3.")
      :setEnable(false)
      :setInclude("channel.tslua", { channel = 2, label = "right rail" }):setPrefix("3."),
  })
```

```lua:docs/manuals/examples/test-writer/channel.tslua
-- channel.tslua: a Sub-schedule. It is complete enough to run on its own, and is listed
-- as hidden in manual.atedef so the Operator never picks it directly.
--
-- Each include runs this file again, with the keyvals passed as the chunk's argument.
-- Its own Actions take part in the run's Pre-test and Post-test order.
local kv = ...
local channel = kv.channel or 1
local label = kv.label or "a channel"

return ScheduleParams("Channel check", "one output channel")
  :setPreTestAction(function()
    ATE.log(Log.INFO, "channel " .. channel .. " (" .. label .. ") pre-test")
  end)
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "channel " .. channel .. " post-test, run stands at " .. passFail)
  end)
  :setSchedule({
    StepParams("Switch the channel on"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "channel " .. channel .. " switches on")
      end),

    -- A Sub-schedule inside a Sub-schedule: step numbers become 2.2.1. and so on.
    StepParams("Its LEDs"):setStepNo("2.")
      :setInclude("led.tslua", { channel = channel }):setPrefix("2."),
  })
```

```lua:docs/manuals/examples/test-writer/led.tslua
-- led.tslua: the innermost Sub-schedule, included by channel.tslua.
local kv = ...
local channel = kv.channel or 1

return ScheduleParams("LED check", "the indicator LEDs of one channel")
  :setSchedule({
    StepParams("The LED lights"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "channel " .. channel .. " LED is lit")
      end),
  })
```

## Modules and the `.atedef`

`require` is ATE's own. It resolves the name against the release's files, then its
dependencies, never against `package.path`, which ATE empties. An unlisted name is an error. A
module runs once per Test Run and its return value is cached. `ate.` is reserved for modules
bundled with ATE (`ate.devices`).

Checksums are SHA-256 of the raw bytes, lowercase hex. A mismatch is a WARNING, the file still
loads, and both checksums are recorded with the run. `ate definition update <file.atedef>`
computes each listed file's SHA-256 and writes it back over the old value, filling in any
`sha256` that is absent; the file is only rewritten if a checksum actually changed, so a
correct `.atedef` comes back byte for byte the same. A listed file it cannot read keeps its
current value and warns.

Every field a `.atedef` can carry:

| Field | Meaning |
| --- | --- |
| `format` | must be `1`; anything else makes the definition broken |
| `mediaNumber` | the release's identity; not a version |
| `name`, `version` | `version` must be valid semver 2.0.0; other releases name it in `depends` |
| `depends` | a list of `{"name": ..., "version": ...}`; resolved depth first, in declaration order |
| `files[]` | one entry per Lua file: `path`, `sha256` |
| `files[].path` | relative to the `.atedef` (or absolute), forward slashes on both platforms |
| `files[].hidden` | only on a `.tslua`: keeps it off the Run screen while still allowing it as a Sub-schedule |
| `files[].module` | only on a `.lua`: the name `require` uses. Otherwise the path derives it: `lib/rails.lua` is `lib.rails`. An absolute path, or one containing `..`, must state it |

Here is the definition the manual's own examples ship, exactly as it sits on disk - note
`channel.tslua` and `led.tslua` marked `hidden`, and `modules/limits.lua` given the module name
`board_limits`:

```json:docs/manuals/examples/test-writer/manual.atedef
{
  "format": 1,
  "mediaNumber": "MR-MANUAL-TW",
  "name": "Test writer manual examples",
  "version": "1.0.0",
  "files": [
    {
      "path": "first.tslua",
      "sha256": "a260d2b923819ff6f71804370d4afce9ac77269ec9d9cf9c4b0dae35b85e48c8"
    },
    {
      "path": "steps.tslua",
      "sha256": "2203cddc55bf0daf0e557d3ecf1b847e77bf8fa873385cea775e32f240c5af97"
    },
    {
      "path": "inputs.tslua",
      "sha256": "6a746ebad482542f81d5b846b65a57adb467942e5289d9fc7e76b9bdaabd22fc"
    },
    {
      "path": "actions.tslua",
      "sha256": "a15e9a9fa060f0e829cc2e047d2c0313a1a2203c7c33356e792f699d1b02c526"
    },
    {
      "path": "retry.tslua",
      "sha256": "5375d9870ad11af1553eeaa1d5fd27ce586c2a5cce78af6b4a493a029fee58c9"
    },
    {
      "path": "results.tslua",
      "sha256": "49f80374f637c13e34281140912335fcb583fa87865674def089b18188130ce7"
    },
    {
      "path": "main.tslua",
      "sha256": "fc768ae110764fb3a7a9a4a29fdd93df3beafc09b8fff6ea7fa71efa825964d4"
    },
    {
      "path": "channel.tslua",
      "hidden": true,
      "sha256": "1cecd8b3566e4783c86375ebcdac6b69eaae52f1ea74e32c495babf7009b6e63"
    },
    {
      "path": "led.tslua",
      "hidden": true,
      "sha256": "3477e6a5a1578108bd76687e13e211ca1c48b5bac3343160a4e86319f87f46f8"
    },
    {
      "path": "modules.tslua",
      "sha256": "f34234441c454b9e0559e778fb86c6f10086f2edd4248207953ce67f22fae386"
    },
    {
      "path": "lib/rails.lua",
      "sha256": "392e385ab81dfb814a7ddec0cb1c0799c97d714b3d97c2cfb3306e6ba6cde029"
    },
    {
      "path": "modules/limits.lua",
      "module": "board_limits",
      "sha256": "ea8a21a8a7bef16b0abaa9efcc07c8f08416b4a07b712ac35b9ffdbf8c31fd99"
    },
    {
      "path": "tcp.tslua",
      "sha256": "8439beda05cc96f5c6bd69629951fc2318a7526eb085fb2bb322fc711c2cb8c7"
    },
    {
      "path": "udp.tslua",
      "sha256": "324ce6659ab74bcea159fbe20d063925e5b5a88992b2ad1755e4d68625f461b4"
    },
    {
      "path": "serial.tslua",
      "sha256": "dd717a441db74b490b84b999f82a54b7e0d2b3b3185906ae83150562c7dcb768"
    },
    {
      "path": "visa.tslua",
      "sha256": "5c9d69646277814d6d0e134380ba32e6402ce4d9d3f4006dbe4f622bb107afb8"
    },
    {
      "path": "errors.tslua",
      "sha256": "66151279fc9e62462e80a75e273785fac0bb1c8f2a7bf0aeec62f312a1fb0fe2"
    },
    {
      "path": "left_open.tslua",
      "sha256": "a53f424fe15813bafdcdf74f0c7eb4e69b37bbbb5fa27d833b58eb7d1923b279"
    },
    {
      "path": "logging.tslua",
      "sha256": "ee6b7ee6f1a3b1ed15522544d93645c694dfd29d27d53188416c8e956170dd02"
    },
    {
      "path": "abort.tslua",
      "sha256": "b164d0057d61fa7d5f4092a2b47154dc4ca16dfd685640ef09338f4b862eb308"
    },
    {
      "path": "sandbox.tslua",
      "sha256": "e40b7d8eafd167b5d52f844957962b505f53009d791a38bb2729413462aaee99"
    },
    {
      "path": "forbidden.tslua",
      "sha256": "eae7b5e968792a6abdcf883ccde1e956fdf4f7a6c14e2d8aab8ff02c1fc8dd97"
    }
  ]
}
```

```lua:docs/manuals/examples/test-writer/modules.tslua
-- modules.tslua: sharing code with require, including ATE's own bundled modules.
--
-- Demonstrates:
--   * require finds only the files the Test Definition lists (and its dependencies'),
--     never anything on package.path;
--   * the module name is derived from a plain relative path, or given explicitly with the
--     "module" field;
--   * the reserved "ate." namespace is ATE's own: require("ate.devices") is always ATE's
--     Device params builder, never a file in the Media Release.
local rails = require("lib.rails")      -- derived name: the path without .lua, / as .
local limits = require("board_limits")  -- explicit "module" name in manual.atedef
local devices = require("ate.devices")  -- bundled with ATE

return ScheduleParams("Manual: modules", "require and the ate. namespace")
  :setSchedule({
    StepParams("Check every rail"):setStepNo("1.")
      :setAction(function()
        local ok = true
        for channel = 1, 3 do
          local volts = rails.nominalVolts(channel)
          ok = ok and volts >= limits.low and volts <= limits.high
          ATE.log(Log.INFO, "channel " .. channel .. " nominal " .. volts .. " V")
        end
        ATE.report(ok and Result.PASS or Result.FAIL,
                   "every rail is inside " .. limits.low .. " to " .. limits.high .. " V")
      end),

    -- A builder is just a params table: nothing is opened, so no Device is involved.
    StepParams("Build params without opening"):setStepNo("2.")
      :setAction(function()
        local params = devices.Tcp("127.0.0.1", 5025):setTimeout(2.5)
        ATE.report(Result.PASS, "timeout set to " .. params.timeout, params.timeout, 2.5)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/lib/rails.lua
-- lib/rails.lua: an ordinary Lua module. Its module name is derived from its path:
-- "lib/rails.lua" becomes "lib.rails". require runs a module once per Test Run and
-- caches what it returns; later requires of the same name get the cached value.
local rails = {}

function rails.nominalVolts(channel)
  return 1.5 * channel
end

return rails
```

```lua:docs/manuals/examples/test-writer/modules/limits.lua
-- modules/limits.lua: a module whose name is given explicitly in manual.atedef with the
-- "module" field, because a name derived from the path would be "modules.limits".
return { low = 1.0, high = 5.0 }
```

## Devices

`ATE.open(deviceType, params [, onError])` returns an opaque handle. Every field `open` needs
is required - there are no defaults in `ATE.open`; the builders supply them.

| Device Type | `open` fields | builder (defaults) |
| --- | --- | --- |
| `DeviceType.TCP` | `host`, `port`, `timeout`, `readTerm`, `writeTerm` | `devices.Tcp(host, port)`: timeout 1.0, terminators `"\n"` |
| `DeviceType.SERIAL` | `port`, `baud`, `dataBits`, `stopBits`, `parity`, `flowControl`, `timeout`, `readTerm`, `writeTerm` | `devices.Serial(port)`: 9600, 8, 1, `Parity.NONE`, `FlowControl.NONE`, 1.0, terminators `"\n"` |
| `DeviceType.UDP` | `host`, `port`, `localPort`, `timeout` | `devices.Udp(host, port)`: `localPort` 0, timeout 1.0 |
| `DeviceType.VISA` | `resource`, `timeout`, `readTerm`, `writeTerm` | `devices.Visa(resource)`: timeout 1.0, terminators `"\n"` |

Per-call fields, all optional, falling back to the open-time configuration: `read` takes
`size`, `timeout`, `readTerm`; `write` takes `data` (required), `writeTerm`, `timeout`;
`close` takes no fields of its own but accepts an optional params table and an optional
`onError` — `ATE.close(dev [, params] [, onError])` — which is used if the close itself fails.
UDP has no terminators: its `read` takes `size` and `timeout`, and its `write` takes `data`
(required) and `timeout`. Passing a `readTerm` to a UDP Device is an error naming the field.
Builder setters: `setHost`, `setPort`, `setLocalPort`, `setResource`, `setBaud`, `setDataBits`,
`setStopBits`, `setParity`, `setFlowControl`, `setTimeout`, `setReadTerm`, `setWriteTerm`,
`setSize`, and `devices.Write(data)`.

Reads and writes are raw bytes. The terminator is stripped from `data`; `count` is what is
left. A write's `count` includes the terminator.

TCP, SERIAL and UDP are read in the background from `open` to `close` and buffered, capped at
the Station config key `devices.receiveBufferBytes` (16777216, 16 MiB); on overflow ATE drops
the oldest bytes, logs one WARNING, and the next read carries `overflow = true`. `ATE.flush(dev)`
empties the buffer and returns the count discarded. VISA has no background reader and no buffer,
so `flush` discards nothing.

A VISA resource string names the instrument, and the shape matters:
`GPIB0::5::INSTR` is a GPIB instrument, `TCPIP0::192.168.1.10::inst0::INSTR` is a VXI-11
instrument, and `TCPIP0::127.0.0.1::5025::SOCKET` is a plain TCP socket wrapped in a VISA
session - the same stream a `DeviceType.TCP` Device would open, with pyvisa in the way. ATE
uses whichever pyvisa backend it finds; only pyvisa-py is shipped, which reaches TCPIP and
USBTMC. A GPIB instrument needs a real VISA driver on the Station (NI-VISA or linux-GPIB): ask
the schedule author which resource string their instrument needs before writing the schedule,
because nothing installed with ATE can open a GPIB address.

```lua:docs/manuals/examples/test-writer/tcp.tslua
-- tcp.tslua: a TCP Device, from open to close.
--
-- The host and port come from the environment, which the test harness sets to a
-- simulator peer listening on 127.0.0.1. Point them at a real instrument to use this
-- schedule as it is.
--
-- Demonstrates:
--   * the ate.devices builders, whose setters chain, and how a Device opened in the
--     Pre-test Action is still open in the steps, because a device object lives for the
--     whole Test Run;
--   * per-call overrides (timeout, readTerm, size) that do not change the open-time
--     configuration;
--   * a read that times out after some bytes is a partial read, not a Device failure;
--   * a read that times out with nothing at all is a Device failure and goes through
--     onError, which is given the failure table and the params table and may return a
--     value for the call to carry on with;
--   * flush discards whatever the receive buffer already holds.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

local instrument = nil

return ScheduleParams("Manual: TCP", "open, write, read, flush and close a TCP Device")
  :setStopOnFail(false)
  :setPreTestAction(function()
    -- devices.Tcp(host, port) fills in every field TCP needs: timeout 1.0 s,
    -- readTerm and writeTerm "\n". open has no defaults of its own, so a plain table
    -- would have to name them all.
    instrument = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(2.0))
    ATE.addEquipment("SCPI simulator", "SIM-TCP", "TCP " .. host .. ":" .. port, "2026-01-01")
  end)
  :setPostTestAction(function(passFail)
    ATE.close(instrument)   -- closing a second time does nothing and still reports ok
  end)
  :setSchedule({
    StepParams("Identify the instrument"):setStepNo("1.")
      :setAction(function()
        local sent = ATE.write(instrument, devices.Write("*IDN?"))
        local reply = ATE.read(instrument, devices.Read():setTimeout(1.0))
        ATE.report((sent.ok and reply.count > 0) and Result.PASS or Result.FAIL,
                   "reply: " .. (reply.data or "nothing"),
                   reply.count, 26)
      end),

    -- The peer answers "MEAS:VOLT?" with "1.234\n". readTerm "3" stops the read at the "3",
    -- so the first read takes "1.2" and the second takes the rest of the reply.
    StepParams("Stop a read at a terminator"):setStepNo("2.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MEAS:VOLT?"))
        local first = ATE.read(instrument, devices.Read():setReadTerm("3"))
        local rest = ATE.read(instrument)
        ATE.report((first.data == "1.2" and rest.data == "4") and Result.PASS or Result.FAIL,
                   "first read " .. (first.data or "nothing") .. ", second " .. (rest.data or "nothing"),
                   first.count + rest.count, 4)
      end),

    -- "PARTIAL?" is answered with bytes and no terminator: the read times out having taken
    -- some of them. That is partial = true, a WARNING in the log, and not a Device failure.
    StepParams("A read that times out partway"):setStepNo("3.")
      :setAction(function()
        ATE.write(instrument, devices.Write("PARTIAL?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3))
        ATE.report((r.partial and r.count > 0) and Result.PASS or Result.FAIL,
                   "got " .. r.count .. " bytes before the timeout",
                   tostring(r.partial), "true")
      end),

    -- "NEVER?" is answered with silence: the read times out with no bytes at all, which IS
    -- a Device failure, so onError runs. Returning a value makes the call continue with it.
    StepParams("Handle a read that times out"):setStepNo("4.")
      :setAction(function()
        ATE.write(instrument, devices.Write("NEVER?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3), function(opResult, opArgs)
          ATE.log(Log.WARNING, "no reply from the instrument: " .. opResult.err)
          return { data = nil, count = 0, partial = false }
        end)
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "the timeout was handled, so the step carried on")
      end),

    -- The peer sends three lines; the schedule reads the first and throws away the rest.
    StepParams("Discard the receive buffer"):setStepNo("5.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MULTI?"))
        ATE.read(instrument, devices.Read():setSize(7))
        local f = ATE.flush(instrument)
        ATE.report((f.ok and f.count > 0) and Result.PASS or Result.FAIL, "discarded " .. f.count .. " buffered bytes")
      end),
  })
```

```lua:docs/manuals/examples/test-writer/udp.tslua
-- udp.tslua: a UDP Device, where one datagram is one message.
--
-- Demonstrates:
--   * UDP has no terminators, so readTerm and writeTerm are not fields this Device Type
--     knows; passing one is a schedule bug that raises naming the field;
--   * a datagram longer than the size asked for is truncated and returned with partial =
--     true, which the log calls a truncation rather than a timeout;
--   * the receive buffer is a queue of whole datagrams, so a reply that arrives before the
--     schedule reads it is not lost.
local devices = require("ate.devices")

local host = os.getenv("ATE_UDP_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_UDP_PORT")) or 5026

return ScheduleParams("Manual: UDP", "send and read datagrams")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Echo one datagram"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("PING"))
        local reply = ATE.read(dev)
        ATE.report(reply.data == "ECHO:PING" and Result.PASS or Result.FAIL,
                   "the peer echoed " .. (reply.data or "nothing"),
                   reply.count, 9)
        ATE.close(dev)
      end),

    -- Ask for 10 bytes of a 40-byte datagram: ATE returns the first 10 and says partial.
    StepParams("Truncate a datagram"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("a datagram longer than the size asked for"))
        local r = ATE.read(dev, devices.Read():setSize(10))
        ATE.report((r.count == 10 and r.partial) and Result.PASS or Result.FAIL,
                   "read " .. r.count .. " of a longer datagram",
                   r.count, 10)
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/serial.tslua
-- serial.tslua: a SERIAL Device through pyserial.
--
-- The port here is pyserial's socket:// URL, which opens a TCP connection to the
-- simulator peer; a real station names a COM or tty port instead.
--
-- Demonstrates:
--   * open has no defaults, so every field is named;
--   * the Parity and FlowControl enums;
--   * the same connection read with the Device's own terminator.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: SERIAL", "open a serial port and read a reply")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Open the port"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.SERIAL, {
          port = "socket://" .. host .. ":" .. port,
          baud = 115200,
          dataBits = 8,
          stopBits = 1,
          parity = Parity.NONE,
          flowControl = FlowControl.NONE,
          timeout = 2.0,
          readTerm = "\n",
          writeTerm = "\n",
        })
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "serial reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/visa.tslua
-- visa.tslua: a VISA instrument through pyvisa.
--
-- VISA has no background reader: ATE reads the instrument only when the schedule asks,
-- because polling an SCPI instrument with reads that have nothing pending makes it log
-- "Query UNTERMINATED" (-420) and ties up the bus. ATE ships pyvisa-py; a VISA resource
-- string names the instrument, here the simulator peer as a plain TCPIP socket.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: VISA", "read an instrument through pyvisa")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Ask the instrument who it is"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.VISA,
          devices.Visa("TCPIP0::" .. host .. "::" .. port .. "::SOCKET"):setTimeout(2.0))
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        local f = ATE.flush(dev)   -- VISA has no receive buffer: this discards nothing
        ATE.log(Log.INFO, "flush discarded " .. f.count .. " bytes")
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "VISA reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

A handle lives for the whole run. Closing a closed Device does nothing and reports success;
using one is a failure through `onError`. At the end of every run ATE closes whatever is still
open and logs a WARNING for each.

```lua:docs/manuals/examples/test-writer/left_open.tslua
-- left_open.tslua: a Device the schedule never closes.
--
-- A Test Run closes every Device still open when it ends, and logs one WARNING naming
-- each one. That is why a Device opened in a Pre-test Action does not have to be closed
-- by hand - it is worth closing anyway, so the log stays clean.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: a Device left open", "ATE closes what the schedule forgets")
  :setSchedule({
    StepParams("Open and forget"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.report(Result.PASS, "opened a Device and left it open")
      end),
  })
```

## Device failures, Stop on Fail, Retry, Abort

`onError(opResult, opArgs)` is called when the operation fails. If it returns anything but
`nil`, the call returns that value and the schedule continues. If it returns `nil`, or there is
no handler, the call raises a Lua error and the step records `Result.ERROR`. A read that times
out having received some bytes is not a failure: it returns `partial = true` and logs a
WARNING. ATE never reconnects: close the Device and open it again with the same params.

A field the Device Type does not know, a missing required field, or a wrong-typed value is not a
Device failure. It never reaches `onError`; the call returns `false` with the message as its
second result and the traceback is logged at ERROR.

```lua:docs/manuals/examples/test-writer/errors.tslua
-- errors.tslua: how Device failures reach the schedule, and what does not.
--
-- Demonstrates:
--   * a Device failure with no onError raises a Lua error, which ends the step as
--     Result.ERROR;
--   * an onError that returns a value makes the call continue with that value;
--   * a bad argument (a misspelled field) is a schedule bug, not a Device failure: it
--     never reaches onError, it is logged at ERROR, and the call returns false with the
--     message as its second result;
--   * the handler given to open is the fallback for that Device's read, write and close.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025
local deadPort = tonumber(os.getenv("ATE_DEAD_PORT")) or 1

return ScheduleParams("Manual: Device errors", "onError, a closed Device and a bad field")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A misspelled field"):setStepNo("1.")
      :setAction(function()
        local opened, why = ATE.open(DeviceType.TCP, { host = host, port = port, timeout = 1.0,
                                                     readTerm = "\n", writeTerm = "\n", buad = 9600 })
        ATE.report(opened == false and Result.PASS or Result.FAIL, "the open was refused: " .. why)
      end),

    -- Nothing is listening on deadPort: the open fails. This handler logs and returns nil,
    -- which means "no recovery", so ATE.open raises and the step records Result.ERROR.
    StepParams("A port nothing listens on"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, deadPort):setTimeout(0.5),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "nothing is listening on " .. opArgs.host .. ":" .. opArgs.port)
            return nil
          end)
        ATE.report(Result.PASS, "this line is never reached")
      end),

    -- A Device that is closed cannot be read. With no handler the call raises.
    StepParams("Read a closed Device"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.close(dev)
        ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(Result.PASS, "this line is never reached either")
      end),

    -- The same failure, handled: the step carries on with what onError returns.
    StepParams("Handle the closed Device"):setStepNo("4.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "the Device is gone: " .. opResult.err)
            return { data = nil, count = 0, partial = false }
          end)
        ATE.close(dev)
        local r = ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "recovered from reading a closed Device")
      end),
  })
```

Stop on Fail pauses the run at a `FAIL` or `ERROR` step so the Operator can choose `CONTINUE`,
`RETRY` or `ABORT`. Retry runs the action again from the start with POLL reset and blocking
inputs cleared; each Attempt is recorded and the step's result is the last Attempt's. There is
no Attempt number in Lua - keep your own count.

```lua:docs/manuals/examples/test-writer/retry.tslua
-- retry.tslua: Stop on Fail, and what a Retry does.
--
-- Demonstrates:
--   * setStopOnFail(true) is the default: a step ending FAIL or ERROR pauses the run and
--     the Operator chooses Continue, Retry or Abort.
--   * Retry runs the step's action again from the beginning. Each Attempt is recorded;
--     the step's result is its last Attempt's, and the Operator's choice is recorded too.
--   * ATE gives a step no way to read its own Attempt number, so the schedule keeps the
--     count itself in a local the action closes over.
local attempts = 0

return ScheduleParams("Manual: retry", "the first Attempt fails, the Retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Retry this one"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt " .. attempts .. ": the joint is cold", 0.2, 2.4)
        else
          ATE.report(Result.PASS, "attempt " .. attempts .. ": the joint is warm", 2.4, 2.4)
        end
      end),

    -- Only reached after the step passed, or after the Operator chose to continue.
    StepParams("Next check"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "the rail is steady", 5.0, 5.0)
      end),
  })
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

## Actions and Equipment

A Pre-test Action runs before the first step; a Post-test Action runs after the last one, in
reverse Pre-test order. Both are called with the run's standing result as their first argument
- always `"PASS"` for a Pre-test Action, because nothing has failed yet. A Pre-test Action that
raises aborts the run with `PRETEST_FAILED` and every step `NOTRUN`, and the Post-test Actions
still run: every schedule instance the Pre-test pass reached gets its teardown, in reverse
order, including the instance whose own Pre-test Action raised; an instance the pass never got
to has none. Post-test Actions run even after an abort: ATE clears the abort flag before them,
an abort during one ends that Action only, and an error in one fails the run while the rest
still run.

`ATE.addEquipment(name, serial, connection, calDue, notes)` - all strings, `name` required.
A repeat of the same `name` and `serial` adds no row and logs a DEBUG line.

```lua:docs/manuals/examples/test-writer/actions.tslua
-- actions.tslua: Pre-test and Post-test Actions, and Equipment.
--
-- Demonstrates:
--   * a Pre-test Action runs before the first step; a Post-test Action runs after the
--     last one. Both are called with the run's current result as their first argument:
--     a Pre-test Action always sees "PASS", the Post-test one sees the final PASS or FAIL.
--   * ATE.addEquipment is legal in an Action, a step, or both; the report's Equipment
--     section comes from it. Declaring the same name and serial twice adds no second row.
--   * ATE.report and ATE.input are NOT legal in an Action: only Test Steps have a result
--     and an Operator prompt.
return ScheduleParams("Manual: Actions", "Pre-test and Post-test Actions")
  :setPreTestAction(function(passFail)
    ATE.log(Log.INFO, "pre-test action, run stands at " .. passFail)
    ATE.addEquipment("Bench power supply", "PSU-123", "GPIB::1", "2026-12-01", "0-60 V")
  end)
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action, run result was " .. passFail)
  end)
  :setSchedule({
    StepParams("Measure the rail"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "rail at 5.0 V", 5.0, 5.0)
      end),
  })
```

`ATE.log(level, ...)` writes to the run log; `print` is redirected to
`ATE.log(Log.INFO, ...)`. `ATE.run` is the read-only table of the current run:

```lua:docs/manuals/examples/test-writer/logging.tslua
-- logging.tslua: logging inside a Test Run, and reading the run's own context.
--
-- Demonstrates:
--   * ATE.log(level, ...) with the Log enum; every argument is turned to text and the
--     parts are joined with spaces;
--   * print is redirected to ATE.log(Log.INFO, ...), so a stray print is still recorded;
--   * the main screen shows INFO and above; the debug window shows every level;
--   * ATE.run is a read-only table of the current Test Run. Writing to it is an error.
--   * DEBUG device tracing is off unless the application config turns it on with
--     deviceTrace.enabled; every line a schedule writes is stored with the Test Run.
return ScheduleParams("Manual: logging", "log levels, print and ATE.run")
  :setSchedule({
    StepParams("One line at each level"):setStepNo("1.")
      :setAction(function()
        ATE.log(Log.DEBUG, "a detail only the debug window shows")
        ATE.log(Log.INFO, "the rail settled", 5.0, "V")
        ATE.log(Log.WARNING, "the probe is loose")
        ATE.log(Log.ERROR, "the supply did not come up")
        print("print lands at INFO")
        ATE.report(Result.PASS, "logged at every level")
      end),

    StepParams("Who is running"):setStepNo("2.")
      :setAction(function()
        ATE.log(Log.INFO, "station " .. ATE.run.Station
                             .. ", project " .. ATE.run.Project
                             .. ", UUT " .. ATE.run.UUT_serial
                             .. ", operator " .. ATE.run.Operator
                             .. ", schedule " .. ATE.run.Test_Schedule_Name
                             .. ", release " .. ATE.run.Media_Release_Media_Number)
        ATE.report(Result.PASS, "read the run context", ATE.run.Operator, "Ada Lovelace")
      end),
  })
```

## Sub-schedules

`:setInclude(file, keyvals)` expands the file's steps beneath the header row. The included file
gets `keyvals` as the chunk argument (`local kv = ...`), and ATE runs it again per include, so
each include is its own instance. `keyvals` must be plain data (strings, numbers, booleans,
nested tables of those). A top-level schedule, or an include with no keyvals, gets `{}`.

The file is resolved against the release that owns the including schedule, then its
dependencies depth first - the same order as `require`. Prefixes are prepended exactly as
written and stack outward-in. A header row is not a Test Step: it has no Result and is not
counted in the summary. `setEnable(false)` on a header forces the default of every step
beneath it, and an instance whose whole subtree is disabled has both its Actions skipped. The
included schedule's doc fields and its `setStopOnFail` are ignored; only its Actions and steps
take part.

```lua:docs/manuals/examples/test-writer/main.tslua
-- main.tslua: the top-level schedule that includes Sub-schedules.
--
-- Demonstrates:
--   * :setInclude(file, keyvals) makes a step a header row, not a Test Step: the steps of
--     the included schedule are pulled in beneath it.
--   * The same file can be included more than once; each include is its own instance with
--     its own keyvals.
--   * :setPrefix is prepended to the included steps' Step Numbers, as written, with no
--     separator, and prefixes stack as the includes nest.
--   * :setEnable on a header forces the default of every step beneath it.
return ScheduleParams("Manual: sub-schedules", "includes, keyvals, prefixes and nesting")
  :setDocTitle("Manual examples - Sub-schedules")
  :setDocId("MR-MANUAL-SUBS")
  :setDocRev("1.0")
  :setSchedule({
    StepParams("Rail present"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "the rail is fitted") end),

    -- 2.1. and 2.2. come from channel.tslua, included once for channel 1.
    StepParams("Channel 1"):setStepNo("2.")
      :setInclude("channel.tslua", { channel = 1, label = "left rail" }):setPrefix("2."),

    -- A disabled header: every step beneath it records NOTRUN, and its Actions are skipped.
    StepParams("Channel 2, not fitted"):setStepNo("3.")
      :setEnable(false)
      :setInclude("channel.tslua", { channel = 2, label = "right rail" }):setPrefix("3."),
  })
```

```lua:docs/manuals/examples/test-writer/channel.tslua
-- channel.tslua: a Sub-schedule. It is complete enough to run on its own, and is listed
-- as hidden in manual.atedef so the Operator never picks it directly.
--
-- Each include runs this file again, with the keyvals passed as the chunk's argument.
-- Its own Actions take part in the run's Pre-test and Post-test order.
local kv = ...
local channel = kv.channel or 1
local label = kv.label or "a channel"

return ScheduleParams("Channel check", "one output channel")
  :setPreTestAction(function()
    ATE.log(Log.INFO, "channel " .. channel .. " (" .. label .. ") pre-test")
  end)
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "channel " .. channel .. " post-test, run stands at " .. passFail)
  end)
  :setSchedule({
    StepParams("Switch the channel on"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "channel " .. channel .. " switches on")
      end),

    -- A Sub-schedule inside a Sub-schedule: step numbers become 2.2.1. and so on.
    StepParams("Its LEDs"):setStepNo("2.")
      :setInclude("led.tslua", { channel = channel }):setPrefix("2."),
  })
```

```lua:docs/manuals/examples/test-writer/led.tslua
-- led.tslua: the innermost Sub-schedule, included by channel.tslua.
local kv = ...
local channel = kv.channel or 1

return ScheduleParams("LED check", "the indicator LEDs of one channel")
  :setSchedule({
    StepParams("The LED lights"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "channel " .. channel .. " LED is lit")
      end),
  })
```

## Modules and the `.atedef`

`require` is ATE's own. It resolves the name against the release's files, then its
dependencies, never against `package.path`, which ATE empties. An unlisted name is an error. A
module runs once per Test Run and its return value is cached. `ate.` is reserved for modules
bundled with ATE (`ate.devices`).

Checksums are SHA-256 of the raw bytes, lowercase hex. A mismatch is a WARNING, the file still
loads, and both checksums are recorded with the run. `ate definition update <file.atedef>`
computes each listed file's SHA-256 and writes it back over the old value, filling in any
`sha256` that is absent; the file is only rewritten if a checksum actually changed, so a
correct `.atedef` comes back byte for byte the same. A listed file it cannot read keeps its
current value and warns.

Every field a `.atedef` can carry:

| Field | Meaning |
| --- | --- |
| `format` | must be `1`; anything else makes the definition broken |
| `mediaNumber` | the release's identity; not a version |
| `name`, `version` | `version` must be valid semver 2.0.0; other releases name it in `depends` |
| `depends` | a list of `{"name": ..., "version": ...}`; resolved depth first, in declaration order |
| `files[]` | one entry per Lua file: `path`, `sha256` |
| `files[].path` | relative to the `.atedef` (or absolute), forward slashes on both platforms |
| `files[].hidden` | only on a `.tslua`: keeps it off the Run screen while still allowing it as a Sub-schedule |
| `files[].module` | only on a `.lua`: the name `require` uses. Otherwise the path derives it: `lib/rails.lua` is `lib.rails`. An absolute path, or one containing `..`, must state it |

Here is the definition the manual's own examples ship, exactly as it sits on disk - note
`channel.tslua` and `led.tslua` marked `hidden`, and `modules/limits.lua` given the module name
`board_limits`:

```json:docs/manuals/examples/test-writer/manual.atedef
{
  "format": 1,
  "mediaNumber": "MR-MANUAL-TW",
  "name": "Test writer manual examples",
  "version": "1.0.0",
  "files": [
    {
      "path": "first.tslua",
      "sha256": "a260d2b923819ff6f71804370d4afce9ac77269ec9d9cf9c4b0dae35b85e48c8"
    },
    {
      "path": "steps.tslua",
      "sha256": "2203cddc55bf0daf0e557d3ecf1b847e77bf8fa873385cea775e32f240c5af97"
    },
    {
      "path": "inputs.tslua",
      "sha256": "6a746ebad482542f81d5b846b65a57adb467942e5289d9fc7e76b9bdaabd22fc"
    },
    {
      "path": "actions.tslua",
      "sha256": "a15e9a9fa060f0e829cc2e047d2c0313a1a2203c7c33356e792f699d1b02c526"
    },
    {
      "path": "retry.tslua",
      "sha256": "5375d9870ad11af1553eeaa1d5fd27ce586c2a5cce78af6b4a493a029fee58c9"
    },
    {
      "path": "results.tslua",
      "sha256": "49f80374f637c13e34281140912335fcb583fa87865674def089b18188130ce7"
    },
    {
      "path": "main.tslua",
      "sha256": "fc768ae110764fb3a7a9a4a29fdd93df3beafc09b8fff6ea7fa71efa825964d4"
    },
    {
      "path": "channel.tslua",
      "hidden": true,
      "sha256": "1cecd8b3566e4783c86375ebcdac6b69eaae52f1ea74e32c495babf7009b6e63"
    },
    {
      "path": "led.tslua",
      "hidden": true,
      "sha256": "3477e6a5a1578108bd76687e13e211ca1c48b5bac3343160a4e86319f87f46f8"
    },
    {
      "path": "modules.tslua",
      "sha256": "f34234441c454b9e0559e778fb86c6f10086f2edd4248207953ce67f22fae386"
    },
    {
      "path": "lib/rails.lua",
      "sha256": "392e385ab81dfb814a7ddec0cb1c0799c97d714b3d97c2cfb3306e6ba6cde029"
    },
    {
      "path": "modules/limits.lua",
      "module": "board_limits",
      "sha256": "ea8a21a8a7bef16b0abaa9efcc07c8f08416b4a07b712ac35b9ffdbf8c31fd99"
    },
    {
      "path": "tcp.tslua",
      "sha256": "8439beda05cc96f5c6bd69629951fc2318a7526eb085fb2bb322fc711c2cb8c7"
    },
    {
      "path": "udp.tslua",
      "sha256": "324ce6659ab74bcea159fbe20d063925e5b5a88992b2ad1755e4d68625f461b4"
    },
    {
      "path": "serial.tslua",
      "sha256": "dd717a441db74b490b84b999f82a54b7e0d2b3b3185906ae83150562c7dcb768"
    },
    {
      "path": "visa.tslua",
      "sha256": "5c9d69646277814d6d0e134380ba32e6402ce4d9d3f4006dbe4f622bb107afb8"
    },
    {
      "path": "errors.tslua",
      "sha256": "66151279fc9e62462e80a75e273785fac0bb1c8f2a7bf0aeec62f312a1fb0fe2"
    },
    {
      "path": "left_open.tslua",
      "sha256": "a53f424fe15813bafdcdf74f0c7eb4e69b37bbbb5fa27d833b58eb7d1923b279"
    },
    {
      "path": "logging.tslua",
      "sha256": "ee6b7ee6f1a3b1ed15522544d93645c694dfd29d27d53188416c8e956170dd02"
    },
    {
      "path": "abort.tslua",
      "sha256": "b164d0057d61fa7d5f4092a2b47154dc4ca16dfd685640ef09338f4b862eb308"
    },
    {
      "path": "sandbox.tslua",
      "sha256": "e40b7d8eafd167b5d52f844957962b505f53009d791a38bb2729413462aaee99"
    },
    {
      "path": "forbidden.tslua",
      "sha256": "eae7b5e968792a6abdcf883ccde1e956fdf4f7a6c14e2d8aab8ff02c1fc8dd97"
    }
  ]
}
```

```lua:docs/manuals/examples/test-writer/modules.tslua
-- modules.tslua: sharing code with require, including ATE's own bundled modules.
--
-- Demonstrates:
--   * require finds only the files the Test Definition lists (and its dependencies'),
--     never anything on package.path;
--   * the module name is derived from a plain relative path, or given explicitly with the
--     "module" field;
--   * the reserved "ate." namespace is ATE's own: require("ate.devices") is always ATE's
--     Device params builder, never a file in the Media Release.
local rails = require("lib.rails")      -- derived name: the path without .lua, / as .
local limits = require("board_limits")  -- explicit "module" name in manual.atedef
local devices = require("ate.devices")  -- bundled with ATE

return ScheduleParams("Manual: modules", "require and the ate. namespace")
  :setSchedule({
    StepParams("Check every rail"):setStepNo("1.")
      :setAction(function()
        local ok = true
        for channel = 1, 3 do
          local volts = rails.nominalVolts(channel)
          ok = ok and volts >= limits.low and volts <= limits.high
          ATE.log(Log.INFO, "channel " .. channel .. " nominal " .. volts .. " V")
        end
        ATE.report(ok and Result.PASS or Result.FAIL,
                   "every rail is inside " .. limits.low .. " to " .. limits.high .. " V")
      end),

    -- A builder is just a params table: nothing is opened, so no Device is involved.
    StepParams("Build params without opening"):setStepNo("2.")
      :setAction(function()
        local params = devices.Tcp("127.0.0.1", 5025):setTimeout(2.5)
        ATE.report(Result.PASS, "timeout set to " .. params.timeout, params.timeout, 2.5)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/lib/rails.lua
-- lib/rails.lua: an ordinary Lua module. Its module name is derived from its path:
-- "lib/rails.lua" becomes "lib.rails". require runs a module once per Test Run and
-- caches what it returns; later requires of the same name get the cached value.
local rails = {}

function rails.nominalVolts(channel)
  return 1.5 * channel
end

return rails
```

```lua:docs/manuals/examples/test-writer/modules/limits.lua
-- modules/limits.lua: a module whose name is given explicitly in manual.atedef with the
-- "module" field, because a name derived from the path would be "modules.limits".
return { low = 1.0, high = 5.0 }
```

## Devices

`ATE.open(deviceType, params [, onError])` returns an opaque handle. Every field `open` needs
is required - there are no defaults in `ATE.open`; the builders supply them.

| Device Type | `open` fields | builder (defaults) |
| --- | --- | --- |
| `DeviceType.TCP` | `host`, `port`, `timeout`, `readTerm`, `writeTerm` | `devices.Tcp(host, port)`: timeout 1.0, terminators `"\n"` |
| `DeviceType.SERIAL` | `port`, `baud`, `dataBits`, `stopBits`, `parity`, `flowControl`, `timeout`, `readTerm`, `writeTerm` | `devices.Serial(port)`: 9600, 8, 1, `Parity.NONE`, `FlowControl.NONE`, 1.0, terminators `"\n"` |
| `DeviceType.UDP` | `host`, `port`, `localPort`, `timeout` | `devices.Udp(host, port)`: `localPort` 0, timeout 1.0 |
| `DeviceType.VISA` | `resource`, `timeout`, `readTerm`, `writeTerm` | `devices.Visa(resource)`: timeout 1.0, terminators `"\n"` |

Per-call fields, all optional, falling back to the open-time configuration: `read` takes
`size`, `timeout`, `readTerm`; `write` takes `data` (required), `writeTerm`, `timeout`;
`close` takes no fields of its own but accepts an optional params table and an optional
`onError` — `ATE.close(dev [, params] [, onError])` — which is used if the close itself fails.
UDP has no terminators: its `read` takes `size` and `timeout`, and its `write` takes `data`
(required) and `timeout`. Passing a `readTerm` to a UDP Device is an error naming the field.
Builder setters: `setHost`, `setPort`, `setLocalPort`, `setResource`, `setBaud`, `setDataBits`,
`setStopBits`, `setParity`, `setFlowControl`, `setTimeout`, `setReadTerm`, `setWriteTerm`,
`setSize`, and `devices.Write(data)`.

Reads and writes are raw bytes. The terminator is stripped from `data`; `count` is what is
left. A write's `count` includes the terminator.

TCP, SERIAL and UDP are read in the background from `open` to `close` and buffered, capped at
the Station config key `devices.receiveBufferBytes` (16777216, 16 MiB); on overflow ATE drops
the oldest bytes, logs one WARNING, and the next read carries `overflow = true`. `ATE.flush(dev)`
empties the buffer and returns the count discarded. VISA has no background reader and no buffer,
so `flush` discards nothing.

A VISA resource string names the instrument, and the shape matters:
`GPIB0::5::INSTR` is a GPIB instrument, `TCPIP0::192.168.1.10::inst0::INSTR` is a VXI-11
instrument, and `TCPIP0::127.0.0.1::5025::SOCKET` is a plain TCP socket wrapped in a VISA
session - the same stream a `DeviceType.TCP` Device would open, with pyvisa in the way. ATE
uses whichever pyvisa backend it finds; only pyvisa-py is shipped, which reaches TCPIP and
USBTMC. A GPIB instrument needs a real VISA driver on the Station (NI-VISA or linux-GPIB): ask
the schedule author which resource string their instrument needs before writing the schedule,
because nothing installed with ATE can open a GPIB address.

```lua:docs/manuals/examples/test-writer/tcp.tslua
-- tcp.tslua: a TCP Device, from open to close.
--
-- The host and port come from the environment, which the test harness sets to a
-- simulator peer listening on 127.0.0.1. Point them at a real instrument to use this
-- schedule as it is.
--
-- Demonstrates:
--   * the ate.devices builders, whose setters chain, and how a Device opened in the
--     Pre-test Action is still open in the steps, because a device object lives for the
--     whole Test Run;
--   * per-call overrides (timeout, readTerm, size) that do not change the open-time
--     configuration;
--   * a read that times out after some bytes is a partial read, not a Device failure;
--   * a read that times out with nothing at all is a Device failure and goes through
--     onError, which is given the failure table and the params table and may return a
--     value for the call to carry on with;
--   * flush discards whatever the receive buffer already holds.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

local instrument = nil

return ScheduleParams("Manual: TCP", "open, write, read, flush and close a TCP Device")
  :setStopOnFail(false)
  :setPreTestAction(function()
    -- devices.Tcp(host, port) fills in every field TCP needs: timeout 1.0 s,
    -- readTerm and writeTerm "\n". open has no defaults of its own, so a plain table
    -- would have to name them all.
    instrument = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(2.0))
    ATE.addEquipment("SCPI simulator", "SIM-TCP", "TCP " .. host .. ":" .. port, "2026-01-01")
  end)
  :setPostTestAction(function(passFail)
    ATE.close(instrument)   -- closing a second time does nothing and still reports ok
  end)
  :setSchedule({
    StepParams("Identify the instrument"):setStepNo("1.")
      :setAction(function()
        local sent = ATE.write(instrument, devices.Write("*IDN?"))
        local reply = ATE.read(instrument, devices.Read():setTimeout(1.0))
        ATE.report((sent.ok and reply.count > 0) and Result.PASS or Result.FAIL,
                   "reply: " .. (reply.data or "nothing"),
                   reply.count, 26)
      end),

    -- The peer answers "MEAS:VOLT?" with "1.234\n". readTerm "3" stops the read at the "3",
    -- so the first read takes "1.2" and the second takes the rest of the reply.
    StepParams("Stop a read at a terminator"):setStepNo("2.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MEAS:VOLT?"))
        local first = ATE.read(instrument, devices.Read():setReadTerm("3"))
        local rest = ATE.read(instrument)
        ATE.report((first.data == "1.2" and rest.data == "4") and Result.PASS or Result.FAIL,
                   "first read " .. (first.data or "nothing") .. ", second " .. (rest.data or "nothing"),
                   first.count + rest.count, 4)
      end),

    -- "PARTIAL?" is answered with bytes and no terminator: the read times out having taken
    -- some of them. That is partial = true, a WARNING in the log, and not a Device failure.
    StepParams("A read that times out partway"):setStepNo("3.")
      :setAction(function()
        ATE.write(instrument, devices.Write("PARTIAL?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3))
        ATE.report((r.partial and r.count > 0) and Result.PASS or Result.FAIL,
                   "got " .. r.count .. " bytes before the timeout",
                   tostring(r.partial), "true")
      end),

    -- "NEVER?" is answered with silence: the read times out with no bytes at all, which IS
    -- a Device failure, so onError runs. Returning a value makes the call continue with it.
    StepParams("Handle a read that times out"):setStepNo("4.")
      :setAction(function()
        ATE.write(instrument, devices.Write("NEVER?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3), function(opResult, opArgs)
          ATE.log(Log.WARNING, "no reply from the instrument: " .. opResult.err)
          return { data = nil, count = 0, partial = false }
        end)
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "the timeout was handled, so the step carried on")
      end),

    -- The peer sends three lines; the schedule reads the first and throws away the rest.
    StepParams("Discard the receive buffer"):setStepNo("5.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MULTI?"))
        ATE.read(instrument, devices.Read():setSize(7))
        local f = ATE.flush(instrument)
        ATE.report((f.ok and f.count > 0) and Result.PASS or Result.FAIL, "discarded " .. f.count .. " buffered bytes")
      end),
  })
```

```lua:docs/manuals/examples/test-writer/udp.tslua
-- udp.tslua: a UDP Device, where one datagram is one message.
--
-- Demonstrates:
--   * UDP has no terminators, so readTerm and writeTerm are not fields this Device Type
--     knows; passing one is a schedule bug that raises naming the field;
--   * a datagram longer than the size asked for is truncated and returned with partial =
--     true, which the log calls a truncation rather than a timeout;
--   * the receive buffer is a queue of whole datagrams, so a reply that arrives before the
--     schedule reads it is not lost.
local devices = require("ate.devices")

local host = os.getenv("ATE_UDP_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_UDP_PORT")) or 5026

return ScheduleParams("Manual: UDP", "send and read datagrams")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Echo one datagram"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("PING"))
        local reply = ATE.read(dev)
        ATE.report(reply.data == "ECHO:PING" and Result.PASS or Result.FAIL,
                   "the peer echoed " .. (reply.data or "nothing"),
                   reply.count, 9)
        ATE.close(dev)
      end),

    -- Ask for 10 bytes of a 40-byte datagram: ATE returns the first 10 and says partial.
    StepParams("Truncate a datagram"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("a datagram longer than the size asked for"))
        local r = ATE.read(dev, devices.Read():setSize(10))
        ATE.report((r.count == 10 and r.partial) and Result.PASS or Result.FAIL,
                   "read " .. r.count .. " of a longer datagram",
                   r.count, 10)
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/serial.tslua
-- serial.tslua: a SERIAL Device through pyserial.
--
-- The port here is pyserial's socket:// URL, which opens a TCP connection to the
-- simulator peer; a real station names a COM or tty port instead.
--
-- Demonstrates:
--   * open has no defaults, so every field is named;
--   * the Parity and FlowControl enums;
--   * the same connection read with the Device's own terminator.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: SERIAL", "open a serial port and read a reply")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Open the port"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.SERIAL, {
          port = "socket://" .. host .. ":" .. port,
          baud = 115200,
          dataBits = 8,
          stopBits = 1,
          parity = Parity.NONE,
          flowControl = FlowControl.NONE,
          timeout = 2.0,
          readTerm = "\n",
          writeTerm = "\n",
        })
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "serial reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/visa.tslua
-- visa.tslua: a VISA instrument through pyvisa.
--
-- VISA has no background reader: ATE reads the instrument only when the schedule asks,
-- because polling an SCPI instrument with reads that have nothing pending makes it log
-- "Query UNTERMINATED" (-420) and ties up the bus. ATE ships pyvisa-py; a VISA resource
-- string names the instrument, here the simulator peer as a plain TCPIP socket.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: VISA", "read an instrument through pyvisa")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Ask the instrument who it is"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.VISA,
          devices.Visa("TCPIP0::" .. host .. "::" .. port .. "::SOCKET"):setTimeout(2.0))
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        local f = ATE.flush(dev)   -- VISA has no receive buffer: this discards nothing
        ATE.log(Log.INFO, "flush discarded " .. f.count .. " bytes")
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "VISA reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

A handle lives for the whole run. Closing a closed Device does nothing and reports success;
using one is a failure through `onError`. At the end of every run ATE closes whatever is still
open and logs a WARNING for each.

```lua:docs/manuals/examples/test-writer/left_open.tslua
-- left_open.tslua: a Device the schedule never closes.
--
-- A Test Run closes every Device still open when it ends, and logs one WARNING naming
-- each one. That is why a Device opened in a Pre-test Action does not have to be closed
-- by hand - it is worth closing anyway, so the log stays clean.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: a Device left open", "ATE closes what the schedule forgets")
  :setSchedule({
    StepParams("Open and forget"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.report(Result.PASS, "opened a Device and left it open")
      end),
  })
```

## Device failures, Stop on Fail, Retry, Abort

`onError(opResult, opArgs)` is called when the operation fails. If it returns anything but
`nil`, the call returns that value and the schedule continues. If it returns `nil`, or there is
no handler, the call raises a Lua error and the step records `Result.ERROR`. A read that times
out having received some bytes is not a failure: it returns `partial = true` and logs a
WARNING. ATE never reconnects: close the Device and open it again with the same params.

A field the Device Type does not know, a missing required field, or a wrong-typed value is not a
Device failure. It never reaches `onError`; the call returns `false` with the message as its
second result and the traceback is logged at ERROR.

```lua:docs/manuals/examples/test-writer/errors.tslua
-- errors.tslua: how Device failures reach the schedule, and what does not.
--
-- Demonstrates:
--   * a Device failure with no onError raises a Lua error, which ends the step as
--     Result.ERROR;
--   * an onError that returns a value makes the call continue with that value;
--   * a bad argument (a misspelled field) is a schedule bug, not a Device failure: it
--     never reaches onError, it is logged at ERROR, and the call returns false with the
--     message as its second result;
--   * the handler given to open is the fallback for that Device's read, write and close.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025
local deadPort = tonumber(os.getenv("ATE_DEAD_PORT")) or 1

return ScheduleParams("Manual: Device errors", "onError, a closed Device and a bad field")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A misspelled field"):setStepNo("1.")
      :setAction(function()
        local opened, why = ATE.open(DeviceType.TCP, { host = host, port = port, timeout = 1.0,
                                                     readTerm = "\n", writeTerm = "\n", buad = 9600 })
        ATE.report(opened == false and Result.PASS or Result.FAIL, "the open was refused: " .. why)
      end),

    -- Nothing is listening on deadPort: the open fails. This handler logs and returns nil,
    -- which means "no recovery", so ATE.open raises and the step records Result.ERROR.
    StepParams("A port nothing listens on"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, deadPort):setTimeout(0.5),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "nothing is listening on " .. opArgs.host .. ":" .. opArgs.port)
            return nil
          end)
        ATE.report(Result.PASS, "this line is never reached")
      end),

    -- A Device that is closed cannot be read. With no handler the call raises.
    StepParams("Read a closed Device"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.close(dev)
        ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(Result.PASS, "this line is never reached either")
      end),

    -- The same failure, handled: the step carries on with what onError returns.
    StepParams("Handle the closed Device"):setStepNo("4.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "the Device is gone: " .. opResult.err)
            return { data = nil, count = 0, partial = false }
          end)
        ATE.close(dev)
        local r = ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "recovered from reading a closed Device")
      end),
  })
```

Stop on Fail pauses the run at a `FAIL` or `ERROR` step so the Operator can choose `CONTINUE`,
`RETRY` or `ABORT`. Retry runs the action again from the start with POLL reset and blocking
inputs cleared; each Attempt is recorded and the step's result is the last Attempt's. There is
no Attempt number in Lua - keep your own count.

```lua:docs/manuals/examples/test-writer/retry.tslua
-- retry.tslua: Stop on Fail, and what a Retry does.
--
-- Demonstrates:
--   * setStopOnFail(true) is the default: a step ending FAIL or ERROR pauses the run and
--     the Operator chooses Continue, Retry or Abort.
--   * Retry runs the step's action again from the beginning. Each Attempt is recorded;
--     the step's result is its last Attempt's, and the Operator's choice is recorded too.
--   * ATE gives a step no way to read its own Attempt number, so the schedule keeps the
--     count itself in a local the action closes over.
local attempts = 0

return ScheduleParams("Manual: retry", "the first Attempt fails, the Retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Retry this one"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt " .. attempts .. ": the joint is cold", 0.2, 2.4)
        else
          ATE.report(Result.PASS, "attempt " .. attempts .. ": the joint is warm", 2.4, 2.4)
        end
      end),

    -- Only reached after the step passed, or after the Operator chose to continue.
    StepParams("Next check"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "the rail is steady", 5.0, 5.0)
      end),
  })
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

`ATE.log(level, ...)` writes to the run log; `print` is redirected to
`ATE.log(Log.INFO, ...)`. `ATE.run` is the read-only table of the current run:

```lua:docs/manuals/examples/test-writer/logging.tslua
-- logging.tslua: logging inside a Test Run, and reading the run's own context.
--
-- Demonstrates:
--   * ATE.log(level, ...) with the Log enum; every argument is turned to text and the
--     parts are joined with spaces;
--   * print is redirected to ATE.log(Log.INFO, ...), so a stray print is still recorded;
--   * the main screen shows INFO and above; the debug window shows every level;
--   * ATE.run is a read-only table of the current Test Run. Writing to it is an error.
--   * DEBUG device tracing is off unless the application config turns it on with
--     deviceTrace.enabled; every line a schedule writes is stored with the Test Run.
return ScheduleParams("Manual: logging", "log levels, print and ATE.run")
  :setSchedule({
    StepParams("One line at each level"):setStepNo("1.")
      :setAction(function()
        ATE.log(Log.DEBUG, "a detail only the debug window shows")
        ATE.log(Log.INFO, "the rail settled", 5.0, "V")
        ATE.log(Log.WARNING, "the probe is loose")
        ATE.log(Log.ERROR, "the supply did not come up")
        print("print lands at INFO")
        ATE.report(Result.PASS, "logged at every level")
      end),

    StepParams("Who is running"):setStepNo("2.")
      :setAction(function()
        ATE.log(Log.INFO, "station " .. ATE.run.Station
                             .. ", project " .. ATE.run.Project
                             .. ", UUT " .. ATE.run.UUT_serial
                             .. ", operator " .. ATE.run.Operator
                             .. ", schedule " .. ATE.run.Test_Schedule_Name
                             .. ", release " .. ATE.run.Media_Release_Media_Number)
        ATE.report(Result.PASS, "read the run context", ATE.run.Operator, "Ada Lovelace")
      end),
  })
```

## Sub-schedules

`:setInclude(file, keyvals)` expands the file's steps beneath the header row. The included file
gets `keyvals` as the chunk argument (`local kv = ...`), and ATE runs it again per include, so
each include is its own instance. `keyvals` must be plain data (strings, numbers, booleans,
nested tables of those). A top-level schedule, or an include with no keyvals, gets `{}`.

The file is resolved against the release that owns the including schedule, then its
dependencies depth first - the same order as `require`. Prefixes are prepended exactly as
written and stack outward-in. A header row is not a Test Step: it has no Result and is not
counted in the summary. `setEnable(false)` on a header forces the default of every step
beneath it, and an instance whose whole subtree is disabled has both its Actions skipped. The
included schedule's doc fields and its `setStopOnFail` are ignored; only its Actions and steps
take part.

```lua:docs/manuals/examples/test-writer/main.tslua
-- main.tslua: the top-level schedule that includes Sub-schedules.
--
-- Demonstrates:
--   * :setInclude(file, keyvals) makes a step a header row, not a Test Step: the steps of
--     the included schedule are pulled in beneath it.
--   * The same file can be included more than once; each include is its own instance with
--     its own keyvals.
--   * :setPrefix is prepended to the included steps' Step Numbers, as written, with no
--     separator, and prefixes stack as the includes nest.
--   * :setEnable on a header forces the default of every step beneath it.
return ScheduleParams("Manual: sub-schedules", "includes, keyvals, prefixes and nesting")
  :setDocTitle("Manual examples - Sub-schedules")
  :setDocId("MR-MANUAL-SUBS")
  :setDocRev("1.0")
  :setSchedule({
    StepParams("Rail present"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "the rail is fitted") end),

    -- 2.1. and 2.2. come from channel.tslua, included once for channel 1.
    StepParams("Channel 1"):setStepNo("2.")
      :setInclude("channel.tslua", { channel = 1, label = "left rail" }):setPrefix("2."),

    -- A disabled header: every step beneath it records NOTRUN, and its Actions are skipped.
    StepParams("Channel 2, not fitted"):setStepNo("3.")
      :setEnable(false)
      :setInclude("channel.tslua", { channel = 2, label = "right rail" }):setPrefix("3."),
  })
```

```lua:docs/manuals/examples/test-writer/channel.tslua
-- channel.tslua: a Sub-schedule. It is complete enough to run on its own, and is listed
-- as hidden in manual.atedef so the Operator never picks it directly.
--
-- Each include runs this file again, with the keyvals passed as the chunk's argument.
-- Its own Actions take part in the run's Pre-test and Post-test order.
local kv = ...
local channel = kv.channel or 1
local label = kv.label or "a channel"

return ScheduleParams("Channel check", "one output channel")
  :setPreTestAction(function()
    ATE.log(Log.INFO, "channel " .. channel .. " (" .. label .. ") pre-test")
  end)
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "channel " .. channel .. " post-test, run stands at " .. passFail)
  end)
  :setSchedule({
    StepParams("Switch the channel on"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "channel " .. channel .. " switches on")
      end),

    -- A Sub-schedule inside a Sub-schedule: step numbers become 2.2.1. and so on.
    StepParams("Its LEDs"):setStepNo("2.")
      :setInclude("led.tslua", { channel = channel }):setPrefix("2."),
  })
```

```lua:docs/manuals/examples/test-writer/led.tslua
-- led.tslua: the innermost Sub-schedule, included by channel.tslua.
local kv = ...
local channel = kv.channel or 1

return ScheduleParams("LED check", "the indicator LEDs of one channel")
  :setSchedule({
    StepParams("The LED lights"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "channel " .. channel .. " LED is lit")
      end),
  })
```

## Modules and the `.atedef`

`require` is ATE's own. It resolves the name against the release's files, then its
dependencies, never against `package.path`, which ATE empties. An unlisted name is an error. A
module runs once per Test Run and its return value is cached. `ate.` is reserved for modules
bundled with ATE (`ate.devices`).

Checksums are SHA-256 of the raw bytes, lowercase hex. A mismatch is a WARNING, the file still
loads, and both checksums are recorded with the run. `ate definition update <file.atedef>`
computes each listed file's SHA-256 and writes it back over the old value, filling in any
`sha256` that is absent; the file is only rewritten if a checksum actually changed, so a
correct `.atedef` comes back byte for byte the same. A listed file it cannot read keeps its
current value and warns.

Every field a `.atedef` can carry:

| Field | Meaning |
| --- | --- |
| `format` | must be `1`; anything else makes the definition broken |
| `mediaNumber` | the release's identity; not a version |
| `name`, `version` | `version` must be valid semver 2.0.0; other releases name it in `depends` |
| `depends` | a list of `{"name": ..., "version": ...}`; resolved depth first, in declaration order |
| `files[]` | one entry per Lua file: `path`, `sha256` |
| `files[].path` | relative to the `.atedef` (or absolute), forward slashes on both platforms |
| `files[].hidden` | only on a `.tslua`: keeps it off the Run screen while still allowing it as a Sub-schedule |
| `files[].module` | only on a `.lua`: the name `require` uses. Otherwise the path derives it: `lib/rails.lua` is `lib.rails`. An absolute path, or one containing `..`, must state it |

Here is the definition the manual's own examples ship, exactly as it sits on disk - note
`channel.tslua` and `led.tslua` marked `hidden`, and `modules/limits.lua` given the module name
`board_limits`:

```json:docs/manuals/examples/test-writer/manual.atedef
{
  "format": 1,
  "mediaNumber": "MR-MANUAL-TW",
  "name": "Test writer manual examples",
  "version": "1.0.0",
  "files": [
    {
      "path": "first.tslua",
      "sha256": "a260d2b923819ff6f71804370d4afce9ac77269ec9d9cf9c4b0dae35b85e48c8"
    },
    {
      "path": "steps.tslua",
      "sha256": "2203cddc55bf0daf0e557d3ecf1b847e77bf8fa873385cea775e32f240c5af97"
    },
    {
      "path": "inputs.tslua",
      "sha256": "6a746ebad482542f81d5b846b65a57adb467942e5289d9fc7e76b9bdaabd22fc"
    },
    {
      "path": "actions.tslua",
      "sha256": "a15e9a9fa060f0e829cc2e047d2c0313a1a2203c7c33356e792f699d1b02c526"
    },
    {
      "path": "retry.tslua",
      "sha256": "5375d9870ad11af1553eeaa1d5fd27ce586c2a5cce78af6b4a493a029fee58c9"
    },
    {
      "path": "results.tslua",
      "sha256": "49f80374f637c13e34281140912335fcb583fa87865674def089b18188130ce7"
    },
    {
      "path": "main.tslua",
      "sha256": "fc768ae110764fb3a7a9a4a29fdd93df3beafc09b8fff6ea7fa71efa825964d4"
    },
    {
      "path": "channel.tslua",
      "hidden": true,
      "sha256": "1cecd8b3566e4783c86375ebcdac6b69eaae52f1ea74e32c495babf7009b6e63"
    },
    {
      "path": "led.tslua",
      "hidden": true,
      "sha256": "3477e6a5a1578108bd76687e13e211ca1c48b5bac3343160a4e86319f87f46f8"
    },
    {
      "path": "modules.tslua",
      "sha256": "f34234441c454b9e0559e778fb86c6f10086f2edd4248207953ce67f22fae386"
    },
    {
      "path": "lib/rails.lua",
      "sha256": "392e385ab81dfb814a7ddec0cb1c0799c97d714b3d97c2cfb3306e6ba6cde029"
    },
    {
      "path": "modules/limits.lua",
      "module": "board_limits",
      "sha256": "ea8a21a8a7bef16b0abaa9efcc07c8f08416b4a07b712ac35b9ffdbf8c31fd99"
    },
    {
      "path": "tcp.tslua",
      "sha256": "8439beda05cc96f5c6bd69629951fc2318a7526eb085fb2bb322fc711c2cb8c7"
    },
    {
      "path": "udp.tslua",
      "sha256": "324ce6659ab74bcea159fbe20d063925e5b5a88992b2ad1755e4d68625f461b4"
    },
    {
      "path": "serial.tslua",
      "sha256": "dd717a441db74b490b84b999f82a54b7e0d2b3b3185906ae83150562c7dcb768"
    },
    {
      "path": "visa.tslua",
      "sha256": "5c9d69646277814d6d0e134380ba32e6402ce4d9d3f4006dbe4f622bb107afb8"
    },
    {
      "path": "errors.tslua",
      "sha256": "66151279fc9e62462e80a75e273785fac0bb1c8f2a7bf0aeec62f312a1fb0fe2"
    },
    {
      "path": "left_open.tslua",
      "sha256": "a53f424fe15813bafdcdf74f0c7eb4e69b37bbbb5fa27d833b58eb7d1923b279"
    },
    {
      "path": "logging.tslua",
      "sha256": "ee6b7ee6f1a3b1ed15522544d93645c694dfd29d27d53188416c8e956170dd02"
    },
    {
      "path": "abort.tslua",
      "sha256": "b164d0057d61fa7d5f4092a2b47154dc4ca16dfd685640ef09338f4b862eb308"
    },
    {
      "path": "sandbox.tslua",
      "sha256": "e40b7d8eafd167b5d52f844957962b505f53009d791a38bb2729413462aaee99"
    },
    {
      "path": "forbidden.tslua",
      "sha256": "eae7b5e968792a6abdcf883ccde1e956fdf4f7a6c14e2d8aab8ff02c1fc8dd97"
    }
  ]
}
```

```lua:docs/manuals/examples/test-writer/modules.tslua
-- modules.tslua: sharing code with require, including ATE's own bundled modules.
--
-- Demonstrates:
--   * require finds only the files the Test Definition lists (and its dependencies'),
--     never anything on package.path;
--   * the module name is derived from a plain relative path, or given explicitly with the
--     "module" field;
--   * the reserved "ate." namespace is ATE's own: require("ate.devices") is always ATE's
--     Device params builder, never a file in the Media Release.
local rails = require("lib.rails")      -- derived name: the path without .lua, / as .
local limits = require("board_limits")  -- explicit "module" name in manual.atedef
local devices = require("ate.devices")  -- bundled with ATE

return ScheduleParams("Manual: modules", "require and the ate. namespace")
  :setSchedule({
    StepParams("Check every rail"):setStepNo("1.")
      :setAction(function()
        local ok = true
        for channel = 1, 3 do
          local volts = rails.nominalVolts(channel)
          ok = ok and volts >= limits.low and volts <= limits.high
          ATE.log(Log.INFO, "channel " .. channel .. " nominal " .. volts .. " V")
        end
        ATE.report(ok and Result.PASS or Result.FAIL,
                   "every rail is inside " .. limits.low .. " to " .. limits.high .. " V")
      end),

    -- A builder is just a params table: nothing is opened, so no Device is involved.
    StepParams("Build params without opening"):setStepNo("2.")
      :setAction(function()
        local params = devices.Tcp("127.0.0.1", 5025):setTimeout(2.5)
        ATE.report(Result.PASS, "timeout set to " .. params.timeout, params.timeout, 2.5)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/lib/rails.lua
-- lib/rails.lua: an ordinary Lua module. Its module name is derived from its path:
-- "lib/rails.lua" becomes "lib.rails". require runs a module once per Test Run and
-- caches what it returns; later requires of the same name get the cached value.
local rails = {}

function rails.nominalVolts(channel)
  return 1.5 * channel
end

return rails
```

```lua:docs/manuals/examples/test-writer/modules/limits.lua
-- modules/limits.lua: a module whose name is given explicitly in manual.atedef with the
-- "module" field, because a name derived from the path would be "modules.limits".
return { low = 1.0, high = 5.0 }
```

## Devices

`ATE.open(deviceType, params [, onError])` returns an opaque handle. Every field `open` needs
is required - there are no defaults in `ATE.open`; the builders supply them.

| Device Type | `open` fields | builder (defaults) |
| --- | --- | --- |
| `DeviceType.TCP` | `host`, `port`, `timeout`, `readTerm`, `writeTerm` | `devices.Tcp(host, port)`: timeout 1.0, terminators `"\n"` |
| `DeviceType.SERIAL` | `port`, `baud`, `dataBits`, `stopBits`, `parity`, `flowControl`, `timeout`, `readTerm`, `writeTerm` | `devices.Serial(port)`: 9600, 8, 1, `Parity.NONE`, `FlowControl.NONE`, 1.0, terminators `"\n"` |
| `DeviceType.UDP` | `host`, `port`, `localPort`, `timeout` | `devices.Udp(host, port)`: `localPort` 0, timeout 1.0 |
| `DeviceType.VISA` | `resource`, `timeout`, `readTerm`, `writeTerm` | `devices.Visa(resource)`: timeout 1.0, terminators `"\n"` |

Per-call fields, all optional, falling back to the open-time configuration: `read` takes
`size`, `timeout`, `readTerm`; `write` takes `data` (required), `writeTerm`, `timeout`;
`close` takes no fields of its own but accepts an optional params table and an optional
`onError` — `ATE.close(dev [, params] [, onError])` — which is used if the close itself fails.
UDP has no terminators: its `read` takes `size` and `timeout`, and its `write` takes `data`
(required) and `timeout`. Passing a `readTerm` to a UDP Device is an error naming the field.
Builder setters: `setHost`, `setPort`, `setLocalPort`, `setResource`, `setBaud`, `setDataBits`,
`setStopBits`, `setParity`, `setFlowControl`, `setTimeout`, `setReadTerm`, `setWriteTerm`,
`setSize`, and `devices.Write(data)`.

Reads and writes are raw bytes. The terminator is stripped from `data`; `count` is what is
left. A write's `count` includes the terminator.

TCP, SERIAL and UDP are read in the background from `open` to `close` and buffered, capped at
the Station config key `devices.receiveBufferBytes` (16777216, 16 MiB); on overflow ATE drops
the oldest bytes, logs one WARNING, and the next read carries `overflow = true`. `ATE.flush(dev)`
empties the buffer and returns the count discarded. VISA has no background reader and no buffer,
so `flush` discards nothing.

A VISA resource string names the instrument, and the shape matters:
`GPIB0::5::INSTR` is a GPIB instrument, `TCPIP0::192.168.1.10::inst0::INSTR` is a VXI-11
instrument, and `TCPIP0::127.0.0.1::5025::SOCKET` is a plain TCP socket wrapped in a VISA
session - the same stream a `DeviceType.TCP` Device would open, with pyvisa in the way. ATE
uses whichever pyvisa backend it finds; only pyvisa-py is shipped, which reaches TCPIP and
USBTMC. A GPIB instrument needs a real VISA driver on the Station (NI-VISA or linux-GPIB): ask
the schedule author which resource string their instrument needs before writing the schedule,
because nothing installed with ATE can open a GPIB address.

```lua:docs/manuals/examples/test-writer/tcp.tslua
-- tcp.tslua: a TCP Device, from open to close.
--
-- The host and port come from the environment, which the test harness sets to a
-- simulator peer listening on 127.0.0.1. Point them at a real instrument to use this
-- schedule as it is.
--
-- Demonstrates:
--   * the ate.devices builders, whose setters chain, and how a Device opened in the
--     Pre-test Action is still open in the steps, because a device object lives for the
--     whole Test Run;
--   * per-call overrides (timeout, readTerm, size) that do not change the open-time
--     configuration;
--   * a read that times out after some bytes is a partial read, not a Device failure;
--   * a read that times out with nothing at all is a Device failure and goes through
--     onError, which is given the failure table and the params table and may return a
--     value for the call to carry on with;
--   * flush discards whatever the receive buffer already holds.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

local instrument = nil

return ScheduleParams("Manual: TCP", "open, write, read, flush and close a TCP Device")
  :setStopOnFail(false)
  :setPreTestAction(function()
    -- devices.Tcp(host, port) fills in every field TCP needs: timeout 1.0 s,
    -- readTerm and writeTerm "\n". open has no defaults of its own, so a plain table
    -- would have to name them all.
    instrument = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(2.0))
    ATE.addEquipment("SCPI simulator", "SIM-TCP", "TCP " .. host .. ":" .. port, "2026-01-01")
  end)
  :setPostTestAction(function(passFail)
    ATE.close(instrument)   -- closing a second time does nothing and still reports ok
  end)
  :setSchedule({
    StepParams("Identify the instrument"):setStepNo("1.")
      :setAction(function()
        local sent = ATE.write(instrument, devices.Write("*IDN?"))
        local reply = ATE.read(instrument, devices.Read():setTimeout(1.0))
        ATE.report((sent.ok and reply.count > 0) and Result.PASS or Result.FAIL,
                   "reply: " .. (reply.data or "nothing"),
                   reply.count, 26)
      end),

    -- The peer answers "MEAS:VOLT?" with "1.234\n". readTerm "3" stops the read at the "3",
    -- so the first read takes "1.2" and the second takes the rest of the reply.
    StepParams("Stop a read at a terminator"):setStepNo("2.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MEAS:VOLT?"))
        local first = ATE.read(instrument, devices.Read():setReadTerm("3"))
        local rest = ATE.read(instrument)
        ATE.report((first.data == "1.2" and rest.data == "4") and Result.PASS or Result.FAIL,
                   "first read " .. (first.data or "nothing") .. ", second " .. (rest.data or "nothing"),
                   first.count + rest.count, 4)
      end),

    -- "PARTIAL?" is answered with bytes and no terminator: the read times out having taken
    -- some of them. That is partial = true, a WARNING in the log, and not a Device failure.
    StepParams("A read that times out partway"):setStepNo("3.")
      :setAction(function()
        ATE.write(instrument, devices.Write("PARTIAL?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3))
        ATE.report((r.partial and r.count > 0) and Result.PASS or Result.FAIL,
                   "got " .. r.count .. " bytes before the timeout",
                   tostring(r.partial), "true")
      end),

    -- "NEVER?" is answered with silence: the read times out with no bytes at all, which IS
    -- a Device failure, so onError runs. Returning a value makes the call continue with it.
    StepParams("Handle a read that times out"):setStepNo("4.")
      :setAction(function()
        ATE.write(instrument, devices.Write("NEVER?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3), function(opResult, opArgs)
          ATE.log(Log.WARNING, "no reply from the instrument: " .. opResult.err)
          return { data = nil, count = 0, partial = false }
        end)
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "the timeout was handled, so the step carried on")
      end),

    -- The peer sends three lines; the schedule reads the first and throws away the rest.
    StepParams("Discard the receive buffer"):setStepNo("5.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MULTI?"))
        ATE.read(instrument, devices.Read():setSize(7))
        local f = ATE.flush(instrument)
        ATE.report((f.ok and f.count > 0) and Result.PASS or Result.FAIL, "discarded " .. f.count .. " buffered bytes")
      end),
  })
```

```lua:docs/manuals/examples/test-writer/udp.tslua
-- udp.tslua: a UDP Device, where one datagram is one message.
--
-- Demonstrates:
--   * UDP has no terminators, so readTerm and writeTerm are not fields this Device Type
--     knows; passing one is a schedule bug that raises naming the field;
--   * a datagram longer than the size asked for is truncated and returned with partial =
--     true, which the log calls a truncation rather than a timeout;
--   * the receive buffer is a queue of whole datagrams, so a reply that arrives before the
--     schedule reads it is not lost.
local devices = require("ate.devices")

local host = os.getenv("ATE_UDP_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_UDP_PORT")) or 5026

return ScheduleParams("Manual: UDP", "send and read datagrams")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Echo one datagram"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("PING"))
        local reply = ATE.read(dev)
        ATE.report(reply.data == "ECHO:PING" and Result.PASS or Result.FAIL,
                   "the peer echoed " .. (reply.data or "nothing"),
                   reply.count, 9)
        ATE.close(dev)
      end),

    -- Ask for 10 bytes of a 40-byte datagram: ATE returns the first 10 and says partial.
    StepParams("Truncate a datagram"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("a datagram longer than the size asked for"))
        local r = ATE.read(dev, devices.Read():setSize(10))
        ATE.report((r.count == 10 and r.partial) and Result.PASS or Result.FAIL,
                   "read " .. r.count .. " of a longer datagram",
                   r.count, 10)
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/serial.tslua
-- serial.tslua: a SERIAL Device through pyserial.
--
-- The port here is pyserial's socket:// URL, which opens a TCP connection to the
-- simulator peer; a real station names a COM or tty port instead.
--
-- Demonstrates:
--   * open has no defaults, so every field is named;
--   * the Parity and FlowControl enums;
--   * the same connection read with the Device's own terminator.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: SERIAL", "open a serial port and read a reply")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Open the port"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.SERIAL, {
          port = "socket://" .. host .. ":" .. port,
          baud = 115200,
          dataBits = 8,
          stopBits = 1,
          parity = Parity.NONE,
          flowControl = FlowControl.NONE,
          timeout = 2.0,
          readTerm = "\n",
          writeTerm = "\n",
        })
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "serial reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/visa.tslua
-- visa.tslua: a VISA instrument through pyvisa.
--
-- VISA has no background reader: ATE reads the instrument only when the schedule asks,
-- because polling an SCPI instrument with reads that have nothing pending makes it log
-- "Query UNTERMINATED" (-420) and ties up the bus. ATE ships pyvisa-py; a VISA resource
-- string names the instrument, here the simulator peer as a plain TCPIP socket.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: VISA", "read an instrument through pyvisa")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Ask the instrument who it is"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.VISA,
          devices.Visa("TCPIP0::" .. host .. "::" .. port .. "::SOCKET"):setTimeout(2.0))
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        local f = ATE.flush(dev)   -- VISA has no receive buffer: this discards nothing
        ATE.log(Log.INFO, "flush discarded " .. f.count .. " bytes")
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "VISA reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

A handle lives for the whole run. Closing a closed Device does nothing and reports success;
using one is a failure through `onError`. At the end of every run ATE closes whatever is still
open and logs a WARNING for each.

```lua:docs/manuals/examples/test-writer/left_open.tslua
-- left_open.tslua: a Device the schedule never closes.
--
-- A Test Run closes every Device still open when it ends, and logs one WARNING naming
-- each one. That is why a Device opened in a Pre-test Action does not have to be closed
-- by hand - it is worth closing anyway, so the log stays clean.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: a Device left open", "ATE closes what the schedule forgets")
  :setSchedule({
    StepParams("Open and forget"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.report(Result.PASS, "opened a Device and left it open")
      end),
  })
```

## Device failures, Stop on Fail, Retry, Abort

`onError(opResult, opArgs)` is called when the operation fails. If it returns anything but
`nil`, the call returns that value and the schedule continues. If it returns `nil`, or there is
no handler, the call raises a Lua error and the step records `Result.ERROR`. A read that times
out having received some bytes is not a failure: it returns `partial = true` and logs a
WARNING. ATE never reconnects: close the Device and open it again with the same params.

A field the Device Type does not know, a missing required field, or a wrong-typed value is not a
Device failure. It never reaches `onError`; the call returns `false` with the message as its
second result and the traceback is logged at ERROR.

```lua:docs/manuals/examples/test-writer/errors.tslua
-- errors.tslua: how Device failures reach the schedule, and what does not.
--
-- Demonstrates:
--   * a Device failure with no onError raises a Lua error, which ends the step as
--     Result.ERROR;
--   * an onError that returns a value makes the call continue with that value;
--   * a bad argument (a misspelled field) is a schedule bug, not a Device failure: it
--     never reaches onError, it is logged at ERROR, and the call returns false with the
--     message as its second result;
--   * the handler given to open is the fallback for that Device's read, write and close.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025
local deadPort = tonumber(os.getenv("ATE_DEAD_PORT")) or 1

return ScheduleParams("Manual: Device errors", "onError, a closed Device and a bad field")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A misspelled field"):setStepNo("1.")
      :setAction(function()
        local opened, why = ATE.open(DeviceType.TCP, { host = host, port = port, timeout = 1.0,
                                                     readTerm = "\n", writeTerm = "\n", buad = 9600 })
        ATE.report(opened == false and Result.PASS or Result.FAIL, "the open was refused: " .. why)
      end),

    -- Nothing is listening on deadPort: the open fails. This handler logs and returns nil,
    -- which means "no recovery", so ATE.open raises and the step records Result.ERROR.
    StepParams("A port nothing listens on"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, deadPort):setTimeout(0.5),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "nothing is listening on " .. opArgs.host .. ":" .. opArgs.port)
            return nil
          end)
        ATE.report(Result.PASS, "this line is never reached")
      end),

    -- A Device that is closed cannot be read. With no handler the call raises.
    StepParams("Read a closed Device"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.close(dev)
        ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(Result.PASS, "this line is never reached either")
      end),

    -- The same failure, handled: the step carries on with what onError returns.
    StepParams("Handle the closed Device"):setStepNo("4.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "the Device is gone: " .. opResult.err)
            return { data = nil, count = 0, partial = false }
          end)
        ATE.close(dev)
        local r = ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "recovered from reading a closed Device")
      end),
  })
```

Stop on Fail pauses the run at a `FAIL` or `ERROR` step so the Operator can choose `CONTINUE`,
`RETRY` or `ABORT`. Retry runs the action again from the start with POLL reset and blocking
inputs cleared; each Attempt is recorded and the step's result is the last Attempt's. There is
no Attempt number in Lua - keep your own count.

```lua:docs/manuals/examples/test-writer/retry.tslua
-- retry.tslua: Stop on Fail, and what a Retry does.
--
-- Demonstrates:
--   * setStopOnFail(true) is the default: a step ending FAIL or ERROR pauses the run and
--     the Operator chooses Continue, Retry or Abort.
--   * Retry runs the step's action again from the beginning. Each Attempt is recorded;
--     the step's result is its last Attempt's, and the Operator's choice is recorded too.
--   * ATE gives a step no way to read its own Attempt number, so the schedule keeps the
--     count itself in a local the action closes over.
local attempts = 0

return ScheduleParams("Manual: retry", "the first Attempt fails, the Retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Retry this one"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt " .. attempts .. ": the joint is cold", 0.2, 2.4)
        else
          ATE.report(Result.PASS, "attempt " .. attempts .. ": the joint is warm", 2.4, 2.4)
        end
      end),

    -- Only reached after the step passed, or after the Operator chose to continue.
    StepParams("Next check"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "the rail is steady", 5.0, 5.0)
      end),
  })
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

## Sub-schedules

`:setInclude(file, keyvals)` expands the file's steps beneath the header row. The included file
gets `keyvals` as the chunk argument (`local kv = ...`), and ATE runs it again per include, so
each include is its own instance. `keyvals` must be plain data (strings, numbers, booleans,
nested tables of those). A top-level schedule, or an include with no keyvals, gets `{}`.

The file is resolved against the release that owns the including schedule, then its
dependencies depth first - the same order as `require`. Prefixes are prepended exactly as
written and stack outward-in. A header row is not a Test Step: it has no Result and is not
counted in the summary. `setEnable(false)` on a header forces the default of every step
beneath it, and an instance whose whole subtree is disabled has both its Actions skipped. The
included schedule's doc fields and its `setStopOnFail` are ignored; only its Actions and steps
take part.

```lua:docs/manuals/examples/test-writer/main.tslua
-- main.tslua: the top-level schedule that includes Sub-schedules.
--
-- Demonstrates:
--   * :setInclude(file, keyvals) makes a step a header row, not a Test Step: the steps of
--     the included schedule are pulled in beneath it.
--   * The same file can be included more than once; each include is its own instance with
--     its own keyvals.
--   * :setPrefix is prepended to the included steps' Step Numbers, as written, with no
--     separator, and prefixes stack as the includes nest.
--   * :setEnable on a header forces the default of every step beneath it.
return ScheduleParams("Manual: sub-schedules", "includes, keyvals, prefixes and nesting")
  :setDocTitle("Manual examples - Sub-schedules")
  :setDocId("MR-MANUAL-SUBS")
  :setDocRev("1.0")
  :setSchedule({
    StepParams("Rail present"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "the rail is fitted") end),

    -- 2.1. and 2.2. come from channel.tslua, included once for channel 1.
    StepParams("Channel 1"):setStepNo("2.")
      :setInclude("channel.tslua", { channel = 1, label = "left rail" }):setPrefix("2."),

    -- A disabled header: every step beneath it records NOTRUN, and its Actions are skipped.
    StepParams("Channel 2, not fitted"):setStepNo("3.")
      :setEnable(false)
      :setInclude("channel.tslua", { channel = 2, label = "right rail" }):setPrefix("3."),
  })
```

```lua:docs/manuals/examples/test-writer/channel.tslua
-- channel.tslua: a Sub-schedule. It is complete enough to run on its own, and is listed
-- as hidden in manual.atedef so the Operator never picks it directly.
--
-- Each include runs this file again, with the keyvals passed as the chunk's argument.
-- Its own Actions take part in the run's Pre-test and Post-test order.
local kv = ...
local channel = kv.channel or 1
local label = kv.label or "a channel"

return ScheduleParams("Channel check", "one output channel")
  :setPreTestAction(function()
    ATE.log(Log.INFO, "channel " .. channel .. " (" .. label .. ") pre-test")
  end)
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "channel " .. channel .. " post-test, run stands at " .. passFail)
  end)
  :setSchedule({
    StepParams("Switch the channel on"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "channel " .. channel .. " switches on")
      end),

    -- A Sub-schedule inside a Sub-schedule: step numbers become 2.2.1. and so on.
    StepParams("Its LEDs"):setStepNo("2.")
      :setInclude("led.tslua", { channel = channel }):setPrefix("2."),
  })
```

```lua:docs/manuals/examples/test-writer/led.tslua
-- led.tslua: the innermost Sub-schedule, included by channel.tslua.
local kv = ...
local channel = kv.channel or 1

return ScheduleParams("LED check", "the indicator LEDs of one channel")
  :setSchedule({
    StepParams("The LED lights"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "channel " .. channel .. " LED is lit")
      end),
  })
```

## Modules and the `.atedef`

`require` is ATE's own. It resolves the name against the release's files, then its
dependencies, never against `package.path`, which ATE empties. An unlisted name is an error. A
module runs once per Test Run and its return value is cached. `ate.` is reserved for modules
bundled with ATE (`ate.devices`).

Checksums are SHA-256 of the raw bytes, lowercase hex. A mismatch is a WARNING, the file still
loads, and both checksums are recorded with the run. `ate definition update <file.atedef>`
computes each listed file's SHA-256 and writes it back over the old value, filling in any
`sha256` that is absent; the file is only rewritten if a checksum actually changed, so a
correct `.atedef` comes back byte for byte the same. A listed file it cannot read keeps its
current value and warns.

Every field a `.atedef` can carry:

| Field | Meaning |
| --- | --- |
| `format` | must be `1`; anything else makes the definition broken |
| `mediaNumber` | the release's identity; not a version |
| `name`, `version` | `version` must be valid semver 2.0.0; other releases name it in `depends` |
| `depends` | a list of `{"name": ..., "version": ...}`; resolved depth first, in declaration order |
| `files[]` | one entry per Lua file: `path`, `sha256` |
| `files[].path` | relative to the `.atedef` (or absolute), forward slashes on both platforms |
| `files[].hidden` | only on a `.tslua`: keeps it off the Run screen while still allowing it as a Sub-schedule |
| `files[].module` | only on a `.lua`: the name `require` uses. Otherwise the path derives it: `lib/rails.lua` is `lib.rails`. An absolute path, or one containing `..`, must state it |

Here is the definition the manual's own examples ship, exactly as it sits on disk - note
`channel.tslua` and `led.tslua` marked `hidden`, and `modules/limits.lua` given the module name
`board_limits`:

```json:docs/manuals/examples/test-writer/manual.atedef
{
  "format": 1,
  "mediaNumber": "MR-MANUAL-TW",
  "name": "Test writer manual examples",
  "version": "1.0.0",
  "files": [
    {
      "path": "first.tslua",
      "sha256": "a260d2b923819ff6f71804370d4afce9ac77269ec9d9cf9c4b0dae35b85e48c8"
    },
    {
      "path": "steps.tslua",
      "sha256": "2203cddc55bf0daf0e557d3ecf1b847e77bf8fa873385cea775e32f240c5af97"
    },
    {
      "path": "inputs.tslua",
      "sha256": "6a746ebad482542f81d5b846b65a57adb467942e5289d9fc7e76b9bdaabd22fc"
    },
    {
      "path": "actions.tslua",
      "sha256": "a15e9a9fa060f0e829cc2e047d2c0313a1a2203c7c33356e792f699d1b02c526"
    },
    {
      "path": "retry.tslua",
      "sha256": "5375d9870ad11af1553eeaa1d5fd27ce586c2a5cce78af6b4a493a029fee58c9"
    },
    {
      "path": "results.tslua",
      "sha256": "49f80374f637c13e34281140912335fcb583fa87865674def089b18188130ce7"
    },
    {
      "path": "main.tslua",
      "sha256": "fc768ae110764fb3a7a9a4a29fdd93df3beafc09b8fff6ea7fa71efa825964d4"
    },
    {
      "path": "channel.tslua",
      "hidden": true,
      "sha256": "1cecd8b3566e4783c86375ebcdac6b69eaae52f1ea74e32c495babf7009b6e63"
    },
    {
      "path": "led.tslua",
      "hidden": true,
      "sha256": "3477e6a5a1578108bd76687e13e211ca1c48b5bac3343160a4e86319f87f46f8"
    },
    {
      "path": "modules.tslua",
      "sha256": "f34234441c454b9e0559e778fb86c6f10086f2edd4248207953ce67f22fae386"
    },
    {
      "path": "lib/rails.lua",
      "sha256": "392e385ab81dfb814a7ddec0cb1c0799c97d714b3d97c2cfb3306e6ba6cde029"
    },
    {
      "path": "modules/limits.lua",
      "module": "board_limits",
      "sha256": "ea8a21a8a7bef16b0abaa9efcc07c8f08416b4a07b712ac35b9ffdbf8c31fd99"
    },
    {
      "path": "tcp.tslua",
      "sha256": "8439beda05cc96f5c6bd69629951fc2318a7526eb085fb2bb322fc711c2cb8c7"
    },
    {
      "path": "udp.tslua",
      "sha256": "324ce6659ab74bcea159fbe20d063925e5b5a88992b2ad1755e4d68625f461b4"
    },
    {
      "path": "serial.tslua",
      "sha256": "dd717a441db74b490b84b999f82a54b7e0d2b3b3185906ae83150562c7dcb768"
    },
    {
      "path": "visa.tslua",
      "sha256": "5c9d69646277814d6d0e134380ba32e6402ce4d9d3f4006dbe4f622bb107afb8"
    },
    {
      "path": "errors.tslua",
      "sha256": "66151279fc9e62462e80a75e273785fac0bb1c8f2a7bf0aeec62f312a1fb0fe2"
    },
    {
      "path": "left_open.tslua",
      "sha256": "a53f424fe15813bafdcdf74f0c7eb4e69b37bbbb5fa27d833b58eb7d1923b279"
    },
    {
      "path": "logging.tslua",
      "sha256": "ee6b7ee6f1a3b1ed15522544d93645c694dfd29d27d53188416c8e956170dd02"
    },
    {
      "path": "abort.tslua",
      "sha256": "b164d0057d61fa7d5f4092a2b47154dc4ca16dfd685640ef09338f4b862eb308"
    },
    {
      "path": "sandbox.tslua",
      "sha256": "e40b7d8eafd167b5d52f844957962b505f53009d791a38bb2729413462aaee99"
    },
    {
      "path": "forbidden.tslua",
      "sha256": "eae7b5e968792a6abdcf883ccde1e956fdf4f7a6c14e2d8aab8ff02c1fc8dd97"
    }
  ]
}
```

```lua:docs/manuals/examples/test-writer/modules.tslua
-- modules.tslua: sharing code with require, including ATE's own bundled modules.
--
-- Demonstrates:
--   * require finds only the files the Test Definition lists (and its dependencies'),
--     never anything on package.path;
--   * the module name is derived from a plain relative path, or given explicitly with the
--     "module" field;
--   * the reserved "ate." namespace is ATE's own: require("ate.devices") is always ATE's
--     Device params builder, never a file in the Media Release.
local rails = require("lib.rails")      -- derived name: the path without .lua, / as .
local limits = require("board_limits")  -- explicit "module" name in manual.atedef
local devices = require("ate.devices")  -- bundled with ATE

return ScheduleParams("Manual: modules", "require and the ate. namespace")
  :setSchedule({
    StepParams("Check every rail"):setStepNo("1.")
      :setAction(function()
        local ok = true
        for channel = 1, 3 do
          local volts = rails.nominalVolts(channel)
          ok = ok and volts >= limits.low and volts <= limits.high
          ATE.log(Log.INFO, "channel " .. channel .. " nominal " .. volts .. " V")
        end
        ATE.report(ok and Result.PASS or Result.FAIL,
                   "every rail is inside " .. limits.low .. " to " .. limits.high .. " V")
      end),

    -- A builder is just a params table: nothing is opened, so no Device is involved.
    StepParams("Build params without opening"):setStepNo("2.")
      :setAction(function()
        local params = devices.Tcp("127.0.0.1", 5025):setTimeout(2.5)
        ATE.report(Result.PASS, "timeout set to " .. params.timeout, params.timeout, 2.5)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/lib/rails.lua
-- lib/rails.lua: an ordinary Lua module. Its module name is derived from its path:
-- "lib/rails.lua" becomes "lib.rails". require runs a module once per Test Run and
-- caches what it returns; later requires of the same name get the cached value.
local rails = {}

function rails.nominalVolts(channel)
  return 1.5 * channel
end

return rails
```

```lua:docs/manuals/examples/test-writer/modules/limits.lua
-- modules/limits.lua: a module whose name is given explicitly in manual.atedef with the
-- "module" field, because a name derived from the path would be "modules.limits".
return { low = 1.0, high = 5.0 }
```

## Devices

`ATE.open(deviceType, params [, onError])` returns an opaque handle. Every field `open` needs
is required - there are no defaults in `ATE.open`; the builders supply them.

| Device Type | `open` fields | builder (defaults) |
| --- | --- | --- |
| `DeviceType.TCP` | `host`, `port`, `timeout`, `readTerm`, `writeTerm` | `devices.Tcp(host, port)`: timeout 1.0, terminators `"\n"` |
| `DeviceType.SERIAL` | `port`, `baud`, `dataBits`, `stopBits`, `parity`, `flowControl`, `timeout`, `readTerm`, `writeTerm` | `devices.Serial(port)`: 9600, 8, 1, `Parity.NONE`, `FlowControl.NONE`, 1.0, terminators `"\n"` |
| `DeviceType.UDP` | `host`, `port`, `localPort`, `timeout` | `devices.Udp(host, port)`: `localPort` 0, timeout 1.0 |
| `DeviceType.VISA` | `resource`, `timeout`, `readTerm`, `writeTerm` | `devices.Visa(resource)`: timeout 1.0, terminators `"\n"` |

Per-call fields, all optional, falling back to the open-time configuration: `read` takes
`size`, `timeout`, `readTerm`; `write` takes `data` (required), `writeTerm`, `timeout`;
`close` takes no fields of its own but accepts an optional params table and an optional
`onError` — `ATE.close(dev [, params] [, onError])` — which is used if the close itself fails.
UDP has no terminators: its `read` takes `size` and `timeout`, and its `write` takes `data`
(required) and `timeout`. Passing a `readTerm` to a UDP Device is an error naming the field.
Builder setters: `setHost`, `setPort`, `setLocalPort`, `setResource`, `setBaud`, `setDataBits`,
`setStopBits`, `setParity`, `setFlowControl`, `setTimeout`, `setReadTerm`, `setWriteTerm`,
`setSize`, and `devices.Write(data)`.

Reads and writes are raw bytes. The terminator is stripped from `data`; `count` is what is
left. A write's `count` includes the terminator.

TCP, SERIAL and UDP are read in the background from `open` to `close` and buffered, capped at
the Station config key `devices.receiveBufferBytes` (16777216, 16 MiB); on overflow ATE drops
the oldest bytes, logs one WARNING, and the next read carries `overflow = true`. `ATE.flush(dev)`
empties the buffer and returns the count discarded. VISA has no background reader and no buffer,
so `flush` discards nothing.

A VISA resource string names the instrument, and the shape matters:
`GPIB0::5::INSTR` is a GPIB instrument, `TCPIP0::192.168.1.10::inst0::INSTR` is a VXI-11
instrument, and `TCPIP0::127.0.0.1::5025::SOCKET` is a plain TCP socket wrapped in a VISA
session - the same stream a `DeviceType.TCP` Device would open, with pyvisa in the way. ATE
uses whichever pyvisa backend it finds; only pyvisa-py is shipped, which reaches TCPIP and
USBTMC. A GPIB instrument needs a real VISA driver on the Station (NI-VISA or linux-GPIB): ask
the schedule author which resource string their instrument needs before writing the schedule,
because nothing installed with ATE can open a GPIB address.

```lua:docs/manuals/examples/test-writer/tcp.tslua
-- tcp.tslua: a TCP Device, from open to close.
--
-- The host and port come from the environment, which the test harness sets to a
-- simulator peer listening on 127.0.0.1. Point them at a real instrument to use this
-- schedule as it is.
--
-- Demonstrates:
--   * the ate.devices builders, whose setters chain, and how a Device opened in the
--     Pre-test Action is still open in the steps, because a device object lives for the
--     whole Test Run;
--   * per-call overrides (timeout, readTerm, size) that do not change the open-time
--     configuration;
--   * a read that times out after some bytes is a partial read, not a Device failure;
--   * a read that times out with nothing at all is a Device failure and goes through
--     onError, which is given the failure table and the params table and may return a
--     value for the call to carry on with;
--   * flush discards whatever the receive buffer already holds.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

local instrument = nil

return ScheduleParams("Manual: TCP", "open, write, read, flush and close a TCP Device")
  :setStopOnFail(false)
  :setPreTestAction(function()
    -- devices.Tcp(host, port) fills in every field TCP needs: timeout 1.0 s,
    -- readTerm and writeTerm "\n". open has no defaults of its own, so a plain table
    -- would have to name them all.
    instrument = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(2.0))
    ATE.addEquipment("SCPI simulator", "SIM-TCP", "TCP " .. host .. ":" .. port, "2026-01-01")
  end)
  :setPostTestAction(function(passFail)
    ATE.close(instrument)   -- closing a second time does nothing and still reports ok
  end)
  :setSchedule({
    StepParams("Identify the instrument"):setStepNo("1.")
      :setAction(function()
        local sent = ATE.write(instrument, devices.Write("*IDN?"))
        local reply = ATE.read(instrument, devices.Read():setTimeout(1.0))
        ATE.report((sent.ok and reply.count > 0) and Result.PASS or Result.FAIL,
                   "reply: " .. (reply.data or "nothing"),
                   reply.count, 26)
      end),

    -- The peer answers "MEAS:VOLT?" with "1.234\n". readTerm "3" stops the read at the "3",
    -- so the first read takes "1.2" and the second takes the rest of the reply.
    StepParams("Stop a read at a terminator"):setStepNo("2.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MEAS:VOLT?"))
        local first = ATE.read(instrument, devices.Read():setReadTerm("3"))
        local rest = ATE.read(instrument)
        ATE.report((first.data == "1.2" and rest.data == "4") and Result.PASS or Result.FAIL,
                   "first read " .. (first.data or "nothing") .. ", second " .. (rest.data or "nothing"),
                   first.count + rest.count, 4)
      end),

    -- "PARTIAL?" is answered with bytes and no terminator: the read times out having taken
    -- some of them. That is partial = true, a WARNING in the log, and not a Device failure.
    StepParams("A read that times out partway"):setStepNo("3.")
      :setAction(function()
        ATE.write(instrument, devices.Write("PARTIAL?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3))
        ATE.report((r.partial and r.count > 0) and Result.PASS or Result.FAIL,
                   "got " .. r.count .. " bytes before the timeout",
                   tostring(r.partial), "true")
      end),

    -- "NEVER?" is answered with silence: the read times out with no bytes at all, which IS
    -- a Device failure, so onError runs. Returning a value makes the call continue with it.
    StepParams("Handle a read that times out"):setStepNo("4.")
      :setAction(function()
        ATE.write(instrument, devices.Write("NEVER?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3), function(opResult, opArgs)
          ATE.log(Log.WARNING, "no reply from the instrument: " .. opResult.err)
          return { data = nil, count = 0, partial = false }
        end)
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "the timeout was handled, so the step carried on")
      end),

    -- The peer sends three lines; the schedule reads the first and throws away the rest.
    StepParams("Discard the receive buffer"):setStepNo("5.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MULTI?"))
        ATE.read(instrument, devices.Read():setSize(7))
        local f = ATE.flush(instrument)
        ATE.report((f.ok and f.count > 0) and Result.PASS or Result.FAIL, "discarded " .. f.count .. " buffered bytes")
      end),
  })
```

```lua:docs/manuals/examples/test-writer/udp.tslua
-- udp.tslua: a UDP Device, where one datagram is one message.
--
-- Demonstrates:
--   * UDP has no terminators, so readTerm and writeTerm are not fields this Device Type
--     knows; passing one is a schedule bug that raises naming the field;
--   * a datagram longer than the size asked for is truncated and returned with partial =
--     true, which the log calls a truncation rather than a timeout;
--   * the receive buffer is a queue of whole datagrams, so a reply that arrives before the
--     schedule reads it is not lost.
local devices = require("ate.devices")

local host = os.getenv("ATE_UDP_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_UDP_PORT")) or 5026

return ScheduleParams("Manual: UDP", "send and read datagrams")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Echo one datagram"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("PING"))
        local reply = ATE.read(dev)
        ATE.report(reply.data == "ECHO:PING" and Result.PASS or Result.FAIL,
                   "the peer echoed " .. (reply.data or "nothing"),
                   reply.count, 9)
        ATE.close(dev)
      end),

    -- Ask for 10 bytes of a 40-byte datagram: ATE returns the first 10 and says partial.
    StepParams("Truncate a datagram"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("a datagram longer than the size asked for"))
        local r = ATE.read(dev, devices.Read():setSize(10))
        ATE.report((r.count == 10 and r.partial) and Result.PASS or Result.FAIL,
                   "read " .. r.count .. " of a longer datagram",
                   r.count, 10)
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/serial.tslua
-- serial.tslua: a SERIAL Device through pyserial.
--
-- The port here is pyserial's socket:// URL, which opens a TCP connection to the
-- simulator peer; a real station names a COM or tty port instead.
--
-- Demonstrates:
--   * open has no defaults, so every field is named;
--   * the Parity and FlowControl enums;
--   * the same connection read with the Device's own terminator.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: SERIAL", "open a serial port and read a reply")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Open the port"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.SERIAL, {
          port = "socket://" .. host .. ":" .. port,
          baud = 115200,
          dataBits = 8,
          stopBits = 1,
          parity = Parity.NONE,
          flowControl = FlowControl.NONE,
          timeout = 2.0,
          readTerm = "\n",
          writeTerm = "\n",
        })
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "serial reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/visa.tslua
-- visa.tslua: a VISA instrument through pyvisa.
--
-- VISA has no background reader: ATE reads the instrument only when the schedule asks,
-- because polling an SCPI instrument with reads that have nothing pending makes it log
-- "Query UNTERMINATED" (-420) and ties up the bus. ATE ships pyvisa-py; a VISA resource
-- string names the instrument, here the simulator peer as a plain TCPIP socket.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: VISA", "read an instrument through pyvisa")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Ask the instrument who it is"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.VISA,
          devices.Visa("TCPIP0::" .. host .. "::" .. port .. "::SOCKET"):setTimeout(2.0))
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        local f = ATE.flush(dev)   -- VISA has no receive buffer: this discards nothing
        ATE.log(Log.INFO, "flush discarded " .. f.count .. " bytes")
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "VISA reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

A handle lives for the whole run. Closing a closed Device does nothing and reports success;
using one is a failure through `onError`. At the end of every run ATE closes whatever is still
open and logs a WARNING for each.

```lua:docs/manuals/examples/test-writer/left_open.tslua
-- left_open.tslua: a Device the schedule never closes.
--
-- A Test Run closes every Device still open when it ends, and logs one WARNING naming
-- each one. That is why a Device opened in a Pre-test Action does not have to be closed
-- by hand - it is worth closing anyway, so the log stays clean.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: a Device left open", "ATE closes what the schedule forgets")
  :setSchedule({
    StepParams("Open and forget"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.report(Result.PASS, "opened a Device and left it open")
      end),
  })
```

## Device failures, Stop on Fail, Retry, Abort

`onError(opResult, opArgs)` is called when the operation fails. If it returns anything but
`nil`, the call returns that value and the schedule continues. If it returns `nil`, or there is
no handler, the call raises a Lua error and the step records `Result.ERROR`. A read that times
out having received some bytes is not a failure: it returns `partial = true` and logs a
WARNING. ATE never reconnects: close the Device and open it again with the same params.

A field the Device Type does not know, a missing required field, or a wrong-typed value is not a
Device failure. It never reaches `onError`; the call returns `false` with the message as its
second result and the traceback is logged at ERROR.

```lua:docs/manuals/examples/test-writer/errors.tslua
-- errors.tslua: how Device failures reach the schedule, and what does not.
--
-- Demonstrates:
--   * a Device failure with no onError raises a Lua error, which ends the step as
--     Result.ERROR;
--   * an onError that returns a value makes the call continue with that value;
--   * a bad argument (a misspelled field) is a schedule bug, not a Device failure: it
--     never reaches onError, it is logged at ERROR, and the call returns false with the
--     message as its second result;
--   * the handler given to open is the fallback for that Device's read, write and close.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025
local deadPort = tonumber(os.getenv("ATE_DEAD_PORT")) or 1

return ScheduleParams("Manual: Device errors", "onError, a closed Device and a bad field")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A misspelled field"):setStepNo("1.")
      :setAction(function()
        local opened, why = ATE.open(DeviceType.TCP, { host = host, port = port, timeout = 1.0,
                                                     readTerm = "\n", writeTerm = "\n", buad = 9600 })
        ATE.report(opened == false and Result.PASS or Result.FAIL, "the open was refused: " .. why)
      end),

    -- Nothing is listening on deadPort: the open fails. This handler logs and returns nil,
    -- which means "no recovery", so ATE.open raises and the step records Result.ERROR.
    StepParams("A port nothing listens on"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, deadPort):setTimeout(0.5),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "nothing is listening on " .. opArgs.host .. ":" .. opArgs.port)
            return nil
          end)
        ATE.report(Result.PASS, "this line is never reached")
      end),

    -- A Device that is closed cannot be read. With no handler the call raises.
    StepParams("Read a closed Device"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.close(dev)
        ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(Result.PASS, "this line is never reached either")
      end),

    -- The same failure, handled: the step carries on with what onError returns.
    StepParams("Handle the closed Device"):setStepNo("4.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "the Device is gone: " .. opResult.err)
            return { data = nil, count = 0, partial = false }
          end)
        ATE.close(dev)
        local r = ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "recovered from reading a closed Device")
      end),
  })
```

Stop on Fail pauses the run at a `FAIL` or `ERROR` step so the Operator can choose `CONTINUE`,
`RETRY` or `ABORT`. Retry runs the action again from the start with POLL reset and blocking
inputs cleared; each Attempt is recorded and the step's result is the last Attempt's. There is
no Attempt number in Lua - keep your own count.

```lua:docs/manuals/examples/test-writer/retry.tslua
-- retry.tslua: Stop on Fail, and what a Retry does.
--
-- Demonstrates:
--   * setStopOnFail(true) is the default: a step ending FAIL or ERROR pauses the run and
--     the Operator chooses Continue, Retry or Abort.
--   * Retry runs the step's action again from the beginning. Each Attempt is recorded;
--     the step's result is its last Attempt's, and the Operator's choice is recorded too.
--   * ATE gives a step no way to read its own Attempt number, so the schedule keeps the
--     count itself in a local the action closes over.
local attempts = 0

return ScheduleParams("Manual: retry", "the first Attempt fails, the Retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Retry this one"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt " .. attempts .. ": the joint is cold", 0.2, 2.4)
        else
          ATE.report(Result.PASS, "attempt " .. attempts .. ": the joint is warm", 2.4, 2.4)
        end
      end),

    -- Only reached after the step passed, or after the Operator chose to continue.
    StepParams("Next check"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "the rail is steady", 5.0, 5.0)
      end),
  })
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

```lua:docs/manuals/examples/test-writer/channel.tslua
-- channel.tslua: a Sub-schedule. It is complete enough to run on its own, and is listed
-- as hidden in manual.atedef so the Operator never picks it directly.
--
-- Each include runs this file again, with the keyvals passed as the chunk's argument.
-- Its own Actions take part in the run's Pre-test and Post-test order.
local kv = ...
local channel = kv.channel or 1
local label = kv.label or "a channel"

return ScheduleParams("Channel check", "one output channel")
  :setPreTestAction(function()
    ATE.log(Log.INFO, "channel " .. channel .. " (" .. label .. ") pre-test")
  end)
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "channel " .. channel .. " post-test, run stands at " .. passFail)
  end)
  :setSchedule({
    StepParams("Switch the channel on"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "channel " .. channel .. " switches on")
      end),

    -- A Sub-schedule inside a Sub-schedule: step numbers become 2.2.1. and so on.
    StepParams("Its LEDs"):setStepNo("2.")
      :setInclude("led.tslua", { channel = channel }):setPrefix("2."),
  })
```

```lua:docs/manuals/examples/test-writer/led.tslua
-- led.tslua: the innermost Sub-schedule, included by channel.tslua.
local kv = ...
local channel = kv.channel or 1

return ScheduleParams("LED check", "the indicator LEDs of one channel")
  :setSchedule({
    StepParams("The LED lights"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "channel " .. channel .. " LED is lit")
      end),
  })
```

## Modules and the `.atedef`

`require` is ATE's own. It resolves the name against the release's files, then its
dependencies, never against `package.path`, which ATE empties. An unlisted name is an error. A
module runs once per Test Run and its return value is cached. `ate.` is reserved for modules
bundled with ATE (`ate.devices`).

Checksums are SHA-256 of the raw bytes, lowercase hex. A mismatch is a WARNING, the file still
loads, and both checksums are recorded with the run. `ate definition update <file.atedef>`
computes each listed file's SHA-256 and writes it back over the old value, filling in any
`sha256` that is absent; the file is only rewritten if a checksum actually changed, so a
correct `.atedef` comes back byte for byte the same. A listed file it cannot read keeps its
current value and warns.

Every field a `.atedef` can carry:

| Field | Meaning |
| --- | --- |
| `format` | must be `1`; anything else makes the definition broken |
| `mediaNumber` | the release's identity; not a version |
| `name`, `version` | `version` must be valid semver 2.0.0; other releases name it in `depends` |
| `depends` | a list of `{"name": ..., "version": ...}`; resolved depth first, in declaration order |
| `files[]` | one entry per Lua file: `path`, `sha256` |
| `files[].path` | relative to the `.atedef` (or absolute), forward slashes on both platforms |
| `files[].hidden` | only on a `.tslua`: keeps it off the Run screen while still allowing it as a Sub-schedule |
| `files[].module` | only on a `.lua`: the name `require` uses. Otherwise the path derives it: `lib/rails.lua` is `lib.rails`. An absolute path, or one containing `..`, must state it |

Here is the definition the manual's own examples ship, exactly as it sits on disk - note
`channel.tslua` and `led.tslua` marked `hidden`, and `modules/limits.lua` given the module name
`board_limits`:

```json:docs/manuals/examples/test-writer/manual.atedef
{
  "format": 1,
  "mediaNumber": "MR-MANUAL-TW",
  "name": "Test writer manual examples",
  "version": "1.0.0",
  "files": [
    {
      "path": "first.tslua",
      "sha256": "a260d2b923819ff6f71804370d4afce9ac77269ec9d9cf9c4b0dae35b85e48c8"
    },
    {
      "path": "steps.tslua",
      "sha256": "2203cddc55bf0daf0e557d3ecf1b847e77bf8fa873385cea775e32f240c5af97"
    },
    {
      "path": "inputs.tslua",
      "sha256": "6a746ebad482542f81d5b846b65a57adb467942e5289d9fc7e76b9bdaabd22fc"
    },
    {
      "path": "actions.tslua",
      "sha256": "a15e9a9fa060f0e829cc2e047d2c0313a1a2203c7c33356e792f699d1b02c526"
    },
    {
      "path": "retry.tslua",
      "sha256": "5375d9870ad11af1553eeaa1d5fd27ce586c2a5cce78af6b4a493a029fee58c9"
    },
    {
      "path": "results.tslua",
      "sha256": "49f80374f637c13e34281140912335fcb583fa87865674def089b18188130ce7"
    },
    {
      "path": "main.tslua",
      "sha256": "fc768ae110764fb3a7a9a4a29fdd93df3beafc09b8fff6ea7fa71efa825964d4"
    },
    {
      "path": "channel.tslua",
      "hidden": true,
      "sha256": "1cecd8b3566e4783c86375ebcdac6b69eaae52f1ea74e32c495babf7009b6e63"
    },
    {
      "path": "led.tslua",
      "hidden": true,
      "sha256": "3477e6a5a1578108bd76687e13e211ca1c48b5bac3343160a4e86319f87f46f8"
    },
    {
      "path": "modules.tslua",
      "sha256": "f34234441c454b9e0559e778fb86c6f10086f2edd4248207953ce67f22fae386"
    },
    {
      "path": "lib/rails.lua",
      "sha256": "392e385ab81dfb814a7ddec0cb1c0799c97d714b3d97c2cfb3306e6ba6cde029"
    },
    {
      "path": "modules/limits.lua",
      "module": "board_limits",
      "sha256": "ea8a21a8a7bef16b0abaa9efcc07c8f08416b4a07b712ac35b9ffdbf8c31fd99"
    },
    {
      "path": "tcp.tslua",
      "sha256": "8439beda05cc96f5c6bd69629951fc2318a7526eb085fb2bb322fc711c2cb8c7"
    },
    {
      "path": "udp.tslua",
      "sha256": "324ce6659ab74bcea159fbe20d063925e5b5a88992b2ad1755e4d68625f461b4"
    },
    {
      "path": "serial.tslua",
      "sha256": "dd717a441db74b490b84b999f82a54b7e0d2b3b3185906ae83150562c7dcb768"
    },
    {
      "path": "visa.tslua",
      "sha256": "5c9d69646277814d6d0e134380ba32e6402ce4d9d3f4006dbe4f622bb107afb8"
    },
    {
      "path": "errors.tslua",
      "sha256": "66151279fc9e62462e80a75e273785fac0bb1c8f2a7bf0aeec62f312a1fb0fe2"
    },
    {
      "path": "left_open.tslua",
      "sha256": "a53f424fe15813bafdcdf74f0c7eb4e69b37bbbb5fa27d833b58eb7d1923b279"
    },
    {
      "path": "logging.tslua",
      "sha256": "ee6b7ee6f1a3b1ed15522544d93645c694dfd29d27d53188416c8e956170dd02"
    },
    {
      "path": "abort.tslua",
      "sha256": "b164d0057d61fa7d5f4092a2b47154dc4ca16dfd685640ef09338f4b862eb308"
    },
    {
      "path": "sandbox.tslua",
      "sha256": "e40b7d8eafd167b5d52f844957962b505f53009d791a38bb2729413462aaee99"
    },
    {
      "path": "forbidden.tslua",
      "sha256": "eae7b5e968792a6abdcf883ccde1e956fdf4f7a6c14e2d8aab8ff02c1fc8dd97"
    }
  ]
}
```

```lua:docs/manuals/examples/test-writer/modules.tslua
-- modules.tslua: sharing code with require, including ATE's own bundled modules.
--
-- Demonstrates:
--   * require finds only the files the Test Definition lists (and its dependencies'),
--     never anything on package.path;
--   * the module name is derived from a plain relative path, or given explicitly with the
--     "module" field;
--   * the reserved "ate." namespace is ATE's own: require("ate.devices") is always ATE's
--     Device params builder, never a file in the Media Release.
local rails = require("lib.rails")      -- derived name: the path without .lua, / as .
local limits = require("board_limits")  -- explicit "module" name in manual.atedef
local devices = require("ate.devices")  -- bundled with ATE

return ScheduleParams("Manual: modules", "require and the ate. namespace")
  :setSchedule({
    StepParams("Check every rail"):setStepNo("1.")
      :setAction(function()
        local ok = true
        for channel = 1, 3 do
          local volts = rails.nominalVolts(channel)
          ok = ok and volts >= limits.low and volts <= limits.high
          ATE.log(Log.INFO, "channel " .. channel .. " nominal " .. volts .. " V")
        end
        ATE.report(ok and Result.PASS or Result.FAIL,
                   "every rail is inside " .. limits.low .. " to " .. limits.high .. " V")
      end),

    -- A builder is just a params table: nothing is opened, so no Device is involved.
    StepParams("Build params without opening"):setStepNo("2.")
      :setAction(function()
        local params = devices.Tcp("127.0.0.1", 5025):setTimeout(2.5)
        ATE.report(Result.PASS, "timeout set to " .. params.timeout, params.timeout, 2.5)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/lib/rails.lua
-- lib/rails.lua: an ordinary Lua module. Its module name is derived from its path:
-- "lib/rails.lua" becomes "lib.rails". require runs a module once per Test Run and
-- caches what it returns; later requires of the same name get the cached value.
local rails = {}

function rails.nominalVolts(channel)
  return 1.5 * channel
end

return rails
```

```lua:docs/manuals/examples/test-writer/modules/limits.lua
-- modules/limits.lua: a module whose name is given explicitly in manual.atedef with the
-- "module" field, because a name derived from the path would be "modules.limits".
return { low = 1.0, high = 5.0 }
```

## Devices

`ATE.open(deviceType, params [, onError])` returns an opaque handle. Every field `open` needs
is required - there are no defaults in `ATE.open`; the builders supply them.

| Device Type | `open` fields | builder (defaults) |
| --- | --- | --- |
| `DeviceType.TCP` | `host`, `port`, `timeout`, `readTerm`, `writeTerm` | `devices.Tcp(host, port)`: timeout 1.0, terminators `"\n"` |
| `DeviceType.SERIAL` | `port`, `baud`, `dataBits`, `stopBits`, `parity`, `flowControl`, `timeout`, `readTerm`, `writeTerm` | `devices.Serial(port)`: 9600, 8, 1, `Parity.NONE`, `FlowControl.NONE`, 1.0, terminators `"\n"` |
| `DeviceType.UDP` | `host`, `port`, `localPort`, `timeout` | `devices.Udp(host, port)`: `localPort` 0, timeout 1.0 |
| `DeviceType.VISA` | `resource`, `timeout`, `readTerm`, `writeTerm` | `devices.Visa(resource)`: timeout 1.0, terminators `"\n"` |

Per-call fields, all optional, falling back to the open-time configuration: `read` takes
`size`, `timeout`, `readTerm`; `write` takes `data` (required), `writeTerm`, `timeout`;
`close` takes no fields of its own but accepts an optional params table and an optional
`onError` — `ATE.close(dev [, params] [, onError])` — which is used if the close itself fails.
UDP has no terminators: its `read` takes `size` and `timeout`, and its `write` takes `data`
(required) and `timeout`. Passing a `readTerm` to a UDP Device is an error naming the field.
Builder setters: `setHost`, `setPort`, `setLocalPort`, `setResource`, `setBaud`, `setDataBits`,
`setStopBits`, `setParity`, `setFlowControl`, `setTimeout`, `setReadTerm`, `setWriteTerm`,
`setSize`, and `devices.Write(data)`.

Reads and writes are raw bytes. The terminator is stripped from `data`; `count` is what is
left. A write's `count` includes the terminator.

TCP, SERIAL and UDP are read in the background from `open` to `close` and buffered, capped at
the Station config key `devices.receiveBufferBytes` (16777216, 16 MiB); on overflow ATE drops
the oldest bytes, logs one WARNING, and the next read carries `overflow = true`. `ATE.flush(dev)`
empties the buffer and returns the count discarded. VISA has no background reader and no buffer,
so `flush` discards nothing.

A VISA resource string names the instrument, and the shape matters:
`GPIB0::5::INSTR` is a GPIB instrument, `TCPIP0::192.168.1.10::inst0::INSTR` is a VXI-11
instrument, and `TCPIP0::127.0.0.1::5025::SOCKET` is a plain TCP socket wrapped in a VISA
session - the same stream a `DeviceType.TCP` Device would open, with pyvisa in the way. ATE
uses whichever pyvisa backend it finds; only pyvisa-py is shipped, which reaches TCPIP and
USBTMC. A GPIB instrument needs a real VISA driver on the Station (NI-VISA or linux-GPIB): ask
the schedule author which resource string their instrument needs before writing the schedule,
because nothing installed with ATE can open a GPIB address.

```lua:docs/manuals/examples/test-writer/tcp.tslua
-- tcp.tslua: a TCP Device, from open to close.
--
-- The host and port come from the environment, which the test harness sets to a
-- simulator peer listening on 127.0.0.1. Point them at a real instrument to use this
-- schedule as it is.
--
-- Demonstrates:
--   * the ate.devices builders, whose setters chain, and how a Device opened in the
--     Pre-test Action is still open in the steps, because a device object lives for the
--     whole Test Run;
--   * per-call overrides (timeout, readTerm, size) that do not change the open-time
--     configuration;
--   * a read that times out after some bytes is a partial read, not a Device failure;
--   * a read that times out with nothing at all is a Device failure and goes through
--     onError, which is given the failure table and the params table and may return a
--     value for the call to carry on with;
--   * flush discards whatever the receive buffer already holds.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

local instrument = nil

return ScheduleParams("Manual: TCP", "open, write, read, flush and close a TCP Device")
  :setStopOnFail(false)
  :setPreTestAction(function()
    -- devices.Tcp(host, port) fills in every field TCP needs: timeout 1.0 s,
    -- readTerm and writeTerm "\n". open has no defaults of its own, so a plain table
    -- would have to name them all.
    instrument = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(2.0))
    ATE.addEquipment("SCPI simulator", "SIM-TCP", "TCP " .. host .. ":" .. port, "2026-01-01")
  end)
  :setPostTestAction(function(passFail)
    ATE.close(instrument)   -- closing a second time does nothing and still reports ok
  end)
  :setSchedule({
    StepParams("Identify the instrument"):setStepNo("1.")
      :setAction(function()
        local sent = ATE.write(instrument, devices.Write("*IDN?"))
        local reply = ATE.read(instrument, devices.Read():setTimeout(1.0))
        ATE.report((sent.ok and reply.count > 0) and Result.PASS or Result.FAIL,
                   "reply: " .. (reply.data or "nothing"),
                   reply.count, 26)
      end),

    -- The peer answers "MEAS:VOLT?" with "1.234\n". readTerm "3" stops the read at the "3",
    -- so the first read takes "1.2" and the second takes the rest of the reply.
    StepParams("Stop a read at a terminator"):setStepNo("2.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MEAS:VOLT?"))
        local first = ATE.read(instrument, devices.Read():setReadTerm("3"))
        local rest = ATE.read(instrument)
        ATE.report((first.data == "1.2" and rest.data == "4") and Result.PASS or Result.FAIL,
                   "first read " .. (first.data or "nothing") .. ", second " .. (rest.data or "nothing"),
                   first.count + rest.count, 4)
      end),

    -- "PARTIAL?" is answered with bytes and no terminator: the read times out having taken
    -- some of them. That is partial = true, a WARNING in the log, and not a Device failure.
    StepParams("A read that times out partway"):setStepNo("3.")
      :setAction(function()
        ATE.write(instrument, devices.Write("PARTIAL?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3))
        ATE.report((r.partial and r.count > 0) and Result.PASS or Result.FAIL,
                   "got " .. r.count .. " bytes before the timeout",
                   tostring(r.partial), "true")
      end),

    -- "NEVER?" is answered with silence: the read times out with no bytes at all, which IS
    -- a Device failure, so onError runs. Returning a value makes the call continue with it.
    StepParams("Handle a read that times out"):setStepNo("4.")
      :setAction(function()
        ATE.write(instrument, devices.Write("NEVER?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3), function(opResult, opArgs)
          ATE.log(Log.WARNING, "no reply from the instrument: " .. opResult.err)
          return { data = nil, count = 0, partial = false }
        end)
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "the timeout was handled, so the step carried on")
      end),

    -- The peer sends three lines; the schedule reads the first and throws away the rest.
    StepParams("Discard the receive buffer"):setStepNo("5.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MULTI?"))
        ATE.read(instrument, devices.Read():setSize(7))
        local f = ATE.flush(instrument)
        ATE.report((f.ok and f.count > 0) and Result.PASS or Result.FAIL, "discarded " .. f.count .. " buffered bytes")
      end),
  })
```

```lua:docs/manuals/examples/test-writer/udp.tslua
-- udp.tslua: a UDP Device, where one datagram is one message.
--
-- Demonstrates:
--   * UDP has no terminators, so readTerm and writeTerm are not fields this Device Type
--     knows; passing one is a schedule bug that raises naming the field;
--   * a datagram longer than the size asked for is truncated and returned with partial =
--     true, which the log calls a truncation rather than a timeout;
--   * the receive buffer is a queue of whole datagrams, so a reply that arrives before the
--     schedule reads it is not lost.
local devices = require("ate.devices")

local host = os.getenv("ATE_UDP_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_UDP_PORT")) or 5026

return ScheduleParams("Manual: UDP", "send and read datagrams")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Echo one datagram"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("PING"))
        local reply = ATE.read(dev)
        ATE.report(reply.data == "ECHO:PING" and Result.PASS or Result.FAIL,
                   "the peer echoed " .. (reply.data or "nothing"),
                   reply.count, 9)
        ATE.close(dev)
      end),

    -- Ask for 10 bytes of a 40-byte datagram: ATE returns the first 10 and says partial.
    StepParams("Truncate a datagram"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("a datagram longer than the size asked for"))
        local r = ATE.read(dev, devices.Read():setSize(10))
        ATE.report((r.count == 10 and r.partial) and Result.PASS or Result.FAIL,
                   "read " .. r.count .. " of a longer datagram",
                   r.count, 10)
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/serial.tslua
-- serial.tslua: a SERIAL Device through pyserial.
--
-- The port here is pyserial's socket:// URL, which opens a TCP connection to the
-- simulator peer; a real station names a COM or tty port instead.
--
-- Demonstrates:
--   * open has no defaults, so every field is named;
--   * the Parity and FlowControl enums;
--   * the same connection read with the Device's own terminator.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: SERIAL", "open a serial port and read a reply")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Open the port"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.SERIAL, {
          port = "socket://" .. host .. ":" .. port,
          baud = 115200,
          dataBits = 8,
          stopBits = 1,
          parity = Parity.NONE,
          flowControl = FlowControl.NONE,
          timeout = 2.0,
          readTerm = "\n",
          writeTerm = "\n",
        })
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "serial reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/visa.tslua
-- visa.tslua: a VISA instrument through pyvisa.
--
-- VISA has no background reader: ATE reads the instrument only when the schedule asks,
-- because polling an SCPI instrument with reads that have nothing pending makes it log
-- "Query UNTERMINATED" (-420) and ties up the bus. ATE ships pyvisa-py; a VISA resource
-- string names the instrument, here the simulator peer as a plain TCPIP socket.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: VISA", "read an instrument through pyvisa")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Ask the instrument who it is"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.VISA,
          devices.Visa("TCPIP0::" .. host .. "::" .. port .. "::SOCKET"):setTimeout(2.0))
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        local f = ATE.flush(dev)   -- VISA has no receive buffer: this discards nothing
        ATE.log(Log.INFO, "flush discarded " .. f.count .. " bytes")
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "VISA reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

A handle lives for the whole run. Closing a closed Device does nothing and reports success;
using one is a failure through `onError`. At the end of every run ATE closes whatever is still
open and logs a WARNING for each.

```lua:docs/manuals/examples/test-writer/left_open.tslua
-- left_open.tslua: a Device the schedule never closes.
--
-- A Test Run closes every Device still open when it ends, and logs one WARNING naming
-- each one. That is why a Device opened in a Pre-test Action does not have to be closed
-- by hand - it is worth closing anyway, so the log stays clean.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: a Device left open", "ATE closes what the schedule forgets")
  :setSchedule({
    StepParams("Open and forget"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.report(Result.PASS, "opened a Device and left it open")
      end),
  })
```

## Device failures, Stop on Fail, Retry, Abort

`onError(opResult, opArgs)` is called when the operation fails. If it returns anything but
`nil`, the call returns that value and the schedule continues. If it returns `nil`, or there is
no handler, the call raises a Lua error and the step records `Result.ERROR`. A read that times
out having received some bytes is not a failure: it returns `partial = true` and logs a
WARNING. ATE never reconnects: close the Device and open it again with the same params.

A field the Device Type does not know, a missing required field, or a wrong-typed value is not a
Device failure. It never reaches `onError`; the call returns `false` with the message as its
second result and the traceback is logged at ERROR.

```lua:docs/manuals/examples/test-writer/errors.tslua
-- errors.tslua: how Device failures reach the schedule, and what does not.
--
-- Demonstrates:
--   * a Device failure with no onError raises a Lua error, which ends the step as
--     Result.ERROR;
--   * an onError that returns a value makes the call continue with that value;
--   * a bad argument (a misspelled field) is a schedule bug, not a Device failure: it
--     never reaches onError, it is logged at ERROR, and the call returns false with the
--     message as its second result;
--   * the handler given to open is the fallback for that Device's read, write and close.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025
local deadPort = tonumber(os.getenv("ATE_DEAD_PORT")) or 1

return ScheduleParams("Manual: Device errors", "onError, a closed Device and a bad field")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A misspelled field"):setStepNo("1.")
      :setAction(function()
        local opened, why = ATE.open(DeviceType.TCP, { host = host, port = port, timeout = 1.0,
                                                     readTerm = "\n", writeTerm = "\n", buad = 9600 })
        ATE.report(opened == false and Result.PASS or Result.FAIL, "the open was refused: " .. why)
      end),

    -- Nothing is listening on deadPort: the open fails. This handler logs and returns nil,
    -- which means "no recovery", so ATE.open raises and the step records Result.ERROR.
    StepParams("A port nothing listens on"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, deadPort):setTimeout(0.5),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "nothing is listening on " .. opArgs.host .. ":" .. opArgs.port)
            return nil
          end)
        ATE.report(Result.PASS, "this line is never reached")
      end),

    -- A Device that is closed cannot be read. With no handler the call raises.
    StepParams("Read a closed Device"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.close(dev)
        ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(Result.PASS, "this line is never reached either")
      end),

    -- The same failure, handled: the step carries on with what onError returns.
    StepParams("Handle the closed Device"):setStepNo("4.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "the Device is gone: " .. opResult.err)
            return { data = nil, count = 0, partial = false }
          end)
        ATE.close(dev)
        local r = ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "recovered from reading a closed Device")
      end),
  })
```

Stop on Fail pauses the run at a `FAIL` or `ERROR` step so the Operator can choose `CONTINUE`,
`RETRY` or `ABORT`. Retry runs the action again from the start with POLL reset and blocking
inputs cleared; each Attempt is recorded and the step's result is the last Attempt's. There is
no Attempt number in Lua - keep your own count.

```lua:docs/manuals/examples/test-writer/retry.tslua
-- retry.tslua: Stop on Fail, and what a Retry does.
--
-- Demonstrates:
--   * setStopOnFail(true) is the default: a step ending FAIL or ERROR pauses the run and
--     the Operator chooses Continue, Retry or Abort.
--   * Retry runs the step's action again from the beginning. Each Attempt is recorded;
--     the step's result is its last Attempt's, and the Operator's choice is recorded too.
--   * ATE gives a step no way to read its own Attempt number, so the schedule keeps the
--     count itself in a local the action closes over.
local attempts = 0

return ScheduleParams("Manual: retry", "the first Attempt fails, the Retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Retry this one"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt " .. attempts .. ": the joint is cold", 0.2, 2.4)
        else
          ATE.report(Result.PASS, "attempt " .. attempts .. ": the joint is warm", 2.4, 2.4)
        end
      end),

    -- Only reached after the step passed, or after the Operator chose to continue.
    StepParams("Next check"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "the rail is steady", 5.0, 5.0)
      end),
  })
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

```lua:docs/manuals/examples/test-writer/led.tslua
-- led.tslua: the innermost Sub-schedule, included by channel.tslua.
local kv = ...
local channel = kv.channel or 1

return ScheduleParams("LED check", "the indicator LEDs of one channel")
  :setSchedule({
    StepParams("The LED lights"):setStepNo("1.")
      :setAction(function()
        ATE.report(Result.PASS, "channel " .. channel .. " LED is lit")
      end),
  })
```

## Modules and the `.atedef`

`require` is ATE's own. It resolves the name against the release's files, then its
dependencies, never against `package.path`, which ATE empties. An unlisted name is an error. A
module runs once per Test Run and its return value is cached. `ate.` is reserved for modules
bundled with ATE (`ate.devices`).

Checksums are SHA-256 of the raw bytes, lowercase hex. A mismatch is a WARNING, the file still
loads, and both checksums are recorded with the run. `ate definition update <file.atedef>`
computes each listed file's SHA-256 and writes it back over the old value, filling in any
`sha256` that is absent; the file is only rewritten if a checksum actually changed, so a
correct `.atedef` comes back byte for byte the same. A listed file it cannot read keeps its
current value and warns.

Every field a `.atedef` can carry:

| Field | Meaning |
| --- | --- |
| `format` | must be `1`; anything else makes the definition broken |
| `mediaNumber` | the release's identity; not a version |
| `name`, `version` | `version` must be valid semver 2.0.0; other releases name it in `depends` |
| `depends` | a list of `{"name": ..., "version": ...}`; resolved depth first, in declaration order |
| `files[]` | one entry per Lua file: `path`, `sha256` |
| `files[].path` | relative to the `.atedef` (or absolute), forward slashes on both platforms |
| `files[].hidden` | only on a `.tslua`: keeps it off the Run screen while still allowing it as a Sub-schedule |
| `files[].module` | only on a `.lua`: the name `require` uses. Otherwise the path derives it: `lib/rails.lua` is `lib.rails`. An absolute path, or one containing `..`, must state it |

Here is the definition the manual's own examples ship, exactly as it sits on disk - note
`channel.tslua` and `led.tslua` marked `hidden`, and `modules/limits.lua` given the module name
`board_limits`:

```json:docs/manuals/examples/test-writer/manual.atedef
{
  "format": 1,
  "mediaNumber": "MR-MANUAL-TW",
  "name": "Test writer manual examples",
  "version": "1.0.0",
  "files": [
    {
      "path": "first.tslua",
      "sha256": "a260d2b923819ff6f71804370d4afce9ac77269ec9d9cf9c4b0dae35b85e48c8"
    },
    {
      "path": "steps.tslua",
      "sha256": "2203cddc55bf0daf0e557d3ecf1b847e77bf8fa873385cea775e32f240c5af97"
    },
    {
      "path": "inputs.tslua",
      "sha256": "6a746ebad482542f81d5b846b65a57adb467942e5289d9fc7e76b9bdaabd22fc"
    },
    {
      "path": "actions.tslua",
      "sha256": "a15e9a9fa060f0e829cc2e047d2c0313a1a2203c7c33356e792f699d1b02c526"
    },
    {
      "path": "retry.tslua",
      "sha256": "5375d9870ad11af1553eeaa1d5fd27ce586c2a5cce78af6b4a493a029fee58c9"
    },
    {
      "path": "results.tslua",
      "sha256": "49f80374f637c13e34281140912335fcb583fa87865674def089b18188130ce7"
    },
    {
      "path": "main.tslua",
      "sha256": "fc768ae110764fb3a7a9a4a29fdd93df3beafc09b8fff6ea7fa71efa825964d4"
    },
    {
      "path": "channel.tslua",
      "hidden": true,
      "sha256": "1cecd8b3566e4783c86375ebcdac6b69eaae52f1ea74e32c495babf7009b6e63"
    },
    {
      "path": "led.tslua",
      "hidden": true,
      "sha256": "3477e6a5a1578108bd76687e13e211ca1c48b5bac3343160a4e86319f87f46f8"
    },
    {
      "path": "modules.tslua",
      "sha256": "f34234441c454b9e0559e778fb86c6f10086f2edd4248207953ce67f22fae386"
    },
    {
      "path": "lib/rails.lua",
      "sha256": "392e385ab81dfb814a7ddec0cb1c0799c97d714b3d97c2cfb3306e6ba6cde029"
    },
    {
      "path": "modules/limits.lua",
      "module": "board_limits",
      "sha256": "ea8a21a8a7bef16b0abaa9efcc07c8f08416b4a07b712ac35b9ffdbf8c31fd99"
    },
    {
      "path": "tcp.tslua",
      "sha256": "8439beda05cc96f5c6bd69629951fc2318a7526eb085fb2bb322fc711c2cb8c7"
    },
    {
      "path": "udp.tslua",
      "sha256": "324ce6659ab74bcea159fbe20d063925e5b5a88992b2ad1755e4d68625f461b4"
    },
    {
      "path": "serial.tslua",
      "sha256": "dd717a441db74b490b84b999f82a54b7e0d2b3b3185906ae83150562c7dcb768"
    },
    {
      "path": "visa.tslua",
      "sha256": "5c9d69646277814d6d0e134380ba32e6402ce4d9d3f4006dbe4f622bb107afb8"
    },
    {
      "path": "errors.tslua",
      "sha256": "66151279fc9e62462e80a75e273785fac0bb1c8f2a7bf0aeec62f312a1fb0fe2"
    },
    {
      "path": "left_open.tslua",
      "sha256": "a53f424fe15813bafdcdf74f0c7eb4e69b37bbbb5fa27d833b58eb7d1923b279"
    },
    {
      "path": "logging.tslua",
      "sha256": "ee6b7ee6f1a3b1ed15522544d93645c694dfd29d27d53188416c8e956170dd02"
    },
    {
      "path": "abort.tslua",
      "sha256": "b164d0057d61fa7d5f4092a2b47154dc4ca16dfd685640ef09338f4b862eb308"
    },
    {
      "path": "sandbox.tslua",
      "sha256": "e40b7d8eafd167b5d52f844957962b505f53009d791a38bb2729413462aaee99"
    },
    {
      "path": "forbidden.tslua",
      "sha256": "eae7b5e968792a6abdcf883ccde1e956fdf4f7a6c14e2d8aab8ff02c1fc8dd97"
    }
  ]
}
```

```lua:docs/manuals/examples/test-writer/modules.tslua
-- modules.tslua: sharing code with require, including ATE's own bundled modules.
--
-- Demonstrates:
--   * require finds only the files the Test Definition lists (and its dependencies'),
--     never anything on package.path;
--   * the module name is derived from a plain relative path, or given explicitly with the
--     "module" field;
--   * the reserved "ate." namespace is ATE's own: require("ate.devices") is always ATE's
--     Device params builder, never a file in the Media Release.
local rails = require("lib.rails")      -- derived name: the path without .lua, / as .
local limits = require("board_limits")  -- explicit "module" name in manual.atedef
local devices = require("ate.devices")  -- bundled with ATE

return ScheduleParams("Manual: modules", "require and the ate. namespace")
  :setSchedule({
    StepParams("Check every rail"):setStepNo("1.")
      :setAction(function()
        local ok = true
        for channel = 1, 3 do
          local volts = rails.nominalVolts(channel)
          ok = ok and volts >= limits.low and volts <= limits.high
          ATE.log(Log.INFO, "channel " .. channel .. " nominal " .. volts .. " V")
        end
        ATE.report(ok and Result.PASS or Result.FAIL,
                   "every rail is inside " .. limits.low .. " to " .. limits.high .. " V")
      end),

    -- A builder is just a params table: nothing is opened, so no Device is involved.
    StepParams("Build params without opening"):setStepNo("2.")
      :setAction(function()
        local params = devices.Tcp("127.0.0.1", 5025):setTimeout(2.5)
        ATE.report(Result.PASS, "timeout set to " .. params.timeout, params.timeout, 2.5)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/lib/rails.lua
-- lib/rails.lua: an ordinary Lua module. Its module name is derived from its path:
-- "lib/rails.lua" becomes "lib.rails". require runs a module once per Test Run and
-- caches what it returns; later requires of the same name get the cached value.
local rails = {}

function rails.nominalVolts(channel)
  return 1.5 * channel
end

return rails
```

```lua:docs/manuals/examples/test-writer/modules/limits.lua
-- modules/limits.lua: a module whose name is given explicitly in manual.atedef with the
-- "module" field, because a name derived from the path would be "modules.limits".
return { low = 1.0, high = 5.0 }
```

## Devices

`ATE.open(deviceType, params [, onError])` returns an opaque handle. Every field `open` needs
is required - there are no defaults in `ATE.open`; the builders supply them.

| Device Type | `open` fields | builder (defaults) |
| --- | --- | --- |
| `DeviceType.TCP` | `host`, `port`, `timeout`, `readTerm`, `writeTerm` | `devices.Tcp(host, port)`: timeout 1.0, terminators `"\n"` |
| `DeviceType.SERIAL` | `port`, `baud`, `dataBits`, `stopBits`, `parity`, `flowControl`, `timeout`, `readTerm`, `writeTerm` | `devices.Serial(port)`: 9600, 8, 1, `Parity.NONE`, `FlowControl.NONE`, 1.0, terminators `"\n"` |
| `DeviceType.UDP` | `host`, `port`, `localPort`, `timeout` | `devices.Udp(host, port)`: `localPort` 0, timeout 1.0 |
| `DeviceType.VISA` | `resource`, `timeout`, `readTerm`, `writeTerm` | `devices.Visa(resource)`: timeout 1.0, terminators `"\n"` |

Per-call fields, all optional, falling back to the open-time configuration: `read` takes
`size`, `timeout`, `readTerm`; `write` takes `data` (required), `writeTerm`, `timeout`;
`close` takes no fields of its own but accepts an optional params table and an optional
`onError` — `ATE.close(dev [, params] [, onError])` — which is used if the close itself fails.
UDP has no terminators: its `read` takes `size` and `timeout`, and its `write` takes `data`
(required) and `timeout`. Passing a `readTerm` to a UDP Device is an error naming the field.
Builder setters: `setHost`, `setPort`, `setLocalPort`, `setResource`, `setBaud`, `setDataBits`,
`setStopBits`, `setParity`, `setFlowControl`, `setTimeout`, `setReadTerm`, `setWriteTerm`,
`setSize`, and `devices.Write(data)`.

Reads and writes are raw bytes. The terminator is stripped from `data`; `count` is what is
left. A write's `count` includes the terminator.

TCP, SERIAL and UDP are read in the background from `open` to `close` and buffered, capped at
the Station config key `devices.receiveBufferBytes` (16777216, 16 MiB); on overflow ATE drops
the oldest bytes, logs one WARNING, and the next read carries `overflow = true`. `ATE.flush(dev)`
empties the buffer and returns the count discarded. VISA has no background reader and no buffer,
so `flush` discards nothing.

A VISA resource string names the instrument, and the shape matters:
`GPIB0::5::INSTR` is a GPIB instrument, `TCPIP0::192.168.1.10::inst0::INSTR` is a VXI-11
instrument, and `TCPIP0::127.0.0.1::5025::SOCKET` is a plain TCP socket wrapped in a VISA
session - the same stream a `DeviceType.TCP` Device would open, with pyvisa in the way. ATE
uses whichever pyvisa backend it finds; only pyvisa-py is shipped, which reaches TCPIP and
USBTMC. A GPIB instrument needs a real VISA driver on the Station (NI-VISA or linux-GPIB): ask
the schedule author which resource string their instrument needs before writing the schedule,
because nothing installed with ATE can open a GPIB address.

```lua:docs/manuals/examples/test-writer/tcp.tslua
-- tcp.tslua: a TCP Device, from open to close.
--
-- The host and port come from the environment, which the test harness sets to a
-- simulator peer listening on 127.0.0.1. Point them at a real instrument to use this
-- schedule as it is.
--
-- Demonstrates:
--   * the ate.devices builders, whose setters chain, and how a Device opened in the
--     Pre-test Action is still open in the steps, because a device object lives for the
--     whole Test Run;
--   * per-call overrides (timeout, readTerm, size) that do not change the open-time
--     configuration;
--   * a read that times out after some bytes is a partial read, not a Device failure;
--   * a read that times out with nothing at all is a Device failure and goes through
--     onError, which is given the failure table and the params table and may return a
--     value for the call to carry on with;
--   * flush discards whatever the receive buffer already holds.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

local instrument = nil

return ScheduleParams("Manual: TCP", "open, write, read, flush and close a TCP Device")
  :setStopOnFail(false)
  :setPreTestAction(function()
    -- devices.Tcp(host, port) fills in every field TCP needs: timeout 1.0 s,
    -- readTerm and writeTerm "\n". open has no defaults of its own, so a plain table
    -- would have to name them all.
    instrument = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(2.0))
    ATE.addEquipment("SCPI simulator", "SIM-TCP", "TCP " .. host .. ":" .. port, "2026-01-01")
  end)
  :setPostTestAction(function(passFail)
    ATE.close(instrument)   -- closing a second time does nothing and still reports ok
  end)
  :setSchedule({
    StepParams("Identify the instrument"):setStepNo("1.")
      :setAction(function()
        local sent = ATE.write(instrument, devices.Write("*IDN?"))
        local reply = ATE.read(instrument, devices.Read():setTimeout(1.0))
        ATE.report((sent.ok and reply.count > 0) and Result.PASS or Result.FAIL,
                   "reply: " .. (reply.data or "nothing"),
                   reply.count, 26)
      end),

    -- The peer answers "MEAS:VOLT?" with "1.234\n". readTerm "3" stops the read at the "3",
    -- so the first read takes "1.2" and the second takes the rest of the reply.
    StepParams("Stop a read at a terminator"):setStepNo("2.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MEAS:VOLT?"))
        local first = ATE.read(instrument, devices.Read():setReadTerm("3"))
        local rest = ATE.read(instrument)
        ATE.report((first.data == "1.2" and rest.data == "4") and Result.PASS or Result.FAIL,
                   "first read " .. (first.data or "nothing") .. ", second " .. (rest.data or "nothing"),
                   first.count + rest.count, 4)
      end),

    -- "PARTIAL?" is answered with bytes and no terminator: the read times out having taken
    -- some of them. That is partial = true, a WARNING in the log, and not a Device failure.
    StepParams("A read that times out partway"):setStepNo("3.")
      :setAction(function()
        ATE.write(instrument, devices.Write("PARTIAL?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3))
        ATE.report((r.partial and r.count > 0) and Result.PASS or Result.FAIL,
                   "got " .. r.count .. " bytes before the timeout",
                   tostring(r.partial), "true")
      end),

    -- "NEVER?" is answered with silence: the read times out with no bytes at all, which IS
    -- a Device failure, so onError runs. Returning a value makes the call continue with it.
    StepParams("Handle a read that times out"):setStepNo("4.")
      :setAction(function()
        ATE.write(instrument, devices.Write("NEVER?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3), function(opResult, opArgs)
          ATE.log(Log.WARNING, "no reply from the instrument: " .. opResult.err)
          return { data = nil, count = 0, partial = false }
        end)
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "the timeout was handled, so the step carried on")
      end),

    -- The peer sends three lines; the schedule reads the first and throws away the rest.
    StepParams("Discard the receive buffer"):setStepNo("5.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MULTI?"))
        ATE.read(instrument, devices.Read():setSize(7))
        local f = ATE.flush(instrument)
        ATE.report((f.ok and f.count > 0) and Result.PASS or Result.FAIL, "discarded " .. f.count .. " buffered bytes")
      end),
  })
```

```lua:docs/manuals/examples/test-writer/udp.tslua
-- udp.tslua: a UDP Device, where one datagram is one message.
--
-- Demonstrates:
--   * UDP has no terminators, so readTerm and writeTerm are not fields this Device Type
--     knows; passing one is a schedule bug that raises naming the field;
--   * a datagram longer than the size asked for is truncated and returned with partial =
--     true, which the log calls a truncation rather than a timeout;
--   * the receive buffer is a queue of whole datagrams, so a reply that arrives before the
--     schedule reads it is not lost.
local devices = require("ate.devices")

local host = os.getenv("ATE_UDP_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_UDP_PORT")) or 5026

return ScheduleParams("Manual: UDP", "send and read datagrams")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Echo one datagram"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("PING"))
        local reply = ATE.read(dev)
        ATE.report(reply.data == "ECHO:PING" and Result.PASS or Result.FAIL,
                   "the peer echoed " .. (reply.data or "nothing"),
                   reply.count, 9)
        ATE.close(dev)
      end),

    -- Ask for 10 bytes of a 40-byte datagram: ATE returns the first 10 and says partial.
    StepParams("Truncate a datagram"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("a datagram longer than the size asked for"))
        local r = ATE.read(dev, devices.Read():setSize(10))
        ATE.report((r.count == 10 and r.partial) and Result.PASS or Result.FAIL,
                   "read " .. r.count .. " of a longer datagram",
                   r.count, 10)
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/serial.tslua
-- serial.tslua: a SERIAL Device through pyserial.
--
-- The port here is pyserial's socket:// URL, which opens a TCP connection to the
-- simulator peer; a real station names a COM or tty port instead.
--
-- Demonstrates:
--   * open has no defaults, so every field is named;
--   * the Parity and FlowControl enums;
--   * the same connection read with the Device's own terminator.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: SERIAL", "open a serial port and read a reply")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Open the port"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.SERIAL, {
          port = "socket://" .. host .. ":" .. port,
          baud = 115200,
          dataBits = 8,
          stopBits = 1,
          parity = Parity.NONE,
          flowControl = FlowControl.NONE,
          timeout = 2.0,
          readTerm = "\n",
          writeTerm = "\n",
        })
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "serial reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/visa.tslua
-- visa.tslua: a VISA instrument through pyvisa.
--
-- VISA has no background reader: ATE reads the instrument only when the schedule asks,
-- because polling an SCPI instrument with reads that have nothing pending makes it log
-- "Query UNTERMINATED" (-420) and ties up the bus. ATE ships pyvisa-py; a VISA resource
-- string names the instrument, here the simulator peer as a plain TCPIP socket.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: VISA", "read an instrument through pyvisa")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Ask the instrument who it is"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.VISA,
          devices.Visa("TCPIP0::" .. host .. "::" .. port .. "::SOCKET"):setTimeout(2.0))
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        local f = ATE.flush(dev)   -- VISA has no receive buffer: this discards nothing
        ATE.log(Log.INFO, "flush discarded " .. f.count .. " bytes")
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "VISA reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

A handle lives for the whole run. Closing a closed Device does nothing and reports success;
using one is a failure through `onError`. At the end of every run ATE closes whatever is still
open and logs a WARNING for each.

```lua:docs/manuals/examples/test-writer/left_open.tslua
-- left_open.tslua: a Device the schedule never closes.
--
-- A Test Run closes every Device still open when it ends, and logs one WARNING naming
-- each one. That is why a Device opened in a Pre-test Action does not have to be closed
-- by hand - it is worth closing anyway, so the log stays clean.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: a Device left open", "ATE closes what the schedule forgets")
  :setSchedule({
    StepParams("Open and forget"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.report(Result.PASS, "opened a Device and left it open")
      end),
  })
```

## Device failures, Stop on Fail, Retry, Abort

`onError(opResult, opArgs)` is called when the operation fails. If it returns anything but
`nil`, the call returns that value and the schedule continues. If it returns `nil`, or there is
no handler, the call raises a Lua error and the step records `Result.ERROR`. A read that times
out having received some bytes is not a failure: it returns `partial = true` and logs a
WARNING. ATE never reconnects: close the Device and open it again with the same params.

A field the Device Type does not know, a missing required field, or a wrong-typed value is not a
Device failure. It never reaches `onError`; the call returns `false` with the message as its
second result and the traceback is logged at ERROR.

```lua:docs/manuals/examples/test-writer/errors.tslua
-- errors.tslua: how Device failures reach the schedule, and what does not.
--
-- Demonstrates:
--   * a Device failure with no onError raises a Lua error, which ends the step as
--     Result.ERROR;
--   * an onError that returns a value makes the call continue with that value;
--   * a bad argument (a misspelled field) is a schedule bug, not a Device failure: it
--     never reaches onError, it is logged at ERROR, and the call returns false with the
--     message as its second result;
--   * the handler given to open is the fallback for that Device's read, write and close.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025
local deadPort = tonumber(os.getenv("ATE_DEAD_PORT")) or 1

return ScheduleParams("Manual: Device errors", "onError, a closed Device and a bad field")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A misspelled field"):setStepNo("1.")
      :setAction(function()
        local opened, why = ATE.open(DeviceType.TCP, { host = host, port = port, timeout = 1.0,
                                                     readTerm = "\n", writeTerm = "\n", buad = 9600 })
        ATE.report(opened == false and Result.PASS or Result.FAIL, "the open was refused: " .. why)
      end),

    -- Nothing is listening on deadPort: the open fails. This handler logs and returns nil,
    -- which means "no recovery", so ATE.open raises and the step records Result.ERROR.
    StepParams("A port nothing listens on"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, deadPort):setTimeout(0.5),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "nothing is listening on " .. opArgs.host .. ":" .. opArgs.port)
            return nil
          end)
        ATE.report(Result.PASS, "this line is never reached")
      end),

    -- A Device that is closed cannot be read. With no handler the call raises.
    StepParams("Read a closed Device"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.close(dev)
        ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(Result.PASS, "this line is never reached either")
      end),

    -- The same failure, handled: the step carries on with what onError returns.
    StepParams("Handle the closed Device"):setStepNo("4.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "the Device is gone: " .. opResult.err)
            return { data = nil, count = 0, partial = false }
          end)
        ATE.close(dev)
        local r = ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "recovered from reading a closed Device")
      end),
  })
```

Stop on Fail pauses the run at a `FAIL` or `ERROR` step so the Operator can choose `CONTINUE`,
`RETRY` or `ABORT`. Retry runs the action again from the start with POLL reset and blocking
inputs cleared; each Attempt is recorded and the step's result is the last Attempt's. There is
no Attempt number in Lua - keep your own count.

```lua:docs/manuals/examples/test-writer/retry.tslua
-- retry.tslua: Stop on Fail, and what a Retry does.
--
-- Demonstrates:
--   * setStopOnFail(true) is the default: a step ending FAIL or ERROR pauses the run and
--     the Operator chooses Continue, Retry or Abort.
--   * Retry runs the step's action again from the beginning. Each Attempt is recorded;
--     the step's result is its last Attempt's, and the Operator's choice is recorded too.
--   * ATE gives a step no way to read its own Attempt number, so the schedule keeps the
--     count itself in a local the action closes over.
local attempts = 0

return ScheduleParams("Manual: retry", "the first Attempt fails, the Retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Retry this one"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt " .. attempts .. ": the joint is cold", 0.2, 2.4)
        else
          ATE.report(Result.PASS, "attempt " .. attempts .. ": the joint is warm", 2.4, 2.4)
        end
      end),

    -- Only reached after the step passed, or after the Operator chose to continue.
    StepParams("Next check"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "the rail is steady", 5.0, 5.0)
      end),
  })
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

## Modules and the `.atedef`

`require` is ATE's own. It resolves the name against the release's files, then its
dependencies, never against `package.path`, which ATE empties. An unlisted name is an error. A
module runs once per Test Run and its return value is cached. `ate.` is reserved for modules
bundled with ATE (`ate.devices`).

Checksums are SHA-256 of the raw bytes, lowercase hex. A mismatch is a WARNING, the file still
loads, and both checksums are recorded with the run. `ate definition update <file.atedef>`
computes each listed file's SHA-256 and writes it back over the old value, filling in any
`sha256` that is absent; the file is only rewritten if a checksum actually changed, so a
correct `.atedef` comes back byte for byte the same. A listed file it cannot read keeps its
current value and warns.

Every field a `.atedef` can carry:

| Field | Meaning |
| --- | --- |
| `format` | must be `1`; anything else makes the definition broken |
| `mediaNumber` | the release's identity; not a version |
| `name`, `version` | `version` must be valid semver 2.0.0; other releases name it in `depends` |
| `depends` | a list of `{"name": ..., "version": ...}`; resolved depth first, in declaration order |
| `files[]` | one entry per Lua file: `path`, `sha256` |
| `files[].path` | relative to the `.atedef` (or absolute), forward slashes on both platforms |
| `files[].hidden` | only on a `.tslua`: keeps it off the Run screen while still allowing it as a Sub-schedule |
| `files[].module` | only on a `.lua`: the name `require` uses. Otherwise the path derives it: `lib/rails.lua` is `lib.rails`. An absolute path, or one containing `..`, must state it |

Here is the definition the manual's own examples ship, exactly as it sits on disk - note
`channel.tslua` and `led.tslua` marked `hidden`, and `modules/limits.lua` given the module name
`board_limits`:

```json:docs/manuals/examples/test-writer/manual.atedef
{
  "format": 1,
  "mediaNumber": "MR-MANUAL-TW",
  "name": "Test writer manual examples",
  "version": "1.0.0",
  "files": [
    {
      "path": "first.tslua",
      "sha256": "a260d2b923819ff6f71804370d4afce9ac77269ec9d9cf9c4b0dae35b85e48c8"
    },
    {
      "path": "steps.tslua",
      "sha256": "2203cddc55bf0daf0e557d3ecf1b847e77bf8fa873385cea775e32f240c5af97"
    },
    {
      "path": "inputs.tslua",
      "sha256": "6a746ebad482542f81d5b846b65a57adb467942e5289d9fc7e76b9bdaabd22fc"
    },
    {
      "path": "actions.tslua",
      "sha256": "a15e9a9fa060f0e829cc2e047d2c0313a1a2203c7c33356e792f699d1b02c526"
    },
    {
      "path": "retry.tslua",
      "sha256": "5375d9870ad11af1553eeaa1d5fd27ce586c2a5cce78af6b4a493a029fee58c9"
    },
    {
      "path": "results.tslua",
      "sha256": "49f80374f637c13e34281140912335fcb583fa87865674def089b18188130ce7"
    },
    {
      "path": "main.tslua",
      "sha256": "fc768ae110764fb3a7a9a4a29fdd93df3beafc09b8fff6ea7fa71efa825964d4"
    },
    {
      "path": "channel.tslua",
      "hidden": true,
      "sha256": "1cecd8b3566e4783c86375ebcdac6b69eaae52f1ea74e32c495babf7009b6e63"
    },
    {
      "path": "led.tslua",
      "hidden": true,
      "sha256": "3477e6a5a1578108bd76687e13e211ca1c48b5bac3343160a4e86319f87f46f8"
    },
    {
      "path": "modules.tslua",
      "sha256": "f34234441c454b9e0559e778fb86c6f10086f2edd4248207953ce67f22fae386"
    },
    {
      "path": "lib/rails.lua",
      "sha256": "392e385ab81dfb814a7ddec0cb1c0799c97d714b3d97c2cfb3306e6ba6cde029"
    },
    {
      "path": "modules/limits.lua",
      "module": "board_limits",
      "sha256": "ea8a21a8a7bef16b0abaa9efcc07c8f08416b4a07b712ac35b9ffdbf8c31fd99"
    },
    {
      "path": "tcp.tslua",
      "sha256": "8439beda05cc96f5c6bd69629951fc2318a7526eb085fb2bb322fc711c2cb8c7"
    },
    {
      "path": "udp.tslua",
      "sha256": "324ce6659ab74bcea159fbe20d063925e5b5a88992b2ad1755e4d68625f461b4"
    },
    {
      "path": "serial.tslua",
      "sha256": "dd717a441db74b490b84b999f82a54b7e0d2b3b3185906ae83150562c7dcb768"
    },
    {
      "path": "visa.tslua",
      "sha256": "5c9d69646277814d6d0e134380ba32e6402ce4d9d3f4006dbe4f622bb107afb8"
    },
    {
      "path": "errors.tslua",
      "sha256": "66151279fc9e62462e80a75e273785fac0bb1c8f2a7bf0aeec62f312a1fb0fe2"
    },
    {
      "path": "left_open.tslua",
      "sha256": "a53f424fe15813bafdcdf74f0c7eb4e69b37bbbb5fa27d833b58eb7d1923b279"
    },
    {
      "path": "logging.tslua",
      "sha256": "ee6b7ee6f1a3b1ed15522544d93645c694dfd29d27d53188416c8e956170dd02"
    },
    {
      "path": "abort.tslua",
      "sha256": "b164d0057d61fa7d5f4092a2b47154dc4ca16dfd685640ef09338f4b862eb308"
    },
    {
      "path": "sandbox.tslua",
      "sha256": "e40b7d8eafd167b5d52f844957962b505f53009d791a38bb2729413462aaee99"
    },
    {
      "path": "forbidden.tslua",
      "sha256": "eae7b5e968792a6abdcf883ccde1e956fdf4f7a6c14e2d8aab8ff02c1fc8dd97"
    }
  ]
}
```

```lua:docs/manuals/examples/test-writer/modules.tslua
-- modules.tslua: sharing code with require, including ATE's own bundled modules.
--
-- Demonstrates:
--   * require finds only the files the Test Definition lists (and its dependencies'),
--     never anything on package.path;
--   * the module name is derived from a plain relative path, or given explicitly with the
--     "module" field;
--   * the reserved "ate." namespace is ATE's own: require("ate.devices") is always ATE's
--     Device params builder, never a file in the Media Release.
local rails = require("lib.rails")      -- derived name: the path without .lua, / as .
local limits = require("board_limits")  -- explicit "module" name in manual.atedef
local devices = require("ate.devices")  -- bundled with ATE

return ScheduleParams("Manual: modules", "require and the ate. namespace")
  :setSchedule({
    StepParams("Check every rail"):setStepNo("1.")
      :setAction(function()
        local ok = true
        for channel = 1, 3 do
          local volts = rails.nominalVolts(channel)
          ok = ok and volts >= limits.low and volts <= limits.high
          ATE.log(Log.INFO, "channel " .. channel .. " nominal " .. volts .. " V")
        end
        ATE.report(ok and Result.PASS or Result.FAIL,
                   "every rail is inside " .. limits.low .. " to " .. limits.high .. " V")
      end),

    -- A builder is just a params table: nothing is opened, so no Device is involved.
    StepParams("Build params without opening"):setStepNo("2.")
      :setAction(function()
        local params = devices.Tcp("127.0.0.1", 5025):setTimeout(2.5)
        ATE.report(Result.PASS, "timeout set to " .. params.timeout, params.timeout, 2.5)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/lib/rails.lua
-- lib/rails.lua: an ordinary Lua module. Its module name is derived from its path:
-- "lib/rails.lua" becomes "lib.rails". require runs a module once per Test Run and
-- caches what it returns; later requires of the same name get the cached value.
local rails = {}

function rails.nominalVolts(channel)
  return 1.5 * channel
end

return rails
```

```lua:docs/manuals/examples/test-writer/modules/limits.lua
-- modules/limits.lua: a module whose name is given explicitly in manual.atedef with the
-- "module" field, because a name derived from the path would be "modules.limits".
return { low = 1.0, high = 5.0 }
```

## Devices

`ATE.open(deviceType, params [, onError])` returns an opaque handle. Every field `open` needs
is required - there are no defaults in `ATE.open`; the builders supply them.

| Device Type | `open` fields | builder (defaults) |
| --- | --- | --- |
| `DeviceType.TCP` | `host`, `port`, `timeout`, `readTerm`, `writeTerm` | `devices.Tcp(host, port)`: timeout 1.0, terminators `"\n"` |
| `DeviceType.SERIAL` | `port`, `baud`, `dataBits`, `stopBits`, `parity`, `flowControl`, `timeout`, `readTerm`, `writeTerm` | `devices.Serial(port)`: 9600, 8, 1, `Parity.NONE`, `FlowControl.NONE`, 1.0, terminators `"\n"` |
| `DeviceType.UDP` | `host`, `port`, `localPort`, `timeout` | `devices.Udp(host, port)`: `localPort` 0, timeout 1.0 |
| `DeviceType.VISA` | `resource`, `timeout`, `readTerm`, `writeTerm` | `devices.Visa(resource)`: timeout 1.0, terminators `"\n"` |

Per-call fields, all optional, falling back to the open-time configuration: `read` takes
`size`, `timeout`, `readTerm`; `write` takes `data` (required), `writeTerm`, `timeout`;
`close` takes no fields of its own but accepts an optional params table and an optional
`onError` — `ATE.close(dev [, params] [, onError])` — which is used if the close itself fails.
UDP has no terminators: its `read` takes `size` and `timeout`, and its `write` takes `data`
(required) and `timeout`. Passing a `readTerm` to a UDP Device is an error naming the field.
Builder setters: `setHost`, `setPort`, `setLocalPort`, `setResource`, `setBaud`, `setDataBits`,
`setStopBits`, `setParity`, `setFlowControl`, `setTimeout`, `setReadTerm`, `setWriteTerm`,
`setSize`, and `devices.Write(data)`.

Reads and writes are raw bytes. The terminator is stripped from `data`; `count` is what is
left. A write's `count` includes the terminator.

TCP, SERIAL and UDP are read in the background from `open` to `close` and buffered, capped at
the Station config key `devices.receiveBufferBytes` (16777216, 16 MiB); on overflow ATE drops
the oldest bytes, logs one WARNING, and the next read carries `overflow = true`. `ATE.flush(dev)`
empties the buffer and returns the count discarded. VISA has no background reader and no buffer,
so `flush` discards nothing.

A VISA resource string names the instrument, and the shape matters:
`GPIB0::5::INSTR` is a GPIB instrument, `TCPIP0::192.168.1.10::inst0::INSTR` is a VXI-11
instrument, and `TCPIP0::127.0.0.1::5025::SOCKET` is a plain TCP socket wrapped in a VISA
session - the same stream a `DeviceType.TCP` Device would open, with pyvisa in the way. ATE
uses whichever pyvisa backend it finds; only pyvisa-py is shipped, which reaches TCPIP and
USBTMC. A GPIB instrument needs a real VISA driver on the Station (NI-VISA or linux-GPIB): ask
the schedule author which resource string their instrument needs before writing the schedule,
because nothing installed with ATE can open a GPIB address.

```lua:docs/manuals/examples/test-writer/tcp.tslua
-- tcp.tslua: a TCP Device, from open to close.
--
-- The host and port come from the environment, which the test harness sets to a
-- simulator peer listening on 127.0.0.1. Point them at a real instrument to use this
-- schedule as it is.
--
-- Demonstrates:
--   * the ate.devices builders, whose setters chain, and how a Device opened in the
--     Pre-test Action is still open in the steps, because a device object lives for the
--     whole Test Run;
--   * per-call overrides (timeout, readTerm, size) that do not change the open-time
--     configuration;
--   * a read that times out after some bytes is a partial read, not a Device failure;
--   * a read that times out with nothing at all is a Device failure and goes through
--     onError, which is given the failure table and the params table and may return a
--     value for the call to carry on with;
--   * flush discards whatever the receive buffer already holds.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

local instrument = nil

return ScheduleParams("Manual: TCP", "open, write, read, flush and close a TCP Device")
  :setStopOnFail(false)
  :setPreTestAction(function()
    -- devices.Tcp(host, port) fills in every field TCP needs: timeout 1.0 s,
    -- readTerm and writeTerm "\n". open has no defaults of its own, so a plain table
    -- would have to name them all.
    instrument = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(2.0))
    ATE.addEquipment("SCPI simulator", "SIM-TCP", "TCP " .. host .. ":" .. port, "2026-01-01")
  end)
  :setPostTestAction(function(passFail)
    ATE.close(instrument)   -- closing a second time does nothing and still reports ok
  end)
  :setSchedule({
    StepParams("Identify the instrument"):setStepNo("1.")
      :setAction(function()
        local sent = ATE.write(instrument, devices.Write("*IDN?"))
        local reply = ATE.read(instrument, devices.Read():setTimeout(1.0))
        ATE.report((sent.ok and reply.count > 0) and Result.PASS or Result.FAIL,
                   "reply: " .. (reply.data or "nothing"),
                   reply.count, 26)
      end),

    -- The peer answers "MEAS:VOLT?" with "1.234\n". readTerm "3" stops the read at the "3",
    -- so the first read takes "1.2" and the second takes the rest of the reply.
    StepParams("Stop a read at a terminator"):setStepNo("2.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MEAS:VOLT?"))
        local first = ATE.read(instrument, devices.Read():setReadTerm("3"))
        local rest = ATE.read(instrument)
        ATE.report((first.data == "1.2" and rest.data == "4") and Result.PASS or Result.FAIL,
                   "first read " .. (first.data or "nothing") .. ", second " .. (rest.data or "nothing"),
                   first.count + rest.count, 4)
      end),

    -- "PARTIAL?" is answered with bytes and no terminator: the read times out having taken
    -- some of them. That is partial = true, a WARNING in the log, and not a Device failure.
    StepParams("A read that times out partway"):setStepNo("3.")
      :setAction(function()
        ATE.write(instrument, devices.Write("PARTIAL?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3))
        ATE.report((r.partial and r.count > 0) and Result.PASS or Result.FAIL,
                   "got " .. r.count .. " bytes before the timeout",
                   tostring(r.partial), "true")
      end),

    -- "NEVER?" is answered with silence: the read times out with no bytes at all, which IS
    -- a Device failure, so onError runs. Returning a value makes the call continue with it.
    StepParams("Handle a read that times out"):setStepNo("4.")
      :setAction(function()
        ATE.write(instrument, devices.Write("NEVER?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3), function(opResult, opArgs)
          ATE.log(Log.WARNING, "no reply from the instrument: " .. opResult.err)
          return { data = nil, count = 0, partial = false }
        end)
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "the timeout was handled, so the step carried on")
      end),

    -- The peer sends three lines; the schedule reads the first and throws away the rest.
    StepParams("Discard the receive buffer"):setStepNo("5.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MULTI?"))
        ATE.read(instrument, devices.Read():setSize(7))
        local f = ATE.flush(instrument)
        ATE.report((f.ok and f.count > 0) and Result.PASS or Result.FAIL, "discarded " .. f.count .. " buffered bytes")
      end),
  })
```

```lua:docs/manuals/examples/test-writer/udp.tslua
-- udp.tslua: a UDP Device, where one datagram is one message.
--
-- Demonstrates:
--   * UDP has no terminators, so readTerm and writeTerm are not fields this Device Type
--     knows; passing one is a schedule bug that raises naming the field;
--   * a datagram longer than the size asked for is truncated and returned with partial =
--     true, which the log calls a truncation rather than a timeout;
--   * the receive buffer is a queue of whole datagrams, so a reply that arrives before the
--     schedule reads it is not lost.
local devices = require("ate.devices")

local host = os.getenv("ATE_UDP_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_UDP_PORT")) or 5026

return ScheduleParams("Manual: UDP", "send and read datagrams")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Echo one datagram"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("PING"))
        local reply = ATE.read(dev)
        ATE.report(reply.data == "ECHO:PING" and Result.PASS or Result.FAIL,
                   "the peer echoed " .. (reply.data or "nothing"),
                   reply.count, 9)
        ATE.close(dev)
      end),

    -- Ask for 10 bytes of a 40-byte datagram: ATE returns the first 10 and says partial.
    StepParams("Truncate a datagram"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("a datagram longer than the size asked for"))
        local r = ATE.read(dev, devices.Read():setSize(10))
        ATE.report((r.count == 10 and r.partial) and Result.PASS or Result.FAIL,
                   "read " .. r.count .. " of a longer datagram",
                   r.count, 10)
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/serial.tslua
-- serial.tslua: a SERIAL Device through pyserial.
--
-- The port here is pyserial's socket:// URL, which opens a TCP connection to the
-- simulator peer; a real station names a COM or tty port instead.
--
-- Demonstrates:
--   * open has no defaults, so every field is named;
--   * the Parity and FlowControl enums;
--   * the same connection read with the Device's own terminator.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: SERIAL", "open a serial port and read a reply")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Open the port"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.SERIAL, {
          port = "socket://" .. host .. ":" .. port,
          baud = 115200,
          dataBits = 8,
          stopBits = 1,
          parity = Parity.NONE,
          flowControl = FlowControl.NONE,
          timeout = 2.0,
          readTerm = "\n",
          writeTerm = "\n",
        })
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "serial reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/visa.tslua
-- visa.tslua: a VISA instrument through pyvisa.
--
-- VISA has no background reader: ATE reads the instrument only when the schedule asks,
-- because polling an SCPI instrument with reads that have nothing pending makes it log
-- "Query UNTERMINATED" (-420) and ties up the bus. ATE ships pyvisa-py; a VISA resource
-- string names the instrument, here the simulator peer as a plain TCPIP socket.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: VISA", "read an instrument through pyvisa")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Ask the instrument who it is"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.VISA,
          devices.Visa("TCPIP0::" .. host .. "::" .. port .. "::SOCKET"):setTimeout(2.0))
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        local f = ATE.flush(dev)   -- VISA has no receive buffer: this discards nothing
        ATE.log(Log.INFO, "flush discarded " .. f.count .. " bytes")
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "VISA reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

A handle lives for the whole run. Closing a closed Device does nothing and reports success;
using one is a failure through `onError`. At the end of every run ATE closes whatever is still
open and logs a WARNING for each.

```lua:docs/manuals/examples/test-writer/left_open.tslua
-- left_open.tslua: a Device the schedule never closes.
--
-- A Test Run closes every Device still open when it ends, and logs one WARNING naming
-- each one. That is why a Device opened in a Pre-test Action does not have to be closed
-- by hand - it is worth closing anyway, so the log stays clean.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: a Device left open", "ATE closes what the schedule forgets")
  :setSchedule({
    StepParams("Open and forget"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.report(Result.PASS, "opened a Device and left it open")
      end),
  })
```

## Device failures, Stop on Fail, Retry, Abort

`onError(opResult, opArgs)` is called when the operation fails. If it returns anything but
`nil`, the call returns that value and the schedule continues. If it returns `nil`, or there is
no handler, the call raises a Lua error and the step records `Result.ERROR`. A read that times
out having received some bytes is not a failure: it returns `partial = true` and logs a
WARNING. ATE never reconnects: close the Device and open it again with the same params.

A field the Device Type does not know, a missing required field, or a wrong-typed value is not a
Device failure. It never reaches `onError`; the call returns `false` with the message as its
second result and the traceback is logged at ERROR.

```lua:docs/manuals/examples/test-writer/errors.tslua
-- errors.tslua: how Device failures reach the schedule, and what does not.
--
-- Demonstrates:
--   * a Device failure with no onError raises a Lua error, which ends the step as
--     Result.ERROR;
--   * an onError that returns a value makes the call continue with that value;
--   * a bad argument (a misspelled field) is a schedule bug, not a Device failure: it
--     never reaches onError, it is logged at ERROR, and the call returns false with the
--     message as its second result;
--   * the handler given to open is the fallback for that Device's read, write and close.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025
local deadPort = tonumber(os.getenv("ATE_DEAD_PORT")) or 1

return ScheduleParams("Manual: Device errors", "onError, a closed Device and a bad field")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A misspelled field"):setStepNo("1.")
      :setAction(function()
        local opened, why = ATE.open(DeviceType.TCP, { host = host, port = port, timeout = 1.0,
                                                     readTerm = "\n", writeTerm = "\n", buad = 9600 })
        ATE.report(opened == false and Result.PASS or Result.FAIL, "the open was refused: " .. why)
      end),

    -- Nothing is listening on deadPort: the open fails. This handler logs and returns nil,
    -- which means "no recovery", so ATE.open raises and the step records Result.ERROR.
    StepParams("A port nothing listens on"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, deadPort):setTimeout(0.5),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "nothing is listening on " .. opArgs.host .. ":" .. opArgs.port)
            return nil
          end)
        ATE.report(Result.PASS, "this line is never reached")
      end),

    -- A Device that is closed cannot be read. With no handler the call raises.
    StepParams("Read a closed Device"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.close(dev)
        ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(Result.PASS, "this line is never reached either")
      end),

    -- The same failure, handled: the step carries on with what onError returns.
    StepParams("Handle the closed Device"):setStepNo("4.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "the Device is gone: " .. opResult.err)
            return { data = nil, count = 0, partial = false }
          end)
        ATE.close(dev)
        local r = ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "recovered from reading a closed Device")
      end),
  })
```

Stop on Fail pauses the run at a `FAIL` or `ERROR` step so the Operator can choose `CONTINUE`,
`RETRY` or `ABORT`. Retry runs the action again from the start with POLL reset and blocking
inputs cleared; each Attempt is recorded and the step's result is the last Attempt's. There is
no Attempt number in Lua - keep your own count.

```lua:docs/manuals/examples/test-writer/retry.tslua
-- retry.tslua: Stop on Fail, and what a Retry does.
--
-- Demonstrates:
--   * setStopOnFail(true) is the default: a step ending FAIL or ERROR pauses the run and
--     the Operator chooses Continue, Retry or Abort.
--   * Retry runs the step's action again from the beginning. Each Attempt is recorded;
--     the step's result is its last Attempt's, and the Operator's choice is recorded too.
--   * ATE gives a step no way to read its own Attempt number, so the schedule keeps the
--     count itself in a local the action closes over.
local attempts = 0

return ScheduleParams("Manual: retry", "the first Attempt fails, the Retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Retry this one"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt " .. attempts .. ": the joint is cold", 0.2, 2.4)
        else
          ATE.report(Result.PASS, "attempt " .. attempts .. ": the joint is warm", 2.4, 2.4)
        end
      end),

    -- Only reached after the step passed, or after the Operator chose to continue.
    StepParams("Next check"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "the rail is steady", 5.0, 5.0)
      end),
  })
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

```lua:docs/manuals/examples/test-writer/modules.tslua
-- modules.tslua: sharing code with require, including ATE's own bundled modules.
--
-- Demonstrates:
--   * require finds only the files the Test Definition lists (and its dependencies'),
--     never anything on package.path;
--   * the module name is derived from a plain relative path, or given explicitly with the
--     "module" field;
--   * the reserved "ate." namespace is ATE's own: require("ate.devices") is always ATE's
--     Device params builder, never a file in the Media Release.
local rails = require("lib.rails")      -- derived name: the path without .lua, / as .
local limits = require("board_limits")  -- explicit "module" name in manual.atedef
local devices = require("ate.devices")  -- bundled with ATE

return ScheduleParams("Manual: modules", "require and the ate. namespace")
  :setSchedule({
    StepParams("Check every rail"):setStepNo("1.")
      :setAction(function()
        local ok = true
        for channel = 1, 3 do
          local volts = rails.nominalVolts(channel)
          ok = ok and volts >= limits.low and volts <= limits.high
          ATE.log(Log.INFO, "channel " .. channel .. " nominal " .. volts .. " V")
        end
        ATE.report(ok and Result.PASS or Result.FAIL,
                   "every rail is inside " .. limits.low .. " to " .. limits.high .. " V")
      end),

    -- A builder is just a params table: nothing is opened, so no Device is involved.
    StepParams("Build params without opening"):setStepNo("2.")
      :setAction(function()
        local params = devices.Tcp("127.0.0.1", 5025):setTimeout(2.5)
        ATE.report(Result.PASS, "timeout set to " .. params.timeout, params.timeout, 2.5)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/lib/rails.lua
-- lib/rails.lua: an ordinary Lua module. Its module name is derived from its path:
-- "lib/rails.lua" becomes "lib.rails". require runs a module once per Test Run and
-- caches what it returns; later requires of the same name get the cached value.
local rails = {}

function rails.nominalVolts(channel)
  return 1.5 * channel
end

return rails
```

```lua:docs/manuals/examples/test-writer/modules/limits.lua
-- modules/limits.lua: a module whose name is given explicitly in manual.atedef with the
-- "module" field, because a name derived from the path would be "modules.limits".
return { low = 1.0, high = 5.0 }
```

## Devices

`ATE.open(deviceType, params [, onError])` returns an opaque handle. Every field `open` needs
is required - there are no defaults in `ATE.open`; the builders supply them.

| Device Type | `open` fields | builder (defaults) |
| --- | --- | --- |
| `DeviceType.TCP` | `host`, `port`, `timeout`, `readTerm`, `writeTerm` | `devices.Tcp(host, port)`: timeout 1.0, terminators `"\n"` |
| `DeviceType.SERIAL` | `port`, `baud`, `dataBits`, `stopBits`, `parity`, `flowControl`, `timeout`, `readTerm`, `writeTerm` | `devices.Serial(port)`: 9600, 8, 1, `Parity.NONE`, `FlowControl.NONE`, 1.0, terminators `"\n"` |
| `DeviceType.UDP` | `host`, `port`, `localPort`, `timeout` | `devices.Udp(host, port)`: `localPort` 0, timeout 1.0 |
| `DeviceType.VISA` | `resource`, `timeout`, `readTerm`, `writeTerm` | `devices.Visa(resource)`: timeout 1.0, terminators `"\n"` |

Per-call fields, all optional, falling back to the open-time configuration: `read` takes
`size`, `timeout`, `readTerm`; `write` takes `data` (required), `writeTerm`, `timeout`;
`close` takes no fields of its own but accepts an optional params table and an optional
`onError` — `ATE.close(dev [, params] [, onError])` — which is used if the close itself fails.
UDP has no terminators: its `read` takes `size` and `timeout`, and its `write` takes `data`
(required) and `timeout`. Passing a `readTerm` to a UDP Device is an error naming the field.
Builder setters: `setHost`, `setPort`, `setLocalPort`, `setResource`, `setBaud`, `setDataBits`,
`setStopBits`, `setParity`, `setFlowControl`, `setTimeout`, `setReadTerm`, `setWriteTerm`,
`setSize`, and `devices.Write(data)`.

Reads and writes are raw bytes. The terminator is stripped from `data`; `count` is what is
left. A write's `count` includes the terminator.

TCP, SERIAL and UDP are read in the background from `open` to `close` and buffered, capped at
the Station config key `devices.receiveBufferBytes` (16777216, 16 MiB); on overflow ATE drops
the oldest bytes, logs one WARNING, and the next read carries `overflow = true`. `ATE.flush(dev)`
empties the buffer and returns the count discarded. VISA has no background reader and no buffer,
so `flush` discards nothing.

A VISA resource string names the instrument, and the shape matters:
`GPIB0::5::INSTR` is a GPIB instrument, `TCPIP0::192.168.1.10::inst0::INSTR` is a VXI-11
instrument, and `TCPIP0::127.0.0.1::5025::SOCKET` is a plain TCP socket wrapped in a VISA
session - the same stream a `DeviceType.TCP` Device would open, with pyvisa in the way. ATE
uses whichever pyvisa backend it finds; only pyvisa-py is shipped, which reaches TCPIP and
USBTMC. A GPIB instrument needs a real VISA driver on the Station (NI-VISA or linux-GPIB): ask
the schedule author which resource string their instrument needs before writing the schedule,
because nothing installed with ATE can open a GPIB address.

```lua:docs/manuals/examples/test-writer/tcp.tslua
-- tcp.tslua: a TCP Device, from open to close.
--
-- The host and port come from the environment, which the test harness sets to a
-- simulator peer listening on 127.0.0.1. Point them at a real instrument to use this
-- schedule as it is.
--
-- Demonstrates:
--   * the ate.devices builders, whose setters chain, and how a Device opened in the
--     Pre-test Action is still open in the steps, because a device object lives for the
--     whole Test Run;
--   * per-call overrides (timeout, readTerm, size) that do not change the open-time
--     configuration;
--   * a read that times out after some bytes is a partial read, not a Device failure;
--   * a read that times out with nothing at all is a Device failure and goes through
--     onError, which is given the failure table and the params table and may return a
--     value for the call to carry on with;
--   * flush discards whatever the receive buffer already holds.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

local instrument = nil

return ScheduleParams("Manual: TCP", "open, write, read, flush and close a TCP Device")
  :setStopOnFail(false)
  :setPreTestAction(function()
    -- devices.Tcp(host, port) fills in every field TCP needs: timeout 1.0 s,
    -- readTerm and writeTerm "\n". open has no defaults of its own, so a plain table
    -- would have to name them all.
    instrument = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(2.0))
    ATE.addEquipment("SCPI simulator", "SIM-TCP", "TCP " .. host .. ":" .. port, "2026-01-01")
  end)
  :setPostTestAction(function(passFail)
    ATE.close(instrument)   -- closing a second time does nothing and still reports ok
  end)
  :setSchedule({
    StepParams("Identify the instrument"):setStepNo("1.")
      :setAction(function()
        local sent = ATE.write(instrument, devices.Write("*IDN?"))
        local reply = ATE.read(instrument, devices.Read():setTimeout(1.0))
        ATE.report((sent.ok and reply.count > 0) and Result.PASS or Result.FAIL,
                   "reply: " .. (reply.data or "nothing"),
                   reply.count, 26)
      end),

    -- The peer answers "MEAS:VOLT?" with "1.234\n". readTerm "3" stops the read at the "3",
    -- so the first read takes "1.2" and the second takes the rest of the reply.
    StepParams("Stop a read at a terminator"):setStepNo("2.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MEAS:VOLT?"))
        local first = ATE.read(instrument, devices.Read():setReadTerm("3"))
        local rest = ATE.read(instrument)
        ATE.report((first.data == "1.2" and rest.data == "4") and Result.PASS or Result.FAIL,
                   "first read " .. (first.data or "nothing") .. ", second " .. (rest.data or "nothing"),
                   first.count + rest.count, 4)
      end),

    -- "PARTIAL?" is answered with bytes and no terminator: the read times out having taken
    -- some of them. That is partial = true, a WARNING in the log, and not a Device failure.
    StepParams("A read that times out partway"):setStepNo("3.")
      :setAction(function()
        ATE.write(instrument, devices.Write("PARTIAL?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3))
        ATE.report((r.partial and r.count > 0) and Result.PASS or Result.FAIL,
                   "got " .. r.count .. " bytes before the timeout",
                   tostring(r.partial), "true")
      end),

    -- "NEVER?" is answered with silence: the read times out with no bytes at all, which IS
    -- a Device failure, so onError runs. Returning a value makes the call continue with it.
    StepParams("Handle a read that times out"):setStepNo("4.")
      :setAction(function()
        ATE.write(instrument, devices.Write("NEVER?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3), function(opResult, opArgs)
          ATE.log(Log.WARNING, "no reply from the instrument: " .. opResult.err)
          return { data = nil, count = 0, partial = false }
        end)
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "the timeout was handled, so the step carried on")
      end),

    -- The peer sends three lines; the schedule reads the first and throws away the rest.
    StepParams("Discard the receive buffer"):setStepNo("5.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MULTI?"))
        ATE.read(instrument, devices.Read():setSize(7))
        local f = ATE.flush(instrument)
        ATE.report((f.ok and f.count > 0) and Result.PASS or Result.FAIL, "discarded " .. f.count .. " buffered bytes")
      end),
  })
```

```lua:docs/manuals/examples/test-writer/udp.tslua
-- udp.tslua: a UDP Device, where one datagram is one message.
--
-- Demonstrates:
--   * UDP has no terminators, so readTerm and writeTerm are not fields this Device Type
--     knows; passing one is a schedule bug that raises naming the field;
--   * a datagram longer than the size asked for is truncated and returned with partial =
--     true, which the log calls a truncation rather than a timeout;
--   * the receive buffer is a queue of whole datagrams, so a reply that arrives before the
--     schedule reads it is not lost.
local devices = require("ate.devices")

local host = os.getenv("ATE_UDP_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_UDP_PORT")) or 5026

return ScheduleParams("Manual: UDP", "send and read datagrams")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Echo one datagram"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("PING"))
        local reply = ATE.read(dev)
        ATE.report(reply.data == "ECHO:PING" and Result.PASS or Result.FAIL,
                   "the peer echoed " .. (reply.data or "nothing"),
                   reply.count, 9)
        ATE.close(dev)
      end),

    -- Ask for 10 bytes of a 40-byte datagram: ATE returns the first 10 and says partial.
    StepParams("Truncate a datagram"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("a datagram longer than the size asked for"))
        local r = ATE.read(dev, devices.Read():setSize(10))
        ATE.report((r.count == 10 and r.partial) and Result.PASS or Result.FAIL,
                   "read " .. r.count .. " of a longer datagram",
                   r.count, 10)
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/serial.tslua
-- serial.tslua: a SERIAL Device through pyserial.
--
-- The port here is pyserial's socket:// URL, which opens a TCP connection to the
-- simulator peer; a real station names a COM or tty port instead.
--
-- Demonstrates:
--   * open has no defaults, so every field is named;
--   * the Parity and FlowControl enums;
--   * the same connection read with the Device's own terminator.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: SERIAL", "open a serial port and read a reply")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Open the port"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.SERIAL, {
          port = "socket://" .. host .. ":" .. port,
          baud = 115200,
          dataBits = 8,
          stopBits = 1,
          parity = Parity.NONE,
          flowControl = FlowControl.NONE,
          timeout = 2.0,
          readTerm = "\n",
          writeTerm = "\n",
        })
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "serial reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/visa.tslua
-- visa.tslua: a VISA instrument through pyvisa.
--
-- VISA has no background reader: ATE reads the instrument only when the schedule asks,
-- because polling an SCPI instrument with reads that have nothing pending makes it log
-- "Query UNTERMINATED" (-420) and ties up the bus. ATE ships pyvisa-py; a VISA resource
-- string names the instrument, here the simulator peer as a plain TCPIP socket.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: VISA", "read an instrument through pyvisa")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Ask the instrument who it is"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.VISA,
          devices.Visa("TCPIP0::" .. host .. "::" .. port .. "::SOCKET"):setTimeout(2.0))
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        local f = ATE.flush(dev)   -- VISA has no receive buffer: this discards nothing
        ATE.log(Log.INFO, "flush discarded " .. f.count .. " bytes")
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "VISA reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

A handle lives for the whole run. Closing a closed Device does nothing and reports success;
using one is a failure through `onError`. At the end of every run ATE closes whatever is still
open and logs a WARNING for each.

```lua:docs/manuals/examples/test-writer/left_open.tslua
-- left_open.tslua: a Device the schedule never closes.
--
-- A Test Run closes every Device still open when it ends, and logs one WARNING naming
-- each one. That is why a Device opened in a Pre-test Action does not have to be closed
-- by hand - it is worth closing anyway, so the log stays clean.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: a Device left open", "ATE closes what the schedule forgets")
  :setSchedule({
    StepParams("Open and forget"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.report(Result.PASS, "opened a Device and left it open")
      end),
  })
```

## Device failures, Stop on Fail, Retry, Abort

`onError(opResult, opArgs)` is called when the operation fails. If it returns anything but
`nil`, the call returns that value and the schedule continues. If it returns `nil`, or there is
no handler, the call raises a Lua error and the step records `Result.ERROR`. A read that times
out having received some bytes is not a failure: it returns `partial = true` and logs a
WARNING. ATE never reconnects: close the Device and open it again with the same params.

A field the Device Type does not know, a missing required field, or a wrong-typed value is not a
Device failure. It never reaches `onError`; the call returns `false` with the message as its
second result and the traceback is logged at ERROR.

```lua:docs/manuals/examples/test-writer/errors.tslua
-- errors.tslua: how Device failures reach the schedule, and what does not.
--
-- Demonstrates:
--   * a Device failure with no onError raises a Lua error, which ends the step as
--     Result.ERROR;
--   * an onError that returns a value makes the call continue with that value;
--   * a bad argument (a misspelled field) is a schedule bug, not a Device failure: it
--     never reaches onError, it is logged at ERROR, and the call returns false with the
--     message as its second result;
--   * the handler given to open is the fallback for that Device's read, write and close.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025
local deadPort = tonumber(os.getenv("ATE_DEAD_PORT")) or 1

return ScheduleParams("Manual: Device errors", "onError, a closed Device and a bad field")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A misspelled field"):setStepNo("1.")
      :setAction(function()
        local opened, why = ATE.open(DeviceType.TCP, { host = host, port = port, timeout = 1.0,
                                                     readTerm = "\n", writeTerm = "\n", buad = 9600 })
        ATE.report(opened == false and Result.PASS or Result.FAIL, "the open was refused: " .. why)
      end),

    -- Nothing is listening on deadPort: the open fails. This handler logs and returns nil,
    -- which means "no recovery", so ATE.open raises and the step records Result.ERROR.
    StepParams("A port nothing listens on"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, deadPort):setTimeout(0.5),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "nothing is listening on " .. opArgs.host .. ":" .. opArgs.port)
            return nil
          end)
        ATE.report(Result.PASS, "this line is never reached")
      end),

    -- A Device that is closed cannot be read. With no handler the call raises.
    StepParams("Read a closed Device"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.close(dev)
        ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(Result.PASS, "this line is never reached either")
      end),

    -- The same failure, handled: the step carries on with what onError returns.
    StepParams("Handle the closed Device"):setStepNo("4.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "the Device is gone: " .. opResult.err)
            return { data = nil, count = 0, partial = false }
          end)
        ATE.close(dev)
        local r = ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "recovered from reading a closed Device")
      end),
  })
```

Stop on Fail pauses the run at a `FAIL` or `ERROR` step so the Operator can choose `CONTINUE`,
`RETRY` or `ABORT`. Retry runs the action again from the start with POLL reset and blocking
inputs cleared; each Attempt is recorded and the step's result is the last Attempt's. There is
no Attempt number in Lua - keep your own count.

```lua:docs/manuals/examples/test-writer/retry.tslua
-- retry.tslua: Stop on Fail, and what a Retry does.
--
-- Demonstrates:
--   * setStopOnFail(true) is the default: a step ending FAIL or ERROR pauses the run and
--     the Operator chooses Continue, Retry or Abort.
--   * Retry runs the step's action again from the beginning. Each Attempt is recorded;
--     the step's result is its last Attempt's, and the Operator's choice is recorded too.
--   * ATE gives a step no way to read its own Attempt number, so the schedule keeps the
--     count itself in a local the action closes over.
local attempts = 0

return ScheduleParams("Manual: retry", "the first Attempt fails, the Retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Retry this one"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt " .. attempts .. ": the joint is cold", 0.2, 2.4)
        else
          ATE.report(Result.PASS, "attempt " .. attempts .. ": the joint is warm", 2.4, 2.4)
        end
      end),

    -- Only reached after the step passed, or after the Operator chose to continue.
    StepParams("Next check"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "the rail is steady", 5.0, 5.0)
      end),
  })
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

```lua:docs/manuals/examples/test-writer/lib/rails.lua
-- lib/rails.lua: an ordinary Lua module. Its module name is derived from its path:
-- "lib/rails.lua" becomes "lib.rails". require runs a module once per Test Run and
-- caches what it returns; later requires of the same name get the cached value.
local rails = {}

function rails.nominalVolts(channel)
  return 1.5 * channel
end

return rails
```

```lua:docs/manuals/examples/test-writer/modules/limits.lua
-- modules/limits.lua: a module whose name is given explicitly in manual.atedef with the
-- "module" field, because a name derived from the path would be "modules.limits".
return { low = 1.0, high = 5.0 }
```

## Devices

`ATE.open(deviceType, params [, onError])` returns an opaque handle. Every field `open` needs
is required - there are no defaults in `ATE.open`; the builders supply them.

| Device Type | `open` fields | builder (defaults) |
| --- | --- | --- |
| `DeviceType.TCP` | `host`, `port`, `timeout`, `readTerm`, `writeTerm` | `devices.Tcp(host, port)`: timeout 1.0, terminators `"\n"` |
| `DeviceType.SERIAL` | `port`, `baud`, `dataBits`, `stopBits`, `parity`, `flowControl`, `timeout`, `readTerm`, `writeTerm` | `devices.Serial(port)`: 9600, 8, 1, `Parity.NONE`, `FlowControl.NONE`, 1.0, terminators `"\n"` |
| `DeviceType.UDP` | `host`, `port`, `localPort`, `timeout` | `devices.Udp(host, port)`: `localPort` 0, timeout 1.0 |
| `DeviceType.VISA` | `resource`, `timeout`, `readTerm`, `writeTerm` | `devices.Visa(resource)`: timeout 1.0, terminators `"\n"` |

Per-call fields, all optional, falling back to the open-time configuration: `read` takes
`size`, `timeout`, `readTerm`; `write` takes `data` (required), `writeTerm`, `timeout`;
`close` takes no fields of its own but accepts an optional params table and an optional
`onError` — `ATE.close(dev [, params] [, onError])` — which is used if the close itself fails.
UDP has no terminators: its `read` takes `size` and `timeout`, and its `write` takes `data`
(required) and `timeout`. Passing a `readTerm` to a UDP Device is an error naming the field.
Builder setters: `setHost`, `setPort`, `setLocalPort`, `setResource`, `setBaud`, `setDataBits`,
`setStopBits`, `setParity`, `setFlowControl`, `setTimeout`, `setReadTerm`, `setWriteTerm`,
`setSize`, and `devices.Write(data)`.

Reads and writes are raw bytes. The terminator is stripped from `data`; `count` is what is
left. A write's `count` includes the terminator.

TCP, SERIAL and UDP are read in the background from `open` to `close` and buffered, capped at
the Station config key `devices.receiveBufferBytes` (16777216, 16 MiB); on overflow ATE drops
the oldest bytes, logs one WARNING, and the next read carries `overflow = true`. `ATE.flush(dev)`
empties the buffer and returns the count discarded. VISA has no background reader and no buffer,
so `flush` discards nothing.

A VISA resource string names the instrument, and the shape matters:
`GPIB0::5::INSTR` is a GPIB instrument, `TCPIP0::192.168.1.10::inst0::INSTR` is a VXI-11
instrument, and `TCPIP0::127.0.0.1::5025::SOCKET` is a plain TCP socket wrapped in a VISA
session - the same stream a `DeviceType.TCP` Device would open, with pyvisa in the way. ATE
uses whichever pyvisa backend it finds; only pyvisa-py is shipped, which reaches TCPIP and
USBTMC. A GPIB instrument needs a real VISA driver on the Station (NI-VISA or linux-GPIB): ask
the schedule author which resource string their instrument needs before writing the schedule,
because nothing installed with ATE can open a GPIB address.

```lua:docs/manuals/examples/test-writer/tcp.tslua
-- tcp.tslua: a TCP Device, from open to close.
--
-- The host and port come from the environment, which the test harness sets to a
-- simulator peer listening on 127.0.0.1. Point them at a real instrument to use this
-- schedule as it is.
--
-- Demonstrates:
--   * the ate.devices builders, whose setters chain, and how a Device opened in the
--     Pre-test Action is still open in the steps, because a device object lives for the
--     whole Test Run;
--   * per-call overrides (timeout, readTerm, size) that do not change the open-time
--     configuration;
--   * a read that times out after some bytes is a partial read, not a Device failure;
--   * a read that times out with nothing at all is a Device failure and goes through
--     onError, which is given the failure table and the params table and may return a
--     value for the call to carry on with;
--   * flush discards whatever the receive buffer already holds.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

local instrument = nil

return ScheduleParams("Manual: TCP", "open, write, read, flush and close a TCP Device")
  :setStopOnFail(false)
  :setPreTestAction(function()
    -- devices.Tcp(host, port) fills in every field TCP needs: timeout 1.0 s,
    -- readTerm and writeTerm "\n". open has no defaults of its own, so a plain table
    -- would have to name them all.
    instrument = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(2.0))
    ATE.addEquipment("SCPI simulator", "SIM-TCP", "TCP " .. host .. ":" .. port, "2026-01-01")
  end)
  :setPostTestAction(function(passFail)
    ATE.close(instrument)   -- closing a second time does nothing and still reports ok
  end)
  :setSchedule({
    StepParams("Identify the instrument"):setStepNo("1.")
      :setAction(function()
        local sent = ATE.write(instrument, devices.Write("*IDN?"))
        local reply = ATE.read(instrument, devices.Read():setTimeout(1.0))
        ATE.report((sent.ok and reply.count > 0) and Result.PASS or Result.FAIL,
                   "reply: " .. (reply.data or "nothing"),
                   reply.count, 26)
      end),

    -- The peer answers "MEAS:VOLT?" with "1.234\n". readTerm "3" stops the read at the "3",
    -- so the first read takes "1.2" and the second takes the rest of the reply.
    StepParams("Stop a read at a terminator"):setStepNo("2.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MEAS:VOLT?"))
        local first = ATE.read(instrument, devices.Read():setReadTerm("3"))
        local rest = ATE.read(instrument)
        ATE.report((first.data == "1.2" and rest.data == "4") and Result.PASS or Result.FAIL,
                   "first read " .. (first.data or "nothing") .. ", second " .. (rest.data or "nothing"),
                   first.count + rest.count, 4)
      end),

    -- "PARTIAL?" is answered with bytes and no terminator: the read times out having taken
    -- some of them. That is partial = true, a WARNING in the log, and not a Device failure.
    StepParams("A read that times out partway"):setStepNo("3.")
      :setAction(function()
        ATE.write(instrument, devices.Write("PARTIAL?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3))
        ATE.report((r.partial and r.count > 0) and Result.PASS or Result.FAIL,
                   "got " .. r.count .. " bytes before the timeout",
                   tostring(r.partial), "true")
      end),

    -- "NEVER?" is answered with silence: the read times out with no bytes at all, which IS
    -- a Device failure, so onError runs. Returning a value makes the call continue with it.
    StepParams("Handle a read that times out"):setStepNo("4.")
      :setAction(function()
        ATE.write(instrument, devices.Write("NEVER?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3), function(opResult, opArgs)
          ATE.log(Log.WARNING, "no reply from the instrument: " .. opResult.err)
          return { data = nil, count = 0, partial = false }
        end)
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "the timeout was handled, so the step carried on")
      end),

    -- The peer sends three lines; the schedule reads the first and throws away the rest.
    StepParams("Discard the receive buffer"):setStepNo("5.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MULTI?"))
        ATE.read(instrument, devices.Read():setSize(7))
        local f = ATE.flush(instrument)
        ATE.report((f.ok and f.count > 0) and Result.PASS or Result.FAIL, "discarded " .. f.count .. " buffered bytes")
      end),
  })
```

```lua:docs/manuals/examples/test-writer/udp.tslua
-- udp.tslua: a UDP Device, where one datagram is one message.
--
-- Demonstrates:
--   * UDP has no terminators, so readTerm and writeTerm are not fields this Device Type
--     knows; passing one is a schedule bug that raises naming the field;
--   * a datagram longer than the size asked for is truncated and returned with partial =
--     true, which the log calls a truncation rather than a timeout;
--   * the receive buffer is a queue of whole datagrams, so a reply that arrives before the
--     schedule reads it is not lost.
local devices = require("ate.devices")

local host = os.getenv("ATE_UDP_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_UDP_PORT")) or 5026

return ScheduleParams("Manual: UDP", "send and read datagrams")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Echo one datagram"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("PING"))
        local reply = ATE.read(dev)
        ATE.report(reply.data == "ECHO:PING" and Result.PASS or Result.FAIL,
                   "the peer echoed " .. (reply.data or "nothing"),
                   reply.count, 9)
        ATE.close(dev)
      end),

    -- Ask for 10 bytes of a 40-byte datagram: ATE returns the first 10 and says partial.
    StepParams("Truncate a datagram"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("a datagram longer than the size asked for"))
        local r = ATE.read(dev, devices.Read():setSize(10))
        ATE.report((r.count == 10 and r.partial) and Result.PASS or Result.FAIL,
                   "read " .. r.count .. " of a longer datagram",
                   r.count, 10)
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/serial.tslua
-- serial.tslua: a SERIAL Device through pyserial.
--
-- The port here is pyserial's socket:// URL, which opens a TCP connection to the
-- simulator peer; a real station names a COM or tty port instead.
--
-- Demonstrates:
--   * open has no defaults, so every field is named;
--   * the Parity and FlowControl enums;
--   * the same connection read with the Device's own terminator.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: SERIAL", "open a serial port and read a reply")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Open the port"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.SERIAL, {
          port = "socket://" .. host .. ":" .. port,
          baud = 115200,
          dataBits = 8,
          stopBits = 1,
          parity = Parity.NONE,
          flowControl = FlowControl.NONE,
          timeout = 2.0,
          readTerm = "\n",
          writeTerm = "\n",
        })
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "serial reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/visa.tslua
-- visa.tslua: a VISA instrument through pyvisa.
--
-- VISA has no background reader: ATE reads the instrument only when the schedule asks,
-- because polling an SCPI instrument with reads that have nothing pending makes it log
-- "Query UNTERMINATED" (-420) and ties up the bus. ATE ships pyvisa-py; a VISA resource
-- string names the instrument, here the simulator peer as a plain TCPIP socket.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: VISA", "read an instrument through pyvisa")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Ask the instrument who it is"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.VISA,
          devices.Visa("TCPIP0::" .. host .. "::" .. port .. "::SOCKET"):setTimeout(2.0))
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        local f = ATE.flush(dev)   -- VISA has no receive buffer: this discards nothing
        ATE.log(Log.INFO, "flush discarded " .. f.count .. " bytes")
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "VISA reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

A handle lives for the whole run. Closing a closed Device does nothing and reports success;
using one is a failure through `onError`. At the end of every run ATE closes whatever is still
open and logs a WARNING for each.

```lua:docs/manuals/examples/test-writer/left_open.tslua
-- left_open.tslua: a Device the schedule never closes.
--
-- A Test Run closes every Device still open when it ends, and logs one WARNING naming
-- each one. That is why a Device opened in a Pre-test Action does not have to be closed
-- by hand - it is worth closing anyway, so the log stays clean.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: a Device left open", "ATE closes what the schedule forgets")
  :setSchedule({
    StepParams("Open and forget"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.report(Result.PASS, "opened a Device and left it open")
      end),
  })
```

## Device failures, Stop on Fail, Retry, Abort

`onError(opResult, opArgs)` is called when the operation fails. If it returns anything but
`nil`, the call returns that value and the schedule continues. If it returns `nil`, or there is
no handler, the call raises a Lua error and the step records `Result.ERROR`. A read that times
out having received some bytes is not a failure: it returns `partial = true` and logs a
WARNING. ATE never reconnects: close the Device and open it again with the same params.

A field the Device Type does not know, a missing required field, or a wrong-typed value is not a
Device failure. It never reaches `onError`; the call returns `false` with the message as its
second result and the traceback is logged at ERROR.

```lua:docs/manuals/examples/test-writer/errors.tslua
-- errors.tslua: how Device failures reach the schedule, and what does not.
--
-- Demonstrates:
--   * a Device failure with no onError raises a Lua error, which ends the step as
--     Result.ERROR;
--   * an onError that returns a value makes the call continue with that value;
--   * a bad argument (a misspelled field) is a schedule bug, not a Device failure: it
--     never reaches onError, it is logged at ERROR, and the call returns false with the
--     message as its second result;
--   * the handler given to open is the fallback for that Device's read, write and close.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025
local deadPort = tonumber(os.getenv("ATE_DEAD_PORT")) or 1

return ScheduleParams("Manual: Device errors", "onError, a closed Device and a bad field")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A misspelled field"):setStepNo("1.")
      :setAction(function()
        local opened, why = ATE.open(DeviceType.TCP, { host = host, port = port, timeout = 1.0,
                                                     readTerm = "\n", writeTerm = "\n", buad = 9600 })
        ATE.report(opened == false and Result.PASS or Result.FAIL, "the open was refused: " .. why)
      end),

    -- Nothing is listening on deadPort: the open fails. This handler logs and returns nil,
    -- which means "no recovery", so ATE.open raises and the step records Result.ERROR.
    StepParams("A port nothing listens on"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, deadPort):setTimeout(0.5),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "nothing is listening on " .. opArgs.host .. ":" .. opArgs.port)
            return nil
          end)
        ATE.report(Result.PASS, "this line is never reached")
      end),

    -- A Device that is closed cannot be read. With no handler the call raises.
    StepParams("Read a closed Device"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.close(dev)
        ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(Result.PASS, "this line is never reached either")
      end),

    -- The same failure, handled: the step carries on with what onError returns.
    StepParams("Handle the closed Device"):setStepNo("4.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "the Device is gone: " .. opResult.err)
            return { data = nil, count = 0, partial = false }
          end)
        ATE.close(dev)
        local r = ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "recovered from reading a closed Device")
      end),
  })
```

Stop on Fail pauses the run at a `FAIL` or `ERROR` step so the Operator can choose `CONTINUE`,
`RETRY` or `ABORT`. Retry runs the action again from the start with POLL reset and blocking
inputs cleared; each Attempt is recorded and the step's result is the last Attempt's. There is
no Attempt number in Lua - keep your own count.

```lua:docs/manuals/examples/test-writer/retry.tslua
-- retry.tslua: Stop on Fail, and what a Retry does.
--
-- Demonstrates:
--   * setStopOnFail(true) is the default: a step ending FAIL or ERROR pauses the run and
--     the Operator chooses Continue, Retry or Abort.
--   * Retry runs the step's action again from the beginning. Each Attempt is recorded;
--     the step's result is its last Attempt's, and the Operator's choice is recorded too.
--   * ATE gives a step no way to read its own Attempt number, so the schedule keeps the
--     count itself in a local the action closes over.
local attempts = 0

return ScheduleParams("Manual: retry", "the first Attempt fails, the Retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Retry this one"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt " .. attempts .. ": the joint is cold", 0.2, 2.4)
        else
          ATE.report(Result.PASS, "attempt " .. attempts .. ": the joint is warm", 2.4, 2.4)
        end
      end),

    -- Only reached after the step passed, or after the Operator chose to continue.
    StepParams("Next check"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "the rail is steady", 5.0, 5.0)
      end),
  })
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

```lua:docs/manuals/examples/test-writer/modules/limits.lua
-- modules/limits.lua: a module whose name is given explicitly in manual.atedef with the
-- "module" field, because a name derived from the path would be "modules.limits".
return { low = 1.0, high = 5.0 }
```

## Devices

`ATE.open(deviceType, params [, onError])` returns an opaque handle. Every field `open` needs
is required - there are no defaults in `ATE.open`; the builders supply them.

| Device Type | `open` fields | builder (defaults) |
| --- | --- | --- |
| `DeviceType.TCP` | `host`, `port`, `timeout`, `readTerm`, `writeTerm` | `devices.Tcp(host, port)`: timeout 1.0, terminators `"\n"` |
| `DeviceType.SERIAL` | `port`, `baud`, `dataBits`, `stopBits`, `parity`, `flowControl`, `timeout`, `readTerm`, `writeTerm` | `devices.Serial(port)`: 9600, 8, 1, `Parity.NONE`, `FlowControl.NONE`, 1.0, terminators `"\n"` |
| `DeviceType.UDP` | `host`, `port`, `localPort`, `timeout` | `devices.Udp(host, port)`: `localPort` 0, timeout 1.0 |
| `DeviceType.VISA` | `resource`, `timeout`, `readTerm`, `writeTerm` | `devices.Visa(resource)`: timeout 1.0, terminators `"\n"` |

Per-call fields, all optional, falling back to the open-time configuration: `read` takes
`size`, `timeout`, `readTerm`; `write` takes `data` (required), `writeTerm`, `timeout`;
`close` takes no fields of its own but accepts an optional params table and an optional
`onError` — `ATE.close(dev [, params] [, onError])` — which is used if the close itself fails.
UDP has no terminators: its `read` takes `size` and `timeout`, and its `write` takes `data`
(required) and `timeout`. Passing a `readTerm` to a UDP Device is an error naming the field.
Builder setters: `setHost`, `setPort`, `setLocalPort`, `setResource`, `setBaud`, `setDataBits`,
`setStopBits`, `setParity`, `setFlowControl`, `setTimeout`, `setReadTerm`, `setWriteTerm`,
`setSize`, and `devices.Write(data)`.

Reads and writes are raw bytes. The terminator is stripped from `data`; `count` is what is
left. A write's `count` includes the terminator.

TCP, SERIAL and UDP are read in the background from `open` to `close` and buffered, capped at
the Station config key `devices.receiveBufferBytes` (16777216, 16 MiB); on overflow ATE drops
the oldest bytes, logs one WARNING, and the next read carries `overflow = true`. `ATE.flush(dev)`
empties the buffer and returns the count discarded. VISA has no background reader and no buffer,
so `flush` discards nothing.

A VISA resource string names the instrument, and the shape matters:
`GPIB0::5::INSTR` is a GPIB instrument, `TCPIP0::192.168.1.10::inst0::INSTR` is a VXI-11
instrument, and `TCPIP0::127.0.0.1::5025::SOCKET` is a plain TCP socket wrapped in a VISA
session - the same stream a `DeviceType.TCP` Device would open, with pyvisa in the way. ATE
uses whichever pyvisa backend it finds; only pyvisa-py is shipped, which reaches TCPIP and
USBTMC. A GPIB instrument needs a real VISA driver on the Station (NI-VISA or linux-GPIB): ask
the schedule author which resource string their instrument needs before writing the schedule,
because nothing installed with ATE can open a GPIB address.

```lua:docs/manuals/examples/test-writer/tcp.tslua
-- tcp.tslua: a TCP Device, from open to close.
--
-- The host and port come from the environment, which the test harness sets to a
-- simulator peer listening on 127.0.0.1. Point them at a real instrument to use this
-- schedule as it is.
--
-- Demonstrates:
--   * the ate.devices builders, whose setters chain, and how a Device opened in the
--     Pre-test Action is still open in the steps, because a device object lives for the
--     whole Test Run;
--   * per-call overrides (timeout, readTerm, size) that do not change the open-time
--     configuration;
--   * a read that times out after some bytes is a partial read, not a Device failure;
--   * a read that times out with nothing at all is a Device failure and goes through
--     onError, which is given the failure table and the params table and may return a
--     value for the call to carry on with;
--   * flush discards whatever the receive buffer already holds.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

local instrument = nil

return ScheduleParams("Manual: TCP", "open, write, read, flush and close a TCP Device")
  :setStopOnFail(false)
  :setPreTestAction(function()
    -- devices.Tcp(host, port) fills in every field TCP needs: timeout 1.0 s,
    -- readTerm and writeTerm "\n". open has no defaults of its own, so a plain table
    -- would have to name them all.
    instrument = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(2.0))
    ATE.addEquipment("SCPI simulator", "SIM-TCP", "TCP " .. host .. ":" .. port, "2026-01-01")
  end)
  :setPostTestAction(function(passFail)
    ATE.close(instrument)   -- closing a second time does nothing and still reports ok
  end)
  :setSchedule({
    StepParams("Identify the instrument"):setStepNo("1.")
      :setAction(function()
        local sent = ATE.write(instrument, devices.Write("*IDN?"))
        local reply = ATE.read(instrument, devices.Read():setTimeout(1.0))
        ATE.report((sent.ok and reply.count > 0) and Result.PASS or Result.FAIL,
                   "reply: " .. (reply.data or "nothing"),
                   reply.count, 26)
      end),

    -- The peer answers "MEAS:VOLT?" with "1.234\n". readTerm "3" stops the read at the "3",
    -- so the first read takes "1.2" and the second takes the rest of the reply.
    StepParams("Stop a read at a terminator"):setStepNo("2.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MEAS:VOLT?"))
        local first = ATE.read(instrument, devices.Read():setReadTerm("3"))
        local rest = ATE.read(instrument)
        ATE.report((first.data == "1.2" and rest.data == "4") and Result.PASS or Result.FAIL,
                   "first read " .. (first.data or "nothing") .. ", second " .. (rest.data or "nothing"),
                   first.count + rest.count, 4)
      end),

    -- "PARTIAL?" is answered with bytes and no terminator: the read times out having taken
    -- some of them. That is partial = true, a WARNING in the log, and not a Device failure.
    StepParams("A read that times out partway"):setStepNo("3.")
      :setAction(function()
        ATE.write(instrument, devices.Write("PARTIAL?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3))
        ATE.report((r.partial and r.count > 0) and Result.PASS or Result.FAIL,
                   "got " .. r.count .. " bytes before the timeout",
                   tostring(r.partial), "true")
      end),

    -- "NEVER?" is answered with silence: the read times out with no bytes at all, which IS
    -- a Device failure, so onError runs. Returning a value makes the call continue with it.
    StepParams("Handle a read that times out"):setStepNo("4.")
      :setAction(function()
        ATE.write(instrument, devices.Write("NEVER?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3), function(opResult, opArgs)
          ATE.log(Log.WARNING, "no reply from the instrument: " .. opResult.err)
          return { data = nil, count = 0, partial = false }
        end)
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "the timeout was handled, so the step carried on")
      end),

    -- The peer sends three lines; the schedule reads the first and throws away the rest.
    StepParams("Discard the receive buffer"):setStepNo("5.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MULTI?"))
        ATE.read(instrument, devices.Read():setSize(7))
        local f = ATE.flush(instrument)
        ATE.report((f.ok and f.count > 0) and Result.PASS or Result.FAIL, "discarded " .. f.count .. " buffered bytes")
      end),
  })
```

```lua:docs/manuals/examples/test-writer/udp.tslua
-- udp.tslua: a UDP Device, where one datagram is one message.
--
-- Demonstrates:
--   * UDP has no terminators, so readTerm and writeTerm are not fields this Device Type
--     knows; passing one is a schedule bug that raises naming the field;
--   * a datagram longer than the size asked for is truncated and returned with partial =
--     true, which the log calls a truncation rather than a timeout;
--   * the receive buffer is a queue of whole datagrams, so a reply that arrives before the
--     schedule reads it is not lost.
local devices = require("ate.devices")

local host = os.getenv("ATE_UDP_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_UDP_PORT")) or 5026

return ScheduleParams("Manual: UDP", "send and read datagrams")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Echo one datagram"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("PING"))
        local reply = ATE.read(dev)
        ATE.report(reply.data == "ECHO:PING" and Result.PASS or Result.FAIL,
                   "the peer echoed " .. (reply.data or "nothing"),
                   reply.count, 9)
        ATE.close(dev)
      end),

    -- Ask for 10 bytes of a 40-byte datagram: ATE returns the first 10 and says partial.
    StepParams("Truncate a datagram"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("a datagram longer than the size asked for"))
        local r = ATE.read(dev, devices.Read():setSize(10))
        ATE.report((r.count == 10 and r.partial) and Result.PASS or Result.FAIL,
                   "read " .. r.count .. " of a longer datagram",
                   r.count, 10)
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/serial.tslua
-- serial.tslua: a SERIAL Device through pyserial.
--
-- The port here is pyserial's socket:// URL, which opens a TCP connection to the
-- simulator peer; a real station names a COM or tty port instead.
--
-- Demonstrates:
--   * open has no defaults, so every field is named;
--   * the Parity and FlowControl enums;
--   * the same connection read with the Device's own terminator.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: SERIAL", "open a serial port and read a reply")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Open the port"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.SERIAL, {
          port = "socket://" .. host .. ":" .. port,
          baud = 115200,
          dataBits = 8,
          stopBits = 1,
          parity = Parity.NONE,
          flowControl = FlowControl.NONE,
          timeout = 2.0,
          readTerm = "\n",
          writeTerm = "\n",
        })
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "serial reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/visa.tslua
-- visa.tslua: a VISA instrument through pyvisa.
--
-- VISA has no background reader: ATE reads the instrument only when the schedule asks,
-- because polling an SCPI instrument with reads that have nothing pending makes it log
-- "Query UNTERMINATED" (-420) and ties up the bus. ATE ships pyvisa-py; a VISA resource
-- string names the instrument, here the simulator peer as a plain TCPIP socket.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: VISA", "read an instrument through pyvisa")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Ask the instrument who it is"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.VISA,
          devices.Visa("TCPIP0::" .. host .. "::" .. port .. "::SOCKET"):setTimeout(2.0))
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        local f = ATE.flush(dev)   -- VISA has no receive buffer: this discards nothing
        ATE.log(Log.INFO, "flush discarded " .. f.count .. " bytes")
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "VISA reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

A handle lives for the whole run. Closing a closed Device does nothing and reports success;
using one is a failure through `onError`. At the end of every run ATE closes whatever is still
open and logs a WARNING for each.

```lua:docs/manuals/examples/test-writer/left_open.tslua
-- left_open.tslua: a Device the schedule never closes.
--
-- A Test Run closes every Device still open when it ends, and logs one WARNING naming
-- each one. That is why a Device opened in a Pre-test Action does not have to be closed
-- by hand - it is worth closing anyway, so the log stays clean.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: a Device left open", "ATE closes what the schedule forgets")
  :setSchedule({
    StepParams("Open and forget"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.report(Result.PASS, "opened a Device and left it open")
      end),
  })
```

## Device failures, Stop on Fail, Retry, Abort

`onError(opResult, opArgs)` is called when the operation fails. If it returns anything but
`nil`, the call returns that value and the schedule continues. If it returns `nil`, or there is
no handler, the call raises a Lua error and the step records `Result.ERROR`. A read that times
out having received some bytes is not a failure: it returns `partial = true` and logs a
WARNING. ATE never reconnects: close the Device and open it again with the same params.

A field the Device Type does not know, a missing required field, or a wrong-typed value is not a
Device failure. It never reaches `onError`; the call returns `false` with the message as its
second result and the traceback is logged at ERROR.

```lua:docs/manuals/examples/test-writer/errors.tslua
-- errors.tslua: how Device failures reach the schedule, and what does not.
--
-- Demonstrates:
--   * a Device failure with no onError raises a Lua error, which ends the step as
--     Result.ERROR;
--   * an onError that returns a value makes the call continue with that value;
--   * a bad argument (a misspelled field) is a schedule bug, not a Device failure: it
--     never reaches onError, it is logged at ERROR, and the call returns false with the
--     message as its second result;
--   * the handler given to open is the fallback for that Device's read, write and close.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025
local deadPort = tonumber(os.getenv("ATE_DEAD_PORT")) or 1

return ScheduleParams("Manual: Device errors", "onError, a closed Device and a bad field")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A misspelled field"):setStepNo("1.")
      :setAction(function()
        local opened, why = ATE.open(DeviceType.TCP, { host = host, port = port, timeout = 1.0,
                                                     readTerm = "\n", writeTerm = "\n", buad = 9600 })
        ATE.report(opened == false and Result.PASS or Result.FAIL, "the open was refused: " .. why)
      end),

    -- Nothing is listening on deadPort: the open fails. This handler logs and returns nil,
    -- which means "no recovery", so ATE.open raises and the step records Result.ERROR.
    StepParams("A port nothing listens on"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, deadPort):setTimeout(0.5),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "nothing is listening on " .. opArgs.host .. ":" .. opArgs.port)
            return nil
          end)
        ATE.report(Result.PASS, "this line is never reached")
      end),

    -- A Device that is closed cannot be read. With no handler the call raises.
    StepParams("Read a closed Device"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.close(dev)
        ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(Result.PASS, "this line is never reached either")
      end),

    -- The same failure, handled: the step carries on with what onError returns.
    StepParams("Handle the closed Device"):setStepNo("4.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "the Device is gone: " .. opResult.err)
            return { data = nil, count = 0, partial = false }
          end)
        ATE.close(dev)
        local r = ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "recovered from reading a closed Device")
      end),
  })
```

Stop on Fail pauses the run at a `FAIL` or `ERROR` step so the Operator can choose `CONTINUE`,
`RETRY` or `ABORT`. Retry runs the action again from the start with POLL reset and blocking
inputs cleared; each Attempt is recorded and the step's result is the last Attempt's. There is
no Attempt number in Lua - keep your own count.

```lua:docs/manuals/examples/test-writer/retry.tslua
-- retry.tslua: Stop on Fail, and what a Retry does.
--
-- Demonstrates:
--   * setStopOnFail(true) is the default: a step ending FAIL or ERROR pauses the run and
--     the Operator chooses Continue, Retry or Abort.
--   * Retry runs the step's action again from the beginning. Each Attempt is recorded;
--     the step's result is its last Attempt's, and the Operator's choice is recorded too.
--   * ATE gives a step no way to read its own Attempt number, so the schedule keeps the
--     count itself in a local the action closes over.
local attempts = 0

return ScheduleParams("Manual: retry", "the first Attempt fails, the Retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Retry this one"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt " .. attempts .. ": the joint is cold", 0.2, 2.4)
        else
          ATE.report(Result.PASS, "attempt " .. attempts .. ": the joint is warm", 2.4, 2.4)
        end
      end),

    -- Only reached after the step passed, or after the Operator chose to continue.
    StepParams("Next check"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "the rail is steady", 5.0, 5.0)
      end),
  })
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

## Devices

`ATE.open(deviceType, params [, onError])` returns an opaque handle. Every field `open` needs
is required - there are no defaults in `ATE.open`; the builders supply them.

| Device Type | `open` fields | builder (defaults) |
| --- | --- | --- |
| `DeviceType.TCP` | `host`, `port`, `timeout`, `readTerm`, `writeTerm` | `devices.Tcp(host, port)`: timeout 1.0, terminators `"\n"` |
| `DeviceType.SERIAL` | `port`, `baud`, `dataBits`, `stopBits`, `parity`, `flowControl`, `timeout`, `readTerm`, `writeTerm` | `devices.Serial(port)`: 9600, 8, 1, `Parity.NONE`, `FlowControl.NONE`, 1.0, terminators `"\n"` |
| `DeviceType.UDP` | `host`, `port`, `localPort`, `timeout` | `devices.Udp(host, port)`: `localPort` 0, timeout 1.0 |
| `DeviceType.VISA` | `resource`, `timeout`, `readTerm`, `writeTerm` | `devices.Visa(resource)`: timeout 1.0, terminators `"\n"` |

Per-call fields, all optional, falling back to the open-time configuration: `read` takes
`size`, `timeout`, `readTerm`; `write` takes `data` (required), `writeTerm`, `timeout`;
`close` takes no fields of its own but accepts an optional params table and an optional
`onError` — `ATE.close(dev [, params] [, onError])` — which is used if the close itself fails.
UDP has no terminators: its `read` takes `size` and `timeout`, and its `write` takes `data`
(required) and `timeout`. Passing a `readTerm` to a UDP Device is an error naming the field.
Builder setters: `setHost`, `setPort`, `setLocalPort`, `setResource`, `setBaud`, `setDataBits`,
`setStopBits`, `setParity`, `setFlowControl`, `setTimeout`, `setReadTerm`, `setWriteTerm`,
`setSize`, and `devices.Write(data)`.

Reads and writes are raw bytes. The terminator is stripped from `data`; `count` is what is
left. A write's `count` includes the terminator.

TCP, SERIAL and UDP are read in the background from `open` to `close` and buffered, capped at
the Station config key `devices.receiveBufferBytes` (16777216, 16 MiB); on overflow ATE drops
the oldest bytes, logs one WARNING, and the next read carries `overflow = true`. `ATE.flush(dev)`
empties the buffer and returns the count discarded. VISA has no background reader and no buffer,
so `flush` discards nothing.

A VISA resource string names the instrument, and the shape matters:
`GPIB0::5::INSTR` is a GPIB instrument, `TCPIP0::192.168.1.10::inst0::INSTR` is a VXI-11
instrument, and `TCPIP0::127.0.0.1::5025::SOCKET` is a plain TCP socket wrapped in a VISA
session - the same stream a `DeviceType.TCP` Device would open, with pyvisa in the way. ATE
uses whichever pyvisa backend it finds; only pyvisa-py is shipped, which reaches TCPIP and
USBTMC. A GPIB instrument needs a real VISA driver on the Station (NI-VISA or linux-GPIB): ask
the schedule author which resource string their instrument needs before writing the schedule,
because nothing installed with ATE can open a GPIB address.

```lua:docs/manuals/examples/test-writer/tcp.tslua
-- tcp.tslua: a TCP Device, from open to close.
--
-- The host and port come from the environment, which the test harness sets to a
-- simulator peer listening on 127.0.0.1. Point them at a real instrument to use this
-- schedule as it is.
--
-- Demonstrates:
--   * the ate.devices builders, whose setters chain, and how a Device opened in the
--     Pre-test Action is still open in the steps, because a device object lives for the
--     whole Test Run;
--   * per-call overrides (timeout, readTerm, size) that do not change the open-time
--     configuration;
--   * a read that times out after some bytes is a partial read, not a Device failure;
--   * a read that times out with nothing at all is a Device failure and goes through
--     onError, which is given the failure table and the params table and may return a
--     value for the call to carry on with;
--   * flush discards whatever the receive buffer already holds.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

local instrument = nil

return ScheduleParams("Manual: TCP", "open, write, read, flush and close a TCP Device")
  :setStopOnFail(false)
  :setPreTestAction(function()
    -- devices.Tcp(host, port) fills in every field TCP needs: timeout 1.0 s,
    -- readTerm and writeTerm "\n". open has no defaults of its own, so a plain table
    -- would have to name them all.
    instrument = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(2.0))
    ATE.addEquipment("SCPI simulator", "SIM-TCP", "TCP " .. host .. ":" .. port, "2026-01-01")
  end)
  :setPostTestAction(function(passFail)
    ATE.close(instrument)   -- closing a second time does nothing and still reports ok
  end)
  :setSchedule({
    StepParams("Identify the instrument"):setStepNo("1.")
      :setAction(function()
        local sent = ATE.write(instrument, devices.Write("*IDN?"))
        local reply = ATE.read(instrument, devices.Read():setTimeout(1.0))
        ATE.report((sent.ok and reply.count > 0) and Result.PASS or Result.FAIL,
                   "reply: " .. (reply.data or "nothing"),
                   reply.count, 26)
      end),

    -- The peer answers "MEAS:VOLT?" with "1.234\n". readTerm "3" stops the read at the "3",
    -- so the first read takes "1.2" and the second takes the rest of the reply.
    StepParams("Stop a read at a terminator"):setStepNo("2.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MEAS:VOLT?"))
        local first = ATE.read(instrument, devices.Read():setReadTerm("3"))
        local rest = ATE.read(instrument)
        ATE.report((first.data == "1.2" and rest.data == "4") and Result.PASS or Result.FAIL,
                   "first read " .. (first.data or "nothing") .. ", second " .. (rest.data or "nothing"),
                   first.count + rest.count, 4)
      end),

    -- "PARTIAL?" is answered with bytes and no terminator: the read times out having taken
    -- some of them. That is partial = true, a WARNING in the log, and not a Device failure.
    StepParams("A read that times out partway"):setStepNo("3.")
      :setAction(function()
        ATE.write(instrument, devices.Write("PARTIAL?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3))
        ATE.report((r.partial and r.count > 0) and Result.PASS or Result.FAIL,
                   "got " .. r.count .. " bytes before the timeout",
                   tostring(r.partial), "true")
      end),

    -- "NEVER?" is answered with silence: the read times out with no bytes at all, which IS
    -- a Device failure, so onError runs. Returning a value makes the call continue with it.
    StepParams("Handle a read that times out"):setStepNo("4.")
      :setAction(function()
        ATE.write(instrument, devices.Write("NEVER?"))
        local r = ATE.read(instrument, devices.Read():setTimeout(0.3), function(opResult, opArgs)
          ATE.log(Log.WARNING, "no reply from the instrument: " .. opResult.err)
          return { data = nil, count = 0, partial = false }
        end)
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "the timeout was handled, so the step carried on")
      end),

    -- The peer sends three lines; the schedule reads the first and throws away the rest.
    StepParams("Discard the receive buffer"):setStepNo("5.")
      :setAction(function()
        ATE.write(instrument, devices.Write("MULTI?"))
        ATE.read(instrument, devices.Read():setSize(7))
        local f = ATE.flush(instrument)
        ATE.report((f.ok and f.count > 0) and Result.PASS or Result.FAIL, "discarded " .. f.count .. " buffered bytes")
      end),
  })
```

```lua:docs/manuals/examples/test-writer/udp.tslua
-- udp.tslua: a UDP Device, where one datagram is one message.
--
-- Demonstrates:
--   * UDP has no terminators, so readTerm and writeTerm are not fields this Device Type
--     knows; passing one is a schedule bug that raises naming the field;
--   * a datagram longer than the size asked for is truncated and returned with partial =
--     true, which the log calls a truncation rather than a timeout;
--   * the receive buffer is a queue of whole datagrams, so a reply that arrives before the
--     schedule reads it is not lost.
local devices = require("ate.devices")

local host = os.getenv("ATE_UDP_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_UDP_PORT")) or 5026

return ScheduleParams("Manual: UDP", "send and read datagrams")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Echo one datagram"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("PING"))
        local reply = ATE.read(dev)
        ATE.report(reply.data == "ECHO:PING" and Result.PASS or Result.FAIL,
                   "the peer echoed " .. (reply.data or "nothing"),
                   reply.count, 9)
        ATE.close(dev)
      end),

    -- Ask for 10 bytes of a 40-byte datagram: ATE returns the first 10 and says partial.
    StepParams("Truncate a datagram"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("a datagram longer than the size asked for"))
        local r = ATE.read(dev, devices.Read():setSize(10))
        ATE.report((r.count == 10 and r.partial) and Result.PASS or Result.FAIL,
                   "read " .. r.count .. " of a longer datagram",
                   r.count, 10)
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/serial.tslua
-- serial.tslua: a SERIAL Device through pyserial.
--
-- The port here is pyserial's socket:// URL, which opens a TCP connection to the
-- simulator peer; a real station names a COM or tty port instead.
--
-- Demonstrates:
--   * open has no defaults, so every field is named;
--   * the Parity and FlowControl enums;
--   * the same connection read with the Device's own terminator.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: SERIAL", "open a serial port and read a reply")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Open the port"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.SERIAL, {
          port = "socket://" .. host .. ":" .. port,
          baud = 115200,
          dataBits = 8,
          stopBits = 1,
          parity = Parity.NONE,
          flowControl = FlowControl.NONE,
          timeout = 2.0,
          readTerm = "\n",
          writeTerm = "\n",
        })
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "serial reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/visa.tslua
-- visa.tslua: a VISA instrument through pyvisa.
--
-- VISA has no background reader: ATE reads the instrument only when the schedule asks,
-- because polling an SCPI instrument with reads that have nothing pending makes it log
-- "Query UNTERMINATED" (-420) and ties up the bus. ATE ships pyvisa-py; a VISA resource
-- string names the instrument, here the simulator peer as a plain TCPIP socket.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: VISA", "read an instrument through pyvisa")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Ask the instrument who it is"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.VISA,
          devices.Visa("TCPIP0::" .. host .. "::" .. port .. "::SOCKET"):setTimeout(2.0))
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        local f = ATE.flush(dev)   -- VISA has no receive buffer: this discards nothing
        ATE.log(Log.INFO, "flush discarded " .. f.count .. " bytes")
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "VISA reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

A handle lives for the whole run. Closing a closed Device does nothing and reports success;
using one is a failure through `onError`. At the end of every run ATE closes whatever is still
open and logs a WARNING for each.

```lua:docs/manuals/examples/test-writer/left_open.tslua
-- left_open.tslua: a Device the schedule never closes.
--
-- A Test Run closes every Device still open when it ends, and logs one WARNING naming
-- each one. That is why a Device opened in a Pre-test Action does not have to be closed
-- by hand - it is worth closing anyway, so the log stays clean.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: a Device left open", "ATE closes what the schedule forgets")
  :setSchedule({
    StepParams("Open and forget"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.report(Result.PASS, "opened a Device and left it open")
      end),
  })
```

## Device failures, Stop on Fail, Retry, Abort

`onError(opResult, opArgs)` is called when the operation fails. If it returns anything but
`nil`, the call returns that value and the schedule continues. If it returns `nil`, or there is
no handler, the call raises a Lua error and the step records `Result.ERROR`. A read that times
out having received some bytes is not a failure: it returns `partial = true` and logs a
WARNING. ATE never reconnects: close the Device and open it again with the same params.

A field the Device Type does not know, a missing required field, or a wrong-typed value is not a
Device failure. It never reaches `onError`; the call returns `false` with the message as its
second result and the traceback is logged at ERROR.

```lua:docs/manuals/examples/test-writer/errors.tslua
-- errors.tslua: how Device failures reach the schedule, and what does not.
--
-- Demonstrates:
--   * a Device failure with no onError raises a Lua error, which ends the step as
--     Result.ERROR;
--   * an onError that returns a value makes the call continue with that value;
--   * a bad argument (a misspelled field) is a schedule bug, not a Device failure: it
--     never reaches onError, it is logged at ERROR, and the call returns false with the
--     message as its second result;
--   * the handler given to open is the fallback for that Device's read, write and close.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025
local deadPort = tonumber(os.getenv("ATE_DEAD_PORT")) or 1

return ScheduleParams("Manual: Device errors", "onError, a closed Device and a bad field")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A misspelled field"):setStepNo("1.")
      :setAction(function()
        local opened, why = ATE.open(DeviceType.TCP, { host = host, port = port, timeout = 1.0,
                                                     readTerm = "\n", writeTerm = "\n", buad = 9600 })
        ATE.report(opened == false and Result.PASS or Result.FAIL, "the open was refused: " .. why)
      end),

    -- Nothing is listening on deadPort: the open fails. This handler logs and returns nil,
    -- which means "no recovery", so ATE.open raises and the step records Result.ERROR.
    StepParams("A port nothing listens on"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, deadPort):setTimeout(0.5),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "nothing is listening on " .. opArgs.host .. ":" .. opArgs.port)
            return nil
          end)
        ATE.report(Result.PASS, "this line is never reached")
      end),

    -- A Device that is closed cannot be read. With no handler the call raises.
    StepParams("Read a closed Device"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.close(dev)
        ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(Result.PASS, "this line is never reached either")
      end),

    -- The same failure, handled: the step carries on with what onError returns.
    StepParams("Handle the closed Device"):setStepNo("4.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "the Device is gone: " .. opResult.err)
            return { data = nil, count = 0, partial = false }
          end)
        ATE.close(dev)
        local r = ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "recovered from reading a closed Device")
      end),
  })
```

Stop on Fail pauses the run at a `FAIL` or `ERROR` step so the Operator can choose `CONTINUE`,
`RETRY` or `ABORT`. Retry runs the action again from the start with POLL reset and blocking
inputs cleared; each Attempt is recorded and the step's result is the last Attempt's. There is
no Attempt number in Lua - keep your own count.

```lua:docs/manuals/examples/test-writer/retry.tslua
-- retry.tslua: Stop on Fail, and what a Retry does.
--
-- Demonstrates:
--   * setStopOnFail(true) is the default: a step ending FAIL or ERROR pauses the run and
--     the Operator chooses Continue, Retry or Abort.
--   * Retry runs the step's action again from the beginning. Each Attempt is recorded;
--     the step's result is its last Attempt's, and the Operator's choice is recorded too.
--   * ATE gives a step no way to read its own Attempt number, so the schedule keeps the
--     count itself in a local the action closes over.
local attempts = 0

return ScheduleParams("Manual: retry", "the first Attempt fails, the Retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Retry this one"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt " .. attempts .. ": the joint is cold", 0.2, 2.4)
        else
          ATE.report(Result.PASS, "attempt " .. attempts .. ": the joint is warm", 2.4, 2.4)
        end
      end),

    -- Only reached after the step passed, or after the Operator chose to continue.
    StepParams("Next check"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "the rail is steady", 5.0, 5.0)
      end),
  })
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

```lua:docs/manuals/examples/test-writer/udp.tslua
-- udp.tslua: a UDP Device, where one datagram is one message.
--
-- Demonstrates:
--   * UDP has no terminators, so readTerm and writeTerm are not fields this Device Type
--     knows; passing one is a schedule bug that raises naming the field;
--   * a datagram longer than the size asked for is truncated and returned with partial =
--     true, which the log calls a truncation rather than a timeout;
--   * the receive buffer is a queue of whole datagrams, so a reply that arrives before the
--     schedule reads it is not lost.
local devices = require("ate.devices")

local host = os.getenv("ATE_UDP_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_UDP_PORT")) or 5026

return ScheduleParams("Manual: UDP", "send and read datagrams")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Echo one datagram"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("PING"))
        local reply = ATE.read(dev)
        ATE.report(reply.data == "ECHO:PING" and Result.PASS or Result.FAIL,
                   "the peer echoed " .. (reply.data or "nothing"),
                   reply.count, 9)
        ATE.close(dev)
      end),

    -- Ask for 10 bytes of a 40-byte datagram: ATE returns the first 10 and says partial.
    StepParams("Truncate a datagram"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.UDP, devices.Udp(host, port):setTimeout(2.0))
        ATE.write(dev, devices.Write("a datagram longer than the size asked for"))
        local r = ATE.read(dev, devices.Read():setSize(10))
        ATE.report((r.count == 10 and r.partial) and Result.PASS or Result.FAIL,
                   "read " .. r.count .. " of a longer datagram",
                   r.count, 10)
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/serial.tslua
-- serial.tslua: a SERIAL Device through pyserial.
--
-- The port here is pyserial's socket:// URL, which opens a TCP connection to the
-- simulator peer; a real station names a COM or tty port instead.
--
-- Demonstrates:
--   * open has no defaults, so every field is named;
--   * the Parity and FlowControl enums;
--   * the same connection read with the Device's own terminator.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: SERIAL", "open a serial port and read a reply")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Open the port"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.SERIAL, {
          port = "socket://" .. host .. ":" .. port,
          baud = 115200,
          dataBits = 8,
          stopBits = 1,
          parity = Parity.NONE,
          flowControl = FlowControl.NONE,
          timeout = 2.0,
          readTerm = "\n",
          writeTerm = "\n",
        })
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "serial reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/visa.tslua
-- visa.tslua: a VISA instrument through pyvisa.
--
-- VISA has no background reader: ATE reads the instrument only when the schedule asks,
-- because polling an SCPI instrument with reads that have nothing pending makes it log
-- "Query UNTERMINATED" (-420) and ties up the bus. ATE ships pyvisa-py; a VISA resource
-- string names the instrument, here the simulator peer as a plain TCPIP socket.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: VISA", "read an instrument through pyvisa")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Ask the instrument who it is"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.VISA,
          devices.Visa("TCPIP0::" .. host .. "::" .. port .. "::SOCKET"):setTimeout(2.0))
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        local f = ATE.flush(dev)   -- VISA has no receive buffer: this discards nothing
        ATE.log(Log.INFO, "flush discarded " .. f.count .. " bytes")
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "VISA reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

A handle lives for the whole run. Closing a closed Device does nothing and reports success;
using one is a failure through `onError`. At the end of every run ATE closes whatever is still
open and logs a WARNING for each.

```lua:docs/manuals/examples/test-writer/left_open.tslua
-- left_open.tslua: a Device the schedule never closes.
--
-- A Test Run closes every Device still open when it ends, and logs one WARNING naming
-- each one. That is why a Device opened in a Pre-test Action does not have to be closed
-- by hand - it is worth closing anyway, so the log stays clean.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: a Device left open", "ATE closes what the schedule forgets")
  :setSchedule({
    StepParams("Open and forget"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.report(Result.PASS, "opened a Device and left it open")
      end),
  })
```

## Device failures, Stop on Fail, Retry, Abort

`onError(opResult, opArgs)` is called when the operation fails. If it returns anything but
`nil`, the call returns that value and the schedule continues. If it returns `nil`, or there is
no handler, the call raises a Lua error and the step records `Result.ERROR`. A read that times
out having received some bytes is not a failure: it returns `partial = true` and logs a
WARNING. ATE never reconnects: close the Device and open it again with the same params.

A field the Device Type does not know, a missing required field, or a wrong-typed value is not a
Device failure. It never reaches `onError`; the call returns `false` with the message as its
second result and the traceback is logged at ERROR.

```lua:docs/manuals/examples/test-writer/errors.tslua
-- errors.tslua: how Device failures reach the schedule, and what does not.
--
-- Demonstrates:
--   * a Device failure with no onError raises a Lua error, which ends the step as
--     Result.ERROR;
--   * an onError that returns a value makes the call continue with that value;
--   * a bad argument (a misspelled field) is a schedule bug, not a Device failure: it
--     never reaches onError, it is logged at ERROR, and the call returns false with the
--     message as its second result;
--   * the handler given to open is the fallback for that Device's read, write and close.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025
local deadPort = tonumber(os.getenv("ATE_DEAD_PORT")) or 1

return ScheduleParams("Manual: Device errors", "onError, a closed Device and a bad field")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A misspelled field"):setStepNo("1.")
      :setAction(function()
        local opened, why = ATE.open(DeviceType.TCP, { host = host, port = port, timeout = 1.0,
                                                     readTerm = "\n", writeTerm = "\n", buad = 9600 })
        ATE.report(opened == false and Result.PASS or Result.FAIL, "the open was refused: " .. why)
      end),

    -- Nothing is listening on deadPort: the open fails. This handler logs and returns nil,
    -- which means "no recovery", so ATE.open raises and the step records Result.ERROR.
    StepParams("A port nothing listens on"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, deadPort):setTimeout(0.5),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "nothing is listening on " .. opArgs.host .. ":" .. opArgs.port)
            return nil
          end)
        ATE.report(Result.PASS, "this line is never reached")
      end),

    -- A Device that is closed cannot be read. With no handler the call raises.
    StepParams("Read a closed Device"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.close(dev)
        ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(Result.PASS, "this line is never reached either")
      end),

    -- The same failure, handled: the step carries on with what onError returns.
    StepParams("Handle the closed Device"):setStepNo("4.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "the Device is gone: " .. opResult.err)
            return { data = nil, count = 0, partial = false }
          end)
        ATE.close(dev)
        local r = ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "recovered from reading a closed Device")
      end),
  })
```

Stop on Fail pauses the run at a `FAIL` or `ERROR` step so the Operator can choose `CONTINUE`,
`RETRY` or `ABORT`. Retry runs the action again from the start with POLL reset and blocking
inputs cleared; each Attempt is recorded and the step's result is the last Attempt's. There is
no Attempt number in Lua - keep your own count.

```lua:docs/manuals/examples/test-writer/retry.tslua
-- retry.tslua: Stop on Fail, and what a Retry does.
--
-- Demonstrates:
--   * setStopOnFail(true) is the default: a step ending FAIL or ERROR pauses the run and
--     the Operator chooses Continue, Retry or Abort.
--   * Retry runs the step's action again from the beginning. Each Attempt is recorded;
--     the step's result is its last Attempt's, and the Operator's choice is recorded too.
--   * ATE gives a step no way to read its own Attempt number, so the schedule keeps the
--     count itself in a local the action closes over.
local attempts = 0

return ScheduleParams("Manual: retry", "the first Attempt fails, the Retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Retry this one"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt " .. attempts .. ": the joint is cold", 0.2, 2.4)
        else
          ATE.report(Result.PASS, "attempt " .. attempts .. ": the joint is warm", 2.4, 2.4)
        end
      end),

    -- Only reached after the step passed, or after the Operator chose to continue.
    StepParams("Next check"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "the rail is steady", 5.0, 5.0)
      end),
  })
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

```lua:docs/manuals/examples/test-writer/serial.tslua
-- serial.tslua: a SERIAL Device through pyserial.
--
-- The port here is pyserial's socket:// URL, which opens a TCP connection to the
-- simulator peer; a real station names a COM or tty port instead.
--
-- Demonstrates:
--   * open has no defaults, so every field is named;
--   * the Parity and FlowControl enums;
--   * the same connection read with the Device's own terminator.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: SERIAL", "open a serial port and read a reply")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Open the port"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.SERIAL, {
          port = "socket://" .. host .. ":" .. port,
          baud = 115200,
          dataBits = 8,
          stopBits = 1,
          parity = Parity.NONE,
          flowControl = FlowControl.NONE,
          timeout = 2.0,
          readTerm = "\n",
          writeTerm = "\n",
        })
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "serial reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

```lua:docs/manuals/examples/test-writer/visa.tslua
-- visa.tslua: a VISA instrument through pyvisa.
--
-- VISA has no background reader: ATE reads the instrument only when the schedule asks,
-- because polling an SCPI instrument with reads that have nothing pending makes it log
-- "Query UNTERMINATED" (-420) and ties up the bus. ATE ships pyvisa-py; a VISA resource
-- string names the instrument, here the simulator peer as a plain TCPIP socket.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: VISA", "read an instrument through pyvisa")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Ask the instrument who it is"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.VISA,
          devices.Visa("TCPIP0::" .. host .. "::" .. port .. "::SOCKET"):setTimeout(2.0))
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        local f = ATE.flush(dev)   -- VISA has no receive buffer: this discards nothing
        ATE.log(Log.INFO, "flush discarded " .. f.count .. " bytes")
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "VISA reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

A handle lives for the whole run. Closing a closed Device does nothing and reports success;
using one is a failure through `onError`. At the end of every run ATE closes whatever is still
open and logs a WARNING for each.

```lua:docs/manuals/examples/test-writer/left_open.tslua
-- left_open.tslua: a Device the schedule never closes.
--
-- A Test Run closes every Device still open when it ends, and logs one WARNING naming
-- each one. That is why a Device opened in a Pre-test Action does not have to be closed
-- by hand - it is worth closing anyway, so the log stays clean.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: a Device left open", "ATE closes what the schedule forgets")
  :setSchedule({
    StepParams("Open and forget"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.report(Result.PASS, "opened a Device and left it open")
      end),
  })
```

## Device failures, Stop on Fail, Retry, Abort

`onError(opResult, opArgs)` is called when the operation fails. If it returns anything but
`nil`, the call returns that value and the schedule continues. If it returns `nil`, or there is
no handler, the call raises a Lua error and the step records `Result.ERROR`. A read that times
out having received some bytes is not a failure: it returns `partial = true` and logs a
WARNING. ATE never reconnects: close the Device and open it again with the same params.

A field the Device Type does not know, a missing required field, or a wrong-typed value is not a
Device failure. It never reaches `onError`; the call returns `false` with the message as its
second result and the traceback is logged at ERROR.

```lua:docs/manuals/examples/test-writer/errors.tslua
-- errors.tslua: how Device failures reach the schedule, and what does not.
--
-- Demonstrates:
--   * a Device failure with no onError raises a Lua error, which ends the step as
--     Result.ERROR;
--   * an onError that returns a value makes the call continue with that value;
--   * a bad argument (a misspelled field) is a schedule bug, not a Device failure: it
--     never reaches onError, it is logged at ERROR, and the call returns false with the
--     message as its second result;
--   * the handler given to open is the fallback for that Device's read, write and close.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025
local deadPort = tonumber(os.getenv("ATE_DEAD_PORT")) or 1

return ScheduleParams("Manual: Device errors", "onError, a closed Device and a bad field")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A misspelled field"):setStepNo("1.")
      :setAction(function()
        local opened, why = ATE.open(DeviceType.TCP, { host = host, port = port, timeout = 1.0,
                                                     readTerm = "\n", writeTerm = "\n", buad = 9600 })
        ATE.report(opened == false and Result.PASS or Result.FAIL, "the open was refused: " .. why)
      end),

    -- Nothing is listening on deadPort: the open fails. This handler logs and returns nil,
    -- which means "no recovery", so ATE.open raises and the step records Result.ERROR.
    StepParams("A port nothing listens on"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, deadPort):setTimeout(0.5),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "nothing is listening on " .. opArgs.host .. ":" .. opArgs.port)
            return nil
          end)
        ATE.report(Result.PASS, "this line is never reached")
      end),

    -- A Device that is closed cannot be read. With no handler the call raises.
    StepParams("Read a closed Device"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.close(dev)
        ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(Result.PASS, "this line is never reached either")
      end),

    -- The same failure, handled: the step carries on with what onError returns.
    StepParams("Handle the closed Device"):setStepNo("4.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "the Device is gone: " .. opResult.err)
            return { data = nil, count = 0, partial = false }
          end)
        ATE.close(dev)
        local r = ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "recovered from reading a closed Device")
      end),
  })
```

Stop on Fail pauses the run at a `FAIL` or `ERROR` step so the Operator can choose `CONTINUE`,
`RETRY` or `ABORT`. Retry runs the action again from the start with POLL reset and blocking
inputs cleared; each Attempt is recorded and the step's result is the last Attempt's. There is
no Attempt number in Lua - keep your own count.

```lua:docs/manuals/examples/test-writer/retry.tslua
-- retry.tslua: Stop on Fail, and what a Retry does.
--
-- Demonstrates:
--   * setStopOnFail(true) is the default: a step ending FAIL or ERROR pauses the run and
--     the Operator chooses Continue, Retry or Abort.
--   * Retry runs the step's action again from the beginning. Each Attempt is recorded;
--     the step's result is its last Attempt's, and the Operator's choice is recorded too.
--   * ATE gives a step no way to read its own Attempt number, so the schedule keeps the
--     count itself in a local the action closes over.
local attempts = 0

return ScheduleParams("Manual: retry", "the first Attempt fails, the Retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Retry this one"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt " .. attempts .. ": the joint is cold", 0.2, 2.4)
        else
          ATE.report(Result.PASS, "attempt " .. attempts .. ": the joint is warm", 2.4, 2.4)
        end
      end),

    -- Only reached after the step passed, or after the Operator chose to continue.
    StepParams("Next check"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "the rail is steady", 5.0, 5.0)
      end),
  })
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

```lua:docs/manuals/examples/test-writer/visa.tslua
-- visa.tslua: a VISA instrument through pyvisa.
--
-- VISA has no background reader: ATE reads the instrument only when the schedule asks,
-- because polling an SCPI instrument with reads that have nothing pending makes it log
-- "Query UNTERMINATED" (-420) and ties up the bus. ATE ships pyvisa-py; a VISA resource
-- string names the instrument, here the simulator peer as a plain TCPIP socket.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: VISA", "read an instrument through pyvisa")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("Ask the instrument who it is"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.VISA,
          devices.Visa("TCPIP0::" .. host .. "::" .. port .. "::SOCKET"):setTimeout(2.0))
        ATE.write(dev, devices.Write("*IDN?"))
        local reply = ATE.read(dev)
        local f = ATE.flush(dev)   -- VISA has no receive buffer: this discards nothing
        ATE.log(Log.INFO, "flush discarded " .. f.count .. " bytes")
        ATE.report(reply.count > 0 and Result.PASS or Result.FAIL, "VISA reply: " .. (reply.data or "nothing"))
        ATE.close(dev)
      end),
  })
```

A handle lives for the whole run. Closing a closed Device does nothing and reports success;
using one is a failure through `onError`. At the end of every run ATE closes whatever is still
open and logs a WARNING for each.

```lua:docs/manuals/examples/test-writer/left_open.tslua
-- left_open.tslua: a Device the schedule never closes.
--
-- A Test Run closes every Device still open when it ends, and logs one WARNING naming
-- each one. That is why a Device opened in a Pre-test Action does not have to be closed
-- by hand - it is worth closing anyway, so the log stays clean.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: a Device left open", "ATE closes what the schedule forgets")
  :setSchedule({
    StepParams("Open and forget"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.report(Result.PASS, "opened a Device and left it open")
      end),
  })
```

## Device failures, Stop on Fail, Retry, Abort

`onError(opResult, opArgs)` is called when the operation fails. If it returns anything but
`nil`, the call returns that value and the schedule continues. If it returns `nil`, or there is
no handler, the call raises a Lua error and the step records `Result.ERROR`. A read that times
out having received some bytes is not a failure: it returns `partial = true` and logs a
WARNING. ATE never reconnects: close the Device and open it again with the same params.

A field the Device Type does not know, a missing required field, or a wrong-typed value is not a
Device failure. It never reaches `onError`; the call returns `false` with the message as its
second result and the traceback is logged at ERROR.

```lua:docs/manuals/examples/test-writer/errors.tslua
-- errors.tslua: how Device failures reach the schedule, and what does not.
--
-- Demonstrates:
--   * a Device failure with no onError raises a Lua error, which ends the step as
--     Result.ERROR;
--   * an onError that returns a value makes the call continue with that value;
--   * a bad argument (a misspelled field) is a schedule bug, not a Device failure: it
--     never reaches onError, it is logged at ERROR, and the call returns false with the
--     message as its second result;
--   * the handler given to open is the fallback for that Device's read, write and close.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025
local deadPort = tonumber(os.getenv("ATE_DEAD_PORT")) or 1

return ScheduleParams("Manual: Device errors", "onError, a closed Device and a bad field")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A misspelled field"):setStepNo("1.")
      :setAction(function()
        local opened, why = ATE.open(DeviceType.TCP, { host = host, port = port, timeout = 1.0,
                                                     readTerm = "\n", writeTerm = "\n", buad = 9600 })
        ATE.report(opened == false and Result.PASS or Result.FAIL, "the open was refused: " .. why)
      end),

    -- Nothing is listening on deadPort: the open fails. This handler logs and returns nil,
    -- which means "no recovery", so ATE.open raises and the step records Result.ERROR.
    StepParams("A port nothing listens on"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, deadPort):setTimeout(0.5),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "nothing is listening on " .. opArgs.host .. ":" .. opArgs.port)
            return nil
          end)
        ATE.report(Result.PASS, "this line is never reached")
      end),

    -- A Device that is closed cannot be read. With no handler the call raises.
    StepParams("Read a closed Device"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.close(dev)
        ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(Result.PASS, "this line is never reached either")
      end),

    -- The same failure, handled: the step carries on with what onError returns.
    StepParams("Handle the closed Device"):setStepNo("4.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "the Device is gone: " .. opResult.err)
            return { data = nil, count = 0, partial = false }
          end)
        ATE.close(dev)
        local r = ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "recovered from reading a closed Device")
      end),
  })
```

Stop on Fail pauses the run at a `FAIL` or `ERROR` step so the Operator can choose `CONTINUE`,
`RETRY` or `ABORT`. Retry runs the action again from the start with POLL reset and blocking
inputs cleared; each Attempt is recorded and the step's result is the last Attempt's. There is
no Attempt number in Lua - keep your own count.

```lua:docs/manuals/examples/test-writer/retry.tslua
-- retry.tslua: Stop on Fail, and what a Retry does.
--
-- Demonstrates:
--   * setStopOnFail(true) is the default: a step ending FAIL or ERROR pauses the run and
--     the Operator chooses Continue, Retry or Abort.
--   * Retry runs the step's action again from the beginning. Each Attempt is recorded;
--     the step's result is its last Attempt's, and the Operator's choice is recorded too.
--   * ATE gives a step no way to read its own Attempt number, so the schedule keeps the
--     count itself in a local the action closes over.
local attempts = 0

return ScheduleParams("Manual: retry", "the first Attempt fails, the Retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Retry this one"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt " .. attempts .. ": the joint is cold", 0.2, 2.4)
        else
          ATE.report(Result.PASS, "attempt " .. attempts .. ": the joint is warm", 2.4, 2.4)
        end
      end),

    -- Only reached after the step passed, or after the Operator chose to continue.
    StepParams("Next check"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "the rail is steady", 5.0, 5.0)
      end),
  })
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

A handle lives for the whole run. Closing a closed Device does nothing and reports success;
using one is a failure through `onError`. At the end of every run ATE closes whatever is still
open and logs a WARNING for each.

```lua:docs/manuals/examples/test-writer/left_open.tslua
-- left_open.tslua: a Device the schedule never closes.
--
-- A Test Run closes every Device still open when it ends, and logs one WARNING naming
-- each one. That is why a Device opened in a Pre-test Action does not have to be closed
-- by hand - it is worth closing anyway, so the log stays clean.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025

return ScheduleParams("Manual: a Device left open", "ATE closes what the schedule forgets")
  :setSchedule({
    StepParams("Open and forget"):setStepNo("1.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.report(Result.PASS, "opened a Device and left it open")
      end),
  })
```

## Device failures, Stop on Fail, Retry, Abort

`onError(opResult, opArgs)` is called when the operation fails. If it returns anything but
`nil`, the call returns that value and the schedule continues. If it returns `nil`, or there is
no handler, the call raises a Lua error and the step records `Result.ERROR`. A read that times
out having received some bytes is not a failure: it returns `partial = true` and logs a
WARNING. ATE never reconnects: close the Device and open it again with the same params.

A field the Device Type does not know, a missing required field, or a wrong-typed value is not a
Device failure. It never reaches `onError`; the call returns `false` with the message as its
second result and the traceback is logged at ERROR.

```lua:docs/manuals/examples/test-writer/errors.tslua
-- errors.tslua: how Device failures reach the schedule, and what does not.
--
-- Demonstrates:
--   * a Device failure with no onError raises a Lua error, which ends the step as
--     Result.ERROR;
--   * an onError that returns a value makes the call continue with that value;
--   * a bad argument (a misspelled field) is a schedule bug, not a Device failure: it
--     never reaches onError, it is logged at ERROR, and the call returns false with the
--     message as its second result;
--   * the handler given to open is the fallback for that Device's read, write and close.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025
local deadPort = tonumber(os.getenv("ATE_DEAD_PORT")) or 1

return ScheduleParams("Manual: Device errors", "onError, a closed Device and a bad field")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A misspelled field"):setStepNo("1.")
      :setAction(function()
        local opened, why = ATE.open(DeviceType.TCP, { host = host, port = port, timeout = 1.0,
                                                     readTerm = "\n", writeTerm = "\n", buad = 9600 })
        ATE.report(opened == false and Result.PASS or Result.FAIL, "the open was refused: " .. why)
      end),

    -- Nothing is listening on deadPort: the open fails. This handler logs and returns nil,
    -- which means "no recovery", so ATE.open raises and the step records Result.ERROR.
    StepParams("A port nothing listens on"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, deadPort):setTimeout(0.5),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "nothing is listening on " .. opArgs.host .. ":" .. opArgs.port)
            return nil
          end)
        ATE.report(Result.PASS, "this line is never reached")
      end),

    -- A Device that is closed cannot be read. With no handler the call raises.
    StepParams("Read a closed Device"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.close(dev)
        ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(Result.PASS, "this line is never reached either")
      end),

    -- The same failure, handled: the step carries on with what onError returns.
    StepParams("Handle the closed Device"):setStepNo("4.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "the Device is gone: " .. opResult.err)
            return { data = nil, count = 0, partial = false }
          end)
        ATE.close(dev)
        local r = ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "recovered from reading a closed Device")
      end),
  })
```

Stop on Fail pauses the run at a `FAIL` or `ERROR` step so the Operator can choose `CONTINUE`,
`RETRY` or `ABORT`. Retry runs the action again from the start with POLL reset and blocking
inputs cleared; each Attempt is recorded and the step's result is the last Attempt's. There is
no Attempt number in Lua - keep your own count.

```lua:docs/manuals/examples/test-writer/retry.tslua
-- retry.tslua: Stop on Fail, and what a Retry does.
--
-- Demonstrates:
--   * setStopOnFail(true) is the default: a step ending FAIL or ERROR pauses the run and
--     the Operator chooses Continue, Retry or Abort.
--   * Retry runs the step's action again from the beginning. Each Attempt is recorded;
--     the step's result is its last Attempt's, and the Operator's choice is recorded too.
--   * ATE gives a step no way to read its own Attempt number, so the schedule keeps the
--     count itself in a local the action closes over.
local attempts = 0

return ScheduleParams("Manual: retry", "the first Attempt fails, the Retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Retry this one"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt " .. attempts .. ": the joint is cold", 0.2, 2.4)
        else
          ATE.report(Result.PASS, "attempt " .. attempts .. ": the joint is warm", 2.4, 2.4)
        end
      end),

    -- Only reached after the step passed, or after the Operator chose to continue.
    StepParams("Next check"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "the rail is steady", 5.0, 5.0)
      end),
  })
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

## Device failures, Stop on Fail, Retry, Abort

`onError(opResult, opArgs)` is called when the operation fails. If it returns anything but
`nil`, the call returns that value and the schedule continues. If it returns `nil`, or there is
no handler, the call raises a Lua error and the step records `Result.ERROR`. A read that times
out having received some bytes is not a failure: it returns `partial = true` and logs a
WARNING. ATE never reconnects: close the Device and open it again with the same params.

A field the Device Type does not know, a missing required field, or a wrong-typed value is not a
Device failure. It never reaches `onError`; the call returns `false` with the message as its
second result and the traceback is logged at ERROR.

```lua:docs/manuals/examples/test-writer/errors.tslua
-- errors.tslua: how Device failures reach the schedule, and what does not.
--
-- Demonstrates:
--   * a Device failure with no onError raises a Lua error, which ends the step as
--     Result.ERROR;
--   * an onError that returns a value makes the call continue with that value;
--   * a bad argument (a misspelled field) is a schedule bug, not a Device failure: it
--     never reaches onError, it is logged at ERROR, and the call returns false with the
--     message as its second result;
--   * the handler given to open is the fallback for that Device's read, write and close.
local devices = require("ate.devices")

local host = os.getenv("ATE_SCPI_HOST") or "127.0.0.1"
local port = tonumber(os.getenv("ATE_SCPI_PORT")) or 5025
local deadPort = tonumber(os.getenv("ATE_DEAD_PORT")) or 1

return ScheduleParams("Manual: Device errors", "onError, a closed Device and a bad field")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("A misspelled field"):setStepNo("1.")
      :setAction(function()
        local opened, why = ATE.open(DeviceType.TCP, { host = host, port = port, timeout = 1.0,
                                                     readTerm = "\n", writeTerm = "\n", buad = 9600 })
        ATE.report(opened == false and Result.PASS or Result.FAIL, "the open was refused: " .. why)
      end),

    -- Nothing is listening on deadPort: the open fails. This handler logs and returns nil,
    -- which means "no recovery", so ATE.open raises and the step records Result.ERROR.
    StepParams("A port nothing listens on"):setStepNo("2.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, deadPort):setTimeout(0.5),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "nothing is listening on " .. opArgs.host .. ":" .. opArgs.port)
            return nil
          end)
        ATE.report(Result.PASS, "this line is never reached")
      end),

    -- A Device that is closed cannot be read. With no handler the call raises.
    StepParams("Read a closed Device"):setStepNo("3.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0))
        ATE.close(dev)
        ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(Result.PASS, "this line is never reached either")
      end),

    -- The same failure, handled: the step carries on with what onError returns.
    StepParams("Handle the closed Device"):setStepNo("4.")
      :setAction(function()
        local dev = ATE.open(DeviceType.TCP, devices.Tcp(host, port):setTimeout(1.0),
          function(opResult, opArgs)
            ATE.log(Log.WARNING, "the Device is gone: " .. opResult.err)
            return { data = nil, count = 0, partial = false }
          end)
        ATE.close(dev)
        local r = ATE.read(dev, devices.Read():setTimeout(0.2))
        ATE.report(r.count == 0 and Result.PASS or Result.FAIL, "recovered from reading a closed Device")
      end),
  })
```

Stop on Fail pauses the run at a `FAIL` or `ERROR` step so the Operator can choose `CONTINUE`,
`RETRY` or `ABORT`. Retry runs the action again from the start with POLL reset and blocking
inputs cleared; each Attempt is recorded and the step's result is the last Attempt's. There is
no Attempt number in Lua - keep your own count.

```lua:docs/manuals/examples/test-writer/retry.tslua
-- retry.tslua: Stop on Fail, and what a Retry does.
--
-- Demonstrates:
--   * setStopOnFail(true) is the default: a step ending FAIL or ERROR pauses the run and
--     the Operator chooses Continue, Retry or Abort.
--   * Retry runs the step's action again from the beginning. Each Attempt is recorded;
--     the step's result is its last Attempt's, and the Operator's choice is recorded too.
--   * ATE gives a step no way to read its own Attempt number, so the schedule keeps the
--     count itself in a local the action closes over.
local attempts = 0

return ScheduleParams("Manual: retry", "the first Attempt fails, the Retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Retry this one"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt " .. attempts .. ": the joint is cold", 0.2, 2.4)
        else
          ATE.report(Result.PASS, "attempt " .. attempts .. ": the joint is warm", 2.4, 2.4)
        end
      end),

    -- Only reached after the step passed, or after the Operator chose to continue.
    StepParams("Next check"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "the rail is steady", 5.0, 5.0)
      end),
  })
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

Stop on Fail pauses the run at a `FAIL` or `ERROR` step so the Operator can choose `CONTINUE`,
`RETRY` or `ABORT`. Retry runs the action again from the start with POLL reset and blocking
inputs cleared; each Attempt is recorded and the step's result is the last Attempt's. There is
no Attempt number in Lua - keep your own count.

```lua:docs/manuals/examples/test-writer/retry.tslua
-- retry.tslua: Stop on Fail, and what a Retry does.
--
-- Demonstrates:
--   * setStopOnFail(true) is the default: a step ending FAIL or ERROR pauses the run and
--     the Operator chooses Continue, Retry or Abort.
--   * Retry runs the step's action again from the beginning. Each Attempt is recorded;
--     the step's result is its last Attempt's, and the Operator's choice is recorded too.
--   * ATE gives a step no way to read its own Attempt number, so the schedule keeps the
--     count itself in a local the action closes over.
local attempts = 0

return ScheduleParams("Manual: retry", "the first Attempt fails, the Retry passes")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Retry this one"):setStepNo("1.")
      :setAction(function()
        attempts = attempts + 1
        if attempts == 1 then
          ATE.report(Result.FAIL, "attempt " .. attempts .. ": the joint is cold", 0.2, 2.4)
        else
          ATE.report(Result.PASS, "attempt " .. attempts .. ": the joint is warm", 2.4, 2.4)
        end
      end),

    -- Only reached after the step passed, or after the Operator chose to continue.
    StepParams("Next check"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "the rail is steady", 5.0, 5.0)
      end),
  })
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

Abort is whole-run. The running step records `ERROR` "Aborted by Operator", every step that
never ran records `NOTRUN`, and the run's End Reason is `ABORTED`. It cannot be caught: after
an abort `ATE` calls raise `"aborted"`, a busy loop in the step's own code is stopped by the
instruction-count hook (`debug.sethook(check_abort, "", 1000)`), and `pcall` and `xpcall`
re-raise. ATE aims to stop in 0.5 s and the suite fails a run that takes longer than 10 s.

That 10 s holds only for what the hook can reach. Story 83 names two exceptions as allowed rather
than as bugs, both measured against the shipped code. They are not forbidden; they are the two
cases where the abort has to wait, and a writer should know which steps they apply to.

- **A command run by `os.execute` is allowed, and the abort waits for it.** `os.execute` is
  available in a step. A step that ran `os.execute("sleep 20")` was aborted 19.5 s before it ended:
  the run finished only when the command did. The step still reported `PASS`. ATE cannot end a
  process it did not start.
- **A busy loop inside a coroutine is allowed, and the hook does not reach it.** A loop of
  `120000000` iterations inside `coroutine.wrap` ran to completion after an abort: 2.67 s from the
  abort to `FINISHED`, the step reporting `PASS` and the next step recording the abort. An unbounded
  `while true do end` inside `coroutine.wrap` stopped only when the process was killed, its
  `test_run` row left open. The engine's own docstring advises against coroutines for this reason
  (`src/ate/engine/run_engine.py`, "Because a schedule must not use coroutines"): the exception is
  real, but it is hard to stop.

`os.exit` is not an exception and stays a never. `os.exit(7)` in a step exits the process with
status 7. The run row is left with no `ended_at` and no result; the next launch closes it as
`INTERRUPTED`/`FAIL`. No Post-test Action runs, and no report is written.

A plain loop in a step, `ATE.sleep` and `ATE.input` all do stop within the 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. Two cases are documented exceptions, allowed rather than
-- forbidden: a command run with os.execute, which the abort waits for, and a busy loop
-- inside a coroutine, which the hook never reaches. A step that is running when the abort
-- lands records Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
--
-- Post-test Actions still run after an abort - ATE clears the abort flag before them so
-- a run can always tear down - and each one is called with the run's result, which is
-- FAIL for an aborted run. A second abort ends only the Action it lands in.
return ScheduleParams("Manual: abort", "a sleep the Operator aborts, and the teardown that runs anyway")
  :setPostTestAction(function(passFail)
    ATE.log(Log.INFO, "post-test action ran after the abort, result " .. passFail)
  end)
  :setSchedule({
    StepParams("Wait for the rail to settle"):setStepNo("1.")
      :setAction(function()
        ATE.sleep(10.0)                 -- the Operator aborts while this is waiting
        ATE.report(Result.PASS, "settled")
      end),

    StepParams("Measure after the wait"):setStepNo("2.")
      :setAction(function()
        ATE.report(Result.PASS, "this step never runs")
      end),
  })
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

## The sandbox

`dofile`, `loadfile`, `load` and `python` are gone. `package` exists but `package.path` and
`package.cpath` are empty. `debug` is reduced to `debug.traceback`. `io`, `os`, `string`,
`table` and the rest of the standard library are still there. This is traceability, not
security: ATE needs to know which files a run ran. `os.execute` is reachable from a step and is
allowed: the only cost is that an abort waits for the command (see Abort). `os.exit` is also
reachable, and is the one thing in `os` never to call, because it ends the process with nothing
recorded. A schedule can still open a file with `io.open` if it names a path.

```lua:docs/manuals/examples/test-writer/sandbox.tslua
-- sandbox.tslua: what the Test Schedule sandbox gives a schedule, and takes away.
--
-- The sandbox is about traceability, not security: ATE wants to know exactly which files
-- a Test Run ran, and it cannot do that if a schedule reads Lua from wherever it likes.
--   * dofile, loadfile, load and the python table are gone; calling one is an error;
--   * package exists but its path and cpath are empty, so nothing outside the Test
--     Definition can be required;
--   * debug is reduced to debug.traceback;
--   * io, os, string, table and the rest of the standard library are still there.
local devices = require("ate.devices")   -- the ate. namespace is ATE's own

return ScheduleParams("Manual: the sandbox", "what a Test Schedule can reach")
  :setSchedule({
    StepParams("Check what is removed"):setStepNo("1.")
      :setAction(function()
        local removed = dofile == nil and loadfile == nil and load == nil and python == nil
        local noPaths = type(package) == "table" and package.path == "" and package.cpath == ""
        ATE.log(Log.INFO, "debug now holds only: " .. table.concat(
          (function() local t = {} for k in pairs(debug) do t[#t + 1] = k end return t end)(), ", "))
        ATE.report((removed and noPaths) and Result.PASS or Result.FAIL,
                   "the file-loading functions are gone",
                   tostring(removed) .. "/" .. tostring(noPaths), "true/true")
      end),

    -- os and io still work, which is why this is about traceability rather than safety.
    StepParams("The standard library is there"):setStepNo("2.")
      :setAction(function()
        local now = os.time()
        local joined = string.format("%s", table.concat({ "a", "b" }, "-"))
        ATE.report(Result.PASS, "os.time and string.format work", joined, "a-b")
      end),
  })
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

Where each call is legal:

- top level while the file loads: builders, `require`, `ATE.log`, the enums. Everything else
  raises - `ATE.open` at load time says `only legal after the schedule has loaded`.
- Pre-test and Post-test Actions: everything except `ATE.input` and `ATE.report`.
- A step action: everything.

```lua:docs/manuals/examples/test-writer/forbidden.tslua
-- forbidden.tslua: a Test Schedule that fails while it loads.
--
-- A Device is opened inside a Test Run. At the top level of the file there is no run yet,
-- so only the builders, require, ATE.log and the enums are legal there. ATE.open at load
-- time is refused, and the schedule is listed as broken on the Run screen with that error;
-- its Media Release is not.
ATE.open(DeviceType.TCP, { host = "127.0.0.1", port = 5025, timeout = 1.0,
                           readTerm = "\n", writeTerm = "\n" })

return ScheduleParams("Manual: forbidden at load", "opens a Device while the file is loading")
  :setSchedule({
    StepParams("Never reached"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS) end),
  })
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        polls[name.strip()] = (1, value.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

## The `ATE` table

```
ATE.open(deviceType, params [, onError])   opens a Device
ATE.read(dev [, params] [, onError])       reads from it
ATE.write(dev, params [, onError])         writes to it
ATE.close(dev [, params] [, onError])      closes it
ATE.flush(dev)                            discards the receive buffer
ATE.sleep(seconds)                        pauses the step, abortable
ATE.input(n)                              the step's n-th Step Input
ATE.report(passFail, msg, actual, expected) records the step's result
ATE.log(level, ...)                       writes a log line
ATE.addEquipment(name, serial, connection, calDue, notes) declares Equipment
ATE.run                                    the read-only run table
```

Enums:

```
Result:      PASS, FAIL, ERROR, SKIPPED, NOTRUN, FORCED_NOTRUN, NOREPORT
Log:         DEBUG, INFO, WARNING, ERROR
DeviceType:  TCP, SERIAL, UDP, VISA
Parity:      NONE, EVEN, ODD, MARK, SPACE
FlowControl: NONE, RTSCTS, XONXOFF, DSRDTR
```

## Running a schedule of your own

There is no `ate run <schedule>`. An installed ATE has no headless runner: what it has is
`ate _selftest --run`, which runs a built-in one-step schedule to prove a bundle can run a
Test Run, and `ate _selftest --scan DIR`, which loads every schedule in DIR and reports the
broken ones - a load check, not a run. To run your own schedule from a command line you need a
source checkout, and the script kept beside this manual does it:

```bash
QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
  --release docs/manuals/examples/test-writer --schedule "Manual: step inputs" \
  --input "Pick one=Green" --input "Tick several=A" --input "Press a button=RETEST" \
  --input "Whole number=42" --input "Decimal=1.25" --input "Text with a pattern=AB-1234" \
  --input "Blocking prompt=CONTINUE" --poll "POLL in a loop=STOP"
```

It writes a throwaway config in a temporary directory, runs the schedule through
`HeadlessRunSession`, and prints the run row, the step results, the Actions, the Equipment
count, the WARNING and ERROR log lines and the report paths. `--input` is keyed by the
**Step Name**, not the step number; `--choice` answers each Stop-on-Fail pause in order;
`--poll` clicks a POLL input's button (`--poll "Step name=2=STOP"` is the second POLL input of that
step, and a bare `--poll "Step name=STOP"` is the first); `--enable` turns a step on or off before
the run.

```python:docs/manuals/examples/test-writer/run_your_schedule.py
#!/usr/bin/env python3
"""Run one Test Schedule from a Media Release of your own, headless, and print its record.

Works from a source checkout only: it imports ``ate`` and the example harness from this
repository, and an installed ATE ships neither.

    QT_QPA_PLATFORM=offscreen python docs/manuals/examples/test-writer/run_your_schedule.py \
        --release docs/manuals/examples/test-writer --schedule "Manual: first schedule"

Every Step Input the schedule declares has to be answered or the worker blocks forever, so a
schedule with inputs needs ``--input`` for each one, keyed by the Step Name:

    --input "Operator input=1.5" --input "Pick one=Green"

A Stop-on-Fail pause needs an answer too, given in order with ``--choice``. A schedule whose
input is never answered does not fail: it hangs, and this script exits 1 saying
``schedule never finished``. An answer the step's own declaration rejects (a whole number
given as ``42.0``) exits 1 too, naming the value and the rule it broke.

``--poll`` clicks a POLL button by Step Name. A step with more than one POLL input needs the
input number as well: ``--poll "Step name=2=STOP"`` clicks the second input's button.

The Test Report PDF is stubbed out (``pdf_stub`` in the harness) so the run completes on a
machine with no fonts and no Chromium sandbox; the HTML report is still written.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]  # docs/manuals/examples/test-writer -> the repository
for _path in (_ROOT / "src", _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from examples.harness.peers import Peers  # noqa: E402
from examples.harness.run_examples import bench_env, pdf_stub, run_schedule, write_config  # noqa: E402


def parse_value(text: str) -> object:
    """``--input NAME=VALUE``: an int, then a float, otherwise the string as written."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_your_schedule.py", description=__doc__)
    parser.add_argument("--release", required=True, help="directory holding the .atedef and the Lua files")
    parser.add_argument("--schedule", required=True, help="the Test Schedule name, as the .atedef's file returns it")
    parser.add_argument("--input", action="append", default=[], metavar="STEP NAME=VALUE",
                        help="answer for one Step Input, keyed by the Step Name; repeat for each input")
    parser.add_argument("--choice", action="append", default=[], choices=("CONTINUE", "RETRY", "ABORT"),
                        help="answer to each Stop-on-Fail pause, in the order the pauses come up")
    parser.add_argument("--poll", action="append", default=[], metavar="STEP NAME[=N]=BUTTON",
                        help="click a POLL input's button, keyed by the Step Name; add =N for the "
                             "Nth POLL input of that step (default 1); repeat per POLL")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP NAME=true|false",
                        help="turn a step on or off before the run starts")
    parser.add_argument("--operator", default="Script runner")
    parser.add_argument("--project", default="Manual example")
    parser.add_argument("--uut-type", default="Example board")
    parser.add_argument("--uut-serial", default="RUN-0001")
    args = parser.parse_args()

    release = Path(args.release).resolve()
    if not release.is_dir():
        print(f"release directory not found: {release}", file=sys.stderr)
        return 1

    inputs = {}
    for item in args.input:
        name, _, value = item.partition("=")
        inputs[name.strip()] = parse_value(value)

    enable = {}
    for item in args.enable:
        name, _, value = item.partition("=")
        enable[name.strip()] = value.strip().lower() in ("true", "1", "yes")

    polls = {}
    for item in args.poll:
        name, _, value = item.partition("=")
        number, _, button = value.partition("=")
        if button:
            try:
                number = int(number)
            except ValueError:
                print(f"--poll {item!r}: the POLL input number must be an integer, "
                      f"as in STEP NAME[=N]=BUTTON", file=sys.stderr)
                return 1
        else:
            number, button = 1, value
        polls[name.strip()] = (number, button.strip())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = write_config(work, release)
        peers = Peers()
        peers.start()
        try:
            with bench_env(peers), pdf_stub():
                record = run_schedule(
                    config,
                    args.schedule,
                    inputs=inputs or None,
                    choices=args.choice or None,
                    enable=enable or None,
                    polls=polls or None,
                    release_dir=release,
                    operator=args.operator,
                    project=args.project,
                    uut_type=args.uut_type,
                    uut_serial=args.uut_serial,
                )
        except TimeoutError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            print("An unanswered Step Input blocks the worker: give every input a --input, "
                "and every Stop-on-Fail pause a --choice.", file=sys.stderr)
            print(f"The run row is left open: {config.parent / 'ate.db'}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"FAILED: an answer the schedule refused: {exc}", file=sys.stderr)
            print("A Step Input declaration checks its value: a whole-number input rejects 42.0, "
                "a pattern input rejects text that does not match.", file=sys.stderr)
            return 1
        finally:
            peers.stop()

        run = record["run"]
        steps = {n["step_number"]: n["result"] for n in record["nodes"] if n["kind"] == "step"}
        print(f"result={run['result']}  end_reason={run['end_reason']}  run_uuid={run['run_uuid']}")
        print(f"steps: {json.dumps(steps)}")
        print(f"actions: {[(a['phase'], a['outcome']) for a in record['actions']]}")
        print(f"equipment: {len(record['equipment'])}  log lines: {len(record['logs'])}")
        for line in record["logs"]:
            if line["level"] in ("WARNING", "ERROR"):
                print(f"  {line['level']}: {line['text']}")
        for html, pdf in record["reports"]:
            print(f"report: {html}" + (f"  pdf: {pdf}" if pdf else "  (no PDF)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
```

Two things this shows that are easy to get wrong:

- An unanswered Step Input does not fail the schedule. The worker blocks in
  `OperatorInput.get_raw` and the harness raises `TimeoutError: schedule never finished`. The
  run row is left open with no `ended_at`; the next launch closes it as `INTERRUPTED`/`FAIL`.
- A refused answer is refused inside the `input_asked` slot, in the worker thread, so the script
  sees it as a `ValueError` raised by the harness after the event loop stops, not as a traceback
  from the worker: `--input "Whole number=42.0"` exits 1 saying `42.0 refused: Enter a whole
  number`, and the step keeps waiting for a value nobody has.
- `--input` is keyed by Step Name, and a POLL is answered with `--poll`, not `--input`: a POLL
  is a button the Operator clicks while the step's loop reads it, never a blocking call.

The same shape is what `tests/test_manuals.py` uses for every example: `run_schedule(config,
"schedule name", inputs=..., choices=..., release_dir=...)`, asserting on the record it
returns. A schedule you have not run is a schedule whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(the same name with `.pdf`, when the PDF is rendered: an elevated Station is asked first, and
the default answer is Skip). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never call `os.exit` in a step: it ends the whole ATE process and leaves the run row open with
  nothing recorded.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

Two things are deliberately not on this list, because story 83 records them as allowed exceptions
to the must-stop-within-10-s rule: a command a step runs with `os.execute`, and a busy loop inside
a coroutine. Both are measured, both are explained under Abort, and neither is a reason to refuse
a schedule.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
