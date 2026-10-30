"""The ``ate`` command-line entry point.

Ticket 01 wires up the entry point and its single implemented subcommand,
``--version``, which prints ATE's version (from :mod:`ate._version`) and exits 0.
Later tickets add ``ate db ...``; this module dispatches to them rather than
reimplementing argument handling here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from .setup import SKIP_PERMISSIONS, StationLayout, SetupError, default_layout, prepare_station
from ._version import __version__


def _is_root() -> bool:
    """Whether this process runs as root/admin (POSIX euid 0). Unavailable platforms report False.

    Packagers run ``ate setup`` elevated, so the default applies real permissions; an unprivileged
    run warns and skips them rather than failing. Use ``--no-permissions`` for test runs regardless.
    """
    import os

    try:
        return os.geteuid() == 0
    except AttributeError:
        # Non-POSIX (e.g. Windows): we can't prove elevation here, so don't claim it.
        return False


def _print_version() -> int:
    """Print the version to stdout and return the process exit code."""
    print(__version__)
    return 0


def _cmd_pdf_helper(argv: list[str]) -> int:
    """Dispatch ``ate _pdf <html> <pdf> --paper A4|Letter`` — render a saved HTML report to PDF.

    Runs inside the helper child process; :func:`render_pdf` returns the intended exit code (0 on
    success, non-zero on any failure). Any unexpected error is reported and turned into a non-zero
    exit so the GUI side can raise."""
    parser = argparse.ArgumentParser(prog="ate _pdf", add_help=True)
    parser.add_argument("html", help="path to the saved HTML report")
    parser.add_argument("pdf", help="path to write the PDF")
    parser.add_argument("--paper", choices=["A4", "Letter"], default="A4", help="paper size (default: A4)")
    args, _unknown = parser.parse_known_args(argv)

    from .report.pdf import render_pdf

    try:
        code = render_pdf(args.html, args.pdf, args.paper)
    except Exception as exc:  # a bare traceback would leak into the GUI's error channel
        print(f"pdf helper error: {exc}", file=sys.stderr)
        return 1
    return code


def _cmd_selftest(argv: list[str]) -> int:
    """Dispatch ``ate _selftest`` — a bundle checks what it carries (packet 27a).

    Hidden like ``_pdf``. A PyInstaller one-directory build drops everything that no import
    reaches: the report template, the bundled ``ate.`` Lua modules, QtWebEngine's helper
    process, lupa's native runtime, pyvisa-py's backends. This prints one line per thing the
    bundle must carry and exits 1 if any of them is missing, so ``packaging/build_pyinstaller.py
    --smoke`` and the release CI can see which one failed. It is a build-time inventory, not a
    feature: it never touches a Station, a database or a Test Run.
    """
    results = _selftest_checks()
    for name, ok, detail in results:
        print(f"{name}: {'ok' if ok else 'MISSING'} - {detail}")
    return 0 if all(ok for _, ok, _ in results) else 1


def _selftest_checks() -> list[tuple[str, bool, str]]:
    """(what, is present, what was seen) for each thing a frozen bundle must carry."""
    import importlib.resources

    checks: list[tuple[str, bool, str]] = [
        (
            "python",
            True,
            f"{sys.version_info[0]}.{sys.version_info[1]} "
            + ("frozen" if getattr(sys, "frozen", False) else "source"),
        )
    ]

    def resource(rel: str) -> bytes:
        path = importlib.resources.files("ate")
        for part in rel.split("/"):
            path = path / part
        return path.read_bytes()

    # The report template and the bundled Lua modules are data files, not modules.
    try:
        template = resource("report/templates/report.html.j2")
        checks.append(("report template", b"<html" in template, f"{len(template)} bytes at ate/report/templates/report.html.j2"))
    except Exception as exc:
        checks.append(("report template", False, str(exc)))

    try:
        lua = resource("lua/devices.lua")
        checks.append(("bundled Lua", len(lua) > 0, f"ate.devices ({len(lua)} bytes at ate/lua/devices.lua)"))
    except Exception as exc:
        checks.append(("bundled Lua", False, str(exc)))

    # lupa's native runtime, and the builders the bundled module defines.
    try:
        import lupa

        runtime = lupa.LuaRuntime()
        builders = runtime.execute(resource("lua/devices.lua").decode("utf-8"))
        names = [name for name in ("Tcp", "Serial", "Udp", "Visa", "Read", "Write") if builders[name] is not None]
        checks.append(("lupa", len(names) == 6, f"Lua {runtime.lua_version[0]}.{runtime.lua_version[1]}, ate.devices builders {', '.join(names)}"))
    except Exception as exc:
        checks.append(("lupa", False, str(exc)))

    # pyvisa loads its backend by name at runtime; '@py' is the only one ATE ships.
    try:
        import pyvisa
        import pyvisa_py  # noqa: F401 - the backend pyvisa imports when it is asked for '@py'

        manager = pyvisa.ResourceManager("@py")
        manager.close()
        checks.append(("pyvisa-py", pyvisa_py.WRAPPER_CLASS is not None, f"pyvisa {pyvisa.__version__}, ResourceManager('@py') opened"))
    except Exception as exc:
        checks.append(("pyvisa-py", False, str(exc)))

    # The engine and the report renderer pull in lupa, SQLAlchemy and Jinja2.
    try:
        from .engine import lua_host  # noqa: F401
        from .report import html as report_html  # noqa: F401

        checks.append(("engine + report", hasattr(report_html, "render_report"), "ate.engine.lua_host and ate.report.html import"))
    except Exception as exc:
        checks.append(("engine + report", False, str(exc)))

    # QWebEngine: importing the bindings proves they and the Qt libraries are in the bundle;
    # the _pdf smoke check proves the helper process actually runs. QtWebEngineCore is what
    # the PDF helper uses (QtWebEngineWidgets needs an attribute set before the app starts).
    try:
        from PyQt6.QtWebEngineCore import QWebEnginePage  # noqa: F401

        checks.append(("QtWebEngine", True, "QWebEnginePage importable"))
    except Exception as exc:
        checks.append(("QtWebEngine", False, str(exc)))

    return checks


def _build_parser() -> argparse.ArgumentParser:
    """The whole ``ate`` command line, so ``--help`` works at every level (packet 30a).

    Subparsers only make the help text: the subcommands themselves are still dispatched to their
    existing handlers with their original arguments, so nothing about them changes — and stray
    arguments after a subcommand stay tolerated, as in the base CLI (30a review item 5). The
    ``metavar`` keeps the hidden helper ``_pdf`` out of the usage line (item 12).
    """
    parser = argparse.ArgumentParser(
        prog="ate",
        description="ATE — Automated Test Equipment. With no subcommand, opens the application.",
    )
    parser.add_argument("--config", metavar="PATH", help="config file for the GUI launch (default: the platform config)")
    parser.add_argument("-V", "--version", action="store_true", help="print ATE's version and exit")

    sub = parser.add_subparsers(dest="command", metavar="db|setup|definition")

    db = sub.add_parser("db", help="create or check the database")
    db.add_argument("--yes", "-y", action="store_true", help="skip the migrate confirmation prompt")

    setup = sub.add_parser("setup", help="create the Station directories, config and database")
    setup.add_argument("--root", metavar="DIR", help="prepare this directory as the Station root")
    setup.add_argument("--data-dir", metavar="DIR")
    setup.add_argument("--config-dir", metavar="DIR")
    setup.add_argument("--no-permissions", action="store_true", help="skip group/ownership/ACL setup")

    definition = sub.add_parser("definition", help="Test Definition commands")

    return parser


def _subcommand_args(argv: list[str], command: str) -> list[str]:
    """The arguments following ``command``, whatever global flags came before it."""
    for index, token in enumerate(argv):
        if token == command:
            return argv[index + 1 :]
    return []


def main(argv=None) -> int:
    """Run the ``ate`` CLI. Returns the intended process exit code.

    Packet 30a: bare ``ate`` (optionally ``--config PATH``) opens the GUI. ``--version`` still
    prints the version, and ``--help`` at any level prints usage instead of running the command.
    The existing subcommands keep the base CLI's tolerance of stray arguments (parse_known_args);
    a token that is no known command exits 1 with the "no command given" hint, as in the base
    (30a review item 5).
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    # Hidden helper subcommands, dispatched before parsing so they stay out of --help
    # (30a review item 12): _pdf is the PDF child process (ticket 22), _selftest is the
    # frozen bundle's inventory check (packet 27a).
    if argv and argv[0] == "_pdf":
        return _cmd_pdf_helper(argv[1:])
    if argv and argv[0] == "_selftest":
        return _cmd_selftest(argv[1:])
    try:
        args, unknown = _build_parser().parse_known_args(argv)
    except SystemExit as exc:
        if exc.code == 2:
            # argparse's "invalid choice" for an unknown command; the base CLI answered with a
            # hint and exit 1 instead of usage and exit 2.
            print(
                "ate: no command given\ntry 'ate --version', 'ate db status', 'ate setup' or 'ate definition update <file.atedef>'",
                file=sys.stderr,
            )
            return 1
        raise

    if args.version:
        return _print_version()

    command = args.command
    if command is None:
        if unknown:
            print(
                "ate: no command given\ntry 'ate --version', 'ate db status', 'ate setup' or 'ate definition update <file.atedef>'",
                file=sys.stderr,
            )
            return 1
        from .app import launch

        return launch(argv).code
    if command == "db":
        return _cmd_db(_subcommand_args(argv, command))
    if command == "setup":
        return _cmd_setup(_subcommand_args(argv, command))
    if command == "definition":
        return _cmd_definition(_subcommand_args(argv, command))

    print(f"ate: unknown command '{command}'", file=sys.stderr)
    return 1


