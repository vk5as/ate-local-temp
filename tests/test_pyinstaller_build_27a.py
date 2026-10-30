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


def test_the_frozen_smoke_insists_on_a_qt_platform_plugin(tmp_path: Path) -> None:
    """A bundle with the WebEngine helper and no platform plugin has no Qt at all.

    PyInstaller's Qt hooks only collect the plugins when PyQt6 can be imported in the build
    environment; in a container without libGL/libglib the import fails, the hooks are skipped, and
    the bundle ships `QtWebEngineProcess` and its .pak files while `_pdf` dies with "Could not
    find the Qt platform plugin". Checking the helper alone is not enough: the smoke check has to
    fail on the missing plugin, and pass when one is present. The same check wants the WebEngine
    resources it is easy to ship without: `icudtl.dat` and the `.pak` files, since the renderer cannot
    build a page without them.
    """
    bundle = tmp_path / "ate"
    bundle.mkdir()
    internal = bundle / "_internal"
    exe = bundle / "ate"
    exe.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo 1.2.3; exit 0; fi\n'
        'if [ "$1" = "_pdf" ]; then printf "%%PDF-1.4 words\\n" > "$3"; exit 0; fi\n'
        "echo 'selftest ok'\nexit 0\n",
        encoding="utf-8",
    )
    exe.chmod(0o755)
    (internal / "ate" / "lua").mkdir(parents=True)
    (internal / "ate" / "lua" / "devices.lua").write_text("-- lua", encoding="utf-8")
    (internal / "ate" / "report" / "templates").mkdir(parents=True)
    (internal / "ate" / "report" / "templates" / "report.html.j2").write_text("{{ report }}", encoding="utf-8")
    (internal / "PyQt6" / "Qt6" / "libexec").mkdir(parents=True)
    (internal / "PyQt6" / "Qt6" / "libexec" / "QtWebEngineProcess").write_bytes(b"ELF")
    (internal / "PyQt6" / "Qt6" / "resources").mkdir(parents=True)
    (internal / "PyQt6" / "Qt6" / "resources" / "qtwebengine_resources.pak").write_bytes(b"pak")
    (internal / "PyQt6" / "Qt6" / "resources" / "icudtl.dat").write_bytes(b"icu")

    build = _load_module(BUILD_SCRIPT, "build_pyinstaller")
    with pytest.raises(SystemExit) as caught:
        build._run_smoke(exe, "1.2.3")
    assert "platform plugin" in str(caught.value), f"the smoke check only looked at the helper: {caught.value}"

    plugin = internal / "PyQt6" / "Qt6" / "plugins" / "platforms"
    plugin.mkdir(parents=True)
    (plugin / "libqoffscreen.so").write_bytes(b"ELF")
    lines = build._run_smoke(exe, "1.2.3")
    assert any("platform plugin" in line for line in lines), lines


def test_the_size_log_counts_a_link_once(tmp_path: Path) -> None:
    """The logged bundle size is the bytes it holds, not the bytes behind every link.

    ``_size`` must not follow a symlink and must not count the same inode twice: with ``stat()``
    this temporary tree read as 2 KB in three files, while it is one 1 KB file.
    """
    build = _load_module(BUILD_SCRIPT, "build_pyinstaller")
    real = tmp_path / "real.bin"
    real.write_bytes(b"x" * 1024)
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "link.bin").symlink_to(real)
    try:
        (nested / "hard.bin").hardlink_to(real)
    except OSError:
        pass  # a filesystem with no hard links: the symlink alone is the point
    total, files = build._size(tmp_path)
    assert total == 1024 and files == 1, f"counted {files} entries and {total} bytes for one 1 KB file"


