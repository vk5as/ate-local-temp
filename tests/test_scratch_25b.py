"""Scratch: who deletes the abort-test session's QProcess, and when?"""

import gc
import threading
import time

from PyQt6.QtCore import QObject, QProcess

from ate.report.pdf_job import _live_pdf_jobs
from ate.run_session.headless import HeadlessRunSession, SessionState

from ._fixtures import write_ate_env


def test_deleter(qapp, tmp_path):
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    answered = threading.Event()

    def _on_ask(seq, attempt, index):
        op.supply_input(index - 1, 5)
        answered.set()

    session.input_asked.connect(_on_ask)
    session.select_schedule("Abortable schedule")
    engine = session.start()
    op = engine.operator_input
    deadline = time.monotonic() + 10
    while not answered.is_set() and time.monotonic() < deadline:
        qapp.processEvents()
    session.request_abort()
    while session.state != SessionState.FINISHED and time.monotonic() < deadline:
        qapp.processEvents()

    proc = next(iter(_live_pdf_jobs))
    proc.destroyed.connect(lambda *_: print("PROC C++ DESTROYED", flush=True)
    )
    print("proc", id(proc), "parent", id(proc.parent()), "state", proc.state(), flush=True)
    print("engine parent", engine.parent(), "store", type(session.store), flush=True)
    print("live jobs", list(_live_pdf_jobs), flush=True)
    print("app children", [type(c).__name__ for c in qapp.children()], flush=True)
    for c in qapp.children():
        for gc_ in c.children():
            print("  child", type(gc_).__name__, flush=True)
    print("gc collect", flush=True)
    gc.collect()
    qapp.processEvents()
    print("after gc, proc state", proc.state(), "alive?", proc.isValid() if hasattr(proc, "isValid") else "n/a", flush=True)
    print("live jobs after gc", list(_live_pdf_jobs), flush=True)
