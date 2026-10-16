# Research: supplying a custom Lua build to lupa

Sources: lupa 2.8 sdist from PyPI (`https://files.pythonhosted.org/packages/c3/a6/0f869fbb07c393f15473b1eefefb7b5bec162fb7481803d040ed4dc46002/lupa-2.8.tar.gz`), read only, not built or executed. Line numbers below refer to that sdist. GitHub links point at `master`. Public GitHub issue API was used for issue text (no `gh` login).

## Summary

Yes, with limits. lupa 2.8's `setup.py` accepts an external Lua in three ways: explicit `--lua-lib` plus `--lua-includes`, pkg-config discovery (`--no-bundle`), or a modified copy of the bundled source under `third-party/lua55`. Any external build produces a single extension named `lupa.lua` rather than `lupa.lua55`, and only one Lua version is compiled into that install. Nothing can swap the Lua library at runtime: the Lua C API is bound at Cython compile time, and lupa contains no `dlopen`/`ctypes` path for Lua itself. Lua C modules can be loaded only when the Lua symbols are exported from the lupa extension and it is imported with `RTLD_GLOBAL` (`lupa.allow_lua_module_loading`); this is the area with the most history of breakage. Windows builds need MSVC and Cython>=3.2.4, with no MSVC version pinned in the repository. Distribution wheels are built with cibuildwheel and `LUPA_USE_BUNDLE=true`. A custom build loses the multi-version module set and the bundled LuaJIT modules.

## 1. Build-time options

