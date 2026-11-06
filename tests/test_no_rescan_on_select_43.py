"""Packet 43 — selecting a schedule does not rescan the catalogue.

GitHub issue #3: clicking a schedule in the three-pane layout rescanned every Test Definition
directory every time. The chain that did it (see the packet's Comments): a row click runs
``SchedulePicker._on_selection``, which calls ``session.select_schedule`` (a full
``find_definitions`` walk), and its ``selection_changed`` signal makes the layout preview the pick
with ``load_schedule_steps`` — a second walk; a filter keystroke fired both paths per rebuilt row.

Now the picker's ``refresh`` — first load and the Rescan button — is the only whole-catalogue
scan: it hands the definitions it read to the session (``set_catalog_scan``), and every selection,
step preview, mismatch flag, release label and broken row after that is served from that held
scan. The run start still reads the selected schedule's current file — the one schedule, never a
catalogue rescan — so deleting a picked schedule's ``.tslua`` still refuses the start with
"not available" (packet 41).
"""

import hashlib
import json
import time
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel

import ate.catalog as catalog
import ate.run_session.headless as headless
import ate.ui.run_widgets as run_widgets
from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.ui.run_widgets import BROKEN_MARKER
from ate.ui.staged_run_screen import StagedRunScreen
from ate.ui.three_pane_run_screen import ThreePaneRunScreen

from ._fixtures import _append_file, fill_operator_fields, write_ate_env

LAYOUTS = ("staged", "three-pane")

#: A schedule that raises while it loads, so its picker row is broken (packet 05b).
RAISING_SRC = "return {\n"

EXTRA_SRC = """\
return ScheduleParams("Extra schedule", "a schedule added after the first load")
  :setSchedule({
    StepParams("extra"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "extra ok") end),
  })
"""


@pytest.fixture
def scan_spy(monkeypatch):
    """Count every whole-catalogue scan by wrapping the scan function where it is called.

    `find_definitions` is the scan: it walks the Test Definition directories, reads every
    `.atedef` and computes every checksum. The picker and the session hold their own references
    to it, so all three are patched to the same counter."""
    original = catalog.find_definitions
    calls = []

    def spy(*directories):
        calls.append(tuple(str(d) for d in directories))
        return original(*directories)

    monkeypatch.setattr(catalog, "find_definitions", spy)
    monkeypatch.setattr(run_widgets, "find_definitions", spy)
    monkeypatch.setattr(headless, "find_definitions", spy)
    return calls


