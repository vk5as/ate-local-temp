#!/usr/bin/env python3
"""Build the ATE manuals to PDF with latexmk, inside Docker, and stage them for packaging.

    python docs/manuals/build.py                 # every manual, built and staged
    python docs/manuals/build.py test-writer     # one manual
    python docs/manuals/build.py --stage DIR     # copy PDFs a CI job downloaded, no TeX needed
    python docs/manuals/build.py --list          # the six files a release carries, one per line
    python docs/manuals/build.py --image         # the pinned TeX Live image, for `docker pull`

The TeX toolchain is not installed on the build machine and installing system packages is not
allowed, so every manual is compiled by `latexmk -lualatex` in the pinned TeX Live image. Nothing
TeX-related is needed on the host except Docker.

The build writes each PDF twice:

  docs/manuals/build/<name>/main.pdf   latexmk's own output (gitignored)
  src/ate/manuals/<name>.pdf           the staged copy, gitignored, which PyInstaller and the
                                       wheel both collect, so the manuals ship inside the app

The title page of every PDF carries the version this checkout is releasing: the same string
`ate --version` prints, taken from the tag by src/ate/_version.py. A checkout with no tag prints
`0.0.0-dev`, which is why a source build is a draft, not a release.

The build fails if latexmk reports an error, an undefined reference, or a line that overflows the
text width by more than 5 pt: a manual that quietly overflows is a manual someone cannot read. It
also fails if the staged PDF is missing or empty, or if a manual directory exists that the list of
release files does not name.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

MANUALS_DIR = Path(__file__).resolve().parent
REPO = MANUALS_DIR.parents[1]

#: TeX Live 2026 (latexmk 4.88, LuaHBTeX 1.24), pinned by digest so a release can be rebuilt with
#: the same TeX. `latest` moves: the image behind it changed twice in the week before this packet
#: was written. About 9 GB unpacked, so a runner needs room (the job prints `df -h`). A smaller
#: image (`latest-small`, `latest-context`) is not enough: these manuals use listings, booktabs,
#: longtable, microtype and hyperref, and the pinned image is the one the PDFs below were built with.
TEXLIVE_DIGEST = "sha256:07e8062637249c4f780ca59f42cc2ed592bc2dc1db4320769406aee3f98aa70e"
IMAGE = f"texlive/texlive@{TEXLIVE_DIGEST}"

#: Every manual ATE has. This is the list the workflow uploads, the packaging ships and the tests
#: check: a manual directory that is not in it makes the build fail rather than be left out.
MANUAL_NAMES = ("test-writer", "admin", "developer")
#: The six files a GitHub Release carries: the three PDFs and the three Markdown manuals.
MANUAL_PDFS = tuple(f"{name}.pdf" for name in MANUAL_NAMES)
MANUAL_MARKDOWN = tuple(f"{name}.md" for name in MANUAL_NAMES)
MANUAL_FILES = MANUAL_PDFS + MANUAL_MARKDOWN

#: Where the PDFs are staged so the bundle and the wheel pick them up. Gitignored: a source
#: checkout has no PDFs until someone builds them.
STAGE_DIR = REPO / "src" / "ate" / "manuals"
MARKDOWN_DIR = MANUALS_DIR / "llm"
#: An overfull box this wide (in points) is a layout bug, not a footnote that barely fits.
MAX_OVERFULL_PT = 5.0

OVERFULL = re.compile(r"Overfull \\hbox \(([\d.]+)pt too wide\) in (line|paragraph|package)")
#: "Reference undefined" and "Undefined control sequence" are the two ways a manual can
#: point at something that is not there.
UNDEFINED = re.compile(r"(Reference .* undefined|Undefined control sequence|LaTeX Error)")


def manual_dirs() -> dict[str, Path]:
    """A manual is a directory under docs/manuals/ with main.tex next to its chapters."""
    return {
        name: MANUALS_DIR / name
        for name in MANUAL_NAMES
        if (MANUALS_DIR / name / "main.tex").is_file()
    }


def manuals_left_out() -> list[str]:
    """Manual directories that exist but are not in MANUAL_NAMES: someone added one and forgot."""
    listed = set(MANUAL_NAMES)
    return sorted(
        p.name
        for p in MANUALS_DIR.iterdir()
        if p.is_dir() and p.name not in {"common", "examples", "llm", "build"} and (p / "main.tex").is_file() and p.name not in listed
    )


def release_version() -> str:
    """The version this checkout is releasing, exactly as `ate --version` prints it."""
    sys.path.insert(0, str(REPO / "src"))
    from ate._version import __version__

    return __version__


def stamp_version(manual: Path, version: str) -> None:
    """Write the version macro every manual's title page reads (manual.sty defines the fallback).

    The file lands next to the manual's own chapters, which is the directory the build runs in and
    therefore on TEXINPUTS. Adding the build output directory to TEXINPUTS instead is not the way:
    TeX Live 2026's luaotfload aborts when a relative TEXINPUTS entry points outside the working
    directory, so the stamp has to be somewhere the manual already looks.
    """
    stamp = (
        "% written by docs/manuals/build.py: the version this release carries, the same string\n"
        "% `ate --version` prints. The title page shows it, and hyperref puts it in the PDF's\n"
        "% Title, which TeX writes uncompressed: the smoke checks read the bytes, not a viewer.\n"
        "\\renewcommand{\\ATEversion}{" + version + "}\n"
        "\\hypersetup{pdftitle={" + manual.name + " manual, ATE \\ATEversion}}\n"
    )
    (manual / "ateversion.tex").write_text(stamp, encoding="utf-8", newline="\n")


def stage_pdf(pdf: Path, name: str) -> Path:
    """Put one built PDF in the staged directory as `<name>.pdf`, refusing an empty or non-PDF file."""
    if not pdf.is_file():
        raise SystemExit(f"no PDF to stage: {pdf} does not exist")
    data = pdf.read_bytes()
    if not data.startswith(b"%PDF"):
        raise SystemExit(f"{pdf} is not a PDF (starts with {data[:8]!r})")
    STAGE_DIR.mkdir(parents=True, exist_ok=True)
    target = STAGE_DIR / f"{name}.pdf"
    shutil.copyfile(pdf, target)
    print(f"  staged {target.relative_to(REPO)} ({len(data)} bytes)")
    return target


def stage(source: Path) -> int:
    """Copy the PDFs from a CI artifact into the staged directory, without running TeX.

    `release.yml` builds the manuals once in the `manuals` job and hands them to the package jobs,
    which cannot run TeX (the Windows runner has no Docker image pulled for it). This is where they
    put them back: every name in MANUAL_PDFS has to be somewhere under `source`, or the job fails
    rather than packaging an app with two manuals.
    """
    staged = []
    for name in MANUAL_NAMES:
        found = [p for p in source.rglob(f"{name}.pdf") if p.is_file()]
        if not found:
            print(f"staging failed: {name}.pdf is not under {source}", file=sys.stderr)
            return 1
        staged.append(stage_pdf(found[0], name))
    print(f"staged {len(staged)} manuals from {source}")
    return 0


def build(name: str, out: Path, version: str) -> int:
    """Compile one manual. Returns 0 on a clean build, 1 otherwise."""
    out.mkdir(parents=True, exist_ok=True)
    stamp_version(MANUALS_DIR / name, version)
    cmd = [
        "docker", "run", "--rm",
        "-u", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{REPO}:/work",
        "-w", f"/work/docs/manuals/{name}",
        # TeX Live's caches live under HOME; as a non-root user the image's default HOME is not
        # writable, so point them at /tmp inside the container. TEXINPUTS is the manual's own
        # directory and the shared preamble: the version stamp build.py writes sits next to
        # main.tex, so it is already on the path. Adding the build directory here is not
        # possible: TeX Live 2026's luaotfload aborts when TEXINPUTS contains a relative path
        # outside the working directory.
        "-e", "TEXINPUTS=.:../common:",
        "-e", "HOME=/tmp",
        IMAGE,
        "latexmk", "-lualatex", "-interaction=nonstopmode",
        "-output-directory=../build/" + name, "main.tex",
    ]
    print(f"building {name} -> docs/manuals/build/{name}/main.pdf (version {version})")
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
    try:
        stage_pdf(out / "main.pdf", name)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("manual", nargs="?", choices=sorted(manual_dirs()),
                        help="one manual to build; by default all of them")
    parser.add_argument("--stage", metavar="DIR",
                        help="copy the PDFs in DIR into src/ate/manuals instead of running TeX")
    parser.add_argument("--list", action="store_true",
                        help="print the files a release carries, one per line, and stop: this is the "
                             "list the workflow uploads and the publish job checks")
    parser.add_argument("--image", action="store_true",
                        help="print the pinned TeX Live image and stop, so a CI job can pull it itself")
    args = parser.parse_args()

    if args.list:
        print("\n".join(MANUAL_FILES))
        return 0
    if args.image:
        print(IMAGE)
        return 0

    if args.stage:
        return stage(Path(args.stage))

    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("Docker is not available; the manuals need it to build.", file=sys.stderr)
        return 1

    left_out = manuals_left_out()
    if left_out:
        print(
            f"manual directories not in MANUAL_NAMES: {', '.join(left_out)}. "
            "Add them to the list in docs/manuals/build.py, or the release ships without them.",
            file=sys.stderr,
        )
        return 1

    version = release_version()
    names = [args.manual] if args.manual else sorted(manual_dirs())
    failed = [n for n in names if build(n, MANUALS_DIR / "build" / n, version) != 0]
    if failed:
        print(f"build failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
