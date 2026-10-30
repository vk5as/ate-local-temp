#!/usr/bin/env python3
"""Package the frozen ATE bundle for Linux: a `.tar.gz` with `install.sh`, and a `.deb` (packet 28b).

    python packaging/linux/build_linux.py --dist dist                 # dist/ate already built
    python packaging/linux/build_linux.py --build                     # build dist/ate first (27a)
    python packaging/linux/build_linux.py --dist dist --out release   # where the artifacts land

Both artifacts carry the same PyInstaller one-directory bundle that 27a produces, so the recipe is
the same on any CPython and needs nothing but `dpkg-deb` for the Debian package:

    ate-<version>-linux-x86_64.tar.gz   `ate/` (the bundle) + `install.sh` next to it
    ate_<version>_amd64.deb             the bundle under /opt/ate, /usr/bin/ate linking to it

The `.deb` is self-contained: everything ATE runs on (CPython, Qt, QtWebEngine, lupa, pyvisa-py,
ICU, NSS, fontconfig, libX11) is inside `_internal`. What it cannot carry is the system's own
libc and the GL/X libraries the Qt bundle is *linked* against, which is the whole `Depends` list.
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

# What the bundle links against and cannot ship: glibc, GL/EGL, and the X libraries Qt's platform
# plugins need. Determined by ldd over a built bundle's _internal: everything else resolves inside
# the bundle. libdrm and libxcb-dri3 are what QtWebEngine uses for its GPU path; the wayland
# libraries are only reached when the desktop is Wayland rather than X.
DEPENDS = (
    "libc6",  # libc.so.6, libm, libdl, libpthread, libresolv
    "libgl1",  # libGL.so.1, libGLX.so.0, libGLdispatch.so.0
    "libegl1",  # libEGL.so.1, which every Qt6 Gui-linked library wants
    "libdrm2",  # libdrm.so.2, needed by Qt6WebEngineCore
    "libxcb1",  # libxcb.so.1, the X connection Qt's xcb platform plugin uses
    "libxcb-cursor0",  # libxcb-cursor.so.0, the cursor shape extension libQt6XcbQpa needs: without
    # it Qt's xcb platform plugin cannot open a display, so the GUI refuses to start on a clean box.
    "libxcb-dri3-0",  # libxcb-dri3.so.0, QtWebEngine's direct rendering
    "libwayland-client0",
    "libwayland-cursor0",
    "libwayland-egl1",
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
