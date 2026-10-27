"""Packet 15b seam-1 tests: `setInclude`, Sub-schedule instances and the rows they record.

Real `.atedef` trees and real `.tslua` files on disk, expanded by a `LuaHost` that carries the
release's include map, and run end to end through :class:`HeadlessRunSession`. The channel example
from `assets/test-schedule-format.md` is the centre of gravity: one `channel.tslua` included twice
with different `keyvals`, each include its own instance. Cycle detection, Step Number prefixes and
`setEnable` forcing belong to packets 15c/15d; here a runaway include chain is stopped at depth 32.
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
    StepParams("channel 1"):setStepNo("2."):setInclude("channel.tslua", { channel = 1 }),
    StepParams("channel 2"):setStepNo("3."):setInclude("channel.tslua", { channel = 2 }),
  })
"""

# A `require`d module is shared by the host, so its state carries across both instances.
COUNTER_SRC = "local n = 0\nreturn { bump = function() n = n + 1 return n end }\n"

COUNTING_CHANNEL_SRC = """\
local kv = ...
local counter = require("counter")
local n = counter.bump()
return ScheduleParams("Counting channel", "counts how many times this chunk ran")
  :setSchedule({
    StepParams("count"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "run " .. n, tostring(n), tostring(n)) end),
  })
"""

# Two levels of nesting: main -> group -> channel.
GROUP_SRC = """\
local kv = ...
return ScheduleParams("Group schedule", "one channel instance per group member")
  :setSchedule({
    StepParams("inner"):setStepNo("1."):setInclude("channel.tslua", { channel = kv.channel }),
  })
"""

NESTED_MAIN_SRC = """\
return ScheduleParams("Nested schedule", "a Sub-schedule that includes another Sub-schedule")
  :setSchedule({
    StepParams("group 1"):setStepNo("1."):setInclude("group.tslua", { channel = 7 }),
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
    )
    return release_dir


def _write_files(directory: Path, files: dict[str, str]) -> None:
    for rel, src in files.items():
        target = directory / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(src, encoding="utf-8")


def _config(tmp_path: Path, root: Path) -> Path:
    config = {
        "format": 1,
        "station": {"name": "BENCH-15", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c11"},
        "database": {"type": "sqlite", "path": str(tmp_path / "ate.db"), "storeMismatchedFiles": "ask"},
        "reports": {"directory": str(tmp_path / "reports")},
        "testDefinitions": {"directories": [str(root)]},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
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
            0, header_node, "Channel check", "channel.tslua", "MR-MAIN", "", {"channel": seq}
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
        ATE.report(Result.PASS, "n=" .. (kv.n or 0) .. " tag=" .. (inner.tag or "-"), "1", "1")
      end),
  })
"""
    main = """\
return ScheduleParams("Keyvals main", "passes nested keyvals")
  :setSchedule({ StepParams("child"):setStepNo("1."):setInclude("child.tslua", { n = 3, settings = { tag = "abc" } }) })
"""
    directory = _write_release(root, "kv", [_entry("main.tslua", main), _entry("child.tslua", src)])
    _write_files(directory, {"main.tslua": main, "child.tslua": src})
    host = _host(root, "kv")
    assert host.instances[1].keyvals == {"n": 3, "settings": {"tag": "abc"}}


def test_a_require_d_module_is_shared_between_instances(tmp_path) -> None:
    """The chunk runs again per include, but `require`d module state carries across them."""
    root = tmp_path / "defs"
    main = """\
return ScheduleParams("Shared module schedule", "two includes of the counting channel")
  :setSchedule({
    StepParams("first"):setStepNo("1."):setInclude("channel.tslua", { channel = 1 }),
    StepParams("second"):setStepNo("2."):setInclude("channel.tslua", { channel = 2 }),
  })
"""
    files = {"main.tslua": main, "channel.tslua": COUNTING_CHANNEL_SRC, "counter.lua": COUNTER_SRC}
    directory = _write_release(root, "shared", [_entry(p, s) for p, s in files.items()])
    _write_files(directory, files)
    host = _host(root, "shared")
    assert [len(inst.keyvals) for inst in host.instances] == [0, 1, 1]
    assert [t.step_no for t in host.tree] == ["1.", "1.", "2.", "1."]  # each instance numbers its own steps


# --- resolution through the Test Definition --------------------------------


def test_an_include_is_resolved_from_a_dependency_release(tmp_path) -> None:
    """A Sub-schedule listed only by a dependency is found, and its release owns the instance."""
    root = tmp_path / "defs"
    main = """\
return ScheduleParams("Dependency main", "includes a Sub-schedule owned by a dependency")
  :setSchedule({ StepParams("dep"):setStepNo("1."):setInclude("sub.tslua", { channel = 5 }) })
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
  :setSchedule({ StepParams("deep"):setStepNo("1."):setInclude("sub.tslua") })
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
            0, (1, 4)[seq - 1], "Channel check", "channel.tslua", "MR-MAIN", ""
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
    StepParams("first"):setStepNo("1."):setInclude("channel.tslua", { channel = 1 }),
    StepParams("second"):setStepNo("2."):setInclude("channel.tslua", { channel = 2 }),
  })
"""
    files = {"main.tslua": main, "channel.tslua": COUNTING_CHANNEL_SRC, "counter.lua": COUNTER_SRC}
    directory = _write_release(root, "shared", [_entry(p, s) for p, s in files.items()])
    _write_files(directory, files)
    session = HeadlessRunSession(_config(tmp_path, root))
    session.select_schedule("Shared module schedule")
    session.start()
    assert _finish(qapp, session)
    assert [a["msg"] for a in _attempts(session)] == ["run 1", "run 2"]
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
  :setSchedule({ StepParams("sub"):setStepNo("1."):setInclude("sub.tslua") })
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
  :setSchedule({ StepParams("x"):setStepNo("1."):setInclude("nowhere.tslua") })
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
        ("nowhere.tslua", None, None, "include 'nowhere.tslua' is not listed in the Test Definition or its dependencies"),
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
        f'  :setSchedule({{ StepParams("x"):setStepNo("1."):setInclude("{include_file}"{keyvals_src}) }})\n'
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


