"""Packet 27a: the PyInstaller one-directory build.

Two kinds of test here:

* Fast, always on: the spec file itself. It is executed with stubs for PyInstaller's
  ``Analysis``/``PYZ``/``Executable`` so its ``datas`` and ``hiddenimports`` can be read
  without building anything — that catches a resource or a backend quietly dropped from the
  bundle long before CI builds it.
* Fast, always on: the entry point and the build script, exercised directly (a fake bundle
  directory, a temporary directory of files) so the platform-specific parts that cannot be
  built here are still tested.
* Slow and off by default: the real build. ``ATE_BUILD_TESTS=1`` runs
  ``packaging/build_pyinstaller.py --smoke``, which makes a throwaway venv with the current
  CPython (3.13 here, 3.14 in the release CI), builds ``dist/ate``, and checks that the
  frozen executable prints its version, renders a page to PDF through QWebEngine, and
  runs a built-in Test Schedule with ``ate _selftest --run``.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SPEC = REPO / "packaging" / "pyinstaller" / "ate.spec"
ENTRY = REPO / "packaging" / "pyinstaller" / "ate_entry.py"
BUILD_SCRIPT = REPO / "packaging" / "build_pyinstaller.py"

# What a frozen bundle must carry, whatever platform or CPython builds it.
REQUIRED_HIDDEN = {
    "PyQt6.QtWebEngineCore",
    "PyQt6.QtWebEngineWidgets",
    "lupa.lua55",
    "pyvisa_py",
    "pyvisa_py.tcpip",
    "pyvisa_py.serial",
}
REQUIRED_DATA = {"ate/lua/devices.lua", "ate/report/templates/report.html.j2"}


def _executed_spec() -> dict:
    """Run the spec file with PyInstaller's names stubbed, so its data and imports are readable."""
    if not SPEC.is_file():
        pytest.skip("packaging/pyinstaller/ate.spec is missing")

    class _Stub:
        """Anything PyInstaller would return: attribute access and calls both work."""

        def __call__(self, *args, **kwargs):
            return self

        def __getattr__(self, name):
            return self

    namespace = {"SPECPATH": str(SPEC.parent)}
    for name in ("Analysis", "PYZ", "EXE", "COLLECT"):
        namespace[name] = _Stub()
    exec(SPEC.read_text(encoding="utf-8"), namespace)  # noqa: S102 - the spec is our own file
    return namespace


def test_the_spec_bundles_ate_runtime_data() -> None:
    """The report template and the bundled Lua modules are in the bundle."""
    spec = _executed_spec()
    # PyInstaller drops each data file into its destination directory.
    bundled = {str(Path(dest) / Path(src).name) for src, dest in spec["datas"]}
    missing = sorted(REQUIRED_DATA - bundled)
    assert not missing, f"the spec does not bundle {missing}; datas = {sorted(bundled)}"
    # The sweep is what keeps a newly added resource in the bundle without editing the spec.
    assert any(target.startswith("ate/") for target in bundled), "the spec stopped sweeping src/ate"


def test_the_spec_bundles_qtwebengine_and_names_the_helper() -> None:
    """QtWebEngine is collected by PyInstaller's Qt hooks; the entry points at its helper.

    The bindings are only reached through the PDF helper and the GUI, so the spec names them
    explicitly, and the stub the frozen executable runs tells Qt where the bundled
    ``QtWebEngineProcess`` lives.
    """
    text = SPEC.read_text(encoding="utf-8")
    assert "PyQt6.QtWebEngineCore" in text and "PyQt6.QtWebEngineWidgets" in text
    entry = ENTRY.read_text(encoding="utf-8")
    assert "QtWebEngineProcess" in entry and "QTWEBENGINEPROCESS_PATH" in entry


def test_the_spec_declares_the_dynamic_backends() -> None:
    """lupa, pyvisa-py and WebEngine are named: none of them is reachable through an import."""
    tree = ast.parse(SPEC.read_text(encoding="utf-8"))
    assigned = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            assigned[node.targets[0].id] = node.value
    hidden = {item.value for item in getattr(assigned.get("hiddenimports", ast.List()), "elts", [])}
    missing = sorted(REQUIRED_HIDDEN - hidden)
    assert not missing, f"hiddenimports is missing {missing}"


def test_the_entry_point_fixes_up_the_frozen_environment() -> None:
    """The stub PyInstaller runs points Qt at the bundled WebEngine helper."""
    text = ENTRY.read_text(encoding="utf-8")
    assert "frozen" in text and "QTWEBENGINEPROCESS_PATH" in text
    assert "from ate.cli import main" in text, "the entry point must reach the same CLI as the wheel"


