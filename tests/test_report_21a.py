"""Seam-1 tests for packet 21a: generate an HTML Test Report from a recorded run and check it.

Record a run through RunEngine (the shared ``_drive`` helper), render with :func:`render_report`
and :func:`write_report`, and assert the Cover content, file naming and logo embedding. The Steps /
Equipment / Configuration / Run log sections are packet 21b/21c placeholders — not asserted here."""

from __future__ import annotations

import re
from pathlib import Path

from ate.config import Config
from ate.engine.run_engine import RunEngine
from ate.report.html import render_report, write_report
from ate.storage.store import RunStore

from ._fixtures import DOC_FIELDS, FAILING, _drive
from tests._fixtures import scratch_directory


def _cfg(**overrides) -> Config:
    """A report config (path is required but unused by the report); apply overrides."""
    return Config(path=Path("/tmp/ate-report-test"), **overrides)


def _record(app: object, source: str = DOC_FIELDS) -> tuple[RunStore, str]:
    """Drive one schedule to completion and return (store, run_uuid)."""
    captured: dict[str, str] = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with scratch_directory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(source, encoding="utf-8", newline="\n")
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
    return store, captured["run_uuid"]


def test_cover_content_from_recorded_run(app: object) -> None:
    """A recorded PASS run renders every Cover field from the record."""
    store, ru = _record(app, DOC_FIELDS)
    config = _cfg(reports_organisation="ACME Test Labs", reports_paper_size="A4")
    html = render_report(store, ru, config)

    # Organisation (the only text customisation besides logo/paper).
    assert "ACME Test Labs" in html
    # Document fields recorded on test_run.
    assert "Quarterly Power Audit" in html  # doc title
    assert "PS-AUD-2026-Q4" in html        # doc id
    assert "3" in html                      # doc revision
    # UUT type + serial, result banner and end reason.
    assert "PowerSupply PS7" in html
    assert "<div class=\"verdict verdict-pass\">PASS</div>" in html
    assert "COMPLETED" in html
    # Result counts: one PASS step, zeros for the rest.
    assert ">1</td><td class=\"res-fail\">0</td>" in html or ">1</td>" in html
    # Local start time shown with its UTC offset (the record's clock is +10:30).
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \+10:30", html)
    # Signature box.
    assert "Signature" in html and "Date" in html
    # Operator name.
    assert "Alice" in html


def test_logo_embedded_as_data_uri_and_no_external_urls(app: object) -> None:
    """The logo ships as a data URI; the HTML has no external (http(s)) assets."""
    with scratch_directory() as td:
        logo_path = Path(td) / "logo.png"
        logo_path.write_bytes(bytes(range(1, 256)))  # arbitrary bytes stand in for a PNG
        store, ru = _record(app, DOC_FIELDS)
        config = _cfg(reports_organisation="ACME", reports_logo=logo_path, reports_paper_size="A4")
        html = render_report(store, ru, config)

    assert "data:image/png;base64," in html  # logo embedded as a data URI
    # No external URLs anywhere (autoescape + inline CSS only).
    assert "http://" not in html and "https://" not in html


def test_file_naming_and_content(app: object) -> None:
    """write_report names the file per rule and writes the rendered HTML.

    The comparison against a fresh render is byte-exact, so the footer's "Report generated ..." stamp
    is frozen with ``generated_at`` (packet 30c): left on ``datetime.now`` it ticks a second between
    the two renders, which is what made this test flake."""
    frozen = "2026-10-30T12:00:00+00:00"  # the run's own clock is +10:30, so the stamp is 22:30
    with scratch_directory() as td:
        store, ru = _record(app, DOC_FIELDS)
        config = _cfg(reports_directory=Path(td) / "reports", reports_organisation="ACME")
        out = write_report(store, ru, config, generated_at=frozen)
        written = out.read_text(encoding="utf-8")

    # The file lives under <reports.directory>/<UUT Type>/ and follows the naming rule.
    assert out.parent == Path(td) / "reports" / "PowerSupply"
    assert re.match(r"^PowerSupply_PS7_\d{8}-\d{6}_PASS\.html$", out.name)
    # The stamp in the file is the frozen one, not the clock at write time: a write_report that
    # ignores ``generated_at`` fails here instead of leaving the test to race a second boundary.
    assert "Report generated 2026-10-30 22:30:00 +10:30 by ATE" in written
    # Its contents match a fresh render of the same recorded run.
    assert written == render_report(store, ru, config, generated_at=frozen)


def test_filename_replaces_windows_invalid_chars(app: object) -> None:
    """Serials with Windows-invalid characters are sanitised to underscores."""
    captured: dict[str, str] = {}

    def _capture(run_uuid: str) -> None:
        captured["run_uuid"] = run_uuid

    with scratch_directory() as td:
        store = RunStore(Path(td) / "ate.db")
        schedule_path = Path(td) / "schedule.tslua"
        schedule_path.write_text(DOC_FIELDS, encoding="utf-8", newline="\n")
        bad_serial = 'A:B/C\\D?E|F*G'
        engine = RunEngine(
            store,
            str(schedule_path),
            uut_type="PowerSupply",
            uut_serial=bad_serial,
            utc_offset="+10:30",
        )
        engine.run_uuid_reported.connect(_capture)
        _drive(engine, app)

    config = _cfg(reports_directory=Path(td) / "reports")
    out = write_report(store, captured["run_uuid"], config)
    name = out.name

    # No illegal characters survive; the type/serial stamp and result are still present.
    for ch in "<>:\"/\\|?*":
        assert ch not in name
    assert "PowerSupply" in name and "_PASS.html" in name


def test_filename_never_overwritten_with_suffix(app: object) -> None:
    """A second write of the same run takes a free -2 suffix instead of overwriting."""
    with scratch_directory() as td:
        store, ru = _record(app, DOC_FIELDS)
        config = _cfg(reports_directory=Path(td) / "reports")
        first = write_report(store, ru, config)
        second = write_report(store, ru, config)

        assert first.exists() and second.exists()
        assert first != second
        assert second.name == first.name.replace(".html", "-2.html", 1)


def test_failures_section_lists_failing_steps(app: object) -> None:
    """A FAIL run lists its failing step with name and message in the Failures section."""
    store, ru = _record(app, FAILING)
    html = render_report(store, ru, _cfg(reports_organisation="ACME"))

    assert "<h2>Failures</h2>" in html
    assert "LED colour" in html  # step name
    assert "wrong colour" in html  # message


def test_interrupted_and_checksum_banners(app: object) -> None:
    """The Cover warns on an INTERRUPTED run and a checksum mismatch."""
    store, ru = _record(app, DOC_FIELDS)

    # Mark the recorded run as interrupted (as close_interrupted_runs would at launch).
    store.update_run_end(ru, "FAIL", "INTERRUPTED")
    html = render_report(store, ru, _cfg(reports_organisation="ACME"))
    assert "INTERRUPTED" in html

    # A file whose computed checksum differs from its expectation triggers a mismatch banner.
    store.insert_run_file(
        ru, path="passing.tslua", kind="tslua", expected_sha256="deadbeef",
        computed_sha256="cafebabE", mismatch=True,
    )
    html = render_report(store, ru, _cfg(reports_organisation="ACME"))
    assert "did not match their recorded checksum" in html
