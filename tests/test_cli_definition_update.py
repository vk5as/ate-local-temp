"""CLI ``ate definition update <file.atedef>`` tests (ticket 05's seam-2 slice).

Driven through :func:`ate.cli.main` against real ``.atedef`` files in a temp dir: the command
recomputes every listed file's sha256 from its bytes on disk, writing the value back over an existing
one or filling it in when absent (see ``test-definition-format.md`` Checksums). It is idempotent — a
second run finds nothing to change and leaves the file byte-for-byte untouched.
"""

import hashlib
import json

from ate.cli import main


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_atedef(tmp_path, *, files: list[dict], lua: bytes = b'return {}\n', on_disk=None) -> str:
    """Write a ``.atedef`` whose listed files exist on disk (with the given sha256 values).

    ``on_disk`` lists which entries' files are actually written; any entry not listed describes a file
    that is referenced by the .atedef but does not exist on disk (a missing-file defect).
    """
    root = tmp_path / "defs"
    release_dir = root / "demo"
    release_dir.mkdir(parents=True, exist_ok=True)
    for entry in files:
        if isinstance(entry, dict) and "path" in entry:
            target = release_dir / entry["path"]
            if on_disk is not None and entry["path"] not in on_disk:
                continue  # referenced but intentionally absent from disk
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(lua)
    atedef = {
        "format": 1,
        "mediaNumber": "REL-A",
        "name": "Demo release",
        "version": "1.0.0",
        "files": files,
    }
    path = release_dir / "demo.atedef"
    path.write_text(json.dumps(atedef), newline="\n")
    return str(path)


def test_update_recomputes_wrong_checksums_and_fills_missing_in_place(tmp_path, capsys):
    """A wrong sha256 is recomputed and an absent one is filled in; both match the file's bytes."""
    path = _write_atedef(
        tmp_path,
        files=[
            {"path": "a.tslua", "sha256": "deadbeef"},  # wrong -> recompute
            {"path": "b.lua"},                            # missing sha256 -> fill in
        ],
    )

    rc = main(["definition", "update", path])
    assert rc == 0

    data = json.loads((tmp_path / "defs" / "demo" / "demo.atedef").read_text())
    assert _sha256(b'return {}\n') in (data["files"][0]["sha256"], data["files"][1]["sha256"])
    # Both listed files now carry the correct checksum for their bytes.
    assert all(entry["sha256"] == _sha256(b'return {}\n') for entry in data["files"])

    # The non-checksum fields are preserved and only sha256 values changed.
    assert data["format"] == 1 and data["mediaNumber"] == "REL-A" and data["name"] == "Demo release"


def test_update_is_idempotent_and_leaves_an_already_current_file_untouched(tmp_path, capsys):
    """A second run finds nothing to change and does not reformat the file."""
    path = _write_atedef(
        tmp_path,
        files=[{"path": "a.tslua", "sha256": _sha256(b'return {}\n')}],
    )
    main(["definition", "update", path])  # first run writes the correct checksum

    before = (tmp_path / "defs" / "demo" / "demo.atedef").read_bytes()
    rc, captured = main(["definition", "update", path]), capsys.readouterr()
    assert rc == 0
    assert "already current" in captured.out
    # No rewrite happened: the bytes are identical to before the second run.
    assert (tmp_path / "defs" / "demo" / "demo.atedef").read_bytes() == before


def test_update_leaves_a_listed_file_missing_on_disk_untouched(tmp_path, capsys):
    """A listed file that does not exist on disk can't be checksummed; its sha256 is left as-is."""
    path = _write_atedef(
        tmp_path,
        files=[{"path": "ghost.lua", "sha256": "abc123"}],  # the file was never written to disk
        on_disk=set(),  # ghost.lua is referenced but intentionally absent
    )

    rc, captured = main(["definition", "update", path]), capsys.readouterr()
    assert rc == 0  # a missing listed file warns but does not fail the command
    data = json.loads((tmp_path / "defs" / "demo" / "demo.atedef").read_text())
    assert data["files"][0]["sha256"] == "abc123"  # left untouched


def test_update_missing_file_reports_error_and_exits_nonzero(tmp_path, capsys):
    """A path that does not exist at all is a usage error."""
    path = str(tmp_path / "defs" / "demo" / "does-not-exist.atedef")

    rc, captured = main(["definition", "update", path]), capsys.readouterr()
    assert rc == 1
    assert "not found" in captured.err


def test_update_without_args_reports_usage_and_exits_nonzero(tmp_path, capsys):
    """``ate definition update`` with no file reports the missing argument."""
    rc, captured = main(["definition", "update"]), capsys.readouterr()
    assert rc == 1
    assert "missing <file.atedef>" in captured.err
