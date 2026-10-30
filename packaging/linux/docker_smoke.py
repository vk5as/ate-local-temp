#!/usr/bin/env python3
r"""Build the Linux artifacts and smoke-test each one in a real `debian:stable` container (28c).

Everything in `packaging/linux/smoke_linux.py` is written for a Debian container, and until now no
container has ever run it: no tag of this repository has been cut, so the release workflow has never
started. This script is the proof that the script works, run wherever Docker is available:

    python packaging/linux/docker_smoke.py                     # build, then smoke deb, tar, wheel
    python packaging/linux/docker_smoke.py --kinds deb,wheel   # just some of them
    ATE_DOCKER_TESTS=1 pytest tests/test_release_ci_28c.py -m build -q

It needs Docker, the `debian:stable` image and network access (apt and PyPI). In one container it
tags a copy of the checkout `v1.2.3`, builds the frozen bundle with PyInstaller using the container's
own `/usr/bin/python3` (3.13 on Debian stable), packages the tarball and the `.deb`, and builds the
wheel; then, in three *fresh* containers, it runs the smoke script exactly as the release job does:

    python3 packaging/linux/smoke_linux.py --kind deb --dist artifacts --expect-version 1.2.3

and finishes by rendering a report with the installed program and reading it back with
`pdftotext`, because a PDF that is valid but has no glyphs in it is the failure the release CI is
supposed to catch. Each kind's log is written to `<out>/<kind>.log`. Containers and build output are
removed at the end unless `--keep` is given.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUILD_LINUX = REPO / "packaging" / "linux" / "build_linux.py"
IMAGE = "debian:stable"
KINDS = ("deb", "tar", "wheel")
# The tag the containers are built from. ATE's version is the tag, so this must be a tag git
# can describe: `v0.1.0-rc1` builds artifacts that report `0.1.0rc1`, not `0.1.0-rc1`.
TAG = os.environ.get("ATE_SMOKE_TAG", "v1.2.3")


def _version_of(tag: str) -> str:
    """The version a checkout tagged `tag` builds as: `v0.1.0-rc1` builds `0.1.0rc1`.

    `src/ate/_version.py` is the only place that knows how a tag becomes a version, so the
    expectation the build is checked against comes from it rather than from the tag string.
    """
    spec = importlib.util.spec_from_file_location("ate_version", REPO / "src" / "ate" / "_version.py")
    if spec is None or spec.loader is None:
        return tag.lstrip("v")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.normalise(tag) or tag.lstrip("v")


VERSION = _version_of(TAG)


class DockerError(Exception):
    """A container step failed. The run stops there, with the command and its output."""


def _run(argv: list[str], *, timeout: int = 1800, check: bool = True) -> subprocess.CompletedProcess:
    print("$ " + " ".join(str(part) for part in argv))
    proc = subprocess.run([str(part) for part in argv], capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise DockerError(f"{' '.join(map(str, argv))} exited {proc.returncode}:\n{(proc.stdout + proc.stderr)[-2000:]}")
    return proc


def _exec(container: str, script: str, *, timeout: int = 1800, check: bool = True) -> subprocess.CompletedProcess:
    return _run(["docker", "exec", container, "sh", "-c", script], timeout=timeout, check=check)


def _start(name: str, scratch: Path) -> str:
    container = f"ate-28c-{name}"
    # A run that died half way leaves containers behind; make a rerun work without cleanup by hand.
    _run(["docker", "rm", "-f", container], timeout=300, check=False)
    _run(["docker", "run", "-d", "--name", container, "-v", f"{scratch}:/work", IMAGE, "sleep", "infinity"], timeout=600)
    return container


def _stop(name: str) -> None:
    container = f"ate-28c-{name}"
    _run(["docker", "rm", "-f", container], timeout=300, check=False)


def build_artifacts(scratch: Path) -> Path:
    """Build the bundle, the tarball, the .deb and the wheel in one Debian container."""
    build = _start("build", scratch)
    checkout = scratch / "checkout"
    _exec(build, "apt-get update -qq")
    _exec(build, "apt-get install -y -qq python3 python3-venv git dpkg tar binutils poppler-utils")
    # PyInstaller needs the interpreter's shared library, which Debian's python3 does not ship:
    # it is in libpython3.<minor>, and the minor is whatever Debian stable happens to have.
    libpython = _exec(build, "python3 -c 'import sys; print(f\"libpython3.{sys.version_info[1]}\")'").stdout.strip()
    # PyInstaller's Qt hooks only collect QtWebEngine's helper binary, its .pak resources and the Qt
    # platform plugins if PyQt6 can actually be imported in the build environment. In a bare Debian
    # container that import fails on libGL/libglib, the hooks are skipped silently, and the bundle
    # ships a renderer that cannot start: the first `_pdf` call dies with "Could not find the Qt
    # platform plugin offscreen". The libraries the artifacts need at runtime are the same ones the
    # build needs, so install them from the same lists.
    spec = importlib.util.spec_from_file_location("ate_build_linux", BUILD_LINUX)
    packaging = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packaging)
    build_libs = sorted({*packaging.DEPENDS, *packaging.WHEEL_SYSTEM_LIBS, "binutils", libpython})
    _exec(build, f"apt-get install -y -qq --no-install-recommends {' '.join(build_libs)}")
    # A tag in the copied checkout is what makes the version a tag version: the frozen bundle and the
    # wheel both read it at build time, and the smoke run checks `ate --version` against it.
    _exec(build, "git config --global --add safe.directory /work/checkout && cd /work/checkout && git init -q && git add -A && git -c user.email=ci@example.com -c user.name=ci commit -q -m 'release build' && git tag " + TAG)
    _exec(build, "python3 -m venv /work/venv && /work/venv/bin/python -m pip install --quiet -q /work/checkout pyinstaller build")
    version = _exec(build, "/work/venv/bin/python -c 'from ate._version import __version__; print(__version__)'").stdout.strip()
    if version != VERSION:
        raise DockerError(f"the checkout built as version {version!r}, expected {VERSION!r} from the tag {TAG}")
    # What release.yml does before the build: the glibc the bundle is linked against is recorded so
    # every smoke run can read it back.
    _exec(build, "mkdir -p /work/dist && ldd --version | head -n 1 | tee /work/dist/glibc-build.txt")
    _exec(build, "/work/venv/bin/python /work/checkout/packaging/build_pyinstaller.py --dist /work/dist --work /work/pyinstaller-work", timeout=3600)
    _exec(build, "/work/venv/bin/python /work/checkout/packaging/linux/build_linux.py --dist /work/dist --out /work/artifacts", timeout=1800)
    _exec(build, "cp /work/dist/glibc-build.txt /work/artifacts/")
    _exec(build, "cd /work/checkout && /work/venv/bin/python -m build --wheel", timeout=1800)
    # The wheel goes into the artifacts directory inside the container: it belongs to root there, and
    # the person running this script cannot write into it.
    _exec(build, "cp /work/checkout/dist/ate-*.whl /work/artifacts/")
    listing = _exec(build, "ls /work/artifacts").stdout.split()
    if not any(name.endswith(".whl") for name in listing):
        raise DockerError("python -m build --wheel produced no wheel in /work/checkout/dist")
    _stop("build")
    return scratch / "artifacts"


def _ate_path(kind: str) -> str:
    """Where the program is after the smoke run installed it in this container."""
    return "/root/.local/bin/ate" if kind == "wheel" else "/usr/bin/ate"


def pdf_probe(kind: str, scratch: Path) -> str:
    """Install one artifact in a fresh container and render a page with it, then read the PDF back.

    The report PDF is the thing a stock Debian container gets wrong: it renders, writes a valid file,
    and has no glyphs in it. A 1.7 KB PDF that pdftotext reads as empty is a Station that produces
    unreadable Test Reports, so the proof has to be the installed program's own renderer plus a real
    text extractor, not a size guess.
    """
    container = _start(f"{kind}-pdf", scratch)
    _exec(container, "apt-get update -qq")
    _exec(container, "apt-get install -y -qq tar poppler-utils")
    if kind == "deb":
        _exec(container, "apt-get install -y -qq /work/artifacts/ate_*_amd64.deb")
    elif kind == "tar":
        spec = importlib.util.spec_from_file_location("ate_build_linux", BUILD_LINUX)
        packaging = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(packaging)
        _exec(container, f"apt-get install -y -qq --no-install-recommends {' '.join(packaging.DEPENDS)}")
        _exec(container, "mkdir -p /tmp/ate-tar && tar -xzf /work/artifacts/ate-*-linux-x86_64.tar.gz -C /tmp/ate-tar && /tmp/ate-tar/install.sh")
    else:
        spec = importlib.util.spec_from_file_location("ate_build_linux", BUILD_LINUX)
        packaging = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(packaging)
        _exec(container, "apt-get install -y -qq python3 python3-venv pipx " + " ".join(packaging.WHEEL_SYSTEM_LIBS))
        _exec(container, "pipx install /work/artifacts/ate-*.whl")
    # Packet 30d: this container runs as root, so `ate _pdf` refuses unless the parent's decision is
    # handed to it as an argument.
    result = _exec(
        container,
        "printf '<html><body><h1>ATE PDF probe</h1><p>Text must survive the render.</p></body></html>' "
        f"> /tmp/ate-pdf.html && {_ate_path(kind)} _pdf /tmp/ate-pdf.html /tmp/ate-pdf.pdf --paper A4 --unsandboxed && "
        "stat -c 'probe pdf %s bytes' /tmp/ate-pdf.pdf && pdftotext /tmp/ate-pdf.pdf - | sed -n '1,5p'",
        timeout=600,
        check=False,
    )
    _stop(f"{kind}-pdf")
    return (result.stdout + result.stderr).strip()


def check_run(stdout: str, kinds: tuple[str, ...] = KINDS) -> list[str]:
    """What the harness's own output proves, and what it does not.

    Kept separate from `main` so a test can hand it a log and see whether the assertions really
    bite: a run whose selftest PDF has no text, or whose wheel never ran, is a run this returns
    problems for. A container run is too slow to repeat for every mutation, so the checking is
    something a test can exercise directly.
    """
    problems: list[str] = []
    for kind in kinds:
        marker = f"--- {kind} container log ---"
        if marker not in stdout:
            problems.append(f"{kind}: no container log was printed at all")
            continue
        log = stdout.split(marker, 1)[1].split("--- ", 1)[0]
        if "SMOKE FAILED" in log:
            problems.append(f"{kind}: the smoke script reported a failure")
        if f"{kind} smoke:" not in log:
            problems.append(f"{kind}: never reported how many checks passed")
        if "bytes PDF, with text" not in log:
            problems.append(f"{kind}: no selftest PDF said it has text in it")
        if "Text must survive the render" not in log:
            problems.append(f"{kind}: pdftotext never read text out of a rendered report")
    return problems


def finish(kinds: list[str], reports: list[str]) -> int:
    """Print the closing line and return 1 if the run does not prove what it claims.

    Separate from `main` so a test can hand it logs from a run that went wrong — a kind that never
    printed its checks, or a report PDF with no text — and see that it is refused.
    """
    problems = check_run("".join(f"--- {kind} container log ---\n{report}\n" for kind, report in zip(kinds, reports)), tuple(kinds))
    if problems:
        print("DOCKER SMOKE INCOMPLETE: " + "; ".join(problems), file=sys.stderr)
        return 1
    print("docker smoke: all kinds ran in debian:stable")
    return 0


def smoke(kind: str, scratch: Path, logs: Path) -> str:
    """One kind, in its own fresh container, run exactly as release.yml runs it."""
    container = _start(kind, scratch)
    log = logs / f"{kind}.log"
    _exec(container, "apt-get update -qq")
    _exec(container, "apt-get install -y -qq python3 tar poppler-utils")
    script = (
        "python3 /work/checkout/packaging/linux/smoke_linux.py "
        f"--kind {kind} --dist /work/artifacts --expect-version {VERSION}"
    )
    result = _exec(container, script, timeout=3600, check=False)
    output = result.stdout + result.stderr
    probe_out = pdf_probe(kind, scratch)
    report = output + "\n=== pdftotext probe (fresh container, same artifact) ===\n" + probe_out
    log.write_text(report, encoding="utf-8")
    # The log goes to stdout as well: the scratch directory is removed at the end, and whoever runs
    # this needs to see what the container actually said.
    print(f"\n--- {kind} container log ---\n{report}")
    pdf_line = next((line for line in output.splitlines() if "bytes PDF" in line), "")
    print(f"{kind}: smoke exit {result.returncode}; {pdf_line}")
    _stop(kind)
    if result.returncode != 0:
        raise DockerError(f"{kind} smoke failed:\n{output[-1500:]}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and smoke-test the Linux artifacts in debian:stable containers")
    parser.add_argument("--kinds", default=",".join(KINDS), help="which kinds to smoke (default: all three)")
    parser.add_argument("--out", default=None, help="where the logs go (default: a temporary directory)")
    parser.add_argument("--skip-build", action="store_true", help="reuse the artifacts already in --out instead of building them again")
    parser.add_argument("--keep", action="store_true", help="leave the containers and the build output in place")
    args = parser.parse_args(argv)

    kinds = [kind for kind in args.kinds.split(",") if kind.strip()]
    for kind in kinds:
        if kind not in KINDS:
            raise SystemExit(f"unknown kind {kind!r}: it is one of {', '.join(KINDS)}")

    if shutil.which("docker") is None:
        raise SystemExit("docker is not installed: this test needs a Debian container")

    scratch = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="ate-28c-docker-", dir="/tmp"))
    scratch.mkdir(parents=True, exist_ok=True)
    artifacts = scratch / "artifacts"
    started: list[str] = []
    if args.skip_build:
        if not any(p.name.endswith(".whl") for p in artifacts.glob("*")):
            raise SystemExit(f"--skip-build: {artifacts} holds no artifacts; run the build first")
    else:
        # Root-owned leftovers from an earlier `--keep` run are not ours to delete, and they are why
        # `logs.mkdir` below can fail with a permission error: clear them with a container first.
        _run(["docker", "run", "--rm", "-v", f"{scratch}:/work", IMAGE, "sh", "-c", "rm -rf /work/*"], timeout=600, check=False)
    # The checkout the containers run, copied fresh every time: the smoke script a container executes
    # is the one in this working tree, not whatever a previous run left behind. The `git init` inside
    # the build container makes parts of it root-owned, so it is cleared from a container too.
    _run(["docker", "run", "--rm", "-v", f"{scratch}:/work", IMAGE, "sh", "-c", "rm -rf /work/checkout"], timeout=600, check=False)
    shutil.rmtree(scratch / "checkout", ignore_errors=True)
    shutil.copytree(REPO, scratch / "checkout", ignore=shutil.ignore_patterns(".git", "dist", "build", "__pycache__", ".scratch"))
    logs = scratch / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    results: list[str] = []
    try:
        if not args.skip_build:
            artifacts = build_artifacts(scratch)
            print("built artifacts:", ", ".join(sorted(p.name for p in artifacts.iterdir())))
        for kind in kinds:
            started.append(kind)
            results.append(smoke(kind, scratch, logs))
            print(f"  log: {logs / (kind + '.log')}")
    except DockerError as exc:
        print(f"DOCKER SMOKE FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        if not args.keep:
            for kind in started + ["build"]:
                _stop(kind)
            # The containers wrote as root into the scratch directory, so clean it from a container
            # too: the user running this script cannot delete files it does not own.
            _run(["docker", "run", "--rm", "-v", f"{scratch}:/work", IMAGE, "sh", "-c", "rm -rf /work/*"], timeout=600, check=False)
            shutil.rmtree(scratch, ignore_errors=True)
        else:
            print(f"kept: {scratch}")

    return finish(kinds, results)
if __name__ == "__main__":
    sys.exit(main())
