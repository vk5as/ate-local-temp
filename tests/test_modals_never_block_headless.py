"""No modal in ATE may wait for a click that cannot happen.

Two CI runs of `.github/workflows/ci.yml` hung on exactly this: the packet 30d unsandboxed-PDF
question first, then the ticket 06c run-start mismatch confirmation — `QDialog.exec()` on a runner
with `QT_QPA_PLATFORM=offscreen` never returns, the job is cancelled at its timeout, and no
traceback is ever printed. Every modal in `src/ate` is therefore armed with a self-answer through
`ate.dialogs.dismiss_if_headless` before `exec()` is called.

What each answers headless:

| dialog | answer |
|---|---|
| missing-config prompt (`app._missing_config_dialog`) | Abort — no config is created |
| alert (`app._alert_dialog`) | acknowledged, then the launch stops |
| close confirmation (`app._close_confirm_dialog`) | Keep running — the run is not aborted |
| PDF sandbox question (`app._pdf_sandbox_dialog`) | Skip PDF |
| run-start mismatch (`MismatchConfirmation`) | Start the run; store what `database.storeMismatchedFiles` says — `always` stores, `ask` and `never` do not (packet 31d) |

The first test is a static scan, so a new modal cannot be added without an answer; the second runs
the real dialogs in a fresh interpreter (a monkeypatch cannot reach a subprocess) and fails if any
of them blocks; the rest prove the mismatch confirmation's answer comes from the config rather than
from a default nobody chose, in a real headless run and through the `ATE_AUTO_DISMISS_DIALOG` seam.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QDialog, QMessageBox

from ate.dialogs import dismiss_if_headless
from ate.ui.run_widgets import MismatchConfirmation

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "ate"

#: What each `database.storeMismatchedFiles` setting answers when nobody is at a screen (packet 31d):
#: `always` stores the mismatched bytes, `never` stores nothing, and `ask` stores nothing because the
#: ticked-by-default box is not a choice anyone made. The run starts in all three.
HEADLESS_ANSWERS = (("always", True), ("never", False), ("ask", False))

#: Every modal in the package, and the answer it gives when nobody can click. A new dialog `exec()`
#: has to appear here, with its answer, or these tests fail.
MODALS = {
    "app.py:_missing_config_dialog": "Abort (no config is created)",
    "app.py:_alert_dialog": "acknowledged; the launch stops",
    "app.py:_close_confirm_dialog": "Keep running (the run is not aborted)",
    "app.py:_pdf_sandbox_dialog": "Skip PDF",
    "ui/run_widgets.py:_confirm_store_choice": "Start the run; store by database.storeMismatchedFiles",
}

#: A dialog `exec()`, as opposed to the `app.exec()` of a QApplication event loop.
MODAL_EXEC = re.compile(r"\b(box|dialog|dlg|message)\.exec\(\)")


def _modal_exec_sites() -> list[str]:
    """Every guarded or unguarded `dialog.exec()` in src/ate, as `file:function:line`."""
    sites: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        function = ""
        for number, line in enumerate(lines, start=1):
            match = re.match(r"\s*def (\w+)", line)
            if match:
                function = match.group(1)
            if MODAL_EXEC.search(line):
                sites.append(f"{path.relative_to(SRC)}:{function}:{number}")
    return sites


def test_every_modal_in_the_package_is_guarded() -> None:
    """Each dialog `exec()` is preceded by a `dismiss_if_headless` arm, and each is accounted for."""
    unguarded = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if not MODAL_EXEC.search(line):
                continue
            preceding = "\n".join(text.splitlines()[max(0, number - 8):number - 1])
            if "dismiss_if_headless(" not in preceding:
                unguarded.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")
    assert not unguarded, "modal exec() with no headless answer: " + "; ".join(unguarded)
    sites = _modal_exec_sites()
    assert len(sites) == len(MODALS), f"{len(sites)} modal exec() sites, {len(MODALS)} answers: {sites}"
    functions = {site.split(":")[1] for site in sites}
    assert functions == {name.split(":")[1] for name in MODALS}, functions


def test_the_event_loops_are_not_modals() -> None:
    """`app.exec()` is the application loop, not a question: it is bounded, not self-answered."""
    loops = [
        (path.relative_to(SRC), line.strip())
        for path in sorted(SRC.rglob("*.py"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if re.search(r"\bapp\.exec\(\)", line)
    ]
    assert loops, "the QApplication event loops were expected"
    pdf = (SRC / "report" / "pdf.py").read_text(encoding="utf-8")
    assert "QTimer.singleShot(30_000, app.quit)" in pdf, "the PDF helper's loop has no bounded exit"
    app = (SRC / "app.py").read_text(encoding="utf-8")
    assert "exec_: bool = True" in app, "launch() has no seam for a caller that must not enter the loop"


def test_headless_modals_return_in_a_fresh_process() -> None:
    """The real dialogs, in a new interpreter, with no display and no dismiss variable set.

    A monkeypatch cannot reach a subprocess, so this is the case the seams are really for. If a
    guard is removed the subprocess sits in `exec()` until this test's own bound kills it, which is
    the CI hang these tests exist to prevent."""
    code = (
        "from PyQt6.QtWidgets import QApplication\n"
        "app = QApplication([])\n"
        "from ate.app import _missing_config_dialog, _alert_dialog, _close_confirm_dialog, _pdf_sandbox_dialog\n"
        "print(_missing_config_dialog('/nonexistent/ate-config.json'))\n"
        "print(_alert_dialog('headless probe'))\n"
        "print(_close_confirm_dialog('Close ATE and stop the run?'))\n"
        "print(_pdf_sandbox_dialog())\n"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"), QT_QPA_PLATFORM="offscreen")
    env.pop("ATE_AUTO_DISMISS_DIALOG", None)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    # Abort on the missing-config prompt, None for the alert, False for "abort the run and quit?",
    # and False (Skip PDF) for the unsandboxed-PDF question.
    assert proc.stdout.split() == ["False", "None", "False", "False"], proc.stdout


#: The real run that hung CI: `begin_run` → `_confirm_store_choice` with nobody to click Start. It
#: prints the dialog's answer, whether the run finished, and what the recorded run_file rows say.
MISMATCH_RUN_CODE = (
    "import sys, time\n"
    "from PyQt6.QtWidgets import QApplication\n"
    "app = QApplication([])\n"
    "from ate.run_session.headless import HeadlessRunSession, SessionState\n"
    "from ate.ui.staged_run_screen import StagedRunScreen\n"
    "session = HeadlessRunSession(sys.argv[1])\n"
    "screen = StagedRunScreen(session)\n"
    "screen._refresh_schedule_list()\n"
    "lw = screen.test_schedule_stage.schedule_list\n"
    "item = next(lw.item(i) for i in range(lw.count()) if 'Mismatch schedule' in lw.item(i).text())\n"
    "lw.setCurrentItem(item)\n"
    "screen._begin_run_from_selection()\n"
    "screen.steps_options_stage.run_button.click()\n"
    "print('answer', screen._confirm_dialog.store_choice())\n"
    "deadline = time.monotonic() + 20.0\n"
    "while time.monotonic() < deadline:\n"
    "    app.processEvents()\n"
    "    if session._state == SessionState.FINISHED:\n"
    "        break\n"
    "    time.sleep(0.005)\n"
    "print('finished', session._state == SessionState.FINISHED)\n"
    "rows = session.store.run_files(session.run_uuid)\n"
    "print('stored', any(row['content'] is not None for row in rows))\n"
    "flagged = [row for row in rows if row['mismatch']]\n"
    "print('flagged', len(flagged), all(row['expected_sha256'] and row['computed_sha256'] for row in flagged))\n"
    "print('logged', any('checksum mismatch' in line['text'] for line in session.store.log_lines(session.run_uuid)))\n"
)


def _headless_mismatch_run(config: Path, *, dismiss_seam: bool = False, platform: str = "offscreen") -> list[str]:
    """Run a mismatched schedule in a fresh interpreter with no display, and return its print lines."""
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"), QT_QPA_PLATFORM=platform)
    if dismiss_seam:
        env["ATE_AUTO_DISMISS_DIALOG"] = "1"
    else:
        env.pop("ATE_AUTO_DISMISS_DIALOG", None)
    proc = subprocess.run([sys.executable, "-c", MISMATCH_RUN_CODE, str(config)], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return [line for line in proc.stdout.splitlines() if line.startswith(("answer ", "finished ", "stored ", "flagged ", "logged "))]


@pytest.mark.parametrize("setting, stores", HEADLESS_ANSWERS)
def test_a_mismatched_run_starts_in_a_fresh_headless_process(tmp_path, setting: str, stores: bool) -> None:
    """Start a run whose files mismatch, with no Operator: the confirmation answers itself.

    `database.storeMismatchedFiles` decides whether the mismatched bytes end up on the run_file rows
    (packet 31d), and the mismatch is still flagged, logged and recorded with its checksums whatever
    that answer is.
    """
    config = _write_mismatched_release(tmp_path, setting)
    lines = _headless_mismatch_run(config)
    assert lines == [
        f"answer {stores}",
        "finished True",
        f"stored {stores}",
        "flagged 1 True",
        "logged True",
    ], lines


def test_the_dismiss_seam_answers_a_real_run_the_same_way(tmp_path) -> None:
    """`ATE_AUTO_DISMISS_DIALOG` and `minimal` give the same mismatch answer as `offscreen`.

    A subprocess cannot be monkeypatched, so the seam is proved in a real run: the `always` Station
    still stores the bytes when the dialog is closed by the seam, and again on the other headless
    platform, `minimal`."""
    for seam, platform in ((True, "offscreen"), (False, "minimal"), (True, "minimal")):
        lines = _headless_mismatch_run(_write_mismatched_release(tmp_path / f"seam{seam}{platform}", "always"), dismiss_seam=seam, platform=platform)
        assert lines[0] == "answer True", f"{platform} seam={seam}: {lines}"
        assert lines[2] == "stored True", f"{platform} seam={seam}: {lines}"


def _write_mismatched_release(tmp_path: Path, setting: str) -> Path:
    """One Media Release whose bytes on disk do not match the checksum its `.atedef` records, and a
    config whose `database.storeMismatchedFiles` is `setting`. Returns the config path."""
    src = (
        'return ScheduleParams("Mismatch schedule", "one step")\n'
        '  :setSchedule({StepParams("ok"):setStepNo("1.")'
        ':setAction(function() ATE.report(Result.PASS, "ok") end)})\n'
    )
    defs = tmp_path / "defs"
    release = defs / "rel"
    release.mkdir(parents=True, exist_ok=True)
    (release / "main.tslua").write_bytes(src.encode("utf-8"))
    (release / "rel.atedef").write_text(
        json.dumps(
            {
                "format": 1,
                "mediaNumber": "REL-1",
                "name": "Mismatch release",
                "version": "1.0.0",
                "files": [{"path": "main.tslua", "sha256": "0" * 64}],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "format": 1,
                "station": {"name": "BENCH", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c10"},
                "defaultProject": "Power board",
                "database": {"type": "sqlite", "path": str(tmp_path / "ate.db"), "storeMismatchedFiles": setting},
                "reports": {"directory": str(tmp_path / "reports")},
                "testDefinitions": {"directories": [str(defs)]},
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    return config


@pytest.mark.parametrize("setting, answer", HEADLESS_ANSWERS)
def test_the_headless_answer_follows_the_setting(qapp, setting: str, answer: bool) -> None:
    """`answer_from_setting` answers Start and stores exactly what `database.storeMismatchedFiles` says.

    `ask` is the one that needs care: the box is ticked by default, and default is not a decision, so
    nobody's answer to it is "store nothing" (packet 31d)."""
    dialog = MismatchConfirmation("S", "REL", [("a.tslua", "aa", "bb", "MR-1")], setting)
    dialog.answer_from_setting()
    assert dialog.is_accepted(), f"{setting}: an unattended run must still start"
    assert dialog.store_choice() is answer, f"{setting} answered {dialog.store_choice()}"
    if dialog.store_checkbox is not None:
        assert not dialog.store_checkbox.isChecked(), f"{setting} left the box ticked"


