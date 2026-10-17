# Lua worker thread on Windows

Type: prototype
Status: claimed
Blocked by: 09

## Question

Does the runtime design from "Choose the Lua runtime" hold on Windows? Build a throwaway PyQt6 app on 64-bit Windows with CPython 3.13 and 3.14 and the stock `lupa` wheel, running Lua through `lupa.lua55` on a `QThread` worker, and check:

- the GUI stays responsive while Lua runs a busy loop (lupa releases the GIL);
- the worker can block in a Python function standing in for `ATE.input` while a GUI button stays clickable, and resumes when the button is clicked;
- an abort button stops a pure-Lua busy loop through a count hook (every 1000 instructions), and stops a blocked call through the abort flag;
- `xpcall(fn, debug.traceback)` returns a usable traceback, including for an error raised from a Python function.

Human task: run on a Windows machine. The design in ticket 09 assumes this passes; a failure reopens it.
