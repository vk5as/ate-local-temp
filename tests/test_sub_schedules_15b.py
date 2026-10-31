"""Packet 15b seam-1 tests: `setInclude`, Sub-schedule instances and the rows they record.

Real `.atedef` trees and real `.tslua` files on disk, expanded by a `LuaHost` that carries the
release's include map, and run end to end through :class:`HeadlessRunSession`. The channel example
from `assets/test-schedule-format.md` is the centre of gravity: one `channel.tslua` included twice
with different `keyvals`, each include its own instance; each include header carries a `setPrefix`
so the expanded tree has no duplicate Step Numbers (packet 15c checks that). Cycle detection and
prefix stacking are tested in `test_tree_checks_15c.py`; `setEnable` forcing belongs to packet 15d.
"""

import hashlib
import json
import time
from pathlib import Path
from typing import Optional

import pytest
from PyQt6.QtCore import Qt

from ate.catalog import _search_chain, definition_mismatches, find_definitions, include_map, list_schedule_rows, module_map
from ate.engine.lua_host import HostError, LuaHost
from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.ui.staged_run_screen import StagedRunScreen

WRONG_SHA = "0" * 64

# The spec's channel example: runnable on its own, or included once per output channel.
CHANNEL_SRC = """\
local kv = ...
local ch = kv.channel or 1
return ScheduleParams("Channel check", "One output channel")
  :setSchedule({
    StepParams("output on"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "channel " .. ch, tostring(ch), tostring(ch)) end),
    StepParams("output off"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "off " .. ch, tostring(ch), tostring(ch)) end),
  })
"""

MAIN_SRC = """\
return ScheduleParams("Main schedule", "the channel example: two includes of one file")
  :setSchedule({
    StepParams("setup"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "setup", "1", "1") end),
    StepParams("channel 1"):setStepNo("2."):setPrefix("2."):setInclude("channel.tslua", { channel = 1 }),
    StepParams("channel 2"):setStepNo("3."):setPrefix("3."):setInclude("channel.tslua", { channel = 2 }),
  })
"""

# A `require`d module is shared by the host, so its state carries across both instances.
COUNTER_SRC = "local n = 0\nreturn { bump = function() n = n + 1 return n end, peek = function() return n end }\n"

COUNTING_CHANNEL_SRC = """\
local kv = ...
local counter = require("counter")
local n = counter.bump()
return ScheduleParams("Counting channel", "counts how many times this chunk ran")
  :setSchedule({
    StepParams("count"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "run " .. n, tostring(n), tostring(n)) end),
    StepParams("peek"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "ch " .. kv.channel .. " sees " .. counter.peek(), "1", "1") end),
  })
"""

# Two levels of nesting: main -> group -> channel.
GROUP_SRC = """\
local kv = ...
return ScheduleParams("Group schedule", "one channel instance per group member")
  :setSchedule({
    StepParams("inner"):setStepNo("1."):setPrefix("1."):setInclude("channel.tslua", { channel = kv.channel }),
  })
"""

NESTED_MAIN_SRC = """\
return ScheduleParams("Nested schedule", "a Sub-schedule that includes another Sub-schedule")
  :setSchedule({
    StepParams("group 1"):setStepNo("1."):setPrefix("1."):setInclude("group.tslua", { channel = 7 }),
    StepParams("plain"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "plain step", "1", "1") end),
  })
"""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entry(path: str, src: Optional[str] = None, sha: Optional[str] = None, module: Optional[str] = None) -> dict:
    """One `files` entry; `sha` is the definition's expectation (which may deliberately be wrong)."""
    entry = {"path": path, "sha256": sha if sha is not None else _sha(src)}
    if module:
        entry["module"] = module
    return entry


