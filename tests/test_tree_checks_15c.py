"""Packet 15c seam-1 tests: Step Number prefixes, and the duplicate and cycle checks on the expanded tree.

Real `.atedef` trees and real `.tslua` files on disk, loaded by a `LuaHost` with the release's
include map, scanned by `list_schedule_rows` and run end to end through `HeadlessRunSession`, with
the recorded rows read back. A prefix is prepended as is and prefixes stack outside in; a header's
own number lives in its parent's numbering. A duplicate anywhere in the expanded tree, or a file
twice on one include chain, is a load error for the including top-level schedule only.
"""

import hashlib
import json
import time
from pathlib import Path
from typing import Optional

import pytest

from ate.catalog import find_definitions, include_map, list_schedule_rows, module_map
from ate.engine.lua_host import HostError, LuaHost
from ate.run_session.headless import HeadlessRunSession, SessionError, SessionState
from ate.ui.staged_run_screen import StagedRunScreen


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _release(root: Path, label: str, files: dict[str, str]) -> Path:
    """Write `<label>.atedef` listing every file in `files`, and the files beside it."""
    directory = root / label
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{label}.atedef").write_text(
        json.dumps(
            {
                "format": 1,
                "mediaNumber": f"MR-{label.upper()}",
                "name": f"Release {label}",
                "version": "1.0.0",
                "depends": [],
                "files": [{"path": rel, "sha256": _sha(src)} for rel, src in files.items()],
            }
        ),
        encoding="utf-8",
    )
    for rel, src in files.items():
        (directory / rel).write_text(src, encoding="utf-8")
    return directory


def _host(root: Path, label: str, schedule: str = "main.tslua") -> LuaHost:
    definition = next(d for d in find_definitions(root) if d.media_number == f"MR-{label.upper()}")
    return LuaHost(definition.path / schedule, modules=module_map(definition), includes=include_map(definition))


def _load_error(root: Path, label: str, schedule: str = "main.tslua") -> str:
    with pytest.raises(HostError) as excinfo:
        _host(root, label, schedule)
    return str(excinfo.value)


