#!/usr/bin/env python3
"""Build the ATE manuals to PDF with latexmk, inside Docker.

The TeX toolchain is not installed on the build machine and installing system packages is not
allowed, so every manual is compiled by `latexmk -lualatex` in the `texlive/texlive:latest`
image. Nothing TeX-related is needed on the host except Docker.

    python docs/manuals/build.py                 # every manual
    python docs/manuals/build.py test-writer     # one manual

PDFs land in docs/manuals/build/<name>/, which is gitignored. The build fails if latexmk
reports an error, an undefined reference, or a line that overflows the text width by more than
5 pt: a manual that quietly overflows is a manual someone cannot read.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

MANUALS_DIR = Path(__file__).resolve().parent
REPO = MANUALS_DIR.parents[1]
IMAGE = "texlive/texlive:latest"
#: An overfull box this wide (in points) is a layout bug, not a footnote that barely fits.
MAX_OVERFULL_PT = 5.0
#: A manual is a directory under docs/manuals/ with main.tex next to its chapters.
MANUAL_DIRS = {
    p.name: p for p in MANUALS_DIR.iterdir()
    if p.is_dir() and p.name not in {"common", "examples", "llm", "build"}
    and (p / "main.tex").exists()
}

OVERFULL = re.compile(r"Overfull \\hbox \(([\d.]+)pt too wide\) in (line|paragraph|package)")
#: "Reference undefined" and "Undefined control sequence" are the two ways a manual can
#: point at something that is not there.
UNDEFINED = re.compile(r"(Reference .* undefined|Undefined control sequence|LaTeX Error)")


def build(name: str, out: Path) -> int:
    """Compile one manual. Returns 0 on a clean build, 1 otherwise."""
    out.mkdir(parents=True, exist_ok=True)
    cmd = [
        "docker", "run", "--rm",
        "-u", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{REPO}:/work",
        "-w", f"/work/docs/manuals/{name}",
        # texlive's caches live under HOME; as a non-root user the image's default HOME is
        # not writable, so point them at /tmp inside the container.
        "-e", "TEXINPUTS=.:../common:",
        "-e", "HOME=/tmp",
        IMAGE,
        "latexmk", "-lualatex", "-interaction=nonstopmode",
        "-output-directory=../build/" + name, "main.tex",
    ]
    print(f"building {name} -> docs/manuals/build/{name}/main.pdf")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout[-2500:], file=sys.stderr)
        print(result.stderr[-1500:], file=sys.stderr)
        return 1

    log = out / "main.log"
    if not log.exists():
        print(f"no {log}: latexmk produced nothing", file=sys.stderr)
        return 1
    text = log.read_text(encoding="utf-8", errors="replace")

    problems: list[str] = []
    for line in text.splitlines():
        match = OVERFULL.search(line)
        if match and float(match.group(1)) > MAX_OVERFULL_PT:
            problems.append(f"overfull box: {line.strip()}")
        elif UNDEFINED.search(line):
            problems.append(f"undefined: {line.strip()}")
    if problems:
        print(f"{name}: {len(problems)} layout or reference problem(s):", file=sys.stderr)
        for line in problems[:20]:
            print("  " + line, file=sys.stderr)
        return 1

    pages = re.search(r"Output written on .*?(\d+) page", text)
    count = pages.group(1) if pages else "?"
    print(f"  ok: docs/manuals/build/{name}/main.pdf ({count} pages)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manual", nargs="?", choices=sorted(MANUAL_DIRS),
                        help="one manual to build; by default all of them")
    args = parser.parse_args()

    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("Docker is not available; the manuals need it to build.", file=sys.stderr)
        return 1

    names = [args.manual] if args.manual else sorted(MANUAL_DIRS)
    failed = [n for n in names if build(n, MANUALS_DIR / "build" / n) != 0]
    if failed:
        print(f"build failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