def _write_release(root: Path, label: str, files: list[dict], deps: Optional[list[dict]] = None) -> Path:
    """Write `<label>.atedef` plus every file it lists under `defs/<label>/`."""
    release_dir = root / label
    release_dir.mkdir(parents=True, exist_ok=True)
    (release_dir / f"{label}.atedef").write_text(
        json.dumps(
            {
                "format": 1,
                "mediaNumber": f"MR-{label.upper()}",
                "name": f"Release {label}",
                "version": "1.0.0",
                "depends": deps or [],
                "files": files,
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    return release_dir


def _write_files(directory: Path, files: dict[str, str]) -> None:
    for rel, src in files.items():
        target = directory / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(src, encoding="utf-8", newline="\n")


def _config(tmp_path: Path, root: Path) -> Path:
    config = {
        "format": 1,
        "station": {"name": "BENCH-15", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c11"},
        "defaultProject": "Power board",
        "database": {"type": "sqlite", "path": str(tmp_path / "ate.db"), "storeMismatchedFiles": "ask"},
        "reports": {"directory": str(tmp_path / "reports")},
        "testDefinitions": {"directories": [str(root)]},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8", newline="\n")
    return path


def _channel_release(tmp_path: Path, label: str = "main") -> Path:
    """The channel example as one release: main.tslua + channel.tslua + counter.lua."""
    root = tmp_path / "defs"
    files = {
        "main.tslua": MAIN_SRC,
        "channel.tslua": CHANNEL_SRC,
        "counter.lua": COUNTER_SRC,
    }
    directory = _write_release(root, label, [_entry(p, s) for p, s in files.items()])
    _write_files(directory, files)
    return root


def _definition(root: Path, label: str):
    return next(d for d in find_definitions(root) if d.media_number == f"MR-{label.upper()}")


def _host(root: Path, label: str, schedule: str = "main.tslua") -> LuaHost:
    """A host for one schedule of a release, with that release's module and include maps."""
    definition = _definition(root, label)
    return LuaHost(definition.path / schedule, modules=module_map(definition), includes=include_map(definition))


def _finish(qapp, session: HeadlessRunSession) -> bool:
    """Pump the event loop until the run is FINISHED and its report job is done."""
    written: list[tuple[str, str]] = []
    session.report_written.connect(lambda html, pdf: written.append((html, pdf)))
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if session._state == SessionState.FINISHED and written:
            return True
        time.sleep(0.005)
    raise TimeoutError("the run never reached FINISHED with its report written")


def _nodes(session: HeadlessRunSession) -> dict[int, dict]:
    return {row["node_seq"]: row for row in session.store.nodes(session.run_uuid)}


def _instances(session: HeadlessRunSession) -> dict[int, dict]:
    return {row["instance_seq"]: row for row in session.store.schedule_instances(session.run_uuid)}


def _attempts(session: HeadlessRunSession) -> list[dict]:
    return session.store.attempts(session.run_uuid)


def _step_messages(host: LuaHost) -> list[Optional[str]]:
    """Run every step of the expanded tree in order, as the engine would; return each report's msg.

    This is what the schedule's own Lua sees when its actions run after the whole tree loaded."""
    messages = []
    for entry in host.tree:
        if entry.is_header:
            continue
        before = len(host.reports)
        host._run_action(entry.action, "step")
        messages.append(host.reports[-1][1] if len(host.reports) > before else None)
    return messages


def _bare(tmp_path: Path, files: dict[str, str], schedule: str = "main.tslua") -> LuaHost:
    """A bare host (no Test Definition) over `files` written into one directory."""
    directory = tmp_path / "bare"
    _write_files(directory, files)
    return LuaHost(directory / schedule)


# --- expansion of the channel example --------------------------------------


def test_expansion_walks_the_tree_in_node_order(tmp_path) -> None:
    """Every include is expanded where its header sits, depth first, with parent links."""
    host = _host(_channel_release(tmp_path), "main")
    tree = host.tree
    assert [(t.kind, t.parent, t.instance, t.depth) for t in tree] == [
        ("step", None, 0, 0),   # setup
        ("header", None, 0, 0),  # channel 1
        ("step", 1, 1, 1),      # output on
        ("step", 1, 1, 1),      # output off
        ("header", None, 0, 0),  # channel 2
        ("step", 4, 2, 1),
        ("step", 4, 2, 1),
    ]
    assert [t.name for t in tree] == ["setup", "channel 1", "output on", "output off", "channel 2", "output on", "output off"]
    assert len(tree) == len(host.schedule.steps) + 4  # the two headers plus their four steps


def test_each_include_is_its_own_instance(tmp_path) -> None:
    """The top-level schedule plus one instance per include, with parent, header, file and keyvals."""
    host = _host(_channel_release(tmp_path), "main")
    instances = host.instances
    assert len(instances) == 3
    top = instances[0]
    assert (top.parent_instance, top.header, top.name, top.file, top.media_number, top.prefix, top.keyvals) == (
        None, None, "Main schedule", "main.tslua", "MR-MAIN", "", {}
    )
    for seq, header_node in enumerate((1, 4), start=1):
        inst = instances[seq]
        assert (inst.parent_instance, inst.header, inst.name, inst.file, inst.media_number, inst.prefix, inst.keyvals) == (
            0, header_node, "Channel check", "channel.tslua", "MR-MAIN", f"{seq + 1}.", {"channel": seq}
        )


def test_keyvals_reach_the_included_chunk(tmp_path) -> None:
    """A nested keyvals table survives; a top-level schedule with no keyvals receives `{}`."""
    root = tmp_path / "defs"
    src = """\
local kv = ...
return ScheduleParams("Deep keyvals", "reads a nested table")
  :setSchedule({
    StepParams("read"):setStepNo("1.")
      :setAction(function()
        local inner = kv.settings or {}
        ATE.report(Result.PASS, "n=" .. (kv.n or 0) .. " tag=" .. (inner.tag or "-") .. " second=" .. tostring(kv.list and kv.list[2]) .. " " .. type(kv.settings), "1", "1")
      end),
  })
"""
    main = """\
local kv = ...
return ScheduleParams("Keyvals main", "passes nested keyvals")
  :setSchedule({
    StepParams("top"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, type(kv) .. " " .. tostring(next(kv)), "1", "1") end),
    StepParams("child"):setStepNo("2."):setPrefix("2."):setInclude("child.tslua", { n = 3, settings = { tag = "abc" }, list = { 10, 20 } }),
  })
"""
    directory = _write_release(root, "kv", [_entry("main.tslua", main), _entry("child.tslua", src)])
    _write_files(directory, {"main.tslua": main, "child.tslua": src})
    host = _host(root, "kv")
    assert host.instances[1].keyvals == {"n": 3, "settings": {"tag": "abc"}, "list": {1: 10, 2: 20}}
    # Seen from Lua: the top level got an empty table, the include its nested keyvals, array included.
    assert _step_messages(host) == ["table nil", "n=3 tag=abc second=20 table"]


def test_a_require_d_module_is_shared_between_instances(tmp_path) -> None:
    """The chunk runs again per include, but `require`d module state carries across them."""
    root = tmp_path / "defs"
    main = """\
return ScheduleParams("Shared module schedule", "two includes of the counting channel")
  :setSchedule({
    StepParams("first"):setStepNo("1."):setPrefix("1."):setInclude("channel.tslua", { channel = 1 }),
    StepParams("second"):setStepNo("2."):setPrefix("2."):setInclude("channel.tslua", { channel = 2 }),
  })
"""
    files = {"main.tslua": main, "channel.tslua": COUNTING_CHANNEL_SRC, "counter.lua": COUNTER_SRC}
    directory = _write_release(root, "shared", [_entry(p, s) for p, s in files.items()])
    _write_files(directory, files)
    host = _host(root, "shared")
    assert [t.step_no for t in host.tree] == ["1.", "1.1.", "1.2.", "2.", "2.1.", "2.2."]  # each instance under its own prefix
    # Seen from the steps: each instance bumped the one shared counter at load (1, then 2), and
    # both read 2 when they run, because `require` loaded the module once for the whole run.
    assert _step_messages(host) == ["run 1", "ch 1 sees 2", "run 2", "ch 2 sees 2"]
    assert [kind for _path, kind, _sha in host._files_used].count("lua") == 1


# --- each instance has its own globals ---------------------------------------

MARKER_SRC = """\
local kv = ...
local seen_at_load = tostring(MARK)
MARK = kv.channel
return ScheduleParams("Marker", "sets a global per instance")
  :setSchedule({
    StepParams("mark"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, seen_at_load .. "/" .. tostring(MARK), "1", "1") end),
  })
"""


def test_sibling_includes_do_not_share_globals(tmp_path) -> None:
    """A global one include sets is invisible to its sibling, at load and when the actions run."""
    main = """\
return ScheduleParams("Main schedule", "includes the marker twice")
  :setSchedule({
    StepParams("a"):setStepNo("1."):setPrefix("1."):setInclude("sub.tslua", { channel = 1 }),
    StepParams("b"):setStepNo("2."):setPrefix("2."):setInclude("sub.tslua", { channel = 2 }),
  })
"""
    host = _bare(tmp_path, {"main.tslua": main, "sub.tslua": MARKER_SRC})
    # Shared globals would read "nil/2" and "1/2": instance 2 sees instance 1's MARK at load, and
    # both actions read the last value written.
    assert _step_messages(host) == ["nil/1", "nil/2"]


def test_parent_and_include_globals_do_not_leak_either_way(tmp_path) -> None:
    """The parent's globals (`_G.x` included) don't reach the include, and the include's don't reach the parent."""
    main = """\
PARENT = "p"
_G.VIA_G = "g"
return ScheduleParams("Main schedule", "sets globals around an include")
  :setSchedule({
    StepParams("child"):setStepNo("1."):setPrefix("1."):setInclude("sub.tslua"),
    StepParams("after"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, tostring(CHILD) .. " " .. PARENT .. " " .. VIA_G, "1", "1") end),
  })
"""
    sub = """\
CHILD = "c"
local at_load = tostring(PARENT) .. " " .. tostring(VIA_G)
return ScheduleParams("Sub", "reads the parent's globals")
  :setSchedule({
    StepParams("read"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, at_load .. " " .. tostring(PARENT) .. " " .. CHILD, "1", "1") end),
  })
"""
    host = _bare(tmp_path, {"main.tslua": main, "sub.tslua": sub})
    assert _step_messages(host) == ["nil nil nil c", "nil p g"]
    assert host.instances[0].env["PARENT"] == "p" and host.instances[1].env["CHILD"] == "c"


def test_shadowing_a_shared_name_stays_in_its_instance(tmp_path) -> None:
    """Assigning the global `ATE` or `Result` in an include only shadows it there."""
    main = """\
return ScheduleParams("Main schedule", "an include shadows ATE")
  :setSchedule({
    StepParams("child"):setStepNo("1."):setPrefix("1."):setInclude("sub.tslua"),
    StepParams("after"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "top still has " .. Result.PASS, "1", "1") end),
  })
"""
    sub = """\
ATE, Result = nil, nil
return ScheduleParams("Sub", "no ATE here") :setSchedule({})
"""
    host = _bare(tmp_path, {"main.tslua": main, "sub.tslua": sub})
    assert _step_messages(host) == ["top still has PASS"]


@pytest.mark.parametrize(
    "statement, expected",
    [
        ('Result.PASS = "FAIL"', "Result.PASS is read-only"),
        ('Log.INFO = "DEBUG"', "Log.INFO is read-only"),
        ("ATE.log = function() end", "ATE.log is read-only"),
        ("ATE.report = nil", "ATE.report is read-only"),
        ("StepParams.setAction = nil", "StepParams.setAction is read-only"),
        ("ScheduleParams.extra = 1", "ScheduleParams.extra is read-only"),
        ('rawset(Result, "PASS", "FAIL")', "rawset: table is read-only"),
        ("setmetatable(Result, nil)", "protected metatable"),
    ],
)
def test_an_include_cannot_redefine_the_shared_api(tmp_path, statement, expected) -> None:
    """Writing into `ATE`, an enum table or a builder from an include is a load error."""
    main = """\
return ScheduleParams("Main schedule", "includes a schedule that rewrites the API")
  :setSchedule({ StepParams("child"):setStepNo("1."):setPrefix("1."):setInclude("sub.tslua") })
"""
    sub = statement + '\nreturn ScheduleParams("Sub", "") :setSchedule({})\n'
    with pytest.raises(HostError) as excinfo:
        _bare(tmp_path, {"main.tslua": main, "sub.tslua": sub})
    assert expected in str(excinfo.value)
    assert "main.tslua:1.: include 'sub.tslua'" in str(excinfo.value)


@pytest.mark.parametrize(
    "escape",
    [
        # debug.getregistry holds the instance loader and, as its upvalue, the stock load.
        "for _, v in pairs(debug.getregistry()) do end",
        # debug.setmetatable would strip a read-only enum's lock.
        "debug.setmetatable(Result, nil)",
        # The instance metatable's __index is the shared global table; writing through it leaks.
        "getmetatable(_ENV).__index.LEAK = 1",
    ],
)
def test_an_include_cannot_reach_the_loader_or_the_shared_globals(tmp_path, escape) -> None:
    """Round-2 escapes: no `debug` library, and an instance's metatable is hidden."""
    main = """\
return ScheduleParams("Main schedule", "includes a schedule that tries to escape")
  :setSchedule({ StepParams("child"):setStepNo("1."):setPrefix("1."):setInclude("sub.tslua") })
"""
    sub = escape + '\nreturn ScheduleParams("Sub", "") :setSchedule({})\n'
    with pytest.raises(HostError) as excinfo:
        _bare(tmp_path, {"main.tslua": main, "sub.tslua": sub})
    assert "main.tslua:1.: include 'sub.tslua'" in str(excinfo.value)


def test_the_enum_tables_still_iterate(tmp_path) -> None:
    """Read-only enums keep working for reads and `pairs`."""
    main = """\
local n = 0
for _ in pairs(Result) do n = n + 1 end
return ScheduleParams("Main schedule", "counts Result labels")
  :setSchedule({ StepParams("x"):setStepNo("1.")
    :setAction(function() ATE.report(Result.PASS, n .. " " .. Result.NOTRUN, "1", "1") end) })
"""
    host = _bare(tmp_path, {"main.tslua": main})
    assert _step_messages(host) == ["7 NOTRUN"]


# --- resolution through the Test Definition --------------------------------


def test_an_include_is_resolved_from_a_dependency_release(tmp_path) -> None:
    """A Sub-schedule listed only by a dependency is found, and its release owns the instance."""
    root = tmp_path / "defs"
    main = """\
return ScheduleParams("Dependency main", "includes a Sub-schedule owned by a dependency")
  :setSchedule({ StepParams("dep"):setStepNo("1."):setPrefix("1."):setInclude("sub.tslua", { channel = 5 }) })
"""
    own = [_entry("main.tslua", main)]
    dep = [_entry("sub.tslua", CHANNEL_SRC)]
    _write_release(root, "own", own, [{"name": "Release dep", "version": "^1.0"}])
    _write_files(root / "own", {"main.tslua": main})
    dep_dir = _write_release(root, "dep", dep)
    _write_files(dep_dir, {"sub.tslua": CHANNEL_SRC})
    host = _host(root, "own")
    inst = host.instances[1]
    assert (inst.file, inst.media_number, inst.keyvals) == ("sub.tslua", "MR-DEP", {"channel": 5})
    assert host.tree[0].kind == "header" and host.tree[1].depth == 1


def test_an_include_is_found_transitively_through_a_dependency_chain(tmp_path) -> None:
    """The include search walks the chain depth first, so a grandchild release can own it."""
    root = tmp_path / "defs"
    main = """\
return ScheduleParams("Main schedule", "includes a Sub-schedule owned by a grandchild release")
  :setSchedule({ StepParams("deep"):setStepNo("1."):setPrefix("1."):setInclude("sub.tslua") })
"""
    _write_release(root, "own", [_entry("main.tslua", main)], [{"name": "Release mid", "version": "^1.0"}])
    _write_files(root / "own", {"main.tslua": main})
    mid = _write_release(root, "mid", [], [{"name": "Release leaf", "version": "^1.0"}])
    _write_files(mid, {})
    leaf = _write_release(root, "leaf", [_entry("sub.tslua", CHANNEL_SRC)])
    _write_files(leaf, {"sub.tslua": CHANNEL_SRC})
    definition = _definition(root, "own")
    assert [m.media_number for m in _search_chain(definition)] == ["MR-OWN", "MR-MID", "MR-LEAF"]
    host = _host(root, "own")
    inst = host.instances[1]
    assert (inst.file, inst.media_number, inst.keyvals) == ("sub.tslua", "MR-LEAF", {})
    assert host.tree[1].name == "output on" and host.tree[1].depth == 1


# --- the recorded rows of a real run ----------------------------------------


def test_the_channel_example_runs_and_records_its_tree(qapp, tmp_path) -> None:
    """Node rows in tree order with parents and instances; one schedule_instance per schedule."""
    root = _channel_release(tmp_path)
    session = HeadlessRunSession(_config(tmp_path, root))
    session.select_schedule("Main schedule")
    session.start()
    assert _finish(qapp, session)
    run_uuid = session.run_uuid
    nodes = _nodes(session)
    assert len(nodes) == 7
    assert [n["kind"] for n in nodes.values()] == ["step", "header", "step", "step", "header", "step", "step"]
    assert [n["parent_node_seq"] for n in nodes.values()] == [None, None, 1, 1, None, 4, 4]
    assert [n["instance_seq"] for n in nodes.values()] == [0, 0, 1, 1, 0, 2, 2]
    assert [n["result"] for n in nodes.values()] == ["PASS", None, "PASS", "PASS", None, "PASS", "PASS"]
    assert nodes[1]["step_name"] == "channel 1" and nodes[1]["attempt_count"] == 0

    instances = _instances(session)
    assert sorted(instances) == [0, 1, 2]
    top = instances[0]
    assert (top["parent_instance_seq"], top["header_node_seq"], top["schedule_name"], top["file"], top["media_number"], top["prefix"], top["keyvals"]) == (
        None, None, "Main schedule", "main.tslua", "MR-MAIN", "", "{}"
    )
    for seq, channel in enumerate((1, 2), start=1):
        row = instances[seq]
        assert (row["parent_instance_seq"], row["header_node_seq"], row["schedule_name"], row["file"], row["media_number"], row["prefix"]) == (
            0, (1, 4)[seq - 1], "Channel check", "channel.tslua", "MR-MAIN", f"{seq + 1}."
        )
        assert json.loads(row["keyvals"]) == {"channel": channel}

    # Each instance sees its own keyvals: the Attempt messages read back channel 1 and channel 2.
    messages = [a["msg"] for a in _attempts(session)]
    assert messages == ["setup", "channel 1", "off 1", "channel 2", "off 2"]

    files = {row["path"]: row for row in session.store.run_files(run_uuid)}
    assert "channel.tslua" in files
    assert files["channel.tslua"]["kind"] == "tslua" and files["channel.tslua"]["media_number"] == "MR-MAIN"
    assert files["channel.tslua"]["computed_sha256"] == _sha(CHANNEL_SRC) == files["channel.tslua"]["expected_sha256"]
    assert session.store.get_test_run(run_uuid)["result"] == "PASS"
    assert session.store.get_test_run(run_uuid)["schedule_name"] == "Main schedule"


def test_a_shared_require_d_module_carries_state_across_instances(qapp, tmp_path) -> None:
    """Two includes of the same counting Sub-schedule report run 1 then run 2."""
    root = tmp_path / "defs"
    main = """\
return ScheduleParams("Shared module schedule", "two includes of the counting channel")
  :setSchedule({
    StepParams("first"):setStepNo("1."):setPrefix("1."):setInclude("channel.tslua", { channel = 1 }),
    StepParams("second"):setStepNo("2."):setPrefix("2."):setInclude("channel.tslua", { channel = 2 }),
  })
"""
    files = {"main.tslua": main, "channel.tslua": COUNTING_CHANNEL_SRC, "counter.lua": COUNTER_SRC}
    directory = _write_release(root, "shared", [_entry(p, s) for p, s in files.items()])
    _write_files(directory, files)
    session = HeadlessRunSession(_config(tmp_path, root))
    session.select_schedule("Shared module schedule")
    session.start()
    assert _finish(qapp, session)
    assert [a["msg"] for a in _attempts(session)] == ["run 1", "ch 1 sees 2", "run 2", "ch 2 sees 2"]
    assert session.store.get_test_run(session.run_uuid)["result"] == "PASS"


def test_two_levels_of_nesting_are_recorded_as_one_tree(qapp, tmp_path) -> None:
    """main -> group -> channel: node order, parents, and three instances with one grandchild."""
    root = tmp_path / "defs"
    files = {"main.tslua": NESTED_MAIN_SRC, "group.tslua": GROUP_SRC, "channel.tslua": CHANNEL_SRC}
    directory = _write_release(root, "nest", [_entry(p, s) for p, s in files.items()])
    _write_files(directory, files)
    session = HeadlessRunSession(_config(tmp_path, root))
    session.select_schedule("Nested schedule")
    session.start()
    assert _finish(qapp, session)
    nodes = _nodes(session)
    assert [n["kind"] for n in nodes.values()] == ["header", "header", "step", "step", "step"]
    assert [n["parent_node_seq"] for n in nodes.values()] == [None, 0, 1, 1, None]
    assert [n["instance_seq"] for n in nodes.values()] == [0, 1, 2, 2, 0]
    instances = _instances(session)
    assert instances[2]["parent_instance_seq"] == 1
    assert instances[2]["header_node_seq"] == 1
    assert json.loads(instances[2]["keyvals"]) == {"channel": 7}
    assert [a["msg"] for a in _attempts(session)] == ["channel 7", "off 7", "plain step"]


def test_an_included_schedule_doc_fields_and_stop_on_fail_are_ignored(qapp, tmp_path) -> None:
    """Only the top-level schedule's doc fields and `setStopOnFail` take part."""
    root = tmp_path / "defs"
    sub = """\
return ScheduleParams("Sub with doc fields", "ignored description")
  :setDocTitle("Ignored title")
  :setDocId("IGNORED-ID")
  :setDocRev("9")
  :setStopOnFail(false)
  :setSchedule({
    StepParams("fails"):setStepNo("1.")
      :setAction(function() ATE.report(Result.FAIL, "sub failed", "0", "1") end),
  })
"""
    main = """\
return ScheduleParams("Stop on fail main", "the top level's stop-on-fail still applies")
  :setDocTitle("Main Report")
  :setStopOnFail(true)
  :setSchedule({ StepParams("sub"):setStepNo("1."):setPrefix("1."):setInclude("sub.tslua") })
"""
    directory = _write_release(root, "clf", [_entry("main.tslua", main), _entry("sub.tslua", sub)])
    _write_files(directory, {"main.tslua": main, "sub.tslua": sub})
    session = HeadlessRunSession(_config(tmp_path, root))
    session.select_schedule("Stop on fail main")
    assert session.selected_stop_on_fail is True  # the include's setStopOnFail(false) did not reach it
    session.failure_prompt.connect(lambda _seq, _attempt: session.choose("CONTINUE"))
    session.start()
    assert _finish(qapp, session)
    run = session.store.get_test_run(session.run_uuid)
    assert run["doc_title"] == "Main Report" and run["doc_id"] is None and run["doc_rev"] is None
    assert run["stop_on_fail"] == 1 and run["result"] == "FAIL"


# --- load errors: the including schedule breaks, its release does not --------


def _error_of(root: Path, label: str) -> Optional[str]:
    """The picker's row for `main.tslua` of a release: its error, or None if it loads.

    A schedule that fails to load has no name on its row, so the broken row is found by the
    filename its error quotes (`main.tslua:1.: ...`)."""
    for row in list_schedule_rows(root):
        if row.media_number != f"MR-{label.upper()}":
            continue
        if row.error is None:
            if row.name == "Main schedule":
                return None
        elif "main.tslua:" in row.error:
            return row.error
    raise AssertionError(f"no picker row for main.tslua in MR-{label.upper()}")


def test_a_sibling_schedule_still_loads_when_an_include_is_broken(tmp_path) -> None:
    """A Sub-schedule problem breaks the schedule that includes it, not its Media Release."""
    root = tmp_path / "defs"
    main = """\
return ScheduleParams("Main schedule", "includes a file that is not listed")
  :setSchedule({ StepParams("x"):setStepNo("1."):setPrefix("1."):setInclude("nowhere.tslua") })
"""
    sibling = """\
return ScheduleParams("Sibling schedule", "no includes at all")
  :setSchedule({ StepParams("ok"):setStepNo("1.")
    :setAction(function() ATE.report(Result.PASS, "fine", "1", "1") end) })
"""
    directory = _write_release(root, "sib", [_entry("main.tslua", main), _entry("sibling.tslua", sibling)])
    _write_files(directory, {"main.tslua": main, "sibling.tslua": sibling})
    rows = list_schedule_rows(root)
    broken = [r for r in rows if r.error]
    assert len(broken) == 1 and "not listed" in broken[0].error
    assert any(r.error is None and r.name == "Sibling schedule" for r in rows)
    assert not any(r.name == "Main schedule" and r.error is None for r in rows)


@pytest.mark.parametrize(
    "include_file, sub_src, keyvals, expected",
    [
        ("nowhere.tslua", None, None, "main.tslua:1.: include 'nowhere.tslua' is not listed in the Test Definition or its dependencies"),
        ("bad.tslua", 'return ScheduleParams("Bad", "")\n  :setSchedule({ StepParams("x"):setStepNo("1.") })\n',
         None, "ordinary step needs an Action"),
        ("boom.tslua", 'error("the sub-schedule raises")\n', None, "failed to load"),
    ],
)
def test_include_load_errors_break_the_including_schedule(tmp_path, include_file, sub_src, keyvals, expected) -> None:
    """Not listed, missing on disk, raising while loading, and a bad setter inside the include."""
    root = tmp_path / "defs"
    keyvals_src = "" if keyvals is None else ", " + keyvals
    main = (
        'return ScheduleParams("Main schedule", "an include that fails")\n'
        f'  :setSchedule({{ StepParams("x"):setStepNo("1."):setPrefix("1."):setInclude("{include_file}"{keyvals_src}) }})\n'
    )
    files = {"main.tslua": main}
    if sub_src is not None:
        files[include_file] = sub_src
    directory = _write_release(root, "err", [_entry(p, s) for p, s in files.items()])
    _write_files(directory, files)
    message = _error_of(root, "err")
    assert message is not None
    assert expected in message
    assert "main.tslua:1.:" in message and f"include '{include_file}'" in message
    assert message.count(f"'{include_file}'") == 1  # the include is named once, by its own level


def test_an_include_listed_but_absent_on_disk_is_a_load_error(tmp_path) -> None:
    """The release lists the Sub-schedule but the file has gone: a load error, not a silent skip.

    The catalog's own validation rejects a `.atedef` entry whose file is missing, so this case is
    reached with a hand-built definition (the same seam packet 07b uses for the search order)."""
    from ate.catalog import Definition, FileEntry

    directory = tmp_path / "own"
    directory.mkdir()
    (directory / "main.tslua").write_text(
        'return ScheduleParams("Main schedule", "includes a file that is not on disk")\n'
        '  :setSchedule({ StepParams("x"):setStepNo("1."):setPrefix("1."):setInclude("sub.tslua") })\n',
        encoding="utf-8",
        newline="\n",
    )
    definition = Definition(
        path=directory, media_number="MR-GONE", name="Release gone", version="1.0.0",
        files=(FileEntry(rel_path="main.tslua", is_schedule=True), FileEntry(rel_path="sub.tslua", is_schedule=True)),
    )
    with pytest.raises(HostError) as excinfo:
        LuaHost(directory / "main.tslua", modules=module_map(definition), includes=include_map(definition))
    message = str(excinfo.value)
    assert "include 'sub.tslua' is missing" in message
    assert "main.tslua:1.:" in message


def _named(name: str) -> str:
    """A Sub-schedule whose ScheduleParams name says which copy was loaded."""
    return f'return ScheduleParams("{name}", "") :setSchedule({{}})\n'


def test_the_including_release_own_file_wins_over_a_dependency(tmp_path) -> None:
    """Listed by the release itself and by a dependency: the release's own file wins, as with `require`."""
    root = tmp_path / "defs"
    main = """\
return ScheduleParams("Main schedule", "includes a file the release and a dependency both list")
  :setSchedule({ StepParams("x"):setStepNo("1."):setPrefix("1."):setInclude("sub.tslua") })
"""
    _write_release(root, "amb", [_entry("main.tslua", main), _entry("sub.tslua", _named("own sub"))],
                  [{"name": "Release dep", "version": "^1.0"}])
    _write_files(root / "amb", {"main.tslua": main, "sub.tslua": _named("own sub")})
    dep = _write_release(root, "dep", [_entry("sub.tslua", _named("dep sub"))])
    _write_files(dep, {"sub.tslua": _named("dep sub")})
    assert _error_of(root, "amb") is None
    inst = _host(root, "amb").instances[1]
    assert (inst.name, inst.media_number) == ("own sub", "MR-AMB")


def test_an_include_is_resolved_from_the_including_file_release(tmp_path) -> None:
    """Resolution is anchored at the release owning the INCLUDING file, not the top-level release.

    A lists `own.tslua` and depends on B; B lists `b.tslua` and its own `own.tslua`. A includes
    `b.tslua` (found in B), and `b.tslua` includes `own.tslua`, which must be B's own copy."""
    root = tmp_path / "defs"
    main = """\
return ScheduleParams("Main schedule", "includes b.tslua from its dependency")
  :setSchedule({ StepParams("b"):setStepNo("1."):setPrefix("1."):setInclude("b.tslua") })
"""
    b_src = """\
return ScheduleParams("B schedule", "includes own.tslua")
  :setSchedule({ StepParams("own"):setStepNo("1."):setPrefix("1."):setInclude("own.tslua") })
"""
    a_files = {"main.tslua": main, "own.tslua": _named("A own")}
    _write_release(root, "a", [_entry(p, src) for p, src in a_files.items()], [{"name": "Release b", "version": "^1.0"}])
    _write_files(root / "a", a_files)
    b_files = {"b.tslua": b_src, "own.tslua": _named("B own")}
    b_dir = _write_release(root, "b", [_entry(p, src) for p, src in b_files.items()])
    _write_files(b_dir, b_files)
    host = _host(root, "a")
    assert [(i.name, i.file, i.media_number) for i in host.instances] == [
        ("Main schedule", "main.tslua", "MR-A"),
        ("B schedule", "b.tslua", "MR-B"),
        ("B own", "own.tslua", "MR-B"),
    ]


def test_an_include_does_not_see_files_only_the_top_release_lists(tmp_path) -> None:
    """A dependency's schedule can't include a file that only the release above it lists."""
    root = tmp_path / "defs"
    main = """\
return ScheduleParams("Main schedule", "includes b.tslua from its dependency")
  :setSchedule({ StepParams("b"):setStepNo("1."):setPrefix("1."):setInclude("b.tslua") })
"""
    b_src = """\
return ScheduleParams("B schedule", "includes a file only A lists")
  :setSchedule({ StepParams("x"):setStepNo("1."):setPrefix("1."):setInclude("aonly.tslua") })
"""
    a_files = {"main.tslua": main, "aonly.tslua": _named("A only")}
    _write_release(root, "a", [_entry(p, src) for p, src in a_files.items()], [{"name": "Release b", "version": "^1.0"}])
    _write_files(root / "a", a_files)
    b_dir = _write_release(root, "b", [_entry("b.tslua", b_src)])
    _write_files(b_dir, {"b.tslua": b_src})
    message = _error_of(root, "a")
    assert "main.tslua:1.: include 'b.tslua' b.tslua:1.: include 'aonly.tslua' is not listed" in message


def test_an_include_two_dependencies_list_is_ambiguous(tmp_path) -> None:
    """Not in the release's own files and listed by two of its dependencies: a load error."""
    root = tmp_path / "defs"
    main = """\
return ScheduleParams("Main schedule", "includes a file two dependencies list")
  :setSchedule({ StepParams("x"):setStepNo("1."):setPrefix("1."):setInclude("sub.tslua") })
"""
    _write_release(root, "amb", [_entry("main.tslua", main)],
                  [{"name": "Release one", "version": "^1.0"}, {"name": "Release two", "version": "^1.0"}])
    _write_files(root / "amb", {"main.tslua": main})
    for label in ("one", "two"):
        dep = _write_release(root, label, [_entry("sub.tslua", CHANNEL_SRC)])
        _write_files(dep, {"sub.tslua": CHANNEL_SRC})
    message = _error_of(root, "amb")
    assert "main.tslua:1.: include 'sub.tslua' is ambiguous" in message
    assert "MR-ONE" in message and "MR-TWO" in message
    assert message.count("sub.tslua") == 1


def test_a_function_in_keyvals_is_a_load_error(tmp_path) -> None:
    """`keyvals` is plain data only: a function, nested anywhere, is a load error naming the header."""
    root = tmp_path / "defs"
    for keyvals in ("{ f = function() end }", "{ nested = { f = function() end } }", "{ x = StepParams(\"y\") }"):
        main = (
            'return ScheduleParams("Main schedule", "keyvals hold a function")\n'
            f'  :setSchedule({{ StepParams("x"):setStepNo("1."):setPrefix("1."):setInclude("channel.tslua", {keyvals}) }})\n'
        )
        directory = _write_release(root, "kv", [_entry("main.tslua", main), _entry("channel.tslua", CHANNEL_SRC)])
        _write_files(directory, {"main.tslua": main, "channel.tslua": CHANNEL_SRC})
        message = _error_of(root, "kv")
        assert "keyvals must be plain data" in message
        assert "main.tslua:1.:" in message


@pytest.mark.parametrize(
    "keyvals, expected",
    [
        ("5", "keyvals must be a table, got 5"),
        ("true", "keyvals must be a table, got True"),
        ('"channel=1"', "keyvals must be a table, got 'channel=1'"),
        ("setmetatable({}, {})", "keyvals must be plain data, got a table with a metatable"),
        ("{ inner = setmetatable({ x = 1 }, { __metatable = false }) }", "keyvals must be plain data, got a table with a metatable"),
        ("(function() local t = {} t.t = t return t end)()", "keyvals are nested more than 32 deep"),
        ("{ [{}] = 1 }", "keyvals must be plain data, got a key"),
    ],
)
def test_keyvals_that_are_not_a_plain_data_table_are_a_load_error(tmp_path, keyvals, expected) -> None:
    """A non-table, a table with a metatable (nested too) or a self-containing table: a clean load error."""
    main = (
        'return ScheduleParams("Main schedule", "bad keyvals")\n'
        f'  :setSchedule({{ StepParams("x"):setStepNo("1."):setPrefix("1."):setInclude("sub.tslua", {keyvals}) }})\n'
    )
    with pytest.raises(HostError) as excinfo:
        _bare(tmp_path, {"main.tslua": main, "sub.tslua": CHANNEL_SRC})
    assert expected in str(excinfo.value)
    assert "main.tslua:1.: include 'sub.tslua' keyvals" in str(excinfo.value)


def test_bare_host_reports_a_missing_include_as_a_load_error(tmp_path) -> None:
    """Without a Test Definition a missing include is the same HostError, not a raw FileNotFoundError."""
    main = """\
return ScheduleParams("Bare main", "includes a file that is not there")
  :setSchedule({ StepParams("x"):setStepNo("1."):setPrefix("1."):setInclude("gone.tslua") })
"""
    with pytest.raises(HostError) as excinfo:
        _bare(tmp_path, {"main.tslua": main})
    assert "main.tslua:1.: include 'gone.tslua' is missing (" in str(excinfo.value)


def test_a_self_include_is_a_cycle(tmp_path) -> None:
    """A schedule that includes itself fails at the first repeat with the cycle error (packet 15c)."""
    root = tmp_path / "defs"
    src = """\
return ScheduleParams("Looping schedule", "includes itself")
  :setSchedule({ StepParams("again"):setStepNo("1."):setPrefix("1."):setInclude("loop.tslua") })
"""
    directory = _write_release(root, "loop", [_entry("loop.tslua", src)])
    _write_files(directory, {"loop.tslua": src})
    includes = include_map(_definition(root, "loop"))
    with pytest.raises(HostError) as excinfo:
        host = LuaHost(directory / "loop.tslua", modules={}, includes=includes)
    message = str(excinfo.value)
    assert message == "loop.tslua:1.: include 'loop.tslua' is an include cycle: loop.tslua → loop.tslua"


# --- the pre-run mismatch scope (06c) ---------------------------------------


def test_a_mismatched_included_file_is_flagged_before_the_run(qapp, tmp_path) -> None:
    """`pending_mismatches` covers the `.tslua` files the schedule includes, not just its own."""
    root = tmp_path / "defs"
    files = {"main.tslua": MAIN_SRC, "channel.tslua": CHANNEL_SRC}
    directory = _write_release(root, "mis", [
        _entry("main.tslua", MAIN_SRC),
        _entry("channel.tslua", CHANNEL_SRC, sha=WRONG_SHA),
    ])
    _write_files(directory, files)
    session = HeadlessRunSession(_config(tmp_path, root))
    session.select_schedule("Main schedule")
    flagged = session.pending_mismatches()
    assert [(path, media) for path, _expected, _computed, media in flagged] == [("channel.tslua", "MR-MIS")]
    sibling = """\
return ScheduleParams("Sibling schedule", "does not include the mismatched file")
  :setSchedule({ StepParams("ok"):setStepNo("1.")
    :setAction(function() ATE.report(Result.PASS, "fine", "1", "1") end) })
"""
    _write_files(directory, {"sibling.tslua": sibling})
    (directory / "mis.atedef").write_text(
        json.dumps({
            "format": 1, "mediaNumber": "MR-MIS", "name": "Release mis", "version": "1.0.0",
            "files": [_entry("main.tslua", MAIN_SRC), _entry("channel.tslua", CHANNEL_SRC, sha=WRONG_SHA),
                      _entry("sibling.tslua", sibling)],
        }),
        encoding="utf-8",
        newline="\n",
    )
    definition = _definition(root, "mis")
    assert definition_mismatches(definition, "Sibling schedule") == []
    assert [m[0] for m in definition_mismatches(definition, "Main schedule")] == ["channel.tslua"]


# --- the Run screen -----------------------------------------------------------


def test_run_screen_lists_children_indented_under_their_header(qapp, tmp_path) -> None:
    """The Steps & options rows show the header and its expanded children, indented by depth."""
    root = _channel_release(tmp_path)
    session = HeadlessRunSession(_config(tmp_path, root))
    screen = StagedRunScreen(session)
    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    item = next(screen.test_schedule_stage.schedule_list.item(i)
                for i in range(screen.test_schedule_stage.schedule_list.count())
                if "Main schedule" in screen.test_schedule_stage.schedule_list.item(i).text())
    screen.test_schedule_stage.schedule_list.setCurrentItem(item)
    screen._begin_run_from_selection()
    texts = [screen.steps_options_stage.step_list.item(i).text() for i in range(screen.steps_options_stage.step_list.count())]
    assert texts == [
        "1.  setup",
        "2.  channel 1",
        "  2.1.  output on",
        "  2.2.  output off",
        "3.  channel 2",
        "  3.1.  output on",
        "  3.2.  output off",
    ]
    assert all(box.checkState() == Qt.CheckState.Checked for box in [
        screen.steps_options_stage.step_list.item(i) for i in range(7)
    ])


def test_disabling_an_included_child_records_it_notrun(qapp, tmp_path) -> None:
    """A toggle on a child row maps to that child's node seq and records NOTRUN with no Attempt."""
    root = _channel_release(tmp_path)
    session = HeadlessRunSession(_config(tmp_path, root))
    screen = StagedRunScreen(session)
    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    item = next(screen.test_schedule_stage.schedule_list.item(i)
                for i in range(screen.test_schedule_stage.schedule_list.count())
                if "Main schedule" in screen.test_schedule_stage.schedule_list.item(i).text())
    screen.test_schedule_stage.schedule_list.setCurrentItem(item)
    screen._begin_run_from_selection()
    screen.steps_options_stage.step_list.item(3).setCheckState(Qt.CheckState.Unchecked)  # "output off" of channel 1
    session.start()
    assert _finish(qapp, session)
    nodes = _nodes(session)
    assert nodes[3]["result"] == "NOTRUN" and nodes[3]["enabled"] == 0 and nodes[3]["default_enabled"] == 1
    assert not any(a["node_seq"] == 3 for a in _attempts(session))
    assert nodes[2]["result"] == "PASS" and nodes[5]["result"] == "PASS"
    rows = [screen.run_view.step_list.item(i).text() for i in range(screen.run_view.step_list.count())]
    # The finished list shows the whole tree: header rows (no result) with their children indented,
    # so the two "output on"/"output off" pairs read as two different places. Packet 15d adds the
    # header rollup: every step beneath a header has a Result here, so both read "2/2 done".
    assert rows == [
        "1.  setup  →  PASS",
        "2.  channel 1  2/2 done",
        "  2.1.  output on  →  PASS",
        "  2.2.  output off  →  NOTRUN",
        "3.  channel 2  2/2 done",
        "  3.1.  output on  →  PASS",
        "  3.2.  output off  →  PASS",
    ]


def test_the_paused_run_list_shows_headers_and_indents_children(qapp, tmp_path) -> None:
    """A failure in an include pauses the run; the paused list shows the tree with the failing row."""
    root = tmp_path / "defs"
    channel = CHANNEL_SRC.replace(
        'ATE.report(Result.PASS, "off " .. ch',
        'ATE.report(kv.fail and Result.FAIL or Result.PASS, "off " .. ch',
    )
    main = MAIN_SRC.replace("{ channel = 2 }", "{ channel = 2, fail = true }")
    files = {"main.tslua": main, "channel.tslua": channel}
    directory = _write_release(root, "pause", [_entry(p, src) for p, src in files.items()])
    _write_files(directory, files)
    session = HeadlessRunSession(_config(tmp_path, root))
    screen = StagedRunScreen(session)
    prompts: list = []
    session.failure_prompt.connect(lambda node_seq, attempt_no: prompts.append((node_seq, attempt_no)))
    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    item = next(screen.test_schedule_stage.schedule_list.item(i)
                for i in range(screen.test_schedule_stage.schedule_list.count())
                if "Main schedule" in screen.test_schedule_stage.schedule_list.item(i).text())
    screen.test_schedule_stage.schedule_list.setCurrentItem(item)
    screen._begin_run_from_selection()
    screen.steps_options_stage.run_button.click()
    deadline = time.monotonic() + 10.0
    while not prompts and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert prompts == [(6, 1)]
    rows = [screen.run_view.step_list.item(i).text() for i in range(screen.run_view.step_list.count())]
    # Packet 15d's rollup reads live: channel 1's two steps are both recorded, channel 2's failing
    # step has no node row yet (it is written after its last Attempt), so it counts 1 of 2.
    assert rows == [
        "1.  setup  →  PASS",
        "2.  channel 1  2/2 done",
        "  2.1.  output on  →  PASS",
        "  2.2.  output off  →  PASS",
        "3.  channel 2  1/2 done",
        "  3.1.  output on  →  PASS",
        "  3.2.  output off  →  FAIL",
    ]
    assert screen._failed_row is screen.run_view.step_list.item(6)
    screen.run_view.failure_prompt_bar.continue_button.click()
    assert _finish(qapp, session)


def test_bare_host_resolves_includes_against_its_own_directory(tmp_path) -> None:
    """Without a Test Definition the include is read from the schedule's directory (a test seam)."""
    directory = tmp_path / "sched"
    directory.mkdir()
    (directory / "main.tslua").write_text(
        'return ScheduleParams("Bare main", "no definition behind it")\n'
        '  :setSchedule({ StepParams("child"):setStepNo("1."):setPrefix("1."):setInclude("sub.tslua", { channel = 9 }) })\n',
        encoding="utf-8",
        newline="\n",
    )
    (directory / "sub.tslua").write_text(CHANNEL_SRC, encoding="utf-8", newline="\n")
    host = LuaHost(directory / "main.tslua")
    inst = host.instances[1]
    assert (inst.file, inst.media_number, inst.keyvals) == ("sub.tslua", None, {"channel": 9})
    assert host.tree[1].depth == 1
