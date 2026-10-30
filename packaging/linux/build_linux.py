#!/usr/bin/env python3
"""Package the frozen ATE bundle for Linux: a `.tar.gz` with `install.sh`, and a `.deb` (packet 28b).

    python packaging/linux/build_linux.py --dist dist                 # dist/ate already built
    python packaging/linux/build_linux.py --build                     # build dist/ate first (27a)
    python packaging/linux/build_linux.py --dist dist --out release   # where the artifacts land

Both artifacts carry the same PyInstaller one-directory bundle that 27a produces, so the recipe is
the same on any CPython and needs nothing but `dpkg-deb` for the Debian package:

    ate-<version>-linux-x86_64.tar.gz   `ate/` (the bundle) + `install.sh` next to it
    ate_<version>_amd64.deb             the bundle under /opt/ate, /usr/bin/ate linking to it

The `.deb` carries its own CPython, Qt, QtWebEngine, lupa and pyvisa-py inside `_internal`, but not
the libraries those are *linked* against: the system libc, GL/EGL, and a font, because QtWebEngine
renders the Test Report through the system's fontconfig and fonts and writes a PDF with no text at
all when there is none. That is the `Depends` list below, established by running the built bundle in
a clean `debian:stable` container (packaging/linux/docker_smoke.py), not by reading a manifest.

The wheel is a different animal: it uses the container's own CPython and PyPI's PyQt6, so it needs
more system libraries than the bundle does. `WHEEL_SYSTEM_LIBS` is that list, kept here so the
smoke run and the docs install the same set.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
BUILD_PYINSTALLER = REPO / "packaging" / "build_pyinstaller.py"
INSTALL_SH = HERE / "install.sh"
MAINTAINER = "VK5AS <vk5as.com@gmail.com>"
SEMVER = re.compile(r"\d+\.\d+\.\d+")

# What the bundle links against and cannot ship: glibc, GL/EGL, and a font. Everything else Qt is
# linked against (libX11, libXext, the libxcb family, libXcomposite, libXrandr, libXtst, libglib,
# libdbus, libxkbcommon, libasound, libnss, ICU, freetype, fontconfig) is inside `_internal`, which
# is why the package needs no Debian Python or Qt packages at all.
#
# This list is not a guess: `packaging/linux/docker_smoke.py` builds the bundle in a debian:stable
# container, installs the .deb there with apt-get, and runs `ate _selftest --run`. With these five
# packages the run passes; drop libgl1 or libegl1 and the selftest dies with
# "libGL.so.1: cannot open shared object file". libdrm, libxcb and libxkbcommon come in as libegl1's
# and libgl1's own dependencies, so naming them here would only duplicate apt.
#
# fontconfig and fonts-dejavu-core are not a nicety: a Debian container ships no fonts at all, and
# QtWebEngine then renders the report page with no glyphs — a 1.7 KB PDF with no text in it, which
# pdftotext reads as empty. With them the same report is a ~66 KB PDF with the words in it. See
# ate.report.pdf.pdf_has_text, which the selftest uses to catch exactly this.
DEPENDS = (
    "libc6",  # libc.so.6, libm, libdl, libpthread, libresolv
    "libgl1",  # libGL.so.1, libGLX.so.0, libGLdispatch.so.0: Qt's Gui is linked against them
    "libegl1",  # libEGL.so.1, which Qt6Gui and Qt6WebEngineCore both want
    "fontconfig",  # /etc/fonts: the bundled libfontconfig has no font list to read without it
    "fonts-dejavu-core",  # DejaVu Sans, the smallest font Debian ships that Chromium accepts
)

# The wheel uses the container's own CPython and PyPI's PyQt6/QtWebEngine, so the system libraries
# it needs are not the bundle's: PyPI's Qt is linked against a wider set of X and GL libraries than
# the bundle's own Qt, and it carries no fonts either. Established in a fresh debian:stable
# container by installing the wheel with pipx and then removing one package at a time: each entry
# below makes `ate _selftest --run` die with "cannot open shared object file". libdrm, libxcb and
# libxkbcommon come in as libgl1's and libegl1's own dependencies, so they are not repeated here;
# libnss, libgssapi, libpulse, libpcsclite and the rest of the xcb family turned out not to be
# needed at all by a headless report render.
WHEEL_SYSTEM_LIBS = (
    "libgl1",  # libGL.so.1, libGLX.so.0, libGLdispatch.so.0
    "libegl1",  # libEGL.so.1
    "libglib2.0-0t64",  # libglib-2.0.so.0 and libgthread-2.0.so.0: Qt6 Core/Gui/Network/DBus all want them
    "libdbus-1-3",  # libdbus-1.so.3, Qt6 Gui/DBus and Chromium's session bus probe
    "libxcomposite1",
    "libxdamage1",
    "libxfixes3",
    "libxrandr2",
    "libxtst6",
    "libxkbcommon0",
    "libxkbfile1",  # Qt6WebEngineCore's X plumbing
    "libasound2t64",  # libasound.so.2, Qt6WebEngineCore
    "libfontconfig1",  # libfontconfig.so.1, the font matcher PyPI's Qt is linked against
    "fonts-dejavu-core",  # without a font the report PDF renders with no text in it at all
)


def _run(cmd: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess:
    """Run a command, printing it, and exit loudly on failure."""
    print("$ " + " ".join(str(part) for part in cmd))
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise SystemExit(f"{' '.join(map(str, cmd))}\nfailed with exit {proc.returncode}:\n{(proc.stdout + proc.stderr)[-1500:]}")
    return proc


def _build_bundle(dist: Path, work: Path) -> Path:
    """Run 27a's recipe to make ``dist/ate``."""
    _run([sys.executable, str(BUILD_PYINSTALLER), "--dist", str(dist), "--work", str(work)], timeout=3600)
    return dist / "ate"