def _set_store_setting(config_path: Path, setting: str) -> None:
    """Rewrite `database.storeMismatchedFiles` in the config before the session reads it."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["database"]["storeMismatchedFiles"] = setting
    config_path.write_text(json.dumps(config), encoding="utf-8", newline="\n")


def _build(qapp, tmp_path, layout, setting: str | None = None):
    """A screen on a real session, picker populated, with the scan spy in place.

    ``setting`` writes `database.storeMismatchedFiles` before the session reads the config."""
    env = write_ate_env(tmp_path)
    if setting:
        _set_store_setting(env["config"], setting)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session) if layout == "staged" else ThreePaneRunScreen(session)
    qapp.processEvents()
    return screen, env


def _pick(screen, name: str):
    """Select the picker row that stands for a schedule, as an Operator would."""
    rows = screen.picker.schedule_list
    item = next(rows.item(i) for i in range(rows.count()) if rows.item(i).data(Qt.ItemDataRole.UserRole) == name)
    rows.setCurrentItem(item)


def _picked_names(screen) -> list[str]:
    rows = screen.picker.schedule_list
    return [rows.item(i).data(Qt.ItemDataRole.UserRole) for i in range(rows.count())]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_selecting_three_schedules_triggers_no_extra_scans(qapp, tmp_path, layout, scan_spy):
    """The rc4 complaint, measured: picks scan nothing after the first load.

    The layout scans once when it loads (rows, mismatch flags, release labels). Selecting three
    schedules, re-selecting one and switching back scans nothing, and a filter change — which
    re-renders the list from the same held scan — scans nothing either."""
    screen, _env = _build(qapp, tmp_path, layout)
    at_load = len(scan_spy)
    assert at_load, "the first load must scan the catalogue to populate the picker"

    _pick(screen, "Passing schedule")
    _pick(screen, "Failing schedule")
    _pick(screen, "Logging schedule")
    _pick(screen, "Logging schedule")  # re-select the same row
    _pick(screen, "Passing schedule")  # switch between schedules
    screen.picker.filter_edit.setText("pass")
    qapp.processEvents()

    assert screen.session.selected_schedule == "Passing schedule"
    assert len(scan_spy) == at_load, f"selecting schedules rescanned the catalogue {len(scan_spy) - at_load} times ({layout})"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_rescan_picks_up_a_newly_added_schedule_file(qapp, tmp_path, layout, scan_spy):
    """Rescan is the explicit action that finds new files: without it a new schedule stays unseen."""
    screen, env = _build(qapp, tmp_path, layout)
    _pick(screen, "Passing schedule")
    qapp.processEvents()
    at_load = len(scan_spy)

    _append_file(env["defs_dir"], "extra.tslua", EXTRA_SRC)
    assert "Extra schedule" not in _picked_names(screen), "selecting schedules went to disk and found the new file"

    screen.picker.rescan_button.click()
    qapp.processEvents()
    after_rescan = len(scan_spy)
    assert after_rescan > at_load, "Rescan did not scan the catalogue"
    assert "Extra schedule" in _picked_names(screen)

    _pick(screen, "Extra schedule")
    qapp.processEvents()
    assert screen.session.selected_schedule == "Extra schedule"
    assert screen.session.state == SessionState.SELECTING
    # The pick itself scans nothing more.
    assert len(scan_spy) == after_rescan, "picking the new schedule rescanned"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_broken_row_stays_broken_after_selecting(qapp, tmp_path, layout, scan_spy):
    """The held scan keeps the picker's rows correct (packet 05b): picks scan nothing and the
    broken row — a schedule that raises while loading — is still broken after selecting others."""
    screen, env = _build(qapp, tmp_path, layout)
    _append_file(env["defs_dir"], "raising.tslua", RAISING_SRC)
    screen.picker.rescan_button.click()
    qapp.processEvents()

    def broken_rows() -> list:
        rows = screen.picker.schedule_list
        return [rows.item(i) for i in range(rows.count()) if rows.item(i).text().startswith(BROKEN_MARKER)]

    assert broken_rows(), "the raising schedule should list as a broken row"
    _pick(screen, "Passing schedule")
    _pick(screen, "Failing schedule")
    _pick(screen, "Passing schedule")
    qapp.processEvents()

    assert broken_rows(), "selecting schedules cleared the broken row from the held scan"
    assert screen.session.selected_schedule == "Passing schedule"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_mismatch_flag_still_shows_after_selection(qapp, tmp_path, layout, scan_spy):
    """The ⚠ flag comes from the held scan: tamper a schedule before the first load, pick it,
    and the flag shows without any rescan — and stays showing after switching away and back."""
    env = write_ate_env(tmp_path)
    tampered = env["defs_dir"] / "passing.tslua"
    tampered.write_bytes(tampered.read_bytes() + b"\n-- tampered after the Test Definition was written\n")
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session) if layout == "staged" else ThreePaneRunScreen(session)
    qapp.processEvents()
    at_load = len(scan_spy)

    def flag_shown() -> bool:
        if layout == "staged":
            return "⚠ Checksum mismatch" in screen.picker.mismatch_label.text() and "passing.tslua" in screen.picker.mismatch_label.text()
        rows = screen.picker.schedule_list
        item = next(rows.item(i) for i in range(rows.count()) if rows.item(i).data(Qt.ItemDataRole.UserRole) == "Passing schedule")
        widget = rows.itemWidget(item)
        return any(label.text().startswith("⚠") for label in widget.findChildren(QLabel))

    _pick(screen, "Passing schedule")
    qapp.processEvents()
    assert flag_shown(), f"the selected mismatched schedule shows no flag ({layout})"
    assert session.pending_mismatches(), "the held scan reports no mismatch for the tampered schedule"

    _pick(screen, "Failing schedule")
    _pick(screen, "Passing schedule")
    qapp.processEvents()
    assert flag_shown(), f"the mismatch flag vanished after switching schedules ({layout})"
    assert len(scan_spy) == at_load, "showing the mismatch flag rescanned the catalogue"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_start_refuses_a_deleted_schedule_without_rescanning(qapp, tmp_path, layout, scan_spy):
    """Packet 41's refusal probe, scan-counted: delete the picked schedule's file and Start
    refuses with "not available" — the run-start read is the one schedule, not a rescan."""
    screen, env = _build(qapp, tmp_path, layout)
    _pick(screen, "Passing schedule")
    fill_operator_fields(screen)
    qapp.processEvents()
    at_load = len(scan_spy)

    (env["defs_dir"] / "passing.tslua").unlink()
    screen.start_button.click()
    qapp.processEvents()

    refusal = screen.error_label.text()
    assert "Passing schedule" in refusal and "not available" in refusal, refusal
    assert screen.session.run_uuid is None, "the refused start opened a run"
    assert len(scan_spy) == at_load, "refusing the start rescanned the whole catalogue"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_restored_schedule_starts_without_rescanning(qapp, tmp_path, layout, scan_spy):
    """The start-time read is current: put the deleted file back and the same selection starts —
    with no rescan — because Start reads the one picked schedule's file, not the catalogue."""
    screen, env = _build(qapp, tmp_path, layout)
    _pick(screen, "Passing schedule")
    fill_operator_fields(screen)
    qapp.processEvents()
    at_load = len(scan_spy)

    path = env["defs_dir"] / "passing.tslua"
    original = path.read_text(encoding="utf-8")
    path.unlink()
    screen.start_button.click()
    qapp.processEvents()
    assert "not available" in screen.error_label.text(), "the deleted schedule was not refused"

    path.write_text(original, encoding="utf-8", newline="\n")
    screen.start_button.click()
    qapp.processEvents()
    assert screen.session.state == SessionState.STARTED, "the restored schedule did not start"
    assert len(scan_spy) == at_load, "the run start rescanned the catalogue"

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if screen.session.state == SessionState.FINISHED:
            break
        time.sleep(0.005)
    screen.session.close()


