"""Packet 32a: which thread a worker's signal slot runs on, and what the signals really carry.

The engine does its work on a `QThread` and talks to the session and the screens through signals.
A binding decides how those are delivered: a slot could run in the emitting worker's thread, which
would mean a widget or the store being touched from outside the main thread. This records both ends
of every engine signal during one real run: the thread that emitted it, and the thread each shape of
slot ran on. Without a recorded emission on the worker the delivery check would prove nothing, and
`test_the_check_catches_a_slot_that_really_runs_on_the_worker` proves that the check can fail.

The values are read on the sending side. A slot cannot see a bad one: `Signal(int)` turns 2**31 into
-2147483648 and `Signal(str)` turns 5 into `''` before any slot runs (pinned below).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from pytestqt.exceptions import capture_exceptions

import ate.report.pdf_job
from ate.engine.run_engine import RunEngine, RunStatus
from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.ui.staged_run_screen import StagedRunScreen

from ._fixtures import WRITES_PDF, fake_pdf_helper, write_ate_env

ENGINE_SIGNALS = sorted(name for name, value in vars(RunEngine).items() if isinstance(value, Signal))
SESSION_SIGNALS = sorted(name for name, value in vars(HeadlessRunSession).items() if isinstance(value, Signal))

#: Pre- and Post-test Actions, a POLL (announced by the worker itself), a blocking input (announced
#: through `OperatorInput.asked`) and a Stop-on-Fail failure: every signal the engine has.
EVERY_SIGNAL = """\
return ScheduleParams("Every signal schedule", "emits each of the engine's signals")
  :setSchedule({
    StepParams("asks"):setStepNo("1.")
      :setInputs({ [1] = { desc = "poll", type = "POLL", prompt = { "Go" } },
                   [2] = { desc = "voltage", type = "INTEGER" } })
      :setAction(function() local v=ATE.input(2); ATE.report(Result.PASS, "ok "..v) end),
    StepParams("fails"):setStepNo("2.")
      :setAction(function() ATE.report(Result.FAIL, "nope") end),
  })
  :setStopOnFail(true)
  :setPreTestAction(function(passFail) ATE.log(Log.INFO, "pretest "..passFail) end)
  :setPostTestAction(function(passFail) ATE.log(Log.INFO, "posttest "..passFail) end)
