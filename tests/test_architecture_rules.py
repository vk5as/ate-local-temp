"""Architectural invariant tests.

The spec's central architectural rule is that the **engine** and **run session**
never import widgets — they are driven only through Qt signals by a layout, so
they must stay GUI-free and testable without a display. This test scans every
``.py`` under ``src/ate/engine`` and ``src/ate/run_session`` for widget imports
(``PySide6.QtWidgets``, ``PySide6.uic``, or any ``widgets`` submodule) and fails
loudly if it finds one.

It is a structural guard: against the current skeleton (documented stubs only)
it passes, but it will catch a future ticket that accidentally pulls Qt into those
layers. It never imports PySide6 itself, so running it needs no GUI.
"""

import ast
from pathlib import Path

# Module paths scanned for widget imports — deliberately engine + run session only.
_GUARDED_DIRS = [
    "src/ate/engine",
    "src/ate/run_session",
]

_WIDGET_TOKENS = {"QtWidgets", "uic", "QtUiTools", "widgets"}


def _offending_imports(source: str) -> list[str]:
    """Return human-readable descriptions of widget imports in ``source``."""
    tree = ast.parse(source, filename="<guarded>")
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                path = alias.name
                if _path_offends(path):
                    found.append(f"import {path}")
        elif isinstance(node, ast.ImportFrom):
            # ``from X import (a, b)`` — the module itself and each name may offend.
            names = [node.module] if node.module else []
            for name in [*names, *(alias.name for alias in node.names)]:
                path = name or node.module
                if _path_offends(path):
                    found.append(f"from {node.module} import {name}")
    return found


def _path_offends(path: str) -> bool:
    """True if ``path`` is a widget module (QtWidgets, uic, or any widgets submodule)."""
    parts = path.split(".")
    return any(part in _WIDGET_TOKENS for part in parts)


def _scan_dir(directory: Path) -> list[tuple[Path, str]]:
    """Yield ``(path, source)`` for every ``.py`` under ``directory``."""
    results = []
    if not directory.exists():
        return results
    for path in sorted(directory.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        results.append((path, source))
    return results


def _check_directory(directory: Path) -> None:
    for path, source in _scan_dir(directory):
        imports = _offending_imports(source)
        if imports:
            rel = str(path.relative_to(Path("src").resolve()))
            raise AssertionError(
                f"widget import(s) found in {rel} (architectural rule violated):\n"
                + "\n".join(f"  - {imp}" for imp in imports)
            )


def test_engine_and_run_session_import_no_widgets() -> None:
    """Engine and run session must never import widgets."""
    base = Path(__file__).resolve().parent.parent / "src"
    for directory in _GUARDED_DIRS:
        _check_directory(base / directory)


def test_the_guard_still_bites_on_a_planted_widget_import() -> None:
    """A guard that matches nothing is not a guard: plant the import and it must be named.

    The tokens were written for PyQt6's names; the PySide6 names are the ones that matter now,
    so this plants each form and checks the scan reports it. A rename that left the patterns
    pointing at a binding nobody imports would pass every test above and protect nothing.
    """
    for source in (
        "from PySide6.QtWidgets import QPushButton",
        "import PySide6.QtWidgets",
        "from PySide6 import QtWidgets",
        "from PySide6.QtUiTools import QUiLoader",
        "from PyQt6.QtWidgets import QPushButton",  # the old name still off-limits
    ):
        assert _offending_imports(source), f"the guard saw nothing in {source!r}"

    assert not _offending_imports("from PySide6.QtCore import QThread, Signal"), "the guard flags a clean import"