def _config(tmp_path: Path, root: Path) -> Path:
    config = {
        "format": 1,
        "station": {"name": "BENCH-15", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c11"},
        "defaultProject": "Power board",
        "database": {"type": "sqlite", "path": str(tmp_path / "ate.db"), "storeMismatchedFiles": "always"},
        "reports": {"directory": str(tmp_path / "reports")},
        "testDefinitions": {"directories": [str(root)]},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _finish(qapp, session: HeadlessRunSession) -> str:
    """Pump the event loop until the run is FINISHED with its report written; return the HTML path."""
    written: list[tuple[str, str]] = []
    session.report_written.connect(lambda html, pdf: written.append((html, pdf)))
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if session._state == SessionState.FINISHED and written:
            return written[0][0]
        time.sleep(0.005)
    raise TimeoutError("the run never reached FINISHED with its report written")


def _step(number: str, name: str) -> str:
    return f'StepParams("{name}"):setStepNo("{number}"):setAction(function() ATE.report(Result.PASS, "{name}", "1", "1") end)'


def _header(number: Optional[str], name: str, file: str, prefix: Optional[str] = None) -> str:
    out = f'StepParams("{name}")'
    if number is not None:
        out += f':setStepNo("{number}")'
    if prefix is not None:
        out += f':setPrefix("{prefix}")'
    return out + f':setInclude("{file}")'


def _schedule(name: str, *rows: str) -> str:
    return f'return ScheduleParams("{name}", "15c")\n  :setSchedule({{\n    ' + ",\n    ".join(rows) + ",\n  })\n"


# Three levels of includes, each with its own prefix: main -> a ("3.") -> b ("2.") -> c ("1.").
# Each header's prefix differs from what its own number would get if the prefix applied to it.
STACKED = {
    "main.tslua": _schedule("Stacked main", _step("1.", "setup"), _header("3.", "group A", "a.tslua", prefix="3.")),
    "a.tslua": _schedule("A", _step("1.", "a step"), _header("2.", "group B", "b.tslua", prefix="2.")),
    "b.tslua": _schedule("B", _header("1.", "group C", "c.tslua", prefix="1.")),
    "c.tslua": _schedule("C", _step("1.", "c one"), _step("2.", "c two")),
}
STACKED_NUMBERS = ["1.", "3.", "3.1.", "3.2.", "3.2.1.", "3.2.1.1.", "3.2.1.2."]


# --- numbering ---------------------------------------------------------------


def test_prefixes_stack_outside_in_through_three_levels(tmp_path) -> None:
    """Every step gets each enclosing include's prefix, outermost first, with no separator added."""
    root = tmp_path / "defs"
    _release(root, "stack", STACKED)
    host = _host(root, "stack")
    assert [t.step_no for t in host.tree] == STACKED_NUMBERS
    assert [t.name for t in host.tree][-2:] == ["c one", "c two"]
    # The instance keeps the prefix as given; the stacking is applied to the tree.
    assert [i.prefix for i in host.instances] == ["", "3.", "2.", "1."]


def test_a_header_takes_outer_prefixes_but_not_its_own(tmp_path) -> None:
    """A header's number lives in its parent's numbering: the parents' prefixes apply, its own does not."""
    root = tmp_path / "defs"
    files = {
        "main.tslua": _schedule("Header main", _header("5.", "outer", "mid.tslua", prefix="P.")),
        "mid.tslua": _schedule("Mid", _header("7.", "inner", "leaf.tslua", prefix="Q."), _header(None, "unnumbered", "leaf.tslua", prefix="R.")),
        "leaf.tslua": _schedule("Leaf", _step("1.", "leaf")),
    }
    _release(root, "hdr", files)
    host = _host(root, "hdr")
    assert [(t.name, t.step_no) for t in host.tree] == [
        ("outer", "5."),  # not "P.5."
        ("inner", "P.7."),  # not "P.Q.7."
        ("leaf", "P.Q.1."),
        ("unnumbered", None),  # no Step Number of its own: nothing to prefix
        ("leaf", "P.R.1."),
    ]


def test_a_non_string_prefix_is_a_load_error(tmp_path) -> None:
    root = tmp_path / "defs"
    files = {
        "main.tslua": 'return ScheduleParams("Bad prefix", "")\n'
                      '  :setSchedule({ StepParams("x"):setStepNo("1."):setPrefix(3):setInclude("leaf.tslua") })\n',
        "leaf.tslua": _schedule("Leaf", _step("1.", "leaf")),
    }
    _release(root, "badp", files)
    assert _load_error(root, "badp") == "main.tslua:1.: setPrefix needs a string, got 3"


def test_prefixed_numbers_are_recorded_and_shown_everywhere(qapp, tmp_path) -> None:
    """node.step_number, the Steps stage, the Run view and the HTML report all carry the full number."""
    root = tmp_path / "defs"
    # The deepest step fails (without pausing) so the report's failure list names it.
    files = dict(STACKED)
    files["main.tslua"] = files["main.tslua"].replace("  :setSchedule(", "  :setStopOnFail(false)\n  :setSchedule(")
    files["c.tslua"] = files["c.tslua"].replace('Result.PASS, "c two"', 'Result.FAIL, "c two"')
    _release(root, "stack", files)
    session = HeadlessRunSession(_config(tmp_path, root))
    screen = StagedRunScreen(session)
    screen.uut_operator_stage.operator_edit.setText("Ada")
    screen._advance(1)
    schedules = screen.test_schedule_stage.schedule_list
    item = next(schedules.item(i) for i in range(schedules.count()) if "Stacked main" in schedules.item(i).text())
    schedules.setCurrentItem(item)
    screen._begin_run_from_selection()
    steps = screen.steps_options_stage.step_list
    assert [steps.item(i).text().split()[0] for i in range(steps.count())] == STACKED_NUMBERS
    session.start()
    html = _finish(qapp, session)

    nodes = session.store.nodes(session.run_uuid)
    assert [n["step_number"] for n in nodes] == STACKED_NUMBERS
    attempts = session.store.attempts(session.run_uuid)
    by_seq = {n["node_seq"]: n["step_number"] for n in nodes}
    assert [by_seq[a["node_seq"]] for a in attempts] == ["1.", "3.1.", "3.2.1.1.", "3.2.1.2."]

    rows = [screen.run_view.step_list.item(i).text() for i in range(screen.run_view.step_list.count())]
    assert [r.split()[0] for r in rows] == STACKED_NUMBERS
    assert rows[-1].strip() == "3.2.1.2.  c two  →  FAIL"

    report = Path(html).read_text(encoding="utf-8")
    assert "3.2.1.2." in report and "c two" in report


# --- duplicates --------------------------------------------------------------


def test_a_duplicate_across_branches_is_a_load_error_naming_both_chains(tmp_path) -> None:
    """Two includes with the same prefix collide one level down, in different branches."""
    root = tmp_path / "defs"
    files = {
        "main.tslua": _schedule("Cross main", _header("1.", "x", "x.tslua", prefix="A."), _header("2.", "y", "y.tslua", prefix="A.")),
        "x.tslua": _schedule("X", _step("1.", "x one")),
        "y.tslua": _schedule("Y", _step("2.", "y two"), _step("1.", "y one")),
    }
    _release(root, "cross", files)
    assert _load_error(root, "cross") == (
        "main.tslua: duplicate Step Number 'A.1.' in the expanded tree: "
        "main.tslua:1. → x.tslua:1. and main.tslua:2. → y.tslua:1."
    )


def test_a_duplicate_caused_by_a_missing_prefix_is_a_load_error(tmp_path) -> None:
    """An include without a prefix numbers its steps in the parent's space, so they collide there."""
    root = tmp_path / "defs"
    files = {
        "main.tslua": _schedule("Missing prefix main", _step("1.", "setup"), _header("2.", "sub", "sub.tslua")),
        "sub.tslua": _schedule("Sub", _step("1.", "sub one")),
    }
    _release(root, "nopre", files)
    assert _load_error(root, "nopre") == (
        "main.tslua: duplicate Step Number '1.' in the expanded tree: main.tslua:1. and main.tslua:2. → sub.tslua:1."
    )


def test_a_header_number_is_checked_against_prefixed_steps(tmp_path) -> None:
    """A header's number is in the duplicate check too, against a prefixed step deeper down."""
    root = tmp_path / "defs"
    files = {
        "main.tslua": _schedule("Header dup main", _header("1.", "sub", "sub.tslua", prefix="1."), _header("1.1.", "other", "leaf.tslua", prefix="9.")),
        "sub.tslua": _schedule("Sub", _step("1.", "sub one")),
        "leaf.tslua": _schedule("Leaf", _step("1.", "leaf")),
    }
    _release(root, "hdup", files)
    assert _load_error(root, "hdup") == (
        "main.tslua: duplicate Step Number '1.1.' in the expanded tree: main.tslua:1. → sub.tslua:1. and main.tslua:1.1."
    )


# --- cycles and depth --------------------------------------------------------


def test_a_direct_cycle_fails_fast_naming_the_chain(tmp_path) -> None:
    """A self-include stops at its first repeat, not after some depth limit."""
    root = tmp_path / "defs"
    _release(root, "self", {"main.tslua": _schedule("Self main", _header("1.", "again", "main.tslua", prefix="1."))})
    started = time.monotonic()
    message = _load_error(root, "self")
    assert time.monotonic() - started < 2.0
    assert message == "main.tslua:1.: include 'main.tslua' is an include cycle: main.tslua → main.tslua"


def test_an_indirect_cycle_names_the_chain(tmp_path) -> None:
    """main -> a -> main is a cycle; so is a -> b -> a below a top level that is not on the loop."""
    root = tmp_path / "defs"
    files = {
        "main.tslua": _schedule("Indirect main", _header("1.", "a", "a.tslua", prefix="1.")),
        "a.tslua": _schedule("A", _header("1.", "back", "main.tslua", prefix="1.")),
        "top.tslua": _schedule("Top", _header("1.", "b", "b.tslua", prefix="1.")),
        "b.tslua": _schedule("B", _header("1.", "c", "c.tslua", prefix="1.")),
        "c.tslua": _schedule("C", _header("1.", "back to b", "b.tslua", prefix="1.")),
    }
    _release(root, "ind", files)
    message = _load_error(root, "ind")
    assert message.endswith("is an include cycle: main.tslua → a.tslua → main.tslua")
    assert message.startswith("main.tslua:1.: include 'a.tslua' a.tslua:1.: include 'main.tslua' ")
    assert _load_error(root, "ind", "top.tslua").endswith("is an include cycle: top.tslua → b.tslua → c.tslua → b.tslua")


def test_the_same_file_in_sibling_branches_is_not_a_cycle(tmp_path) -> None:
    """One file included from two branches (and twice at one level) is two instances, not a cycle."""
    root = tmp_path / "defs"
    files = {
        "main.tslua": _schedule("Sibling main", _header("1.", "a", "a.tslua", prefix="1."), _header("2.", "b", "b.tslua", prefix="2."),
                                _header("3.", "leaf again", "leaf.tslua", prefix="3.")),
        "a.tslua": _schedule("A", _header("1.", "leaf", "leaf.tslua", prefix="1.")),
        "b.tslua": _schedule("B", _header("1.", "leaf", "leaf.tslua", prefix="1.")),
        "leaf.tslua": _schedule("Leaf", _step("1.", "leaf")),
    }
    _release(root, "sib", files)
    host = _host(root, "sib")
    assert [i.file for i in host.instances] == ["main.tslua", "a.tslua", "leaf.tslua", "b.tslua", "leaf.tslua", "leaf.tslua"]
    assert [t.step_no for t in host.tree if not t.is_header] == ["1.1.1.", "2.1.1.", "3.1."]


def test_a_forty_level_acyclic_chain_loads(tmp_path) -> None:
    """Nesting has no depth limit: forty distinct files, each including the next, load and number."""
    root = tmp_path / "defs"
    levels = 40
    files = {"main.tslua": _schedule("Deep main", _header("1.", "level 1", "l1.tslua", prefix="1."))}
    for n in range(1, levels):
        files[f"l{n}.tslua"] = _schedule(f"L{n}", _header("1.", f"level {n + 1}", f"l{n + 1}.tslua", prefix="1."))
    files[f"l{levels}.tslua"] = _schedule("Bottom", _step("1.", "bottom"))
    _release(root, "deep", files)
    host = _host(root, "deep")
    bottom = host.tree[-1]
    assert bottom.name == "bottom" and bottom.depth == levels
    assert bottom.step_no == "1." * (levels + 1)
    assert len(host.instances) == levels + 1


def test_a_chain_too_deep_for_python_is_a_load_error_not_a_crash(tmp_path) -> None:
    """Past the interpreter's recursion limit loading raises the schedule's load error, not RecursionError."""
    import sys

    root = tmp_path / "defs"
    levels = 150
    files = {"main.tslua": _schedule("Deep main", _header("1.", "level 1", "l1.tslua", prefix="1."))}
    for n in range(1, levels):
        files[f"l{n}.tslua"] = _schedule(f"L{n}", _header("1.", f"level {n + 1}", f"l{n + 1}.tslua", prefix="1."))
    files[f"l{levels}.tslua"] = _schedule("Bottom", _step("1.", "bottom"))
    _release(root, "deep", files)
    frame, depth = sys._getframe(), 0
    while frame is not None:
        frame, depth = frame.f_back, depth + 1
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(depth + 60)  # room for the loader, not for 150 include levels
    try:
        with pytest.raises(HostError, match="include tree too deep to load"):
            _host(root, "deep")
    finally:
        sys.setrecursionlimit(old)


# --- where the errors surface ------------------------------------------------


def test_a_bad_tree_breaks_only_its_schedule_at_scan_and_run_start(qapp, tmp_path) -> None:
    """The scan shows the cyclic and the duplicating schedules broken while a sibling loads; neither can start."""
    root = tmp_path / "defs"
    files = {
        "main.tslua": _schedule("Cyclic main", _header("1.", "a", "a.tslua", prefix="1.")),
        "a.tslua": _schedule("A", _header("1.", "back", "main.tslua", prefix="1.")),
        "dup.tslua": _schedule("Dup main", _step("1.", "setup"), _header("2.", "leaf", "leaf.tslua")),
        "leaf.tslua": _schedule("Leaf", _step("1.", "leaf")),
        "good.tslua": _schedule("Good main", _step("1.", "setup"), _header("2.", "leaf", "leaf.tslua", prefix="2.")),
    }
    _release(root, "scope", files)
    rows = list_schedule_rows(root)
    errors = sorted(r.error for r in rows if r.error)
    loaded = sorted(r.name for r in rows if r.error is None)
    assert "Good main" in loaded and "Leaf" in loaded
    assert "Cyclic main" not in loaded and "Dup main" not in loaded and "A" not in loaded
    assert any(e.startswith("main.tslua:1.: include 'a.tslua'") and "main.tslua → a.tslua → main.tslua" in e for e in errors)
    assert any(e.startswith("a.tslua:1.: include 'main.tslua'") and "a.tslua → main.tslua → a.tslua" in e for e in errors)
    assert "dup.tslua: duplicate Step Number '1.' in the expanded tree: dup.tslua:1. and dup.tslua:2. → leaf.tslua:1." in errors

    # Run start expands the tree again: a schedule that scanned clean but has since lost its
    # prefix collides with its include, so it can no longer start and no run row is written.
    session = HeadlessRunSession(_config(tmp_path, root))
    session.select_schedule("Good main")
    (root / "scope" / "good.tslua").write_text(
        _schedule("Good main", _step("1.", "setup"), _header("2.", "leaf", "leaf.tslua")), encoding="utf-8")
    with pytest.raises(SessionError, match="'Good main' is not available"):
        session.start()
    assert session.store.query_runs() == []
    with pytest.raises(HostError, match="duplicate Step Number '1.' in the expanded tree"):
        _host(root, "scope", "good.tslua")  # what the run engine's own load would raise
