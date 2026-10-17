# PROTOTYPE: Lua worker thread on Windows (throwaway, never merged)

Answers "Lua worker thread on Windows" (`.scratch/ate-v1/issues/16-lua-worker-windows.md`): does the runtime design from "Choose the Lua runtime" hold on Windows?

`lua_worker_check.py` is a PyQt6 app with a `QThread` worker running `lupa.lua55`, a fresh `LuaRuntime` per run, and a 10 ms GUI heartbeat.

- `python lua_worker_check.py --auto` runs every check unattended. The GUI clicks Continue or Abort itself on a timer. It prints PASS or FAIL for each check and exits 0 if all pass.
- `python lua_worker_check.py` opens the window so you can drive it by hand.
- `run_windows.bat` runs the checks with CPython 3.13 and 3.14, with venvs under `%LOCALAPPDATA%`, writes `out\results.txt`, then opens the window.

Checks: GUI stall during a pure-Lua busy loop; `ATE.input` blocking until Continue; Abort stopping a busy loop (count hook), a loop that `pcall`s or `xpcall`s a busy loop, and a blocked `ATE.input`; `xpcall` tracebacks for a Lua error and for a Python exception inside an `ATE` function; loading locked down.

Design gaps found on Linux and fixed in the prelude:

- `unpack_returned_tuples=True` is needed, or a Python `(ok, value)` tuple reaches Lua as one userdata.
- The count hook's `"aborted"` error is catchable, so a schedule's own `pcall` swallowed it and looped forever. ATE wraps `pcall` and `xpcall` to raise it again after an abort.
- A Python exception reaching Lua directly is userdata, so `debug.traceback` returns it unchanged, with no traceback. ATE functions catch Python exceptions and raise a string with `error(msg, 2)`, which also blames the calling schedule line.
- `register_builtins=False` still leaves a `python` table (`as_function`, `iter`, …), so the prelude sets `python = nil`.
- Chunks are loaded with `name="@<path>"` (tracebacks show `file:line`) and `mode="t"` (no bytecode).
- Tail calls drop frames from tracebacks. That's Lua, not a bug.