def _bundle_version(bundle: Path) -> str:
    """Ask the bundle what version it is; a bundle that will not say is packaged as 0.0.0-dev."""
    exe = bundle / "ate"
    if not exe.is_file():
        raise SystemExit(f"{exe} is not a built one-directory bundle (run packaging/build_pyinstaller.py first)")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([str(exe), "--version"], env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"warning: {exe} --version failed ({proc.returncode}); using 0.0.0-dev", file=sys.stderr)
        return "0.0.0-dev"
    text = proc.stdout.strip()
    if not text:
        # An empty version would name the artifacts "ate--linux-x86_64.tar.gz" and "ate__amd64.deb":
        # better to stop than to ship something nobody can identify.
        raise SystemExit(f"{exe} --version printed nothing: cannot package a bundle with no version")
    return text


def _deb_version(version: str) -> str:
    """Turn the version ATE prints into one dpkg will accept.

    Debian version strings are ``[A-Za-z0-9.+:-]`` with ``~`` sorting before everything, while a
    bare ``-`` sorts *after* the digits before it. So every prerelease suffix becomes ``~``:
    ``0.0.0-dev`` -> ``0.0.0~dev``, ``1.2.3-rc1`` -> ``1.2.3~rc1``. A prerelease then precedes the
    release it is a prerelease of, which is what it means.
    """
    text = (version or "").strip() or "0.0.0"
    text = re.sub(r"-(?:dev|pre|alpha|beta|rc)\d*", lambda m: "~" + m.group(0)[1:], text, flags=re.IGNORECASE)
    text = re.sub(r"[^A-Za-z0-9.+~:-]", "-", text)
    return text if any(ch.isdigit() for ch in text) else "0.0.0"


