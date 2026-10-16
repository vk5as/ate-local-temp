# The ATE Lua API

Type: grilling
Status: resolved
Blocked by: 01, 15

## Question

What is the exact surface and semantics of the `ATE` table injected into Lua: `open`/`read`/`write`/`close`, `input`, `log` and its levels, `include`, and anything else Test Schedules need? Signatures, return values, and error behaviour.

Inputs: `ScheduleParams` and `StepParams` are also runtime-provided globals (see `assets/test-schedule-format.md`). Settle the clash between `include` (auxiliary code) and `setInclude` (Sub-schedule), and what `ATE.input(n)` returns per Step Input type (`CHECKBOX` returns many values).
Answered since: `ATE.report(<pass/fail enum>)` exists. `PROMPT` blocks; `POLL` is non-blocking, returning `nil` or the clicked button's text.
Answered since: the signature is `ATE.report(passFail, result, expectedResult, message)`.
Answered since: what `ATE.input(n)` returns per type, `POLL` stickiness and reset, and `Result.ERROR` are settled in "Test Step structure and outcome model" (04).

## Answer

Full definition: `assets/ate-table-reference.md`.

- **`ATE.report(passFail, msg, actual, expected)`**: `msg` and `expected` are optional; `actual` and `expected` may be a number, string or boolean and are stored as text.
- **`Result` enum** stays, including `Result.ERROR`.
- **Logging** is `ATE.log(Log.X, ...)` with `DEBUG`, `INFO`, `WARNING`, `ERROR`; `print` is redirected to `ATE.log(Log.INFO, ...)`.
- **Timing** is `ATE.sleep(seconds)`, interruptible by an abort. There is no `ATE.gc`; schedules use `collectgarbage()`.
- **`ATE.run`**: a read-only table with the UUT serial, Operator, Station, Test Schedule name and Media Release.
- **Devices**: `ATE.open(deviceType, params [, onError])`, and `(dev, params [, onError])` for `read`, `write` and `close`. `deviceType` is a `DeviceType` enum value (string keys of every supported Device Type). `params` is a plain table of type-specific fields: connection details for `open`, operation details otherwise. ATE ships a utility Lua file of builders for these tables. The device object holds `_handle` and is opaque.
- **Return values**: `read` returns `{data}` or `{data = nil, err}`; `write` and `close` return `{ok = true}` or `{ok = false, err}`. Fields may be added later.
- **`onError(opResult, opArgs)`**: falls back to the handler given to `open`. A non-`nil` return lets the script continue, and the failed call returns that value. A `nil` return, or no handler, raises a Lua error, so the step records `Result.ERROR` unless the schedule catches it. Errors that aren't Device failures always raise.
- **Device lifetime, where calls are allowed, `require` and the standard library**: as in the partial grilling below, with `ATE.log` and the enums allowed at load time.

## Comments

### Partial grilling, 2026-10-16

Paused: the rest of this ticket waits for "Draft the ATE table" (15). Settled so far:

- **Loading auxiliary code**: ATE replaces the standard `require`. It resolves only files listed in the Test Definition, warns on checksum mismatch, and caches each module once per Test Run. Nothing is named `include`; `setInclude` means Sub-schedules only. Documented in `assets/test-schedule-format.md`.
- **Errors**: device calls do not rely on `pcall`. `ATE.open` takes an error-handler function, and `read` and `write` return a table with an `err` field. The details come from the draft.
- **Handle lifetime**: a handle lasts for the Test Run. ATE closes any still open at run end (including abort or error) and warns for each. Closing twice does nothing; using a closed handle is an error.
- **Logging**: `ATE.log(level, ...)` with a `Log` enum (`DEBUG`, `INFO`, `WARNING`, `ERROR`); arguments go through `tostring` and are joined with spaces. `print` is redirected to `ATE.log(Log.INFO, ...)`.
- **Extras**: `ATE.sleep(seconds)` (interruptible) and a read-only `ATE.run` table (UUT serial, Operator, Station, Test Schedule name, Media Release).
- **Where calls are allowed**: at load time only the builders, `require` and `ATE.log`; in Pre-test and Post-test Actions everything except `input` and `report`; in step actions everything.
- **Standard library**: full, except that `dofile` and `loadfile` are removed. Lua is trusted; the `require` restriction exists for traceability, not security.
- **Still open, pending the draft**: `read`/`write` signatures and options, the `open` error handler's contract, how errors in non-device calls behave (e.g. `ATE.input(n)` with an undeclared `n`), and whether the logging, extras and call contexts above fit the draft.

Draft supplied (15): its conflicts with earlier map decisions (`report` argument order, enum and logging names, the `open` signature) and undocumented details are settled here.
