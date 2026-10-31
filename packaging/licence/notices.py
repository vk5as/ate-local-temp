#!/usr/bin/env python3
"""Write the licence notices every distributed ATE artifact carries (packet 32a).

    python packaging/licence/notices.py --out build/licences

Three files land in ``--out``:

  NOTICE-Qt-PySide6.txt    what Qt and PySide6 are in this bundle, their licence, the version,
                           where the source is, and that Qt can be replaced
  THIRD_PARTY_NOTICES.txt  every runtime dependency, its version and the licence its wheel
                           declares, including what those dependencies themselves require
  LGPL-3.0.txt, GPL-3.0.txt  the full texts, copied from this directory

Generated from what is installed rather than written by hand, so the version numbers are the
ones the bundle really carries. A licence the wheel does not name, or that this script cannot
name, is reported: nothing here is guessed. Needs ``packaging``, which PyInstaller already brings
into the build venv. ``packaging/build_pyinstaller.py`` runs this and puts the result in the
bundle, where ``verify_bundle()`` refuses to ship it if any of it is missing.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import re
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

HERE = Path(__file__).resolve().parent

#: A licence name this script recognises as permissive. Each term of a declaration has to be one of
#: these in full: a word found inside a sentence proves nothing ("Not MIT", "unlimited seats").
_PERMISSIVE = re.compile(
    r"(?:the\s+)?"
    r"(?:mit|0bsd|bsd(?:-\d-clause(?:-clear)?)?|apache(?:-2\.0|\s+2\.0|\s+software)?|isc|psf(?:-2\.0)?"
    r"|python-2\.0|python\s+software\s+foundation)"
    r"(?:\s+licen[cs]e)?(?:\s+\(iscl\))?",
    re.IGNORECASE,
)
#: An SPDX id's version ("-3.0-only", "-2.1+", "v3") and a classifier's ("v3 or later (LGPLv3+)").
_SPDX_VERSION = r"(?:-?v?\d+(?:\.\d+)?(?:-only|-or-later|\+)?)?"
_CLASSIFIER_VERSION = r"(?:\s+v\d+(?:\s+or\s+later)?)?(?:\s+\({0}" + _SPDX_VERSION + r"\))?"
_LGPL = re.compile(
    r"(?:gnu\s+)?(?:lgpl" + _SPDX_VERSION + r"|(?:library\s+or\s+)?lesser\s+general\s+public\s+licen[cs]e"
    + _CLASSIFIER_VERSION.format("lgpl") + ")",
    re.IGNORECASE,
)
_GPL = re.compile(
    r"(?:gnu\s+)?(?:a?gpl" + _SPDX_VERSION + r"|(?:affero\s+)?general\s+public\s+licen[cs]e"
    + _CLASSIFIER_VERSION.format("a?gpl") + ")",
    re.IGNORECASE,
)

PERMISSIVE_NOTE = "permissive"
LGPL_NOTE = "copyleft (LGPL): keeps its own licence, which ATE's does not replace"

# The Lua that lupa embeds is not a distribution: lupa's wheel carries it compiled, as lupa.lua55.
LUA_NOTE = (
    "lupa embeds Lua, and ATE's bundle carries its Lua 5.5 build (lupa.lua55). Lua's licence: "
    "https://www.lua.org/license.html, the link lupa's own licence file gives."
)

#: Where Qt's source is, as a URL ``git clone`` takes. The wheels name PySide6's repository but not
#: Qt's. This is the same host's top-level Qt repository, with a tag for each release: ``git ls-remote``
#: on this URL found ``v6.11.3`` on 2026-11-01, and the host's ``/cgit/`` web page for it is not a remote.
QT_REPOSITORY = "https://code.qt.io/qt/qt5.git"


def _version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not installed in this interpreter"


def _qt_version() -> str:
    """The Qt the interpreter loaded, or the version PySide6's wheel ships when Qt will not import."""
    try:
        from PySide6.QtCore import qVersion

        return qVersion()
    except Exception:
        return _version("PySide6-Addons")


