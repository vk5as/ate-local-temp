"""Packet 30d: an unsandboxed PDF is a decision, never a default.

Chromium's renderer sandbox does not start under root or an administrator, so a Test Report PDF
rendered there needs `--no-sandbox`. ATE used to do that silently (27c). Now every PDF goes through
one decision built at launch (:func:`ate.report.pdf.pdf_sandbox_choice`): an ordinary user is never
asked and never loses the sandbox; a privileged user is asked by a dialog whose default is "Skip PDF",
or presets the answer with `--allow-unsandboxed-pdf`; and with no way to ask (headless,
`_selftest --run`, a dismissed dialog) the answer is No and no PDF is written.

Elevation is simulated by monkeypatching `os.geteuid` — nothing here ever runs as root on the host.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import time
from pathlib import Path
from typing import Callable

import pytest
from PyQt6.QtCore import QObject, QCoreApplication, Qt
from PyQt6.QtWidgets import QMessageBox

from ate.app import _pdf_sandbox_box, _pdf_sandbox_dialog, launch
from ate.cli import main
from ate.config import read_config
from ate.history.session import HistorySession
from ate.report import pdf
from ate.report.pdf import PDF_SANDBOXED, PDF_SKIPPED, PDF_UNSANDBOXED, pdf_sandbox_choice
from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.storage.store import RunStore

from ._fixtures import PASSING, recording_pdf_helper, write_ate_env

ATE_ENTRY = Path(__file__).resolve().parents[1] / "packaging" / "pyinstaller" / "ate_entry.py"


def _as_root(monkeypatch) -> None:
    """Pretend this process is root, without ever being root."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)


def _as_ordinary_user(monkeypatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)


@pytest.fixture(autouse=True)
def _restore_sandbox_environment():
    """Never let a test that lets the helper set the knobs leave them set for the next one.

    `monkeypatch` only restores what it changed itself: a `delenv` recorded *after* the code under
    test wrote `QTWEBENGINE_DISABLE_SANDBOX=1` restores that value, which then leaks into every
    later test in the session (packet 30d). These two variables are scrubbed before each test and
    put back afterwards."""
    keys = ("QTWEBENGINE_DISABLE_SANDBOX", "QTWEBENGINE_CHROMIUM_FLAGS")
    saved = {key: os.environ.get(key) for key in keys}
    for key in keys:
        os.environ.pop(key, None)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _run_to_finish(session: HeadlessRunSession, qapp) -> None:
    """Select the demo Passing schedule, start it, and pump until the session is FINISHED."""
    session.set_operator("Ada")
    session.set_project("Power board")
    session.set_uut_type("PSU")
    session.set_uut_serial("PS-1")
    session.select_schedule("Passing schedule")
    session.start()
    deadline = time.monotonic() + 10.0
    while session.state is not SessionState.FINISHED:
        qapp.processEvents()
        if time.monotonic() > deadline:
            raise TimeoutError("the run never reached FINISHED")
        time.sleep(0.005)