def _cmd_db(argv: list[str]) -> int:
    """Dispatch ``ate db <status|migrate> [PATH] [--yes]``.

    The path is the first non-flag token after the subcommand, so `--yes` may appear anywhere
    in the db arguments without being taken as the path (30a review item 6)."""
    if not argv:
        print("ate db: no subcommand given\ntry 'ate db status' or 'ate db migrate'", file=sys.stderr)
        return 1

    sub = argv[0]
    rest = argv[1:]
    path = next((token for token in rest if not token.startswith("-")), "ate.db")
    if sub == "status":
        return _db_status(path)
    if sub == "migrate":
        return _db_migrate(path, rest)
    print(f"ate db: unknown command '{sub}'\ntry 'ate db status' or 'ate db migrate'", file=sys.stderr)
    return 1


def _db_status(path: str) -> int:
    """Print the database's schema version (PRAGMA user_version); read-only."""
    from .storage import DbLaunchStatus, check_database_at_launch

    result = check_database_at_launch(path)
    if result.status != DbLaunchStatus.OK:
        print(f"database {path}: {result.message}", file=sys.stderr)
        return 1
    conn = __import__("sqlite3").connect(f"file:{path}?mode=ro", uri=True)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()
    print(version)
    return 0


def _db_migrate(path: str, extra: list[str]) -> int:
    """Create or upgrade the database at ``path`` after confirmation.

    ``--yes``/``-y`` skips the interactive prompt (for automation and tests). A database
    built by a newer ATE is refused rather than downgraded; everything else is opened with
    :class:`RunStore`, which creates the tables if missing and sets the schema version.
    """
    parser = argparse.ArgumentParser(prog="ate db migrate", add_help=False)
    parser.add_argument("--yes", "-y", action="store_true", help="do not prompt for confirmation")
    args, _unknown = parser.parse_known_args(extra)

    if not args.yes:
        answer = input(f"Create or upgrade database at {path}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted", file=sys.stderr)
            return 1

    from .storage import DbLaunchStatus, RunStore, check_database_at_launch

    result = check_database_at_launch(path)
    # Only a database built by a newer ATE must be refused; a missing or older one is
    # exactly what migrate exists to create or upgrade.
    if result.status == DbLaunchStatus.TOO_OLD:
        print(f"database {path}: {result.message}", file=sys.stderr)
        return 1

    store = RunStore(path)  # creates the tables if missing and sets the schema version
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()
    print(f"created or upgraded database at {path} (schema version {version})")
    return 0


