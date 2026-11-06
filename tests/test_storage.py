"""Storage seam tests: a Test Run and its children round-trip through RunStore.

These drive the public :class:`RunStore` interface (insert + read back) against a
real SQLite file in a temp directory — no mocks, no SQL strings. They verify the
portable-type contract: UUIDv7 text keys, ISO-8601 UTC ms times, 0/1 booleans and
JSON inputs survive a write/read cycle unchanged.
"""

import json
import sqlite3

from ate.storage import RunStore, new_uuid7


def test_run_round_trips_through_store(tmp_path):
    """A run with one step and one Attempt is written and read back intact."""
    store = RunStore(tmp_path / "ate.db")

    # A fresh UUIDv7 key: byte 8 (string index 14) carries the version nibble, 7.
    run_uuid = new_uuid7()
    assert run_uuid[14] == "7"

    store.insert_test_run(
        {
            "run_uuid": run_uuid,
            "station_uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c10",
            "station_name": "BENCH-03",
            "operator": "Ada",
            "project": "Power board",
            "uut_type": "PSU",
            "uut_serial": "PSU-001",
            "ate_version": "1.2.3",
            "schedule_name": "Rail checks",
            "media_number": "REL-A",
            "stop_on_fail": True,
            "utc_offset": "+09:30",
        }
    )

    store.insert_node(
        run_uuid,
        0,
        kind="step",
        step_number="1.",
        step_name="LED colour",
        default_enabled=True,
        enabled=True,
        inputs={"[1]": {"desc": "colour", "type": "ENUM"}},
        result="PASS",
        attempt_count=1,
    )

    store.insert_attempt(
        run_uuid,
        0,
        1,
        pass_fail="PASS",
        msg="ok",
        actual="Green",
        expected="Green",
    )

    # Read back through the interface and assert every column survived.
    row = store.get_test_run(run_uuid)
    assert row is not None
    assert row["operator"] == "Ada"
    assert row["project"] == "Power board"
    assert row["uut_type"] == "PSU"
    assert row["utc_offset"] == "+09:30"
    # stop_on_fail stored as 0/1, not a bool.
    assert row["stop_on_fail"] == 1

    store._conn.row_factory = sqlite3.Row
    node = store._conn.execute(
        "SELECT * FROM node WHERE run_uuid=? AND node_seq=0", (run_uuid,)
    ).fetchone()
    assert json.loads(node["inputs"]) == {"[1]": {"desc": "colour", "type": "ENUM"}}
    # inputs column is JSON text.
    assert isinstance(node["step_name"], str)

    attempt = store._conn.execute(
        "SELECT * FROM attempt WHERE run_uuid=? AND node_seq=0 AND attempt_no=1", (run_uuid,)
    ).fetchone()
    assert attempt["pass_fail"] == "PASS"  # pass_fail


def test_update_run_end_stamps_times_and_result(tmp_path):
    """Closing a run stamps ended_at, result and end_reason."""
    store = RunStore(tmp_path / "ate.db")
    run_uuid = new_uuid7()
    store.insert_test_run(
        {
            "run_uuid": run_uuid,
            "operator": "Ada",
            "project": "P",
            "uut_type": "U",
            "uut_serial": "S",
        }
    )

    before = store.get_test_run(run_uuid)
    assert before["ended_at"] is None
    assert before["result"] is None

    store.update_run_end(run_uuid, result="FAIL", end_reason="STOPPED_ON_FAIL")

    after = store.get_test_run(run_uuid)
    assert after["ended_at"] is not None
    assert after["result"] == "FAIL"
    assert after["end_reason"] == "STOPPED_ON_FAIL"


def test_open_runs_lists_only_unfinished(tmp_path):
    """A run with no end time is reported open; a closed one is not."""
    store = RunStore(tmp_path / "ate.db")
    open_uuid = new_uuid7()
    closed_uuid = new_uuid7()

    store.insert_test_run({"run_uuid": open_uuid, "operator": "A", "project": "P"})
    store.insert_test_run({"run_uuid": closed_uuid, "operator": "B", "project": "Q"})
    store.update_run_end(closed_uuid, result="PASS", end_reason="COMPLETED")

    assert sorted(store.open_runs()) == [open_uuid]


