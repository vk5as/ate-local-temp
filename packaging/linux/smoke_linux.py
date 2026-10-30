#!/usr/bin/env python3
r"""Smoke-test ATE's Linux artifacts in a clean Debian stable container (packet 28c).

Run by `.github/workflows/release.yml` on a tagged release, after `packaging/build_pyinstaller.py
--smoke` built the bundle, `packaging/linux/build_linux.py` packaged the `.tar.gz` and the `.deb`,
and `python -m build --wheel` made the wheel, and after the workflow downloaded the artifacts into
`artifacts/`:

    python3 packaging/linux/smoke_linux.py --kind deb --dist artifacts
    python3 packaging/linux/smoke_linux.py --kind tar --dist artifacts
    python3 packaging/linux/smoke_linux.py --kind wheel --dist artifacts

One artifact per run, from a container nothing else has touched: the `.deb` is installed with
`apt-get`, which proves its `Depends` resolve on Debian stable; the tarball is unpacked and
`install.sh` run, then run again (an upgrade) and finally with `--uninstall`; the wheel is installed
with `pipx`, which pulls PyQt6 and QtWebEngine from PyPI and needs the same GL/X libraries. Every
kind then has to leave a Station behind: `/etc/ate/config.json`, the database it names,
`ate db status` printing a schema version, `ate _selftest --run` running a Test Schedule, and
`ate _selftest --scan <definitions>` scanning the shipped example Media Release with nothing broken
and nothing mismatched (assets/packaging.md, "CI").

**RUN IN DEBIAN CONTAINERS, NEVER IN THE WORKFLOW.** `packaging/linux/docker_smoke.py` runs this
script in fresh `debian:stable` containers with `ATE_DOCKER_TESTS=1`: `dpkg` has installed the
`.deb`, `install.sh` has installed the tarball and `pipx` has installed the wheel. GitHub Actions
has still never run it, because no tag of this repository has been cut.
`tests/test_release_ci_28c.py` runs the same sequence against a fake container rooted in a
temporary directory — a modelled `apt-get` and `install.sh`, and the real source CLI for every
`ate` call — so it proves this script *notices* a broken install.

The two seams the tests use:

| Seam | What it does |
| --- | --- |
| `Runner` (injected into `run_smoke`) | Every command this script runs on the machine goes through it: `apt-get`, `tar`, `install.sh`, `pipx`, `ldd`, `ate`. The test replaces it with a fake container, so nothing is ever installed on the host that runs the tests. |
| `Station(root=...)` | The paths an installed ATE lives at (`/opt/ate`, `/usr/bin/ate`, `/etc/ate`, `/var/lib/ate`, per assets/packaging.md), rooted at a directory instead of `/`. In the container `root` is `/`, so these are the real paths. |

The container runs as root, which is what makes `ate setup`'s group and ownership path run for real;
`QT_QPA_PLATFORM=offscreen` is set by the workflow and by `SubprocessRunner` so QtWebEngine never
tries to open a window.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
BUILD_LINUX = HERE / "build_linux.py"
VERSION_FILE = REPO / "src" / "ate" / "_version.py"
SAMPLE_RELEASE = REPO / "examples" / "media-release"

KINDS = ("deb", "tar", "wheel")
# The names the artifacts are built with (packaging/linux/build_linux.py and the wheel's
# dist/ate-<version>-py3-none-any.whl). Keep them in step with the workflow's upload globs:
# tests/test_release_ci_28c.py checks the glob against the name the build actually produces.
ARTIFACT_GLOBS = {"deb": "ate_*_amd64.deb", "tar": "ate-*-linux-x86_64.tar.gz", "wheel": "ate-*.whl"}
GLIBC_MARKER = "glibc-build.txt"
MARKER = "28c-smoke-marker.txt"  # written in the data directory before an upgrade
STALE = Path("_internal") / "stale-from-the-previous-build.py"  # planted inside the bundle before an upgrade

SCAN_COUNTS = re.compile(r"(\d+)\s+schedules,\s+(\d+)\s+broken,\s+(\d+)\s+mismatched")
SCHEMA_SEEN = re.compile(r"schema (\d+) == (\d+)")
GLIBC_VERSION = re.compile(r"(\d+\.\d+)")


class SmokeError(Exception):
    """The first assertion that failed. The run stops there."""


def fail(message: str) -> None:
    raise SmokeError(message)


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    def output(self) -> str:
        return (self.stdout + self.stderr).strip()


class Runner:
    """Everything this script does to the machine goes through here.

    `SubprocessRunner` is the one the CI uses; the tests hand in a fake container. No command is
    run anywhere else in the script, so nothing destructive can reach the host.
    """

    def run(self, argv: list[str]) -> CommandResult:
        raise NotImplementedError


class SubprocessRunner(Runner):
    def __init__(self, env: dict[str, str] | None = None) -> None:
        # QtWebEngine is started by `ate _selftest --run` and by the bundle's `_pdf` helper; a
        # container has no display.
        self.env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        self.env.update(env or {})

    def run(self, argv: list[str]) -> CommandResult:
        print("$ " + " ".join(str(part) for part in argv))
        proc = subprocess.run([str(part) for part in argv], capture_output=True, text=True, env=self.env, timeout=1800)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)


@dataclass(frozen=True)
class Station:
    r"""Where an installed ATE lives on a Linux Station (assets/packaging.md, "Location").

    `root` is the test seam: in the container it is `/` and every path below is the real one.
    `home` is where pipx puts the wheel's `ate`.
    """

    root: Path = Path("/")
    home: Path | None = None

    @property
    def app_dir(self) -> Path:
        return self.root / "opt" / "ate"

    @property
    def bin(self) -> Path:
        return self.root / "usr" / "bin" / "ate"

    @property
    def config_dir(self) -> Path:
        return self.root / "etc" / "ate"

    @property
    def data_dir(self) -> Path:
        return self.root / "var" / "lib" / "ate"

    @property
    def config(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def wheel_bin(self) -> Path:
        return (self.home or Path.home()) / ".local" / "bin" / "ate"


def apt(runner: Runner, *args: str) -> CommandResult:
    result = runner.run(["apt-get", *args])
    if result.returncode != 0:
        fail(f"apt-get {' '.join(args)} failed (exit {result.returncode}): {result.output()[-400:]}")
    return result


def _packaging_module():
    spec = importlib.util.spec_from_file_location("ate_build_linux", BUILD_LINUX)
    if spec is None or spec.loader is None:
        fail(f"cannot read {BUILD_LINUX}: the runtime library lists have to come from the packaging script")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runtime_libs() -> tuple[str, ...]:
    """The `.deb`'s `Depends`, read from build_linux.py rather than copied into this script.

    The tarball needs exactly the libraries the package declares: glibc, the GL/X libraries the
    bundle is linked against but cannot carry, and a font, because a Debian container ships no
    fonts and QtWebEngine then renders a PDF with no text in it. Copying the list here would let
    the two drift apart, and a smoke run that installs the wrong list proves nothing.
    """
    depends = tuple(getattr(_packaging_module(), "DEPENDS", ()))
    if not depends:
        fail(f"{BUILD_LINUX} defines no DEPENDS: the runtime libraries cannot be installed")
    return depends


def wheel_libs() -> tuple[str, ...]:
    """The system libraries PyPI's PyQt6 and QtWebEngine need — more than the bundle's own Qt.

    The wheel runs on the container's CPython with PyQt6 from PyPI, which is linked against a
    wider set of X, GL, NSS and font libraries than the frozen bundle carries. The list lives in
    `packaging/linux/build_linux.py` (`WHEEL_SYSTEM_LIBS`) so the smoke run and
    docs/building-from-source.md install the same set. It was established in a fresh
    `debian:stable` container: with only the `.deb`'s Depends, `ate _selftest --run` dies there
    with "cannot open shared object file".
    """
    libs = tuple(getattr(_packaging_module(), "WHEEL_SYSTEM_LIBS", ()))
    if not libs:
        fail(f"{BUILD_LINUX} defines no WHEEL_SYSTEM_LIBS: the wheel's system libraries cannot be installed")
    return libs


def find_artifact(dist: Path, kind: str) -> Path:
    """The one artifact of this kind the build uploaded."""
    glob = ARTIFACT_GLOBS[kind]
    found = sorted(dist.glob(glob))
    if not found:
        fail(f"no {kind} artifact in {dist}: the download of ate-linux-artifacts produced nothing matching {glob}")
    if len(found) > 1:
        fail(f"{dist} holds {len(found)} {kind} artifacts ({', '.join(p.name for p in found)}): a tag builds one of each")
    return found[0]


def _glibc_version(text: str) -> tuple[int, ...]:
    matches = GLIBC_VERSION.findall(text)
    if not matches:
        fail(f"no glibc version in {text!r}: ldd --version printed something unexpected")
    return tuple(int(part) for part in matches[-1].split("."))


def check_glibc(runner: Runner, dist: Path) -> None:
    """The container's glibc must be at least the one the bundle was linked against.

    glibc is forward compatible: a binary built against 2.39 runs on 2.41, not the other way round.
    The artifacts are built on a pinned Ubuntu and smoke-tested in Debian stable, so the build job
    records its own glibc and every smoke run reads it back instead of trusting the assumption.
    """
    marker = dist / GLIBC_MARKER
    if not marker.is_file():
        fail(f"{GLIBC_MARKER} is not in the artifacts: the build job has to record the glibc it linked against")
    built = _glibc_version(marker.read_text(encoding="utf-8"))
    result = runner.run(["ldd", "--version"])
    if result.returncode != 0:
        fail(f"ldd --version failed (exit {result.returncode}): {result.output()[-300:]}")
    container = _glibc_version(result.stdout)
    if container < built:
        fail(
            f"the bundle was linked against glibc {'.'.join(map(str, built))} and this container has "
            f"{'.'.join(map(str, container))}: a binary needs a glibc at least as new as the one it was built against"
        )
    print(f"glibc: built against {'.'.join(map(str, built))}, container has {'.'.join(map(str, container))}")


def _version_module():
    """``src/ate/_version.py``, read the way the packaging scripts read ``build_linux.py``.

    The smoke container has no git and no installed ATE, and this script must not import the
    package it is testing. `_version.py` is stdlib only, so the file can be loaded on its own.
    """
    spec = importlib.util.spec_from_file_location("ate_version", VERSION_FILE)
    if spec is None or spec.loader is None:
        fail(f"cannot read {VERSION_FILE}: the tag-to-version rule lives there")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expected_version(expect: str | None) -> str | None:
    """The tag, spelled the way the artifact reports it.

    `release.yml` passes the tag as it was cut, `0.1.0-rc1`, and the bundle built from it prints
    `0.1.0rc1`, because that is how PEP 440 and the wheel's metadata write it. `_version.normalise`
    is the only place that knows how the two are related, so the comparison goes through it instead
    of through a second rule here. An expectation it cannot read — a branch name from
    `workflow_dispatch`, say — is compared exactly as given.
    """
    if not expect:
        return None
    parsed = _version_module().normalise(expect)
    return parsed or expect


def check_ate_runs(runner: Runner, ate: str, expect_version: str | None = None) -> None:
    """The installed program runs, and prints the version the tag built it with.

    `--version` is the only thing an installed artifact can say about itself. The frozen bundle
    carries the tag in `ate/version.txt` (packet 27a) and the wheel in its own metadata
    (`importlib.metadata`), so an artifact built from tag `v1.2.3` must answer `1.2.3` here. A
    wheel built from a tagged checkout that still prints `0.0.0-dev` is a wheel that baked nothing.
    """
    result = runner.run([ate, "--version"])
    if result.returncode != 0:
        fail(f"ate is not runnable as {ate} (exit {result.returncode}): {result.output()[-300:]}")
    printed = result.stdout.strip()
    if not printed:
        fail(f"{ate} --version printed nothing")
    expected = expected_version(expect_version)
    if expected and printed != expected:
        fail(
            f"{ate} --version prints {printed!r}, but this artifact was built for {expect_version!r} "
            f"(as a version string that is {expected!r}): the version comes from the tag and is "
            "stamped into the bundle and the wheel at build time (assets/packaging.md, 'Version')"
        )
    print(f"ate --version: {printed}")


def check_station_prepared(runner: Runner, station: Station, ate: str) -> dict[str, object]:
    """`ate setup` ran, the config it wrote is readable, and its database is at this ATE's schema."""
    if not station.config.is_file():
        fail(
            f"{station.config} does not exist, so 'ate setup' never ran. For the .deb that is "
            f"postinst's job; for the tarball, install.sh's; for the wheel, this run's."
        )
    try:
        config = json.loads(station.config.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"{station.config} is not readable JSON: {exc}")

    database = Path(str(config.get("database", {}).get("path", "")))
    if not database.is_file():
        fail(f"'ate setup' left no database at {database} named by {station.config}")

    result = runner.run([ate, "--config", str(station.config), "db", "status"])
    if result.returncode != 0 or not result.stdout.strip().isdigit():
        fail(
            f"ate db status did not print a schema version for {database} (exit {result.returncode}): "
            f"{result.output()[-300:]}"
        )
    print(f"database: {database} at schema version {result.stdout.strip()}")
    return {"config": config, "database": database, "schema": int(result.stdout.strip())}


