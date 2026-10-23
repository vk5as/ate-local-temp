"""Enabling and disabling steps before the run (ticket 11).

A step's default enable state comes from the author's ``:setEnable(b)``; the Operator can
toggle any step in stage 3 of the Run screen before Start. A step disabled at run start is
not executed and records ``NOTRUN`` with no Attempt, while a step enabled after being off by
default still runs. Both seams are covered here — the engine directly (its ``overrides`` map)
and through :class:`HeadlessRunSession`'s ``toggle_node`` command — against real SQLite and
real .tslua schedules; no mocks.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication

from ate.engine.run_engine import RunEngine
from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.storage.store import RunStore

from ._fixtures import MULTI, write_ate_env


def _drive(engine: RunEngine, app: QCoreApplication) -> None:
    """Start the worker and poll processEvents until it stops running."""
    engine.state_changed.connect(lambda status: None)  # swallow; we read state from the store
    engine.start()
    for _ in range(2000):
        if not engine.isRunning():
            break
        app.processEvents()


def _nodes(store: RunStore, run_uuid: str) -> list[dict]:
    conn = store._conn
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(
        "SELECT node_seq, step_name, default_enabled, enabled, result, attempt_count FROM node "
        "WHERE run_uuid=? ORDER BY node_seq", (run_uuid,)
    )]


def _by_name(nodes: list[dict], name: str) -> dict:
    """Return the single node with ``step_name == name``."""
    matches = [n for n in nodes if n["step_name"] == name]
    assert len(matches) == 1, f"expected one node named {name!r}, got {[n['step_name'] for n in matches]}"
    return matches[0]


def _wait_finished(session: HeadlessRunSession) -> bool:
    """Poll an application's processEvents until the session reaches FINISHED (or times out)."""
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    for _ in range(2000):
        app.processEvents()
        if session._state == SessionState.FINISHED:
            return True
    return False


# -- engine seam --------------------------------------------------------------


def test_disabled_step_by_default_records_notrun_without_attempt(app: QCoreApplication) -> None:
    """A step the author disabled (:setEnable(false)) is skipped and records NOTRUN, no Attempt."""
    store, row = _run_schedule(app, MULTI)

    assert row["result"] == "PASS"  # the two enabled steps pass; the disabled one does not fail it

    nodes = {n["node_seq"]: n for n in _nodes(store, row["run_uuid"])}
    by_name = {n["step_name"]: n for n in nodes.values()}

    # seq 0: enabled by default and runs PASS.
    first = by_name["one"]
    assert (first["default_enabled"], first["enabled"]) == (1, 1)
    assert first["result"] == "PASS" and first["attempt_count"] == 1

    # seq 1: disabled at the schedule's default -> NOTRUN with no Attempt.
    second = by_name["two"]
    assert (second["default_enabled"], second["enabled"]) == (0, 0)
    assert second["result"] == "NOTRUN" and second["attempt_count"] == 0

    # seq 2: enabled by default and runs PASS.
    third = by_name["three"]
    assert (third["default_enabled"], third["enabled"]) == (1, 1)
    assert third["result"] == "PASS" and third["attempt_count"] == 1

    attempts = store.attempts(row["run_uuid"])
    assert all(a["node_seq"] not in (1,) for a in attempts)


def test_override_enables_a_default_disabled_step(app: QCoreApplication) -> None:
    """Toggling seq 1 on runs the disabled step; its FAIL result fails the run."""
    store, row = _run_schedule(app, MULTI, overrides={1: True})

    assert row["result"] == "FAIL"  # the now-enabled step reports FAIL

    second = _by_name(_nodes(store, row["run_uuid"]), "two")
    # default stays what the author set (off); enabled reflects the Operator's toggle.
    assert (second["default_enabled"], second["enabled"]) == (0, 1)
    assert second["result"] == "FAIL" and second["attempt_count"] == 1


def test_override_disables_a_default_enabled_step(app: QCoreApplication) -> None:
    """Toggling an enabled step off records default=1/enabled=0 and NOTRUN."""
    store, row = _run_schedule(app, MULTI, overrides={2: False})

    assert row["result"] == "PASS"  # the disabled step is skipped; nothing fails

    third = _by_name(_nodes(store, row["run_uuid"]), "three")
    assert (third["default_enabled"], third["enabled"]) == (1, 0)
    assert third["result"] == "NOTRUN" and third["attempt_count"] == 0


