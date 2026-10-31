"""Packet 32a: which thread a worker's signal slot runs on.

The engine does its work on a `QThread` and talks to the session and the screens only through
signals. A binding decides how those signals are delivered: a slot could run in the emitting
worker's thread, which would mean a widget or the store being touched from outside the main
thread. Both PyQt6 and PySide6 queue a cross-thread signal onto the receiver's thread, but this
proves it rather than assumes it: every slot below records the thread it ran on, and the run is
checked to have actually happened on a worker.

Lambdas, closures, plain functions and methods of a `QObject` are each exercised, because those
are the shapes the screens and the headless session actually connect.
"""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject

import ate.engine.run_engine as run_engine_module
import ate.report.pdf_job
from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.ui.staged_run_screen import StagedRunScreen

from ._fixtures import WRITES_PDF, fake_pdf_helper, write_ate_env


class Watcher(QObject):
    """A QObject whose method is connected straight to a worker signal."""

    def __init__(self) -> None:
        self.threads: list[str] = []

    def on_step(self, node_seq: int) -> None:
        self.threads.append(threading.current_thread().name)


def _wrap_slot(screen_class, name: str, record):
    """Wrap one of the screen's slots so every delivery records its thread."""
    original = getattr(screen_class, name)

    def wrapper(self, *args, **kwargs):
        record(threading.current_thread().name)
        return original(self, *args, **kwargs)

    return wrapper


def test_worker_signals_reach_every_slot_on_the_main_thread(qapp, tmp_path, monkeypatch) -> None:
    """A run that emits every signal the engine has, with each slot recording its thread."""
    env = write_ate_env(tmp_path)
    monkeypatch.setattr(ate.report.pdf_job, "pdf_helper_command", fake_pdf_helper(WRITES_PDF))

    main = threading.current_thread().name
    worker_threads: list[str] = []
    real_run = run_engine_module.RunEngine.run

    def run_logging_thread(self):
        worker_threads.append(threading.current_thread().name)
        real_run(self)

    monkeypatch.setattr(run_engine_module.RunEngine, "run", run_logging_thread)

    screen_threads: list[str] = []
    for name in ("_on_failure_prompt", "_on_prompt_answered", "_on_state_changed"):
        monkeypatch.setattr(StagedRunScreen, name, _wrap_slot(StagedRunScreen, name, screen_threads.append))

    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    screen.resize(1280, 800)
    screen.show()
    qapp.processEvents()

    landed: list[tuple[str, str]] = []

    def note(name):
        def slot(*_args):
            landed.append((name, threading.current_thread().name))

        return slot

    watcher = Watcher()
    session.select_schedule("Pre post pass schedule")
    engine = session.start()
    # Connected before the event loop is pumped, so nothing emitted is missed.
    for signal, name in (
        (engine.run_uuid_reported, "run_uuid_reported"),
        (engine.state_changed, "state_changed"),
        (engine.step_started, "step_started"),
        (engine.step_finished, "step_finished"),
        (engine.action_started, "action_started"),
        (engine.input_asked, "input_asked"),
        (engine.failure_prompt, "failure_prompt"),
    ):
        signal.connect(note(name))
    engine.step_finished.connect(watcher.on_step)

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if session.state == SessionState.FINISHED:
            break
        time.sleep(0.01)
    qapp.processEvents()

    assert worker_threads and all(thread != main for thread in worker_threads), (
        f"the schedule did not run on a worker thread: {worker_threads}"
    )
    names = {name for name, _ in landed}
    assert {"run_uuid_reported", "state_changed", "step_started", "step_finished", "action_started"} <= names, (
        f"the run emitted only {sorted(names)}: the test proved nothing"
    )
    off_thread = [(name, thread) for name, thread in landed if thread != main]
    assert not off_thread, f"a worker signal ran its slot outside the main thread: {off_thread}"
    assert watcher.threads and all(thread == main for thread in watcher.threads), watcher.threads
    assert screen_threads and all(thread == main for thread in screen_threads), screen_threads


def test_a_blocking_input_is_answered_from_the_main_thread(qapp, tmp_path, monkeypatch) -> None:
    """The one signal the Operator answers by blocking: its slot still runs on the main thread."""
    env = write_ate_env(tmp_path)
    monkeypatch.setattr(ate.report.pdf_job, "pdf_helper_command", fake_pdf_helper(WRITES_PDF))
    main = threading.current_thread().name

    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    screen.show()
    qapp.processEvents()

    asked: list[tuple[str, tuple[int, int, int]]] = []

    def record(node_seq: int, attempt_no: int, index: int) -> None:
        asked.append((threading.current_thread().name, (node_seq, attempt_no, index)))

    session.select_schedule("Blocking input schedule")
    engine = session.start()
    engine.input_asked.connect(record)

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not asked:
        qapp.processEvents()
        time.sleep(0.01)
    assert asked, "the worker never announced a blocking input"
    assert all(thread == main for thread, _ in asked), asked

    # Answer it so the worker does not stay blocked.
    _thread, (node_seq, attempt_no, index) = asked[0]
    session.supply_input(node_seq, attempt_no, index, 5)
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and session.state != SessionState.FINISHED:
        qapp.processEvents()
        time.sleep(0.01)
    session.close()