def check_scan(runner: Runner, station: Station, ate: str, prepared: dict[str, object], sample: Path) -> None:
    """The headless launch scanned the shipped example Media Release: schedules found, nothing broken.

    The numbers are parsed rather than searched for: `"0 broken" in out` is also true of
    `"10 broken"`, so a substring check would call a Media Release with ten broken schedules clean.
    """
    config = prepared["config"] if isinstance(prepared["config"], dict) else {}
    directories = config.get("testDefinitions", {}).get("directories") or [str(station.data_dir / "definitions")]
    definitions = Path(str(directories[0]))
    if not definitions.is_dir():
        fail(f"the config names a Test Definition directory that does not exist: {definitions}")
    shutil.copytree(sample, definitions, dirs_exist_ok=True)

    result = runner.run([ate, "_selftest", "--scan", str(definitions), "--config", str(station.config)])
    out = result.output()
    counts = SCAN_COUNTS.search(out)
    if not counts:
        fail(f"the headless scan of {definitions} printed no counts (exit {result.returncode}): {out[-400:]}")
    schedules, broken, mismatched = (int(number) for number in counts.groups())
    if broken:
        fail(f"the headless scan of {definitions} reported {broken} broken Test Definitions: {out[-400:]}")
    if mismatched:
        fail(f"the headless scan of {definitions} reported {mismatched} checksum mismatches: {out[-400:]}")
    if schedules <= 0:
        fail(f"the headless scan of {definitions} found no Test Schedule: copying the example release failed")
    if result.returncode != 0:
        fail(f"the headless scan of {definitions} exited {result.returncode}: {out[-400:]}")
    seen = SCHEMA_SEEN.search(out)
    if not seen:
        fail(f"the scan of {definitions} never reported the database schema it opened: {out[-400:]}")
    schema, expected = (int(number) for number in seen.groups())
    if schema != expected:
        fail(f"the database at {prepared['database']} is schema {schema}, this ATE expects {expected}")
    if schema != prepared["schema"]:
        fail(f"ate db status reports schema {prepared['schema']} but the scan sees {schema}")
    print(f"scan: {schedules} schedules, 0 broken, 0 mismatched in {definitions}")


