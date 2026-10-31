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
* Chromium will not build a renderer sandbox under root or an administrator, but this entry
  point never turns it off on its own (packet 30d). Only a process the parent told it to —
  the ``_pdf`` helper started with ``--unsandboxed`` after the Operator or the
  ``--allow-unsandboxed-pdf`` flag said yes — gets the sandbox-off knobs. An ordinary user
  keeps the sandbox, and a privileged ATE that was never asked renders no PDF at all.
"""

from __future__ import annotations

import os
import sys

# The helper argument that carries the packet 30d decision. Written out here rather than
# imported from ate.report.pdf because this module runs before ATE is imported, and a frozen
# bundle must set these knobs before QtWebEngine starts.
UNSANDBOXED_FLAG = "--unsandboxed"


def _decision_yes(argv: list[str]) -> bool:
    """True only when the parent explicitly asked this process to render without the sandbox."""
    return UNSANDBOXED_FLAG in argv


def _elevated() -> bool:
    """Root on Linux, an administrator on Windows.

    Written out here rather than imported from ``ate.report.pdf`` because this module runs
    before ATE is imported, and a frozen bundle must set these knobs before QtWebEngine starts.
    ``ATE_ELEVATED`` (the same seam as :data:`ate.report.pdf.ELEVATION_ENV`) answers first, so a
    test or a CI job can tell the bundle what privilege it is pretending to have."""
    forced = os.environ.get("ATE_ELEVATED", "").strip().lower()
    if forced in ("0", "false", "no", "off"):
        return False
    if forced in ("1", "true", "yes", "on"):
        return True
    if os.name == "nt":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


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


def _chromium_flags(allow_unsandboxed: bool = False) -> str:
    """Chromium flags for a headless render, added to whatever the caller already set.

    ``setdefault`` would throw away flags the packager or CI set; appending keeps them and makes
    ``--no-gpu`` apply whatever else is in there. ``--no-sandbox`` is added only when the packet 30d
    decision said yes for this process and it is privileged: a renderer under root or an
    administrator cannot build a sandbox, and nothing here turns it off on its own."""
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
    for flag in ("--no-gpu", "--disable-gpu"):
        if flag not in flags:
            flags.append(flag)
    if allow_unsandboxed and _elevated() and "--no-sandbox" not in flags:
        flags.append("--no-sandbox")
    return " ".join(flags)


def _strip_inherited_sandbox_off(where: str) -> list[str]:
    """Remove sandbox-off settings this process was never chosen to have.

    Written out here as well as in `ate.report.pdf` because the frozen entry point runs before ATE
    is imported at all: QtWebEngine reads `QTWEBENGINE_DISABLE_SANDBOX` and
    `QTWEBENGINE_CHROMIUM_FLAGS` when the first QCoreApplication starts and when it spawns
    `QtWebEngineProcess`, which is before `render_pdf` ever gets a chance to decide. Scrubbing them
    here means an exported `QTWEBENGINE_DISABLE_SANDBOX=1` cannot silently render a Test Report
    without Chromium's sandbox (packet 30d review). Only `--no-sandbox` is dropped from the flags,
    so every other flag the person set survives."""
    removed: list[str] = []
    if os.environ.get("QTWEBENGINE_DISABLE_SANDBOX"):
        os.environ.pop("QTWEBENGINE_DISABLE_SANDBOX")
        removed.append("QTWEBENGINE_DISABLE_SANDBOX")
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
    if "--no-sandbox" in flags:
        kept = [flag for flag in flags if flag != "--no-sandbox"]
        if kept:
            os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(kept)
        else:
            os.environ.pop("QTWEBENGINE_CHROMIUM_FLAGS")
        removed.append("QTWEBENGINE_CHROMIUM_FLAGS=--no-sandbox")
    if removed:
        print(
            f"ate WARNING: {where} inherited Chromium's sandbox switched off ({', '.join(removed)}); "
            "ATE did not choose that, so a Test Report PDF here renders with the sandbox on. For an "
            "unsandboxed PDF, run ATE with --allow-unsandboxed-pdf or answer yes to the dialog.",
            file=sys.stderr,
        )
    return removed


def _prepare_webengine_env(argv: list[str] | None = None) -> None:
    """Set the environment a frozen ATE needs before QtWebEngine starts.

    Only the frozen entry point calls it, but it is a function so the tests can run it without a
    frozen bundle. A headless render has no GPU to look for; the renderer sandbox is disabled only
    for a process the parent explicitly told to render unsandboxed (packet 30d), never because this
    process happens to be root or an administrator, and never because the environment already said
    so: inherited sandbox-off settings are removed first, in the same breath, so Qt never sees them."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    name = "QtWebEngineProcess.exe" if os.name == "nt" else "QtWebEngineProcess"
    helper = _webengine_process(base, name)
    if helper:
        os.environ["QTWEBENGINEPROCESS_PATH"] = helper
    elif os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        print(f"ate: no bundled {name} found under {base}", file=sys.stderr)
    allow_unsandboxed = _decision_yes(list(sys.argv[1:] if argv is None else argv)) and _elevated()
    if not allow_unsandboxed:
        _strip_inherited_sandbox_off("this ATE process")
    if allow_unsandboxed:
        # The parent's decision, carried in as --unsandboxed: the renderer sandbox cannot be built
        # under root or an administrator, so this one render runs without it.
        os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
        flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
        if "--no-sandbox" not in flags:
            os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags + ["--no-sandbox"])
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = _chromium_flags(allow_unsandboxed)


if getattr(sys, "frozen", False):
    _prepare_webengine_env()

from ate.cli import main

if __name__ == "__main__":
    sys.exit(main())
