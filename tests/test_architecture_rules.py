"""Architectural invariant tests.

The spec's central architectural rule is that the **engine** and **run session**
never import widgets — they are driven only through Qt signals by a layout, so
they must stay GUI-free and testable without a display. This test scans every
``.py`` under ``src/ate/engine`` and ``src/ate/run_session`` for widget imports
(``PyQt6.QtWidgets``, ``PyQt6.uic``, or any ``widgets`` submodule) and fails
loudly if it finds one.

It is a structural guard: against the current skeleton (documented stubs only)
it passes, but it will catch a future ticket that accidentally pulls Qt into those
layers. It never imports PyQt6 itself, so running it needs no GUI.
"""

import ast
from pathlib import Path

# Module paths scanned for widget imports — deliberately engine + run session only.
_GUARDED_DIRS = [
    "src/ate/engine",
    "src/ate/run_session",
]

_WIDGET_TOKENS = {"QtWidgets", "uic", "widgets"}


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