def install_deb(runner: Runner, station: Station, artifact: Path) -> str:
    """`apt-get install ./ate_*.deb`: apt resolving the Depends is the check (assets/packaging.md)."""
    apt(runner, "update")
    apt(runner, "install", "-y", str(artifact))
    if not (station.app_dir / "ate").is_file():
        fail(f"the .deb installed but {station.app_dir}/ate is not there: the package carries no bundle")
    if not station.bin.is_file():
        fail(f"{station.bin} is missing after installing the .deb: /usr/bin/ate is the link it ships")
    return str(station.bin)


def install_tar(runner: Runner, station: Station, artifact: Path) -> tuple[str, Path]:
    """Unpack the tarball and run `install.sh` exactly as an operator would."""
    apt(runner, "update")
    apt(runner, "install", "-y", *runtime_libs())
    work = Path(tempfile.mkdtemp(prefix="ate-smoke-"))
    result = runner.run(["tar", "-xzf", str(artifact), "-C", str(work)])
    if result.returncode != 0:
        fail(f"extracting {artifact} failed (exit {result.returncode}): {result.output()[-300:]}")
    installer = work / "install.sh"
    if not installer.is_file():
        fail(f"{artifact.name} has no install.sh at its root: there is nothing to install")
    if not (work / "ate" / "ate").is_file():
        fail(f"{artifact.name} has no ate bundle beside install.sh")
    result = runner.run([str(installer), "--prefix", str(station.app_dir), "--bin", str(station.bin)])
    if result.returncode != 0:
        fail(f"install.sh failed (exit {result.returncode}): {result.output()[-400:]}")
    if not station.bin.is_file():
        fail(f"install.sh did not create {station.bin}")
    return str(station.bin), work


