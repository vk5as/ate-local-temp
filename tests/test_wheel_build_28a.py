"""Packet 28a: the wheel is a complete, correctly declared package.

Everything is read back out of the built ``.whl`` zip rather than out of
``pyproject.toml``, so the test fails if setuptools drops a template, a Lua module
or a metadata field.

The wheel is built from a clean COPY of the checkout, never from the checkout: an
editable install leaves ``src/ate.egg-info/SOURCES.txt`` behind, and setuptools reads
that and ships every file it names whatever ``package-data`` says. Building from a
copy with no build state is the only way the resource test can actually fail when a
resource is left out. Only the install test needs the network, and it checks for one
when it runs, not at collection, so a deselected run pays nothing.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent  # repository root
SRC = REPO / "src"
DOCS = REPO / "docs" / "building-from-source.md"

# What the build reads. The version comes from src/ate/_version.py, not from git, so
# no .git or tests/ is needed.
BUILD_INPUTS = ("pyproject.toml", "README.md", "src")

# Runtime dependencies and the lowest version each one is known to work with.
# An upper bound is deliberately absent: the frozen builds pin exact versions, the
# wheel floats to whatever PyPI has that satisfies the floor.
EXPECTED_DEPS = {
    "pyqt6": "6.7",
    "pyqt6-webengine": "6.7",
    "lupa": "2.8",
    "pyvisa": "1.16",  # PyPI's newest is 1.16.2; 1.22 was never released
    "pyvisa-py": "0.7",
    "jinja2": "3.1",
    "sqlalchemy": "2.0",
    "pyserial": "3.3",
}

# The build tooling a fresh venv does not have and this file and the docs need.
BUILD_TOOLING = {"setuptools", "build"}


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


def _version_tuple(text: str) -> tuple[int, ...]:
    """A version as ints so `1.16` compares against `1.16.2` without a dependency."""
    return tuple(int(part) for part in text.split(".") if part.isdigit())


def _build_distribution_available() -> bool:
    """Whether the `build` package is installed (checked by distribution, not import)."""
    try:
        importlib.metadata.version("build")
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


def _build_state() -> list[str]:
    """The build junk in the checkout (build/, dist/, *.egg-info), as paths.

    A developer who ran `pip install -e .` legitimately has an egg-info here; the
    point of the test below is that building the wheel neither creates nor deletes
    any of it.
    """
    junk = [REPO / "build", REPO / "dist", *REPO.glob("*.egg-info"), *SRC.glob("*.egg-info")]
    return sorted(p.relative_to(REPO).as_posix() for p in junk if p.exists())


def _clean_build_copy(root: Path = REPO) -> Path:
    """Copy of the checkout with no build state: no egg-info, no build/, no dist/."""
    copy = Path(tempfile.mkdtemp(prefix="ate-build-"))
    for name in BUILD_INPUTS:
        source = root / name
        if not source.exists():
            continue
        if source.is_dir():
            shutil.copytree(source, copy / name, ignore=shutil.ignore_patterns("*.egg-info", "__pycache__"))
        else:
            shutil.copyfile(source, copy / name)
    return copy


def _build_wheel(source: Path, out: Path) -> Path:
    """Build the wheel from ``source`` into ``out``, with or without the `build` package."""
    if _build_distribution_available():
        # --no-isolation keeps this offline and fast: setuptools is in the test extra.
        cmd = [sys.executable, "-m", "build", "--wheel", "--no-isolation", "-o", str(out)]
    else:
        cmd = [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-w", str(out), "."]
    result = subprocess.run(cmd, cwd=source, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"wheel build failed: {result.stderr[-800:]}")
    wheels = list(out.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected exactly one wheel, got {[w.name for w in wheels]}")
    return wheels[0]


@pytest.fixture(scope="session")
def wheel_build():
    """Build the wheel once per session, then delete the copy and the output directory."""
    before = _build_state()
    source = _clean_build_copy()
    # The build itself makes an egg-info in the copy; what matters is that the copy it
    # started from had none, and that the checkout's own build state is left alone.
    copy_is_clean = not list(source.glob("**/*.egg-info"))
    out = Path(tempfile.mkdtemp(prefix="ate-wheel-"))
    try:
        wheel = _build_wheel(source, out)
        yield wheel, source, copy_is_clean, before, _build_state()
    finally:
        shutil.rmtree(source, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


@pytest.fixture(scope="session")
def wheel(wheel_build) -> zipfile.ZipFile:
    """The same wheel, open as a zip."""
    with zipfile.ZipFile(wheel_build[0]) as zf:
        yield zf


def _dist_info(wheel: zipfile.ZipFile, name: str) -> str:
    """Contents of a ``.dist-info`` file in the wheel."""
    for arc in wheel.namelist():
        if arc.endswith(f".dist-info/{name}"):
            return wheel.read(arc).decode("utf-8")
    return ""


def test_wheel_is_built_from_a_clean_copy(wheel_build) -> None:
    """The build reads a copy in a temp dir, and the checkout's own build state is untouched."""
    _, source, copy_is_clean, before, after = wheel_build
    assert source != REPO and source.parent == Path(tempfile.gettempdir()), f"built in {source}"
    assert (source / "pyproject.toml").is_file() and (source / "src").is_dir(), "the copy is incomplete"
    assert copy_is_clean, "the copy carried an egg-info: setuptools ships the files its SOURCES.txt names, whatever package-data says"
    assert before == after, f"the build changed the checkout: {before} -> {after}"