# -- the run-start decision is made on the files as they are now (packet 43 review fix) --

#: Appended to a schedule's `.tslua` on disk so its bytes disagree with its Test Definition. No
#: Rescan is involved: the held scan still holds the checksum taken before the edit.
EDIT = b"\n-- edited on disk after the Rescan\n"


def _tamper(path: Path) -> bytes:
    """Change a schedule file on disk and return the bytes it had before."""
    original = path.read_bytes()
    path.write_bytes(original + EDIT)
    return original


def _run_rows(session) -> dict[str, dict]:
    """This run's `run_file` rows keyed by recorded path."""
    return {row["path"]: row for row in session.store.run_files(session.run_uuid)}


def _pump_until_finished(qapp, session) -> bool:
    """Pump the loop until the run is FINISHED and its report written, so the helper process is gone."""
    written: list[tuple[str, str]] = []
    session.report_written.connect(lambda html, pdf: written.append((html, pdf)))
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if session.state == SessionState.FINISHED and written:
            return True
        time.sleep(0.005)
    raise TimeoutError("the run never reached FINISHED with its report written")


@pytest.mark.parametrize("layout", LAYOUTS)
def test_start_confirms_a_schedule_edited_after_the_scan(qapp, tmp_path, layout, scan_spy):
    """Spec lines 24/79/96/232: Start checks the schedule it is about to run, as it is now.

    The held scan made the confirmation invisible for a `.tslua` edited after the last Rescan: Start
    went straight into the run and the engine still logged the mismatch."""
    screen, env = _build(qapp, tmp_path, layout)
    _pick(screen, "Passing schedule")
    fill_operator_fields(screen)
    qapp.processEvents()
    asked: list[object] = []

    def choose(dialog):
        asked.append(dialog)
        dialog.ok_button.click()

    screen.confirm_choice = choose
    _tamper(env["defs_dir"] / "passing.tslua")
    at_load = len(scan_spy)

    screen.start_button.click()
    qapp.processEvents()

    assert asked, "Start skipped the mismatch confirmation for a schedule edited after the scan"
    assert "passing.tslua" in asked[0].file_lines.text(), asked[0].file_lines.text()
    assert _pump_until_finished(qapp, screen.session), "the confirmed run never finished"
    assert len(scan_spy) == at_load, "reading the selected schedule rescanned the whole catalogue"
    screen.session.close()


