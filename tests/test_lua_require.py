"""Packet 06a — the replacement ``require``: resolution and caching.

Schedules load helper modules through ATE's replacement ``require``, which only knows the module
names listed in the Test Definition (never ``package.path``). Unknown names raise naming the module;
modules run once per host (Test Run) and are cached, so a second ``require`` of the same name returns
the cached value. Verified against a real lupa runtime — no mocks."""

import hashlib
import json
from pathlib import Path

import pytest

from ate.catalog import LuaHost, module_map, read_definition
from ate.engine.lua_host import HostError, NotAllowedError


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# A helper module that increments a shared counter each time it actually runs and reports how many
# times it has run. Requiring it twice must run it once (counter stays 1) and return the same value.
COUNTER_MODULE = """\
-- packet 06a: a require'd module counts how many times it really ran, then returns that count
COUNTER = (COUNTER or 0) + 1
return { name = "counter", count = COUNTER }
"""


def _write_def_with_module(tmp_path: Path) -> tuple[Path, dict]:
    """Write one Media Release listing a ``helper.lua`` module and a schedule that requires it.

    Returns ``(defs_dir, definition)`` where ``definition`` is the parsed :class:`Definition`."""
    defs_dir = tmp_path / "defs"
    defs_dir.mkdir(parents=True, exist_ok=True)

    helper_bytes = COUNTER_MODULE.encode("utf-8")
    (defs_dir / "helper.lua").write_bytes(helper_bytes)
    # A schedule that requires the helper at load time and inside a step, then reports PASS.
    main_text = (
        'local helper = require("helper")\n'
        "_G.H_NAME = helper.name\n"
        'return ScheduleParams("Load require", "requires its helper at load time") :setSchedule({\n'
        '  StepParams("use helper"):setStepNo("1.")\n'
        '    :setAction(function() ATE.report(Result.PASS, "helper loaded") end),\n'
        "})\n"
    )
    (defs_dir / "main.tslua").write_text(main_text, encoding="utf-8")

    atedef = {
        "format": 1,
        "mediaNumber": "REL-06A",
        "name": "Require release",
        "version": "1.0.0",
        "files": [
            {"path": "helper.lua", "sha256": _sha256(helper_bytes)},
            {"path": "main.tslua", "sha256": _sha256(main_text.encode("utf-8"))},
        ],
    }
    (defs_dir / "demo.atedef").write_text(json.dumps(atedef), encoding="utf-8")

    definition = read_definition(defs_dir / "demo.atedef")
    return defs_dir, definition


def _host(tmp_path: Path) -> tuple[Path, LuaHost]:
    """Build a host for the schedule with this release's module map installed."""
    defs_dir, definition = _write_def_with_module(tmp_path)
    modules = module_map(definition)
    return defs_dir, LuaHost(str(defs_dir / "main.tslua"), modules=modules)


def test_module_map_lists_helper_modules(tmp_path: Path) -> None:
    """``module_map`` maps every non-schedule entry with a module name to its absolute path."""
    defs_dir, definition = _write_def_with_module(tmp_path)

    mapping = module_map(definition)
    assert "helper" in mapping
    assert Path(mapping["helper"]) == (defs_dir / "helper.lua").resolve()


def test_listed_module_resolves(tmp_path: Path) -> None:
    """A listed module name resolves and its return value is available to the schedule."""
    defs_dir, host = _host(tmp_path)
    assert host.schedule.name == "Load require"
    # The load-time require ran and its value is visible to the schedule's step.
    # The load-time require resolved and its value flowed into the schedule's global, which lives
    # in the top-level instance's own global table (packet 15b), not the shared one.
    name = host.instances[0].env["H_NAME"]
    assert name == "counter"  # helper.name from the loaded module


def test_unknown_name_raises_naming_the_module(tmp_path: Path) -> None:
    """Requiring a name not listed in the definition raises, naming the module."""
    _, host = _host(tmp_path)
    with pytest.raises(NotAllowedError) as excinfo:
        host.runtime.execute("require('ghost')")
    assert "ghost" in str(excinfo.value)


def test_package_path_module_is_not_found(tmp_path: Path) -> None:
    """A module that exists on disk (and package.path) is not found unless it is listed."""
    _, host = _host(tmp_path)
    with pytest.raises(NotAllowedError) as excinfo:
        host.runtime.execute("require('os')")
    assert "os" in str(excinfo.value)


def test_requiring_twice_runs_once_and_returns_same_value(tmp_path: Path) -> None:
    """A second require of the same name returns the cached value and does not re-run the module."""
    _, host = _host(tmp_path)
    # Read a scalar field out of each require result so they can be compared: Lua tables never
    # compare equal by value, and lupa wraps the same underlying object in a fresh proxy on every
    # access, so identity across two execute() calls is meaningless. Both cache hits expose the
    # module's count, which stays 1 because it ran once for the whole host.
    first = host.runtime.execute("return require('helper').count")
    second = host.runtime.execute("return require('helper').count")
    assert first == 1 and second == 1 and first == second
    # The module's side effect ran exactly once across both requires (counter stays 1).
    counter = host.runtime.execute("return _G.COUNTER")
    assert counter == 1
