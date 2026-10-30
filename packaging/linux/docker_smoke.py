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
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
IMAGE = "debian:stable"
KINDS = ("deb", "tar", "wheel")
TAG = "v1.2.3"
VERSION = TAG[1:]


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
    _run(["docker", "run", "-d", "--name", container, "-v", f"{scratch}:/work", IMAGE, "sleep", "infinity"], timeout=600)
    return container


def _stop(name: str) -> None:
    container = f"ate-28c-{name}"
    _run(["docker", "rm", "-f", container], timeout=300, check=False)


def build_artifacts(scratch: Path) -> Path:
    """Build the bundle, the tarball, the .deb and the wheel in one Debian container."""
    build = _start("build", scratch)
    checkout = scratch / "checkout"
    shutil.copytree(REPO, checkout, ignore=shutil.ignore_patterns(".git", "dist", "build", "__pycache__", ".scratch"))
    _exec(build, "apt-get update -qq && apt-get install -y -qq python3 python3-venv git dpkg tar poppler-utils")
    # A tag in the copied checkout is what makes the version a tag version: the frozen bundle and the
    # wheel both read it at build time, and the smoke run checks `ate --version` against it.
    _exec(build, "cd /work/checkout && git init -q && git add -A && git -c user.email=ci@example.com -c user.name=ci commit -q -m 'release build' && git tag " + TAG)
    _exec(build, "python3 -m venv /work/venv && /work/venv/bin/python -m pip install --quiet -q /work/checkout pyinstaller build")
    version = _exec(build, "/work/venv/bin/python -c 'from ate._version import __version__; print(__version__)'").stdout.strip()
    if version != VERSION:
        raise DockerError(f"the checkout built as version {version!r}, expected {VERSION!r} from the tag {TAG}")
    _exec(build, "/work/venv/bin/python /work/checkout/packaging/build_pyinstaller.py --dist /work/dist --work /work/pyinstaller-work", timeout=3600)
    _exec(build, "/work/venv/bin/python /work/checkout/packaging/linux/build_linux.py --dist /work/dist --out /work/artifacts", timeout=1800)
    _exec(build, "cd /work/checkout && /work/venv/bin/python -m build --wheel", timeout=1800)
    wheels = sorted((checkout / "dist").glob("ate-*.whl"))
    if not wheels:
        raise DockerError("python -m build --wheel produced no wheel")
    shutil.copyfile(wheels[0], scratch / "artifacts" / wheels[0].name)
    _stop("build")
    return scratch / "artifacts"


def _ate_path(kind: str) -> str:
    """Where the program is after the smoke run installed it in this container."""
    return "/root/.local/bin/ate" if kind == "wheel" else "/usr/bin/ate"


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
    ate = _ate_path(kind)
    # The report PDF is the thing a stock Debian container gets wrong: it renders, writes a valid
    # file, and has no glyphs in it. Prove the bytes are words with the installed program's own
    # renderer and a real text extractor.
    probe = _exec(
        container,
        "printf '<html><body><h1>ATE PDF probe</h1><p>Text must survive the render.</p></body></html>' "
        f"> /tmp/ate-pdf.html && {ate} _pdf /tmp/ate-pdf.html /tmp/ate-pdf.pdf --paper A4 && "
        "stat -c 'probe pdf %s bytes' /tmp/ate-pdf.pdf && pdftotext /tmp/ate-pdf.pdf - | sed -n '1,5p'",
        timeout=600,
        check=False,
    )
    probe_out = probe.stdout + probe.stderr
    log.write_text(output + "\n=== pdftotext probe ===\n" + probe_out, encoding="utf-8")
    pdf_line = next((line for line in output.splitlines() if "bytes PDF" in line), "")
    print(f"{kind}: smoke exit {result.returncode}; {pdf_line}")
    print(f"    probe: {probe_out.strip()[-300:]}")
    _stop(kind)
    if result.returncode != 0:
        raise DockerError(f"{kind} smoke failed:\n{output[-1500:]}")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and smoke-test the Linux artifacts in debian:stable containers")
    parser.add_argument("--kinds", default=",".join(KINDS), help="which kinds to smoke (default: all three)")
    parser.add_argument("--out", default=None, help="where the logs go (default: a temporary directory)")
    parser.add_argument("--keep", action="store_true", help="leave the containers and the build output in place")
    args = parser.parse_args(argv)

    kinds = [kind for kind in args.kinds.split(",") if kind.strip()]
    for kind in kinds:
        if kind not in KINDS:
            raise SystemExit(f"unknown kind {kind!r}: it is one of {', '.join(KINDS)}")

    if shutil.which("docker") is None:
        raise SystemExit("docker is not installed: this test needs a Debian container")

    scratch = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="ate-28c-docker-", dir="/tmp"))
    logs = scratch / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    started: list[str] = []
    try:
        artifacts = build_artifacts(scratch)
        print("built artifacts:", ", ".join(sorted(p.name for p in artifacts.iterdir())))
        for kind in kinds:
            container = f"ate-28c-{kind}"
            started.append(kind)
            smoke(kind, scratch, logs)
            print(f"  log: {logs / (kind + '.log')}")
    except DockerError as exc:
        print(f"DOCKER SMOKE FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        if not args.keep:
            for kind in started + ["build"]:
                _stop(kind)
            shutil.rmtree(scratch, ignore_errors=True)
        else:
            print(f"kept: {scratch}")
    print("docker smoke: all kinds ran in debian:stable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