@pytest.mark.parametrize("layout", LAYOUTS)
def test_an_edit_after_the_scan_is_stored_when_the_setting_is_always(qapp, tmp_path, layout, scan_spy):
    """`storeMismatchedFiles=always` stores the changed bytes — which needs Start to see the mismatch."""
    screen, env = _build(qapp, tmp_path, layout, setting="always")
    _pick(screen, "Passing schedule")
    fill_operator_fields(screen)
    qapp.processEvents()
    at_load = len(scan_spy)

    _tamper(env["defs_dir"] / "passing.tslua")
    screen.start_button.click()
    qapp.processEvents()
    assert _pump_until_finished(qapp, screen.session)

    row = _run_rows(screen.session)["passing.tslua"]
    assert row["mismatch"] == 1, "the run did not record the edited file as a mismatch"
    assert row["content"] == (env["defs_dir"] / "passing.tslua").read_bytes(), (
        f"always stored nothing for passing.tslua: the Start decision used the scan's checksums ({layout})"
    )
    assert len(scan_spy) == at_load, "the store decision rescanned the whole catalogue"
    screen.session.close()


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_file_fixed_on_disk_after_a_stale_scan_needs_no_confirmation(qapp, tmp_path, layout, scan_spy):
    """The reverse: the scan saw a mismatch, the Operator put the released bytes back, Start asks nothing."""
    env = write_ate_env(tmp_path)
    path = env["defs_dir"] / "passing.tslua"
    original = _tamper(path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session) if layout == "staged" else ThreePaneRunScreen(session)
    qapp.processEvents()
    _pick(screen, "Passing schedule")
    fill_operator_fields(screen)
    qapp.processEvents()
    at_load = len(scan_spy)
    path.write_bytes(original)  # fixed on disk, no Rescan

    asked: list[object] = []

    def choose(dialog):  # must not be reached: nothing mismatches any more
        asked.append(dialog)
        dialog.ok_button.click()

    screen.confirm_choice = choose
    screen.start_button.click()
    qapp.processEvents()

    assert not asked, "Start confirmed a file the Operator had already fixed on disk"
    assert _pump_until_finished(qapp, session)
    assert all(row["mismatch"] == 0 for row in _run_rows(session).values()), (
        "the run recorded a mismatch for a file that now matches its Test Definition"
    )
    assert len(scan_spy) == at_load, "Start rescanned the whole catalogue"
    session.close()


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_run_flags_the_same_files_the_confirmation_lists(qapp, tmp_path, layout, scan_spy):
    """With the default `ask`, the confirmation's file list and the run's mismatch flags are one set."""
    screen, env = _build(qapp, tmp_path, layout)  # storeMismatchedFiles defaults to "ask"
    _pick(screen, "Passing schedule")
    fill_operator_fields(screen)
    qapp.processEvents()
    asked: list[object] = []

    def choose(dialog):
        asked.append(dialog)
        dialog.ok_button.click()

    screen.confirm_choice = choose
    _tamper(env["defs_dir"] / "passing.tslua")
    at_load = len(scan_spy)
    screen.start_button.click()
    qapp.processEvents()
    assert _pump_until_finished(qapp, screen.session)

    confirmed = {rel_path for rel_path, _expected, _computed, _media in asked[0].mismatches} if asked else set()
    flagged = {path for path, row in _run_rows(screen.session).items() if row["mismatch"]}
    assert confirmed == flagged, f"the confirmation listed {sorted(confirmed)}, the run flagged {sorted(flagged)}"
    assert len(scan_spy) == at_load, "Start rescanned the whole catalogue"
    screen.session.close()


# -- one selection per click, one walk per Rescan (packet 43 review fixes) --