def install_wheel(runner: Runner, station: Station, artifact: Path) -> str:
    """pipx installs the wheel; PyQt6 and QtWebEngine come from PyPI, so their system libraries must be there."""
    apt(runner, "update")
    apt(runner, "install", "-y", "python3", "python3-venv", "pipx", *wheel_libs())
    result = runner.run(["pipx", "install", str(artifact)])
    if result.returncode != 0:
        fail(f"pipx install {artifact.name} failed (exit {result.returncode}): {result.output()[-400:]}")
    if not station.wheel_bin.is_file():
        fail(f"pipx installed the wheel but {station.wheel_bin} is not there (HOME={station.home})")
    # A wheel has no postinst: the operator runs `ate setup` themselves (assets/packaging.md).
    result = runner.run([str(station.wheel_bin), "setup"])
    if result.returncode != 0:
        fail(f"'ate setup' after pipx install failed (exit {result.returncode}): {result.output()[-400:]}")
    return str(station.wheel_bin)


def check_remove_keeps_the_station(runner: Runner, station: Station, prepared: dict[str, object]) -> None:
    """`apt remove` removes the program and leaves the Station alone (assets/packaging.md)."""
    apt(runner, "remove", "-y", "ate")
    if (station.app_dir / "ate").exists():
        fail(f"apt remove left {station.app_dir}/ate behind: a package removes its own files")
    if station.bin.exists():
        fail(f"apt remove left {station.bin} behind")
    if not station.config.is_file():
        fail(
            f"apt remove deleted {station.config}: removing the program must leave the Station alone "
            f"(assets/packaging.md: 'apt remove removes only the program')"
        )
    if not Path(str(prepared["database"])).is_file():
        fail(f"apt remove deleted the database at {prepared['database']}: Test Runs are records")


