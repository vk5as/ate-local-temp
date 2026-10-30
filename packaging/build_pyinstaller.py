#!/usr/bin/env python3
"""Build ATE's frozen one-directory bundle with PyInstaller (packet 27a).

    python packaging/build_pyinstaller.py                # build dist/ate/
    python packaging/build_pyinstaller.py --smoke        # build, then run the smoke checks
    python packaging/build_pyinstaller.py --clean        # ...and remove dist/ and build/ after
    python packaging/build_pyinstaller.py --no-venv      # use this interpreter (deps already present)
    python packaging/build_pyinstaller.py --verify dist/ate/ate  # check a bundle without building or running it

The build is deliberately CPython-version-agnostic: it makes a throwaway venv with the
interpreter running this script (3.13 on a workstation, 3.14 in the release CI), installs
the package and PyInstaller into it, and hands ``packaging/pyinstaller/ate.spec`` to
PyInstaller. Nothing here reads a version number, so the same recipe builds the Windows
installer (27b), the ``.tar.gz`` and the ``.deb`` (28b).

Output: ``dist/ate/ate`` plus its ``_internal`` directory. Every build is checked for the parts
Qt needs before it can print a PDF — a platform plugin, ``QtWebEngineProcess``, the WebEngine
resources — so a bundle whose hooks collected nothing is a failed build, not a small one. Smoke
checks (``--smoke``) then run the bundle:

    dist/ate/ate --version           the version baked from the git tag
    dist/ate/ate _pdf in.html out.pdf   proves QtWebEngine works frozen (offscreen)
    dist/ate/ate _selftest --run     proves lupa, the bundled Lua, pyvisa-py and WebEngine are there,
                                     and that a built-in Test Schedule runs and reports to HTML and PDF

``build/`` and ``dist/`` are in ``.gitignore``: no build output is ever committed.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "packaging" / "pyinstaller" / "ate.spec"
SEMVER = re.compile(r"\d+\.\d+\.\d+")

# A page small enough to render in a second; the smoke check only cares that the frozen
# QWebEngine can load a file and print it.
SMOKE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>ATE build smoke test</title></head>
<body><h1>ATE frozen build smoke test</h1><p>QWebEngine renders this to PDF.</p></body></html>
"""


def _python_in(venv: Path) -> Path:
    """The interpreter inside ``venv``.

    Windows venvs put it in ``Scripts``, every other platform in ``bin``; getting this wrong makes
    the build fail at the first ``_run`` on a Windows runner."""
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _run(cmd: list[str], *, cwd: Path = REPO, env: dict | None = None, timeout: int = 1800) -> subprocess.CompletedProcess:
    """Run a command, printing it, and exit loudly on failure.

    1800 s, not 600: a onedir build that bundles QtWebEngine takes well over ten minutes on a
    shared CI runner, especially the Windows one, and a timeout that fires mid-build reads as a
    PyInstaller failure rather than a slow machine."""
    print("$ " + " ".join(str(part) for part in cmd))
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        detail = (proc.stdout + "\n" + proc.stderr)[-1500:]
        raise SystemExit(f"{' '.join(map(str, cmd))}\nfailed with exit {proc.returncode}:\n{detail}")
    return proc


def _build_venv(path: Path) -> Path:
    """Create (or reuse) the throwaway build venv and return its interpreter."""
    python = _python_in(path)
    if not python.exists():
        _run([sys.executable, "-m", "venv", str(path)])
    return python