def test_an_egg_info_from_an_editable_install_never_reaches_the_build() -> None:
    """Setuptools ships whatever an egg-info's SOURCES.txt names, whatever package-data says.

    `pip install -e .` leaves `src/ate.egg-info` in the checkout, so the build must run on a
    copy that has none. This feeds the copier a fake checkout with one and checks it is dropped.
    """
    fake = Path(tempfile.mkdtemp(prefix="ate-checkout-"))
    try:
        (fake / "pyproject.toml").write_text('[project]\nname = "ate"\n', encoding="utf-8")
        (fake / "src" / "ate.egg-info").mkdir(parents=True)
        (fake / "src" / "ate.egg-info" / "SOURCES.txt").write_text("src/ate/lua/devices.lua\n", encoding="utf-8")
        copy = _clean_build_copy(fake)
        try:
            assert (copy / "pyproject.toml").is_file() and (copy / "src").is_dir(), "the copy is incomplete"
            assert not list(copy.glob("**/*.egg-info")), "the build copy carried an egg-info"
        finally:
            shutil.rmtree(copy, ignore_errors=True)
    finally:
        shutil.rmtree(fake, ignore_errors=True)


def test_package_data_decides_what_the_wheel_carries() -> None:
    """Drop `*.lua` from package-data and the Lua module is not in the wheel: the resource test can fail.

    Built in the checkout with an editable install's egg-info still in it, the Lua file shipped
    anyway, which is why the build runs on a clean copy.
    """
    source = _clean_build_copy()
    out = Path(tempfile.mkdtemp(prefix="ate-wheel-mut-"))
    try:
        pyproject = source / "pyproject.toml"
        pyproject.write_text(pyproject.read_text(encoding="utf-8").replace('"*.lua", ', ""), encoding="utf-8")
        wheel = _build_wheel(source, out)
        with zipfile.ZipFile(wheel) as zf:
            lua = [n for n in zf.namelist() if n.endswith(".lua")]
        assert not lua, f"package-data lists no Lua, the wheel carries {lua}"
    finally:
        shutil.rmtree(source, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


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


def test_wheel_declares_every_runtime_dependency(wheel: zipfile.ZipFile) -> None:
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
    for name, expected in EXPECTED_DEPS.items():
        bounds = declared[name]
        assert ">=" in bounds, f"{name} has no lower bound ({bounds})"
        got = re.search(r">=(\d+(?:\.\d+)*)", bounds).group(1)
        assert _version_tuple(got) == _version_tuple(expected), f"{name} floor {got} != declared {expected}"


def test_every_floor_is_a_version_that_exists(wheel: zipfile.ZipFile) -> None:
    """A floor nobody ever released makes the wheel uninstallable: `pyvisa>=1.22` did that.

    Each floor must be at most the version installed in the venv running this suite. The
    suite passes against those installs, so a floor above them cannot exist on PyPI. No
    network needed.
    """
    for name, floor in EXPECTED_DEPS.items():
        installed = importlib.metadata.version(name)
        assert _version_tuple(floor) <= _version_tuple(installed), (
            f"{name}>= {floor} is above the installed {installed}: no such release exists"
        )
        # and the wheel really says that floor
        assert f"{name}>={floor}".lower() in _dist_info(wheel, "METADATA").lower(), (
            f"wheel declares {name} differently: {floor} expected"
        )


def test_the_test_extra_declares_the_build_tooling() -> None:
    """`pip install -e ".[test]"` in a fresh venv must provide what these tests and the docs use.

    A venv ships pip only, so `python -m build` and setuptools' build backend fail unless
    the extra asks for them.
    """
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    extra = pyproject["project"]["optional-dependencies"]["test"]
    names = {re.split(r"[><=~!]", spec)[0].lower() for spec in extra}
    assert BUILD_TOOLING <= names, f"[test] extra is missing build tooling: {sorted(extra)}"


def _network_available() -> bool:
    """Whether pip can reach PyPI at all. Called by the install test, not at collection."""
    import socket

    try:
        with socket.create_connection(("pypi.org", 443), timeout=5):
            return True
    except OSError:
        return False


def _venv_exe(venv: Path, name: str) -> str:
    """Path to an executable inside a venv, on Linux or Windows."""
    exe_dir = venv / ("Scripts" if os.name == "nt" else "bin")
    return str(exe_dir / (name + (".exe" if os.name == "nt" else "")))


def _install_wheel(venv: Path, wheel_path: Path, env: dict[str, str]) -> None:
    """Install the wheel into a fresh venv. A pip error raises: only an unreachable network skips.

    Dependencies are not fetched: only SQLAlchemy (plus typing_extensions, which it
    imports) are installed — that is the whole import chain of `ate --version`. Pulling
    PyQt6 would cost minutes.
    """
    result = subprocess.run(
        [_venv_exe(venv, "python"), "-m", "pip", "install", "--no-deps", str(wheel_path), "SQLAlchemy>=2.0", "typing_extensions"],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pip could not install the wheel: {result.stderr[-300:]}")


def test_a_pip_error_is_a_failure_not_a_skip(monkeypatch) -> None:
    """The install test must not hide a broken install behind a skip.

    `pytest.skip()` on a pip error makes the suite green while the wheel is uninstallable.
    """

    def failing_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "ERROR: could not find a version that satisfies the requirement")

    monkeypatch.setattr(subprocess, "run", failing_run)
    with pytest.raises(RuntimeError, match="pip could not install the wheel"):
        _install_wheel(Path("/nonexistent-venv"), Path("ate-0.0.0.dev0-py3-none-any.whl"), {})


def test_the_network_probe_runs_when_the_test_runs() -> None:
    """The socket probe is inside the install test, so `-m "not slow"` pays nothing for it."""
    body = inspect.getsource(test_installed_wheel_runs_ate_version)
    assert "_network_available()" in body, "the network check belongs in the test body, not a collection-time skipif"
    marks = [getattr(mark, "name", "") for mark in getattr(test_installed_wheel_runs_ate_version, "pytestmark", [])]
    assert "skipif" not in marks, f"skipif runs the probe at collection, deselected or not: {marks}"
    assert "slow" in marks, "the install test is the only slow one; -m 'not slow' must deselect it"


@pytest.mark.slow
def test_installed_wheel_runs_ate_version(wheel_build) -> None:
    """A fresh venv installs the wheel and `ate --version` runs from it.

    Skipped only when PyPI is unreachable; a pip failure on a reachable network fails.
    """
    wheel_path = wheel_build[0]
    if not _network_available():
        pytest.skip("no network to fetch dependencies")
    tmp = Path(tempfile.mkdtemp(prefix="ate-venv-"))
    venv = tmp / ".venv"
    # PYTHONPATH would put the checkout on the new venv's sys.path, and pip would
    # then think `ate` is already installed and skip the wheel.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    try:
        create = subprocess.run([sys.executable, "-m", "venv", str(venv)], capture_output=True, text=True)
        if create.returncode != 0:
            pytest.skip(f"cannot create a venv: {create.stderr[-300:]}")
        _install_wheel(venv, wheel_path, env)
        run = subprocess.run([_venv_exe(venv, "ate"), "--version"], capture_output=True, text=True, env=env)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert run.returncode == 0, run.stderr
    assert re.fullmatch(r"\d+\.\d+\.\d+[^\n]*", run.stdout.strip()), run.stdout


def test_the_source_docs_describe_a_cli_that_exists() -> None:
    """The docs must not promise a GUI: `ate` with no subcommand is not a launcher yet.

    The application shell that makes bare `ate` open the GUI is packet 30a.
    """
    text = DOCS.read_text(encoding="utf-8")
    assert "ate --version" in text, "the docs must show how to check the install"
    assert "opens the GUI" not in text, "there is no GUI launcher yet (packet 30a adds it)"
    assert "not slow" in text and "Skip both" not in text, "only the install test is marked slow"
    assert "must be run as root" not in text, "setup runs unprivileged; it just skips group and ownership"
    assert "python -m build" in text, "the docs still describe the wheel build"
