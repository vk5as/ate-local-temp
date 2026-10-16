# Draft the ATE table

Type: task
Status: resolved
Blocked by: none

## Question

Provide a first draft of the `ATE` table that Test Schedules call, written up as `assets/ate-table-reference.md`. It must cover every function, its signature and its return values. In particular: the error-handler function that `ATE.open` takes, the table that `read` and `write` return (with its `err` field), `close`, `input`, `report` and logging. "The ATE Lua API" is blocked on this.

## Answer

Drafted by the user and written up as `assets/ate-table-reference.md`.

- **Devices:** `ATE.open(type, params, onError)` returns a device table holding `_handle`. `read`, `write` and `close` each take `(dev, params, onError)`. `onError(opResult, opArgs)` falls back to the handler given to `open`.
- **Other functions:** timing, `input(n)`, logging and `report(passFail, msg, actual, expected)`.
- **Conflicts with earlier map decisions,** for "The ATE Lua API" to settle: the `report` argument order, enum and logging names, and the `open` signature.
- **Not in the draft:** enum values, params fields, the `read` return format (including any `err` field), `opResult`/`opArgs` contents, and what happens on failure without `onError`.