"""


class _Recording:
    """Stands in for one signal on one object: notes what is emitted and where, then really emits it."""

    def __init__(self, name: str, signal, sent: list) -> None:
        self._name, self._signal, self._sent = name, signal, sent

    def emit(self, *args):
        self._sent.append((self._name, threading.get_ident(), args))
        return self._signal.emit(*args)

    def connect(self, *args, **kwargs):
        return self._signal.connect(*args, **kwargs)


def _record_sends(owner, names, sent) -> None:
    for name in names:
        setattr(owner, name, _Recording(name, getattr(owner, name), sent))


class _QObjectSlots(QObject):
    """A QObject whose method is connected straight to a worker signal."""

    def __init__(self, landed: list) -> None:
        super().__init__()
        self.landed = landed

    def on_signal(self, *_args) -> None:
        self.landed.append(("QObject method", threading.get_ident()))


class _PlainSlots:
    """A method of something that is not a QObject, so there is no receiver for Qt to look at."""

    def __init__(self, landed: list) -> None:
        self.landed = landed

    def on_signal(self, *_args) -> None:
        self.landed.append(("plain method", threading.get_ident()))


def _connect_every_shape(engine, landed: list, keep: list) -> None:
    """Connect each signal to a lambda, a closure, a QObject method and a plain method."""
    for name in ENGINE_SIGNALS:
        signal = getattr(engine, name)
        shapes: list = []

        def closure(*_args, name=name):
            landed.append((f"{name}: closure", threading.get_ident()))

        qobject = _QObjectSlots(shapes)
        plain = _PlainSlots(shapes)
        signal.connect(lambda *_args, name=name: landed.append((f"{name}: lambda", threading.get_ident())))
        signal.connect(closure)
        signal.connect(qobject.on_signal)
        signal.connect(plain.on_signal)
        keep.append((qobject, plain, shapes, name))


def _run_every_signal(qapp, tmp_path, monkeypatch, before_start=lambda engine: None) -> dict:
    """Run EVERY_SIGNAL through a real session and screen: answer the input, then the pause."""
    env = write_ate_env(tmp_path)
    schedule = env["defs_dir"] / "every_signal.tslua"
    schedule.write_text(EVERY_SIGNAL, encoding="utf-8", newline="\n")
    manifest_path = env["defs_dir"] / "demo.atedef"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append({"path": schedule.name, "sha256": hashlib.sha256(EVERY_SIGNAL.encode()).hexdigest()})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    monkeypatch.setattr(ate.report.pdf_job, "pdf_helper_command", fake_pdf_helper(WRITES_PDF))

    result: dict = {
        "sent": [],
        "session_sent": [],
        "landed": [],
        "internal": [],
        "keep": [],
        "worker": [],
        "session_landed": [],
    }

    # The session's and the engine's own slots: the real receivers of the worker's signals.
    for owner, names in (
        (
            HeadlessRunSession,
            (
                "_on_run_uuid",
                "_on_input_asked",
                "_on_step_finished",
                "_on_failure_prompt",
                "_on_prompt_answered",
                "_on_action_started",
                "_on_step_started",
            ),
        ),
        (RunEngine, ("_on_asked", "_forget_ended_worker")),
    ):
        for name in names:
            original = getattr(owner, name)

            def wrapped(self, *args, _original=original, _name=name):
                result["internal"].append((_name, threading.get_ident()))
                return _original(self, *args)

            monkeypatch.setattr(owner, name, wrapped)

    real_run = RunEngine.run

    def run_noting_thread(self):
        result["worker"].append(threading.get_ident())
        real_run(self)

    monkeypatch.setattr(RunEngine, "run", run_noting_thread)

    # Everything is connected in the gap between the engine being built and its worker starting,
    # so nothing the worker emits can be missed.
    real_start = RunEngine.start

    def start_connected(self, *args):
        _connect_every_shape(self, result["landed"], result["keep"])
        _record_sends(self, ENGINE_SIGNALS, result["sent"])
        before_start(self)
        real_start(self, *args)

    monkeypatch.setattr(RunEngine, "start", start_connected)

    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)  # connects its slots to the session's real signals
    screen.resize(1280, 800)
    screen.show()
    qapp.processEvents()
    _record_sends(session, SESSION_SIGNALS, result["session_sent"])
    # Connected to the real signal under the recording: the session's forward, on the session's side.
    for name in SESSION_SIGNALS:
        getattr(session, name).connect(
            lambda *_args, name=name: result["session_landed"].append((name, threading.get_ident()))
        )

    asked: list[tuple[int, int, int]] = []
    prompted: list[tuple[int, int]] = []
    session.input_asked.connect(lambda *args: asked.append(args))
    session.failure_prompt.connect(lambda *args: prompted.append(args))

    session.select_schedule("Every signal schedule")
    session.set_stop_on_fail(True)
    session.start()

    answered_input = answered_prompt = False
    written: list[tuple[str, str]] = []
    session.report_written.connect(lambda *args: written.append(args))
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline and not (session.state == SessionState.FINISHED and written):
        qapp.processEvents()
        if not answered_input:
            for node_seq, attempt_no, index in asked:
                if index == 2:  # the POLL (index 1) is announced too, and never blocks
                    session.supply_input(node_seq, attempt_no, index, 5)
                    answered_input = True
                    break
        if prompted and not answered_prompt:
            session.choose("CONTINUE")
            answered_prompt = True
        time.sleep(0.005)
    qapp.processEvents()
    assert session.state == SessionState.FINISHED, f"the run did not finish: {session.state}"
    assert written, "the run-end PDF job never reported"
    assert answered_input and answered_prompt
    # The worker ends after FINISHED, and its `finished` signal is queued to this thread.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not any(name == "_forget_ended_worker" for name, _ in result["internal"]):
        qapp.processEvents()
        time.sleep(0.005)
    session.close()
    screen.close()
    return result


def test_every_worker_signal_is_emitted_on_the_worker_and_lands_on_the_main_thread(qapp, tmp_path, monkeypatch) -> None:
    """All eight signals, every slot shape, and the session's and the engine's own slots."""
    main = threading.get_ident()
    result = _run_every_signal(qapp, tmp_path, monkeypatch)

    assert result["worker"] and main not in result["worker"], "the schedule did not run on a worker thread"
    worker = result["worker"][0]

    assert len(ENGINE_SIGNALS) == 8, ENGINE_SIGNALS
    emitted_on_worker = {name for name, thread, _args in result["sent"] if thread == worker}
    assert emitted_on_worker == set(ENGINE_SIGNALS), (
        f"not emitted on the worker: {sorted(set(ENGINE_SIGNALS) - emitted_on_worker)}; the delivery check proves nothing for those"
    )

    expected = {f"{name}: {shape}" for name in ENGINE_SIGNALS for shape in ("lambda", "closure")}
    assert expected <= {shape for shape, _ in result["landed"]}
    for qobject, plain, shapes, name in result["keep"]:
        assert {"QObject method", "plain method"} == {shape for shape, _ in shapes}, f"{name}: {shapes}"
        assert all(thread == main for _, thread in shapes), f"{name} ran a slot off the main thread"
    off_main = [(shape, thread) for shape, thread in result["landed"] if thread != main]
    assert not off_main, f"a worker signal ran its slot outside the main thread: {off_main}"

    # `run_engine.py`'s own two connects are here: `_op.asked` and `finished`, both from the worker.
    internal = {name for name, _ in result["internal"]}
    assert internal == {
        "_on_run_uuid",
        "_on_input_asked",
        "_on_step_finished",
        "_on_failure_prompt",
        "_on_prompt_answered",
        "_on_action_started",
        "_on_step_started",
        "_on_asked",
        "_forget_ended_worker",
    }, internal
    assert all(thread == main for _, thread in result["internal"]), result["internal"]

    # What the screens are connected to: the session's forwards, and `report_written` from the PDF
    # job, whose `QProcess` signals (pdf_job.py) are owned by and delivered on this thread.
    assert all(thread == main for _, thread in result["session_landed"]), result["session_landed"]
    assert all(thread == main for _name, thread, _args in result["session_sent"]), result["session_sent"]
    assert "report_written" in {name for name, _ in result["session_landed"]}


def test_the_check_catches_a_slot_that_really_runs_on_the_worker(qapp, tmp_path, monkeypatch) -> None:
    """A DirectConnection really does run on the worker: this is what the test above would see."""
    main = threading.get_ident()
    direct: list[int] = []

    def connect_direct(engine) -> None:
        engine.step_started.connect(lambda *_args: direct.append(threading.get_ident()), Qt.ConnectionType.DirectConnection)

    result = _run_every_signal(qapp, tmp_path, monkeypatch, connect_direct)
    assert direct and main not in direct, "a direct connection did not run on the worker: the recording cannot tell"
    assert set(direct) == {result["worker"][0]}


def test_the_values_every_signal_really_sends(qapp, tmp_path, monkeypatch) -> None:
    """On the sending side, every declared `int` is a 32-bit `int` and every `str` is a real `str`."""
    result = _run_every_signal(qapp, tmp_path, monkeypatch)
    assert {name for name, _thread, _args in result["sent"]} == set(ENGINE_SIGNALS)

    declared = {
        "run_uuid_reported": (str,),
        "state_changed": (RunStatus,),
        "input_asked": (int, int, int),
        "step_finished": (int,),
        "failure_prompt": (int, int),
        "prompt_answered": (int, int, str),
        "action_started": (str, int, int),
        "step_started": (int,),
    }
    session_declared = {
        "state_changed": (SessionState,),
        "input_asked": (int, int, int),
        "input_answered": (int, int, int, object),
        "step_finished": (int,),
        "failure_prompt": (int, int),
        "prompt_answered": (int, int, str),
        "action_started": (str, int, int),
        "step_started": (int,),
        "report_written": (str, str),
    }
    for sent, table in ((result["sent"], declared), (result["session_sent"], session_declared)):
        for name, _thread, args in sent:
            types = table[name]
            assert len(args) == len(types), f"{name}: {args}"
            for arg, wanted in zip(args, types):
                if wanted is object:
                    continue
                assert type(arg) is wanted, f"{name} sent {arg!r}, declared {wanted.__name__}: a slot would not see it"
                if wanted is int:
                    assert -(2**31) <= arg <= 2**31 - 1, f"{name} sent {arg}, which Signal(int) wraps: declare object"

    by_name: dict[str, list[tuple]] = {}
    for name, _thread, args in result["sent"]:
        by_name.setdefault(name, []).append(args)
    assert {args[0] for args in by_name["action_started"]} == {"pre", "post"}
    assert {args[2] for args in by_name["prompt_answered"]} == {"CONTINUE"}
    assert all(args[0] for args in by_name["run_uuid_reported"])
    written = [args for name, _thread, args in result["session_sent"] if name == "report_written"]
    assert written and written[0][0].endswith(".html") and written[0][1].endswith(".pdf"), written


class _Probe(QObject):
    number = Signal(int)
    text = Signal(str)


def test_why_the_values_are_read_on_the_sending_side(qapp) -> None:
    """Pins what PySide6 does to a bad value, which is why a slot cannot be the one to check it."""
    probe = _Probe()
    numbers: list = []
    texts: list = []
    probe.number.connect(lambda value: numbers.append(value))
    probe.text.connect(lambda value: texts.append(value))
    # The OverflowError stays pending behind the slot, where its next C call can raise SystemError.
    with capture_exceptions(), pytest.warns(RuntimeWarning, match="Overflow"):
        probe.number.emit(2**31)
    assert numbers == [-(2**31)]
    probe.text.emit(5)
    assert texts == [""]

    unconnected = _Probe()
    with pytest.raises(OverflowError), pytest.warns(RuntimeWarning, match="Overflow"):
        unconnected.number.emit(2**31)
