# Survey: Python ↔ Lua binding candidates

Answers ticket `.scratch/ate-v1/issues/02-lua-binding-survey.md`. Facts only. The choice is made in "Choose the Lua runtime".

Researched 2026-10-16. "Observed" means I ran it against `lupa==2.8` (CPython 3.13, Linux x86_64) with the scripts described in [Experiments](#experiments-lupa-28). Everything else cites a primary source.

## Candidates found

| Candidate | Kind | Lua versions | PyPI | Windows wheels | Licence | Last activity |
|---|---|---|---|---|---|---|
| **lupa** | Cython wrapper around real Lua / LuaJIT, bundled | 5.1, 5.2, 5.3, 5.4, 5.5, LuaJIT 2.0, 2.1 | `lupa` 2.8 (2026-04-15) | Yes (win_amd64, win32, win_arm64) | MIT | Repo pushed 2026-10-08 |
| **lunatic-python** (bastibe fork) | C extension linking against a system Lua | 5.1 to 5.5 (claimed) | None for this fork; the old `lunatic-python` 1.0 has no files | None | LGPL-2.1 | Commits 2026-06-19 |
| **ffilupa** | CFFI binding | not verified | `ffilupa` 3.0.1 (2018-11-23), sdist only | None | LGPLv3+ | Repo gone (404) |
| **Pure-Python interpreters** (e.g. `abstra-lua`, numerodix/luna, MoonPie) | Lua reimplemented in Python | varies, partial | `abstra-lua` only | n/a (pure Python) | varies / unstated | Small hobby or early projects |

Other names checked and ruled out:

- `lupapy` 2.2 is a one-off 2024-10-15 upload that points at the lupa repo. ([PyPI JSON](https://pypi.org/pypi/lupapy/json))
- PyPI `mlua` is a small helper built on lupa, not a binding to Rust's mlua. ([PyPI JSON](https://pypi.org/pypi/mlua/json))
- `pylua` 0.1.1 is AGPLv3 from 2015 with no files. ([PyPI JSON](https://pypi.org/pypi/pylua/json))
- `lunatic-python-universal` 2.1.1 is from 2018 with no files. ([PyPI JSON](https://pypi.org/pypi/lunatic-python-universal/json))
- `luapy` is "Lua-inspired", not Lua. ([PyPI JSON](https://pypi.org/pypi/luapy/json))
- I found no maintained PyO3/mlua package for Python.

---

## 1. lupa

Sources: [README](https://github.com/scoder/lupa/blob/master/README.rst), [CHANGES](https://github.com/scoder/lupa/blob/master/CHANGES.rst), [PyPI JSON](https://pypi.org/pypi/lupa/json), the `LuaRuntime` docstrings in the 2.8 wheel, and my own experiments.

### Supported Lua versions

- The README says it "ships with Lua 5.1, 5.2, 5.3, 5.4 and 5.5 as well as LuaJIT 2.0 and 2.1 on systems that support it". You pick one by importing a submodule, e.g. `from lupa.lua54 import LuaRuntime`. Plain `import lupa` uses the latest version.
- The 2.8 wheels contain these modules (I listed the wheel contents):
  - `win_amd64` (cp312) and `abi3-win32`: `lua51 lua52 lua53 lua54 lua55 luajit20 luajit21`.
  - `manylinux x86_64` (cp312): the same seven.
  - `macosx arm64` (cp312): `lua51` to `lua55` only, no LuaJIT.
- Changelog history:
  - 2.4: "The windows wheels now bundle LuaJIT 2.0 and 2.1."
  - 2.7: "Lua 5.5 is included in the binary wheels."
  - 2.1: bundled Lua 5.1.5 and 5.2.4.
  - 2.5: bundled Lua 5.4.8.
  - Recent releases say the LuaJIT copies were "updated to the latest git branches". The observed `lua_implementation` was `LuaJIT 2.1.1774896198`.
- Each runtime exposes `lua_version` and `lua_implementation`. Observed: `(5, 1)` for `luajit21`.

### Calling Lua from Python

- `LuaRuntime.execute(code, *args, name=None, mode=None)`, `eval(...)` and `compile(code, name=None, mode=None)` all exist. `compile` returns a callable Lua function. Source: the 2.8 docstrings.
- `name` overrides the chunk name in error messages (added in 2.1). Observed: `name="@sched.lua"` gives messages like `sched.lua:1: x`.
- `lua.globals()` returns the global table. Lua tables follow Python's mapping protocol, and Lua functions are Python callables.
- Observed: a Test Schedule-shaped chunk returned `{ {name=..., fn=function() ... end}, ... }`. Python indexed it as `sched[i].fn` and called each function in order.

### Exposing Python to Lua (functions, tables, opaque objects)

- The README says: "Python objects are either converted when passed into Lua (e.g. numbers and strings) or passed as wrapped object references."
- `lua.table_from(dict)` / `lua.table(...)` build Lua tables. `recursive=True` was added in 2.1.
- `unpack_returned_tuples=True` turns a Python tuple return into multiple Lua return values.
- `attribute_filter` and `attribute_handlers` control which attributes Lua can read or write on Python objects.
- `register_eval` and `register_builtins` both default to True. They expose `python.eval` and `python.builtins` to Lua and can be turned off.
- Observed:
  - An `ATE` table of Python functions injected via `lua.globals().ATE = lua.table_from({...})` was callable from Lua.
  - A Lua table passed to a Python function arrives as a `_LuaTable` that `dict()` can convert.
  - A Python object returned by `ATE.open` reports `type(h) == "userdata"` in Lua. Passing it back to `ATE.read(h, ...)` delivers the original Python object.

### Error and traceback propagation

- Lua errors raise `lupa.LuaError` in Python. Syntax errors raise `LuaSyntaxError`, and `LuaMemoryError` subclasses `LuaError` and `MemoryError` (README). Since 2.0, "Lua stack traces in Python exception messages are now reversed" to match Python's order (CHANGES).
- Observed, Lua error in a normal call: the `LuaError` message includes the chunk name and line plus a Lua `stack traceback:`.
- Observed, Python exception in a normal call (Python → Lua → Python): the **original Python exception** (`ValueError`) re-raises in Python with the full Python traceback through the callback.
- Observed, Python exception inside Lua `pcall`: Lua catches it, `pcall` returns `false`, and the error object stringifies to the exception message (`"boom from python"`).
- Observed, errors **inside a coroutine** (`fn.coroutine().send(...)`):
  - A Lua error raises `LuaError` with `file:line: msg` but **no** stack traceback.
  - A Python exception raised in a callback surfaces as `LuaError('')`. The original exception type and message are **lost**, and `__cause__`/`__context__` are both None.
  - Lua code can still collect a traceback itself with `xpcall(f, debug.traceback)`.

### Pausing in `ATE.input` (coroutines and threads)

- Coroutines (README):
  - "A wrapped Lua coroutine behaves exactly like a Python coroutine." You create one with `.coroutine()`, then resume it with `.send()` or iterate it.
  - "The `.throw()` method is not supported."
- Observed, Lua-side yield: with the shim `ATE.input = function(n) return coroutine.yield(n) end`, Python can drive each step as `co = step.fn.coroutine(); req = co.send(None)`. The step pauses mid-function and hands back `n`, and a later `co.send("42")` resumes it with the value. This worked on lua51, lua54 and luajit21.
- Observed, yield from Python: a **Python** function that calls `coroutine.yield` fails with `LuaError` (empty message). That is a yield across a C-call boundary, so the yield must happen in Lua code.
- Observed, yield inside `pcall`:
  - It fails on **Lua 5.1**: `pcall` catches the yield error, so the coroutine never pauses. Lua 5.1's message for a yield across a C boundary is "attempt to yield across metamethod/C-call boundary".
  - It works on 5.2, 5.3, 5.4, 5.5, LuaJIT 2.0 and LuaJIT 2.1.
  - Yield inside a C function callback such as a `table.sort` comparator fails on every version.
- Observed: a coroutine suspended on one thread can be resumed from another.
- Threads (README):
  - Lupa "frees the GIL and supports threading in separate runtimes when calling into Lua".
  - "Each `LuaRuntime` is protected by a global lock that prevents concurrent access to it."
- Observed, worker-thread model: Lua ran on a `threading.Thread`, and `ATE.input` was a plain Python function blocking on `threading.Event.wait()`. While Lua ran a CPU loop on the worker, the main thread kept running Python (150 ticks in 0.16 s). After the event was set, the step finished with the supplied value.
- CHANGES 2.2 fixed a deadlock: "Freeing Python objects from a thread while running Lua code could run into a deadlock."

### Replacing `require` / `dofile` / `loadfile`

- The host can change all globals through `lua.globals()`. Observed: after setting `require`, `dofile` and `loadfile` to `None` and adding a Python `include` global, `tostring(require)` was `"nil"` in Lua and `include("x.lua")` called the Python function.
- `LuaRuntime.compile(source, name="@file.lua")` turns source text into a Lua function, with error messages naming that file. This is one way for a Python `include` to load checked file contents.
- Lua's own `load` (and `loadstring` on 5.1) stays available unless removed.
- The README's security note says untrusted Lua should not have `python.builtins` access. Sandboxing is out of scope for v1.

### Wheels, maintenance, licence

- 2.8 has **Windows** wheels:
  - `win_amd64`: per-version wheels for cp38 to cp314 and cp314t, plus an abi3 wheel.
  - `win32`: cp38, cp39, cp314t and abi3.
  - `win_arm64`: cp39, cp314t and abi3.
- 2.8 has **Linux** wheels:
  - manylinux2014 / `manylinux_2_28` for x86_64 and aarch64, per CPython version.
  - abi3 wheels for i686, armv7l, ppc64le and riscv64.
  - musllinux wheels.
- `requires_python >=3.8`.
- Releases: 2.3 (2025-01-09), 2.4 (2025-01-10), 2.5 (2025-06-15), 2.6 (2025-10-24), 2.7 (2026-04-07), 2.8 (2026-04-15). A 2.9 is unreleased; its CHANGES entry fixes a Lua 5.5 GC timing bug (GH#294).
- GitHub `scoder/lupa`: 1159 stars, 63 open issues, pushed 2026-10-08, not archived. ([API](https://api.github.com/repos/scoder/lupa))
- Licence: "MIT style" on PyPI, classifier "MIT License". Development Status 5 - Production/Stable.
- Distro packages exist too: Debian/Ubuntu `python3-lupa`, Alpine `py3-lupa`.

---

## 2. lunatic-python (bastibe fork)

Sources: [repo](https://github.com/bastibe/lunatic-python), [README](https://raw.githubusercontent.com/bastibe/lunatic-python/master/README.md), [commits API](https://api.github.com/repos/bastibe/lunatic-python/commits?per_page=3), [PyPI `lunatic-python`](https://pypi.org/pypi/lunatic-python/json).

- **Versions:** the README says it "works with Python 2.7-3.x and Lua 5.1-5.5". It does not mention LuaJIT.
- **Build:** "you will need to have the Python and Lua development libraries on your system". It "has been modified to compile under Ubuntu". The repo ships CMake, `setup.py` and rockspecs, and installs via LuaRocks or `pip install git+https://...`. The README does not mention Windows.
- **Wheels:** none. The PyPI `lunatic-python` 1.0 points at the original labix project and has no files. `lunatic-python-universal` 2.1.1 (2018) has no files.
- **API:** `lua.eval`, `lua.execute`, `lua.globals()`, `lua.require(name)`. It is bidirectional, so Lua can also `require "python"`.
- **Errors, coroutines, threads:** the README does not document any of them.
- **Maintenance:** the latest commits are dated 2026-06-19 ("setup.py: Refactor Lua library discovery", README fixes). There are no releases on PyPI.
- **Licence:** LGPL-2.1.
- Lupa's README describes lupa as "a partial rewrite of LunaticPython in Cython with some additional features such as proper coroutine support".

## 3. ffilupa

Sources: [PyPI JSON](https://pypi.org/pypi/ffilupa/json), [PyPI page](https://pypi.org/project/ffilupa).

- CFFI-based binding. Its README presents it as an alternative to lupa, saying lupa's Cython backend is "less friendly to PyPy".
- The latest PyPI release is 3.0.1 (2018-11-23), as a `.zip` sdist only, with no wheels.
- Licence: LGPLv3+.
- The GitHub repo `TitanSnow/ffilupa` returns 404 from the GitHub API, so it was deleted or renamed.
- I did not verify its Lua version support, coroutines or errors, because no live primary docs remain beyond the PyPI page.

## 4. Pure-Python Lua interpreters

Source: [GitHub search](https://api.github.com/search/repositories?q=lua+interpreter+in+python+language:Python&sort=stars).

- **abstra-lua** ([repo](https://github.com/abstra-app/abstra-lua-python), PyPI `abstra-lua`):
  - Described as "A sandboxed Lua 5.5 interpreter written in pure Python", with no C extensions.
  - Supports metatables and the basic, string, table and math libraries, plus a limited `os` (`clock`, `difftime`, `time`).
  - The README does not mention coroutines.
  - Errors map to `LuaSyntaxError` and `LuaRuntimeError`. You call Lua functions from Python via `session.get(name)(...)`.
  - The repo had 5 commits, 2 stars and no releases on GitHub. Its licence is not stated on the repo page.
- **Others** are small or stale experiments, none packaged:
  - numerodix/luna: "experimental", MIT, last push 2017.
  - marteinn/Lua-Interpreter-In-Python: last push 2019.
  - rhulha/MoonPie: 3 stars, MIT.
- No pure-Python option documents full Lua 5.x compatibility, coroutine support or production status. Being pure Python, any of them would run Lua code at Python speed while holding the GIL.

---

## Experiments (lupa 2.8)

Scripts were kept outside the repo in `/tmp/lupatest/` and run with `/tmp/lupavenv/bin/python -I` after `pip install lupa==2.8`. They exercised:

1. A schedule table of step functions and the `ATE` table of Python functions, run on `lua51`, `lua54` and `luajit21`. This covered opaque handles, the `ATE.input` coroutine shim, and the four error kinds.
2. Python exception propagation in direct calls versus coroutines, a yield attempted from a Python function, and `name=` chunk names.
3. Yield inside `pcall` and inside a `table.sort` comparator on all seven bundled runtimes.
4. A Lua step on a worker thread with a blocking Python `ATE.input`, while the main thread kept running Python.

None of this ran on Windows. The Windows facts come from the listed wheel contents only.

## Open points not settled here

- Whether schedules use `pcall` around `ATE.input`. On Lua 5.1 that rules out a pure-coroutine pause, though a worker thread avoids it.
- Behaviour on Windows with Qt (`QThread` versus `threading.Thread`) was not tested.
