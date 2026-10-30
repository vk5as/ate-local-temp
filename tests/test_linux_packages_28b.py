"""Packet 28b: the Linux ``.tar.gz`` with ``install.sh`` and the self-contained ``.deb``.

Everything here runs against a *fake* bundle — a stub ``ate`` script shaped like the PyInstaller
one-directory build — so the packaging is tested in milliseconds without building anything:

* the tarball's layout and its ``install.sh``, which is run for real against a temporary prefix
  (the stub records the ``setup`` call it was given);
* the ``.deb``: contents, the ``/usr/bin/ate`` symlink, the control fields, and the maintainer
  scripts, which are syntax-checked with ``sh -n`` and then executed against a fake root;
* the Debian version string, including the ``0.0.0-dev`` fallback.

Two test-only environment variables exist, and only these scripts read them:

| Variable | What it does |
| --- | --- |
| ``ATE_DEB_ROOT`` | Prefixes ``/opt/ate``, ``/etc/ate`` and ``/var/lib/ate`` in ``postinst``/``postrm`` so they can run against a temporary root |
| ``STUB_LOG`` | File the stub ``ate`` script appends each invocation to |

Slow and off by default: the real build. ``ATE_BUILD_TESTS=1`` builds the bundle with
``packaging/build_pyinstaller.py``, packages it both ways, extracts the ``.deb`` with
``dpkg-deb -x`` and runs ``--version`` and ``_selftest`` from the extracted ``/opt/ate``.
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

REPO = Path(__file__).resolve().parent.parent
BUILD_LINUX = REPO / "packaging" / "linux" / "build_linux.py"
INSTALL_SH = REPO / "packaging" / "linux" / "install.sh"
POSTINST = REPO / "packaging" / "linux" / "deb" / "postinst"
POSTRM = REPO / "packaging" / "linux" / "deb" / "postrm"

# The libraries the bundle cannot carry: glibc, and the GL/X libraries Qt is linked against.
# Everything else (Qt, ICU, NSS, fontconfig, glib, libX11) is inside ``_internal``, which is why
# the package needs no Debian Python or Qt packages at all.
REQUIRED_DEPENDS = {
    "libc6",
    "libgl1",
    "libegl1",
    "libdrm2",
    "libxcb1",
    "libxcb-dri3-0",
    "libwayland-client0",
    "libwayland-cursor0",
    "libwayland-egl1",
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
    """A bundle shaped like ``dist/ate``: the executable plus an ``_internal`` directory."""
    bundle = root / "dist" / "ate"
    (bundle / "_internal" / "PyQt6" / "Qt6" / "lib").mkdir(parents=True)
    (bundle / "_internal" / "ate").mkdir(parents=True)
    exe = bundle / "ate"
    exe.write_text(script, encoding="utf-8")
    exe.chmod(0o755)
    (bundle / "_internal" / "base_library.zip").write_text("not really a zip", encoding="utf-8")
    (bundle / "_internal" / "PyQt6" / "Qt6" / "lib" / "libQt6Core.so.6").write_text("stub", encoding="utf-8")
    (bundle / "_internal" / "ate" / "version.txt").write_text("1.2.3", encoding="utf-8")
    return bundle


def _package(bundle: Path, out: Path) -> tuple[Path, Path]:
    """Run the packaging script on ``bundle`` and return ``(tarball, deb)``."""
    proc = subprocess.run(
        [sys.executable, str(BUILD_LINUX), "--dist", str(bundle.parent), "--out", str(out)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, f"build_linux.py failed: {proc.stdout}\n{proc.stderr}"
    tarball = next(p for p in out.iterdir() if p.suffix == ".gz")
    deb = next(p for p in out.iterdir() if p.suffix == ".deb")
    return tarball, deb


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
    (root / "etc" / "ate" / "config.json").write_text('{"format": 1}', encoding="utf-8")
    data = root / "var" / "lib" / "ate"
    (data / "reports").mkdir(parents=True)
    (data / "logs").mkdir(parents=True)
    (data / "ate.db").write_text("the record of every Test Run", encoding="utf-8")
    (data / "reports" / "run.html").write_text("a report", encoding="utf-8")
    (data / "logs" / "app.log").write_text("a log", encoding="utf-8")
    return root


def _run_script(script: Path, arg: str, root: Path, log: Path | None = None) -> subprocess.CompletedProcess:
    """Run a maintainer script against a fake root, exactly as dpkg would."""
    env = dict(os.environ, ATE_DEB_ROOT=str(root))
    if log is not None:
        env["STUB_LOG"] = str(log)
    return subprocess.run(["/bin/sh", str(script), arg], env=env, capture_output=True, text=True, timeout=60)


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
    (station_data / "ate.db").write_text("records", encoding="utf-8")
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

    # install.sh never removes config or data: every path it deletes is one it created.
    for line in INSTALL_SH.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("rm"):
            assert "/etc/ate" not in line and "/var/lib/ate" not in line, line

    # --uninstall removes the program only.
    removed = subprocess.run(
        ["/bin/sh", str(here / "install.sh"), "--uninstall", "--prefix", str(prefix), "--bin", str(link)],
        env=dict(os.environ, STUB_LOG=str(log)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert not prefix.exists() and not link.exists(), "uninstall left the program behind"
    assert (station_data / "ate.db").is_file(), "uninstall removed Station data it does not own"


def test_install_sh_defaults_to_usr_bin_when_root(tmp_path: Path) -> None:
    """install.sh links /usr/bin/ate as root; --bin aims it elsewhere for tests."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "/usr/bin/ate" in text, "install.sh must default to /usr/bin/ate"
    assert "id -u" in text, "install.sh must detect that it is not root"

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
    depends = {d.strip() for d in control["Depends"].split(",")}
    missing = sorted(REQUIRED_DEPENDS - depends)
    assert not missing, f"Depends is missing {missing}; got {sorted(depends)}"
    assert "Installed-Size" in control, "the package does not say how big it is"


