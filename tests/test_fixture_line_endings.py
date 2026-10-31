"""A checksum is a hash of bytes, so a fixture must be written with LF on every platform.

`Path.write_text` in text mode turns "\n" into `os.linesep` when no `newline` is given: on Windows
that is "\r\n". A fixture that records `sha256(SRC)` of the string it then writes ends up with
bytes on disk that do not match its own Test Definition, so every schedule in it reads as
checksum-mismatched. On the Windows CI runner that is what opened the run-start confirmation
(ticket 06c) and hung the job inside `QDialog.exec()` for 30 minutes.

The tests here make a Linux run behave like Windows where it matters — the fixture writer, the
catalog scan, the definition-update command — so the mistake is caught on the machine where it can
be fixed. `.gitattributes` pins the shipped examples to LF; this covers the fixtures the tests
write themselves.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ate.catalog import find_definitions, list_schedule_mismatches
from ate.cli import _definition_update
from tests._fixtures import write_ate_env
from tests.test_broken_rows_05b import _write_env

REPO = Path(__file__).resolve().parents[1]


def _write_like_windows(monkeypatch) -> None:
    """Make `Path.write_text` translate a bare "\n" into "\r\n", the way Windows does."""

    def write_text(self: Path, data: str, encoding=None, errors=None, newline=None) -> int:
        if newline is None:
            newline = "\r\n"
        return self.write_bytes(data.replace("\n", newline).encode(encoding or "utf-8", errors or "strict"))

    monkeypatch.setattr(Path, "write_text", write_text)


def _mismatches(*directories) -> list[str]:
    """Every (path, expected, found) the catalog would flag in these Test Definitions."""
    return [
        f"{rel}: expected {expected}, found {computed}"
        for schedule, entries in list_schedule_mismatches(*directories).items()
        for rel, expected, computed, _media in entries
    ]


def test_the_demo_fixture_is_lf_under_windows_newlines(monkeypatch, tmp_path) -> None:
    """`write_ate_env` records each file's hash, so its bytes must be the bytes it writes."""
    _write_like_windows(monkeypatch)
    env = write_ate_env(tmp_path)
    flagged = _mismatches(env["defs_dir"])
    assert not flagged, f"a fixture's bytes changed under CRLF: {flagged}"


def test_the_broken_rows_fixture_is_lf_under_windows_newlines(monkeypatch, tmp_path) -> None:
    """The packet 05b fixture hashes a schedule *string*, so its files must be written as LF.

    This is the fixture that hung the Windows CI run: four of its rows came out checksum-mismatched
    instead of broken, and `test_the_good_schedule_still_starts_a_run` then blocked on the
    confirmation nobody could click."""
    _write_like_windows(monkeypatch)
    env = _write_env(tmp_path)
    flagged = _mismatches(env["defs"])
    assert not flagged, f"a fixture's bytes changed under CRLF: {flagged}"


def test_every_fixture_write_in_the_suite_says_newline() -> None:
    """No test may write a fixture whose bytes ATE later hashes without saying LF.

    A `write_text` call with no `newline=` is a platform-dependent file: identical on Linux,
    different on Windows, and the checksums recorded in the Test Definitions are written for one of
    them. Every `write_text` in the suite therefore states the newline it writes."""
    offenders = []
    for path in sorted((REPO / "tests").glob("*.py")) + sorted((REPO / "examples").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\.write_text\(", text):
            start = match.end()
            depth, i = 1, start
            while depth:
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                i += 1
            call = text[start:i - 1]
            if "newline=" not in call:
                line = text[:match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(REPO)}:{line}: write_text without newline=\\n")
    assert not offenders, "fixtures written with the platform's line ending: " + "; ".join(offenders)


def test_the_catalog_hashes_the_bytes_on_disk_not_the_text(tmp_path) -> None:
    """A schedule whose bytes are CRLF is *those* bytes: the catalog never reads it as text.

    Production is correct here on purpose — an Operator who edits a release on Notepad has changed
    its bytes, and the run must say so rather than pretend the file still matches."""
    release = tmp_path / "release"
    release.mkdir()
    crlf = b'return ScheduleParams("CRLF schedule", "one step")\r\n  :setSchedule({})\r\n'
    (release / "main.tslua").write_bytes(crlf)
    (release / "main.atedef").write_text(
        '{"format": 1, "mediaNumber": "REL-CRLF", "name": "CRLF release", "version": "1.0.0", '
        '"files": [{"path": "main.tslua", "sha256": "' + "0" * 64 + '"}]}\n',
        encoding="utf-8",
        newline="\n",
    )
    definitions = find_definitions(release)
    computed = [f.computed_sha256 for definition in definitions for f in definition.files]
    assert computed == [hashlib.sha256(crlf).hexdigest()], computed
    assert _mismatches(release), "a CRLF file silently matched the LF checksum it was given"


def test_definition_update_records_the_bytes_on_disk(tmp_path) -> None:
    """`ate definition update` writes back the sha256 of the file's bytes, CRLF included."""
    crlf = b"return ScheduleParams('x', 'y')\r\n  :setSchedule({})\r\n"
    (tmp_path / "main.tslua").write_bytes(crlf)
    atedef = tmp_path / "demo.atedef"
    atedef.write_bytes(b'{"format": 1, "mediaNumber": "M", "name": "n", "version": "1", "files": [{"path": "main.tslua", "sha256": "abc"}]}\n')
    assert _definition_update(str(atedef)) == 0
    import json

    recorded = json.loads(atedef.read_text(encoding="utf-8"))["files"][0]["sha256"]
    assert recorded == hashlib.sha256(crlf).hexdigest(), "definition update hashed something other than the bytes"
    assert recorded != hashlib.sha256(crlf.replace(b"\r\n", b"\n")).hexdigest(), "definition update normalised line endings"