def _pump_events(qapp, predicate: Callable[[], bool], seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while not predicate():
        qapp.processEvents()
        if time.monotonic() > deadline:
            return False
        time.sleep(0.002)
    return True


def _run_session(tmp_path, monkeypatch, qapp, choice, record: list, logs: list) -> tuple[HeadlessRunSession, list[tuple[str, str]]]:
    """One finished run whose PDF helper is recorded, and whose decisions are collected in `logs`."""
    env = write_ate_env(tmp_path)
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", recording_pdf_helper(record))
    session = HeadlessRunSession(env["config"], pdf_choice=choice)
    events: list[tuple[str, str]] = []
    session.report_written.connect(lambda html, pdf_path: events.append((html, pdf_path)), Qt.ConnectionType.QueuedConnection)
    _run_to_finish(session, qapp)
    assert _pump_events(qapp, lambda: events), "report_written never fired: the run-end flow did not complete"
    return session, events


# --- the decision itself -----------------------------------------------------------------------


def test_an_ordinary_user_is_never_asked_and_never_sees_the_sandbox_off(monkeypatch, capsys):
    """With and without the flag, a normal user keeps the sandbox and no dialog is opened."""
    _as_ordinary_user(monkeypatch)
    for flag in (False, True):
        asked: list[bool] = []
        choice = pdf_sandbox_choice(flag, ask=lambda: asked.append(True) or True)
        plan = choice()
        assert plan == PDF_SANDBOXED, f"a privileged question was asked for an ordinary user (flag={flag})"
        assert asked == [], "an ordinary user was asked about the sandbox"
        _program, args, env = pdf.pdf_helper_command("a.html", "a.pdf", "A4", plan == PDF_UNSANDBOXED)
        assert pdf.UNSANDBOXED_FLAG not in args
        assert "QTWEBENGINE_DISABLE_SANDBOX" not in env
        assert "--no-sandbox" not in env.get("QTWEBENGINE_CHROMIUM_FLAGS", "")


def test_privileged_with_the_flag_presets_yes_without_a_dialog(monkeypatch):
    """`--allow-unsandboxed-pdf` is the headless yes: no dialog, and it is logged."""
    _as_root(monkeypatch)
    asked: list[bool] = []
    logs: list[tuple[str, str]] = []
    choice = pdf_sandbox_choice(True, ask=lambda: asked.append(True) or True, log=lambda level, source, text: logs.append((level, text)))
    assert choice() == PDF_UNSANDBOXED
    assert asked == [], "the flag should not open a dialog"
    assert any("--allow-unsandboxed-pdf" in text for _level, text in logs), logs


def test_privileged_dialog_yes_and_no_are_both_honoured(monkeypatch):
    """The dialog is asked for every PDF, and its answer decides."""
    _as_root(monkeypatch)
    for answer, expected in ((True, PDF_UNSANDBOXED), (False, PDF_SKIPPED)):
        calls = 0

        def ask() -> bool:
            nonlocal calls
            calls += 1
            return answer

        logs: list[str] = []
        choice = pdf_sandbox_choice(False, ask=ask, log=lambda _level, _source, text: logs.append(text))
        assert choice() == expected
        assert choice() == expected, "the question is asked before each PDF, not once per launch"
        assert calls == 2
        assert logs, "the decision was not logged"


def test_privileged_with_no_way_to_ask_skips_the_pdf(monkeypatch):
    """Headless and elevated: nobody can be asked, so the answer is No."""
    _as_root(monkeypatch)
    logs: list[str] = []
    choice = pdf_sandbox_choice(log=lambda _level, _source, text: logs.append(text))
    assert choice() == PDF_SKIPPED
    assert any("no dialog" in text for text in logs), logs


def test_the_dialog_text_says_what_the_sandbox_is_and_defaults_to_skip():
    """Two plain lines, two buttons, and the default is the safe one."""
    box = _pdf_sandbox_box()
    text = (box.text() + " " + box.informativeText()).lower()
    assert "sandbox" in text and ("root" in text or "administrator" in text)
    assert "html" in text, "the dialog must say the HTML report is already saved"
    labels = [button.text() for button in box.buttons()]
    assert "Generate PDF without sandbox" in labels and "Skip PDF" in labels
    assert box.defaultButton().text() == "Skip PDF", labels


def test_a_dismissed_dialog_means_no(qapp, monkeypatch):
    """`ATE_AUTO_DISMISS_DIALOG` closes the box without clicking a button: that is No (30c seam)."""
    monkeypatch.setenv("ATE_AUTO_DISMISS_DIALOG", "1")
    assert _pdf_sandbox_dialog() is False
    monkeypatch.setenv("ATE_AUTO_DISMISS_DIALOG", "0")
    assert _pdf_sandbox_box().defaultButton().text() == "Skip PDF"


def test_launch_wires_the_flag_and_the_dialog_to_the_session_and_history(tmp_path, monkeypatch, qapp):
    """The decision built at launch is the one the run session and History ask (packet 30d)."""
    _as_root(monkeypatch)
    env = write_ate_env(tmp_path)
    main(["--config", str(env["config"]), "db", "migrate", "--yes"])
    asked: list[bool] = []

    outcome = launch(
        ["--config", str(env["config"])],
        exec_=False,
        prompt=lambda _path: True,
        alert=lambda _msg: None,
        pdf_dialog=lambda: asked.append(True) or False,
    )
    assert outcome.code == 0, outcome
    window = outcome.window
    assert window.pdf_choice() == PDF_SKIPPED, "the window does not carry the launch decision"
    assert window.session._pdf_choice() == PDF_SKIPPED, "the run session was not given the decision"
    assert window.history_session.pdf_choice() == PDF_SKIPPED, "History was not given the decision"
    assert len(asked) == 3, "each PDF asks its own question, and the flag was not enough to preset it"
    window.session.close()

    asked.clear()
    outcome = launch(
        ["--config", str(env["config"]), "--allow-unsandboxed-pdf"],
        exec_=False,
        prompt=lambda _path: True,
        pdf_dialog=lambda: asked.append(True) or True,
    )
    assert outcome.window.pdf_choice() == PDF_UNSANDBOXED
    assert asked == [], "the flag presets Yes: no dialog is opened"
    outcome.window.session.close()


def test_the_choice_is_not_read_from_the_environment_by_the_session(monkeypatch):
    """The session and History are given the decision; they do not sniff privilege themselves."""
    _as_root(monkeypatch)
    assert "QTWEBENGINE_DISABLE_SANDBOX" not in os.environ
    _program, args, env = pdf.pdf_helper_command("a.html", "a.pdf", "A4")
    assert "QTWEBENGINE_DISABLE_SANDBOX" not in env, "an elevated parent handed the child sandbox-off knobs unprompted"


# --- the run-end path --------------------------------------------------------------------------


def test_elevated_run_end_without_the_flag_writes_html_and_skips_the_pdf(tmp_path, monkeypatch, qapp):
    """No PDF, no helper process, but report_written still fires so nothing waits forever."""
    _as_root(monkeypatch)
    logs: list[str] = []
    record: list[dict] = []
    session, events = _run_session(
        tmp_path, monkeypatch, qapp, pdf_sandbox_choice(log=lambda _l, _s, text: logs.append(text)), record, logs
    )
    html_path, pdf_path = events[-1]
    assert pdf_path == "", "a skipped PDF must still report, with no PDF path"
    assert Path(html_path).is_file() and Path(html_path).stat().st_size > 0
    assert not Path(html_path).with_suffix(".pdf").exists()
    assert record == [], "the helper was started even though the PDF was skipped"
    assert any("skipped" in text.lower() for text in logs), logs
    session.close()


def test_elevated_run_end_with_the_flag_tells_the_helper_to_render_unsandboxed(tmp_path, monkeypatch, qapp):
    """Yes: the child gets `--unsandboxed` and the sandbox-off environment, and the PDF is written."""
    _as_root(monkeypatch)
    logs: list[str] = []
    record: list[dict] = []
    session, events = _run_session(
        tmp_path,
        monkeypatch,
        qapp,
        pdf_sandbox_choice(True, log=lambda _l, _s, text: logs.append(text)),
        record,
        logs,
    )
    html_path, pdf_path = events[-1]
    assert pdf_path, "the flag was given but no PDF was produced"
    assert len(record) == 1, record
    args, env = record[0]["args"], record[0]["env"]
    assert pdf.UNSANDBOXED_FLAG in args, args
    assert env["QTWEBENGINE_DISABLE_SANDBOX"] == "1"
    assert "--no-sandbox" in env["QTWEBENGINE_CHROMIUM_FLAGS"]
    session.close()


def test_elevated_run_end_with_a_dialog_answers_the_dialog_before_rendering(tmp_path, monkeypatch, qapp):
    """The GUI asks once per run, and a No skips the PDF just like headless does."""
    _as_root(monkeypatch)
    asked: list[bool] = []

    def ask() -> bool:
        asked.append(True)
        return False  # the Operator declined

    record: list[dict] = []
    logs: list[str] = []
    session, events = _run_session(
        tmp_path,
        monkeypatch,
        qapp,
        pdf_sandbox_choice(ask=ask, log=lambda _l, _s, text: logs.append(text)),
        record,
        logs,
    )
    assert len(asked) == 1, "the Operator was not asked, or was asked more than once"
    assert events[-1][1] == ""
    assert record == []
    session.close()


def test_ordinary_user_run_end_renders_the_pdf_with_the_sandbox_on(tmp_path, monkeypatch, qapp):
    """Nothing changes for a normal Operator: the PDF is rendered, with no question asked."""
    _as_ordinary_user(monkeypatch)
    asked: list[bool] = []
    record: list[dict] = []
    logs: list[str] = []
    session, events = _run_session(
        tmp_path,
        monkeypatch,
        qapp,
        pdf_sandbox_choice(ask=lambda: asked.append(True) or True, log=lambda _l, _s, text: logs.append(text)),
        record,
        logs,
    )
    assert asked == []
    html_path, pdf_path = events[-1]
    assert pdf_path and Path(pdf_path).exists()
    assert len(record) == 1 and pdf.UNSANDBOXED_FLAG not in record[0]["args"]
    assert "QTWEBENGINE_DISABLE_SANDBOX" not in record[0]["env"]
    session.close()


def test_the_skip_is_written_to_the_application_log(tmp_path, monkeypatch, qapp):
    """The run's own app log carries the skip, not just stdout."""
    _as_root(monkeypatch)
    env = write_ate_env(tmp_path)
    log_dir = tmp_path / "applog"
    log_dir.mkdir()
    from ate.log import open_app_log

    app_log = open_app_log(log_dir)
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", recording_pdf_helper([]))
    session = HeadlessRunSession(env["config"], app_log=app_log, pdf_choice=pdf_sandbox_choice(log=app_log.log))
    reported: list[tuple[str, str]] = []
    session.report_written.connect(lambda html, pdf_path: reported.append((html, pdf_path)), Qt.ConnectionType.QueuedConnection)
    _run_to_finish(session, qapp)
    # Packet 30d review: the report is written on a zero timer after FINISHED, so the pump has to
    # wait for it rather than run for a fixed time.
    assert _pump_events(qapp, lambda: reported), "the run-end report never reported"
    app_log.close()
    text = "\n".join(p.read_text(encoding="utf-8") for p in log_dir.glob("**/*") if p.is_file())
    assert any("skipped" in line.lower() and "sandbox" in line.lower() and session.run_uuid in line for line in text.splitlines()), text[:400]
    session.close()


# --- History's Regenerate -----------------------------------------------------------------------


def test_history_regenerate_asks_and_skips_when_declined(tmp_path, monkeypatch, qapp):
    """Regenerate writes the HTML and asks before the PDF; No means no helper is started."""
    _as_root(monkeypatch)
    env = write_ate_env(tmp_path)
    config = read_config(env["config"])
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", recording_pdf_helper([]))
    session = HeadlessRunSession(config, pdf_choice=pdf_sandbox_choice(ask=lambda: False))
    _run_to_finish(session, qapp)
    run_uuid = session.run_uuid
    history = HistorySession(session.store, config=config, pdf_choice=pdf_sandbox_choice(ask=lambda: False))
    result = history.regenerate(run_uuid, html=True, pdf=True, parent=QObject())
    assert result["html"] and Path(result["html"]).is_file()
    assert result["pdf_skipped"] is True and result["pdf_started"] is False
    assert not Path(result["html"]).with_suffix(".pdf").exists()
    session.close()


def test_history_regenerate_renders_when_the_answer_is_yes(tmp_path, monkeypatch, qapp):
    """Yes passes `--unsandboxed` to the child; an ordinary user gets no flag at all."""
    env = write_ate_env(tmp_path)
    config = read_config(env["config"])
    record: list[dict] = []
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", recording_pdf_helper(record))
    for elevated, expect_flag in ((True, True), (False, False)):
        record.clear()
        if elevated:
            _as_root(monkeypatch)
        else:
            _as_ordinary_user(monkeypatch)
        session = HeadlessRunSession(config, pdf_choice=pdf_sandbox_choice(ask=lambda: elevated))
        _run_to_finish(session, qapp)
        record.clear()  # keep only what History's Regenerate handed the child
        history = HistorySession(session.store, config=config, pdf_choice=pdf_sandbox_choice(ask=lambda: elevated))
        result = history.regenerate(session.run_uuid, html=True, pdf=True, parent=QObject())
        assert result["pdf_started"] is True and result["pdf_skipped"] is False
        assert len(record) == 1, record
        assert (pdf.UNSANDBOXED_FLAG in record[0]["args"]) is expect_flag
        assert ("QTWEBENGINE_DISABLE_SANDBOX" in record[0]["env"]) is elevated
        session.close()


# --- the helper child ---------------------------------------------------------------------------


def test_pdf_helper_as_root_without_the_argument_refuses(tmp_path, monkeypatch, capsys):
    """`ate _pdf` run directly by a root operator exits non-zero and says why."""
    _as_root(monkeypatch)
    html = tmp_path / "report.html"
    html.write_text("<html><body>x</body></html>", encoding="utf-8")
    pdf_path = tmp_path / "report.pdf"
    code = main(["_pdf", str(html), str(pdf_path)])
    err = capsys.readouterr().err
    assert code != 0, "a privileged helper rendered without being asked"
    assert not pdf_path.exists()
    assert "--unsandboxed" in err and ("root" in err.lower() or "administrator" in err.lower()), err


def test_pdf_helper_as_root_with_the_argument_does_not_refuse(monkeypatch, tmp_path, capsys):
    """The only difference is the argument: with it, the helper is allowed to go on."""
    _as_root(monkeypatch)
    monkeypatch.delenv("QTWEBENGINE_DISABLE_SANDBOX", raising=False)
    monkeypatch.delenv("QTWEBENGINE_CHROMIUM_FLAGS", raising=False)
    assert pdf._apply_sandbox_decision(True) is False
    assert os.environ["QTWEBENGINE_DISABLE_SANDBOX"] == "1"
    assert "--no-sandbox" in os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]
    assert not capsys.readouterr().err, "an accepted decision should print nothing"


