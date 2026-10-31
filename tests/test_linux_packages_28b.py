"""Packet 28b: the Linux ``.tar.gz`` with ``install.sh`` and the self-contained ``.deb``.

Everything here runs against a *fake* bundle — a stub ``ate`` script shaped like the PyInstaller
one-directory build — so the packaging is tested in milliseconds without building anything:

* the tarball's layout and its ``install.sh``, which is run for real against a temporary prefix
  (the stub records the ``setup`` call it was given);
* the ``.deb``: contents, the ``/usr/bin/ate`` symlink, the control fields, and the maintainer
  scripts, which are syntax-checked with ``sh -n`` and then run against a temporary root. The
  scripts have no test seam - they name the real paths - so the test runs a *copy* of each with
  ``/opt/ate``, ``/etc/ate`` and ``/var/lib/ate`` rewritten to a temporary root;
* the Debian version string, including the ``0.0.0-dev`` fallback and the prerelease mapping,
  checked with `dpkg --compare-versions`;
* the tarball's ownership and modes, built from a copy of the packaging scripts so the checkout's
  own file bits cannot make it pass.

Only one environment variable is test-only, and only the test's stub reads it:

| Variable | What it does |
| --- | --- |
| ``STUB_LOG`` | File the stub ``ate`` script appends each invocation to |

Slow and off by default: the real build. ``ATE_BUILD_TESTS=1`` builds the bundle with
``packaging/build_pyinstaller.py``, packages it both ways, extracts the ``.deb`` with
``dpkg-deb -x``, runs ``--version`` and ``_selftest`` from the extracted ``/opt/ate``, and runs
``ldd`` over every ``.so`` in it so that ``Depends`` must cover every library the bundle cannot
resolve on its own.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

if os.name != "posix":
    # install.sh, dpkg-deb and /bin/sh do not exist on a Windows runner: the packaging these
    # tests exercise is Linux-only, so running them there would fail for the wrong reason.
    pytest.skip("the tarball's install.sh and the .deb are POSIX-only", allow_module_level=True)

REPO = Path(__file__).resolve().parent.parent
BUILD_LINUX = REPO / "packaging" / "linux" / "build_linux.py"
BUILD_PYINSTALLER = REPO / "packaging" / "build_pyinstaller.py"
INSTALL_SH = REPO / "packaging" / "linux" / "install.sh"
POSTINST = REPO / "packaging" / "linux" / "deb" / "postinst"
POSTRM = REPO / "packaging" / "linux" / "deb" / "postrm"

# What the bundle cannot carry: glibc, GL/EGL, the X libraries the platform plugin needs, and a font.
# Verified by installing the .deb in a clean debian:stable container and running `ate _selftest --run`
# (packaging/linux/docker_smoke.py): libgl1 and libegl1 are needed (without them the selftest dies on
# libGL.so.1), and a font is needed (without fontconfig/fonts-dejavu-core the report renders as a
# 1.7 KB PDF with no text in it). libdrm and libxcb come in as apt's own dependencies of
# libgl1/libegl1, so naming them here would only repeat what apt already does.
#
# The six X libraries are not apt's doing and the selftest cannot see them: they are the xcb platform
# plugin's own dependencies, which a headless render never loads. The bundle built in a debian:stable
# container and installed without them aborts under `xvfb-run` with "Could not load the Qt platform
# plugin \"xcb\"", and starts once they are installed (2026-10-31). Whether they are inside
# `_internal` depends on the build machine, so the package asks for them.
REQUIRED_DEPENDS = {
    "libc6",
    "libgl1",
    "libegl1",
    "libxcb-cursor0",
    "libxcb-icccm4",
    "libxcb-keysyms1",
    "libxcb-shape0",
    "libxcb-xkb1",
    "libxkbcommon-x11-0",
    "fontconfig",
    "fonts-dejavu-core",
}

# What apt installs on top of those five in debian:stable. `test_libraries_the_bundle_cannot_resolve`
# uses it to check that every library the bundle links against but does not ship is still present on
# a machine that installed the package.
APT_INSTALLS = {
    "libdrm2",
    "libglx",
    "libxcb1",
    "libxcb-dri3-0",
    "libxcb-render0",
    "libxcb-shm0",
    "libx11-6",
    "libxext6",
    "libwayland-client0",
    "libxkbcommon0",
    "libfreetype6",
    "libpng16-16t64",
    "mesa-libgallium",
}

# soname -> the Debian package that provides it, for every library a `ldd` over the real bundle
# reports as `not found` even with `_internal` on the search path. `test_libraries_the_bundle_cannot_
# resolve` derives the set from the built bundle itself, so a Depends list that no longer covers the
# libraries the bundle actually loads fails.
SONAME_PACKAGES = {
    "libc.so.6": "libc6",
    "libm.so.6": "libc6",
    "libdl.so.2": "libc6",
    "libpthread.so.0": "libc6",
    "libresolv.so.2": "libc6",
    "libGL.so.1": "libgl1",
    "libGLX.so.0": "mesa-libgallium",
    "libGLdispatch.so.0": "mesa-libgallium",
    "libEGL.so.1": "libegl1",
    "libdrm.so.2": "libdrm2",
    "libxcb.so.1": "libxcb1",
    "libxcb-cursor.so.0": "libxcb-cursor0",  # the xcb platform plugin: the GUI will not start without it
    "libxcb-icccm.so.4": "libxcb-icccm4",
    "libxcb-keysyms.so.1": "libxcb-keysyms1",
    "libxcb-shape.so.0": "libxcb-shape0",
    "libxcb-xkb.so.1": "libxcb-xkb1",
    "libxkbcommon-x11.so.0": "libxkbcommon-x11-0",
    "libfontconfig.so.1": "fontconfig",
    "libfreetype.so.6": "libfreetype6",
}

# Libraries the bundle is linked against but never opens: the Wayland platform plugins, the extra
# xcb helpers the X plugin pulls in itself, and the GTK/CUPS print backend. A machine without them
# still runs the program, which is why they are not in Depends. The six xcb libraries the platform
# plugin needs directly (cursor, icccm, keysyms, shape, xkb, xkbcommon-x11) are not on this list: a
# Station with a screen loads them, so the package depends on them.
NEVER_OPENED = {
    "libatk-1.0.so.0",
    "libcairo-gobject.so.2",
    "libcups.so.2",
    "libgdk-3.so.0",
    "libgdk_pixbuf-2.0.so.0",
    "libgtk-3.so.0",
    "libharfbuzz.so.0",
    "libpango-1.0.so.0",
    "libpangocairo-1.0.so.0",
    "libwayland-cursor.so.0",
    "libwayland-egl.so.1",
    "libxcb-image.so.0",
    "libxcb-render-util.so.0",
    "libxcb-util.so.1",
}

# The stub stands in for the frozen executable: it prints a version and records every call.
STUB_ATE = """#!/bin/sh
echo "1.2.3"
if [ "${STUB_LOG:-}" ]; then
    for arg in "$@"; do printf '%s\\n' "$arg" >> "$STUB_LOG"; done
