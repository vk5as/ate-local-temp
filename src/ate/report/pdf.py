"""Turn a saved HTML Test Report into PDF through a separate helper process.

The GUI (ticket 22) never renders to PDF itself: it spawns ``ate _pdf <html> <pdf> --paper A4`` in a
child process and waits for it, raising on failure or timeout so the Run/History screens can never
stall. The child runs :func:`render_pdf`, which loads the *saved file* (never ``setHtml``) into
QWebEngine and calls ``printToPdf`` with ``QPageLayout`` margins equal to the template's CSS
``@page`` margins (:data:`REPORT_PAGE_MARGINS_MM`).

Public API:
    render_pdf(html_path, pdf_path, paper="A4"|"Letter") -> int   # runs inside the helper process
    pdf_helper_command(html_path, pdf_path, paper="A4") -> (program, args, env)  # for QProcess
    run_pdf_helper(html_path, pdf_path, paper="A4", *, timeout=30.0) -> None  # blocking; raises on failure/timeout
"""

from __future__ import annotations

import os
import re
import sys
import zlib
from pathlib import Path
from typing import Optional

from .html import REPORT_PAGE_MARGINS_MM

# A text-showing operator in a content stream: `Tj` shows a string, `TJ` a string array, and the
# quote/double-quote operators are shorthand for a line break plus a string.
_TEXT_OPERATORS = re.compile(rb"(?:T[Jj]|'|\")")
_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)


def pdf_has_text(pdf: bytes) -> bool:
    r"""Whether a PDF really contains text, not just a PDF header.

    QtWebEngine prints the report through Chromium, which embeds the glyphs it draws as a font
    program (`/FontFile`, `/FontFile2`, `/FontFile3`). A stock Debian container has no fonts at
    all, so Chromium finds none, draws no glyphs, and the file is a page with an empty content
    stream: 1.7 KB, a valid `%PDF` header, and nothing a reader can see. A report that is a valid
    PDF but has no text in it is a packaging failure, not a rendering failure, so `ate _selftest
    --run` checks this rather than trusting the header.

    Two layers: an embedded font program is conclusive; otherwise every stream is inflated and
    searched for text operators, which also catches a PDF that draws text with a font it did not
    embed.
    """
    if not pdf.startswith(b"%PDF"):
        return False
    if b"/FontFile" in pdf:  # /FontFile, /FontFile2 and /FontFile3 all start with this
        return True
    for raw in _STREAM.findall(pdf):
        try:
            data = zlib.decompress(raw)
        except zlib.error:
            data = raw
        if b"/FontFile" in data or _TEXT_OPERATORS.search(data):
            return True
    return False