# -- headless session seam ----------------------------------------------------


def test_disabled_step_runs_headless_and_records_notrun(tmp_path) -> None:
    """With no toggles, the default-disabled step is NOTRUN while its neighbours run."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    states = []
    session.state_changed.connect(states.append)

    session.set_operator("Ada")
    session.set_project("Power board")
    session.set_uut_type("PSU")
    session.set_uut_serial("PSU-001")
    session.select_schedule("Multi schedule")

    engine = session.start()
    assert _wait_finished(session)

    row = session.store.get_test_run(session.run_uuid)
    assert row is not None and row["result"] == "PASS"  # NOTRUN does not fail the run

    nodes = {n["node_seq"]: n for n in _nodes(session.store, session.run_uuid)}
    assert set(nodes) == {0, 1, 2}

    first = nodes[0]   # enabled by default, passes
    assert (first["default_enabled"], first["enabled"]) == (1, 1)
    assert first["result"] == "PASS" and first["attempt_count"] == 1

    second = nodes[1]  # disabled at the schedule's default
    assert (second["default_enabled"], second["enabled"]) == (0, 0)
    assert second["result"] == "NOTRUN" and second["attempt_count"] == 0
    # A NOTRUN step records no Attempt row.
    attempts = session.store.attempts(session.run_uuid)
    assert all(a["node_seq"] not in (1,) for a in attempts)

    third = nodes[2]  # enabled by default, passes
    assert (third["default_enabled"], third["enabled"]) == (1, 1)
    assert third["result"] == "PASS" and third["attempt_count"] == 1


def test_toggle_node_reenables_a_disabled_step(tmp_path) -> None:
    """Toggling the default-disabled step on before start runs it; its FAIL fails the run."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    session.select_schedule("Multi schedule")
    # seq 1 is "two", disabled by default. Toggle it on, then start.
    session.toggle_node(1, True)

    engine = session.start()
    assert _wait_finished(session)

    row = session.store.get_test_run(session.run_uuid)
    assert row is not None and row["result"] == "FAIL"  # the re-enabled step reports FAIL

    second = _by_name(_nodes(session.store, session.run_uuid), "two")
    assert (second["default_enabled"], second["enabled"]) == (0, 1)
    assert second["result"] == "FAIL" and second["attempt_count"] == 1


def test_toggle_node_before_start_disables_a_step(tmp_path) -> None:
    """Toggling an enabled step off before start records default=1/enabled=0 and NOTRUN."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    states = []
    session.state_changed.connect(states.append)

    session.set_operator("Ada")
    session.select_schedule("Multi schedule")
    # seq 2 is "three", enabled by default. Toggle it off before start.
    session.toggle_node(2, False)

    engine = session.start()
    assert _wait_finished(session)

    row = session.store.get_test_run(session.run_uuid)
    assert row is not None and row["result"] == "PASS"  # the disabled step is skipped

    third = _by_name(_nodes(session.store, session.run_uuid), "three")
    assert (third["default_enabled"], third["enabled"]) == (1, 0)
    assert third["result"] == "NOTRUN" and third["attempt_count"] == 0

    # Observable state still progressed through SELECTING -> STARTED.
    assert SessionState("SELECTING") in states
    assert SessionState("STARTED") in states


def test_toggle_node_rejects_bad_arguments(tmp_path) -> None:
    """A non-boolean enable or a non-int seq is a clear error, not a silent mis-set."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])

    with pytest.raises(Exception):  # noqa: B017 - any SessionError is acceptable here
        session.toggle_node(1, "yes")


def _run_schedule(app: QCoreApplication, source: str, *, overrides=None) -> tuple[RunStore, dict]:
    """Create a fresh store + engine for one schedule on disk; return (store, run_row)."""
    captured = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8")
        engine = RunEngine(store, str(schedule_path), overrides=overrides or {})
        engine.run_uuid_reported.connect(_capture)
        _drive(engine, app)

    assert captured.get("run_uuid") is not None, "engine never reported a run key"
    return store, store.get_test_run(captured["run_uuid"])
