"""Ticket 10 checkboxes c/d/e — every Step Input value recorded through a real run.

Each test drives a genuine ``.tslua`` schedule on disk through a :class:`RunEngine` worker and its
:class:`OperatorInput` controller, then reads the recorded rows straight from the SQLite store (no
mocks). The driver connects one ``input_asked`` handler: it supplies/clicks each announced input from
this thread while the worker waits, so a blocked ``get_raw`` wakes and a POLL is clicked while its
step runs.

c POLL returns nil until its button is clicked, then that text; recorded when clicked.
d Inputs are announced per-step (one ``input_asked`` per declared input under its step); a blocking
  supply is typed and recorded as an ``input_value`` row.
e Every type is one ``input_value`` row — CHECKBOX stored as JSON — with the declarations themselves
  persisted on the step's node in schedule format.
"""

import json
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ate.engine.operator_input import OperatorInput
from ate.engine.run_engine import RunEngine, RunStatus
from ate.storage.store import RunStore

from ._fixtures import (
    ALL_TYPES_INPUTS,
    BLOCKING_INPUT,
    POLL_INPUT,
    INPUTS_ALL,
    INPUTS_BLOCKING,
    INPUTS_POLL,
    _default_input_value,
)


def _rows(conn, run_uuid: str) -> dict[int, tuple[str, str]]:
    """Return ``{input_index: (type, value_text)}`` recorded for a run."""
    conn.row_factory = sqlite3.Row
    return {
        row["input_index"]: (row["type"], row["value"])
        for row in conn.execute(
            "SELECT input_index, type, value FROM input_value WHERE run_uuid=?", (run_uuid,)
        ).fetchall()
    }


def _drive(app, engine: RunEngine, op: OperatorInput, strategy=INPUTS_BLOCKING, values_by_index=None):
    """Start an unstarted ``engine`` and pump until FINISHED with a single ``input_asked`` handler.

    Returns ``(asked, run_uuid)`` where ``asked`` is the list of ``(seq, attempt_no, index)`` tuples
    every step announced while asking for its inputs — so a test can assert the per-step announcement
    contract from the one connection that also supplies/clicks (a second handler would double-supply)."""
    asked: list[tuple[int, int, int]] = []
    sink: dict[str, Any] = {}

    def _on_input_asked(seq, attempt_no, index):
        key = index - 1  # 0-based position into this step's declarations
        if strategy == INPUTS_POLL:
            op.click_poll(node_seq=seq, attempt_no=attempt_no, index=key, text="clicked")
        else:
            value = values_by_index[key] if values_by_index is not None else _default_input_value(key)
            op.supply_input(key, value)
        asked.append((seq, attempt_no, index))

    engine.input_asked.connect(_on_input_asked)
    engine.run_uuid_reported.connect(lambda u: sink.__setitem__("u", u))
    finished = threading.Event()
    engine.state_changed.connect(lambda status: finished.set() if status == RunStatus.FINISHED else None)
    engine.start()
    deadline = time.time() + 10.0
    while not finished.is_set():
        app.processEvents()
        if time.time() > deadline:
            raise TimeoutError("run did not reach FINISHED within 10 s")
    return asked, sink["u"]


def _write_and_run(app, op, schedule_text, strategy=INPUTS_BLOCKING, values_by_index=None):
    """Write ``schedule_text`` to a fresh store-backed engine and drive it; return (store, run_uuid)."""
    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(schedule_text, encoding="utf-8")
        engine = RunEngine(store, str(schedule_path), operator_input=op)
        asked, run_uuid = _drive(app, engine, op, strategy, values_by_index=values_by_index)
        return store, run_uuid


# --- c: POLL ------------------------------------------------------------------


def test_poll_recorded_when_clicked(app):
    """A POLL input returns nil until its button is clicked, then that text — recorded as a row."""
    op = OperatorInput()
    store, run_uuid = _write_and_run(
        app, op, POLL_INPUT, strategy=INPUTS_POLL  # clicks the single POLL button "clicked"
    )
    rows = {idx: (typ, val) for idx, (typ, val) in _rows(store._conn, run_uuid).items()}
    assert rows == {1: ("POLL", "clicked")}


