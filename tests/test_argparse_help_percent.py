"""A bare `%` in an argparse help string is a crash.

argparse runs every help string through `%` formatting (`HelpFormatter._expand_help` does
`self._get_help_string(action) % params`), so `packaging/windows/smoke_installer.py`'s
`--data-dir` help, which said `default: %PROGRAMDATA%\\ATE`, was never a plain sentence. CPython
3.14 expands it when the argument is registered and the smoke script died on the 3.14 CI matrix
entry with `ValueError: badly formed help string` before the installer ran; 3.13 only breaks when
the help is printed, with `ValueError: unsupported format character 'P'`, which is why the Linux
half of the matrix never noticed it.

Two checks, because a source scan alone cannot prove a parser is buildable and building one
alone cannot catch a string that only meets an un-imported module:

* every `help=`, `description=` and `epilog=` literal under `src/`, `packaging/` and `examples/`
  is fed through a real `ArgumentParser` and a `%`-format round trip, so a bare `%` fails on
  *any* Python;
* the parsers the app and the packagers actually build are built and `format_help()`-ed, so a
  help string assembled at runtime is exercised too.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
PACKAGING = REPO / "packaging"
EXAMPLES = REPO / "examples"

#: The keyword arguments argparse runs through `%` formatting.
HELP_KEYWORDS = ("help", "description", "epilog")


def _percent_is_escaped(text: str, where: str) -> None:
    """Fail on a help string argparse would reject, on any Python.

    Formatting the string by hand is the version-independent check: 3.14 does it when the argument
    is registered, 3.13 when the help is printed. `%%` and `%(prog)s` survive; a bare `%WORD%` or
    a stray `%s` does not."""
    try:
        text % {"prog": "ate"}
    except (TypeError, ValueError) as exc:
        pytest.fail(
            f"{where}: {text!r} has a bare % ({exc}). Python 3.14 raises "
            "ValueError: badly formed help string at add_argument time; write %% instead."
        )


def _literal_help_strings(root: Path):
    """(path, line, keyword, text) for every help/description/epilog written as a literal."""
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg not in HELP_KEYWORDS:
                    continue
                value = keyword.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    yield path, node.lineno, keyword.arg, value.value
                elif isinstance(value, ast.JoinedStr):
                    # An f-string: the literal pieces are what argparse would format, so
                    # reassemble them with the dynamic parts as empty.
                    parts = []
                    for piece in value.values:
                        if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                            parts.append(piece.value)
                        else:
                            parts.append("")
                    yield path, node.lineno, keyword.arg, "".join(parts)


def test_every_help_string_in_the_repo_is_percent_safe() -> None:
    """The source scan: a bare `%` in a help string is a 3.14 crash, and `%%` is the fix."""
    found = 0
    for root in (SRC, PACKAGING, EXAMPLES):
        for path, line, keyword, text in _literal_help_strings(root):
            found += 1
            _percent_is_escaped(text, f"{path.relative_to(REPO)}:{line} {keyword}")
    assert found > 0, "the scan found no help strings at all, so it proves nothing"


def _parsers_from(module_path: Path, argv: list[str]):
    """Build a packager's real parser by running its `main(["--help"])`.

    argparse prints the help and raises SystemExit before any of the packager's work starts, so
    this exercises the parser a release job actually uses without building anything."""
    spec = importlib.util.spec_from_file_location(module_path.stem, module_path)
    module = importlib.util.module_from_spec(spec)
    # Registered before the module body runs: its dataclasses look up their own module by name.
    sys.modules[module_path.stem] = module
    spec.loader.exec_module(module)
    try:
        module.main(argv)
    except SystemExit:
        pass
    return module


def _walk_subparsers(parser: argparse.ArgumentParser):
    """The main parser and every subcommand's parser, one pair of (name, parser) each."""
    yield parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from _walk_subparsers(sub)


def test_the_app_and_packager_parsers_build_and_format_help() -> None:
    """No parser the app or a release job runs can be built with an unescaped `%`."""
    from ate.cli import _build_parser

    for parser in _walk_subparsers(_build_parser()):
        text = parser.format_help()
        assert "badly formed help string" not in text, parser.prog

    for module_path in (
        PACKAGING / "build_pyinstaller.py",
        PACKAGING / "linux" / "build_linux.py",
        PACKAGING / "linux" / "docker_smoke.py",
        PACKAGING / "linux" / "smoke_linux.py",
        PACKAGING / "windows" / "build_installer.py",
        PACKAGING / "windows" / "smoke_installer.py",
    ):
        _parsers_from(module_path, ["--help"])


def test_the_smoke_installer_help_prints_one_percent_sign(capsys) -> None:
    """The escape is `%%` in the source and one `%` on the screen: not two, and not a lost path.

    Over-escaping is the mistake this guards against: `%%%%` formats happily and then reads as
    `%%PROGRAMDATA%%` to an Operator looking for the data directory."""
    _parsers_from(PACKAGING / "windows" / "smoke_installer.py", ["--help"])
    out = capsys.readouterr().out
    assert "%PROGRAMDATA%" in out, out
    assert "%%PROGRAMDATA%%" not in out, out