def test_pdf_helper_for_an_ordinary_user_ignores_the_argument(monkeypatch, capsys):
    """The flag cannot take the sandbox away from someone who does not need to."""
    _as_ordinary_user(monkeypatch)
    monkeypatch.delenv("QTWEBENGINE_DISABLE_SANDBOX", raising=False)
    monkeypatch.delenv("QTWEBENGINE_CHROMIUM_FLAGS", raising=False)
    assert pdf._apply_sandbox_decision(True) is False
    assert "QTWEBENGINE_DISABLE_SANDBOX" not in os.environ
    assert "--no-sandbox" not in os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    assert "ignored" in capsys.readouterr().err.lower()


# --- the CLI and _selftest -----------------------------------------------------------------------


def test_help_documents_the_flag(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "--allow-unsandboxed-pdf" in out, out


def test_selftest_run_as_root_without_the_flag_fails_and_says_the_flag(capsys, monkeypatch, tmp_path):
    """The CI runs as root: it must be told, and the failure names the fix."""
    _as_root(monkeypatch)
    code = main(["_selftest", "--run"])
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert code != 0, "a privileged selftest passed without rendering a PDF"
    assert "skipped" in out.lower() and "no --allow-unsandboxed-pdf was given" in out, out
    assert "_selftest --run" in out, "the failure must say how to rerun it"


def test_selftest_run_as_root_with_the_flag_before_the_subcommand_renders_a_pdf(capsys, monkeypatch):
    """`ate --allow-unsandboxed-pdf _selftest --run` writes a PDF that has text in it."""
    _as_root(monkeypatch)
    code = main(["--allow-unsandboxed-pdf", "_selftest", "--run"])
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert code == 0, out
    assert "with text" in out, out


def test_docs_advertise_the_flag():
    doc = Path(__file__).resolve().parents[1] / "docs" / "building-from-source.md"
    assert "--allow-unsandboxed-pdf" in doc.read_text(encoding="utf-8")


# --- an environment that already switched the sandbox off (30d review) -------------


INHERITED_FLAGS = "--no-sandbox --disable-process-per-site"


def _inherit_sandbox_off(monkeypatch) -> None:
    """The Operator exported Chromium's sandbox-off knobs for some other program."""
    monkeypatch.setenv("QTWEBENGINE_DISABLE_SANDBOX", "1")
    monkeypatch.setenv("QTWEBENGINE_CHROMIUM_FLAGS", INHERITED_FLAGS)


def _entry_point():
    """The frozen entry point, loaded without a frozen bundle (packet 27c seam)."""
    spec = importlib.util.spec_from_file_location("ate_entry_30d", ATE_ENTRY)
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    return entry


def test_an_inherited_off_setting_is_stripped_for_an_ordinary_user(monkeypatch):
    """An exported --no-sandbox is not an ATE decision: the child gets no sandbox-off settings."""
    _as_ordinary_user(monkeypatch)
    _inherit_sandbox_off(monkeypatch)
    logs: list[tuple[str, str]] = []
    _program, args, env = pdf.pdf_helper_command(
        "a.html", "a.pdf", "A4", False, log=lambda level, _source, text: logs.append((level, text))
    )
    assert pdf.UNSANDBOXED_FLAG not in args
    assert "QTWEBENGINE_DISABLE_SANDBOX" not in env, env
    flags = env.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
    assert "--no-sandbox" not in flags, f"the inherited --no-sandbox survived: {flags}"
    assert "--disable-process-per-site" in flags, f"the flags the user set were thrown away: {flags}"
    assert any(
        level == "WARNING" and "QTWEBENGINE_DISABLE_SANDBOX" in text and "--no-sandbox" in text
        for level, text in logs
    ), logs
    assert "QTWEBENGINE_DISABLE_SANDBOX" in os.environ, "the parent scrubbed its own environment, not the child's"


def test_an_inherited_off_setting_is_stripped_for_an_elevated_run_that_answered_no(monkeypatch):
    """Root with a declined dialog: the helper's environment has nothing that switches the sandbox off."""
    _as_root(monkeypatch)
    _inherit_sandbox_off(monkeypatch)
    logs: list[str] = []
    plan = pdf_sandbox_choice(ask=lambda: False, log=lambda _level, _source, text: logs.append(text))()
    assert plan == PDF_SKIPPED
    _program, _args, env = pdf.pdf_helper_command("a.html", "a.pdf", "A4", plan == PDF_UNSANDBOXED, log=lambda _l, _s, text: logs.append(text))
    assert "QTWEBENGINE_DISABLE_SANDBOX" not in env, env
    assert "--no-sandbox" not in env.get("QTWEBENGINE_CHROMIUM_FLAGS", ""), env
    assert any("inherited" in text for text in logs), logs


def test_a_yes_decision_keeps_the_sandbox_off_knobs_and_says_nothing_about_inheritance(monkeypatch):
    """Yes is Yes: the knobs are set, the user's other flags survive, and nothing is removed."""
    _as_root(monkeypatch)
    _inherit_sandbox_off(monkeypatch)
    logs: list[str] = []
    plan = pdf_sandbox_choice(True, log=lambda _level, _source, text: logs.append(text))()
    assert plan == PDF_UNSANDBOXED
    _program, args, env = pdf.pdf_helper_command("a.html", "a.pdf", "A4", plan == PDF_UNSANDBOXED, log=lambda _l, _s, text: logs.append(text))
    assert pdf.UNSANDBOXED_FLAG in args
    assert env["QTWEBENGINE_DISABLE_SANDBOX"] == "1"
    assert "--no-sandbox" in env["QTWEBENGINE_CHROMIUM_FLAGS"]
    assert "--disable-process-per-site" in env["QTWEBENGINE_CHROMIUM_FLAGS"]
    assert not any("inherited" in text for text in logs), logs


def test_the_helper_scrubs_its_own_inherited_environment(monkeypatch, capsys):
    """`ate _pdf` decides from its argument, not from what the environment already carried."""
    _as_ordinary_user(monkeypatch)
    _inherit_sandbox_off(monkeypatch)
    assert pdf._apply_sandbox_decision(False) is False
    assert "QTWEBENGINE_DISABLE_SANDBOX" not in os.environ
    assert "--no-sandbox" not in os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    assert "--disable-process-per-site" in os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    err = capsys.readouterr().err
    assert "WARNING" in err and "QTWEBENGINE_DISABLE_SANDBOX" in err, err


def test_the_frozen_entry_point_scrubs_an_inherited_off_setting_before_qt_starts(monkeypatch, capsys):
    """In the bundle, QtWebEngine reads the environment before `render_pdf` runs, so the entry point
    is where an inherited sandbox-off setting has to go (packet 30d review)."""
    entry = _entry_point()
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    for elevated in (True, False):
        _inherit_sandbox_off(monkeypatch)
        monkeypatch.setattr(os, "geteuid", lambda: 0 if elevated else 1000)
        entry._prepare_webengine_env(["_pdf", "a.html", "a.pdf"])
        assert "QTWEBENGINE_DISABLE_SANDBOX" not in os.environ, f"root={elevated}"
        assert "--no-sandbox" not in os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", ""), f"root={elevated}"
        assert "--disable-process-per-site" in os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
        assert "inherited" in capsys.readouterr().err.lower(), f"removing it was not reported (root={elevated})"

    monkeypatch.delenv("QTWEBENGINE_DISABLE_SANDBOX", raising=False)
    monkeypatch.delenv("QTWEBENGINE_CHROMIUM_FLAGS", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("QTWEBENGINE_CHROMIUM_FLAGS", INHERITED_FLAGS)
    entry._prepare_webengine_env(["_pdf", "a.html", "a.pdf", "--unsandboxed"])
    assert os.environ["QTWEBENGINE_DISABLE_SANDBOX"] == "1", "the parent's Yes was not honoured"
    assert "--no-sandbox" in os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]
    assert "inherited" not in capsys.readouterr().err.lower(), "a chosen unsandboxed render was warned about"


# --- FINISHED comes before the question (30d review) --------------------------------


def test_the_question_is_asked_after_finished_is_announced_and_the_html_is_written(tmp_path, monkeypatch, qapp):
    """Order is FINISHED → HTML on disk → question, and report_written fires exactly once."""
    _as_root(monkeypatch)
    env = write_ate_env(tmp_path)
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", recording_pdf_helper([]))
    seen: list[tuple[SessionState, bool, int, bool]] = []
    session = HeadlessRunSession(
        env["config"],
        pdf_choice=pdf_sandbox_choice(
            ask=lambda: seen.append(
                (session.state, session._engine is None, len(list(env["reports_dir"].rglob("*.html"))), session._report_written)
            ) or False
        ),
    )
    events: list[tuple[str, str]] = []
    session.report_written.connect(lambda html, pdf_path: events.append((html, pdf_path)), Qt.ConnectionType.QueuedConnection)
    _run_to_finish(session, qapp)
    assert _pump_events(qapp, lambda: seen), "the Operator was never asked about the sandbox"
    state, engine_released, html_files, already_reported = seen[0]
    assert state is SessionState.FINISHED, f"the box opened while the session still said {state.value}"
    assert engine_released, "the finished worker was still held when the question was asked"
    assert html_files == 1, "the HTML Test Report was not on disk when the question was asked"
    assert not already_reported, "report_written fired before the decision was made"
    assert _pump_events(qapp, lambda: events), "report_written never fired after the decision"
    assert len(events) == 1, events
    session.close()


def test_the_run_screen_is_finished_when_the_pdf_question_is_asked(tmp_path, monkeypatch, qapp):
    """The Run screen must have seen FINISHED before the dialog opens: Abort off, New run on."""
    _as_root(monkeypatch)
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", recording_pdf_helper([]))
    seen: list[tuple[SessionState, bool, bool]] = []

    def ask() -> bool:
        screen = window.run_screen
        view = getattr(screen, "run_view", screen)
        seen.append((screen.state, view.abort_button.isEnabled(), view.new_run_button.isEnabled()))
        return False

    outcome = launch(
        ["--config", str(env["config"])],
        exec_=False,
        prompt=lambda _path: True,
        alert=lambda _msg: None,
        pdf_dialog=ask,
    )
    window = outcome.window
    session = window.session
    session.select_schedule("Passing schedule")
    session.set_operator("Ada")
    session.set_project("Power board")
    session.set_uut_type("PSU")
    session.set_uut_serial("PS-1")
    session.start()
    assert _pump_events(qapp, lambda: session.state is SessionState.FINISHED), "the run never finished"
    assert _pump_events(qapp, lambda: seen), "the sandbox question was never asked"
    state, abort_enabled, new_run_enabled = seen[0]
    assert state is SessionState.FINISHED, f"the dialog opened while the Run screen said {state.value}"
    assert not abort_enabled, "Abort is still enabled while the PDF question is up"
    assert new_run_enabled, "the finished run is not offered as finished while the question is up"
    session.close()


def test_a_session_closed_right_after_finishing_still_writes_its_html(tmp_path, monkeypatch, qapp):
    """The report write is deferred by a timer, so closing a session must not drop it (spec 21a)."""
    _as_root(monkeypatch)
    env = write_ate_env(tmp_path)
    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", recording_pdf_helper([]))
    session = HeadlessRunSession(env["config"], pdf_choice=pdf_sandbox_choice(ask=lambda: False))
    _run_to_finish(session, qapp)
    session.close()
    assert list(env["reports_dir"].rglob("*.html")), "closing the session lost the run-end report"
    from ate.report.pdf_job import _live_pdf_jobs

    assert not _live_pdf_jobs, "a session that is closing left a PDF helper running"


def test_the_decision_log_names_the_report_it_decided_about(monkeypatch, tmp_path, qapp):
    """History's choices can only be matched to a run if the line says which report it is about."""
    _as_root(monkeypatch)
    logs: list[str] = []
    decide = pdf_sandbox_choice(ask=lambda: True, log=lambda _level, _source, text: logs.append(text))
    assert decide("Test_Report-2026-10-31-1a2b3c.html") == PDF_UNSANDBOXED
    assert any("Test_Report-2026-10-31-1a2b3c.html" in text for text in logs), logs

    env = write_ate_env(tmp_path)
    run_logs: list[str] = []
    session, events = _run_session(
        tmp_path, monkeypatch, qapp, pdf_sandbox_choice(ask=lambda: True, log=lambda _l, _s, text: run_logs.append(text)), [], run_logs
    )
    html_name = Path(events[-1][0]).name
    assert any(html_name in text for text in run_logs), run_logs
    session.close()


# --- the selftest keeps what it names (30d review) ----------------------------------


def test_selftest_failure_keeps_the_directory_it_names(capsys, monkeypatch):
    """The message points at a report; deleting the directory would leave the name pointing nowhere."""
    _as_root(monkeypatch)
    code = main(["_selftest", "--run"])
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert code != 0, out
    assert "kept at" in out, out
    kept = Path(out.split("kept at ", 1)[1].split(" so the report", 1)[0].strip())
    assert kept.is_dir(), f"the failure named {kept}, which is gone"
    assert list(kept.rglob("*.html")), f"{kept} holds no report"
    shutil.rmtree(kept, ignore_errors=True)


def test_selftest_reports_what_the_failed_helper_said(capsys, monkeypatch):
    """A helper that writes no PDF must say why, not just that it wrote nothing."""
    _as_ordinary_user(monkeypatch)
    monkeypatch.setattr(
        "ate.report.pdf_job.pdf_helper_command",
        recording_pdf_helper([], "import sys; sys.stderr.write('Chromium found no fonts'); sys.exit(1)"),
    )
    code = main(["_selftest", "--run"])
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert code != 0, out
    assert "Chromium found no fonts" in out, out
    kept = Path(out.split("kept at ", 1)[1].split(" so the report", 1)[0].strip())
    shutil.rmtree(kept, ignore_errors=True)


def test_an_ordinary_user_with_the_flag_is_told_it_has_no_effect(capsys, monkeypatch):
    """The flag cannot take the sandbox away from someone who never lost it."""
    _as_ordinary_user(monkeypatch)
    logs: list[str] = []
    assert pdf_sandbox_choice(True, log=lambda _level, _source, text: logs.append(text))() == PDF_SANDBOXED
    assert any("no effect" in text for text in logs), logs
    code = main(["--allow-unsandboxed-pdf", "_selftest", "--run"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "no effect" in out, out


# --- the dialog's wording and the elevation check ------------------------------------


def test_the_dialog_warns_and_asks_in_two_sentences():
    """A question, the consequence, the icon, and Skip as the default (packet 30d review)."""
    box = _pdf_sandbox_box()
    text = (box.text() + " " + box.informativeText()).lower()
    assert "sandbox" in text and ("root" in text or "administrator" in text)
    assert "html" in text, "the dialog must say the HTML report is already saved"
    assert "render the pdf without the sandbox?" in text, box.text()
    assert "affect this computer" in text, "the consequence of turning the sandbox off is not stated"
    assert box.windowTitle() == "Render PDF without sandbox?", box.windowTitle()
    assert box.icon() == QMessageBox.Icon.Warning, box.icon()
    labels = [button.text() for button in box.buttons()]
    assert "Generate PDF without sandbox" in labels and "Skip PDF" in labels
    assert box.defaultButton().text() == "Skip PDF", labels


def test_an_unreadable_admin_check_leaves_the_sandbox_on_and_says_so(monkeypatch):
    """IsUserAnAdmin can fail; a failed render must not blame the sandbox for a guess."""
    monkeypatch.setattr(os, "name", "nt")
    assert pdf._elevation() is None, "the Windows check should report that it could not tell"
    assert pdf._is_elevated() is False, "unknown elevation must not disable the sandbox"
    hint = pdf._render_failure_hint(False)
    assert "elevated is unknown" in hint, hint
    assert "IsUserAnAdmin" in hint
    # On Linux the check cannot fail, so the hint stays the sandbox one.
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert "elevated is unknown" not in pdf._render_failure_hint(True)
    assert "sandbox" in pdf._render_failure_hint(True)
    assert pdf._render_failure_hint(False) == "", "an ordinary user's failure is not an elevation story"
