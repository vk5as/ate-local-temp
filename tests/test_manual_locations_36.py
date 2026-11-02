"""Packet 36, part 2: the PDFs ride in every package, and `ate --help` says where they are.

Nothing here needs TeX, Docker or a runner. Three things are proved:

* **one staged directory.** `docs/manuals/build.py` writes the PDFs into `src/ate/manuals`, which
  `.gitignore` keeps out of the repository, and the two rules that collect it are the PyInstaller
  spec's sweep of every non-`.py` file under `src/ate` and `[tool.setuptools.package-data]`'s
  `*.pdf`. A new manual needs no packaging change; a manual not in the list fails the build.
* **a release build refuses a bundle without them.** `verify_bundle()` fails on a missing or empty
  PDF when `ATE_RELEASE_BUILD=1`, the same way it fails on a bundle without the licence notices. A
  plain source build has no PDFs, says so plainly, and is not a release.
* **the path `--help` prints is the running install's own.** Each layout below is real: a copied
  package in a site-packages directory, a one-directory bundle with `_internal`, this checkout. The
  program is run, the line is read back with `packaging/manuals.py`, and the files named there are
  opened.

`src/ate/manuals.py` is the code behind that line; `packaging/manuals.py` is the packaging side that
reads the same PDFs. Both are stdlib only, so every test here runs on Linux and on Windows.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
SPEC = REPO / "packaging" / "pyinstaller" / "ate.spec"
GITIGNORE = REPO / ".gitignore"
PYPROJECT = REPO / "pyproject.toml"
MANUALS_BUILD = REPO / "docs" / "manuals" / "build.py"
MANUALS_HELP = REPO / "packaging" / "manuals.py"
BUILD_PYINSTALLER = REPO / "packaging" / "build_pyinstaller.py"
MANUALS_MODULE = SRC / "ate" / "manuals.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = _load("manuals_build_for_locations", MANUALS_BUILD)
helpers = _load("packaging_manuals_for_locations", MANUALS_HELP)
pyinstaller = _load("build_pyinstaller_for_locations", BUILD_PYINSTALLER)

PDF_NAMES = build.MANUAL_PDFS


def _pdf(name: str, version: str = "1.2.3") -> bytes:
    from tests._fixtures import fake_manual_pdf

    return fake_manual_pdf(f"{name.removesuffix('.pdf')} manual, ATE {version}")


def _write_pdfs(directory: Path, version: str = "1.2.3") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in PDF_NAMES:
        (directory / name).write_bytes(_pdf(name, version))


def _run(script: str, env: dict[str, str]) -> str:
    """Run one Python process with this script and return its stdout."""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
        timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(f"the subprocess failed ({proc.returncode}): {proc.stdout}\n{proc.stderr}")
    return proc.stdout


# --- the staged directory, and what collects it -----------------------------------------------------


def test_build_py_stages_into_the_directory_git_ignores():
    stage = build.STAGE_DIR
    assert stage == REPO / "src" / "ate" / "manuals", f"the stage directory is {stage}, not the one the packaging reads"
    proc = subprocess.run(
        ["git", "check-ignore", "-q", str(stage / "admin.pdf")],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"git would commit a built PDF: {stage} is not ignored by .gitignore"
    assert "src/ate/manuals/" in GITIGNORE.read_text(encoding="utf-8"), "the ignore rule is what makes the stage a stage"


def test_the_two_collection_rules_cover_a_pdf_in_that_directory():
    """PyInstaller sweeps `src/ate`; setuptools is told to take `*.pdf`. One list, one place."""
    spec_text = SPEC.read_text(encoding="utf-8")
    assert "(SRC / \"ate\").rglob(\"*\")" in spec_text, "the spec derives its data files from the tree, so a staged PDF is collected without a change"
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    assert '"*.pdf"' in pyproject, "the wheel only carries the PDFs if package-data says so"
    assert (SRC / "ate").is_dir() and (build.STAGE_DIR.parent == SRC / "ate"), build.STAGE_DIR


def test_a_new_manual_cannot_be_left_out_of_the_shipped_files():
    """The names the artifacts carry are the names `build.py` builds, and a directory off that list fails."""
    assert build.MANUAL_PDFS == tuple(f"{name}.pdf" for name in build.MANUAL_NAMES)
    assert helpers.manual_pdf_names() == build.MANUAL_PDFS, "packaging reads the same list, not a copy of it"
    assert build.manuals_left_out() == [], f"manual directories the release would not ship: {build.manuals_left_out()}"


# --- verify_bundle: a release build without a manual is not a release ---------------------------


def _stub_bundle(root: Path, manuals: str = "full", version: str = "1.2.3") -> Path:
    """A bundle shaped like `dist/ate`, with everything `verify_bundle` checks for except the manuals.

    `manuals` is the way the staged PDFs are broken: "full", "missing", "empty", "not-pdf", "none"."""
    from tests._fixtures import stub_licence_notices

    bundle = root / "dist" / "ate"
    internal = bundle / "_internal"
    for directory in (
        internal / "PySide6" / "Qt" / "libexec",
        internal / "PySide6" / "Qt" / "resources",
        internal / "PySide6" / "Qt" / "plugins" / "platforms",
        internal / "ate" / "lua",
        internal / "ate" / "report" / "templates",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    exe = bundle / "ate"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    exe.chmod(0o755)
    (internal / "PySide6" / "Qt" / "plugins" / "platforms" / "libqoffscreen.so").write_text("stub", encoding="utf-8", newline="\n")
    (internal / "PySide6" / "Qt" / "libexec" / "QtWebEngineProcess").write_text("stub", encoding="utf-8", newline="\n")
    (internal / "PySide6" / "Qt" / "resources" / "icudtl.dat").write_text("stub", encoding="utf-8", newline="\n")
    (internal / "PySide6" / "Qt" / "resources" / "qtwebengine_resources.pak").write_text("stub", encoding="utf-8", newline="\n")
    (internal / "ate" / "lua" / "devices.lua").write_text("-- bundled", encoding="utf-8", newline="\n")
    (internal / "ate" / "report" / "templates" / "report.html.j2").write_text("<html>", encoding="utf-8", newline="\n")
    stub_licence_notices(internal)

    staged = internal / "ate" / "manuals"
    if manuals != "none":
        _write_pdfs(staged, version)
    if manuals == "missing":
        (staged / PDF_NAMES[0]).unlink()
    elif manuals == "empty":
        (staged / PDF_NAMES[1]).write_bytes(b"")
    elif manuals == "not-pdf":
        (staged / PDF_NAMES[1]).write_bytes(b"<!DOCTYPE html>\n")
    return bundle


def test_a_release_build_refuses_a_bundle_with_a_manual_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("ATE_RELEASE_BUILD", "1")
    exe = _stub_bundle(tmp_path, manuals="missing") / "ate"
    with pytest.raises(SystemExit) as exc:
        pyinstaller.verify_bundle(exe)
    message = str(exc.value)
    assert "test-writer.pdf" in message and "ATE_RELEASE_BUILD" in message, message
    assert "python docs/manuals/build.py" in message, "the fix the message gives is the one that works"


def test_a_release_build_refuses_a_manual_that_is_present_but_not_a_pdf(tmp_path, monkeypatch):
    monkeypatch.setenv("ATE_RELEASE_BUILD", "1")
    exe = _stub_bundle(tmp_path, manuals="empty") / "ate"
    with pytest.raises(SystemExit) as exc:
        pyinstaller.verify_bundle(exe)
    assert "not a PDF" in str(exc.value), exc.value

    exe = _stub_bundle(tmp_path, manuals="not-pdf") / "ate"
    with pytest.raises(SystemExit) as exc2:
        pyinstaller.verify_bundle(exe)
    assert "not a PDF" in str(exc2.value), exc2.value


def test_a_release_build_refuses_a_bundle_with_no_manuals_at_all(tmp_path, monkeypatch):
    monkeypatch.setenv("ATE_RELEASE_BUILD", "1")
    exe = _stub_bundle(tmp_path, manuals="none") / "ate"
    with pytest.raises(SystemExit) as exc:
        pyinstaller.verify_bundle(exe)
    assert "no manual PDFs" in str(exc.value) or "no manuals under" in str(exc.value), exc.value


def test_a_source_build_without_manuals_carries_on_and_says_so(tmp_path, monkeypatch):
    """A build from a plain checkout has no PDFs and is not a release: it is allowed to continue."""
    monkeypatch.delenv("ATE_RELEASE_BUILD", raising=False)
    exe = _stub_bundle(tmp_path, manuals="none") / "ate"
    lines = pyinstaller.verify_bundle(exe)
    assert any(line.startswith("manuals: none") for line in lines), lines
    assert any("source build" in line for line in lines), lines
    assert not any("manuals: 3" in line for line in lines), lines


def test_a_bundle_with_every_manual_reports_them(tmp_path, monkeypatch):
    monkeypatch.setenv("ATE_RELEASE_BUILD", "1")
    exe = _stub_bundle(tmp_path, manuals="full") / "ate"
    lines = pyinstaller.verify_bundle(exe)
    line = next(line for line in lines if line.startswith("manuals:"))
    assert line.startswith("manuals: 3 PDFs"), line
    assert "test-writer.pdf" in line and "admin.pdf" in line and "developer.pdf" in line, line


# --- where an install keeps them: three real layouts --------------------------------------------


def test_a_source_checkout_names_src_ate_manuals():
    """Run `ate --help` from this checkout: the epilog names the directory the build stages into."""
    out = _run("import sys; sys.path.insert(0, r'%s'); from ate.cli import main; sys.exit(main(['--help']))" % str(SRC), dict(os.environ, PYTHONPATH=str(SRC)))
    last = out.strip().splitlines()[-1]
    assert last.startswith("manuals:"), last
    expected = REPO / "src" / "ate" / "manuals"
    on_disk = sorted(p.name for p in expected.glob("*.pdf") if helpers.is_pdf(p))
    if on_disk:
        assert helpers.manuals_directory(out) == expected, last
        assert f"{len(on_disk)} PDFs in {expected}" in last, last
    else:
        assert "none in this install" in last and str(expected) in last, last


def test_a_frozen_one_directory_bundle_names_its_internal_directory(tmp_path):
    """PyInstaller puts the data files in `_internal`; a frozen ATE reports exactly that path."""
    bundle = tmp_path / "dist" / "ate"
    internal = bundle / "_internal"
    _write_pdfs(internal / "ate" / "manuals")
    exe = bundle / "ate"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    exe.chmod(0o755)
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        f"sys.frozen = True\n"
        f"sys._MEIPASS = {str(internal)!r}\n"
        "from ate.cli import main\n"
        "sys.exit(main(['--help']))\n"
    )
    out = _run(script, dict(os.environ, PYTHONPATH=str(SRC)))
    directory = helpers.manuals_directory(out)
    assert directory == internal / "ate" / "manuals", directory
    assert helpers.problems_with(directory, "1.2.3") == [], helpers.problems_with(directory, "1.2.3")
    assert helpers.bytes_of(directory) > 0, directory


def test_a_frozen_bundle_without_meipass_falls_back_to_the_sibling_internal(tmp_path):
    """The fallback branch: `sys._MEIPASS` is absent, so the executable's own directory is asked."""
    bundle = tmp_path / "ate"
    _write_pdfs(bundle / "_internal" / "ate" / "manuals")
    exe = bundle / "ate"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    exe.chmod(0o755)
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "sys.frozen = True\n"
        f"sys.executable = {str(exe)!r}\n"
        "from ate.manuals import manuals_note, manuals_dir\n"
        "print(manuals_note())\n"
        "print(manuals_dir())\n"
    )
    out = _run(script, dict(os.environ, PYTHONPATH=str(SRC)))
    note = out.splitlines()[0]
    assert note.startswith("manuals: 3 PDFs in"), note
    assert helpers.manuals_directory(note) == bundle / "_internal" / "ate" / "manuals", note