- `--lua-lib <file>` and `--lua-includes <dir>` must both be given; otherwise they are ignored. They are read from `sys.argv` only, not from environment variables. `setup.py` L129-150 and L322-330 (`get_option`). Documented in README.rst "Building with different Lua versions" (https://github.com/scoder/lupa/blob/master/README.rst).
- On Linux the library is passed as `extra_objects`, so it is expected to be a static archive (`.a`). On Windows, a `.lib` plus `.dll` pair is accepted and the `.dll` is copied into the package and listed in `package_data` (`setup.py` L485-495).
- `--no-bundle` (env `LUPA_NO_BUNDLE=true`) skips the bundled sources and runs discovery (`setup.py` L385-393). Discovery order (`find_lua_build`, L153-215): a `luajit*/src/libluajit.a` inside the lupa tree, then pkg-config package `luajit` (version >=2), then pkg-config names `lua5.4`, `lua-5.4`, `lua54`, `lua`, and the same for 5.3, 5.2, 5.1 (each with its own minimum version, L185-191).
- `--use-bundle` skips discovery and uses bundled sources. `--no-luajit` (env `LUPA_NO_LUAJIT`) excludes LuaJIT. Note: the README table lists `--no-lua-jit`, but the code checks `--no-luajit` (`setup.py` `has_option('--no-luajit')`). Use the code spelling.
- `--with-lua-checks` defines `LUA_USE_APICHECK`; `--with-lua-dlopen` defines `LUA_USE_DLOPEN`; `--without-assert`, `--limited-api`, `--with-cython` also exist (`setup.py` L355-390). Every flag has an env form `LUPA_<NAME>=true` via `has_option` (L332-340). The sdist's `INSTALL.rst` and README describe `--use-bundle`/`--no-bundle`.
- Accepted Lua versions: the code gates on `LUA_VERSION_NUM` (`lupa/luaapi.pxd` L425-490) and raises `#error` below 5.1. Nothing checks that the supplied library is 5.5. The pkg-config names above accept any version at or above their minimum, with no upper bound (inferred for 5.5: a `lua` or `lua5.4` .pc reporting 5.5.x passes the check).
- Module naming (`prepare_extensions`, `setup.py` L416-440): the extension name is `config.get('libversion', 'lua')` and is `libversion` only for bundled builds. An external build therefore yields `lupa/lua.<abi>.so` and `lupa/lua.pyx`. `_lupa.pyx` is never compiled as a module; it is copied to `lupa/<name>.pyx`. `lupa/__init__.py` (`_import_newest_lib`) picks the highest `lua*` module present, so `lupa.LuaRuntime` still works, but `from lupa.lua55 import LuaRuntime` fails. Module naming is verified from code; the import behaviour on an installed build is inferred.
- Historical note: issue #218 (closed 2022-08-14) reported that `lua.pyx` collided with `lua.pxd` on external builds. The current code still emits `lua.pyx`; the closure suggests it was resolved, not verified.

## 2. Patched or custom-configured Lua 5.5

- `third-party/lua55` is a git submodule pointing at `https://github.com/lua/lua.git` (`.gitmodules`, https://github.com/scoder/lupa/blob/master/.gitmodules). The sdist contains its flat source tree (no `src/` directory) with `lua.h` reporting 5.5.1.
- `use_bundled_lua` (`setup.py` L253-316) looks for `makefile` at the top of the submodule, parses the `CORE_O`, `AUX_O`, `LIB_O` lists, drops `lua.o`, and compiles those `.c` files with lupa's own macros (`c_defines`) and the submodule directory as include path. Consequences (inferred from this code path):
  - Editing `third-party/lua55/luaconf.h` changes the build, since that directory is the include path.
  - Extra C sources can be compiled in only by adding them to the object lists in the Lua makefile, because that is the only input the parser reads. Adding a new `luaopen_*` function also needs lupa-side code.
  - The makefile's own `CFLAGS` (`-march=native`, `-DLUA_USE_LINUX`) are not applied; only object lists and include paths are taken from it.
  - The parser raises `RuntimeError` on unexpected makefile syntax, so layout changes in the Lua makefile break the build.
- lupa calls `luaL_openlibs(L)` for every `LuaRuntime` (`lupa/_lupa.pyx` L317). Extra C libraries compiled in are not opened unless Lua code (for example `package.preload`) or lupa code opens them.
- The submodule is pinned by commit. A patched tree is a local modification of a submodule (dirty state), or requires a fork of the submodule URL. This is inferred from git semantics; the repo does not record a fork.

## 3. Runtime loading of a different Lua library

No. The Lua API is declared through Cython `cdef extern from "lua.h"` / `"lauxlib.h"` / `"lualib.h"` blocks (`lupa/luaapi.pxd` L21, L272, L400), so the symbols are resolved when the extension is linked. A grep of `lupa/*.pyx`, `lupa/*.pxd`, and `lupa/__init__.py` finds no `ctypes`, `CDLL`, `dlopen`, or `dlsym` used to load a Lua library. The only dynamic-loading control in the package is `lupa.allow_lua_module_loading()` (`lupa/__init__.py`), which sets `RTLD_NOW | RTLD_GLOBAL` while importing a lupa module so that loaded C modules can resolve Lua symbols. It does not select a different Lua runtime. Confirmed by reading the code.

## 4. Loading Lua C modules (`require` of `.so`/`.dll`)

- Lua side: `loadlib.c` uses `dlopen` when `LUA_USE_DLOPEN` is set (`third-party/lua55/loadlib.c` L93) and `LoadLibrary` when `LUA_DL_DLL` is set (L128). `luaconf.h` sets `LUA_DL_DLL` on Windows (L55-58). lupa sets `LUA_USE_DLOPEN` only with `--with-lua-dlopen` / `LUPA_WITH_LUA_DLOPEN` (`setup.py` L373-374). The upstream wheel workflow sets it true on Linux and macOS and false on Windows (`.github/workflows/wheels.yml`, `env` block, https://github.com/scoder/lupa/blob/master/.github/workflows/wheels.yml).
- Linux: a C module resolves `lua_*` from the global symbol namespace. This works only if the lupa extension exports them and is loaded with `RTLD_GLOBAL`. Export is inferred from default ELF visibility (not verified by a build). The `RTLD_GLOBAL` requirement is confirmed by `lupa/__init__.py` and by issue #246 (below).
- Issue #246 "Error loading C modules in 2.x" (closed 2023-12-10, https://github.com/scoder/lupa/issues/246): undefined symbols after upgrading to 2.x with system Lua. Maintainer comment (2023-10-12): only one `luaXY` module can have global symbols enabled, because symbols can mix up. The resolution was the context manager.
- Issue #278 "Loading C modules with pre-built wheels?" (closed 2025-06-15, https://github.com/scoder/lupa/issues/278): a 2025-09-09 comment reports `undefined symbol: lua_gettop` on a Linux build; the commenter used `LUPA_WITH_DL_OPEN`, which is not the variable `setup.py` reads (`LUPA_WITH_LUA_DLOPEN`). Observed, not reproduced.
- Windows: `LUA_API` is `extern` unless `LUA_BUILD_AS_DLL` is defined (`luaconf.h` L305-318). The bundled Lua is compiled into the `.pyd` as static objects (`ext_libraries`, `setup.py` L253-316 and L416-440), and `setup.py` sets no `dllexport`. Inferred: the `.pyd` does not export `lua_*`, so a C module DLL cannot link against it. A C module on Windows would need a Lua DLL built with `LUA_BUILD_AS_DLL` (the `--lua-lib` `.lib`/`.dll` path). Not verified.

## 5. Limitations and costs

- Windows toolchain: `pyproject.toml` requires `Cython>=3.2.4` and `setuptools` (https://github.com/scoder/lupa/blob/master/pyproject.toml). No MSVC version is pinned. Upstream CI uses `TheMrMilchmann/setup-msvc-dev` at a fixed action version, with the runner's default MSVC (`.github/workflows/wheels.yml`). The CI also runs `rm -f /usr/bin/link` so MSVC `link.exe` is found.
- Wheels: `pyproject.toml` sets `LUPA_USE_BUNDLE=true` for Linux, and `LUPA_WITH_LUA_DLOPEN` for Linux and macOS. The test command imports `lupa.lua54` (and `lupa.luajit21` on Windows), so a custom build must change those test commands. Linux wheels go through `auditwheel repair` (`pyproject.toml`). A shared-library Lua passed to `--lua-lib` would need vendoring by `auditwheel`; inferred, not tested.
- Lost with a custom build: the multi-version module set (`lupa.lua51` to `lupa.lua55`, `lupa.luajit20`/`21`); `__init__.py` then exposes only `lupa.lua`. Changelog 2.7 says Lua 5.5 is in the binary wheels (https://github.com/scoder/lupa/blob/master/CHANGES.rst). LuaJIT is available only if it is supplied (pkg-config `luajit` or the `luajit*` directory discovery).
- Licence: lupa is MIT-style (`LICENSE.txt`, `setup.py` `license='MIT style'`). Lua 5.5 carries the MIT copyright notice in `lua.h` (Lua.org, PUC-Rio). Patches to Lua carry the same MIT terms; no copyleft is introduced by lupa. The bundled LuaJIT licence was not read.
- Maintenance: CHANGES records bundled-runtime updates in most releases (Lua 5.4 patch releases in 2.3 and 2.5; LuaJIT branch updates in 2.3, 2.5, 2.6; Lua 5.5 included in binary wheels from 2.7). A fork must be rebased onto each lupa release and each Lua submodule bump. The build also depends on lupa's `setup.py` parsing the Lua makefile (section 2), and on Cython: when Cython is absent, `setup.py` uses the pre-generated `lupa/lua55.c` (`prepare_extensions`), which must be regenerated after any change to `_lupa.pyx` (inferred).

## Open / unverified

- A shared-library `.so` passed to `--lua-lib` on Linux: not tested; the code passes it as an `extra_objects` input.
- Whether `lua_*` symbols are exported from `lupa/lua55*.so` on Linux, and whether a C module then resolves them under `allow_lua_module_loading`: not built or run.
- Windows `.pyd` export behaviour: inferred from `luaconf.h` and `setup.py`, not checked with a linker.
- Whether pkg-config `lua`/`lua5.4` reporting 5.5.x is accepted in practice: inferred from names and minimums.
- Behaviour when Windows has no pkg-config: `find_lua_build` returns `{}`, `no_lua_error()` prints a message, and the config list then contains `{}`; the resulting failure mode was not traced.
- Whether issue #218 and the `lua.pyx` naming are fully resolved in current code: closure only.
- Bundled LuaJIT licence text: not read.
- The issue survey is partial (public API search, one page of results, no `gh` login).
