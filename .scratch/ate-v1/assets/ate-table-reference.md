# The `ATE` Lua table

ATE's definition of the `ATE` table that Test Schedules call, settled in "The ATE Lua API". Vocabulary follows `GLOSSARY.md`.

## Execution model

- A Test Step's action runs until it calls an `ATE` function. That call hands the work (Device I/O, waiting, Operator input) to the host, and the step resumes with the result once the work is done.
- The GUI stays responsive while a step runs, without help from the schedule.
- How ATE implements this (coroutine or worker thread) is decided in "Choose the Lua runtime"; schedules must not be able to tell the difference. Schedules cannot rely on using coroutines themselves.

## Devices

| Call | Returns | Meaning |
| --- | --- | --- |
| `ATE.open(deviceType, params [, onError])` | device object | Opens a Device of the given Device Type |
| `ATE.read(dev, params [, onError])` | `{data = <string>}` | Reads from an open Device |
| `ATE.write(dev, params [, onError])` | `{ok = true}` | Writes to an open Device |
| `ATE.close(dev, params [, onError])` | `{ok = true}` | Closes a Device and releases it |

- `deviceType` is a value of the `DeviceType` enum, whose values are the string keys of every Device Type ATE supports (e.g. `DeviceType.SERIAL`). Which types v1 supports is settled in "Device connection details and v1 connection types".
- `params` is a plain Lua table whose fields depend on the Device Type. For `open` it holds the connection details (for serial: COM port, baud rate, data bits, stop bits, parity and so on); for `read`, `write` and `close` it holds the details of that operation.
- ATE ships a utility Lua file of builder objects that construct these tables, in the same chained-setter style as `ScheduleParams`. The calls accept any table with the right fields, so the builders are a convenience, not a requirement.
- The device object is a Lua table holding a host handle in `_handle`. Schedules treat it as opaque and only pass it back to `read`, `write` and `close`.
- On failure, `read` returns `{data = nil, err = <message>}`, and `write` and `close` return `{ok = false, err = <message>}`. These shapes can gain fields later as needs arise.

### Errors and `onError`

- `onError` is an optional function, called as `onError(opResult, opArgs)` when the operation fails. `opResult` is the failure table the call would return (with `err` set); `opArgs` is the `params` table that was passed in.
- If `read`, `write` or `close` don't supply an `onError`, the one given to `open` is used.
- If `onError` returns a value other than `nil`, the schedule continues and the failed call returns that value.
- If `onError` returns `nil`, or there is no `onError` at all, the call raises a Lua error. Unless the schedule catches it, the step records `Result.ERROR`. Schedules are not expected to use `pcall`.
- Errors that aren't Device failures always raise: a wrong argument type, `ATE.input(n)` with an undeclared `n`, or a call made where it isn't allowed. Every error message names the function, and for Device calls the Device's connection details.

### Device lifetime

- A device object lasts for the whole Test Run, so a Pre-test Action can open a Device and later steps can use it (through an upvalue or a module-level local).
- At the end of the run, including an aborted or errored run, ATE closes every Device still open and logs a warning for each.
- Closing a Device that is already closed does nothing and returns success.
- Using a closed Device is a failure and takes the `onError` path above.

## Timing

| Call | Meaning |
| --- | --- |
| `ATE.sleep(seconds)` | Pauses the step for `seconds` (fractions allowed). The Operator can still abort the run during the pause |

There is no `ATE.gc`: schedules that need a collection call the standard `collectgarbage()`.

## Operator input

`ATE.input(n)` returns the value for the current step's n-th Step Input (see `test-schedule-format.md` for what each type returns). For blocking types, the step pauses until the Operator accepts the value in the GUI. `POLL` doesn't block: it returns `nil` until a button is clicked, then that button's text.

## Logging

`ATE.log(level, ...)` writes a log line.

- `level` is a value of the `Log` enum: `Log.DEBUG`, `Log.INFO`, `Log.WARNING`, `Log.ERROR`.
- The remaining arguments go through `tostring` and are joined with spaces.
- The global `print(...)` is redirected to `ATE.log(Log.INFO, ...)`, so stray prints are still shown and recorded.
- The main UI shows `INFO` and above; the debug window shows every level.

## Results

`ATE.report(passFail, msg, actual, expected)` records the step's result.

- `passFail` is a value of the `Result` enum (values in `test-schedule-format.md`).
- `msg` is optional human-readable text.
- `actual` is the measured value or state; `expected` is optional, the expected value or tolerance. Each may be a number, string or boolean, and ATE stores them as text using `tostring`.
- The step-result rules in `test-schedule-format.md` apply: the last call wins, every call is logged, and no call means `NOREPORT`.

## Run information

`ATE.run` is a read-only table describing the current Test Run: the UUT serial number, the Operator, the Station, the Test Schedule name and the Media Release. Writing to it raises an error.

## Where each call is allowed

- **While the `.tslua` file loads** (top level): the builders, `require`, `ATE.log` and the enums. Everything else raises an error, because loading happens before a Test Run exists.
- **In the Pre-test and Post-test Actions**: everything except `ATE.input` and `ATE.report`, which belong to steps.
- **In a step action**: everything.

## Loading auxiliary code

Not part of the `ATE` table: auxiliary Lua files load through ATE's replacement `require` (see `test-schedule-format.md`). The standard library is otherwise complete, except that `dofile` and `loadfile` are removed.