def _build_copy(work: Path) -> Path:
    """A clean copy of the checkout to install into the venv.

    Installing the checkout itself makes setuptools write ``build/`` into the repo; the copy
    keeps every scrap of the build under the work directory. Same trick as the wheel test.
    """
    copy = work / "checkout"
    if copy.exists():
        shutil.rmtree(copy)
    copy.mkdir(parents=True)
    for name in ("pyproject.toml", "README.md", "src"):
        source = REPO / name
        if source.is_dir():
            shutil.copytree(source, copy / name, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
        elif source.exists():
            shutil.copyfile(source, copy / name)
    return copy


def _install_deps(python: Path, work: Path) -> None:
    """Install ATE and PyInstaller into the build venv. Needs the network on a fresh venv."""
    try:
        _run([str(python), "-m", "pip", "install", "--quiet", str(_build_copy(work)), "pyinstaller"], cwd=work, timeout=900)
    except SystemExit as exc:
        raise SystemExit(f"could not install ATE and PyInstaller into the build venv: {exc}")


def _version(python: Path) -> str:
    """The version the bundle will carry, computed the same way the wheel's is."""
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
    return _run([str(python), "-c", "from ate._version import __version__; print(__version__)"], env=env).stdout.strip()


def _build(python: Path, dist: Path, work: Path, version_file: Path) -> Path:
    """Run PyInstaller and return the one-directory bundle (``dist/ate``)."""
    env = dict(os.environ, ATE_BUILD_VERSION_FILE=str(version_file))
    # PyInstaller is run from the work directory so its scratch never lands in the checkout.
    _run(
        [
            str(python),
            "-m",
            "PyInstaller",
            str(SPEC),
            "--noconfirm",  # rebuild over a previous dist/ate
            "--distpath",
            str(dist),
            "--workpath",
            str(work / "pyinstaller-work"),
        ],
        cwd=work,
        env=env,
    )
    bundle = dist / "ate"
    exe = bundle / ("ate.exe" if os.name == "nt" else "ate")
    if not exe.is_file():
        raise SystemExit(f"PyInstaller produced no executable at {exe}")
    return exe


def verify_bundle(exe: Path) -> list[str]:
    """Check that the bundle can actually render a Test Report, before anything ships it.

    PyInstaller's PyQt6 hooks wrap their collection in `if pyqt6_library_info.version is not None:`:
    when PyQt6 cannot be imported while PyInstaller is running — no libGL, no libglib, no X library —
    the version is None, the hooks collect nothing, and PyInstaller still writes the executable and
    reports success. That bundle has no Qt platform plugin, no `QtWebEngineProcess` and no WebEngine
    resources: `ate --version` prints fine, every `ate _pdf` dies at "Could not find the Qt platform
    plugin", and the build script used to say "built ... 121 files, 362 MB" and exit 0, because only
    ``--smoke`` looked at the contents. This runs on every build: the .deb, the .tar.gz and the
    Windows installer all take this bundle as their input (packet 30d review).

    Returns one line per thing found; raises SystemExit naming what is missing and why.
    """
    internal = exe.parent / "_internal"
    if not internal.is_dir():
        raise SystemExit(f"{exe.parent} is not a one-directory bundle: no _internal beside {exe.name}")

    for rel in ("ate/lua/devices.lua", "ate/report/templates/report.html.j2"):
        if not (internal / rel).is_file():
            raise SystemExit(f"bundle check: {rel} is not in the bundle")

    helper_name = "QtWebEngineProcess.exe" if os.name == "nt" else "QtWebEngineProcess"
    helper = [p for p in internal.rglob(helper_name) if p.is_file()]
    paks = [p for p in internal.rglob("*.pak") if p.is_file()]
    icu = [p for p in internal.rglob("icudtl.dat") if p.is_file()]
    # A platform plugin is what Qt needs before anything else: no plugin, no QApplication, no page,
    # no PDF — however many megabytes of Qt are sitting next to it.
    plugins = [p for p in internal.rglob("*") if "platforms" in p.parts and p.suffix in (".so", ".dll") and p.is_file()]

    if not plugins:
        raise SystemExit(
            f"bundle check: no Qt platform plugin under {internal}: PyQt6 could not be "
            "imported while PyInstaller ran, so its hooks collected nothing and this bundle cannot "
            "start Qt at all. Install what Qt is linked against (libgl1, libegl1, libglib2.0-0t64, "
            "libX11, libXext, libxcb, libxkbcommon) or build on a machine that has them, and rebuild."
        )
    if not helper:
        raise SystemExit(
            f"bundle check: no {helper_name} in the bundle: the QtWebEngine helper is not there, so a "
            "Test Report PDF has nothing to render it. The PyQt6 hook collected part of Qt only."
        )
    if not paks or not icu:
        raise SystemExit(
            f"bundle check: QtWebEngine resources missing ({len(paks)} .pak files, {len(icu)} icudtl.dat): "
            "the renderer has no ICU data and no web resources to load a page with."
        )

    return [
        f"qt platform plugins: {len(plugins)} files (one per platform Qt can open)",
        f"webengine helper: {helper[0].name}",
        f"webengine resources: {len(paks)} .pak files, {len(icu)} icudtl.dat",
    ]


def _run_smoke(exe: Path, expected_version: str) -> list[str]:
    """Run the four checks a frozen bundle must pass; return one line per check."""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    lines: list[str] = []

    # 1. the CLI runs and prints the version baked at build time.
    out = _run([str(exe), "--version"], env=env, timeout=120)
    version = out.stdout.strip()
    if not SEMVER.search(version):
        raise SystemExit(f"smoke: --version printed {version!r}, not a semver")
    lines.append(f"version: {version} (build expected {expected_version})")

    # 2. QWebEngine renders a page to PDF inside the frozen bundle. Packet 30d: the helper is told
    #    explicitly to render unsandboxed (the build runners are root); an ordinary user's `_pdf`
    #    ignores the flag and keeps the sandbox.
    tmp = Path(tempfile.mkdtemp(prefix="ate-smoke-"))
    try:
        html, pdf = tmp / "report.html", tmp / "report.pdf"
        html.write_text(SMOKE_HTML, encoding="utf-8")
        _run([str(exe), "_pdf", str(html), str(pdf), "--paper", "A4", "--unsandboxed"], env=env, timeout=300)
        data = pdf.read_bytes()
        if not data.startswith(b"%PDF"):
            raise SystemExit(f"smoke: _pdf wrote {len(data)} bytes that are not a PDF")
        lines.append(f"pdf: rendered {html.name} -> {pdf.name} ({len(data)} bytes)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 3. the bundle's own inventory, and a built-in Test Schedule run end to end through it:
    #    engine, store, report HTML, and the _pdf child (the bundle's own executable when frozen).
    #    The flag (packet 30d) is what lets a privileged runner's schedule produce a PDF at all.
    out = _run([str(exe), "--allow-unsandboxed-pdf", "_selftest", "--run"], env=env, timeout=300)
    lines.append("selftest: " + "; ".join(out.stdout.strip().splitlines()))

    # 4. the bundle's inventory — the WebEngine helper, its resources, the report template, the
    #    bundled Lua, a Qt platform plugin. verify_bundle already ran when this build was made; it
    #    runs again here so a bundle handed in from elsewhere is checked too.
    lines.extend(verify_bundle(exe))

    return lines


def _size(path: Path) -> tuple[int, int]:
    """Total bytes and file count under ``path``.

    ``lstat``, never ``stat``: a symlink is a few bytes of its own and the bundle's real size is the
    bytes it points at, counted once. Following links here logged 942 MB for a bundle that is about
    560 MB, and a hard link would be counted twice as well. Each inode is counted once, so the log
    says what the installer or tarball actually has to carry.
    """
    total = 0
    count = 0
    seen: set[tuple[int, int]] = set()
    for root, _dirs, files in os.walk(path):  # os.walk does not descend into linked directories
        for name in files:
            entry = os.path.join(root, name)
            if os.path.islink(entry):
                continue  # the link is not a second copy of the data
            st = os.lstat(entry)
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue  # the same inode reached by another name
            seen.add(key)
            total += st.st_size
            count += 1
    return total, count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", default=str(REPO / "dist"), help="where dist/ate lands (default: the repo's dist/)")
    parser.add_argument("--work", default=str(REPO / "build"), help="PyInstaller work/build directories")
    parser.add_argument("--venv", default=None, help="build venv to create or reuse (default: <work>/pyinstaller-venv)")
    parser.add_argument("--no-venv", action="store_true", help="build with the current interpreter instead of a fresh venv")
    parser.add_argument("--skip-install", action="store_true", help="don't pip-install into the venv (reuse it as-is)")
    parser.add_argument("--smoke", action="store_true", help="run the smoke checks after building")
    parser.add_argument("--verify", metavar="EXE", help="check an existing bundle and exit: no build, no run")
    parser.add_argument("--clean", action="store_true", help="remove dist/ and the work directories when finished")
    args = parser.parse_args(argv)

    if args.verify:
        exe = Path(args.verify)
        if not exe.is_file():
            raise SystemExit(f"--verify: {exe} is not an executable")
        for line in verify_bundle(exe):
            print(f"bundle ok: {line}")
        return 0

    dist, work = Path(args.dist), Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    venv = Path(args.venv) if args.venv else work / "pyinstaller-venv"
    python = sys.executable if args.no_venv else _build_venv(venv)

    if not args.skip_install:
        _install_deps(python, work)
    version = _version(python)

    version_file = work / "bundle" / "version.txt"  # collected as ate/version.txt in the bundle
    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text(version, encoding="utf-8")

    started = time.monotonic()
    exe = _build(python, dist, work, version_file)
    elapsed = time.monotonic() - started
    total, files = _size(dist / "ate")
    print(f"built {exe} in {elapsed:.1f}s: {files} files, {total / 1e6:.1f} MB")

    # The bundle is checked whatever the flags say: a bundle with no Qt in it is not a build that
    # succeeded, and packaging (28b) and the installer (27b) take it as input.
    for line in verify_bundle(exe):
        print(f"bundle ok: {line}")

    if args.smoke:
        for line in _run_smoke(exe, version):
            print(f"smoke ok: {line}")

    if args.clean:
        shutil.rmtree(dist, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
