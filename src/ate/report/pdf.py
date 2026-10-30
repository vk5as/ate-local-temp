"""Turn a saved HTML Test Report into PDF through a separate helper process.

The GUI (ticket 22) never renders to PDF itself: it spawns ``ate _pdf <html> <pdf> --paper A4`` in a
child process and waits for it, raising on failure or timeout so the Run/History screens can never
stall. The child runs :func:`render_pdf`, which loads the *saved file* (never ``setHtml``) into
QWebEngine and calls ``printToPdf`` with ``QPageLayout`` margins equal to the template's CSS
``@page`` margins (:data:`REPORT_PAGE_MARGINS_MM`).

Chromium's renderer sandbox does not start under root or an administrator (packet 30d), so a PDF
rendered there needs ``--no-sandbox``, which is a real loss of isolation and is never done silently:
the parent decides first (:func:`pdf_sandbox_choice`) and tells this helper with an explicit
``--unsandboxed`` argument. A helper run directly as root without it refuses.

Public API:
    pdf_sandbox_choice(allow_unsandboxed, ask, log) -> callable returning a plan  # packet 30d
    render_pdf(html_path, pdf_path, paper="A4"|"Letter", unsandboxed=False) -> int  # in the helper
    pdf_helper_command(html_path, pdf_path, paper="A4", unsandboxed=False) -> (program, args, env)
    run_pdf_helper(html_path, pdf_path, paper="A4", unsandboxed=False, *, timeout=30.0) -> None
"""

from __future__ import annotations

import os
import re
import sys
import zlib
from pathlib import Path
from typing import Callable, Optional

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


# The three answers a run-end or a History Regenerate can get (packet 30d). An ordinary user is
# never asked and never loses the sandbox; only a privileged process has to choose.
PDF_SANDBOXED = "sandboxed"  # render with Chromium's sandbox: the normal case
PDF_UNSANDBOXED = "unsandboxed"  # the Operator or --allow-unsandboxed-pdf said yes for this PDF
PDF_SKIPPED = "skipped"  # privileged and nobody said yes: the HTML stands, no PDF is written

UNSANDBOXED_FLAG = "--unsandboxed"  # the only way the parent tells the helper the choice was Yes


def _note(log: Optional[Callable[[str, str, str], None]], level: str, message: str) -> None:
    """Record one decision in the application log, or on stdout when there is no log to write to."""
    if log is not None:
        log(level, "ate", message)
    else:
        print(message)


def pdf_sandbox_choice(
    allow_unsandboxed: bool = False,
    ask: Optional[Callable[[], bool]] = None,
    log: Optional[Callable[[str, str, str], None]] = None,
) -> Callable[[], str]:
    """Build the one decision every PDF asks (packet 30d): sandboxed, unsandboxed, or skipped.

    Built at launch and handed down to the run session and to History, so neither of them reads the
    environment or guesses at privilege. Each call answers for one PDF:

    * not root and not an administrator — :data:`PDF_SANDBOXED`, nothing is asked;
    * privileged and ``--allow-unsandboxed-pdf`` was given — :data:`PDF_UNSANDBOXED`, no dialog,
      logged;
    * privileged and there is a dialog to ask — its answer; No (or a dismissed dialog) is
      :data:`PDF_SKIPPED`, and the HTML Test Report is still written;
    * privileged with no way to ask (headless, ``_selftest --run``) — :data:`PDF_SKIPPED`.
    """

    def decide() -> str:
        if not _is_elevated():
            return PDF_SANDBOXED
        if allow_unsandboxed:
            _note(log, "WARNING", "--allow-unsandboxed-pdf: this Test Report PDF renders without Chromium's sandbox (ATE is running as root or an administrator)")
            return PDF_UNSANDBOXED
        if ask is None:
            _note(log, "WARNING", "ATE is privileged and there is no dialog to ask and no --allow-unsandboxed-pdf: the Test Report PDF is skipped rather than rendered with the sandbox off")
            return PDF_SKIPPED
        if ask():
            _note(log, "WARNING", "the Operator chose to render this Test Report PDF without Chromium's sandbox")
            return PDF_UNSANDBOXED
        _note(log, "INFO", "the Operator declined an unsandboxed PDF: the Test Report is HTML only")
        return PDF_SKIPPED

    return decide


