"""Seam-1 tests for the History session and its RunStore query (ticket 23a).

These drive :class:`RunStore.query_runs` against a real SQLite file in a temp directory — no
mocks, no SQL strings. Twelve runs are inserted directly through ``insert_test_run`` /
``update_run_end`` with varied values; the tests then exercise sorting, every filter and paging
through the public interface only.

One setup detail: :meth:`RunStore.insert_test_run` stamps ``started_at`` from the run clock, so to
test date-range edges we give each run a controlled start time by updating it once through the store's
stdlib connection (:attr:`_conn`). That is fixture scaffolding — every query under test still goes
through :meth:`RunStore.query_runs`, never raw SQL.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import cmp_to_key

import pytest

from ate.history import HistorySession
from ate.storage import HISTORY_SORT_KEYS, RunStore, new_uuid7


# Twelve runs with distinct start times and varied columns so each sort key, filter and date edge
# has clear matches. Insertion order is the run's identity (r1..r12); ``started_at`` is stamped in
# the fixture below. PSU/Inverter/Meter each appear four times (good for tie-break tests), all four
# end reasons are present, and r11's serial carries both a % and an _ to exercise LIKE escaping.
RUN_SPECS = [
    {"started_at": "2026-01-01T00:00:00.000+00:00", "uut_type": "PSU", "uut_serial": "PSU-A1", "result": "PASS", "end_reason": "COMPLETED", "schedule_name": "Rail checks", "project": "Power board", "operator": "Ada"},
    {"started_at": "2026-01-01T03:00:00.000+00:00", "uut_type": "PSU", "uut_serial": "PSU-A2", "result": "FAIL", "end_reason": "STOPPED_ON_FAIL", "schedule_name": "Rail checks", "project": "Power board", "operator": "Ben"},
    {"started_at": "2026-01-01T06:00:00.000+00:00", "uut_type": "Inverter", "uut_serial": "INV-B1", "result": "PASS", "end_reason": "COMPLETED", "schedule_name": "MPPT tests", "project": "Solar array", "operator": "Ada"},
    {"started_at": "2026-01-01T09:00:00.000+00:00", "uut_type": "Inverter", "uut_serial": "INV-B2", "result": "FAIL", "end_reason": "ABORTED", "schedule_name": "MPPT tests", "project": "Solar array", "operator": "Ben"},
    {"started_at": "2026-01-01T12:00:00.000+00:00", "uut_type": "Meter", "uut_serial": "MTR-C1", "result": "PASS", "end_reason": "COMPLETED", "schedule_name": "Calibration", "project": "Utility grid", "operator": "Ada"},
    {"started_at": "2026-01-01T15:00:00.000+00:00", "uut_type": "Meter", "uut_serial": "MTR-C2", "result": "FAIL", "end_reason": "STOPPED_ON_FAIL", "schedule_name": "Calibration", "project": "Utility grid", "operator": "Ben"},
    {"started_at": "2026-01-01T18:00:00.000+00:00", "uut_type": "PSU", "uut_serial": "PSU-A3", "result": "PASS", "end_reason": "COMPLETED", "schedule_name": "Rail checks", "project": "Power board", "operator": "Ada"},
    {"started_at": "2026-01-01T21:00:00.000+00:00", "uut_type": "Inverter", "uut_serial": "INV-B3", "result": "FAIL", "end_reason": "INTERRUPTED", "schedule_name": "MPPT tests", "project": "Solar array", "operator": "Ben"},
    {"started_at": "2026-01-02T00:00:00.000+00:00", "uut_type": "Meter", "uut_serial": "MTR-C3", "result": "PASS", "end_reason": "COMPLETED", "schedule_name": "Calibration", "project": "Utility grid", "operator": "Ada"},
    {"started_at": "2026-01-02T03:00:00.000+00:00", "uut_type": "PSU", "uut_serial": "psu-a4", "result": "FAIL", "end_reason": "STOPPED_ON_FAIL", "schedule_name": "Rail checks", "project": "Power board", "operator": "Ben"},
    {"started_at": "2026-01-02T06:00:00.000+00:00", "uut_type": "Meter", "uut_serial": "MTR_%5", "result": "PASS", "end_reason": "COMPLETED", "schedule_name": "Calibration", "project": "Utility grid", "operator": "Ada"},
    {"started_at": "2026-01-02T09:00:00.000+00:00", "uut_type": "Inverter", "uut_serial": "INV-B4", "result": "PASS", "end_reason": "COMPLETED", "schedule_name": "MPPT tests", "project": "Solar array", "operator": "Ada"},
]


def _stamp_started_at(store: RunStore, run_uuid: str, iso: str) -> None:
    """Give one run a controlled start time (insert_test_run stamps the clock instead).

    ``RunStore`` stores started_at at millisecond precision (see :func:`_now_iso`), so the stamp is
    re-emitted in that same format; otherwise a second-precision value would compare against the
    store's own normalized filter bounds as if it were earlier, breaking date-range edges."""
    dt = datetime.fromisoformat(iso).astimezone(timezone.utc)
    stamp = dt.isoformat(timespec="milliseconds")
    store._conn.execute("UPDATE test_run SET started_at=? WHERE run_uuid=?", (stamp, run_uuid)).fetchall()