fi
exit 0
"""

FROZEN_STUB = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "0.0.0-dev"; exit 0; fi
if [ "${STUB_LOG:-}" ]; then printf '%s\\n' "$1" >> "$STUB_LOG"; fi
exit 0
"""


def _fake_bundle(root: Path, script: str = STUB_ATE) -> Path:
    """A bundle shaped like ``dist/ate``: the executable, an ``_internal`` directory, and the parts
    `packaging/build_pyinstaller.py`'s `verify_bundle` insists on, so packaging has something to
    accept."""
    bundle = root / "dist" / "ate"
    internal = bundle / "_internal"
    (internal / "PyQt6" / "Qt6" / "lib").mkdir(parents=True)
    (internal / "PyQt6" / "Qt6" / "plugins" / "platforms").mkdir(parents=True)
    (internal / "PyQt6" / "Qt6" / "libexec").mkdir(parents=True)
    (internal / "PyQt6" / "Qt6" / "resources").mkdir(parents=True)
    (internal / "ate" / "lua").mkdir(parents=True)
    (internal / "ate" / "report" / "templates").mkdir(parents=True)
    exe = bundle / "ate"
    exe.write_text(script, encoding="utf-8", newline="\n")
    exe.chmod(0o755)
    (internal / "base_library.zip").write_text("not really a zip", encoding="utf-8", newline="\n")
    (internal / "PyQt6" / "Qt6" / "lib" / "libQt6Core.so.6").write_text("stub", encoding="utf-8", newline="\n")
    (internal / "PyQt6" / "Qt6" / "plugins" / "platforms" / "libqoffscreen.so").write_text("stub", encoding="utf-8", newline="\n")
    (internal / "PyQt6" / "Qt6" / "libexec" / "QtWebEngineProcess").write_text("stub", encoding="utf-8", newline="\n")
    (internal / "PyQt6" / "Qt6" / "resources" / "icudtl.dat").write_text("stub", encoding="utf-8", newline="\n")
    (internal / "PyQt6" / "Qt6" / "resources" / "qtwebengine_resources.pak").write_text("stub", encoding="utf-8", newline="\n")
    (internal / "ate" / "lua" / "devices.lua").write_text("-- bundled", encoding="utf-8", newline="\n")
    (internal / "ate" / "report" / "templates" / "report.html.j2").write_text("<html>", encoding="utf-8", newline="\n")
    (internal / "ate" / "version.txt").write_text("1.2.3", encoding="utf-8", newline="\n")
    return bundle