def _pyside_repository() -> str | None:
    """The source repository PySide6's own wheel names, or None when it names none."""
    try:
        urls = importlib.metadata.metadata("PySide6-Addons").get_all("Project-URL") or []
    except importlib.metadata.PackageNotFoundError:
        return None
    for entry in urls:
        label, _, url = entry.partition(",")
        if label.strip().lower() == "repository":
            return url.strip()
    return None


def _declared(name: str) -> list[str]:
    """Each licence a distribution's wheel names, from every metadata field that names one.

    A ``License`` field is sometimes the whole licence text, which is not a name: it counts only when
    nothing else names the licence, and then by its first line.
    """
    metadata = importlib.metadata.metadata(name)
    expression = " ".join((metadata.get("License-Expression") or "").split())
    field = (metadata.get("License") or "").strip()
    classifiers = [
        entry.split("::")[-1].strip() for entry in metadata.get_all("Classifier") or [] if entry.startswith("License ::")
    ]
    pasted = "\n" in field
    named = [expression] if expression else []
    if field and field.upper() != "UNKNOWN" and not pasted:
        named.append(" ".join(field.split()))
    named += classifiers
    if not named and pasted:
        named.append(field.splitlines()[0].strip())
    return named


def _kind(term: str) -> str:
    # Grouping parentheses of an expression go; "ISC License (ISCL)" keeps its own.
    for candidate in (term.strip(), term.strip(" ()")):
        for kind, pattern in (("permissive", _PERMISSIVE), ("lgpl", _LGPL), ("gpl", _GPL)):
            if pattern.fullmatch(candidate):
                return kind
    return "unknown"


def _classify_one(declaration: str) -> str:
    """"permissive", "lgpl", "gpl" or "unknown" for one declaration, reading OR and AND as SPDX does."""
    # A classifier's own "or" ("v3 or later") is part of one name, not a choice between two.
    if (whole := _kind(declaration)) != "unknown":
        return whole
    parts = re.split(r"\s+(or|and)\s+", declaration, flags=re.IGNORECASE)
    kinds, operators = [_kind(term) for term in parts[0::2]], {op.lower() for op in parts[1::2]}
    if "unknown" in kinds or len(operators) > 1:
        return "unknown"
    # An OR is the licensee's choice, so the most permissive option counts; an AND binds all of them.
    order = ["permissive", "lgpl", "gpl"]
    ranks = [order.index(kind) for kind in kinds]
    return order[min(ranks) if operators == {"or"} else max(ranks)]


def _classify(declared: list[str]) -> str:
    """One line on what a dependency's declared licences are. Anything unclear says "check upstream"."""
    if not declared:
        return "the wheel states no licence: check upstream"
    kinds = {_classify_one(declaration) for declaration in declared}
    if kinds == {"permissive"}:
        return PERMISSIVE_NOTE
    if kinds == {"lgpl"}:
        return LGPL_NOTE
    if kinds == {"gpl"}:
        return "GPL with no LGPL or permissive option: check upstream before shipping this with ATE"
    if len(kinds) > 1:
        return f"the wheel's licence fields disagree ({'; '.join(declared)}): check upstream"
    return f"the wheel declares {declared[0]!r}, which this script cannot name: check upstream"