def check_purge_removes_only_the_config(runner: Runner, station: Station, prepared: dict[str, object]) -> None:
    """`apt purge` removes the config setup wrote, and never the database, reports or logs."""
    apt(runner, "purge", "-y", "ate")
    if station.config.exists():
        fail(
            f"apt purge left {station.config} behind: postrm removes it because setup wrote it and "
            f"dpkg knows nothing about it"
        )
    config = prepared["config"] if isinstance(prepared["config"], dict) else {}
    records = {
        "database": Path(str(prepared["database"])),
        "reports": Path(str(config.get("reports", {}).get("directory", ""))),
        "logs": Path(str(config.get("log", {}).get("directory", ""))),
    }
    for label, path in records.items():
        if not path.exists():
            fail(f"apt purge removed the {label} at {path}: purge never touches the records (assets/packaging.md)")


def lifecycle_deb(runner: Runner, station: Station, prepared: dict[str, object]) -> None:
    check_remove_keeps_the_station(runner, station, prepared)
    check_purge_removes_only_the_config(runner, station, prepared)


def lifecycle_tar(runner: Runner, station: Station, prepared: dict[str, object], work: Path) -> None:
    """Run install.sh again (an upgrade), then uninstall: the records survive both."""
    database = Path(str(prepared["database"]))
    installer = work / "install.sh"

    marker = station.data_dir / MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("written before the upgrade", encoding="utf-8")
    stale = station.app_dir / STALE
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("a file the previous build shipped and this one does not", encoding="utf-8")

    result = runner.run([str(installer), "--prefix", str(station.app_dir), "--bin", str(station.bin)])
    if result.returncode != 0:
        fail(f"the upgrade run of install.sh failed (exit {result.returncode}): {result.output()[-400:]}")
    if not marker.is_file():
        fail(f"the upgrade deleted {marker}: an upgrade installs in place and keeps the data")
    if stale.exists():
        fail(
            f"the upgrade left {stale} in {station.app_dir}: install.sh clears the bundle's own "
            f"contents before copying the new ones, so no file of the previous version survives"
        )

    result = runner.run([str(installer), "--uninstall", "--prefix", str(station.app_dir), "--bin", str(station.bin)])
    if result.returncode != 0:
        fail(f"install.sh --uninstall failed (exit {result.returncode}): {result.output()[-400:]}")
    if (station.app_dir / "ate").exists():
        fail(f"uninstall left the bundle in {station.app_dir}")
    if station.bin.exists():
        fail(f"uninstall left the command {station.bin} behind")
    if not station.config.is_file():
        fail(f"uninstall deleted {station.config}: uninstalling removes only the program")
    if not database.is_file():
        fail(f"uninstall deleted the database at {database}: Test Runs are records")


