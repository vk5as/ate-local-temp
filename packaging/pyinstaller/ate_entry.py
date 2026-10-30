"""Entry point for the frozen ATE bundle (packet 27a).

``pyproject.toml``'s console script is ``ate = "ate.cli:main"``; a PyInstaller executable
has no console script, so the spec runs this instead. It does only what a frozen app needs
that the plain CLI does not:

* QtWebEngine spawns its helper process outside the Python process. Qt looks for it beside
  its own libraries, which the bundle's layout changes, so point it at the copy the hooks
  collected. Where that copy is differs by platform: PyQt6 wheels keep it in ``Qt6/libexec``
  on Linux and ``Qt6/bin`` on Windows (``QtWebEngineProcess.exe`` there).
* Headless runs (``QT_QPA_PLATFORM=offscreen``, the PDF helper) have no GPU to show a page
  on; Chromium is told not to look for one. The GUI is unaffected.
"""

from __future__ import annotations

import os
import sys


def _webengine_process(base: str, name: str) -> str | None:
    """The bundled WebEngine helper, wherever this build's Qt put it.

    ``base`` is the bundle's ``_internal`` directory. The guesses are the layouts PyInstaller's
    Qt hooks collect into; the walk is the safety net for a Qt build that keeps its executables
    somewhere else, so a bundle that has the helper is never refused for a wrong guess.
    """
    for rel in (
        ("PyQt6", "Qt6", "bin", name),  # PyQt6 wheels on Windows
        ("PyQt6", "Qt6", "libexec", name),  # PyQt6 wheels on Linux
        ("PyQt6", "Qt", "libexec", name),  # PyQt6 before the Qt6 layout change
        (name,),  # collected in the bundle root
    ):
        path = os.path.join(base, *rel)
        if os.path.isfile(path):
            return path
    for root, _dirs, files in os.walk(base):
        if name in files:
            return os.path.join(root, name)
    return None


def _chromium_flags() -> str:
    """Chromium flags for a headless render, added to whatever the caller already set.

    ``setdefault`` would throw away flags the packager or CI set; appending keeps them and makes
    ``--no-gpu`` apply whatever else is in there.
    """
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
    for flag in ("--no-gpu", "--disable-gpu"):
        if flag not in flags:
            flags.append(flag)
    return " ".join(flags)


if getattr(sys, "frozen", False):
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    name = "QtWebEngineProcess.exe" if os.name == "nt" else "QtWebEngineProcess"
    helper = _webengine_process(base, name)
    if helper:
        os.environ["QTWEBENGINEPROCESS_PATH"] = helper
    elif os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        print(f"ate: no bundled {name} found under {base}", file=sys.stderr)
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = _chromium_flags()

from ate.cli import main

if __name__ == "__main__":
    sys.exit(main())