@pytest.fixture
def runs(tmp_path):
    """Insert twelve varied runs and return ``(store, rows)`` keyed by run label."""
    store = RunStore(tmp_path / "ate.db")
    # Our timestamp stamps go through the store's stdlib connection; put it in autocommit so a
    # stamp never leaves an open write transaction that blocks the engine's own writes.
    store._conn.isolation_level = None
    rows: dict[str, dict] = {}
    for spec in RUN_SPECS:
        ru = new_uuid7()
        store.insert_test_run({**spec, "run_uuid": ru})
        _stamp_started_at(store, ru, spec["started_at"])
        store.update_run_end(ru, result=spec["result"], end_reason=spec["end_reason"])
        rows[spec["uut_serial"]] = {"label": spec["uut_serial"], "run_uuid": ru, **spec}
    return store, rows


@pytest.fixture
def empty_store(tmp_path):
    """A fresh RunStore with no runs — for tests that only need the query interface."""
    return RunStore(tmp_path / "ate.db")


def _uuid(rows: dict[str, dict], key: str) -> str:
    """The run_uuid of the row labelled ``key`` (its serial)."""
    return rows[key]["run_uuid"]


def _cols(row: dict) -> tuple:
    return tuple(row[c] for c in HISTORY_SORT_KEYS)


def _expected_order(rows: list[dict], key: str, descending: bool) -> list[dict]:
    """Mirror SQL's (column <direction>, run_uuid ascending) total order over null-free data."""
    def cmp(a: dict, b: dict) -> int:
        av, bv = a[key], b[key]
        if av == bv:
            return -1 if a["run_uuid"] < b["run_uuid"] else (1 if a["run_uuid"] > b["run_uuid"] else 0)
        result = -1 if av < bv else 1
        return result if not descending else -result

    return sorted(rows, key=cmp_to_key(cmp))


def _in_window(row: dict, lo: str, hi: str) -> bool:
    """``lo <= started_at < hi`` by lexical ISO order (all stamps are millisecond precision)."""
    return lo <= row["started_at"] < hi


def _session(store: RunStore, page_size: int = 100) -> HistorySession:
    return HistorySession(store, page_size=page_size)


def test_newest_first_by_default(runs):
    """With no filters/sort the default is started_at descending (newest first)."""
    store, rows = runs
    got = [_cols(r) for r in _session(store).page(0)]
    assert got == [_cols(rows[spec["uut_serial"]]) for spec in reversed(RUN_SPECS)]


def test_sort_every_column_both_directions(runs):
    """Each of the eight sort keys works ascending and descending."""
    store, rows = runs
    labels = list(rows)  # r1..r12 in insertion order == run_uuid-ascending tie-break base
    for key in HISTORY_SORT_KEYS:
        asc = [_cols(r) for r in _session(store).page(0, sort_key=key, descending=False)]
        assert asc == [_cols(l) for l in _expected_order(list(rows.values()), key, False)], f"sort {key} asc"
        desc = [_cols(r) for r in _session(store).page(0, sort_key=key, descending=True)]
        assert desc == [_cols(l) for l in _expected_order(list(rows.values()), key, True)], f"sort {key} desc"


def test_unknown_sort_key_raises(empty_store):
    """A sort key outside the eight columns is rejected at both the store and session level."""
    with pytest.raises(ValueError):
        empty_store.query_runs(sort_key="bogus")
    with pytest.raises(ValueError):
        _session(empty_store).set_sort("station_name")


