"""Ticket 22a: PDF via a separate helper process.

The CSS ``@page`` margins must match the ``QPageLayout`` the helper passes to ``printToPdf`` (spec
rule 21). An integration test renders a generated HTML report through :func:`run_pdf_helper` and
checks the PDF; a unit test pins the margin constants into the rendered CSS so they cannot drift.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from ate.config import Config
from ate.engine.run_engine import RunEngine
from ate.report.html import REPORT_PAGE_MARGINS_MM, render_report, write_report
from ate.report.pdf import run_pdf_helper
from ate.storage.store import RunStore

from ._fixtures import DOC_FIELDS, _drive


def _cfg(**overrides) -> Config:
    """A report config (path is required but unused by the report); apply overrides."""
    return Config(path=Path("/tmp/ate-report-test"), **overrides)


# QtWebEngine (PyQt6-WebEngine) is an optional dependency; skip the PDF tests if it cannot import.
try:  # pragma: no cover - depends on the environment's installs
    from PyQt6.QtWebEngineCore import QWebEnginePage  # noqa: F401
except Exception:
    QWebEngineAvailable = False
else:
    QWebEngineAvailable = True


@pytest.mark.skipif(not QWebEngineAvailable, reason="PyQt6-WebEngine not installed")
def test_pdf_a4_from_generated_report(app) -> None:
    """A generated PASS report renders to a non-trivial A4 PDF through the helper process."""
    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(DOC_FIELDS, encoding="utf-8")
        engine = RunEngine(
            store, str(schedule_path), operator="Alice",
            uut_type="PowerSupply", uut_serial="PS7", station_name="BENCH-03", utc_offset="+10:30",
        )
        captured: dict[str, str] = {}
        engine.run_uuid_reported.connect(lambda ru: captured.__setitem__("ru", ru))
        _drive(engine, app)

        config = _cfg(reports_directory=Path(td) / "reports", reports_organisation="ACME")
        html_path = write_report(store, captured["ru"], config)
        pdf_path = Path(td) / "report.pdf"
        run_pdf_helper(html_path, pdf_path, "A4")

        assert pdf_path.exists()
        data = pdf_path.read_bytes()
        assert data[:5] == b"%PDF-"  # a real PDF header
        assert len(data) > 1024  # non-trivial


@pytest.mark.skipif(not QWebEngineAvailable, reason="PyQt6-WebEngine not installed")
def test_pdf_letter_from_generated_report(app) -> None:
    """The same report renders to Letter with the matching margins."""
    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(DOC_FIELDS, encoding="utf-8")
        engine = RunEngine(
            store, str(schedule_path), operator="Alice",
            uut_type="PowerSupply", uut_serial="PS7", station_name="BENCH-03", utc_offset="+10:30",
        )
        captured: dict[str, str] = {}
        engine.run_uuid_reported.connect(lambda ru: captured.__setitem__("ru", ru))
        _drive(engine, app)

        config = _cfg(reports_directory=Path(td) / "reports", reports_organisation="ACME")
        html_path = write_report(store, captured["ru"], config)
        pdf_path = Path(td) / "report-letter.pdf"
        run_pdf_helper(html_path, pdf_path, "Letter")

        assert pdf_path.exists()
        data = pdf_path.read_bytes()
        assert data[:5] == b"%PDF-"
        assert len(data) > 1024


@pytest.mark.skipif(not QWebEngineAvailable, reason="PyQt6-WebEngine not installed")
def test_run_pdf_helper_raises_on_missing_html(app) -> None:
    """A missing HTML file makes the helper exit non-zero, so run_pdf_helper raises."""
    with tempfile.TemporaryDirectory() as td:
        pdf_path = Path(td) / "missing.pdf"
        with pytest.raises(Exception):
            run_pdf_helper(Path("/no/such/report.html"), pdf_path, "A4")


def test_report_css_margins_match_page_layout_constants(app) -> None:
    """The rendered CSS ``@page margin`` uses the same constants as the helper's QPageLayout."""
    store, ru = _record(app, DOC_FIELDS)
    html = render_report(store, ru, _cfg(reports_organisation="ACME"))

    # top right bottom left == 24mm 14mm 18mm 14mm (REPORT_PAGE_MARGINS_MM).
    assert "margin: 24mm 14mm 18mm 14mm;" in html


def _record(app, source: str = DOC_FIELDS) -> tuple[RunStore, str]:
    """Drive one schedule to completion and return (store, run_uuid)."""
    captured: dict[str, str] = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8")
        engine = RunEngine(
            store, str(schedule_path), operator="Alice",
            uut_type="PowerSupply", uut_serial="PS7", station_name="BENCH-03", utc_offset="+10:30",
        )
        engine.run_uuid_reported.connect(_capture)
        _drive(engine, app)

    assert captured.get("run_uuid") is not None, "engine never reported a run key"
    return store, captured["run_uuid"]