@pytest.mark.parametrize("setting, answer", HEADLESS_ANSWERS)
def test_the_dismiss_seam_arms_the_same_answer(qapp, monkeypatch, setting: str, answer: bool) -> None:
    """`ATE_AUTO_DISMISS_DIALOG` arms the mismatch dialog with the setting's answer, not a bare accept.

    `QT_QPA_PLATFORM` is set to a window server here so the seam alone is what arms the dialog."""
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    monkeypatch.setenv("ATE_AUTO_DISMISS_DIALOG", "1")
    dialog = MismatchConfirmation("S", "REL", [("a.tslua", "aa", "bb", "MR-1")], setting)
    assert dismiss_if_headless(dialog, MismatchConfirmation.answer_from_setting) is True
    for _ in range(5):
        qapp.processEvents()
    assert dialog.store_choice() is answer, f"{setting} answered {dialog.store_choice()}"


def test_dismiss_if_headless_does_nothing_when_a_display_exists(monkeypatch) -> None:
    """On a machine with a display the guard stays out of the way: the Operator still answers."""
    monkeypatch.delenv("ATE_AUTO_DISMISS_DIALOG", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "xcb")
    box = QMessageBox()
    assert dismiss_if_headless(box) is False
    dialog = QDialog()
    assert dismiss_if_headless(dialog, lambda d: d.reject()) is False
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    assert dismiss_if_headless(box) is True