def test_filter_uut_type_exact(runs):
    """Exact UUT Type match returns only that type's runs."""
    store, rows = runs
    got = {r["run_uuid"] for r in _session(store).page(0, filters={"uut_type": "PSU"})}
    assert got == {_uuid(rows, s["uut_serial"]) for s in RUN_SPECS if s["uut_type"] == "PSU"}


def test_filter_serial_case_insensitive_substring(runs):
    """A serial substring matches regardless of case (lowercase 'psu' hits the PSU group)."""
    store, rows = runs
    got = {r["run_uuid"] for r in _session(store).page(0, filters={"uut_serial": "psu"})}
    assert got == {_uuid(rows, s["uut_serial"]) for s in RUN_SPECS if s["uut_type"] == "PSU"}


def test_filter_serial_escapes_percent_and_underscore(runs):
    """A literal % or _ in the search matches only a serial that really contains it."""
    store, rows = runs
    got_pct = {r["run_uuid"] for r in _session(store).page(0, filters={"uut_serial": "%"})}
    got_uscore = {r["run_uuid"] for r in _session(store).page(0, filters={"uut_serial": "_"})}
    assert got_pct == {_uuid(rows, "MTR_%5")}  # only "MTR_%5" contains a literal %
    assert got_uscore == {_uuid(rows, "MTR_%5")}  # and the same row is the only one with an underscore


def test_filter_result_exact(runs):
    """Exact result match splits PASS from FAIL."""
    store, rows = runs
    pass_runs = {r["run_uuid"] for r in _session(store).page(0, filters={"result": "PASS"})}
    fail_runs = {r["run_uuid"] for r in _session(store).page(0, filters={"result": "FAIL"})}
    assert pass_runs == {_uuid(rows, s["uut_serial"]) for s in RUN_SPECS if s["result"] == "PASS"}
    assert fail_runs == {_uuid(rows, s["uut_serial"]) for s in RUN_SPECS if s["result"] == "FAIL"}


def test_filter_end_reason_exact(runs):
    """Exact End Reason match returns only that reason's runs."""
    store, rows = runs
    completed = {r["run_uuid"] for r in _session(store).page(0, filters={"end_reason": "COMPLETED"})}
    interrupted = {r["run_uuid"] for r in _session(store).page(0, filters={"end_reason": "INTERRUPTED"})}
    assert completed == {_uuid(rows, s["uut_serial"]) for s in RUN_SPECS if s["end_reason"] == "COMPLETED"}
    assert interrupted == {_uuid(rows, s["uut_serial"]) for s in RUN_SPECS if s["end_reason"] == "INTERRUPTED"}


def test_filter_project_exact(runs):
    """Exact Project match returns only that project's runs."""
    store, rows = runs
    got = {r["run_uuid"] for r in _session(store).page(0, filters={"project": "Solar array"})}
    assert got == {_uuid(rows, s["uut_serial"]) for s in RUN_SPECS if s["project"] == "Solar array"}


def test_date_range_edges_inclusive_from_exclusive_to(runs):
    """date_from is inclusive and date_to is exclusive — for both datetime and ISO-string inputs."""
    store, rows = runs
    session = _session(store)

    # 06:00 <= started_at < 12:00 keeps the 06:00 (INV-B1) and 09:00 (INV-B2) runs only.
    expected_window = {"INV-B1", "INV-B2"}

    dt_filters = {"date_from": datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc), "date_to": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)}
    got_dt = {r["run_uuid"] for r in session.page(0, filters=dt_filters)}
    assert got_dt == {_uuid(rows, s) for s in expected_window}

    str_filters = {"date_from": "2026-01-01T06:00:00.000+00:00", "date_to": "2026-01-01T12:00:00.000+00:00"}
    got_str = {r["run_uuid"] for r in session.page(0, filters=str_filters)}
    assert got_str == {_uuid(rows, s) for s in expected_window}

    # Boundary behaviour on both edges: an inclusive-from keeps that exact run; an exclusive-to drops it.
    lo = "2026-01-01T06:00:00.000+00:00"  # INV-B1 starts exactly here (inclusive-from keeps it)
    hi = "2026-01-01T12:00:00.000+00:00"  # MTR-C1 starts exactly here (exclusive-to drops it)
    inclusive_from = {r["run_uuid"] for r in session.page(0, filters={"date_from": lo, "date_to": hi})}
    assert inclusive_from == {_uuid(rows, s["uut_serial"]) for s in RUN_SPECS if _in_window(rows[s["uut_serial"]], lo, hi)}

    exclusive_to = {r["run_uuid"] for r in session.page(0, filters={"date_from": "2026-01-01T00:00:00.000+00:00", "date_to": lo})}
    assert exclusive_to == {_uuid(rows, s["uut_serial"]) for s in RUN_SPECS if _in_window(rows[s["uut_serial"]], "2026-01-01T00:00:00.000+00:00", lo)}