def test_an_include_listed_but_absent_on_disk_is_a_load_error(tmp_path) -> None:
    """The release lists the Sub-schedule but the file has gone: a load error, not a silent skip.

    The catalog's own validation rejects a `.atedef` entry whose file is missing, so this case is
    reached with a hand-built definition (the same seam packet 07b uses for the search order)."""
    from ate.catalog import Definition, FileEntry

    directory = tmp_path / "own"
    directory.mkdir()
    (directory / "main.tslua").write_text(
        'return ScheduleParams("Main schedule", "includes a file that is not on disk")\n'
        '  :setSchedule({ StepParams("x"):setStepNo("1."):setInclude("sub.tslua") })\n',
        encoding="utf-8",
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


def test_an_ambiguous_include_is_a_load_error(tmp_path) -> None:
    """A Sub-schedule listed by the release and by a dependency is ambiguous (spec: an error)."""
    root = tmp_path / "defs"
    main = """\
return ScheduleParams("Main schedule", "includes a file the release and a dependency both list")
  :setSchedule({ StepParams("x"):setStepNo("1."):setInclude("sub.tslua") })
"""
    _write_release(root, "amb", [_entry("main.tslua", main), _entry("sub.tslua", CHANNEL_SRC)],
                  [{"name": "Release dep", "version": "^1.0"}])
    _write_files(root / "amb", {"main.tslua": main, "sub.tslua": CHANNEL_SRC})
    dep = _write_release(root, "dep", [_entry("sub.tslua", CHANNEL_SRC)])
    _write_files(dep, {"sub.tslua": CHANNEL_SRC})
    message = _error_of(root, "amb")
    assert "ambiguous include 'sub.tslua'" in message
    assert "MR-AMB" in message and "MR-DEP" in message


def test_a_function_in_keyvals_is_a_load_error(tmp_path) -> None:
    """`keyvals` is plain data only: a function, nested anywhere, is a load error naming the header."""
    root = tmp_path / "defs"
    for keyvals in ("{ f = function() end }", "{ nested = { f = function() end } }", "{ x = StepParams(\"y\") }"):
        main = (
            'return ScheduleParams("Main schedule", "keyvals hold a function")\n'
            f'  :setSchedule({{ StepParams("x"):setStepNo("1."):setInclude("channel.tslua", {keyvals}) }})\n'
        )
        directory = _write_release(root, "kv", [_entry("main.tslua", main), _entry("channel.tslua", CHANNEL_SRC)])
        _write_files(directory, {"main.tslua": main, "channel.tslua": CHANNEL_SRC})
        message = _error_of(root, "kv")
        assert "keyvals must be plain data" in message
        assert "main.tslua:1.:" in message


def test_a_self_include_hits_the_depth_limit(tmp_path) -> None:
    """Until 15c detects cycles properly, a chain that recurses stops at depth 32 with a load error."""
    root = tmp_path / "defs"
    src = """\
return ScheduleParams("Looping schedule", "includes itself")
  :setSchedule({ StepParams("again"):setStepNo("1."):setInclude("loop.tslua") })
"""
    directory = _write_release(root, "loop", [_entry("loop.tslua", src)])
    _write_files(directory, {"loop.tslua": src})
    includes = include_map(_definition(root, "loop"))
    with pytest.raises(HostError) as excinfo:
        host = LuaHost(directory / "loop.tslua", modules={}, includes=includes)
    message = str(excinfo.value)
    assert "include depth exceeds 32" in message
    # The chain prefix names each level the walk reached before the limit stopped it: 32 legal
    # include levels, then the 33rd that raises.
    assert message.count("include 'loop.tslua'") == 33


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
        "  1.  output on",
        "  2.  output off",
        "3.  channel 2",
        "  1.  output on",
        "  2.  output off",
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
    # Headers are not Test Steps, so the finished list carries the five steps of the expanded tree.
    assert len(rows) == 5
    assert "output off" in rows[2] and "output on" in rows[1]


def test_bare_host_resolves_includes_against_its_own_directory(tmp_path) -> None:
    """Without a Test Definition the include is read from the schedule's directory (a test seam)."""
    directory = tmp_path / "sched"
    directory.mkdir()
    (directory / "main.tslua").write_text(
        'return ScheduleParams("Bare main", "no definition behind it")\n'
        '  :setSchedule({ StepParams("child"):setStepNo("1."):setInclude("sub.tslua", { channel = 9 }) })\n',
        encoding="utf-8",
    )
    (directory / "sub.tslua").write_text(CHANNEL_SRC, encoding="utf-8")
    host = LuaHost(directory / "main.tslua")
    inst = host.instances[1]
    assert (inst.file, inst.media_number, inst.keyvals) == ("sub.tslua", None, {"channel": 9})
    assert host.tree[1].depth == 1
