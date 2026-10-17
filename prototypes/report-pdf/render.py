"""PROTOTYPE, throwaway. Renders the sample report to PDF with one engine, inside a running
Qt app with a 10 ms heartbeat timer, and prints how long it took and the longest stall the
GUI thread saw. That stall is what an Operator would feel on the Run screen.

python render.py --engine qwe|x2p-thread|x2p-process [--steps N] [--logs M] [--paper A4|Letter]
"""
import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from PyQt6.QtCore import QMarginsF, QUrl, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QPageLayout, QPageSize
from PyQt6.QtWidgets import QApplication, QLabel

import sample

OUT = Path(__file__).parent / "out"


def x2p_render(html, pdf_path):
    from xhtml2pdf import pisa
    with open(pdf_path, "wb") as f:
        st = pisa.CreatePDF(html, dest=f, encoding="utf-8")
    return st.err


class X2PThread(QThread):
    done = pyqtSignal(int)

    def __init__(self, html, pdf):
        super().__init__()
        self.html, self.pdf = html, pdf

    def run(self):
        self.done.emit(x2p_render(self.html, self.pdf))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True, choices=["qwe", "x2p-thread", "x2p-process"])
    ap.add_argument("--steps", type=int, default=40)
    ap.add_argument("--logs", type=int, default=200)
    ap.add_argument("--paper", default="A4", choices=["A4", "Letter"])
    ap.add_argument("--tag", default="")
    a = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    ctx = sample.build(a.steps, a.logs)
    ctx["paper"] = a.paper
    furniture = "css" if a.engine == "qwe" else "xhtml2pdf"
    t0 = time.perf_counter()
    html = sample.render(ctx, furniture)
    t_tpl = time.perf_counter() - t0
    stem = f"{a.engine.split('-')[0]}_{a.steps}s_{a.paper}{a.tag}"
    (OUT / f"{stem}.html").write_text(html, encoding="utf-8")
    pdf = str(OUT / f"{stem}.pdf")

    app = QApplication(sys.argv)
    lbl = QLabel("rendering…")  # a widget exists, as it would on the Run screen
    state = dict(last=time.perf_counter(), gap=0.0, beats=0)

    def beat():
        now = time.perf_counter()
        state["gap"] = max(state["gap"], now - state["last"])
        state["last"] = now
        state["beats"] += 1
    hb = QTimer(interval=10, timeout=beat)
    hb.start()

    start = [0.0]

    def finish(ok):
        dt = time.perf_counter() - start[0]
        hb.stop()
        from pypdf import PdfReader
        n = len(PdfReader(pdf).pages) if ok else 0
        print(f"engine={a.engine} steps={a.steps} logs={a.logs} paper={a.paper} ok={ok} "
              f"html={len(html) / 1024:.0f}KiB template={t_tpl:.2f}s render={dt:.2f}s "
              f"pages={n} pdf={os.path.getsize(pdf) / 1024:.0f}KiB max_gui_stall={state['gap'] * 1000:.0f}ms")
        app.quit()

    def go():
        start[0] = time.perf_counter()
        state["last"] = start[0]
        if a.engine == "qwe":
            from PyQt6.QtWebEngineCore import QWebEnginePage
            page = app._page  # made at startup, as the real app would
            size = QPageSize(QPageSize.PageSizeId.A4 if a.paper == "A4" else QPageSize.PageSizeId.Letter)
            layout = QPageLayout(size, QPageLayout.Orientation.Portrait,
                                 # Chromium takes the page margins from Qt, not from CSS @page; they must match
                                 QMarginsF(14, 24, 14, 18), QPageLayout.Unit.Millimeter)
            page.pdfPrintingFinished.connect(lambda path, ok: finish(ok))
            page.loadFinished.connect(lambda ok: page.printToPdf(pdf, layout))
            # load the written HTML file: setHtml silently produces a blank page once the
            # percent-encoded HTML passes 2 MB (about 1.5 MB of report)
            page.load(QUrl.fromLocalFile(str(OUT / f"{stem}.html")))
        elif a.engine == "x2p-thread":
            t = X2PThread(html, pdf)
            app._t = t
            t.done.connect(lambda err: finish(err == 0))
            t.start()
        else:
            ex = ProcessPoolExecutor(1)
            app._ex = ex
            fut = ex.submit(x2p_render, html, pdf)
            poll = QTimer(interval=50)
            poll.timeout.connect(lambda: fut.done() and (poll.stop(), finish(fut.result() == 0)))
            app._poll = poll
            poll.start()

    if a.engine == "qwe":
        from PyQt6.QtWebEngineCore import QWebEnginePage
        app._page = QWebEnginePage()  # first page spins up Chromium; that hitch is not the render's
        app._page.setHtml("<p></p>")
    QTimer.singleShot(1500, go)
    app.exec()


if __name__ == "__main__":
    main()
