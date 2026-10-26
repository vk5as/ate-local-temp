"""Packet 06c — the mismatch flag, the run-start confirmation and `database.storeMismatchedFiles`.

A Media Release whose bytes on disk no longer match the checksums written in its `.atedef` is flagged
in the Run screen's schedule list (⚠) and under the picker, and Start asks before the run begins:
with `ask` the confirmation carries a "Store mismatched files with this Test Run" checkbox ticked by
default, `always` and `never` need no checkbox at all, and only a file that actually mismatched gets
its raw bytes written into `run_file.content`. Everything runs through the real
:class:`HeadlessRunSession` with real files on disk; the dialog is driven by injecting a choice
callable, so no test ever blocks on `exec()`.
"""

import hashlib
import json
import time
from pathlib import Path
from typing import Optional

from ate.history.session import HistorySession
from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.storage.store import RunStore
from ate.ui.history_screen import HistoryScreen
from ate.ui.staged_run_screen import StagedRunScreen

# The helper whose checksum the .atedef records wrongly, and the schedule that requires it.
HELPER_SRC = "-- packet 06c: a helper whose bytes changed after the release was cut\nreturn { name = 'helper', value = 42 }\n"
SCHEDULE_SRC = (
    'local h = require("helper")\n'
    'local dev = require("ate.devices")\n'
    'return ScheduleParams("Checksum schedule", "requires a helper and an ate. module") :setSchedule({\n'
    '  StepParams("check"):setStepNo("1.")\n'
    '    :setAction(function() ATE.report(Result.PASS, "helper is "..h.value) end),\n'
    "})\n"
)
WRONG_SHA = "0" * 64  # cannot match the file on disk: the point of the packet


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_release(tmp_path: Path, *, mismatch: bool = True, setting: str = "ask") -> dict[str, Path]:
    """Write one Media Release (helper + requiring schedule) and its config; return the path map.

    ``mismatch`` writes the helper's declared checksum as :data:`WRONG_SHA` so its bytes on disk
    disagree with the Test Definition; ``setting`` is the config's `database.storeMismatchedFiles`.
    """
    defs = tmp_path / "defs"
    defs.mkdir(parents=True, exist_ok=True)
    (defs / "helper.lua").write_text(HELPER_SRC, encoding="utf-8")
    (defs / "main.tslua").write_text(SCHEDULE_SRC, encoding="utf-8")

    helper_sha = WRONG_SHA if mismatch else _sha(HELPER_SRC)
    atedef = {
        "format": 1,
        "mediaNumber": "REL-06C",
        "name": "Mismatch release",
        "version": "2.3.0",
        "files": [
            {"path": "helper.lua", "sha256": helper_sha},
            {"path": "main.tslua", "sha256": _sha(SCHEDULE_SRC)},
        ],
    }
    (defs / "demo.atedef").write_text(json.dumps(atedef), encoding="utf-8")

    config = {
        "format": 1,
        "station": {"name": "BENCH-03", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c10"},
        "database": {"type": "sqlite", "path": str(tmp_path / "ate.db"), "storeMismatchedFiles": setting},
        "reports": {"directory": str(tmp_path / "reports")},
        "testDefinitions": {"directories": [str(defs)]},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return {"config": config_path, "defs": defs, "db": tmp_path / "ate.db"}


def _finish(qapp, session: HeadlessRunSession) -> bool:
    """Pump the shared event loop until the run is FINISHED and its report job is done.

    The run-end PDF helper is a QProcess owned by the session: the per-test Qt teardown deletes the
    session, so a test that stops pumping while the helper is still running aborts Qt. Waiting for
    ``report_written`` (as the ticket 02 session tests do) is what keeps the process gone before the
    session is dropped."""
    written: list[tuple[str, str]] = []
    session.report_written.connect(lambda html, pdf: written.append((html, pdf)))
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if session._state == SessionState.FINISHED and written:
            return True
        time.sleep(0.005)
    raise TimeoutError("the run never reached FINISHED with its report written")


def _files(session: HeadlessRunSession) -> dict[str, dict]:
    """This run's `run_file` rows keyed by recorded path."""
    return {row["path"]: row for row in session.store.run_files(session.run_uuid)}


def _warn_lines(session: HeadlessRunSession) -> list[str]:
    """Every WARNING line the run logged about checksums."""
    return [line["text"] for line in session.store.log_lines(session.run_uuid) if "checksum mismatch" in line["text"]]


# -- the session's view of a mismatch -----------------------------------------


def test_pending_mismatches_names_the_bad_file(qapp, tmp_path):
    """The selected schedule's mismatches are (rel_path, expected, computed, media_number)."""
    env = _write_release(tmp_path)
    session = HeadlessRunSession(env["config"])
    session.select_schedule("Checksum schedule")
    assert session.pending_mismatches() == [("helper.lua", WRONG_SHA, _sha(HELPER_SRC), "REL-06C")]


def test_a_clean_schedule_has_no_pending_mismatches(qapp, tmp_path):
    """Nothing is flagged when every listed file matches its recorded checksum."""
    env = _write_release(tmp_path, mismatch=False)
    session = HeadlessRunSession(env["config"])
    session.select_schedule("Checksum schedule")
    assert session.pending_mismatches() == []


# -- the storage settings -------------------------------------------------------


def test_ask_with_true_stores_the_bytes(qapp, tmp_path):
    """`ask` plus a ticked box writes the mismatched file's raw bytes to run_file.content."""
    env = _write_release(tmp_path)
    session = HeadlessRunSession(env["config"])
    session.select_schedule("Checksum schedule")
    session.start(store_mismatched=True)
    assert _finish(qapp, session)
    helper = _files(session)["helper.lua"]
    assert helper["mismatch"] == 1
    assert helper["content"] == HELPER_SRC.encode("utf-8")


def test_ask_with_false_stores_nothing(qapp, tmp_path):
    """`ask` plus an unticked box still records the mismatch, but no bytes."""
    env = _write_release(tmp_path)
    session = HeadlessRunSession(env["config"])
    session.select_schedule("Checksum schedule")
    session.start(store_mismatched=False)
    assert _finish(qapp, session)
    helper = _files(session)["helper.lua"]
    assert helper["mismatch"] == 1 and helper["content"] is None


def test_ask_without_a_choice_raises(qapp, tmp_path):
    """`ask` with mismatches and no decision cannot start a run."""
    env = _write_release(tmp_path)
    session = HeadlessRunSession(env["config"])
    session.select_schedule("Checksum schedule")
    try:
        session.start()
    except ValueError as exc:
        assert "storeMismatchedFiles" in str(exc)
    else:
        raise AssertionError("start() accepted a missing choice while asking")


def test_always_stores_without_being_asked(qapp, tmp_path):
    """`always` stores the bytes whatever the caller passes — or passes nothing."""
    env = _write_release(tmp_path, setting="always")
    session = HeadlessRunSession(env["config"])
    session.select_schedule("Checksum schedule")
    session.start(store_mismatched=False)
    assert _finish(qapp, session)
    assert _files(session)["helper.lua"]["content"] == HELPER_SRC.encode("utf-8")


def test_never_stores_even_when_asked_to(qapp, tmp_path):
    """`never` stores nothing, even if the confirmation box was ticked."""
    env = _write_release(tmp_path, setting="never")
    session = HeadlessRunSession(env["config"])
    session.select_schedule("Checksum schedule")
    session.start(store_mismatched=True)
    assert _finish(qapp, session)
    assert _files(session)["helper.lua"]["content"] is None


def test_no_mismatch_needs_no_choice_and_stores_nothing(qapp, tmp_path):
    """A clean schedule starts with no confirmation and no stored content anywhere."""
    env = _write_release(tmp_path, mismatch=False)
    session = HeadlessRunSession(env["config"])
    session.select_schedule("Checksum schedule")
    session.start()
    assert _finish(qapp, session)
    assert all(row["content"] is None for row in _files(session).values())


def test_matching_files_and_ate_modules_never_store_content(qapp, tmp_path):
    """Only the mismatched file carries bytes; the schedule and `ate.` module do not."""
    env = _write_release(tmp_path)
    session = HeadlessRunSession(env["config"])
    session.select_schedule("Checksum schedule")
    session.start(store_mismatched=True)
    assert _finish(qapp, session)
    files = _files(session)
    assert files["main.tslua"]["content"] is None
    ate = [row for row in files.values() if row["kind"] == "ate-module"]
    assert ate and all(row["content"] is None for row in ate)


def test_the_warning_line_says_when_the_file_was_stored(qapp, tmp_path):
    """The run's mismatch WARNING names the file and, when stored, says so."""
    env = _write_release(tmp_path)
    session = HeadlessRunSession(env["config"])
    session.select_schedule("Checksum schedule")
    session.start(store_mismatched=True)
    assert _finish(qapp, session)
    line = _warn_lines(session)[0]
    assert "helper.lua" in line and "(file stored with this Test Run)" in line

    env2 = _write_release(tmp_path / "clean", setting="never")
    session2 = HeadlessRunSession(env2["config"])
    session2.select_schedule("Checksum schedule")
    session2.start()
    assert _finish(qapp, session2)
    assert "(file stored with this Test Run)" not in _warn_lines(session2)[0]


# -- the Run screen -------------------------------------------------------------


def test_the_schedule_list_flags_a_mismatched_schedule(qapp, tmp_path):
    """A mismatched schedule's row starts with ⚠; a clean one does not."""
    env = _write_release(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    texts = [screen.test_schedule_stage.schedule_list.item(i).text() for i in range(screen.test_schedule_stage.schedule_list.count())]
    assert any(t.startswith("⚠ ") and "Checksum schedule" in t for t in texts)

    clean = _write_release(tmp_path / "clean", mismatch=False)
    clean_session = HeadlessRunSession(clean["config"])
    clean_screen = StagedRunScreen(clean_session)
    texts = [clean_screen.test_schedule_stage.schedule_list.item(i).text() for i in range(clean_screen.test_schedule_stage.schedule_list.count())]
    assert all(not t.startswith("⚠ ") for t in texts)


def test_selecting_a_mismatched_schedule_shows_the_flag_line(qapp, tmp_path):
    """The line under the picker names the release and every mismatched file."""
    env = _write_release(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    list_widget = screen.test_schedule_stage.schedule_list
    item = next(list_widget.item(i) for i in range(list_widget.count()) if "Checksum schedule" in list_widget.item(i).text())
    list_widget.setCurrentItem(item)
    flag = screen.test_schedule_stage.mismatch_label.text()
    assert "Checksum mismatch" in flag and "helper.lua" in flag
    assert "The files on disk are not the released files" in flag


def test_start_with_a_mismatch_shows_the_confirmation(qapp, tmp_path):
    """The confirmation lists each file as `expected …, found …` and asks whether to store."""
    env = _write_release(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)
    asked: list[object] = []

    def choose(dialog):
        asked.append(dialog)
        dialog.ok_button.click()  # default: the box is ticked

    screen.confirm_choice = choose
    _select_and_run(screen, session)
    assert _finish(qapp, session)
    assert len(asked) == 1
    dialog = asked[0]
    text = dialog.file_lines.text()
    assert f"expected {WRONG_SHA}, found {_sha(HELPER_SRC)}" in text
    assert dialog.store_checkbox is not None and dialog.store_checkbox.isChecked()
    assert _files(session)["helper.lua"]["content"] == HELPER_SRC.encode("utf-8")


def test_cancel_on_the_confirmation_starts_nothing(qapp, tmp_path):
    """Cancel leaves the session idle and records no run."""
    env = _write_release(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)

    def choose(dialog):
        dialog.cancel_button.click()

    screen.confirm_choice = choose
    _select_and_run(screen, session)
    assert session._state == SessionState.SELECTING
    assert session.run_uuid is None


def test_ok_with_the_box_unticked_runs_without_storing(qapp, tmp_path):
    """Unticking the box at the confirmation is exactly `ask` answered false."""
    env = _write_release(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)

    def choose(dialog):
        dialog.store_checkbox.setChecked(False)
        dialog.ok_button.click()

    screen.confirm_choice = choose
    _select_and_run(screen, session)
    assert _finish(qapp, session)
    assert _files(session)["helper.lua"]["content"] is None


def test_a_clean_schedule_starts_without_the_confirmation(qapp, tmp_path):
    """No mismatch means Start goes straight to the run and no dialog is built."""
    env = _write_release(tmp_path, mismatch=False)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session)

    def choose(dialog):  # pragma: no cover - must never be reached
        raise AssertionError("the confirmation appeared without a mismatch")

    screen.confirm_choice = choose
    _select_and_run(screen, session)
    assert _finish(qapp, session)
    assert screen._confirm_dialog is None


def test_always_and_never_confirm_without_a_checkbox(qapp, tmp_path):
    """`always`/`never` still confirm on a mismatch, but only state what will be stored."""
    for setting, stored in (("always", True), ("never", False)):
        env = _write_release(tmp_path / setting, setting=setting)
        session = HeadlessRunSession(env["config"])
        screen = StagedRunScreen(session)
        seen: list[object] = []

        def choose(dialog, stored=stored):
            seen.append(dialog)
            dialog.ok_button.click()

        screen.confirm_choice = choose
        _select_and_run(screen, session)
        assert _finish(qapp, session)
        dialog = seen[0]
        assert dialog.store_checkbox is None
        assert ("will be stored" in dialog.store_note.text()) == stored
        assert (_files(session)["helper.lua"]["content"] is not None) == stored


def _select_and_run(screen: StagedRunScreen, session: HeadlessRunSession) -> None:
    """Select the demo schedule through the picker and press Start, as an operator would."""
    list_widget = screen.test_schedule_stage.schedule_list
    item = next(list_widget.item(i) for i in range(list_widget.count()) if "Checksum schedule" in list_widget.item(i).text())
    list_widget.setCurrentItem(item)
    screen._begin_run_from_selection()
    screen.steps_options_stage.run_button.click()


# -- the History screen --------------------------------------------------------


def test_history_shows_the_mismatched_paths(qapp, tmp_path):
    """The summary panel names each mismatched file instead of raising (packet 06c exposes 23b)."""
    env = _write_release(tmp_path)
    session = HeadlessRunSession(env["config"])
    session.select_schedule("Checksum schedule")
    session.start(store_mismatched=True)
    assert _finish(qapp, session)

    store = RunStore(env["db"])
    history = HistoryScreen(HistorySession(store))
    model = history.table_view.model()
    history.table_view.setCurrentIndex(model.index(0, 0))
    qapp.processEvents()
    assert "helper.lua" in history._mismatches.text()