def test_the_frozen_pdf_command_is_the_executable_itself() -> None:
    """A frozen app has no ``-m ate``: the helper child is the bundle's own executable."""
    import sys as _sys

    from ate.report.pdf import pdf_helper_command

    original = getattr(_sys, "frozen", None)
    _sys.frozen = True
    try:
        program, args, _env = pdf_helper_command("report.html", "report.pdf")
    finally:
        if original is None:
            del _sys.frozen
        else:
            _sys.frozen = original
    assert args[0] == "_pdf", f"the helper takes the subcommand directly, got {args}"
    assert program == _sys.executable, "a frozen helper is the bundle's own executable, not python -m ate"


def test_selftest_reports_what_the_source_tree_carries(capsys) -> None:
    """``ate _selftest`` runs outside a bundle too: the same inventory, exit 0 on a full tree."""
    from ate.cli import main

    assert main(["_selftest"]) == 0
    out = capsys.readouterr().out
    for name in ("report template", "bundled Lua", "lupa", "pyvisa-py", "QtWebEngine"):
        assert f"{name}: ok" in out, out


def _load_module(path: Path, name: str):
    """Import a file that is not an installed package: the build script, the spec's entry point."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_ticked_box_claims_only_what_was_built_here() -> None:
    """27a's checkbox describes the recipe, not a CPython 3.14 bundle nothing here builds.

    The packet was reviewed for overclaiming: the box said "bundling CPython 3.14" and was ticked,
    while the only build run in this repository is CPython 3.13. The 3.14 bundle is built and
    smoke-tested by release CI, which is packet 27c.
    """
    issues = REPO / ".scratch" / "ate-v1" / "issues"
    packet = (issues / "27a-pyinstaller-build.md").read_text(encoding="utf-8")
    box = next((line for line in packet.splitlines() if line.startswith("- [x]")), "")
    assert "3.14" not in box, f"the ticked box claims a CPython 3.14 bundle: {box}"
    assert "27c" in packet, "27a must say who builds the 3.14 bundle"

    ci = (issues / "27c-windows-release-ci.md").read_text(encoding="utf-8")
    assert "build_pyinstaller.py --smoke" in ci and "3.14" in ci, "27c must say CI builds the 3.14 bundle with the 27a recipe"
    assert not any(line.startswith("- [x]") and "build_pyinstaller" in line for line in ci.splitlines()), "the line belongs in 27c's body, not a checkbox"


def test_selftest_run_executes_a_schedule_and_writes_both_reports(capsys) -> None:
    """``_selftest --run`` runs a built-in Test Schedule and proves both reports were written.

    The PDF comes from the hidden ``_pdf`` child — the bundle's own executable when frozen — so this
    is the end-to-end proof a frozen ATE can run a Test Run, not just import its modules.
    """
    from ate.cli import main

    assert main(["_selftest", "--run"]) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "run end-to-end: ok" in out, out
    assert "bytes HTML" in out and "bytes PDF" in out, out


def _repo_build_state() -> list[str]:
    """dist/, build/ and any spec scratch in the checkout, as paths."""
    junk = [REPO / "dist", REPO / "build"]
    return sorted(p.relative_to(REPO).as_posix() for p in junk if p.exists())


# The build takes minutes and needs the network for a fresh venv, so it is off unless asked for.
@pytest.mark.build
@pytest.mark.slow
def test_the_frozen_build_builds_and_smoke_tests() -> None:
    """Build dist/ate and run the four smoke checks against the real executable."""
    if os.environ.get("ATE_BUILD_TESTS") != "1":
        pytest.skip("the frozen build takes minutes; set ATE_BUILD_TESTS=1 to run it")

    scratch = Path(tempfile.mkdtemp(prefix="ate-frozen-build-"))
    dist, work = scratch / "dist", scratch / "work"
    before = _repo_build_state()
    try:
        result = subprocess.run(
            [sys.executable, str(BUILD_SCRIPT), "--dist", str(dist), "--work", str(work), "--smoke"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=3600,
        )
        assert result.returncode == 0, f"build failed: {result.stderr[-3000:]}"
        for marker in ("smoke ok: version:", "smoke ok: pdf:", "smoke ok: selftest:", "smoke ok: webengine assets:"):
            assert marker in result.stdout, f"{marker} missing from the build output:\n{result.stdout[-2000:]}"
        assert "run end-to-end: ok" in result.stdout, "the frozen bundle did not run a Test Schedule end to end"
        assert "MISSING" not in result.stdout, f"the frozen bundle is short something:\n{result.stdout[-2000:]}"
        assert (dist / "ate" / ("ate.exe" if os.name == "nt" else "ate")).is_file()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    # The build writes only under the work/dist directories it was given: dist/ and build/
    # are gitignored and must not be created in the checkout by a test run.
    assert before == _repo_build_state(), "the build left output in the checkout"