@pytest.mark.parametrize("layout", LAYOUTS)
def test_one_click_on_a_row_makes_one_selection(qapp, tmp_path, layout):
    """A row click fires both `itemSelectionChanged` and `currentItemChanged`, and the picker ran
    its handler for both: the session got the same schedule twice and the layout loaded its steps
    twice (packet 43 review). One click is one selection."""
    screen, _env = _build(qapp, tmp_path, layout)
    session = screen.session
    select = session.select_schedule
    picks: list[str] = []
    session.select_schedule = lambda name: (picks.append(name), select(name))[1]
    rows = screen.picker.schedule_list

    _pick(screen, "Passing schedule")
    qapp.processEvents()
    assert picks == ["Passing schedule"], f"one click made {len(picks)} selections: {picks}"

    _pick(screen, "Failing schedule")
    qapp.processEvents()
    assert picks == ["Passing schedule", "Failing schedule"], f"switching rows made {len(picks)} selections: {picks}"

    # Keyboard selection is still a selection, and still exactly one per move.
    rows.setCurrentItem(rows.item(0))
    qapp.processEvents()
    before = len(picks)
    QTest.keyClick(rows, Qt.Key.Key_Down, delay=1)
    qapp.processEvents()
    assert len(picks) == before + 1, f"the arrow key made {len(picks) - before} selections"
    assert session.selected_schedule == screen.picker.selected_name()
    assert session.selected_schedule, "the keyboard pick never reached the session"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_dropping_and_repicking_the_same_row_selects_it_again(qapp, tmp_path, layout):
    """The one-selection-per-click guard also swallowed a deliberate re-selection (packet 43 fix 2).

    After a run ends the step boxes stay locked until the schedule is selected again (spec 37), which
    an Operator gets by picking the row again. Two changes — the selection dropped, the row picked —
    are two selections, not one already-handled state: the session is handed the schedule again and
    returns to SELECTING. Re-clicking an already-selected row still changes nothing."""
    screen, _env = _build(qapp, tmp_path, layout)
    session = screen.session
    select = session.select_schedule
    picks: list[str] = []
    session.select_schedule = lambda name: (picks.append(name), select(name))[1]
    rows = screen.picker.schedule_list
    item = next(rows.item(i) for i in range(rows.count()) if rows.item(i).data(Qt.ItemDataRole.UserRole) == "Passing schedule")

    rows.setCurrentItem(item)
    qapp.processEvents()
    assert picks == ["Passing schedule"], f"one click made {len(picks)} selections: {picks}"

    rows.setCurrentItem(item)  # the row is already the selection: nothing changes, nothing re-selects
    qapp.processEvents()
    assert picks == ["Passing schedule"], f"re-clicking the selected row selected it {len(picks)} times"

    rows.selectionModel().clearSelection()
    rows.setCurrentItem(item)
    qapp.processEvents()
    assert picks[-1] == "Passing schedule" and len(picks) > 1, f"re-picking the same row made no selection: {picks}"
    assert session.selected_schedule == "Passing schedule"
    assert session.state == SessionState.SELECTING, f"the re-selection left the session {session.state}"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_rescan_walks_the_catalogue_once(qapp, tmp_path, layout, scan_spy):
    """Rescan reads the Test Definition directories once: the picker takes its rows and its ⚠ flags
    from the scan it just made instead of asking the catalog for each of them separately."""
    screen, env = _build(qapp, tmp_path, layout)
    _append_file(env["defs_dir"], "extra.tslua", EXTRA_SRC)
    before = len(scan_spy)

    screen.picker.rescan_button.click()
    qapp.processEvents()

    scans = len(scan_spy) - before
    assert scans == 1, f"a Rescan walked the catalogue {scans} times ({layout})"
    assert "Extra schedule" in _picked_names(screen), "the single scan lost the new schedule's row"
# -- re-reading the selected release and its chain, release by release (packet 43 review fix 2) --