def test_filters_combine_with_and(runs):
    """Two filters are ANDed, not ORed."""
    store, rows = runs
    got = {r["run_uuid"] for r in _session(store).page(0, filters={"project": "Power board", "result": "PASS"})}
    # Power board is the PSU group; PASS there is only the 00:00 and 18:00 runs.
    assert got == {_uuid(rows, s["uut_serial"]) for s in RUN_SPECS if s["project"] == "Power board" and s["result"] == "PASS"}


def test_paging_no_duplicates_or_gaps_page_size_5(runs):
    """With page size 5 over twelve rows, pages are [5, 5, 2] with no overlap or skipped row."""
    store, rows = runs
    session = _session(store, page_size=5)

    page0 = {r["run_uuid"] for r in session.page(0)}
    page1 = {r["run_uuid"] for r in session.page(1)}
    page2 = {r["run_uuid"] for r in session.page(2)}
    assert len(session.page(0)) == 5   # twelve rows, page size five -> [5, 5, 2]
    assert len(session.page(1)) == 5
    assert len(session.page(2)) == 2
    assert not (page0 & page1) and not (page0 & page2) and not (page1 & page2)  # no row on two pages
    assert page0 | page1 | page2 == {r["run_uuid"] for r in rows.values()}  # every run covered exactly once

    # next_page continues from the cursor; it matches an explicit page request.
    session2 = _session(store, page_size=5)
    first = session2.page(0)
    second = session2.next_page()
    assert [len(p) for p in (first, second)] == [5, 5]  # next_page() lands on page(1): rows 6..10
    # next_page() lands exactly where an explicit page(1) request would.
    assert {r["run_uuid"] for r in second} == {r["run_uuid"] for r in session2.page(1)}


def test_count_matches_filtered_total(runs):
    """count() returns the total under the current filters, independent of paging."""
    store, rows = runs
    session = _session(store)

    assert session.count() == 12
    session.set_filters({"project": "Solar array"})
    assert session.count() == 4   # filter is still active after count()


def test_changing_filter_resets_to_first_page(runs):
    """After moving to a later page, changing the filters returns to page zero."""
    store, rows = runs
    session = _session(store, page_size=5)

    session.page(0)
    later = session.next_page()  # cursor now at page 1 (the last five rows)
    assert len(later) == 5

    # Change filters: the cursor must reset to page 0 under the new filter.
    session.set_filters({"project": "Solar array"})
    assert session._page_index == 0
    # The cursor reset to page 0 under the new filter, so page zero holds the newest Solar run.
    after = session.page(0)
    assert after[0]["run_uuid"] == rows["INV-B4"]["run_uuid"]


def test_history_session_exposes_no_write_method():
    """The History session owns no delete/update/edit/insert — it only reads."""
    writey = ("delete", "update", "edit", "insert")
    cls = HistorySession
    for name in dir(cls):
        if name.startswith("_"):
            continue
        obj = getattr(cls, name)
        assert callable(obj), f"{name} is not a method"
        assert not any(name.lower().startswith(w) for w in writey), f"{name} looks like a write"


def test_query_runs_is_read_only(empty_store):
    """query_runs performs no writes: row count and an existing row survive repeated queries."""
    empty_store.insert_test_run({"run_uuid": new_uuid7(), "operator": "Ada", "project": "P"})
    before = len(empty_store.query_runs())
    # Pick a real run to verify it survives querying (next(iter(dict)) would yield a column name).
    row0 = empty_store.query_runs()[0]
    sample_uuid = row0["run_uuid"]

    session = _session(empty_store, page_size=3)
    for _ in range(5):
        session.page(0)
        session.next_page()
        session.set_filters({"project": "P"})
        session.set_sort("uut_type", descending=False)
        session.count()

    after = len(empty_store.query_runs())
    assert before == after  # no rows added or removed by querying
    row_before = empty_store.get_test_run(sample_uuid)
    row_after = empty_store.get_test_run(sample_uuid)
    assert row_before is not None and row_after is not None
    assert row_before == row_after  # the queried row was never touched
