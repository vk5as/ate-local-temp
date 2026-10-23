"""Turn a saved HTML Test Report into PDF through a separate helper process.

The GUI (ticket 22) never renders to PDF itself: it spawns ``ate _pdf <html> <pdf> --paper A4`` in a
child process and waits for it, raising on failure or timeout so the Run/History screens can never
stall. The child runs :func:`render_pdf`, which loads the *saved file* (never ``setHtml``) into
QWebEngine and calls ``printToPdf`` with ``QPageLayout`` margins equal to the template's CSS
``@page`` margins (:data:`REPORT_PAGE_MARGINS_MM`).

Public API:
    render_pdf(html_path, pdf_path, paper="A4"|"Letter") -> int   # runs inside the helper process
    run_pdf_helper(html_path, pdf_path, paper="A4", *, timeout=30.0) -> None  # GUI side; raises on failure/timeout
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from .html import REPORT_PAGE_MARGINS_MM


def _child_env() -> dict[str, str]:
    """The child helper's environment — a curated minimum rather than ``os.environ.copy()``.

    QtWebEngine builds an offscreen GL context at startup; inheriting the parent process's full
    environment (which under tests carries sandbox/agent variables) makes that context creation fail
    intermittently, so no PDF is written and the helper exits non-zero. A small, explicit set — PATH,
    HOME, the offscreen platform, locale, any library search path already in play, and PYTHONPATH —
    renders reliably across repeated runs.
    """
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        # Headless rendering (tests, CI) — set here so the child does not inherit a display that is
        # only attached to the parent's event loop.
        "QT_QPA_PLATFORM": "offscreen",
    }
    # Locale keeps text/glyph handling consistent with the process that produced the report.
    for key in ("LANG", "LANGUAGE"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    # Library search paths Qt may need to find its bundled system libraries.
    for key in ("LD_LIBRARY_PATH",):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def render_pdf(html_path: str, pdf_path: str, paper: str = "A4") -> int:
    """Load ``html_path`` into QWebEngine and print it to ``pdf_path``. Runs inside the helper process.

    Returns the intended process exit code (0 on success). A missing or unloadable HTML file, a bad
    paper size, or a failed print all yield a non-zero code — never ``setHtml`` is used.
    """
    if paper not in ("A4", "Letter"):
        raise ValueError(f"paper must be 'A4' or 'Letter', got {paper!r}")

    # Headless-friendly: prefer the offscreen platform when no display is attached (tests, CI).
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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
    return 0 if loaded_ok["value"] and result_ok["value"] and file_ok["value"] else 1


def run_pdf_helper(html_path: str, pdf_path: str, paper: str = "A4", *, timeout: float = 30.0) -> None:
    """Start a separate helper process to render ``html_path`` to ``pdf_path``; raise on failure/timeout.

    The child is the hidden ``ate _pdf`` subcommand (see :func:`_cmd_pdf_helper`). When ATE is frozen
    by PyInstaller, ``sys.executable`` runs the app and forwards the subcommand; otherwise it runs
    ``-m ate``. Raises :class:`TimeoutError` on timeout and :class:`RuntimeError` if the child exits
    non-zero (including a missing HTML file).
    """
    import subprocess

    frozen = getattr(sys, "frozen", False)
    # A curated minimum (see :func:`_child_env`) — never the parent's full environment.
    env = _child_env()
    if frozen:
        call = [sys.executable, "_pdf", str(html_path), str(pdf_path), "--paper", paper]
    else:
        call = [sys.executable, "-m", "ate", "_pdf", str(html_path), str(pdf_path), "--paper", paper]
        # Make the local checkout importable even when 'ate' is not on sys.path here (e.g. an
        # editable install that points at a different tree); cwd/src shadows any stale entry.
        src = str(Path.cwd() / "src")
        prev = os.environ.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{src}{os.pathsep}{prev}"

    try:
        proc = subprocess.run(call, env=env, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - surface any launch/capture failure to the caller
        raise
    except subprocess.TimeoutExpired as exc:
        # The child owns its own Qt event loop; kill it if it is still running.
        target = getattr(exc, "process", None)
        if target is not None and target.poll() is None:
            try:
                target.kill()
            except OSError:
                pass
        raise TimeoutError(f"pdf helper timed out after {timeout:g}s") from exc
    except FileNotFoundError as exc:
        raise RuntimeError(f"could not launch pdf helper: {exc}") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"pdf helper exited with code {proc.returncode}: {detail}")
    # Success — the child produced a PDF and exited 0.