# --- d: per-step announcement + blocking value recorded ------------------------


def test_blocking_input_announced_once_per_step_and_recorded(app):
    """One declared input is announced once under its step; the supplied value is typed and stored."""
    op = OperatorInput()
    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "blocking.tslua"
        schedule_path.write_text(BLOCKING_INPUT, encoding="utf-8")
        engine = RunEngine(store, str(schedule_path), operator_input=op)
        asked, run_uuid = _drive(app, engine, op, INPUTS_BLOCKING, values_by_index={0: 42})
    # input_asked fired exactly once for the single declared input under its step (node_seq 0).
    assert asked == [(0, 1, 1)]
    rows = {idx: (typ, val) for idx, (typ, val) in _rows(store._conn, run_uuid).items()}
    assert rows == {1: ("INTEGER", "42")}


# --- e: every type recorded + declarations stored on the node ------------------


def test_every_input_type_recorded_with_checkbox_as_json(app):
    """All six types are one row each (CHECKBOX as JSON); the step's declarations are stored on its node."""
    op = OperatorInput()
    values_by_index = {
        0: 42,          # INTEGER -> "42"
        1: 3.14,        # FLOAT -> "3.14"
        2: "COM7",      # STRING -> "COM7"
        3: "Slow",      # RADIOBUT ENUM -> "Slow"
        4: ["A", "B", "C"],  # CHECKBOX -> stored as JSON ["A","B","C"]
        5: "typed",     # PROMPT -> "typed"
    }
    store, run_uuid = _write_and_run(
        app, op, ALL_TYPES_INPUTS, strategy=INPUTS_ALL, values_by_index=values_by_index
    )
    rows = {idx: (typ, val) for idx, (typ, val) in _rows(store._conn, run_uuid).items()}
    assert rows == {
        1: ("INTEGER", "42"),
        2: ("FLOAT", "3.14"),
        3: ("STRING", "COM7"),
        4: ("ENUM", "Slow"),
        5: ("ENUM", '["A", "B", "C"]'),  # CHECKBOX recorded as a JSON array of labels
        6: ("PROMPT", "typed"),
    }

    node = store._conn.execute(
        "SELECT inputs FROM node WHERE run_uuid=? AND step_number='1.'", (run_uuid,)
    ).fetchone()
    decls = json.loads(node["inputs"])
    assert [d["type"] for d in decls] == ["INTEGER", "FLOAT", "STRING", "ENUM", "ENUM", "PROMPT"]
    checkbox = next(d for d in decls if d["type"] == "ENUM" and d["input"] == "CHECKBOX")
    assert checkbox["enum"] == ["A", "B", "C"]
    # tooltip and orient apply to every type (test-schedule-format.md), so the line-edit
    # declarations keep them; packet 29b review found INTEGER/FLOAT dropping them at validation.
    assert decls[0]["tooltip"] == "Supply voltage in volts" and decls[0]["orient"] == "VERT"
    assert decls[1]["tooltip"] == "Ammeter reading in amps" and decls[1]["orient"] == "HORIZ"


def test_poll_reset_between_runs_clears_stale_pending(app):
    """reset() at run start clears a shared controller's stale blocking value, so a new run reads fresh."""
    op = OperatorInput()  # one controller reused across two runs to exercise reset-at-run-start

    def _run_block(value):
        store, run_uuid = _write_and_run(
            app, op, BLOCKING_INPUT, strategy=INPUTS_BLOCKING, values_by_index={0: value}
        )
        rows = {idx: (typ, val) for idx, (typ, val) in _rows(store._conn, run_uuid).items()}
        return rows

    assert _run_block(42) == {1: ("INTEGER", "42")}  # run 1 records the supplied value
    # Run 2 reuses the same controller; without reset it would return the stale 42.
    assert _run_block(99) == {1: ("INTEGER", "99")}
