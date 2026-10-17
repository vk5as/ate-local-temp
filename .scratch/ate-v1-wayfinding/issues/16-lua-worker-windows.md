# Lua worker thread on Windows

Type: prototype
Status: resolved
Blocked by: 09

## Question

Does the runtime design from "Choose the Lua runtime" hold on Windows? Build a throwaway PyQt6 app on 64-bit Windows with CPython 3.13 and 3.14 and the stock `lupa` wheel, running Lua through `lupa.lua55` on a `QThread` worker, and check:

- the GUI stays responsive while Lua runs a busy loop (lupa releases the GIL);
- the worker can block in a Python function standing in for `ATE.input` while a GUI button stays clickable, and resumes when the button is clicked;
- an abort button stops a pure-Lua busy loop through a count hook (every 1000 instructions), and stops a blocked call through the abort flag;
- `xpcall(fn, debug.traceback)` returns a usable traceback, including for an error raised from a Python function.

Human task: run on a Windows machine. The design in ticket 09 assumes this passes; a failure reopens it.

## Answer

**The design holds on Windows**, with five refinements found by the prototype. Every check passed on Windows 11 with CPython 3.14.7, lupa 2.8 (`lua_version == (5, 5)`) and Qt 6.11, and the user found the app responsive by hand. Python 3.13 was not installed on the Windows machine. It passed on Linux, and CI covers it on Windows.

- **Responsive GUI**: the GUI stalled at most 11 ms during a 3 s pure-Lua loop with the hook on, and 20 ms while `ATE.input` blocked the worker.
- **Abort**: 28–43 ms on Windows (1–12 ms on Linux), whether stopping a busy loop through the count hook, a blocked `ATE.input` through the abort flag, or a loop that `pcall`s or `xpcall`s a busy loop.
- **Tracebacks**: `xpcall` gives `file:line` and the named calling functions, for Lua errors and for Python exceptions inside `ATE` functions. Tail calls drop frames, which is normal Lua behaviour.

Refinements to "Choose the Lua runtime":

1. **`pcall` and `xpcall` are wrapped** to raise `"aborted"` again once the abort flag is set. Without this, a schedule that `pcall`s a busy loop swallowed the hook's error and never stopped. ATE's own step runner keeps the original `xpcall`.
2. **`ATE` functions never let a Python exception reach Lua.** One arrives as userdata, so `debug.traceback` returns it unchanged with no traceback. Python halves return `(ok, value)`, and a Lua shim raises `error("ATE.x: " .. msg, 2)`, which also blames the schedule's calling line.
3. **`LuaRuntime(..., unpack_returned_tuples=True)`**, or a Python tuple reaches Lua as one userdata.
4. **`python = nil`** in the prelude: `register_builtins=False` still leaves a `python` table (`as_function`, `iter`, …).
5. **Chunks load with `name="@<path>"` and `mode="t"`**, so tracebacks show the real file and line and bytecode is refused.

The schedule-visible parts (error message form, an abort can't be caught, no `python` table, no bytecode) are added to `assets/ate-table-reference.md`.

## Comments

### Prototype, 2026-10-18

Asset: `prototypes/lua-worker/` on branch `prototype/lua-worker` (throwaway, never merged). `lua_worker_check.py --auto` runs eleven checks unattended; without `--auto` it opens a window to drive by hand. `run_windows.bat` runs it on CPython 3.13 and 3.14.

- On Linux, the first runs failed, which exposed refinements 1 to 4 (the `pcall` scenario hung forever). After the prelude was fixed, all checks passed on Linux.

### Windows run, 2026-10-18

All eleven checks passed on Windows 11 with CPython 3.14.7. The busy loop did 9.9M iterations in 3 s with the hook on, the longest GUI stall was 27 ms, and Abort took 28–43 ms. Python 3.13 was skipped because it is not installed. The user: "It felt really good and seemed really performant."