def test_the_deb_version_is_a_valid_debian_version(tmp_path: Path) -> None:
    """``0.0.0-dev`` is not something dpkg accepts as-is; it becomes ``0.0.0~dev``."""
    spec = importlib.util.spec_from_file_location("build_linux", BUILD_LINUX)
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)

    assert build._deb_version("1.2.3") == "1.2.3"
    assert build._deb_version("0.0.0-dev") == "0.0.0~dev"
    assert build._deb_version("1.2.3.dev4+gabc123de") == "1.2.3.dev4+gabc123de"
    assert build._deb_version("") == "0.0.0"

    # A bundle whose --version prints the untagged fallback still builds a package dpkg accepts.
    bundle = tmp_path / "dist" / "ate"
    bundle.mkdir(parents=True)
    exe = bundle / "ate"
    exe.write_text(FROZEN_STUB, encoding="utf-8")
    exe.chmod(0o755)
    _, deb = _package(bundle, tmp_path / "out")
    assert deb.name == "ate_0.0.0~dev_amd64.deb", deb.name
    assert _control(deb)["Version"] == "0.0.0~dev"


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
    stub.write_text(FROZEN_STUB, encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "stub.log"
    proc = _run_script(control / "postinst", "configure", root, log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert log.read_text(encoding="utf-8").split() == ["setup"], log.read_text(encoding="utf-8")

    # Anything else (remove, abort) does nothing.
    quiet = _run_script(control / "postinst", "remove", root, log)
    assert quiet.returncode == 0 and len(log.read_text(encoding="utf-8").split()) == 1


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
    (empty / "etc" / "ate" / "config.json").write_text("{}", encoding="utf-8")
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


def test_no_packaging_output_lands_in_the_checkout(tmp_path: Path) -> None:
    """The build writes only where it was told to."""
    before = {p.name for p in REPO.iterdir() if p.name in ("dist", "build")}
    _package(_fake_bundle(tmp_path), tmp_path / "out")
    after = {p.name for p in REPO.iterdir() if p.name in ("dist", "build")}
    assert before == after, "packaging wrote into the checkout"


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
        assert REQUIRED_DEPENDS <= depends, f"the real package's Depends is {sorted(depends)}"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