def test_close_interrupted_runs_marks_fail(tmp_path):
    """An open run is closed as INTERRUPTED / FAIL when recovered at launch."""
    store = RunStore(tmp_path / "ate.db")
    run_uuid = new_uuid7()
    store.insert_test_run({"run_uuid": run_uuid, "operator": "A", "project": "P"})

    log_lines: list[str] = []
    closed = store.close_interrupted_runs(on_log=lambda level, source, text: log_lines.append(f"{level}:{text}"))

    assert closed == [run_uuid]
    row = store.get_test_run(run_uuid)
    assert row["ended_at"] is not None
    assert row["result"] == "FAIL"
    assert row["end_reason"] == "INTERRUPTED"
    # ATE logs that it did so.
    assert any("interrupted" in line for line in log_lines)


def test_log_lines_batch_round_trip_through_store(tmp_path) -> None:
    """A batch of log lines is written and read back with level, source and ownership."""
    store = RunStore(tmp_path / "ate.db")
    run_uuid = new_uuid7()

    # One step's worth of schedule-sourced lines (owned by that Attempt), plus a run-level
    # line owned by the run as a whole. Inserted in one batch keyed by the same seq.
    store.insert_log_lines(
        run_uuid,
        0,
        [
            {"level": "info", "source": "schedule", "text": "step started"},
            {"level": "warning", "source": "schedule", "text": "value out of range"},
            {"level": "error", "source": "device", "text": "port lost"},
        ],
        node_seq=0,
        attempt_no=1,
    )
    # A run-level line (no owner) recorded in a separate batch.
    store.insert_log_lines(
        run_uuid,
        1,
        [{"level": "debug", "source": "ate", "text": "nothing reported"}],
    )

    lines = store.log_lines(run_uuid)
    # Each line gets its own unique seq (the batch holds several), numbered monotonically.
    assert [line["seq"] for line in lines] == [0, 1, 2, 3]

    # The step's three owned lines are identified by ownership, not by a shared seq.
    step_lines = [ln for ln in lines if ln["node_seq"] == 0 and ln["attempt_no"] == 1]
    assert [(ln["level"], ln["source"], ln["text"]) for ln in step_lines] == [
        ("INFO", "schedule", "step started"),
        ("WARNING", "schedule", "value out of range"),
        ("ERROR", "device", "port lost"),
    ]
    # Levels are normalised to upper case; ownership is expressed through the foreign keys.
    assert step_lines[0]["node_seq"] == 0 and step_lines[0]["attempt_no"] == 1
    assert step_lines[0]["action_seq"] is None
    run_line = lines[3]
    assert (run_line["level"], run_line["source"], run_line["text"]) == ("DEBUG", "ate", "nothing reported")
    assert run_line["node_seq"] is None and run_line["attempt_no"] is None and run_line["action_seq"] is None


def test_log_lines_after_returns_only_the_new_rows(tmp_path) -> None:
    """`log_lines_after(run, seq)` reads only the rows recorded after ``seq`` (packet 40a review).

    The live Log pane fetches the new rows rather than the whole log on every notification, so this
    window has to be exact: nothing already drawn, nothing not yet written, nothing from another run."""
    store = RunStore(tmp_path / "ate.db")
    run_uuid = new_uuid7()
    store.insert_log_lines(run_uuid, 0, [{"level": "info", "source": "schedule", "text": f"line {i}"} for i in range(3)])
    assert [ln["seq"] for ln in store.log_lines_after(run_uuid, -1)] == [0, 1, 2]  # nothing drawn yet: every row

    store.insert_log_lines(run_uuid, 0, [{"level": "info", "source": "schedule", "text": "line 3"},
                                         {"level": "debug", "source": "schedule", "text": "line 4"}])
    tail = store.log_lines_after(run_uuid, 2)
    assert [(ln["seq"], ln["text"]) for ln in tail] == [(3, "line 3"), (4, "line 4")]
    assert store.log_lines_after(run_uuid, 4) == []  # nothing newer than the last row drawn

    other = new_uuid7()
    store.insert_log_lines(other, 0, [{"level": "info", "source": "ate", "text": "another run"}])
    assert store.log_lines_after(run_uuid, 4) == []  # another run's rows never leak into the window