def test_a_wheel_installed_in_site_packages_names_that_package(tmp_path):
    """The wheel's package directory is the install root: copy the package, add the PDFs, run it."""
    site = tmp_path / "site-packages"
    package = site / "ate"
    import shutil

    shutil.copytree(SRC / "ate", package)
    _write_pdfs(package / "manuals")
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(site)!r})\n"
        "from ate.cli import main\n"
        "sys.exit(main(['--help']))\n"
    )
    out = _run(script, dict(os.environ, PYTHONPATH=str(site)))
    directory = helpers.manuals_directory(out)
    assert directory == package / "manuals", directory
    assert len(helpers.installed_manuals(directory)) == len(PDF_NAMES), directory


def test_an_install_that_has_no_pdfs_says_it_has_none(tmp_path):
    site = tmp_path / "site-packages"
    import shutil

    shutil.copytree(SRC / "ate", site / "ate", ignore=shutil.ignore_patterns("manuals"))
    out = _run(
        "import sys\n"
        f"sys.path.insert(0, {str(site)!r})\n"
        "from ate.cli import main\n"
        "sys.exit(main(['--help']))\n",
        dict(os.environ, PYTHONPATH=str(site)),
    )
    assert "manuals: none in this install" in out, out[-400:]
    with pytest.raises(ValueError) as exc:
        helpers.manuals_directory(out)
    assert "no manuals at all" in str(exc.value)