def _sandbox_flags(env: dict[str, str], allow_unsandboxed: bool) -> dict[str, str]:
    """Turn Chromium's sandbox off for one render — only when the choice said yes *and* this
    process is privileged.

    A flag handed to an ordinary user is ignored: the sandbox is a real protection and it works
    there, so nothing is given up. Qt WebEngine reads both `QTWEBENGINE_DISABLE_SANDBOX` and
    `QTWEBENGINE_CHROMIUM_FLAGS`; the flags are appended, never replaced, so whatever the packager
    or CI set is kept."""
    if not allow_unsandboxed or not _is_elevated():
        return env
    env["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
    flags = env.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
    if "--no-sandbox" not in flags:
        flags.append("--no-sandbox")
    env["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags)
    return env


def _child_env(allow_unsandboxed: bool = False) -> dict[str, str]:
    """The helper's environment: the parent's, with the offscreen platform forced, and Chromium's
    sandbox disabled only when this render was chosen unsandboxed (packet 30d).

    The helper never shows a window, so it must not depend on a display. Everything else is
    inherited, which Windows needs (Python won't start without ``SYSTEMROOT`` and friends)."""
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    return _sandbox_flags(env, allow_unsandboxed)


def _apply_sandbox_decision(unsandboxed: bool) -> bool:
    """Apply this helper's packet 30d decision to its own environment; True means refuse.

    Three cases, all decided by the parent's argument rather than by who started the process:

    * not told to, and privileged — refuse: Chromium cannot sandbox a renderer under root or an
      administrator, and ATE never turns the sandbox off without being asked;
    * told to, and privileged — set the knobs for this one render;
    * told to, but ordinary — warn and keep the sandbox: the flag buys nothing there.
    """
    if not unsandboxed:
        if _is_elevated():
            print(
                "ate _pdf: refusing to render as root or an administrator without --unsandboxed: "
                "Chromium cannot build a renderer sandbox there, and ATE never turns it off by itself. "
                "The parent asks the Operator first (packet 30d); pass --unsandboxed only when that "
                "choice was Yes, or run ATE with --allow-unsandboxed-pdf.",
                file=sys.stderr,
            )
            return True
        return False
    if _is_elevated():
        os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
        flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
        if "--no-sandbox" not in flags:
            os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags + ["--no-sandbox"])
        return False
    print(
        "ate _pdf: --unsandboxed ignored: this process is not root or an administrator, so "
        "Chromium's sandbox is kept for this render",
        file=sys.stderr,
    )
    return False


def render_pdf(html_path: str, pdf_path: str, paper: str = "A4", unsandboxed: bool = False) -> int:
    """Load ``html_path`` into QWebEngine and print it to ``pdf_path``. Runs inside the helper process.

    Returns the intended process exit code (0 on success). A missing or unloadable HTML file, a bad
    paper size, or a failed print all yield a non-zero code — never ``setHtml`` is used.

    ``unsandboxed`` is the parent's decision, passed as ``--unsandboxed`` (packet 30d): the helper
    does not guess. Privileged and without it, it refuses with a non-zero exit; privileged and with
    it, it turns the sandbox off for this one render; not privileged, it keeps the sandbox whatever
    the argument says.
    """
    if paper not in ("A4", "Letter"):
        raise ValueError(f"paper must be 'A4' or 'Letter', got {paper!r}")

    # Headless-friendly: prefer the offscreen platform when no display is attached (tests, CI).
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if _apply_sandbox_decision(unsandboxed):
        return 1

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
    if unsandboxed and _is_elevated():
        print(
            "pdf render failed while running elevated with the sandbox off: Chromium cannot build a "
            "renderer sandbox under root or an administrator, so this render used "
            "QTWEBENGINE_DISABLE_SANDBOX=1 and --no-sandbox and still produced nothing",
            file=sys.stderr,
        )
    return 1


def pdf_helper_command(
    html_path: str, pdf_path: str, paper: str = "A4", unsandboxed: bool = False
) -> tuple[str, list[str], dict[str, str]]:
    """Return ``(program, args, env)`` that runs the hidden ``ate _pdf`` helper for one report.

    Frozen by PyInstaller, ``sys.executable`` is ATE itself and takes the subcommand directly;
    otherwise it is Python running ``-m ate``, with this package's own ``src`` directory first on
    ``PYTHONPATH`` so the child imports the same ATE as the parent. ``--unsandboxed`` and the
    sandbox-off environment are added only when the caller's decision (packet 30d) was Yes: the
    helper is told, never left to guess from who started it."""
    tail = ["_pdf", str(html_path), str(pdf_path), "--paper", paper]
    if unsandboxed:
        tail.append(UNSANDBOXED_FLAG)
    env = _child_env(unsandboxed)
    if getattr(sys, "frozen", False):
        return sys.executable, tail, env
    src = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH", "")) if p)
    return sys.executable, ["-m", "ate", *tail], env


def run_pdf_helper(
    html_path: str, pdf_path: str, paper: str = "A4", unsandboxed: bool = False, *, timeout: float = 30.0
) -> None:
    """Run the helper process to completion, blocking; raise on failure or timeout.

    For callers that may block (CLI, History regeneration on a worker). The run session uses
    :func:`pdf_helper_command` with a ``QProcess`` instead so the GUI thread never waits.
    ``unsandboxed`` is the caller's decision (packet 30d): the helper is told, not guessed.
    Raises :class:`TimeoutError` on timeout and :class:`RuntimeError` if the child can't start or
    exits non-zero (including a missing HTML file)."""
    import subprocess

    program, args, env = pdf_helper_command(html_path, pdf_path, paper, unsandboxed)
    try:
        proc = subprocess.run([program, *args], env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:  # subprocess.run kills the child before raising
        raise TimeoutError(f"pdf helper timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise RuntimeError(f"could not launch pdf helper: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"pdf helper exited with code {proc.returncode}: {detail}")
