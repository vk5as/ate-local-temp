# Choose the Lua runtime

Type: grilling
Status: resolved
Blocked by: 01, 02, 05

## Question

Given the target Lua version (5.5), the required ATE API, and the binding survey, which Python ↔ Lua implementation does ATE use?

Inputs: the survey points to lupa. Still to settle: the coroutine vs worker-thread model for `ATE.input`, given the Lua 5.1 `pcall` limitation and that coroutines lose error details. Also check lupa with `QThread` on Windows.
Answered since: target Lua 5.5, which lupa bundles; its `pcall` limitation applied only to 5.1. `POLL` inputs must stay clickable while a step runs, so the GUI must never be blocked by Lua execution. That points to a worker thread, or to frequent yields.
Answered since: Test Schedules' own use of coroutines need not be supported. So ATE may use coroutines internally to pause steps, as an alternative to a worker thread.
Also from "Device connection details and v1 connection types" (06): ATE reads every open Device in the background and buffers incoming data until `ATE.read`, so the runtime's threading model must allow background I/O alongside a paused step.

## Answer

- **Binding**: lupa from the stock PyPI wheels (`lupa>=2.8`, 2.9 once released for its Lua 5.5 GC fix), imported as `from lupa.lua55 import LuaRuntime`. ATE refuses to start unless `lua_version == (5, 5)`; there is no fallback to LuaJIT or Lua 5.1–5.4. No custom Lua build, and no Lua C modules in v1 (the research shows a custom build means building and shipping our own wheels, and the stock Windows build can't load C modules).
- **Threading**: one `QThread` worker per Test Run runs the Pre-test Action, every step and the Post-test Action. `ATE.input`, `ATE.sleep` and `ATE.read` block that thread; lupa releases the GIL while Lua runs. The worker reaches the GUI only through queued Qt signals. ATE uses no coroutines internally.
- **Runtime lifetime**: a fresh `LuaRuntime` per load (each Test Run, and each picker load), created and used on its own thread, never shared or reused.
- **Abort**: blocking calls wait on the abort flag too, and a Lua count hook checks it every 1000 instructions; both raise `"aborted"`, and every later `ATE` call raises again.
- **Errors**: actions run through `xpcall(fn, debug.traceback)`. The first line becomes the `Result.ERROR` message, the traceback is logged at `Log.ERROR` and recorded, and any Python traceback goes to the application log.
- **Loading locked down**: `register_eval=False`, `register_builtins=False`; `dofile`, `loadfile` and `load` removed; `require` replaced. `io` and `os` stay.
- **Devices**: `VISA` reads on demand; `SERIAL`, `TCP` and `UDP` buffer in the background.
- **Python**: 64-bit CPython 3.13 and 3.14 (standard GIL builds). CI runs Windows and Linux on both versions.
- **Follow-ups**: prototype ticket "Lua worker thread on Windows" (16) checks the design on Windows. A custom Lua build is parked under Not yet specified.

## Comments

### Round 1, 2026-10-17

- **Binding**: lupa, pinned to its Lua 5.5 module: `from lupa.lua55 import LuaRuntime`, refuse to start if `lua_version != (5, 5)`, no fallback to LuaJIT or Lua 5.1–5.4, `lupa>=2.8` (move to 2.9 for its Lua 5.5 GC timing fix, GH#294).
- **Threading**: one worker thread per Test Run runs the Pre-test Action, every step and the Post-test Action. `ATE.input`, `ATE.sleep` and `ATE.read` are plain Python calls that block that thread; lupa releases the GIL while Lua runs, so the GUI and Device readers keep running. ATE uses no coroutines internally (schedules' own coroutine use is still not promised).
- **Runtime lifetime**: a fresh `LuaRuntime` per load (each Test Run, and each throwaway load for the picker), created and used only on its own thread, never shared or reused.
- **`VISA` reads on demand**, with no background reader; `SERIAL`, `TCP` and `UDP` keep background buffering. Noted on "Device connection details and v1 connection types" (06).
- **Loading locked down**: `register_eval=False`, `register_builtins=False` (no `python.*` global); `dofile`, `loadfile` and `load` removed; `require` replaced. `io` and `os` stay.
- **Research requested**: whether ATE can supply its own Lua build to lupa, how, and with what limitations. Findings: `research/lupa-custom-lua.md` on branch `research/lupa-custom-lua` (throwaway, never merged).

### Round 2, 2026-10-17

- **Thread type**: a `QThread` with a worker object moved onto it. Everything sent to the GUI (log lines, step results, the waiting-for-input state) goes through Qt signals with queued connections; the worker never touches widgets.
- **Abort**: blocking calls (`ATE.input`, `ATE.sleep`, `ATE.read`) also wait on the abort flag and raise the Lua error `"aborted"`. A Lua count hook installed at runtime creation checks the flag every 1000 instructions and raises the same error. After an abort every later `ATE` call raises and the hook keeps firing, so `pcall` can't keep a schedule going. What the aborted step records and whether the Post-test Action runs belong to "Flow control".
- **Errors**: actions run through a Lua-side `xpcall(fn, debug.traceback)` wrapper. The error's first line is the step's `Result.ERROR` message; the full traceback is logged at `Log.ERROR` and recorded with the Test Run. A Python exception behind the error goes to the application log, and the Lua message names the `ATE` function that failed.
- **Windows check**: a new prototype ticket, "Lua worker thread on Windows", run by the user on Windows; blocked by this ticket and blocking nothing. This ticket records the design assuming it passes.
- **Python**: 64-bit CPython 3.13 and 3.14, standard (GIL) builds, both tested in CI. lupa 2.8 publishes `cp313` and `cp314` wheels for `win_amd64` and `manylinux x86_64`. 32-bit and ARM Windows are not supported in v1.
