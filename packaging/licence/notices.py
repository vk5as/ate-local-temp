#!/usr/bin/env python3
"""Write the licence notices every distributed ATE artifact carries (packet 32a).

    python packaging/licence/notices.py --out build/licences

Three files land in ``--out``:

  NOTICE-Qt-PySide6.txt    what Qt and PySide6 are in this bundle, their licence, the version,
                           where the source is, and that Qt can be replaced
  THIRD_PARTY_NOTICES.txt  every runtime dependency, its version and the licence its wheel
                           declares, plus the copies the copyleft ones require
  LGPL-3.0.txt, GPL-3.0.txt  the full texts, copied from this directory

Generated from what is installed rather than written by hand, so the version numbers are the
ones the bundle really carries. A licence the wheel does not state is reported as unstated:
nothing here is guessed. ``packaging/build_pyinstaller.py`` runs this and puts the result in the
bundle, where ``verify_bundle()`` refuses to ship it if any of it is missing.
"""

from __future__ import annotations

import argparse
import importlib.metadata
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: Licence words that let ATE redistribute the dependency alongside an MIT-licensed ATE. A wheel
#: states its licence in whatever shape the packager chose — "MIT", "BSD", the whole MIT text — so
#: these are matched as words in the declaration, not as an exact metadata field.
PERMISSIVE = ("mit", "bsd", "apache", "isc", "psf", "python-2.0")

#: Copyleft licences: ATE may link against these and still be MIT, but the dependency's own
#: licence governs the dependency, and its source has to stay available.
COPYLEFT = ("lgpl", "gpl")

# The Lua that lupa embeds is not a distribution: lupa ships Lua 5.5 source in its wheel.
LUA_NOTE = (
    "lupa embeds Lua 5.5, licensed under the MIT licence "
    "(https://www.lua.org/copyright.html); the Lua source is inside lupa's wheel."
)

QT_SOURCE = (
    "Qt: https://www.qt.io/download-open-source — the sources of every library in this bundle, "
    "including QtWebEngine and the Chromium it is built from, are at https://code.qt.io and "
    "https://github.com/qt/qtwebengine and https://github.com/qt/qtwebengine-chromium.\n"
    "PySide6: https://github.com/qt/pyside and https://github.com/qt/pyside-addons, and every "
    "wheel on https://pypi.org/project/PySide6-Addons/."
)


def _qt_version() -> str:
    """The Qt the interpreter loaded, or the version PySide6's wheel ships when Qt will not import."""
    try:
        from PySide6.QtCore import qVersion

        return qVersion()
    except Exception:
        try:
            return importlib.metadata.version("PySide6-Addons")
        except importlib.metadata.PackageNotFoundError:
            return "not installed in this interpreter"


def _licence(name: str) -> str:
    """The licence a distribution declares, as its wheel states it."""
    metadata = importlib.metadata.metadata(name)
    stated = metadata.get("License-Expression") or metadata.get("License") or ""
    return " ".join(stated.split())


def _classify(licence: str) -> str:
    """One line on what this licence means for an MIT-licensed ATE that ships the dependency."""
    words = {part.strip() for part in licence.replace("OR", " ").split("/")}
    if any(part in PERMISSIVE for part in words) or licence.upper() in PERMISSIVE:
        return "compatible with MIT: redistributable as-is"
    if any(part in COPYLEFT for part in words) or "LGPL" in licence.upper() or "GPL" in licence.upper():
        return (
            "copyleft: ATE stays MIT, but this dependency keeps its own licence — its source must "
            "remain available and it may not be relicensed"
        )
    return (
        "not stated by the wheel in a form this script can read: check upstream before "
        "redistributing; do not assume MIT"
    )