def _cmd_setup(argv: list[str]) -> int:
    """Dispatch ``ate setup [--root DIR] [--no-permissions]``.

    Prepares the Station's directories and default config (:func:`prepare_station`), which also runs
    the database migration, then prints what was created versus already present. ``--root DIR`` roots
    the layout at ``DIR`` for automation and tests; otherwise the platform defaults are used (see
    ``assets/packaging.md``). The group/ownership/ACL treatment is applied by default when running as
    root/admin; an unprivileged run prints a warning on stderr and skips it rather than failing, and
    ``--no-permissions`` skips it explicitly. A failure to create or write anything — e.g. an
    unwritable target — is reported on stderr and turned into a non-zero exit.
    """
    from argparse import ArgumentParser

    parser = ArgumentParser(prog="ate setup", add_help=False)
    parser.add_argument("--root", metavar="DIR", help="prepare this directory as the Station root (for automation and tests)")
    parser.add_argument("--data-dir", metavar="DIR", help="use this data directory instead of --root's data/ (packagers and tests)")
    parser.add_argument("--config-dir", metavar="DIR", help="use this config directory instead of --root's config/ (packagers and tests)")
    parser.add_argument("--no-permissions", action="store_true", help="skip group/ownership/ACL setup (unprivileged or test runs)")
    args, _unknown = parser.parse_known_args(argv)

    layout = StationLayout.from_root(args.root) if args.root is not None else default_layout()
    data_dir = Path(args.data_dir) if args.data_dir is not None else layout.data_dir
    config_dir = Path(args.config_dir) if args.config_dir is not None else layout.config_dir

    elevated = _is_root()
    skip_permissions = args.no_permissions or not elevated  # unprivileged: warn and skip, don't fail
    if skip_permissions:
        print(
            "ate setup: running unprivileged; skipping group/ownership/ACL setup (packagers run this elevated)",
            file=sys.stderr,
        )
    permissions = SKIP_PERMISSIONS if skip_permissions else None
    try:
        report = prepare_station(data_dir, config_dir, permissions=permissions)  # directories + a default config.json when none exists, then migrate the database
    except SetupError as exc:
        print(f"setup failed: {exc}", file=sys.stderr)
        return 1

    print("ate setup:")
    for created in report["created_dirs"]:
        print(f"  created: {created}")
    if report["default_config_written"]:
        print(f"  wrote default config: {report['config_file']}")
    else:
        print(f"  kept existing config: {report['config_file']}")
    print(f"  database: {report['database_path']}")
    return 0