def test_the_deb_doc_directory_is_named_when_it_is_there(tmp_path, monkeypatch):
    """A .deb install also puts the PDFs in /usr/share/doc/ate; the note says so only when they are."""
    import ate.manuals as app_manuals

    internal = tmp_path / "_internal"
    _write_pdfs(internal / "ate" / "manuals")
    doc = tmp_path / "usr/share/doc/ate"
    _write_pdfs(doc)
    monkeypatch.setattr(app_manuals, "DEB_DOC_DIR", doc)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    note = app_manuals.manuals_note()
    assert str(internal / "ate" / "manuals") in note and str(doc) in note, note
    assert note.count("PDFs") == 1, note


def test_the_debian_sentence_does_not_become_part_of_the_parsed_path(tmp_path, monkeypatch):
    """The `.deb` copy is its own line: the smoke helper reads line one as a path, so it must be only a path."""
    import ate.manuals as app_manuals

    internal = tmp_path / "_internal"
    _write_pdfs(internal / "ate" / "manuals")
    doc = tmp_path / "usr/share/doc/ate"
    _write_pdfs(doc)
    monkeypatch.setattr(app_manuals, "DEB_DOC_DIR", doc)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    note = app_manuals.manuals_note()
    directory = helpers.manuals_directory(note)
    assert directory == internal / "ate" / "manuals", note
    assert helpers.problems_with(directory, "1.2.3") == [], helpers.problems_with(directory, "1.2.3")


def test_the_deb_doc_directory_is_not_claimed_when_it_is_absent(tmp_path, monkeypatch):
    import ate.manuals as app_manuals

    internal = tmp_path / "_internal"
    _write_pdfs(internal / "ate" / "manuals")
    monkeypatch.setattr(app_manuals, "DEB_DOC_DIR", tmp_path / "usr" / "share" / "doc" / "ate")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    note = app_manuals.manuals_note()
    assert "/usr/share/doc" not in note, note
    assert note.startswith("manuals: 3 PDFs in"), note