def _package(bundle: Path, out: Path, *, tmpdir: Path | None = None) -> tuple[Path, Path]:
    """Run the packaging script on ``bundle`` and return ``(tarball, deb)``.

    ``tmpdir`` aims the subprocess's `TMPDIR` at the test's own directory, so a staging directory
    the script forgets to remove is a leftover this test can see rather than one in the shared `/tmp`.
    """
    env = dict(os.environ, TMPDIR=str(tmpdir)) if tmpdir else dict(os.environ)
    proc = subprocess.run(
        [sys.executable, str(BUILD_LINUX), "--dist", str(bundle.parent), "--out", str(out)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, f"build_linux.py failed: {proc.stdout}\n{proc.stderr}"
    tarball = next(p for p in out.iterdir() if p.suffix == ".gz")
    deb = next(p for p in out.iterdir() if p.suffix == ".deb")
    return tarball, deb


def test_packaging_refuses_a_bundle_whose_hooks_collected_nothing(tmp_path: Path) -> None:
    """A bundle with an executable but no Qt in it is not ATE, and packaging must say so (30d review).

    PyInstaller writes an executable even when its PyQt6 hooks imported nothing, so `dist/ate/ate`
    exists, `--version` prints, and the `.deb` ships a program that aborts at the first Test Report.
    Take the platform plugin, the WebEngine helper and its resources out of a bundle and packaging
    stops, with the missing part named and no artifacts written.
    """
    bundle = _fake_bundle(tmp_path)
    internal = bundle / "_internal"
    out = tmp_path / "out"
    out.mkdir(parents=True)
    shutil.rmtree(internal / "PyQt6" / "Qt6" / "plugins" / "platforms")
    (internal / "PyQt6" / "Qt6" / "libexec" / "QtWebEngineProcess").unlink()
    shutil.rmtree(internal / "PyQt6" / "Qt6" / "resources")

    proc = subprocess.run(
        [sys.executable, str(BUILD_LINUX), "--dist", str(bundle.parent), "--out", str(out)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode != 0, "packaging shipped a bundle with no Qt in it"
    text = proc.stdout + proc.stderr
    assert "platform plugin" in text, text
    assert not [p for p in out.iterdir()], f"a broken bundle was packaged as {sorted(p.name for p in out.iterdir())}"


def test_the_build_script_checks_a_bundle_it_is_only_asked_to_verify(tmp_path: Path) -> None:
    """`build_pyinstaller.py --verify` accepts a complete bundle and refuses an incomplete one."""
    bundle = _fake_bundle(tmp_path)
    exe = bundle / "ate"
    good = subprocess.run([sys.executable, str(BUILD_PYINSTALLER), "--verify", str(exe)], capture_output=True, text=True, timeout=300)
    assert good.returncode == 0, good.stdout + good.stderr
    assert "qt platform plugins" in good.stdout, good.stdout

    shutil.rmtree(bundle / "_internal" / "PyQt6" / "Qt6" / "plugins" / "platforms")
    bad = subprocess.run([sys.executable, str(BUILD_PYINSTALLER), "--verify", str(exe)], capture_output=True, text=True, timeout=300)
    assert bad.returncode != 0, "a bundle with no Qt platform plugin verified clean"
    assert "PyQt6 could not be imported" in bad.stdout + bad.stderr, bad.stdout + bad.stderr


def _control_text(deb: Path) -> str:
    """The package's DEBIAN/control as text."""
    where = Path(tempfile.mkdtemp(prefix="ate-deb-control-"))
    try:
        subprocess.run(["dpkg-deb", "-e", str(deb), str(where)], check=True)
        return (where / "control").read_text(encoding="utf-8")
    finally:
        shutil.rmtree(where, ignore_errors=True)


def _control(deb: Path) -> dict[str, str]:
    """DEBIAN/control as a mapping; a line starting with a space continues the field above it."""
    fields: dict[str, str] = {}
    for line in _control_text(deb).splitlines():
        if line.startswith(" ") and fields:
            fields[list(fields)[-1]] += "\n" + line
        elif ":" in line:
            key, _, value = line.partition(":")
            fields[key] = value.strip()
    return fields


def _contents(deb: Path) -> list[str]:
    """Every entry line of ``dpkg-deb -c``, as printed."""
    return subprocess.run(["dpkg-deb", "-c", str(deb)], capture_output=True, text=True, check=True).stdout.splitlines()


def _extract_control(deb: Path, where: Path) -> Path:
    """The directory dpkg-deb -e unpacks a ``.deb``'s control archive into."""
    subprocess.run(["dpkg-deb", "-e", str(deb), str(where)], check=True)
    return where


def _fake_station_root(root: Path) -> Path:
    """A machine whose Station already has a config, a database, reports and logs."""
    (root / "etc" / "ate").mkdir(parents=True)
    (root / "etc" / "ate" / "config.json").write_text('{"format": 1}', encoding="utf-8", newline="\n")
    data = root / "var" / "lib" / "ate"
    (data / "reports").mkdir(parents=True)
    (data / "logs").mkdir(parents=True)
    (data / "ate.db").write_text("the record of every Test Run", encoding="utf-8", newline="\n")
    (data / "reports" / "run.html").write_text("a report", encoding="utf-8", newline="\n")
    (data / "logs" / "app.log").write_text("a log", encoding="utf-8", newline="\n")
    return root


def _run_script(script: Path, arg: str, root: Path, log: Path | None = None) -> subprocess.CompletedProcess:
    """Run a maintainer script against a fake root, exactly as dpkg would.

    The shipped scripts carry no test seam: they name the real paths. The test copies the script
    and rewrites those paths to a temporary root before running it, so nothing in production
    reads a test-only variable.
    """
    text = script.read_text(encoding="utf-8")
    for real, fake in (("/opt/ate", str(root / "opt" / "ate")), ("/etc/ate", str(root / "etc" / "ate")), ("/var/lib/ate", str(root / "var" / "lib" / "ate"))):
        text = text.replace(real, fake)
    copy = root / f"{script.name}-test"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text(text, encoding="utf-8", newline="\n")
    copy.chmod(0o755)
    env = dict(os.environ)
    if log is not None:
        env["STUB_LOG"] = str(log)
    return subprocess.run(["/bin/sh", str(copy), arg], env=env, capture_output=True, text=True, timeout=60)


def _unresolved_sonames(bundle: Path) -> set[str]:
    """Every soname `ldd` reports as not found with the bundle's own `_internal` on the search path.

    `_internal` is on the path so a library the bundle ships counts as resolved: what is left is
    exactly what has to come from the system, which is what Depends and apt have to supply.
    """
    internal = bundle / "_internal"
    targets = [bundle / "ate"] + [p for p in bundle.rglob("*") if p.is_file() and ".so" in p.name]
    env = dict(os.environ, LD_LIBRARY_PATH=str(internal))
    unresolved: set[str] = set()
    for target in targets:
        proc = subprocess.run(["ldd", str(target)], capture_output=True, text=True, timeout=120, env=env)
        for line in proc.stdout.splitlines():
            if "=> not found" in line or "=> (null)" in line:
                unresolved.add(line.split("=>")[0].split("(")[0].strip())
    return unresolved


# The scripts are shipped with the executable bit and must be valid sh: a maintainer script that
# dpkg cannot run is a package that installs nothing.
def test_the_packaging_scripts_exist_and_parse() -> None:
    for script in (INSTALL_SH, POSTINST, POSTRM):
        assert script.is_file(), f"{script.relative_to(REPO)} is missing"
        assert script.stat().st_mode & 0o111, f"{script.name} is not executable"
        checked = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True, check=False)
        assert checked.returncode == 0, f"sh -n {script.name}: {checked.stderr}"


def test_the_tarball_carries_the_bundle_and_install_sh(tmp_path: Path) -> None:
    """The tarball is ``ate-<version>-linux-x86_64.tar.gz`` with ``ate/`` and ``install.sh`` inside."""
    bundle = _fake_bundle(tmp_path)
    tarball, _ = _package(bundle, tmp_path / "out")

    assert tarball.name == "ate-1.2.3-linux-x86_64.tar.gz", tarball.name
    with tarfile.open(tarball) as tar:
        members = {m.name for m in tar}
    assert "install.sh" in members, sorted(members)
    assert "ate/ate" in members and "ate/_internal/base_library.zip" in members, sorted(members)
    assert "dist" not in {m.split("/")[0] for m in members}, "the tarball carries dist/ itself"
    assert any(m.startswith("ate/_internal/PyQt6/") for m in members), "the nested _internal tree was flattened"


def test_the_tarball_is_root_owned_and_install_sh_is_executable(tmp_path: Path) -> None:
    """Members are root-owned and install.sh is 0755, whatever the checkout's own bits are.

    Built from a *copy* of the packaging scripts with install.sh made non-executable, so the
    packaging's normalisation is what is being tested and not the file mode in the repository.
    """
    here = tmp_path / "packaging" / "linux"
    here.mkdir(parents=True)
    shutil.copyfile(BUILD_LINUX, here / "build_linux.py")
    # build_linux.py finds build_pyinstaller.py relative to itself, so the copy needs it beside it:
    # the bundle check is part of packaging, not something the copy can reach in the checkout.
    shutil.copyfile(BUILD_PYINSTALLER, here.parent / "build_pyinstaller.py")
    shutil.copyfile(INSTALL_SH, here / "install.sh")
    (here / "install.sh").chmod(0o644)
    (here / "deb").mkdir()
    shutil.copyfile(POSTINST, here / "deb" / "postinst")
    shutil.copyfile(POSTRM, here / "deb" / "postrm")

    bundle = _fake_bundle(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(here / "build_linux.py"), "--dist", str(bundle.parent), "--out", str(tmp_path / "out")],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    tarball = next(p for p in (tmp_path / "out").iterdir() if p.suffix == ".gz")
    with tarfile.open(tarball) as tar:
        infos = {m.name: m for m in tar}
    assert infos["install.sh"].mode == 0o755, f"install.sh ships at {oct(infos['install.sh'].mode)}"
    unowned = sorted(name for name, info in infos.items() if info.uid or info.gid)
    assert not unowned, f"tar members carry the build machine's uid: {unowned}"

    extracted = tmp_path / "extracted"
    with tarfile.open(tarball) as tar:
        tar.extractall(extracted, filter="data")
    assert (extracted / "install.sh").stat().st_mode & 0o777 == 0o755, "extracting gives a non-executable installer"



def test_install_sh_installs_links_and_runs_ate_setup(tmp_path: Path) -> None:
    """install.sh copies the bundle to the prefix, links ``ate``, and runs ``ate setup``."""
    bundle = _fake_bundle(tmp_path)
    tarball, _ = _package(bundle, tmp_path / "out")
    here = tmp_path / "extracted"
    here.mkdir()
    with tarfile.open(tarball) as tar:
        tar.extractall(here, filter="data")

    prefix = tmp_path / "station" / "opt" / "ate"
    log = tmp_path / "stub.log"
    proc = subprocess.run(
        ["/bin/sh", str(here / "install.sh"), "--prefix", str(prefix)],
        env=dict(os.environ, STUB_LOG=str(log)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    exe = prefix / "ate"
    assert exe.is_file() and exe.stat().st_mode & 0o111, "the bundle's ate is not in the prefix"
    assert (prefix / "_internal" / "base_library.zip").is_file(), "the _internal directory did not copy"
    link = prefix / "bin" / "ate"  # unprivileged: the link goes in <prefix>/bin
    assert link.is_symlink() and os.readlink(link) == str(exe), "no link to the bundle"
    assert log.read_text(encoding="utf-8").split() == ["setup"], "install.sh did not run ate setup"

    # Re-running is an upgrade: the bundle is replaced, the Station's files are untouched.
    station_data = tmp_path / "var" / "lib" / "ate"
    station_data.mkdir(parents=True)
    (station_data / "ate.db").write_text("records", encoding="utf-8", newline="\n")
    again = subprocess.run(
        ["/bin/sh", str(here / "install.sh"), "--prefix", str(prefix)],
        env=dict(os.environ, STUB_LOG=str(log)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert again.returncode == 0, again.stdout + again.stderr
    assert log.read_text(encoding="utf-8").split() == ["setup", "setup"], "the upgrade did not re-run setup"
    assert (station_data / "ate.db").is_file(), "an upgrade deleted the Station's data"

    # --uninstall removes the program only. The planted files are inside the prefix: install.sh can
    # only ever reach the prefix, so this is the safe way to prove it deletes nothing it did not
    # install (the real /etc/ate and /var/lib/ate are never named by these tests).
    (prefix / "etc" / "ate").mkdir(parents=True)
    (prefix / "etc" / "ate" / "config.json").write_text('{"format": 1}', encoding="utf-8", newline="\n")
    (prefix / "var" / "lib" / "ate").mkdir(parents=True)
    (prefix / "var" / "lib" / "ate" / "ate.db").write_text("records", encoding="utf-8", newline="\n")
    removed = subprocess.run(
        ["/bin/sh", str(here / "install.sh"), "--uninstall", "--prefix", str(prefix), "--bin", str(link)],
        env=dict(os.environ, STUB_LOG=str(log)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert not (prefix / "ate").exists() and not (prefix / "_internal").exists(), "uninstall left the bundle behind"
    assert not link.exists(), "uninstall left the command link behind"
    assert (prefix / "etc" / "ate" / "config.json").is_file(), "uninstall deleted config it does not own"
    assert (prefix / "var" / "lib" / "ate" / "ate.db").is_file(), "uninstall deleted data it does not own"
    assert prefix.exists(), "uninstall removed a prefix that still held files it never created"
    assert (station_data / "ate.db").is_file(), "uninstall removed Station data it does not own"


def test_an_upgrade_replaces_the_bundle_instead_of_merging_into_it(tmp_path: Path) -> None:
    """A stale file in _internal is gone after re-running install.sh: the bundle is replaced."""
    bundle = _fake_bundle(tmp_path)
    tarball, _ = _package(bundle, tmp_path / "out")
    here = tmp_path / "extracted"
    here.mkdir()
    with tarfile.open(tarball) as tar:
        tar.extractall(here, filter="data")

    prefix = tmp_path / "prefix"
    log = tmp_path / "stub.log"
    first = subprocess.run(["/bin/sh", str(here / "install.sh"), "--prefix", str(prefix)], env=dict(os.environ, STUB_LOG=str(log)), capture_output=True, text=True, timeout=60)
    assert first.returncode == 0, first.stdout + first.stderr

    stale = prefix / "_internal" / "from_the_previous_version.py"
    stale.write_text("left behind by the old cp -a merge", encoding="utf-8", newline="\n")
    nested = prefix / "_internal" / "PyQt6" / "Qt6" / "lib" / "libQt6Old.so.6"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text("old library", encoding="utf-8", newline="\n")

    again = subprocess.run(["/bin/sh", str(here / "install.sh"), "--prefix", str(prefix)], env=dict(os.environ, STUB_LOG=str(log)), capture_output=True, text=True, timeout=60)
    assert again.returncode == 0, again.stdout + again.stderr
    assert not stale.exists(), "an upgrade left a file of the previous version in _internal"
    assert not nested.exists(), "an upgrade left an old library in the nested _internal tree"
    assert (prefix / "_internal" / "base_library.zip").is_file(), "the new bundle did not land in the prefix"
    assert (prefix / "bin" / "ate").is_symlink(), "replacing the bundle deleted the bin/ link inside the prefix"


def test_install_sh_needs_a_value_for_prefix_and_bin(tmp_path: Path) -> None:
    """`--prefix` or `--bin` with nothing after it is a usage error, not a silent empty path."""
    bundle = _fake_bundle(tmp_path)
    tarball, _ = _package(bundle, tmp_path / "out")
    here = tmp_path / "extracted"
    here.mkdir()
    with tarfile.open(tarball) as tar:
        tar.extractall(here, filter="data")

    for flag in ("--prefix", "--bin"):
        proc = subprocess.run(["/bin/sh", str(here / "install.sh"), flag], capture_output=True, text=True, timeout=60)
        assert proc.returncode == 2, f"{flag} alone exited {proc.returncode}: {proc.stdout}\n{proc.stderr}"
        assert "needs a value" in proc.stderr, proc.stderr + proc.stdout
        empty = subprocess.run(["/bin/sh", str(here / "install.sh"), flag, ""], capture_output=True, text=True, timeout=60)
        assert empty.returncode == 2, f"{flag} '' exited {empty.returncode}: {empty.stdout}\n{empty.stderr}"


def test_uninstall_refuses_a_prefix_it_does_not_own(tmp_path: Path) -> None:
    """--uninstall refuses /, an empty prefix, and a directory with no ATE bundle in it."""
    here = tmp_path / "extracted"
    here.mkdir()
    shutil.copyfile(INSTALL_SH, here / "install.sh")

    unowned = tmp_path / "somewhere"
    unowned.mkdir()
    (unowned / "keep.txt").write_text("not an ATE install", encoding="utf-8", newline="\n")

    for prefix in ("/", "", str(unowned), str(tmp_path / "never-installed")):
        proc = subprocess.run(
            ["/bin/sh", str(here / "install.sh"), "--uninstall", "--prefix", prefix],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode != 0, f"uninstall of {prefix!r} succeeded: {proc.stdout}\n{proc.stderr}"
    assert (unowned / "keep.txt").is_file(), "--uninstall deleted a directory it did not install"
    assert tmp_path.exists(), "--uninstall wiped the tree it was pointed at"
    assert Path("/").exists(), "--uninstall tried to remove /"


def test_install_sh_says_so_when_ate_setup_fails(tmp_path: Path) -> None:
    """The program is installed even if setup fails, and the message says how to finish it."""
    bundle = _fake_bundle(tmp_path)
    tarball, _ = _package(bundle, tmp_path / "out")
    here = tmp_path / "extracted"
    here.mkdir()
    with tarfile.open(tarball) as tar:
        tar.extractall(here, filter="data")
    (here / "ate" / "ate").write_text(
        "#!/bin/sh\nif [ \"$1\" = setup ]; then echo \"cannot write /etc/ate/config.json\" >&2; exit 1; fi\nexit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    (here / "ate" / "ate").chmod(0o755)

    prefix = tmp_path / "prefix"
    proc = subprocess.run(["/bin/sh", str(here / "install.sh"), "--prefix", str(prefix)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert (prefix / "ate").is_file() and (prefix / "bin" / "ate").is_symlink(), "a failed setup left the program uninstalled"
    out = proc.stdout + proc.stderr
    assert "installed" in out and "not set up" in out, out
    assert "sudo ate setup" in out, out
    if os.geteuid() != 0:
        assert "not running as root" in proc.stdout, proc.stdout



def test_install_sh_defaults_to_usr_bin_when_root(tmp_path: Path) -> None:
    """install.sh links /usr/bin/ate as root; --bin aims it elsewhere for tests."""
    # The root branch cannot be exercised without root, so the default is checked as text; the
    # unprivileged branch is exercised for real below.
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "/usr/bin/ate" in text, "install.sh must default to /usr/bin/ate"
    assert "id -u" in text, "install.sh must detect that it is not root"
    assert "set -eu" in text, "install.sh must fail on an error or an unset variable"

    bundle = _fake_bundle(tmp_path)
    tarball, _ = _package(bundle, tmp_path / "out")
    here = tmp_path / "extracted"
    here.mkdir()
    with tarfile.open(tarball) as tar:
        tar.extractall(here, filter="data")
    prefix = tmp_path / "prefix"
    bin_dir = tmp_path / "usr" / "bin"
    log = tmp_path / "stub.log"
    proc = subprocess.run(
        ["/bin/sh", str(here / "install.sh"), "--prefix", str(prefix), "--bin", str(bin_dir / "ate")],
        env=dict(os.environ, STUB_LOG=str(log)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert os.readlink(bin_dir / "ate") == str(prefix / "ate")


def test_the_deb_layout_is_opt_ate_plus_the_usr_bin_link(tmp_path: Path) -> None:
    """``/opt/ate`` holds the bundle and ``/usr/bin/ate`` is a symlink to it, all root-owned."""
    _, deb = _package(_fake_bundle(tmp_path), tmp_path / "out")
    lines = _contents(deb)
    link = [line for line in lines if "./usr/bin/ate" in line]
    assert link, "no /usr/bin/ate in the package"
    entry = link[0]
    assert entry.startswith("l"), f"/usr/bin/ate is not a symlink: {entry}"
    assert entry.split(" -> ")[1] == "/opt/ate/ate", entry
    assert "root/root" in entry, f"{entry} is not root-owned"
    assert any("./opt/ate/ate" in line for line in lines), "the bundle's ate is not in /opt/ate"
    assert any("./opt/ate/_internal" in line for line in lines), "_internal is not in the package"
    assert not any("/etc/ate" in line for line in lines), "config.json is shipped, which it must not be"
    assert all("root/root" in line for line in lines), "--root-owner-group was not used"


def test_the_deb_control_fields_are_the_package_we_ship(tmp_path: Path) -> None:
    """Package name, version, architecture, maintainer, description and the dependency list."""
    _, deb = _package(_fake_bundle(tmp_path), tmp_path / "out")
    control = _control(deb)
    assert control["Package"] == "ate", control
    assert control["Version"] == "1.2.3", control
    assert control["Architecture"] == "amd64", control
    assert control.get("Maintainer"), "no maintainer"
    assert control.get("Description"), "no description"
    assert control.get("Section") in ("science", "electronics"), control
    assert control.get("Priority") == "optional", control
    depends = {d.strip() for d in control["Depends"].split(",")}
    extra = sorted(depends - REQUIRED_DEPENDS)
    missing = sorted(REQUIRED_DEPENDS - depends)
    assert not missing and not extra, f"Depends is {sorted(depends)}, expected {sorted(REQUIRED_DEPENDS)}"
    assert "Installed-Size" in control, "the package does not say how big it is"


def test_the_deb_version_is_a_valid_debian_version(tmp_path: Path) -> None:
    """Prerelease suffixes become `~…`, so a prerelease sorts before the release it precedes."""
    spec = importlib.util.spec_from_file_location("build_linux", BUILD_LINUX)
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)

    assert build._deb_version("1.2.3") == "1.2.3"
    assert build._deb_version("0.0.0-dev") == "0.0.0~dev"
    assert build._deb_version("1.2.3-rc1") == "1.2.3~rc1"
    assert build._deb_version("1.2.3-beta2") == "1.2.3~beta2"
    assert build._deb_version("1.2.3.dev4+gabc123de") == "1.2.3.dev4+gabc123de"
    assert build._deb_version("") == "0.0.0"

    # dpkg's own comparison decides the upgrade order, so ask dpkg rather than trust the string.
    def dpkg_lt(a: str, b: str) -> bool:
        return subprocess.run(["dpkg", "--compare-versions", a, "lt", b], check=False).returncode == 0

    assert dpkg_lt(build._deb_version("1.2.3-rc1"), build._deb_version("1.2.3")), "1.2.3~rc1 must sort before 1.2.3"
    assert dpkg_lt(build._deb_version("0.0.0-dev"), build._deb_version("0.0.0")), "a dev build must sort before 0.0.0"
    assert dpkg_lt(build._deb_version("1.2.3-beta2"), build._deb_version("1.2.3-rc1")), "beta precedes rc"
    assert dpkg_lt("0.0.0~dev", "0.0.0")

    # A bundle whose --version prints the untagged fallback still builds a package dpkg accepts.
    _, deb = _package(_fake_bundle(tmp_path, FROZEN_STUB), tmp_path / "out")
    assert deb.name == "ate_0.0.0~dev_amd64.deb", deb.name
    assert _control(deb)["Version"] == "0.0.0~dev"


def test_a_bundle_that_prints_no_version_is_refused(tmp_path: Path) -> None:
    """An empty `--version` would name the artifacts `ate--linux…` and `ate__amd64.deb`, so stop."""
    bundle = _fake_bundle(tmp_path)
    (bundle / "ate").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    out = tmp_path / "out"

    proc = subprocess.run(
        [sys.executable, str(BUILD_LINUX), "--dist", str(bundle.parent), "--out", str(out)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode != 0, f"a bundle with no version was packaged anyway: {proc.stdout}\n{proc.stderr}"
    assert "version" in (proc.stdout + proc.stderr).lower(), proc.stdout + proc.stderr
    artifacts = [p.name for p in out.iterdir()] if out.exists() else []
    assert not artifacts, f"artifacts were written with an empty version: {artifacts}"
    assert not any("ate--" in name or "ate_" == name[:5] for name in artifacts), artifacts



def test_the_maintainer_scripts_are_executable_and_valid_sh(tmp_path: Path) -> None:
    _, deb = _package(_fake_bundle(tmp_path), tmp_path / "out")
    control = _extract_control(deb, tmp_path / "control")
    for script in ("postinst", "postrm"):
        path = control / script
        assert path.is_file(), f"{script} is not in the package"
        assert path.stat().st_mode & 0o111, f"{script} is not executable in the package"
        checked = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True, check=False)
        assert checked.returncode == 0, f"sh -n {script}: {checked.stderr}"
    assert not (control / "conffiles").exists(), "config.json is not a conffile: setup writes it"


def test_postinst_runs_ate_setup_on_configure(tmp_path: Path) -> None:
    """dpkg runs postinst with ``configure``; that is where ``ate setup`` is called."""
    _, deb = _package(_fake_bundle(tmp_path), tmp_path / "out")
    control = _extract_control(deb, tmp_path / "control")
    root = tmp_path / "machine"
    (root / "opt" / "ate").mkdir(parents=True)
    stub = root / "opt" / "ate" / "ate"
    stub.write_text(FROZEN_STUB, encoding="utf-8", newline="\n")
    stub.chmod(0o755)
    log = tmp_path / "stub.log"
    proc = _run_script(control / "postinst", "configure", root, log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert log.read_text(encoding="utf-8").split() == ["setup"], log.read_text(encoding="utf-8")

    # Anything else (remove, reconfigure, abort) does nothing: `reconfigure` is dpkg-reconfigure,
    # not a package operation, and re-running setup on it is not what the operator asked for.
    for arg in ("remove", "reconfigure", "abort"):
        quiet = _run_script(control / "postinst", arg, root, log)
        assert quiet.returncode == 0, f"postinst {arg}: {quiet.stdout}\n{quiet.stderr}"
        assert len(log.read_text(encoding="utf-8").split()) == 1, f"postinst {arg} ran setup again"

    # A postinst that cannot find the bundle says so instead of pretending the Station is ready.
    broken = tmp_path / "broken"
    broken.mkdir()
    proc = _run_script(control / "postinst", "configure", broken, tmp_path / "broken.log")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "missing" in proc.stderr, proc.stderr


def test_the_maintainer_scripts_have_no_test_seam(tmp_path: Path) -> None:
    """The shipped scripts read no test variable: exporting one must not redirect them.

    postinst is run for real against a temporary root: ATE_DEB_ROOT is honoured nowhere, so it
    still looks for /opt/ate/ate, which this machine does not have, and fails. postrm cannot be
    run for real - it names /etc/ate - so it is checked as text instead.
    """
    _, deb = _package(_fake_bundle(tmp_path), tmp_path / "out")
    control = _extract_control(deb, tmp_path / "control")
    root = tmp_path / "machine"
    (root / "opt" / "ate").mkdir(parents=True)
    stub = root / "opt" / "ate" / "ate"
    stub.write_text(FROZEN_STUB, encoding="utf-8", newline="\n")
    stub.chmod(0o755)
    log = tmp_path / "stub.log"

    proc = subprocess.run(
        ["/bin/sh", str(control / "postinst"), "configure"],
        env=dict(os.environ, ATE_DEB_ROOT=str(root)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1, f"ATE_DEB_ROOT redirected postinst: {proc.stdout}\n{proc.stderr}"
    assert not log.exists(), "postinst ran the stub in the fake root"

    for script in (POSTINST, POSTRM, INSTALL_SH):
        assert "ATE_DEB_ROOT" not in script.read_text(encoding="utf-8"), f"{script.name} honours ATE_DEB_ROOT"



def test_postrm_remove_keeps_the_config_and_the_data(tmp_path: Path) -> None:
    _, deb = _package(_fake_bundle(tmp_path), tmp_path / "out")
    control = _extract_control(deb, tmp_path / "control")
    root = _fake_station_root(tmp_path / "machine")
    proc = _run_script(control / "postrm", "remove", root)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (root / "etc" / "ate" / "config.json").is_file(), "apt remove deleted the config"
    assert (root / "var" / "lib" / "ate" / "ate.db").is_file(), "apt remove deleted the database"
    assert (root / "var" / "lib" / "ate" / "reports" / "run.html").is_file(), "apt remove deleted reports"
    assert (root / "var" / "lib" / "ate" / "logs" / "app.log").is_file(), "apt remove deleted logs"


def test_postrm_purge_removes_config_and_never_the_records(tmp_path: Path) -> None:
    """purge drops the config; the database, reports and logs are records and stay."""
    _, deb = _package(_fake_bundle(tmp_path), tmp_path / "out")
    control = _extract_control(deb, tmp_path / "control")
    root = _fake_station_root(tmp_path / "machine")
    proc = _run_script(control / "postrm", "purge", root)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (root / "etc" / "ate" / "config.json").exists(), "purge did not remove the config"
    assert not (root / "etc" / "ate").exists(), "purge left an empty config directory behind"
    data = root / "var" / "lib" / "ate"
    assert (data / "ate.db").is_file(), "purge deleted the database"
    assert (data / "reports" / "run.html").is_file(), "purge deleted the reports"
    assert (data / "logs" / "app.log").is_file(), "purge deleted the logs"

    # A postrm that only purges the config cannot touch the data directory even if it is missing.
    empty = tmp_path / "empty"
    (empty / "etc" / "ate").mkdir(parents=True)
    (empty / "etc" / "ate" / "config.json").write_text("{}", encoding="utf-8", newline="\n")
    proc = _run_script(control / "postrm", "purge", empty)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (empty / "var" / "lib" / "ate").exists(), "postrm created a data directory it should not touch"


def test_postrm_is_a_no_op_for_other_invocations(tmp_path: Path) -> None:
    _, deb = _package(_fake_bundle(tmp_path), tmp_path / "out")
    control = _extract_control(deb, tmp_path / "control")
    root = _fake_station_root(tmp_path / "machine")
    proc = _run_script(control / "postrm", "upgrade", root)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (root / "etc" / "ate" / "config.json").is_file()


def test_packaging_writes_only_where_it_was_told(tmp_path: Path) -> None:
    """The build writes into --out and a staging directory it removes; the checkout stays as it is.

    The staging directory is looked for in this test's own temporary directory, not in the shared
    `/tmp`: another test or another run in progress puts its own `ate-deb-` directory there, and a
    scan of the shared directory would then fail this test for someone else's leftover. Remove the
    `finally: shutil.rmtree(stage)` in `_deb` and the leftover is visible here.
    """
    def snapshot() -> set[str]:
        return {str(p.relative_to(REPO)) for p in REPO.rglob("*") if ".git" not in p.parts}

    before = snapshot()
    _package(_fake_bundle(tmp_path), tmp_path / "out", tmpdir=tmp_path)
    assert snapshot() == before, "packaging wrote into the checkout"
    leftover = [p for p in tmp_path.iterdir() if p.name.startswith("ate-deb-")]
    assert not leftover, f"the .deb staging directory was left behind in {tmp_path}: {leftover}"


@pytest.mark.build
@pytest.mark.slow
def test_the_real_bundle_packages_and_runs_from_the_deb() -> None:
    """Build the frozen bundle, package it both ways, and run the extracted /opt/ate/ate."""
    if os.environ.get("ATE_BUILD_TESTS") != "1":
        pytest.skip("the frozen build takes minutes; set ATE_BUILD_TESTS=1 to run it")

    scratch = Path(tempfile.mkdtemp(prefix="ate-linux-pack-"))
    dist, work, out = scratch / "dist", scratch / "work", scratch / "artifacts"
    out.mkdir()
    try:
        built = subprocess.run(
            [sys.executable, str(REPO / "packaging" / "build_pyinstaller.py"), "--dist", str(dist), "--work", str(work)],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=3600,
        )
        assert built.returncode == 0, f"the frozen build failed: {built.stderr[-2000:]}"

        packaged = subprocess.run(
            [sys.executable, str(BUILD_LINUX), "--dist", str(dist), "--out", str(out)],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert packaged.returncode == 0, f"packaging failed: {packaged.stdout}\n{packaged.stderr[-2000:]}"
        print(packaged.stdout)

        tarball = next(p for p in out.iterdir() if p.suffix == ".gz")
        deb = next(p for p in out.iterdir() if p.suffix == ".deb")
        print(f"REAL BUILD SIZES: {tarball.name} {tarball.stat().st_size} bytes; {deb.name} {deb.stat().st_size} bytes")

        control = _control(deb)

        extracted = scratch / "extracted"
        subprocess.run(["dpkg-deb", "-x", str(deb), str(extracted)], check=True)
        exe = extracted / "opt" / "ate" / "ate"
        assert exe.is_file() and exe.stat().st_mode & 0o111
        version = subprocess.run(
            [str(exe), "--version"], env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), capture_output=True, text=True, timeout=300
        )
        assert version.returncode == 0, version.stderr
        print(f"REAL BUILD VERSION: {version.stdout.strip()}")
        assert re.search(r"\d+\.\d+\.\d+", version.stdout), version.stdout

        selftest = subprocess.run(
            [str(exe), "_selftest"], env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), capture_output=True, text=True, timeout=300
        )
        print(f"REAL BUILD SELFTEST: {selftest.stdout.strip()}")
        assert selftest.returncode == 0, selftest.stderr
        assert "MISSING" not in selftest.stdout, selftest.stdout

        depends = {d.strip() for d in control["Depends"].split(",")}
        assert depends == REQUIRED_DEPENDS, f"the real package's Depends is {sorted(depends)}"

        # Every library the bundle links against but does not carry has to be present on the machine
        # that installed the package: either apt brings it in as part of the five Depends, or the
        # bundle never opens it (the Wayland/xcb extras and the GTK print backend, with the Station
        # running headless). Run ldd over the whole extracted bundle with its own `_internal` on the
        # search path: cut `libegl1` from Depends and this fails, because libEGL.so.1 is then
        # unresolved and nothing else in the package supplies it.
        unresolved = _unresolved_sonames(extracted / "opt" / "ate")
        print(f"REAL BUILD UNRESOLVED LIBRARIES: {sorted(unresolved)}")
        unmapped = sorted(u for u in unresolved if u not in SONAME_PACKAGES and u not in NEVER_OPENED)
        assert not unmapped, f"nothing maps {unmapped}: add the Debian package to SONAME_PACKAGES or explain in NEVER_OPENED why the bundle never opens it"
        uncovered = sorted({SONAME_PACKAGES[u] for u in unresolved} - depends - APT_INSTALLS)
        assert not uncovered, f"Depends does not bring in {uncovered}: {sorted(unresolved)}"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