def qt_notice() -> str:
    """The Qt/PySide6 notice that travels with every artifact: what this build did, not a verdict on it."""
    pyside = _version("PySide6-Addons")
    qt = _qt_version()
    shiboken = _version("shiboken6")
    repository = _pyside_repository()
    pyside_source = (
        f"PySide6 and shiboken6: {repository} (tag v{pyside}), the repository these wheels name."
        if repository
        else "PySide6 and shiboken6: the wheels used for this build name no source repository."
    )
    try:
        declared = " ".join((importlib.metadata.metadata("PySide6-Addons").get("License") or "").split())
    except importlib.metadata.PackageNotFoundError:
        declared = ""
    return f"""Qt and PySide6 in this bundle
=============================

ATE's interface and its Test Report renderer are Qt, used through PySide6.

  Qt:        {qt} (the C++ libraries under _internal/PySide6)
  PySide6:   {pyside} (the PySide6-Essentials and PySide6-Addons wheels)
  shiboken6: {shiboken} (PySide6's C++ object-lifetime runtime)

Licence
-------
The PySide6 wheels declare: {declared or "no licence"}.
The LGPL-3.0 and GPL-3.0 texts are in this directory.

Where the source is
-------------------
{pyside_source}
Qt: {QT_REPOSITORY} (tag v{qt}), Qt's own top-level repository. Its submodules hold qtbase,
qtwebengine and the other Qt libraries.

How this build uses Qt
----------------------
This bundle is one directory. The build copies Qt's shared libraries out of the PySide6 wheels as
separate files, unmodified: it does not link them into ATE's own code, merge them into one binary,
strip or compress them. They can be replaced with another build of the same Qt version by replacing
those files, without rebuilding ATE. ATE's own tests have not run against a replaced Qt.
"""


def _runtime_dependency_names() -> list[str]:
    """Every distribution the bundle's runtime needs: ATE's requirements and all of theirs."""
    try:
        roots = importlib.metadata.distribution("ate").requires or []
    except importlib.metadata.PackageNotFoundError:
        import tomllib

        with open(HERE.parents[1] / "pyproject.toml", "rb") as handle:
            roots = tomllib.load(handle)["project"].get("dependencies", [])

    found: set[str] = set()
    extras_seen: set[tuple[str, str]] = set()

    def wanted(requirement: Requirement, extra: str) -> bool:
        return requirement.marker is None or requirement.marker.evaluate({"extra": extra})

    def visit(requirements: list[str], extra: str) -> None:
        for line in requirements:
            requirement = Requirement(line)
            if not wanted(requirement, extra):
                continue
            name = canonicalize_name(requirement.name)
            new = name not in found
            found.add(name)
            fresh_extras = [e for e in requirement.extras if (name, e) not in extras_seen]
            extras_seen.update((name, e) for e in fresh_extras)
            try:
                requires = importlib.metadata.distribution(name).requires or []
            except importlib.metadata.PackageNotFoundError:
                continue  # listed by third_party_notices() as declared but not installed
            if new:
                visit(requires, "")
            for fresh in fresh_extras:
                visit(requires, fresh)

    visit(roots, "")
    return sorted(found)


def third_party_notices() -> str:
    """Every runtime dependency, its version and its licence, read off the installed wheels."""
    lines = [
        "Third-party licences in ATE",
        "==========================",
        "",
        "Generated from the wheels installed in the interpreter that built this bundle, walking every",
        "requirement of every requirement. The licence shown is what that wheel's metadata names; a",
        "licence it does not name, or that this script cannot name, is reported below, not guessed.",
        "",
    ]
    unclear: list[str] = []
    copyleft: list[str] = []
    for name in _runtime_dependency_names():
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            lines.append(f"  {name}: required but not installed here - licence not read: check upstream")
            unclear.append(name)
            continue
        declared = _declared(name)
        note = _classify(declared)
        if note == LGPL_NOTE:
            copyleft.append(name)
        if "check upstream" in note:
            unclear.append(name)
        lines.append(f"  {name} {version} - {' / '.join(declared) or 'no licence in the wheel metadata'}: {note}")

    lines += [
        "",
        LUA_NOTE,
        "",
        "Qt itself is not a Python distribution: it is the shared libraries under _internal/PySide6,",
        "listed above as part of PySide6. See NOTICE-Qt-PySide6.txt beside this file.",
        "",
        "Reported, not assumed:",
    ]
    if copyleft:
        lines.append(f"  copyleft runtime libraries: {', '.join(copyleft)}")
    if unclear:
        lines.append(f"  licences to check upstream: {', '.join(unclear)}")
    if not copyleft and not unclear:
        lines.append("  nothing: every runtime dependency above names a permissive licence")
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
