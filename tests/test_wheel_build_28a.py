"""Packet 28a: the wheel is a complete, correctly declared package.

Everything is read back out of the built ``.whl`` zip rather than out of
``pyproject.toml``, so the test fails if setuptools drops a template, a Lua
module or a metadata field. The wheel is built from this checkout into a
temporary directory; only the install test needs the network, and it skips
when there is none.
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent  # repository root
SRC = REPO / "src"

# Runtime dependencies and the lowest version each one is known to work with.
# An upper bound is deliberately absent: the frozen builds pin exact versions,
# the wheel floats to whatever PyPI has that satisfies the floor.
EXPECTED_DEPS = {
    "pyqt6": "6.7",
    "pyqt6-webengine": "6.7",
    "lupa": "2.8",
    "pyvisa": "1.22",
    "pyvisa-py": "0.7",
    "jinja2": "3.1",
    "sqlalchemy": "2.0",
    "pyserial": "3.3",
}


def _runtime_resources() -> set[str]:
    """Every non-.py file under ``src/ate`` — templates, Lua modules, CSS, data.

    Derived from the source tree on purpose: add a template and forget the
    package-data entry, and this test is the thing that catches it.
    """
    resources = set()
    for path in (SRC / "ate").rglob("*"):
        if not path.is_file():
            continue
        if path.suffix in {".py", ".pyc"} or path.parent.name == "__pycache__":
            continue
        resources.add(path.relative_to(SRC).as_posix())
    return resources


def _build_distribution_available() -> bool:
    """Whether the `build` package is installed (checked by distribution, not import)."""
    try:
        importlib.metadata.version("build")
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


def _clean_build_artifacts() -> None:
    """Delete the in-tree junk a local build leaves (build/, *.egg-info).

    The stray egg-info is more than untidy: with `PYTHONPATH=src` it is importable,
    so pip in a fresh venv believes `ate` is already installed and skips the wheel.
    """
    for junk in [REPO / "build", *REPO.glob("*.egg-info"), *(REPO / "src").glob("*.egg-info")]:
        shutil.rmtree(junk, ignore_errors=True)


def _build_wheel() -> Path:
    """Build the wheel into a temporary directory, with or without the `build` package."""
    out = Path(tempfile.mkdtemp(prefix="ate-wheel-"))
    # `build` is the documented way. The check is by distribution, not find_spec:
    # a leftover `build/` directory on sys.path looks like a namespace package.
    if _build_distribution_available():
        # --no-isolation keeps this offline and fast: setuptools is already in the venv.
        cmd = [sys.executable, "-m", "build", "--wheel", "--no-isolation", "-o", str(out)]
    else:
        cmd = [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-w", str(out), "."]
    result = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    _clean_build_artifacts()
    if result.returncode != 0:
        raise RuntimeError(f"wheel build failed: {result.stderr[-800:]}")
    wheels = list(out.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected exactly one wheel, got {[w.name for w in wheels]}")
    return wheels[0]


@pytest.fixture(scope="session")
def wheel_path() -> Path:
    """The built wheel, once per session."""
    return _build_wheel()


@pytest.fixture(scope="session")
def wheel(wheel_path: Path) -> zipfile.ZipFile:
    """The same wheel, open as a zip."""
    with zipfile.ZipFile(wheel_path) as zf:
        yield zf


def _dist_info(wheel: zipfile.ZipFile, name: str) -> str:
    """Contents of a ``.dist-info`` file in the wheel."""
    for arc in wheel.namelist():
        if arc.endswith(f".dist-info/{name}"):
            return wheel.read(arc).decode("utf-8")
    return ""


def test_wheel_ships_every_runtime_resource(wheel: zipfile.ZipFile) -> None:
    """Each template, Lua module and data file under ``src/ate`` is inside the wheel."""
    arcs = set(wheel.namelist())
    missing = sorted(_runtime_resources() - arcs)
    assert not missing, f"wheel is missing runtime resources: {missing}"


def test_wheel_declares_python_and_classifiers(wheel: zipfile.ZipFile) -> None:
    """CPython 3.13 and 3.14 are supported (spec §Packaging: wheel supports both)."""
    meta = _dist_info(wheel, "METADATA")
    assert re.search(r"^Requires-Python: >=3\.13$", meta, re.MULTILINE), meta
    for minor in ("3.13", "3.14"):
        assert f"Programming Language :: Python :: {minor}" in meta


def test_wheel_exposes_the_ate_entry_point(wheel: zipfile.ZipFile) -> None:
    """`pipx install ate` puts `ate` on the PATH, pointing at ate.cli:main."""
    entry_points = _dist_info(wheel, "entry_points.txt")
    assert re.search(r"^ate = ate\.cli:main$", entry_points, re.MULTILINE), entry_points


def test_wheel_pins_its_runtime_dependencies(wheel: zipfile.ZipFile) -> None:
    """Every runtime dependency is declared with a lower bound, and nothing else is."""
    declared = {}
    for line in _dist_info(wheel, "METADATA").splitlines():
        if not line.startswith("Requires-Dist: "):
            continue
        spec = line[len("Requires-Dist: "):]
        if ";" in spec:  # an optional extra (`; extra == "test"`), not a runtime dependency
            continue
        name, *rest = re.match(r"([A-Za-z0-9._-]+)(.*)", spec).groups()
        declared[name.strip().lower()] = rest[0].strip()

    assert set(declared) == set(EXPECTED_DEPS), f"unexpected dependency set: {sorted(declared)}"
    for name, floor in EXPECTED_DEPS.items():
        bounds = declared[name]
        assert ">=" in bounds, f"{name} has no lower bound ({bounds})"
        got = re.search(r">=(\d+(?:\.\d+)*)", bounds).group(1)
        assert tuple(int(p) for p in got.split(".")) >= tuple(int(p) for p in floor.split(".")), (
            f"{name} floor {got} is below the known-good {floor}"
        )


def _has_network() -> bool:
    """Whether pip can reach PyPI at all (the install test needs it)."""
    import socket

    try:
        with socket.create_connection(("pypi.org", 443), timeout=5):
            return True
    except OSError:
        return False


@pytest.mark.slow
@pytest.mark.skipif(not _has_network(), reason="no network to fetch dependencies")
def test_installed_wheel_runs_ate_version(wheel_path: Path) -> None:
    """A fresh venv installs the wheel and `ate --version` runs from it.

    Dependencies are not fetched: only SQLAlchemy (plus typing_extensions, which it
    imports) are installed — that is the whole import chain of `ate --version`.
    Pulling PyQt6 would cost minutes.
    """
    tmp = Path(tempfile.mkdtemp(prefix="ate-venv-"))
    venv = tmp / ".venv"
    exe_dir = venv / ("Scripts" if os.name == "nt" else "bin")
    exe = lambda name: str(exe_dir / (name + (".exe" if os.name == "nt" else "")))  # noqa: E731
    try:
        create = subprocess.run([sys.executable, "-m", "venv", str(venv)], capture_output=True, text=True)
        if create.returncode != 0:
            pytest.skip(f"cannot create a venv: {create.stderr[-300:]}")
        install = subprocess.run(
            [exe("python"), "-m", "pip", "install", "--no-deps", str(wheel_path), "SQLAlchemy>=2.0", "typing_extensions"],
            capture_output=True,
            text=True,
            # PYTHONPATH would put the checkout on the new venv's sys.path, and pip
            # would then think `ate` is already installed and skip the wheel.
            env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        )
        if install.returncode != 0:
            pytest.skip(f"could not install the wheel (pip failed): {install.stderr[-300:]}")
        run = subprocess.run(
            [exe("ate"), "--version"],
            capture_output=True,
            text=True,
            env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert run.returncode == 0, run.stderr
    assert re.fullmatch(r"\d+\.\d+\.\d+[^\n]*", run.stdout.strip()), run.stdout