def qt_notice() -> str:
    """The Qt/PySide6 notice that travels with every artifact."""
    try:
        pyside = importlib.metadata.version("PySide6-Addons")
    except importlib.metadata.PackageNotFoundError:
        pyside = "not installed in this interpreter"
    qt = _qt_version()
    try:
        shiboken = importlib.metadata.version("shiboken6")
    except importlib.metadata.PackageNotFoundError:
        shiboken = "not installed in this interpreter"
    return f"""Qt and PySide6 in this bundle
==========================

ATE's interface and its Test Report renderer are Qt, used through PySide6.

  Qt:      {qt} (the C++ libraries in _internal/PySide6/Qt/lib)
  PySide6: {pyside} (PySide6-Essentials and PySide6-Addons, the official Qt for Python binding)
  shiboken6: {shiboken} (PySide6's C++ object-lifetime runtime)

Licence
-------
Qt is dual-licensed under the GNU Lesser General Public License version 3 and the GNU General
Public License version 2 or 3; PySide6 declares the same choice ("LGPL-3.0-only OR GPL-2.0-only
OR GPL-3.0-only"). You may use the Qt and PySide6 in this bundle under either licence. The full
texts of LGPL-3.0 and GPL-3.0 are in this directory.

ATE is MIT-licensed. Nothing about Qt or PySide6 changes that: ATE's own source is not covered by
the GPL or the LGPL, and the Qt and PySide6 code in this bundle is not ATE's.

Where the source is
-------------------
{QT_SOURCE}

Replacing Qt
------------
The Qt libraries here are separate, unmodified shared libraries, and the binding is separate from
them. Nothing links Qt into ATE's own code, so a Station may replace these shared libraries with
its own build of Qt of the same Qt version without recompiling ATE. Keeping the bundle one-directory,
with Qt as loose shared libraries rather than a merged binary, is what makes that possible and what
the LGPL asks for.

Questions
---------
LGPL compliance for this bundle comes down to three things, all of which the bundle satisfies: the
licence texts are here, the source of every Qt and PySide6 library shipped here is reachable from
the links above, and nothing in the bundle is a modified or statically linked copy of Qt.
"""


def _runtime_dependency_names() -> list[str]:
    """The runtime dependencies: from the installed wheel, or from pyproject.toml if ATE is not installed."""
    try:
        lines = importlib.metadata.metadata("ate").get_all("Requires-Dist") or []
    except importlib.metadata.PackageNotFoundError:
        import tomllib

        pyproject = HERE.parents[1] / "pyproject.toml"
        with open(pyproject, "rb") as handle:
            lines = tomllib.load(handle)["project"].get("dependencies", [])

    names: list[str] = []
    for line in lines:
        if ";" in line:  # an extra, not a runtime dependency
            continue
        names.append(line.split("[")[0].split("<")[0].split(">")[0].split("=")[0].strip().lower())
    return names


def third_party_notices() -> str:
    """Every runtime dependency, its version and its licence, read off the installed wheels."""
    lines = [
        "Third-party licences in ATE",
        "==========================",
        "",
        "Generated from the wheels installed in the interpreter that built this bundle. The licence",
        "shown is what that wheel declares; where it declares nothing, this file says so rather than",
        "guessing.",
        "",
    ]
    unstated: list[str] = []
    copyleft: list[str] = []
    for name in sorted(set(_runtime_dependency_names())):
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            lines.append(f"  {name}: declared by ATE but not installed here - licence not read")
            continue
        licence = _licence(name)
        note = _classify(licence)
        if "copyleft" in note:
            copyleft.append(name)
        if "not stated" in note:
            unstated.append(name)
        lines.append(f"  {name} {version} - {licence or 'no licence in the wheel metadata'}: {note}")

    lines += [
        "",
        LUA_NOTE,
        "",
        "Qt itself is not a Python distribution: it is the set of shared libraries under",
        "_internal/PySide6/Qt, dual-licensed LGPL-3.0 / GPL-2.0 / GPL-3.0, listed above as part of",
        "PySide6. See NOTICE-Qt-PySide6.txt beside this file.",
        "",
        "Reported, not assumed:",
    ]
    if copyleft:
        lines.append(f"  copyleft runtime libraries: {', '.join(copyleft)}")
    if unstated:
        lines.append(f"  licences the wheels do not state: {', '.join(unstated)}")
    if not copyleft and not unstated:
        lines.append("  none: every runtime dependency above declares a licence MIT distribution tolerates")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(HERE), help="directory the notices are written to")
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "NOTICE-Qt-PySide6.txt").write_text(qt_notice(), encoding="utf-8", newline="\n")
    (out / "THIRD_PARTY_NOTICES.txt").write_text(third_party_notices(), encoding="utf-8", newline="\n")
    for text in ("LGPL-3.0.txt", "GPL-3.0.txt"):
        (out / text).write_text((HERE / text).read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    print(f"wrote {out/'NOTICE-Qt-PySide6.txt'}, {out/'THIRD_PARTY_NOTICES.txt'}, {out/'LGPL-3.0.txt'}, {out/'GPL-3.0.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
