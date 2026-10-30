"""Entry point for the frozen ATE bundle (packet 27a).

``pyproject.toml``'s console script is ``ate = "ate.cli:main"``; a PyInstaller executable
has no console script, so the spec runs this instead. It does only what a frozen app needs
that the plain CLI does not:

* QtWebEngine spawns ``QtWebEngineProcess`` outside the Python process. Qt looks for it
  beside its own libraries, which the bundle's layout changes, so point it at the copy
  bundled next to the executable.
* Headless runs (``QT_QPA_PLATFORM=offscreen``, the PDF helper) have no GPU to show a page
  on; Chromium is told not to look for one. The GUI is unaffected.
"""

from __future__ import annotations

import os
import sys

if getattr(sys, "frozen", False):
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    name = "QtWebEngineProcess.exe" if os.name == "nt" else "QtWebEngineProcess"
    # PyInstaller's Qt hooks put the helper under the bundled Qt tree; point Qt at it
    # wherever this build's Qt put it.
    helper = next(
        (path for path in (
            os.path.join(base, "PyQt6", "Qt6", "libexec", name),
            os.path.join(base, "PyQt6", "Qt", "libexec", name),
            os.path.join(base, "libexec", name),
            os.path.join(base, name),
        ) if os.path.exists(path)),
        None,
    )
    if helper:
        os.environ.setdefault("QTWEBENGINEPROCESS_PATH", helper)
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-gpu --disable-gpu")

from ate.cli import main

if __name__ == "__main__":
    sys.exit(main())
