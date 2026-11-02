"""Where this install keeps the manuals (packet 36).

The three PDF manuals are built by ``docs/manuals/build.py`` and staged in ``src/ate/manuals``
before anything is packaged, so the same files are inside every artifact:

| Install | Manuals at |
| --- | --- |
| frozen bundle (Windows installer, `.deb`, tarball) | `<install>/_internal/ate/manuals/*.pdf` |
| wheel, installed with pip or pipx | `<site-packages>/ate/manuals/*.pdf` |
| source checkout | `src/ate/manuals/*.pdf`, once someone has built them |

The `.deb` puts a copy of each PDF in `/usr/share/doc/ate/` as well, which is where Debian
expect documentation to be. ATE ships no PDF viewer and opens none of these files: it only says
where they are, in `ate --help`.
"""

from __future__ import annotations

import importlib.resources
import sys
from pathlib import Path

#: Where the .deb puts the same PDFs, as Debian's packaging policy asks.
DEB_DOC_DIR = Path("/usr/share/doc/ate")


def install_root() -> Path:
    """The directory this running ATE's data files live in.

    A frozen bundle has no importable ``ate`` package to ask: PyInstaller put the data files the spec
    collected in ``sys._MEIPASS``, which in a one-directory bundle is the ``_internal`` directory
    beside the executable. A wheel or a source checkout is the ``ate`` package directory.
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass) / "ate"
        return Path(sys.executable).parent / "_internal" / "ate"
    return Path(str(importlib.resources.files("ate")))


def manuals_dir() -> Path:
    """The directory this install carries the manual PDFs in, whether it has them or not."""
    return install_root() / "manuals"


def manual_files() -> list[Path]:
    """The manuals this install actually has: a PDF that exists and is not empty.

    Nothing is assumed about the count. A source checkout that has never run
    ``docs/manuals/build.py`` has no PDFs, and a build that staged only two of them says so.
    """
    directory = manuals_dir()
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.pdf") if p.is_file() and p.stat().st_size > 0)


def manuals_note() -> str:
    """One or two lines for ``ate --help``: where the manuals are in this install.

    The path is the real one this process is running from, so the answer is the same for a frozen
    bundle, a wheel in a pipx venv and a source checkout, and it is the path the smoke tests check.
    The Debian copy goes on its own line on purpose: ``packaging/manuals.py`` reads the first line,
    and a path followed by a parenthetical is a path nobody can parse.
    """
    directory = manuals_dir()
    files = manual_files()
    if files:
        lines = [f"manuals: {len(files)} PDFs in {directory}"]
        if DEB_DOC_DIR.is_dir():
            lines.append(f"the .deb also puts copies in {DEB_DOC_DIR}")
        return "\n".join(lines)
    return (
        f"manuals: none in this install ({directory} is empty). "
        "Build them with: python docs/manuals/build.py"
    )