def _is_elevated() -> bool:
    """Whether this process runs with privileges Chromium's sandbox is known to refuse.

    On Linux the renderer refuses to start as root at all: `ERROR:zygote_host_impl_linux.cc
    Running as root without --no-sandbox is not supported` (crbug.com/638180). On Windows the
    sandbox builds a restricted token for the renderer, and an elevated (High IL) parent has
    been reported to fail that — Electron's `render-process-gone exitCode 18` under
    Administrator is the same failure. ``ate setup`` is run elevated by the installer and by a
    ``.deb`` postinst, so an admin rendering a Test Report is not hypothetical."""
    if os.name == "nt":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def _sandbox_flags(env: dict[str, str]) -> dict[str, str]:
    """Turn Chromium's sandbox off for an elevated/root render, and say which knob did it.

    Qt WebEngine reads both `QTWEBENGINE_DISABLE_SANDBOX` and `QTWEBENGINE_CHROMIUM_FLAGS`; the
    flags are appended, never replaced, so whatever the packager or CI set is kept."""
    if not _is_elevated():
        return env
    env["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
    flags = env.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
    if "--no-sandbox" not in flags:
        flags.append("--no-sandbox")
    env["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags)
    return env


def _child_env() -> dict[str, str]:
    """The helper's environment: the parent's, with the offscreen platform forced and, when
    this process is privileged, Chromium's sandbox disabled.

    The helper never shows a window, so it must not depend on a display. Everything else is
    inherited, which Windows needs (Python won't start without ``SYSTEMROOT`` and friends)."""
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    return _sandbox_flags(env)


def render_pdf(html_path: str, pdf_path: str, paper: str = "A4") -> int:
    """Load ``html_path`` into QWebEngine and print it to ``pdf_path``. Runs inside the helper process.

    Returns the intended process exit code (0 on success). A missing or unloadable HTML file, a bad
    paper size, or a failed print all yield a non-zero code — never ``setHtml`` is used.
    """
    if paper not in ("A4", "Letter"):
        raise ValueError(f"paper must be 'A4' or 'Letter', got {paper!r}")

    # Headless-friendly: prefer the offscreen platform when no display is attached (tests, CI).
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if _is_elevated():
        # `ate _pdf` can be started directly by an operator, so the sandbox knobs are set here
        # as well as in the parent's env: Chromium will not sandbox a renderer whose parent is
        # root or an administrator.
        os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
        flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
        if "--no-sandbox" not in flags:
            os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags + ["--no-sandbox"])

    from PyQt6.QtCore import QMarginsF, QUrl, QTimer
    from PyQt6.QtGui import QPageLayout, QPageSize
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtWebEngineCore import QWebEnginePage

    app = QApplication.instance() or QApplication(sys.argv)
    html_path = Path(html_path)
    pdf_path = Path(pdf_path)

    # Headless rendering (tests, CI): the offscreen platform is already forced above. Qt's RHI may
    # still probe OpenGL/Vulkan for a GPU context; when neither can be created here it falls back to
    # software rendering, which writes the PDF fine. We keep that path but never trust the
    # pdfPrintingFinished success flag alone — see _on_printed below.
    page = QWebEnginePage()
    layout = QPageLayout(
        QPageSize(QPageSize.PageSizeId[paper]),
        QPageLayout.Orientation.Portrait,
        QMarginsF(*[REPORT_PAGE_MARGINS_MM[k] for k in ("left", "top", "right", "bottom")]),
        QPageLayout.Unit.Millimeter,
    )

    loaded_ok = {"value": False}
    result_ok = {"value": None}
    file_ok = {"value": False}

    def _on_load(ok: bool) -> None:
        # If the HTML never loads (missing file, bad URL), there is nothing to print — quit now so a
        # failed render does not wait out the print timeout. Otherwise hand off to printToPdf, which
        # will fire pdfPrintingFinished and end the loop itself.
        loaded_ok["value"] = bool(ok)
        if ok:
            page.printToPdf(str(pdf_path), layout)
        else:
            app.quit()

    def _on_printed(_path: str, success: bool) -> None:
        # The signal can fire with success=True even when no bytes were written (a failed GL context
        # leaves nothing on disk). Confirm the file exists and is non-trivial before declaring success.
        file_ok["value"] = Path(pdf_path).exists() and Path(pdf_path).stat().st_size > 0
        result_ok["value"] = bool(success)
        app.quit()

    page.pdfPrintingFinished.connect(_on_printed)
    page.loadFinished.connect(_on_load)
    page.load(QUrl.fromLocalFile(str(html_path)))
    # Safety net only — the two signal handlers above end the loop in every normal case.
    QTimer.singleShot(30_000, app.quit)

    app.exec()
    if loaded_ok["value"] and result_ok["value"] and file_ok["value"]:
        return 0
    if _is_elevated():
        print(
            "pdf render failed while running elevated: Chromium cannot build a renderer sandbox "
            "under root or an administrator (ATE sets QTWEBENGINE_DISABLE_SANDBOX=1 and --no-sandbox)",
            file=sys.stderr,
        )
    return 1


def pdf_helper_command(html_path: str, pdf_path: str, paper: str = "A4") -> tuple[str, list[str], dict[str, str]]:
    """Return ``(program, args, env)`` that runs the hidden ``ate _pdf`` helper for one report.

    Frozen by PyInstaller, ``sys.executable`` is ATE itself and takes the subcommand directly;
    otherwise it is Python running ``-m ate``, with this package's own ``src`` directory first on
    ``PYTHONPATH`` so the child imports the same ATE as the parent."""
    tail = ["_pdf", str(html_path), str(pdf_path), "--paper", paper]
    env = _child_env()
    if getattr(sys, "frozen", False):
        return sys.executable, tail, env
    src = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH", "")) if p)
    return sys.executable, ["-m", "ate", *tail], env


def run_pdf_helper(html_path: str, pdf_path: str, paper: str = "A4", *, timeout: float = 30.0) -> None:
    """Run the helper process to completion, blocking; raise on failure or timeout.

    For callers that may block (CLI, History regeneration on a worker). The run session uses
    :func:`pdf_helper_command` with a ``QProcess`` instead so the GUI thread never waits.
    Raises :class:`TimeoutError` on timeout and :class:`RuntimeError` if the child can't start or
    exits non-zero (including a missing HTML file)."""
    import subprocess

    program, args, env = pdf_helper_command(html_path, pdf_path, paper)
    try:
        proc = subprocess.run([program, *args], env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:  # subprocess.run kills the child before raising
        raise TimeoutError(f"pdf helper timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise RuntimeError(f"could not launch pdf helper: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"pdf helper exited with code {proc.returncode}: {detail}")
