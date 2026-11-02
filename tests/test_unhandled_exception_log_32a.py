"""Packet 32a: an exception in a slot reaches the application log and the Operator.

PyQt6 aborted the process on one, which was loud. PySide6 hands it to `sys.excepthook` and carries
on, so with nothing listening the screen would quietly stop updating while a frozen ATE's stderr
goes nowhere anyone reads. A raising slot must be logged, must show, must not stop the worker
recording, and must still reach pytest-qt's capture so a test cannot pass over one.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time

import pytest
from PySide6.QtCore import QCoreApplication, QObject, QThread, Signal
from PySide6.QtWidgets import QLabel, QMessageBox
from pytestqt.exceptions import capture_exceptions

import ate.report.pdf_job
from ate.app import launch
from ate.exception_log import UnhandledExceptionLog
from ate.log import AppLog, LogError
from ate.run_session.headless import SessionState
from ate.storage.store import RunStore

from ._fixtures import WRITES_PDF, fake_pdf_helper, write_ate_env


class _Sender(QObject):
    fired = Signal()


def _raise_in_slot() -> None:
    raise ValueError("the slot went wrong")


def test_a_slot_exception_is_logged_and_still_reaches_the_previous_hook(qapp, tmp_path) -> None:
    """The log gets the traceback; pytest-qt's capture, which is the previous hook here, still gets it."""
    log = AppLog(tmp_path, name="hook")
    sender = _Sender()
    sender.fired.connect(_raise_in_slot)
    with capture_exceptions() as caught:
        reporter = UnhandledExceptionLog(log)
        reporter.install()
        sender.fired.emit()
        reporter.uninstall()
    log.close()

    assert [exc_type for exc_type, _value, _tb in caught] == [ValueError]
    text = log.path.read_text(encoding="utf-8")
    assert "ERROR" in text and "Traceback" in text and "the slot went wrong" in text and "_raise_in_slot" in text


def test_uninstall_puts_the_previous_hook_back_and_leaves_a_later_hook_alone(qapp, tmp_path) -> None:
    with capture_exceptions():
        before = sys.excepthook
        with AppLog(tmp_path, name="restore") as log:
            reporter = UnhandledExceptionLog(log)
            reporter.install()
            assert sys.excepthook != before
            reporter.uninstall()
            assert sys.excepthook == before

            reporter.install()
            later = lambda *_args: None  # noqa: E731 - something else took the hook after us
            sys.excepthook = later
            reporter.uninstall()
            assert sys.excepthook is later
            sys.excepthook = before


def _fail_with_log_error(*_args) -> None:
    raise LogError("cannot write application log: disk full")


@pytest.mark.parametrize("break_log", [AppLog.close, lambda log: setattr(log, "log", _fail_with_log_error)], ids=["closed", "log-error"])
def test_a_failed_log_write_still_tells_the_operator_and_reaches_the_previous_hook(qapp, tmp_path, break_log) -> None:
    """The hook runs while Qt is unwinding, so it must not raise, and a log it cannot write is no
    reason to say nothing: the notice still goes out, naming no path, because none holds the details."""
    log = AppLog(tmp_path, name="broken")
    break_log(log)
    shown: list[str] = []
    sender = _Sender()
    sender.fired.connect(_raise_in_slot)
    with capture_exceptions() as caught:
        reporter = UnhandledExceptionLog(log)
        reporter.reported.connect(shown.append)
        reporter.install()
        sender.fired.emit()
        reporter.uninstall()
    log.close()
    assert [exc_type for exc_type, _value, _tb in caught] == [ValueError]
    assert shown == [""], "the Operator must hear of it, and not be sent to a log that lacks it"