def test_log_lines_tail_reads_only_the_newest_rows_per_level(tmp_path) -> None:
    """`log_lines_tail(run, limit, min_level)` is the recovery read: the newest rows, oldest first.

    Packet 40a fix 2, item 2: a pane that (re)draws reads at most ``limit`` rows of its own level
    filter, never the whole log, and `log_line_counts` says how many older rows that left out."""
    store = RunStore(tmp_path / "ate.db")
    run_uuid = new_uuid7()
    levels = ["INFO", "DEBUG", "INFO", "WARNING", "DEBUG", "ERROR"]
    store.insert_log_lines(run_uuid, 0, [{"level": lv, "source": "schedule", "text": f"row {i}"} for i, lv in enumerate(levels)])
    other = new_uuid7()
    store.insert_log_lines(other, 0, [{"level": "INFO", "source": "ate", "text": "another run"}])

    assert [ln["seq"] for ln in store.log_lines_tail(run_uuid, 3)] == [3, 4, 5]  # the newest three, oldest first
    assert [ln["seq"] for ln in store.log_lines_tail(run_uuid, 99)] == [0, 1, 2, 3, 4, 5]
    assert store.log_lines_tail(run_uuid, 0) == []
    assert [ln["seq"] for ln in store.log_lines_tail(run_uuid, 2, min_level="INFO")] == [3, 5]  # INFO and above
    assert [ln["seq"] for ln in store.log_lines_tail(run_uuid, 99, min_level="WARNING")] == [3, 5]
    assert all(ln["run_uuid"] == run_uuid for ln in store.log_lines_tail(run_uuid, 99))
    assert store.log_lines_tail(other, 99)[0]["text"] == "another run"

    # Rows older than seq 3 that the INFO-and-above tail leaves out: seq 0 and 2 (INFO) — 2 of them.
    counts = store.log_line_counts(run_uuid, 3, min_level="INFO")
    assert sum(row["count"] for row in counts) == 2
    assert sum(row["count"] for row in store.log_line_counts(run_uuid, 3)) == 3  # DEBUG too


def test_log_lines_after_can_leave_debug_rows_out_in_the_store(tmp_path) -> None:
    """`log_lines_after(run, seq, min_level="INFO")` never hands a DEBUG row to the Log tab (packet 40a fix 2).

    The Log tab keeps its own read cursor; DEBUG-only rows must not come back to it on every refresh."""
    store = RunStore(tmp_path / "ate.db")
    run_uuid = new_uuid7()
    store.insert_log_lines(run_uuid, 0, [{"level": "INFO", "source": "schedule", "text": "info 0"}])
    store.insert_log_lines(run_uuid, 0, [{"level": "DEBUG", "source": "schedule", "text": f"debug {i}"} for i in range(5)])
    rows = store.log_lines_after(run_uuid, 0, min_level="INFO")
    assert rows == [] and all(ln["level"] != "DEBUG" for ln in rows)
    store.insert_log_lines(run_uuid, 0, [{"level": "WARNING", "source": "ate", "text": "warn"}])
    assert [ln["text"] for ln in store.log_lines_after(run_uuid, 0, min_level="INFO")] == ["warn"]
    assert len(store.log_lines_after(run_uuid, 0)) == 6  # the Debug window's read keeps every level


def test_media_release_and_run_file_round_trip_through_store(tmp_path) -> None:
    """A Media Release (seq 0) and its file-used rows survive a write/read cycle."""
    store = RunStore(tmp_path / "ate.db")
    run_uuid = new_uuid7()

    # The owning release, then one clean file and one mismatched file. media_release and run_file
    # each number their rows from 0 on a counter of their own, so (run_uuid, seq) is unique within
    # every table without shifting the log-line numbering steps rely on.
    store.insert_media_release(
        run_uuid,
        media_number="REL-A",
        name="Demo release",
        version="1.0.0",
        atedef_sha256="deadbeef" * 8,
    )
    store.insert_run_file(run_uuid, path="load.tslua", kind="tslua", expected_sha256="aaa", computed_sha256="aaa")
    store.insert_run_file(
        run_uuid, path="helper.lua", kind="lua", media_number="REL-A",
        expected_sha256="bbb", computed_sha256="ccc", mismatch=True,
    )

    releases = store.media_releases(run_uuid)
    assert len(releases) == 1
    rel = releases[0]
    assert (rel["media_number"], rel["name"], rel["version"], rel["atedef_sha256"]) == ("REL-A", "Demo release", "1.0.0", "deadbeef" * 8)
    assert rel["seq"] == 0

    files = store.run_files(run_uuid)
    assert [(f["path"], f["kind"], f["expected_sha256"], f["computed_sha256"], f["mismatch"]) for f in files] == [
        ("load.tslua", "tslua", "aaa", "aaa", 0),
        ("helper.lua", "lua", "bbb", "ccc", 1),
    ]
    # run_file rows are numbered from 0 within their own counter (monotonic in load order).
    assert [f["seq"] for f in files] == [0, 1]

    # Reading an unknown run yields nothing.
    assert store.media_releases("nope") == []
    assert store.run_files("nope") == []


def test_log_lines_for_unknown_run_is_empty(tmp_path) -> None:
    """Reading log lines for a run that was never recorded returns nothing."""
    store = RunStore(tmp_path / "ate.db")
    assert store.log_lines("does-not-exist") == []
