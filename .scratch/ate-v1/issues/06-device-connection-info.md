# Device connection details and v1 connection types

Type: grilling
Status: resolved
Blocked by: 05

## Question

What is the structure of the connection-info table passed to `ATE.open`, and which Device connection types does v1 support (serial, VISA, UDP, TCP, …) with which parameters?

Answered since ("The ATE Lua API"): the enum is `DeviceType`, with string keys for every supported Device Type, and `params` is a plain table, built optionally with builders from a utility Lua file ATE ships. This ticket now means: which `DeviceType` values v1 supports; the `open`, `read`, `write` and `close` fields for each; the builder API; and how schedules load the utility file, given that `require` only resolves files listed in the Test Definition.

## Answer

Full definition: the Devices section of `assets/ate-table-reference.md`.

- **Device Types**: `SERIAL` (pyserial), `TCP`, `UDP` and `VISA` (pyvisa with whatever backend it finds; ATE doesn't ship NI-VISA). No Device discovery in v1.
- **Data** is raw bytes both ways.
- **`open` params** are all required, with no defaults: `SERIAL` takes `port`, `baud`, `dataBits`, `stopBits`, `parity`, `flowControl`, `timeout`, `readTerm`, `writeTerm`; `TCP` takes `host`, `port`, `timeout` and the terminators; `UDP` takes `host`, `port`, `localPort`, `timeout`; `VISA` takes `resource`, `timeout` and the terminators. `Parity` and `FlowControl` are new enums.
- **`read`/`write`/`close` params** are optional per-call overrides of the `open` configuration: `read` takes `size`, `timeout`, `readTerm`; `write` takes `data` (required) and `writeTerm`; `close` takes none.
- **Unknown, missing or mistyped fields** always raise and name the field; they never go through `onError`.
- **Buffering**: a schedule isn't reactive to incoming data, so ATE buffers everything a Device receives from `open` to `close`, and `read` takes from the buffer first.
- **Results**: a complete read returns `{data, count, partial = false}`. A timeout after some bytes is not an error: it returns `partial = true` and logs a warning. A timeout with no bytes, or any other failure, sets `err` and goes through `onError`. `write` returns `{ok, count}`. Shapes are uniform across types where the types allow; `UDP` reads are partial only when truncated by `size`.
- **Builders** are in the bundled module `ate.devices` (`require` reserves the `ate.` namespace): `Serial(port)`, `Tcp(host, port)`, `Udp(host, port)`, `Visa(resource)`, `Read()`, `Write(data)`, with defaults (9600 8N1, no flow control, 1 s timeout, `"\n"` terminators) and chained setters. The builder is itself the params table.
- **Device tracing** at `Log.DEBUG` is an application config setting, off by default.