def test_the_window_shows_a_failed_log_write_without_claiming_a_path(qapp, tmp_path, monkeypatch) -> None:
    """Through `launch` and the queued connection: the line shows, and names no log file."""
    monkeypatch.setattr(ate.report.pdf_job, "pdf_helper_command", fake_pdf_helper(WRITES_PDF))
    env = write_ate_env(tmp_path)
    RunStore(env["db"]).close()
    sender = _Sender()
    sender.fired.connect(_raise_in_slot)
    with capture_exceptions():
        window = launch(["--config", str(env["config"])], exec_=False, prompt=lambda _path: True, alert=lambda _text: None).window
        log_path = window.app_log.path
        window.app_log.close()
        sender.fired.emit()
        qapp.processEvents()
        label = window.findChild(QLabel, "internal_error_label")
        text = label.text() if label is not None else ""
        window.close()
    assert "Internal error" in text and "could not be written" in text, text
    assert str(log_path) not in text and log_path.name not in text and "Details:" not in text, text


def test_an_exception_on_a_worker_is_logged_there_and_shown_on_the_main_thread(qapp, tmp_path) -> None:
    """`QThread.run` raising calls the hook on the worker, which must not touch a widget from there."""

    class Raises(QThread):
        def run(self) -> None:
            noted.append(threading.get_ident())
            raise KeyError("worker went wrong")

    noted: list[int] = []
    shown: list[tuple[str, int]] = []
    log = AppLog(tmp_path, name="worker")
    with capture_exceptions() as caught:
        reporter = UnhandledExceptionLog(log)
        reporter.reported.connect(lambda path: shown.append((path, threading.get_ident())))
        reporter.install()
        worker = Raises()
        worker.start()
        assert worker.wait(5000)
        QCoreApplication.processEvents()
        reporter.uninstall()
    log.close()

    assert noted and noted[0] != threading.get_ident()
    assert [exc_type for exc_type, _value, _tb in caught] == [KeyError]
    assert "worker went wrong" in log.path.read_text(encoding="utf-8")
    assert shown == [(str(log.path), threading.get_ident())], "the notice did not arrive on the main thread"


def test_a_raising_slot_during_a_real_run_is_logged_shown_and_the_run_still_records(qapp, tmp_path, monkeypatch) -> None:
    """The product behaviour end to end, through `launch`: nothing modal, and the run row is right."""
    monkeypatch.setattr(ate.report.pdf_job, "pdf_helper_command", fake_pdf_helper(WRITES_PDF))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: (_ for _ in ()).throw(AssertionError("a modal box opened")))
    env = write_ate_env(tmp_path)
    RunStore(env["db"]).close()

    def raises_on_every_signal(*_args) -> None:
        raise ValueError("a screen slot went wrong")

    with capture_exceptions() as caught:
        hook_before = sys.excepthook
        outcome = launch(["--config", str(env["config"])], exec_=False, prompt=lambda _path: True, alert=lambda _text: None)
        window = outcome.window
        session = window.session
        assert window.findChild(QLabel, "internal_error_label") is None, "a clean launch shows an error"
        session.step_started.connect(raises_on_every_signal)
        session.step_finished.connect(raises_on_every_signal)
        session.state_changed.connect(raises_on_every_signal)

        session.select_schedule("Pre post pass schedule")
        session.start()
        run_uuid = None
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and session.state != SessionState.FINISHED:
            qapp.processEvents()
            run_uuid = session.run_uuid or run_uuid
            time.sleep(0.005)
        for _ in range(5):
            qapp.processEvents()
        finished = session.state
        label = window.findChild(QLabel, "internal_error_label")
        log_path = window.app_log.path
        label_text = label.text() if label is not None else ""
        window.close()
        restored = sys.excepthook == hook_before

    assert finished == SessionState.FINISHED, "the run did not reach FINISHED past the raising slots"
    assert caught and {exc_type for exc_type, _value, _tb in caught} == {ValueError}, "pytest-qt's capture saw nothing"
    assert restored, "closing the window left the hook installed"

    text = log_path.read_text(encoding="utf-8")
    assert "a screen slot went wrong" in text and "Traceback" in text and "raises_on_every_signal" in text
    assert str(log_path) in label_text and "keeps recording" in label_text, label_text

    conn = sqlite3.connect(env["db"])
    try:
        rows = conn.execute("SELECT result, end_reason FROM test_run WHERE run_uuid = ?", (run_uuid,)).fetchall()
    finally:
        conn.close()
    assert rows and rows[0][0] == "PASS", f"the worker stopped recording: {rows}"