def _disk_bytes(path: Path) -> int:
    """Bytes under ``path`` (Installed-Size's unit is KiB). Symlinks count as the link, not the target."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            entry = os.path.join(root, name)
            if os.path.islink(entry):
                continue
            total += os.lstat(entry).st_size
    return total


def _root_member(member: tarfile.TarInfo, mode: int | None = None) -> tarfile.TarInfo:
    """Root-own a tar member: a tarball extracted on a Station must not carry the build machine's uid."""
    member.uid = 0
    member.gid = 0
    member.uname = "root"
    member.gname = "root"
    if mode is not None:
        member.mode = mode
    return member


def _tarball(bundle: Path, version: str, out: Path) -> Path:
    """``ate-<version>-linux-x86_64.tar.gz``: the bundle as ``ate/``, with install.sh beside it."""
    artifact = out / f"ate-{version}-linux-x86_64.tar.gz"
    with tarfile.open(artifact, "w:gz") as tar:
        tar.add(bundle, arcname="ate", filter=_root_member)
        # install.sh is 0755 in the artifact whatever the checkout's bits say, so extracting the
        # tarball and running ./install.sh never lands on a permission denied.
        tar.add(INSTALL_SH, arcname="install.sh", filter=lambda member: _root_member(member, 0o755))
    return artifact


def _deb(bundle: Path, version: str, out: Path) -> Path:
    """``ate_<version>_amd64.deb``, built with dpkg-deb --root-owner-group --build."""
    artifact = out / f"ate_{_deb_version(version)}_amd64.deb"
    stage = Path(tempfile.mkdtemp(prefix="ate-deb-"))
    try:
        shutil.copytree(bundle, stage / "opt" / "ate", symlinks=True)
        control = stage / "DEBIAN"
        control.mkdir(parents=True)
        (stage / "usr" / "bin").mkdir(parents=True)
        (stage / "usr" / "bin" / "ate").symlink_to("/opt/ate/ate")

        for script in ("postinst", "postrm"):
            source = HERE / "deb" / script
            target = control / script
            shutil.copyfile(source, target)
            target.chmod(0o755)

        control.joinpath("control").write_text(
            "Package: ate\n"
            f"Version: {_deb_version(version)}\n"
            "Section: science\n"
            "Priority: optional\n"
            "Architecture: amd64\n"
            f"Maintainer: {MAINTAINER}\n"
            f"Installed-Size: {max(1, _disk_bytes(stage / 'opt' / 'ate') // 1024)}\n"
            f"Depends: {', '.join(DEPENDS)}\n"
            "Description: Automated Test Execution (ATE) - run Lua Test Schedules against hardware\n"
            " A self-contained PyInstaller bundle in /opt/ate, with /usr/bin/ate linking to it. ATE's\n"
            " Python, Qt and QtWebEngine are inside the bundle, so only libc and the GL/X libraries\n"
            " the bundle links against have to come from Debian.\n",
            encoding="utf-8",
        )
        _run(["dpkg-deb", "--root-owner-group", "--build", str(stage), str(artifact)])
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", default=str(REPO / "dist"), help="directory holding the built ate/ bundle")
    parser.add_argument("--out", default=None, help="where the artifacts land (default: the --dist directory)")
    parser.add_argument("--work", default=str(REPO / "build"), help="PyInstaller work directory for --build")
    parser.add_argument("--build", action="store_true", help="build the bundle with packaging/build_pyinstaller.py first")
    args = parser.parse_args(argv)

    dist, out = Path(args.dist), Path(args.out) if args.out else Path(args.dist)
    out.mkdir(parents=True, exist_ok=True)
    bundle = dist / "ate"
    if args.build or not bundle.is_dir():
        if not bundle.is_dir():
            print(f"{bundle} does not exist; building it with PyInstaller")
        bundle = _build_bundle(dist, Path(args.work))

    version = _bundle_version(bundle)
    tarball = _tarball(bundle, version, out)
    deb = _deb(bundle, version, out)
    print(f"packaged {version}: {tarball} ({tarball.stat().st_size} bytes), {deb} ({deb.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
