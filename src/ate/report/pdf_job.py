"""Run the HTML→PDF helper in a session-owned ``QProcess``.

The GUI (ticket 22) never renders to PDF itself: it hands one report to :func:`start_pdf_job`,
which runs the hidden ``ate _pdf`` helper in a child process and reports back when it ends, so no
worker thread ever waits on it. The QProcess is owned by the caller's QObject (its parent), so its
signals arrive on that thread and the process dies with it; holding each live job here keeps it
reachable until :meth:`QProcess.deleteLater` runs.

Public API:
    start_pdf_job(parent, html_path, pdf_path, paper="A4", *, on_done=None) -> None  # fires ``on_done`` when done
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QProcess, QProcessEnvironment, QTimer

from .pdf import pdf_helper_command


# A PDF still running after this is killed and reported as failed.
PDF_TIMEOUT_MS = 60_000

# Helper processes still running. Holding them here keeps each job (the process, its callbacks and
# the parent they report to) reachable until it ends; otherwise a parent dropped mid-render forms an
# unreachable cycle and the garbage collector clears it under the still-running process.
_live_pdf_jobs: set[QProcess] = set()


def start_pdf_job(parent, html_path, pdf_path, paper="A4", *, on_done=None) -> None:
    """Render ``html_path`` to ``pdf_path`` in a QProcess owned by ``parent``, without waiting.

    The helper runs as a child of ``parent`` (its parent), so its signals land on that thread and no
    worker thread ever touches the session; when it ends, ``on_done(error_or_None)`` fires — ``None``
    means success, otherwise an error string names the failure (spec 22b). Nothing here raises into
    the caller. The PDF is confirmed to exist and be non-empty before a success is reported.
    """
    program, args, env = pdf_helper_command(str(html_path), str(pdf_path), paper)
    proc = QProcess(parent)
    penv = QProcessEnvironment()
    for key, value in env.items():
        penv.insert(key, value)
    proc.setProcessEnvironment(penv)
    timer = QTimer(proc)
    timer.setSingleShot(True)
    timer.timeout.connect(proc.kill)
    done = False

    def finish(error: Optional[str]) -> None:
        nonlocal done
        if done:
            return
        done = True
        timer.stop()
        # A success with no bytes on disk is a failed render, not a clean exit.
        if error is None and not (Path(pdf_path).exists() and Path(pdf_path).stat().st_size > 0):
            error = "PDF helper wrote no PDF"
        if on_done is not None:
            on_done(error)
        _live_pdf_jobs.discard(proc)
        proc.deleteLater()

    def on_finished(code: int, status: QProcess.ExitStatus) -> None:
        if status != QProcess.ExitStatus.NormalExit:
            finish(f"PDF helper crashed or timed out after {PDF_TIMEOUT_MS // 1000} s")
        elif code != 0:
            detail = bytes(proc.readAllStandardError()).decode(errors="replace").strip()
            finish(f"PDF helper exited with code {code}: {detail}")
        else:
            finish(None)

    def on_error(err: QProcess.ProcessError) -> None:
        if err == QProcess.ProcessError.FailedToStart:
            finish(f"could not start PDF helper: {proc.errorString()}")

    proc.finished.connect(on_finished)
    proc.errorOccurred.connect(on_error)
    _live_pdf_jobs.add(proc)
    timer.start(PDF_TIMEOUT_MS)
    proc.start(program, args)
