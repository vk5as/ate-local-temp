"""Seam-1 tests for packet 23b's HistorySession display methods (summary, report_files, regenerate,
uut_filter, new_run_prefill).

Each method is driven against real SQLite runs recorded through RunEngine — the engine writes nodes
and report files to disk itself, so no mocks. The PDF helper is faked at the pdf_job seam by the
autouse conftest fixture (a trivial process that writes a stub PDF), so regenerate's async path stays
offscreen; a test overrides it with a failing script to exercise the error branch."""

from __future__ import annotations

from pathlib import Path

import time

import pytest

from PyQt6.QtCore import QCoreApplication

from ate.config import Config
from ate.engine.run_engine import RunEngine
from ate.history import HistorySession
from ate.report.html import write_report
from ate.storage.store import RunStore

from ._fixtures import DOC_FIELDS, _drive




def _pump_until(app: object, predicate, deadline: float = 10.0) -> bool:
    """Pump an application's event loop until ``predicate()`` holds, within a time bound.

    Queued QProcess signals only arrive while the loop is pumped; a fixed pump count fails
    under CPU load, so the wait is bounded by wall clock instead."""
    app = QCoreApplication.instance() or QCoreApplication([])
    until = time.monotonic() + deadline
    while time.monotonic() < until:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _record_open(tmp_path: Path, app: object, source: str = DOC_FIELDS) -> tuple[RunStore, Path, str]:
    """Drive one schedule to completion and return ``(store, reports_dir, run_uuid)``.

    Unlike the report tests' ``_record``, this keeps its temp directory open so the written report
    files survive for the HistorySession methods under test."""
    captured: dict[str, str] = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    tmp = tmp_path / "run"
    tmp.mkdir(exist_ok=True)
    store = RunStore(tmp / "ate.db")
    schedule_path = tmp / "schedule.tslua"
    schedule_path.write_text(source, encoding="utf-8")
    engine = RunEngine(
        store,
        str(schedule_path),
        operator="Alice",
        uut_type="PowerSupply",
        uut_serial="PS7",
        station_name="BENCH-03",
        utc_offset="+10:30",
    )
    engine.run_uuid_reported.connect(_capture)
    _drive(engine, app)

    assert captured.get("run_uuid") is not None, "engine never reported a run key"
    return store, tmp / "reports", captured["run_uuid"]


def _cfg(reports_dir: Path, **overrides) -> Config:
    """A report config pointing at ``reports_dir`` (path is required but unused by the display)."""
    return Config(path=Path("/tmp/ate-history-test"), reports_directory=reports_dir, **overrides)


def test_summary_counts_and_mismatches(tmp_path: Path, app: object):
    """A PASS run counts its one step; a mismatched file shows up in checksum_mismatches."""
    store, _dir, ru = _record_open(tmp_path, app, DOC_FIELDS)

    # One passing step -> PASS=1 and every other Result zero.
    s = HistorySession(store, config=_cfg(_dir))
    summary = s.summary(ru)
    assert summary["result_counts"]["PASS"] == 1
    for label in ("FAIL", "ERROR", "NOREPORT", "SKIPPED", "FORCED_NOTRUN", "NOTRUN"):
        assert summary["result_counts"][label] == 0

    # A file whose checksum does not match is reported; a matching one is not.
    store.insert_run_file(ru, path="good.tslua", kind="tslua", mismatch=False)
    store.insert_run_file(
        ru, path="bad.tslua", kind="tslua", expected_sha256="deadbeef", computed_sha256="cafebabe", mismatch=True
    )
    summary = s.summary(ru)
    mismatches = {f.get("path") for f in summary["checksum_mismatches"]}
    assert mismatches == {"bad.tslua"}


def test_summary_missing_run_raises(tmp_path: Path, app: object):
    """summary on an unrecorded run raises."""
    store, _dir, ru = _record_open(tmp_path, app)
    with pytest.raises(ValueError):
        HistorySession(store).summary("no-such-run")


def test_report_files_lists_written_reports_newest_first(tmp_path: Path, app: object):
    """After write_report the run's HTML (and a derived PDF) are listed, newest first."""
    store, _dir, ru = _record_open(tmp_path, app, DOC_FIELDS)
    config = _cfg(_dir)
    # Write an HTML report and its PDF so both files exist on disk.
    html_path = write_report(store, ru, config)
    from ate.report.pdf_job import start_pdf_job

    start_pdf_job(None, html_path=html_path, pdf_path=html_path.with_suffix(".pdf"))
    assert _pump_until(app, lambda: html_path.with_suffix(".pdf").exists()), "the PDF helper never finished"

    s = HistorySession(store, config=config)
    rp = s.report_files(ru)
    names = [str(p) for p in rp]
    assert len(rp) == 2
    # The PDF is written after the HTML at run end, so newest-first lists it first.
    assert Path(names[0]).suffix == ".pdf" and Path(names[1]).suffix == ".html"


