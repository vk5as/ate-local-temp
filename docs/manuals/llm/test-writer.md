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

## The schedule format

The top level of the file runs before any Test Run exists. It may use `require`, `ATE.log`, the
builders and the enums, and it must return a `ScheduleParams`. Everything else at the top level
is a load error.

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
raises aborts the run with `PRETEST_FAILED` and every step `NOTRUN`. Post-test Actions run even
after an abort: ATE clears the abort flag before them, an abort during one ends that Action
only, and an error in one fails the run while the rest still run.

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
rewrites every checksum in place.

```json:docs/manuals/examples/test-writer/manual.atedef
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
`size`, `timeout`, `readTerm`; `write` takes `data` (required), `writeTerm`, `timeout`; UDP's
`read` and `write` take no terminators. Builder setters: `setHost`, `setPort`, `setLocalPort`,
`setResource`, `setBaud`, `setDataBits`, `setStopBits`, `setParity`, `setFlowControl`,
`setTimeout`, `setReadTerm`, `setWriteTerm`, `setSize`, and `devices.Write(data)`.

Reads and writes are raw bytes. The terminator is stripped from `data`; `count` is what is
left. A write's `count` includes the terminator.

TCP, SERIAL and UDP are read in the background from `open` to `close` and buffered, capped at
`devices.receiveBufferBytes` (16 MiB); on overflow ATE drops the oldest bytes, logs one
WARNING, and the next read carries `overflow = true`. `ATE.flush(dev)` empties the buffer and
returns the count discarded. VISA has no background reader and no buffer, so `flush` discards
nothing.

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
an abort `ATE` calls raise `"aborted"`, a busy loop is stopped by the instruction-count hook,
and `pcall` and `xpcall` re-raise. ATE aims to stop in 0.5 s and must stop within 10 s.

```lua:docs/manuals/examples/test-writer/abort.tslua
-- abort.tslua: what an abort stops, and what it does not.
--
-- The Operator's Abort is a whole-run abort. ATE gives way within about 0.5 s and must
-- stop within 10 s: ATE.sleep and ATE.input wake at once, and a busy Lua loop is stopped
-- by the instruction-count hook. A step that is running when the abort lands records
-- Result.ERROR "Aborted by Operator"; every step that never ran records NOTRUN.
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
security: ATE needs to know which files a run ran.

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

## How to check a schedule worked

Run it headless and read the record back:

```bash
QT_QPA_PLATFORM=offscreen python -m pytest tests/test_manuals.py -q
```

`run_schedule(config, "schedule name", release_dir=Path("docs/manuals/examples/test-writer"))`
returns the run row, the nodes, the Attempts, the Actions, the log lines, the Equipment, the
input values and the report paths. Assert on that. A schedule you have not run is a schedule
whose record you do not know.

The report is written at run end to
`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`
(and the same `.pdf`). Its sections are Cover, Steps, Equipment, Configuration, Run log. Only
INFO, WARNING and ERROR lines appear in the report's Run log; DEBUG and Device trace stay in
the database.

## Never

- Never open a Device at the top level of the file. Open it in a Pre-test Action.
- Never call `ATE.report` or `ATE.input` in an Action.
- Never rely on `package.path`, `dofile` or `loadfile`: they do not work.
- Never use `pcall` to survive an abort: it re-raises.
- Never assume a checksum mismatch stops the run; it only warns.
- Never write to `ATE.run`; it is read-only.
- Never ship a schedule without running `ate definition update` on its `.atedef`.

## Where the assets and the code disagree

- The assets say a bad Device parameter "always raises". The code isolates it: the call returns
  `false` and the message, and the traceback is logged at ERROR. See
  `tests/test_device_onerror.py::test_bad_param_never_calls_handler`.
- The assets say only Post-test Actions are called with the run result. `_record_phase_action`
  passes the standing result to Pre-test Actions too, always `"PASS"`.
- The assets say the sandbox "has no `package` table". `package` exists; its `path` and `cpath`
  are empty strings.
