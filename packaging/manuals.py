"""The manuals a release carries, and how to prove they are in an install (packet 36).

One list, read from `docs/manuals/build.py`: the three PDF manuals and the three Markdown
manuals. The workflow uploads them, the packaging puts the PDFs inside every artifact, and the
smoke tests read this module to check what actually got installed. If a manual were added to the
TeX sources and not to that list, `build.py`'s build fails rather than the release quietly
shipping one short.

Where the PDFs are in an installed ATE:

| Artifact | Inside the install |
| --- | --- |
| Windows installer, `.deb`, tarball | `<install>/_internal/ate/manuals/*.pdf` |
| wheel (pip/pipx) | `<site-packages>/ate/manuals/*.pdf` |
| `.deb`, additionally | `/usr/share/doc/ate/*.pdf` |

`ate --help` prints the real path for whatever install is running, and `ate.manuals` is the code
behind that line.
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANUALS_BUILD = REPO / "docs" / "manuals" / "build.py"

#: A PDF is a PDF if its first four bytes say so. That is the whole check the smoke tests make:
#: a truncated or empty file is not a manual.
PDF_MAGIC = b"%PDF"


def _manuals_build():
    spec = importlib.util.spec_from_file_location("ate_manuals_build", MANUALS_BUILD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manual_pdf_names() -> tuple[str, ...]:
    """`test-writer.pdf`, `admin.pdf`, `developer.pdf`, in that order."""
    return _manuals_build().MANUAL_PDFS


def manual_file_names() -> tuple[str, ...]:
    """The six files a GitHub Release carries: the three PDFs and the three Markdown manuals."""
    return _manuals_build().MANUAL_FILES


def release_build() -> bool:
    """Whether this build is a release build, as `release.yml` says with `ATE_RELEASE_BUILD=1`.

    A release build refuses to package an artifact without every manual. A build from a plain
    source checkout has no PDFs staged and is not a release, so it carries on without them.
    """
    return os.environ.get("ATE_RELEASE_BUILD") == "1"


def is_pdf(path: Path) -> bool:
    """A real PDF: it exists, it is not empty, and it starts with `%PDF`."""
    return path.is_file() and path.read_bytes()[:4] == PDF_MAGIC


def pdf_title(path: Path) -> str:
    r"""The PDF's `Title`, read out of the Info dictionary.

    TeX writes the Info dictionary uncompressed, so the title is readable without a PDF library.
    It arrives as a PDF string: hyperref encodes it as UTF-16BE with octal escapes, and `\ddd` is
    three characters in the file but one byte in the string, so the escapes have to be undone
    before anything can be compared.
    """
    data = path.read_bytes()
    marker = data.find(b"/Title(")
    if marker < 0:
        return ""
    raw = bytearray()
    i = marker + len("/Title(")
    while i < len(data) and data[i] != 0x29:  # an unescaped ')' ends the string
        if data[i] == 0x5C and i + 1 < len(data) and 0x30 <= data[i + 1] <= 0x37:  # \ddd
            octal = data[i + 1 : i + 4]
            raw.append(int(octal, 8))
            i += 1 + len(octal)
        elif data[i] == 0x5C and i + 1 < len(data):
            raw.append({0x6E: 0x0A, 0x72: 0x0D, 0x74: 0x09, 0x62: 0x08, 0x66: 0x0C}.get(data[i + 1], data[i + 1]))
            i += 2
        else:
            raw.append(data[i])
            i += 1
    text = bytes(raw)
    if text.startswith(b"\xfe\xff"):
        return text[2:].decode("utf-16-be", errors="replace")
    return text.decode("latin-1", errors="replace")


#: A version as it can appear in a manual: base, optional prerelease, optional dev suffix. This is
#: the same shape `_version.py` produces, so a stamp is read whole rather than as a substring.
VERSION_STAMP = re.compile(r"\d+\.\d+\.\d+(?:[a-z]+\d+)?(?:\.dev\d+\+g[0-9a-f]+)?")


def version_stamps(path: Path) -> set[str]:
    """Every version-shaped stamp the PDF carries, read whole.

    `"0.1.0" in title` is true for a manual stamped `0.1.0rc3`: a substring check lets the manual
    of one prerelease pass as the manual of the release it is a prerelease of. Splitting the title
    into stamps and comparing stamps is what makes "this is that version" mean anything.
    """
    stamps = set(VERSION_STAMP.findall(pdf_title(path)))
    raw = path.read_bytes()
    for encoding in ("utf-16-be", "utf-16-le", "latin-1"):
        stamps.update(VERSION_STAMP.findall(raw.decode(encoding, "replace")))
    return stamps


def carries_version(path: Path, version: str) -> bool:
    """Whether the PDF names this version, exactly.

    `docs/manuals/build.py` puts the release version in the PDF's title, which is what the title
    page shows, so this is a check that the manual in the artifact is the manual for this release.
    A viewer is not needed, and none is shipped.
    """
    if not version:
        return True
    return version in version_stamps(path)


def installed_manuals(directory: Path) -> list[Path]:
    """The manual PDFs in a directory, in the order the list names them, missing ones skipped."""
    return [p for p in sorted(directory.glob("*.pdf")) if is_pdf(p)]


#: `src/ate/manuals.py` writes this line into `ate --help`, and the smoke tests read it back rather
#: than guessing where the install put the PDFs: the running program is the only one that knows.
MANUALS_LINE = re.compile(r"^manuals: (\d+) PDFs in (.+)$", re.MULTILINE)
NO_MANUALS_LINE = re.compile(r"^manuals: none", re.MULTILINE)


def manuals_directory(help_text: str) -> Path:
    """The directory `ate --help` names for the install that is running.

    `manuals: 3 PDFs in /opt/ate/_internal/ate/manuals`. An install that says it has no manuals is a
    release that shipped none; a help text with no manuals line at all is a packaging bug. The line
    is printed by a frozen bundle, a wheel in a pipx venv and a source checkout alike.
    """
    match = MANUALS_LINE.search(help_text)
    if match:
        return Path(match.group(2))
    if NO_MANUALS_LINE.search(help_text):
        raise ValueError(f"this install has no manuals at all: {help_text.strip()[-300:]!r}")
    raise ValueError(f"`ate --help` printed no manuals line: {help_text.strip()[-300:]!r}")


def problems_with(directory: Path, version: str | None = None) -> list[str]:
    """Every way the PDFs in an install fail to be this release's manuals.

    Empty means every name in the list is there, is a PDF by its first four bytes, and carries the
    version being released in its title. The smoke scripts print what comes back here; they do not
    decide it themselves. A PDF in that directory that the list does not name is also a problem:
    only `docs/manuals/build.py` puts files there.
    """
    names = set(manual_pdf_names())
    if not directory.is_dir():
        return [f"{directory} does not exist"]
    problems = []
    for name in manual_pdf_names():
        pdf = directory / name
        if not pdf.is_file():
            problems.append(f"{name} is not there")
        elif not is_pdf(pdf):
            problems.append(f"{name} is not a PDF (first bytes {pdf.read_bytes()[:4]!r})")
        elif version and not carries_version(pdf, version):
            problems.append(f"{name} says {pdf_title(pdf)!r}, not the version being released ({version})")
    extra = sorted(p.name for p in directory.glob("*.pdf") if p.name not in names)
    if extra:
        problems.append(f"PDFs this release did not build: {', '.join(extra)}")
    return problems


def bytes_of(directory: Path) -> int:
    """The manuals' contribution to an artifact's size, in bytes."""
    return sum(p.stat().st_size for p in installed_manuals(directory))