def test_report_files_empty_when_directory_missing(tmp_path: Path, app: object):
    """No reports directory -> report_files returns nothing (Open report stays disabled)."""
    store, _dir, ru = _record_open(tmp_path, app)  # no write_report here
    s = HistorySession(store, config=_cfg(_dir))
    assert s.report_files(ru) == []


def test_report_files_needs_config(tmp_path: Path):
    """report_files without a config raises."""
    with pytest.raises(ValueError):
        HistorySession(RunStore(tmp_path / "ate.db")).report_files("run")


def test_regenerate_html_only_is_synchronous(tmp_path: Path, app: object):
    """regenerate(html=True) writes the HTML now and does not start a PDF job."""
    store, _dir, ru = _record_open(tmp_path, app, DOC_FIELDS)
    config = _cfg(_dir)
    done: list[str] = []

    s = HistorySession(store, config=config)
    result = s.regenerate(ru, html=True, pdf=False, on_done=done.append)

    assert "html" in result and Path(result["html"]).exists()
    assert result.get("pdf_started") is False  # no PDF requested -> nothing started
    assert done == []  # synchronous HTML path never fires on_done


def test_regenerate_html_and_pdf_fires_on_done_success(tmp_path: Path, app: object):
    """With the helper faked to write a valid PDF, regenerate starts it and reports success."""
    store, _dir, ru = _record_open(tmp_path, app, DOC_FIELDS)
    config = _cfg(_dir)

    s = HistorySession(store, config=config)
    events: list[str] = []
    done = lambda err: events.append("ok" if err is None else f"err:{err}")  # noqa: E731

    result = s.regenerate(ru, html=True, pdf=True, on_done=done)
    assert result["pdf_started"] is True
    assert _pump_until(app, lambda: events), "the PDF job never reported back"
    assert events == ["ok"]  # the stub helper wrote a non-empty PDF -> success


def test_regenerate_pdf_failure_reports_error(tmp_path: Path, app: object, monkeypatch):
    """A failing helper still writes HTML and reports the error through on_done."""
    store, _dir, ru = _record_open(tmp_path, app, DOC_FIELDS)
    config = _cfg(_dir)

    monkeypatch.setattr(
        "ate.report.pdf_job.pdf_helper_command",
        # The autouse fixture already stubs a success; override with a failing helper.
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("boom")),
    )

    s = HistorySession(store, config=config)
    events: list[str] = []
    done = lambda err: events.append(err)  # noqa: E731

    result = s.regenerate(ru, html=True, pdf=True, on_done=done)
    assert Path(result["html"]).exists()  # HTML written regardless
    assert _pump_until(app, lambda: events), "the failing PDF job never reported back"
    assert len(events) == 1 and events[0]  # one error string reported


def test_regenerate_needs_config(tmp_path: Path):
    """regenerate without a config raises."""
    with pytest.raises(ValueError):
        HistorySession(RunStore(tmp_path / "ate.db")).regenerate("run")


def test_uut_filter_selects_same_uut(tmp_path: Path, app: object):
    """uut_filter returns the run's UUT Type and serial for "Show all runs of this UUT"."""
    store, _dir, ru = _record_open(tmp_path, app, DOC_FIELDS)
    s = HistorySession(store)
    assert s.uut_filter(ru) == {"uut_type": "PowerSupply", "uut_serial": "PS7"}


def test_uut_filter_missing_run_raises(tmp_path: Path, app: object):
    store, _dir, ru = _record_open(tmp_path, app)
    with pytest.raises(ValueError):
        HistorySession(store).uut_filter("no-such-run")


def test_new_run_prefill_fills_uut_and_project(tmp_path: Path, app: object):
    """new_run_prefill fills UUT Type, serial and Project (from config.station_name); schedule empty."""
    store, _dir, ru = _record_open(tmp_path, app, DOC_FIELDS)
    s = HistorySession(store, config=_cfg(_dir, station_name="BENCH-03"))
    prefill = s.new_run_prefill(ru)

    assert prefill == {
        "uut_type": "PowerSupply",
        "uut_serial": "PS7",
        "project": "BENCH-03",
        "schedule_name": "",
    }


def test_new_run_prefill_missing_run_raises(tmp_path: Path, app: object):
    store, _dir, ru = _record_open(tmp_path, app)
    with pytest.raises(ValueError):
        HistorySession(store).new_run_prefill("no-such-run")