def _cmd_definition(argv: list[str]) -> int:
    """Dispatch ``ate definition <update|…> [PATH]``."""
    sub = argv[0] if argv else None
    if sub == "update":
        if len(argv) < 2:
            print("ate definition update: missing <file.atedef>", file=sys.stderr)
            return 1
        return _definition_update(argv[1])
    if sub is None:
        print(
            "ate definition: no subcommand given\ntry 'ate definition update <file.atedef>'",
            file=sys.stderr,
        )
        return 1
    print(f"ate definition: unknown command '{sub}'", file=sys.stderr)
    return 1


def _definition_update(path: str) -> int:
    """Recompute every listed file's sha256 in place, filling in any that are missing.

    Reads ``<file.atedef>`` as JSON; for each entry with a ``path``, the file is read and its SHA-256
    (lowercase hex) written back over the existing value or filled in when absent. The definition file
    is only rewritten when at least one checksum changed, so an already-correct definition is left
    untouched. A listed file that cannot be read leaves its current sha256 and warns rather than failing
    the whole command.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        print(f"definition update: file not found: {path}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"definition update: could not read {path}: {exc}", file=sys.stderr)
        return 1

    if not isinstance(data, dict):
        print(f"definition update: '{path}' must be a JSON object", file=sys.stderr)
        return 1

    atedef_path = Path(path).resolve()
    def_dir = atedef_path.parent
    updated = 0
    for index, entry in enumerate(data.get("files", []) or []):
        if not isinstance(entry, dict) or "path" not in entry:
            print(f"definition update: files[{index}] has no 'path'; leaving its sha256", file=sys.stderr)
            continue

        rel = entry["path"]
        full = rel if Path(rel).is_absolute() else def_dir / rel
        try:
            data_bytes = full.read_bytes()
        except FileNotFoundError:
            print(f"definition update: cannot checksum missing file {rel}; leaving its sha256", file=sys.stderr)
            continue

        computed = hashlib.sha256(data_bytes).hexdigest()
        if "sha256" not in entry or entry["sha256"] != computed:
            entry["sha256"] = computed
            updated += 1

    if updated:
        atedef_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"definition update: wrote {updated} checksum(s) to {path}")
    else:
        print(f"definition update: all checksums already current in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
