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
| `ATE.read(dev [, params] [, onError])` | read result | Reads from an open Device |
| `ATE.write(dev, params [, onError])` | write result | Writes to an open Device |
| `ATE.close(dev [, params] [, onError])` | `{ok = true}` | Closes a Device and releases it |

- The device object is a Lua table holding a host handle in `_handle`. Schedules treat it as opaque and only pass it back to `read`, `write` and `close`.
- Data is raw bytes in both directions: Lua strings, binary-safe, with no encoding or decoding. Schedules convert with `string.byte`/`string.pack` when they need to.
- ATE has no Device discovery (listing COM ports or VISA resources) in v1. A schedule names its Devices.

### Device Types

`DeviceType` has these values in v1:

| Value | Meaning |
| --- | --- |
| `DeviceType.SERIAL` | RS-232 or USB serial, through pyserial |
| `DeviceType.TCP` | A raw socket stream, e.g. a LAN instrument taking SCPI on port 5025, or a UUT debug port |
| `DeviceType.UDP` | Datagrams; one datagram is one message |
| `DeviceType.VISA` | GPIB, USBTMC and VXI-11 instruments through pyvisa, addressed by a VISA resource string. It uses whatever backend pyvisa finds; ATE doesn't ship NI-VISA |

### `open` params

Every field is required: `open` has no defaults, and a missing field raises. The builders below supply defaults.

| Type | Fields |
| --- | --- |
| `SERIAL` | `port`, `baud`, `dataBits` (5–8), `stopBits` (1, 1.5, 2), `parity`, `flowControl`, `timeout`, `readTerm`, `writeTerm` |
| `TCP` | `host`, `port`, `timeout`, `readTerm`, `writeTerm` |
| `UDP` | `host`, `port` (remote), `localPort` (0 = any), `timeout`. No terminators, because one datagram is one message |
| `VISA` | `resource` (e.g. `"GPIB0::5::INSTR"`), `timeout`, `readTerm`, `writeTerm` |

- `timeout` is in seconds. `readTerm` and `writeTerm` are byte strings; `""` means none, for binary protocols.
- `parity` is a `Parity` enum value: `Parity.NONE`, `EVEN`, `ODD`, `MARK`, `SPACE`.
- `flowControl` is a `FlowControl` enum value: `FlowControl.NONE`, `RTSCTS`, `XONXOFF`, `DSRDTR`.
- The `open` values are the Device's configuration for the rest of the run.

### `read`, `write` and `close` params

Every field is optional; an absent field uses the Device's `open` configuration.

- `read`: `size` (a byte limit; with none, the read ends on the terminator or the timeout), `timeout`, `readTerm`.
- `write`: `data` (required), `writeTerm`. `writeTerm` is appended to `data`.
- `close`: no fields, and `params` may be omitted.

A field the Device Type doesn't know (a typo such as `buad`), a missing required field, or a value of the wrong type is a schedule bug, not a Device failure. It always raises, names the field, and never goes through `onError`.

### Buffering incoming data

A schedule isn't reactive to incoming data; it only sees data when it calls `ATE.read`. So from `open` until `close`, ATE reads each Device in the background and buffers everything that arrives. `ATE.read` takes from that buffer first and waits only for what isn't there yet, so a response that arrives before the schedule asks for it isn't lost. For `UDP` the buffer is a queue of whole datagrams.

### Results

The shapes are the same for every Device Type, as far as the types allow:

| Outcome | Result | Through `onError`? |
| --- | --- | --- |
| `read` complete (terminator seen or `size` reached) | `{data = <bytes>, count = n, partial = false}` | no |
| `read` timed out after some bytes | `{data = <bytes>, count = n, partial = true}`, and a `Log.WARNING` | no |
| `read` timed out with no bytes | `{data = nil, count = 0, partial = false, err = "timeout"}` | yes |
| `read` other failure (port gone, connection reset) | `{data = nil, count = 0, partial = false, err = <message>}` | yes |
| `write` success / failure | `{ok = true, count = n}` / `{ok = false, count = 0, err = <message>}` | on failure |
| `close` success / failure | `{ok = true}` / `{ok = false, err = <message>}` | on failure |

- The read terminator is stripped from `data`, and `count` is `#data` after stripping.
- A `write` `count` includes the terminator.
- A `UDP` read never times out partway: a datagram arrives whole or not at all. A datagram longer than `size` is truncated and returned with `partial = true`.

### Builders (`ate.devices`)

ATE ships the builders as a module in the reserved `ate.` namespace (see "Loading auxiliary code"):

```lua
local devices = require("ate.devices")
local psu = ATE.open(DeviceType.SERIAL,
  devices.Serial("COM3"):setBaud(115200):setParity(Parity.EVEN))
local r = ATE.read(psu, devices.Read():setTimeout(5))
ATE.write(psu, devices.Write("*IDN?"))
```

- Constructors take the fields that identify a Device or call: `Serial(port)`, `Tcp(host, port)`, `Udp(host, port)`, `Visa(resource)`, `Read()` and `Write(data)`.
- Every other field starts from a default and is changed with a chained `setX` setter, as with `ScheduleParams`. Defaults: 9600 baud, 8 data bits, no parity, 1 stop bit, `FlowControl.NONE`, `timeout = 1.0`, `readTerm = writeTerm = "\n"`, `localPort = 0`.
- The builder is the params table itself: its fields are plain fields and its setters live in a metatable, so it passes straight to `ATE.open` with no `:build()` step and passes the unknown-field check.

### Tracing

When Device tracing is turned on in the application config (off by default), ATE logs every `open`, `read`, `write` and `close` at `Log.DEBUG`, with the Device, the bytes (escaped, and truncated past a set length) and the timing. Trace lines show in the debug window and are recorded with the Test Run.

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

- **While the `.tslua` file loads** (top level): the builders (including `ate.devices`), `require`, `ATE.log` and the enums. Everything else raises an error, because loading happens before a Test Run exists.
- **In the Pre-test and Post-test Actions**: everything except `ATE.input` and `ATE.report`, which belong to steps.
- **In a step action**: everything.

## Loading auxiliary code

Not part of the `ATE` table: auxiliary Lua files load through ATE's replacement `require` (see `test-schedule-format.md`). Module names starting with `ate.` resolve to modules bundled with ATE, such as `ate.devices`. The standard library is otherwise complete, except that `dofile` and `loadfile` are removed.