def test_the_frozen_helper_search_covers_both_platform_layouts(tmp_path: Path) -> None:
    """PyQt6 keeps QtWebEngineProcess in Qt6/libexec on Linux and Qt6/bin on Windows.

    The entry point points Qt at the bundled helper, so it has to find it in the layout the hooks
    collect into on the platform the bundle is built for — and in any other layout a Qt build
    happens to use.
    """
    entry = _load_module(ENTRY, "ate_entry")
    for index, rel in enumerate(
        (
            ("PyQt6", "Qt6", "bin", "QtWebEngineProcess.exe"),  # PyQt6 wheel, Windows
            ("PyQt6", "Qt6", "libexec", "QtWebEngineProcess"),  # PyQt6 wheel, Linux
            ("PyQt6", "Qt", "libexec", "QtWebEngineProcess"),  # PyQt6 before the Qt6 layout
        )
    ):
        base = tmp_path / f"bundle{index}"
        helper = base.joinpath(*rel)
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_bytes(b"ELF")
        found = entry._webengine_process(str(base), rel[-1])
        assert found == str(helper), f"the helper at {helper} was not found"

    # Anywhere else the helper is: still found.
    odd = tmp_path / "odd" / "deep" / "nest"
    odd.mkdir(parents=True)
    helper = odd / "QtWebEngineProcess"
    helper.write_bytes(b"ELF")
    assert entry._webengine_process(str(tmp_path / "odd"), "QtWebEngineProcess") == str(helper)
    assert entry._webengine_process(str(tmp_path / "nothing"), "QtWebEngineProcess") is None


def test_the_gpu_flags_do_not_replace_the_callers(monkeypatch) -> None:
    """``--no-gpu`` is added to Chromium's flags, never written over them."""
    entry = _load_module(ENTRY, "ate_entry")
    monkeypatch.setenv("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-process-per-site")
    flags = entry._chromium_flags().split()
    assert "--disable-process-per-site" in flags, f"the caller's flag was dropped: {flags}"
    assert "--no-gpu" in flags and "--disable-gpu" in flags, flags
    assert flags.count("--no-gpu") == 1, f"flags repeated: {flags}"


def test_the_build_venv_interpreter_is_the_platform_one(monkeypatch, tmp_path: Path) -> None:
    """A Windows venv keeps its interpreter in ``Scripts``, not ``bin``."""
    build = _load_module(BUILD_SCRIPT, "build_pyinstaller")
    venv = tmp_path / "venv"
    monkeypatch.setattr(os, "name", "nt")
    assert build._python_in(venv) == venv / "Scripts" / "python.exe"
    monkeypatch.setattr(os, "name", "posix")
    assert build._python_in(venv) == venv / "bin" / "python"


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


def test_the_spec_excludes_qt_bindings_ate_never_imports() -> None:
    """The spec drops the Qt modules ATE never touches, and nothing in ATE imports them.

    QtQuick/QML, Qt3D, QtMultimedia and QtBluetooth are the heavy ones: 89 MB of the bundle
    (2688 files → 489.6 MB vs 578.8 MB). The shared libraries QtWebEngine is *linked* against
    are still collected by PyInstaller's own dependency walk, so what goes away is only the
    binding the hooks would have pulled in. If ATE ever imports one of these, the exclusion has
    to go with it — hence the second half of this test.
    """
    spec = _executed_spec()
    excluded = set(spec["excludes"])
    expected = {
        "PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.QtQuickWidgets", "PyQt6.QtQuick3D",
        "PyQt6.Qt3DCore", "PyQt6.Qt3DRender", "PyQt6.QtMultimedia", "PyQt6.QtMultimediaWidgets",
        "PyQt6.QtBluetooth",
    }
    missing = sorted(expected - excluded)
    assert not missing, f"the spec stopped excluding {missing}"

    used = set()
    for source in (REPO / "src" / "ate").rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("PyQt6."):
                used.add(node.module)
            elif isinstance(node, ast.Import):
                used.update(alias.name for alias in node.names if alias.name.startswith("PyQt6."))
    clash = sorted(used & excluded)
    assert not clash, f"ATE imports {clash}, which the spec excludes"


# Build scratch that must not be left in the checkout by a run.
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