#: A dependency release's module, and the schedule that requires it.
CHILD_SRC = "return { name = 'child', value = 7 }\n"
PARENT_SRC = (
    'local c = require("child")\n'
    'return ScheduleParams("Chain schedule", "a schedule that requires a dependency release module")\n'
    "  :setSchedule({\n"
    '    StepParams("check"):setStepNo("1.")\n'
    '      :setAction(function() ATE.report(Result.PASS, "child value " .. c.value) end),\n'
    "  })\n"
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_chain(tmp_path: Path, *, same_dir: bool = False) -> dict[str, Path]:
    """Write a parent Media Release and the release it depends on, in one or two directories.

    ``same_dir`` puts both `.atedef` files in the same Test Definition directory, which is the case
    the Start-time re-read used to collapse into a single release. Returns the path map."""
    defs = tmp_path / "defs"
    defs.mkdir(parents=True, exist_ok=True)
    child_dir = defs if same_dir else defs / "child"
    child_dir.mkdir(parents=True, exist_ok=True)
    (defs / "parent.tslua").write_text(PARENT_SRC, encoding="utf-8", newline="\n")
    (child_dir / "child.lua").write_text(CHILD_SRC, encoding="utf-8", newline="\n")
    (defs / "parent.atedef").write_text(
        json.dumps(
            {
                "format": 1,
                "mediaNumber": "REL-P",
                "name": "Parent release",
                "version": "1.0.0",
                "depends": [{"name": "Child release", "version": "^1"}],
                "files": [{"path": "parent.tslua", "sha256": _sha(PARENT_SRC)}],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    (child_dir / "child.atedef").write_text(
        json.dumps(
            {
                "format": 1,
                "mediaNumber": "REL-C",
                "name": "Child release",
                "version": "1.0.0",
                "files": [{"path": "child.lua", "sha256": _sha(CHILD_SRC)}],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    config = {
        "format": 1,
        "station": {"name": "BENCH-03", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c10"},
        "defaultProject": "Power board",
        "database": {"type": "sqlite", "path": str(tmp_path / "ate.db"), "storeMismatchedFiles": "ask"},
        "reports": {"directory": str(tmp_path / "reports")},
        "testDefinitions": {"directories": [str(defs)]},
        # A log directory of its own, so the AppLog never lands in the platform data directory.
        "log": {"directory": str(tmp_path / "logs")},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8", newline="\n")
    return {"config": config_path, "defs": defs, "child": child_dir / "child.lua"}


def _build_screen(qapp, config_path: Path, layout: str):
    """A screen on a session over the given config, with the picker populated."""
    session = HeadlessRunSession(config_path)
    screen = StagedRunScreen(session) if layout == "staged" else ThreePaneRunScreen(session)
    qapp.processEvents()
    return screen


def _asked_start(screen, qapp) -> list[object]:
    """Wire a confirming choice into the screen and return the dialogs Start showed."""
    asked: list[object] = []

    def choose(dialog):
        asked.append(dialog)
        dialog.ok_button.click()

    screen.confirm_choice = choose
    screen.start_button.click()
    qapp.processEvents()
    return asked


def test_refresh_definition_reads_each_atedef_in_one_directory(tmp_path):
    """Two Test Definitions in one directory are two releases, not one (packet 43 review fix 2).

    `refresh_definition` cached each rebuilt release by `Definition.path`, which is the directory
    holding the `.atedef`. A dependency sharing that directory was never read again: the chain carried
    a copy of the release above it, its files were never re-hashed, and the engine could not match the
    module it loaded to any release."""
    env = _write_chain(tmp_path, same_dir=True)
    scan = catalog.find_definitions(env["defs"])
    parent = next(d for d in scan if d.media_number == "REL-P")

    refreshed = catalog.refresh_definition(parent)
    chain = tuple(m.media_number for m in refreshed.chain)
    assert chain == ("REL-P", "REL-C"), f"the chain rebuilt as {chain}: the dependency became a copy of the parent"
    files = {m.media_number: tuple(f.rel_path for f in m.files) for m in refreshed.chain}
    assert files == {"REL-P": ("parent.tslua",), "REL-C": ("child.lua",)}, files


def test_refresh_definition_rehashes_every_release_in_the_chain(tmp_path):
    """The chain rebuild re-reads each release's own `.atedef` and re-hashes its own files."""
    env = _write_chain(tmp_path)  # parent and child in separate directories
    scan = catalog.find_definitions(env["defs"])
    parent = next(d for d in scan if d.media_number == "REL-P")
    env["child"].write_text(CHILD_SRC + "-- edited after the scan\n", encoding="utf-8", newline="\n")

    refreshed = catalog.refresh_definition(parent)
    assert len(refreshed.chain) == 2, "refreshing dropped the dependency from the chain"
    child = refreshed.chain[1]
    assert child.media_number == "REL-C"
    entry = child.files[0]
    assert entry.rel_path == "child.lua"
    assert entry.computed_sha256 == _sha(env["child"].read_text(encoding="utf-8")), (
        "the dependency's file was not re-hashed from the bytes on disk"
    )
    assert entry.expected_sha256 == _sha(CHILD_SRC), "refreshing rewrote the checksum the Test Definition declares"


def test_refresh_definition_keeps_a_deleted_atedef_list_rehashed(tmp_path):
    """A `.atedef` that has gone is not a Start refusal: its file list is kept and only re-hashed.

    :func:`read_definition` raises on a file it cannot read at all, so `refresh_definition` falls back
    to re-hashing the list the scan already held. That is the deleted-schedule behaviour packet 43
    keeps: the run still starts on the bytes on disk."""
    env = _write_chain(tmp_path)
    scan = catalog.find_definitions(env["defs"])
    parent = next(d for d in scan if d.media_number == "REL-P")
    (env["defs"] / "parent.atedef").unlink()
    env["child"].write_text(CHILD_SRC + "-- edited after the scan\n", encoding="utf-8", newline="\n")

    refreshed = catalog.refresh_definition(parent)
    assert refreshed.broken is None, f"the re-read of a missing .atedef broke the release: {refreshed.broken}"
    assert len(refreshed.chain) == 2, "the dependency vanished from the chain with the unreadable .atedef"
    assert refreshed.files[0].rel_path == "parent.tslua"
    assert refreshed.chain[1].media_number == "REL-C"


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("same_dir", (True, False), ids=("two in one directory", "one per directory"))
def test_start_confirms_a_dependency_module_edited_after_the_scan(qapp, tmp_path, layout, same_dir):
    """A module owned by a dependency release is confirmed, naming its owner (packet 43 review fixes).

    The parent's schedule `require`s the child release's module. Editing that module after the scan
    used to go unnoticed: Start saw only the parent's files, asked nothing, and the run recorded no
    row for the dependency's file at all — most clearly when both `.atedef` files shared a directory."""
    env = _write_chain(tmp_path, same_dir=same_dir)
    screen = _build_screen(qapp, env["config"], layout)
    _pick(screen, "Chain schedule")
    fill_operator_fields(screen)
    qapp.processEvents()
    env["child"].write_text(CHILD_SRC + "-- edited after the Rescan\n", encoding="utf-8", newline="\n")

    asked = _asked_start(screen, qapp)
    assert asked, "Start skipped the confirmation for a dependency module edited after the scan"
    owner = next(m for m in asked[0].mismatches if m[0] == "child.lua")
    assert owner[3] == "REL-C", f"child.lua was flagged against release {owner[3]}"
    assert _pump_until_finished(qapp, screen.session), "the confirmed run never finished"
    row = _run_rows(screen.session)["child.lua"]
    assert row["mismatch"] == 1, "the run did not record the dependency's module as a mismatch"
    screen.session.close()
# -- a Test Definition broken after the scan is refused at Start (packet 43 review fix 3) --


@pytest.mark.parametrize("layout", LAYOUTS)
def test_start_refuses_a_definition_broken_after_the_scan(qapp, tmp_path, layout, scan_spy):
    """Corrupting the picked schedule's `.atedef` after the scan must stop the run, not silence it.

    `read_definition` answers invalid JSON with a definition that is broken and has no files, so the
    re-read's fallback never fired and Start ran the schedule with nothing verified: PASS, no Media
    Release row, no `run_file` rows. Start refuses instead, and the packet 41 refusal line names the
    release (packet 43 review fix 3)."""
    screen, env = _build(qapp, tmp_path, layout)
    _pick(screen, "Passing schedule")
    fill_operator_fields(screen)
    qapp.processEvents()
    at_load = len(scan_spy)
    (env["defs_dir"] / "demo.atedef").write_text("{ this is not JSON", encoding="utf-8", newline="\n")

    screen.start_button.click()
    qapp.processEvents()

    refusal = screen.error_label.text()
    assert "demo.atedef" in refusal and "invalid JSON" in refusal, f"Start accepted a broken Test Definition: {refusal}"
    assert screen.session.run_uuid is None, "the refused start opened a run"
    assert len(scan_spy) == at_load, "refusing the start rescanned the whole catalogue"
    screen.session.close()


@pytest.mark.parametrize("layout", LAYOUTS)
def test_start_refuses_a_dependency_broken_after_the_scan(qapp, tmp_path, layout):
    """A release the picked schedule depends on has to be readable too, and the refusal names it.

    The dependency's `.atedef` is valid at the scan and an unsupported format afterwards; the chain
    rebuild then hands back a member with no files, so the run would load a module with no checksum
    behind it."""
    env = _write_chain(tmp_path)
    screen = _build_screen(qapp, env["config"], layout)
    _pick(screen, "Chain schedule")
    fill_operator_fields(screen)
    qapp.processEvents()
    (env["defs"] / "child" / "child.atedef").write_text(
        json.dumps({"format": 2, "mediaNumber": "REL-C", "name": "Child release", "version": "1.0.0"}),
        encoding="utf-8",
        newline="\n",
    )

    screen.start_button.click()
    qapp.processEvents()

    refusal = screen.error_label.text()
    assert "REL-C" in refusal and "broken" in refusal, f"Start accepted a broken dependency: {refusal}"
    assert screen.session.run_uuid is None, "the refused start opened a run"
    screen.session.close()


@pytest.mark.parametrize("layout", LAYOUTS)
def test_start_still_runs_when_the_atedef_is_deleted(qapp, tmp_path, layout):
    """A `.atedef` that has gone is still the clean fallback: the scan's file list is re-hashed.

    Packet 43 keeps this behaviour — a missing definition file is not a broken one — so the refusal for
    a corrupted `.atedef` must not sweep it up. The run starts and records its own schedule's checksums."""
    env = _write_chain(tmp_path)
    screen = _build_screen(qapp, env["config"], layout)
    _pick(screen, "Chain schedule")
    fill_operator_fields(screen)
    qapp.processEvents()
    (env["defs"] / "parent.atedef").unlink()

    asked = _asked_start(screen, qapp)
    assert not asked, "a run with no mismatch asked for a confirmation"
    assert _pump_until_finished(qapp, screen.session), "the run the deleted .atedef should not have stopped did not finish"
    rows = _run_rows(screen.session)
    assert rows["parent.tslua"]["mismatch"] == 0
    assert rows["child.lua"]["media_number"] == "REL-C"
    screen.session.close()
# -- the files a run loads, checked at Start (packet 43 review, coverage) --

#: A Media Release with one helper module and one schedule that requires it.
HELPER_SRC = "return { name = 'helper', value = 42 }\n"
MODULE_SRC = (
    'local h = require("helper")\n'
    'return ScheduleParams("Module schedule", "a schedule that requires a release module")\n'
    "  :setSchedule({\n"
    '    StepParams("check"):setStepNo("1.")\n'
    '      :setAction(function() ATE.report(Result.PASS, "helper " .. h.value) end),\n'
    "  })\n"
)


def _write_module_release(tmp_path: Path) -> dict[str, Path]:
    """Write one Media Release holding a `.lua` module and the schedule that requires it."""
    defs = tmp_path / "defs"
    defs.mkdir(parents=True, exist_ok=True)
    (defs / "helper.lua").write_text(HELPER_SRC, encoding="utf-8", newline="\n")
    (defs / "main.tslua").write_text(MODULE_SRC, encoding="utf-8", newline="\n")
    (defs / "demo.atedef").write_text(
        json.dumps(
            {
                "format": 1,
                "mediaNumber": "REL-M",
                "name": "Module release",
                "version": "1.0.0",
                "files": [
                    {"path": "helper.lua", "sha256": _sha(HELPER_SRC)},
                    {"path": "main.tslua", "sha256": _sha(MODULE_SRC)},
                ],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    config = {
        "format": 1,
        "station": {"name": "BENCH-03", "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c10"},
        "defaultProject": "Power board",
        "database": {"type": "sqlite", "path": str(tmp_path / "ate.db"), "storeMismatchedFiles": "ask"},
        "reports": {"directory": str(tmp_path / "reports")},
        "testDefinitions": {"directories": [str(defs)]},
        "log": {"directory": str(tmp_path / "logs")},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8", newline="\n")
    return {"config": config_path, "defs": defs, "helper": defs / "helper.lua"}


@pytest.mark.parametrize("layout", LAYOUTS)
def test_start_confirms_a_release_module_edited_after_the_scan(qapp, tmp_path, layout):
    """A `.lua` module of the selected release is read at Start like its own `.tslua`.

    The picker's ⚠ flag comes from the held scan, but the confirmation and the run's `run_file` rows
    come from the bytes on disk: editing the module after the Rescan is confirmed, and the run records
    it as a mismatch belonging to the release that owns it."""
    env = _write_module_release(tmp_path)
    screen = _build_screen(qapp, env["config"], layout)
    _pick(screen, "Module schedule")
    fill_operator_fields(screen)
    qapp.processEvents()
    env["helper"].write_text(HELPER_SRC + "-- edited after the Rescan\n", encoding="utf-8", newline="\n")

    asked = _asked_start(screen, qapp)
    assert asked, "Start skipped the confirmation for a release module edited after the scan"
    assert "helper.lua" in asked[0].file_lines.text(), asked[0].file_lines.text()
    assert asked[0].mismatches[0][3] == "REL-M", asked[0].mismatches
    assert _pump_until_finished(qapp, screen.session), "the confirmed run never finished"
    row = _run_rows(screen.session)["helper.lua"]
    assert row["mismatch"] == 1, "the run recorded the edited module without flagging it"
    assert row["media_number"] == "REL-M"
    screen.session.close()


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_rescan_rows_and_flags_match_a_fresh_scan(qapp, tmp_path, layout, scan_spy):
    """One walk feeds the picker everything, and it matches what a fresh scan of the same dirs gives.

    The Rescan derives its rows and ⚠ flags from the scan it just made (packet 43 review fix 3). Read
    against `list_schedule_rows` and `list_schedule_mismatches` over the same directories — the view a
    layout would have built with three walks — the held scan gives the identical rows and flags."""
    screen, env = _build(qapp, tmp_path, layout)
    _append_file(env["defs_dir"], "raising.tslua", RAISING_SRC)
    _tamper(env["defs_dir"] / "passing.tslua")
    screen.picker.rescan_button.click()
    qapp.processEvents()

    held_rows, held_flags = list(screen.picker._rows), dict(screen.picker.mismatch_flags)
    fresh_rows = catalog.list_schedule_rows(env["defs_dir"])
    fresh_flags = catalog.list_schedule_mismatches(env["defs_dir"])

    assert held_rows == fresh_rows, f"the held rows differ from a fresh scan: {held_rows} vs {fresh_rows}"
    assert held_flags == fresh_flags, f"the held flags differ from a fresh scan: {held_flags} vs {fresh_flags}"
    assert held_flags, "nothing mismatched, so this test compared two empty maps"
    screen.session.close()