def lifecycle_wheel(runner: Runner, station: Station, ate: str) -> None:
    """The wheel's operator runs `ate setup` themselves: doing it again must not clobber the config."""
    before = station.config.read_text(encoding="utf-8")
    result = runner.run([ate, "setup"])
    if result.returncode != 0:
        fail(f"running 'ate setup' a second time failed (exit {result.returncode}): {result.output()[-300:]}")
    if station.config.read_text(encoding="utf-8") != before:
        fail("running 'ate setup' again rewrote the Station's config: setup writes a default only when none exists")


def run_smoke(kind: str, dist: Path, runner: Runner, station: Station, sample: Path, expect_version: str | None = None) -> list[str]:
    """Install one artifact and prove it runs. Returns the checks that passed."""
    if kind not in KINDS:
        fail(f"unknown --kind {kind!r}: it is one of {', '.join(KINDS)}")
    passed: list[str] = []
    work: Path | None = None

    def note(name: str) -> None:
        passed.append(name)
        print(f"ok: {name}")

    try:
        artifact = find_artifact(dist, kind)
        note(f"the {kind} artifact is there: {artifact.name}")

        # The glibc pin only matters for the frozen bundle: the wheel runs on the container's own
        # CPython, so nothing it links against was built on the packaging machine.
        if kind in ("deb", "tar"):
            check_glibc(runner, dist)
            note("this container's glibc is not older than the one the bundle was linked against")

        if kind == "deb":
            ate = install_deb(runner, station, artifact)
            note("apt-get installed the .deb: its Depends resolved on Debian stable")
        elif kind == "tar":
            ate, work = install_tar(runner, station, artifact)
            note("install.sh installed the bundle from the tarball and ran 'ate setup'")
        else:
            ate = install_wheel(runner, station, artifact)
            note("pipx installed the wheel and pulled PyQt6 from PyPI")

        check_ate_runs(runner, ate, expect_version)
        note("ate runs from where it was installed, printing the version the tag built")

        # Packet 30d: the container runs as root, so Chromium cannot sandbox this render. The flag
        # says so out loud instead of ATE quietly turning the sandbox off.
        result = runner.run([ate, "--allow-unsandboxed-pdf", "_selftest", "--run"])
        if result.returncode != 0:
            fail(f"ate _selftest --run failed (exit {result.returncode}): {result.output()[-600:]}")
        # The selftest's own lines are the evidence the CI log needs: the report PDF it writes and
        # whether that PDF has text in it. Without them a passing smoke run says nothing about the
        # bytes the Station actually gets.
        print(result.output())
        note("the bundle ran a Test Schedule end to end headless, and the PDF it wrote has text in it")

        prepared = check_station_prepared(runner, station, ate)
        note("setup left a config and a database at this ATE's schema version")
        check_scan(runner, station, ate, prepared, sample)
        note("a headless launch scanned the example Media Release")

        if kind == "deb":
            lifecycle_deb(runner, station, prepared)
            note("apt remove keeps the Station; apt purge removes the config and keeps the records")
        elif kind == "tar":
            lifecycle_tar(runner, station, prepared, work)
            note("an upgrade in place kept the data, and uninstalling removed only the program")
        else:
            lifecycle_wheel(runner, station, ate)
            note("running 'ate setup' again left the config alone")

        return passed
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Smoke-test one Linux artifact in a clean Debian container")
    p.add_argument("--kind", required=True, choices=KINDS, help="which artifact to install and test")
    p.add_argument("--dist", default="artifacts", help="the directory the artifacts were downloaded into")
    p.add_argument("--root", default=None, help="the container's filesystem root (tests root it in a temporary directory)")
    p.add_argument("--home", default=None, help="the home directory pipx installs into (default: $HOME)")
    p.add_argument("--sample", default=str(SAMPLE_RELEASE), help="the Media Release to copy into the definitions directory")
    p.add_argument(
        "--expect-version",
        default=None,
        help="the version the tag built the artifact with; `ate --version` must print exactly this",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    station = Station(
        root=Path(args.root) if args.root else Path("/"),
        home=Path(args.home) if args.home else Path(os.environ.get("HOME") or Path.home()),
    )
    runner = SubprocessRunner()
    try:
        passed = run_smoke(args.kind, Path(args.dist), runner, station, Path(args.sample), args.expect_version)
    except SmokeError as exc:
        print(f"SMOKE FAILED ({args.kind}): {exc}", file=sys.stderr)
        return 1
    print(f"{args.kind} smoke: {len(passed)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
