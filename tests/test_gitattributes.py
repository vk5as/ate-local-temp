"""Packet 29a review: the example schedules' checksums must survive a Windows checkout.

`ate definition update` records a sha256 of each file's bytes (packet 06b), and a run refuses to
start when the bytes it loads do not match. A user on Windows with `core.autocrlf=true` gets CRLF
line endings at checkout, so every checksum in a Test Definition would mismatch and the example
Media Release would never run. `.gitattributes` pins the schedule and definition text to LF so Git
normalises them the same way on every platform.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Every text file whose bytes are checksummed by a Test Definition.
REQUIRED = {"*.tslua": "text eol=lf", "*.lua": "text eol=lf", "*.atedef": "text eol=lf"}


def _rules() -> dict[str, str]:
    """``.gitattributes`` as {pattern: attributes}, ignoring comments and blanks."""
    rules: dict[str, str] = {}
    for line in (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pattern, *attrs = line.split()
        rules[pattern] = " ".join(attrs)
    return rules


def test_gitattributes_pins_schedules_and_definitions_to_lf() -> None:
    rules = _rules()
    for pattern, attributes in REQUIRED.items():
        assert rules.get(pattern) == attributes, (
            f".gitattributes needs '{pattern} {attributes}', found {rules.get(pattern)!r}"
        )


def test_the_example_files_git_protects_are_lf_on_disk() -> None:
    """eol=lf only helps if the stored bytes are already LF, so the example files are checked too."""
    from examples.harness.run_examples import RELEASE_DIR

    for path in sorted(RELEASE_DIR.rglob("*")):
        if path.suffix in (".tslua", ".lua", ".atedef"):
            assert b"\r" not in path.read_bytes(), f"{path} carries a CR byte"
