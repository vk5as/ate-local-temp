# Survey of the Python to Lua binding candidates

Type: research
Status: resolved
Blocked by: none

## Question

What Python ↔ Lua implementations exist (e.g. lupa with its bundled Lua/LuaJIT variants, other bindings, pure-Python Lua), and for each: supported Lua versions; calling Lua functions from Python; exposing Python functions, tables and opaque objects to Lua; error and traceback propagation both ways; coroutines or threads so a Lua function can pause in `ATE.input` while the Qt GUI stays responsive; replacing `require`/`dofile`/`loadfile` with a custom `include`; Windows and Linux wheel availability; maintenance health and licence. Facts only: the choice is made in "Choose the Lua runtime".

## Answer

Findings: `research/lua-binding-survey.md` on branch `research/lua-binding-survey` (throwaway, never merged).

- **lupa** (MIT, active): the only viable candidate found.
  - Bundles Lua 5.1 through 5.5 plus LuaJIT 2.0 and 2.1, with Windows and Linux packages.
  - The `ATE` table injection, Lua function calls from Python, and opaque handles passed back and forth all work.
  - **Pausing in `ATE.input`, coroutine route:** the yield must happen in Lua. On Lua 5.1 a pause inside `pcall` fails; on 5.2+ and LuaJIT it works.
  - **Pausing in `ATE.input`, worker-thread route:** lupa releases Python's global lock while Lua runs, and blocking a worker thread on an event worked.
  - **Errors:** normal calls keep Lua and Python tracebacks, but inside a coroutine the error details are lost.
  - **`include`:** `require`, `dofile` and `loadfile` can be replaced; `load` must also be removed to fully restrict file loading.
- **lunatic-python**: Linux only, no packages on PyPI. **ffilupa**: dead. **Pure-Python interpreters**: immature and slow.
- Open, for "Choose the Lua runtime": whether schedules call `ATE.input` inside `pcall`; how lupa behaves with `QThread` on Windows.
