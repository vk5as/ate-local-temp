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
    """Whether this process runs as root/admin. Unavailable platforms report False.

    Packagers run ``ate setup`` elevated, so the default applies real permissions; an unprivileged
    run warns and skips them rather than failing. Use ``--no-permissions`` for test runs regardless.
    """
    import os
    import sys

    # Windows has no euid: an elevated process is one UAC says is an administrator. Both spellings
    # of "Windows" are checked so tests can steer the branch without changing os.name (which
    # pathlib reads).
    if sys.platform == "win32" or os.name == "nt":
        return _is_windows_admin()
    try:
        return os.geteuid() == 0
    except AttributeError:
        # No euid to read here: we can't prove elevation, so don't claim it.
        return False


def _is_windows_admin() -> bool:
    """``shell32.IsUserAnAdmin`` — True when the process is elevated. False when it can't be asked."""
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _print_version() -> int:
    """Print the version to stdout and return the process exit code."""
    print(__version__)
    return 0


def _cmd_pdf_helper(argv: list[str]) -> int:
    """Dispatch ``ate _pdf <html> <pdf> --paper A4|Letter [--unsandboxed]`` — render a saved HTML report to PDF.

    Runs inside the helper child process; :func:`render_pdf` returns the intended exit code (0 on
    success, non-zero on any failure). Any unexpected error is reported and turned into a non-zero
    exit so the GUI side can raise. Packet 30d: the sandbox is never turned off on its own — the
    parent says so with ``--unsandboxed``, and a privileged helper run without it refuses.
    """
    parser = argparse.ArgumentParser(prog="ate _pdf", add_help=True)
    parser.add_argument("html", help="path to the saved HTML report")
    parser.add_argument("pdf", help="path to write the PDF")
    parser.add_argument("--paper", choices=["A4", "Letter"], default="A4", help="paper size (default: A4)")
    parser.add_argument(
        "--unsandboxed",
        action="store_true",
        help="the parent's packet 30d choice: render this PDF with Chromium's sandbox off (privileged only)",
    )
    args, _unknown = parser.parse_known_args(argv)

    from .report.pdf import render_pdf

    try:
        code = render_pdf(args.html, args.pdf, args.paper, args.unsandboxed)
    except Exception as exc:  # a bare traceback would leak into the GUI's error channel
        print(f"pdf helper error: {exc}", file=sys.stderr)
        return 1
    return code


def _cmd_selftest(argv: list[str], allow_unsandboxed_pdf: bool = False) -> int:
    """Dispatch ``ate _selftest`` — a bundle checks what it carries (packet 27a).

    Hidden like ``_pdf``. A PyInstaller one-directory build drops everything that no import
    reaches: the report template, the bundled ``ate.`` Lua modules, QtWebEngine's helper
    process, lupa's native runtime, pyvisa-py's backends. This prints one line per thing the
    bundle must carry and exits 1 if any of them is missing, so ``packaging/build_pyinstaller.py
    --smoke`` and the release CI can see which one failed. It is a build-time inventory, not a
    feature: it never touches a Station, a database or a Test Run.

    ``--run`` adds one end-to-end check on top of the inventory: a built-in one-step Test
    Schedule is run headless in a temporary directory (:class:`~ate.run_session.headless.HeadlessRunSession`,
    its own config and database), and the check fails unless the run wrote its HTML report and
    the hidden ``_pdf`` child wrote its PDF. That is the only way to prove a frozen bundle can
    actually run a Test Run rather than merely import its modules.

    Packet 30d: a privileged ``--run`` renders its PDF only with ``--allow-unsandboxed-pdf``; without
    it there is no dialog to ask, so the PDF is skipped and the check fails with that said plainly.
    An ordinary user renders the PDF with the sandbox on and needs no flag.

    ``--scan DIR`` is release CI's headless launch (packet 27c): it reads the config the
    installer's `ate setup` wrote, refuses a database that is not at this ATE's schema version,
    and scans a Test Definition directory — the shipped example Media Release, copied into the
    configured definitions directory — reporting the schedules it found and the broken entries.
    """
    parser = argparse.ArgumentParser(prog="ate _selftest", add_help=True)
    parser.add_argument("--run", action="store_true", help="run a built-in one-step Test Schedule head end to end")
    parser.add_argument("--scan", metavar="DIR", help="scan a Test Definition directory headless and check the database version")
    parser.add_argument("--config", metavar="PATH", help="config file for --scan (default: the platform config)")
    parser.add_argument(
        "--allow-unsandboxed-pdf",
        action="store_true",
        help="for --run as root or an administrator: render the Test Report PDF without Chromium's sandbox",
    )
    args, _unknown = parser.parse_known_args(argv)

    results = _selftest_checks()
    if args.run:
        results.append(_selftest_end_to_end(args.allow_unsandboxed_pdf or allow_unsandboxed_pdf))
    if args.scan:
        results.append(_selftest_scan(args.scan, args.config))
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
        checks.append(("pyvisa-py", pyvisa_py.WRAPPER_CLASS is not None, "ResourceManager('@py') opened (pyvisa_py backend)"))
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
    # the PDF helper uses; QtWebEngineWidgets is only collected (its hook's attribute must be
    # set before the first QCoreApplication, so importing it here would fail in a test run).
    try:
        from PyQt6.QtWebEngineCore import QWebEnginePage  # noqa: F401

        checks.append(("QtWebEngine", True, "QWebEnginePage importable"))
    except Exception as exc:
        checks.append(("QtWebEngine", False, str(exc)))

    return checks


# The schedule ``_selftest --run`` builds: one step that reports PASS. Built into the CLI so the
# check needs no Media Release on disk — and so the frozen bundle is exercised by the same Lua the
# engine really loads.
_SELFTEST_SCHEDULE = """\
return ScheduleParams("Selftest schedule", "a single step that reports PASS")
  :setSchedule({
    StepParams("selftest step"):setStepNo("1.")
      :setAction(function() ATE.report(Result.PASS, "selftest ran") end),
  })
"""


def _report_problem(html: bytes, pdf: bytes) -> str | None:
    r"""Why the reports this run wrote are not readable, or `None` if they are.

    A Test Report that is a valid PDF but has nothing in it is a packaging failure, not a rendering
    one: on a Debian container with no fonts installed Chromium finds no font, draws no glyphs, and
    prints a valid empty page. The selftest has to say so in terms an operator can act on, because
    `%PDF` alone proves only that a file was written.
    """
    from .report.pdf import pdf_has_text

    if b"<html" not in html:
        return "the HTML report is not HTML"
    if not pdf.startswith(b"%PDF"):
        return "the file the _pdf helper wrote is not a PDF"
    if not pdf_has_text(pdf):
        return (
            f"the PDF has no fonts: {len(pdf)} bytes with no text in it. "
            "Install fontconfig and a font (for example fonts-dejavu-core) so QtWebEngine has "
            "something to draw with"
        )
    return None


def _selftest_end_to_end(allow_unsandboxed_pdf: bool = False) -> tuple[str, bool, str]:
    """Run a built-in Test Schedule headless and check both reports were written (packet 27a).

    Everything is in a temporary directory with its own config and database: no Station is touched,
    and the directory is removed again. The run goes through the real seams — Test Definition,
    :class:`~ate.storage.store.RunStore`, :class:`~ate.engine.run_engine.RunEngine` — and ends with
    the report writer starting the hidden ``_pdf`` child, which inside a frozen bundle is the bundle's
    own executable. Returns ``(name, ok, what was seen)`` for ``_selftest``'s output.
    """
    import shutil
    import tempfile
    import time
    from pathlib import Path

    def pump(app, predicate, seconds: float) -> bool:
        """Pump Qt events until ``predicate()`` is true, giving up after ``seconds``."""
        deadline = time.monotonic() + seconds
        while not predicate():
            app.processEvents()
            if time.monotonic() > deadline:
                return False
            time.sleep(0.005)
        return True

    work = Path(tempfile.mkdtemp(prefix="ate-selftest-"))
    try:
        definitions = work / "definitions"
        definitions.mkdir(parents=True)
        schedule = definitions / "selftest.tslua"
        schedule.write_text(_SELFTEST_SCHEDULE, encoding="utf-8")
        (definitions / "selftest.atedef").write_text(
            json.dumps(
                {
                    "format": 1,
                    "mediaNumber": "SELFTEST",
                    "name": "Selftest release",
                    "version": "1.0.0",
                    "files": [{"path": "selftest.tslua", "sha256": hashlib.sha256(schedule.read_bytes()).hexdigest()}],
                }
            ),
            encoding="utf-8",
        )
        config_file = work / "config.json"  # absolute paths: nothing here uses the <data> placeholder
        config_file.write_text(
            json.dumps(
                {
                    "format": 1,
                    "station": {"name": "selftest", "uuid": "00000000-0000-7000-8000-000000000001"},
                    "defaultProject": "Selftest",
                    "testDefinitions": {"directories": [str(definitions)]},
                    "database": {"type": "sqlite", "path": str(work / "ate.db")},
                    "reports": {"directory": str(work / "reports")},
                    "log": {"directory": str(work / "logs")},
                }
            ),
            encoding="utf-8",
        )

        from PyQt6.QtCore import QCoreApplication

        from .config import read_config
        from .report.pdf import pdf_helper_command, pdf_has_text, pdf_sandbox_choice
        from .run_session.headless import HeadlessRunSession, SessionState

        # Packet 30d: a headless run has no dialog, so the PDF renders unsandboxed only if the flag
        # said so; otherwise the HTML is written and the PDF is skipped.
        session = HeadlessRunSession(read_config(config_file), pdf_choice=pdf_sandbox_choice(allow_unsandboxed_pdf))
        written: list[tuple[str, str]] = []
        try:
            session.report_written.connect(lambda html, pdf: written.append((html, pdf)))
            session.select_schedule("Selftest schedule")
            session.set_operator("selftest")
            session.set_project("selftest")
            session.start()
            app = QCoreApplication.instance() or QCoreApplication([])
            if not pump(app, lambda: session.state is SessionState.FINISHED, 30.0):
                return ("run end-to-end", False, "the Test Run never reached FINISHED")
            if not pump(app, lambda: written, 90.0):
                return ("run end-to-end", False, "the _pdf helper never reported the report")
            html_path, pdf_path = written[0]
            if not pdf_path:
                if _is_root() and not allow_unsandboxed_pdf:
                    return (
                        "run end-to-end",
                        False,
                        f"the PDF was skipped: ATE is running as root or an administrator and Chromium "
                        f"cannot render with the sandbox there, and no --allow-unsandboxed-pdf was given. "
                        f"The HTML report is at {html_path}; rerun with 'ate --allow-unsandboxed-pdf _selftest --run' to get the PDF",
                    )
                return ("run end-to-end", False, "the _pdf helper wrote no PDF")
            html = Path(html_path).read_bytes()
            pdf = Path(pdf_path).read_bytes()
            helper = Path(pdf_helper_command(html_path, pdf_path)[0]).name
            problem = _report_problem(html, pdf)
            if problem:
                return ("run end-to-end", False, f"{helper} ran the schedule but {problem}")
            return (
                "run end-to-end",
                True,
                f"{helper} ran the schedule: {html_path} ({len(html)} bytes HTML), "
                f"{pdf_path} ({len(pdf)} bytes PDF, with text)",
            )
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001 - the selftest reports a failure instead of raising
        return ("run end-to-end", False, str(exc))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _selftest_scan(directory: str, config_path: str | None = None) -> tuple[str, bool, str]:
    """Scan one Test Definition directory against the Station's own config and database (27c).

    This is the headless launch the release CI is required to make work: a frozen bundle that can
    read the config `ate setup` wrote, open the database it created, and list the schedules of a
    Media Release. It writes nothing and runs no Test Run. Fails on a database whose schema version
    is not this ATE's, on a directory with no ``.atedef`` in it, and on any broken Media Release or
    schedule the scan reports — a shipped example that scans broken is a packaging bug.

    Returns ``(name, ok, what was seen)`` for ``_selftest``'s output.
    """
    from .catalog import definition_mismatches, find_definitions, list_schedule_rows
    from .config import ConfigError, config_hint, read_config
    from .storage import DbLaunchStatus, SCHEMA_VERSION, check_database_at_launch

    try:
        config = read_config(config_path)
    except ConfigError as exc:
        return ("scan", False, f"config: {exc}")

    status = check_database_at_launch(config.database_path, config_file=config_hint(config.path))
    if status.status is not DbLaunchStatus.OK:
        return ("scan", False, f"database {config.database_path}: {status.message}")

    rows = list_schedule_rows(directory)
    broken = [row for row in rows if row.error]
    mismatched = sum(1 for definition in find_definitions(directory) if not definition.broken and definition_mismatches(definition))
    if not rows:
        return ("scan", False, f"no Test Definitions found in {directory}")
    ok = not broken and not mismatched
    detail = (
        f"{directory}: {len(rows) - len(broken)} schedules, {len(broken)} broken, "
        f"{mismatched} mismatched (schema {status.user_version} == {SCHEMA_VERSION})"
    )
    return ("scan", ok, detail)


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
    parser.add_argument("--config", metavar="PATH", help="config file to read (default: the platform config)")
    parser.add_argument(
        "--allow-unsandboxed-pdf",
        action="store_true",
        help="when ATE runs as root or an administrator, render Test Report PDFs without Chromium's sandbox instead of asking (or skipping them)",
    )
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
    # frozen bundle's inventory check (packet 27a). Packet 30d's flag belongs to `ate`, so
    # it is read off argv here too: `ate --allow-unsandboxed-pdf _selftest --run` is the same
    # decision as `ate _selftest --run --allow-unsandboxed-pdf`.
    allow_unsandboxed_pdf = "--allow-unsandboxed-pdf" in argv
    hidden = [token for token in argv if token != "--allow-unsandboxed-pdf"]
    if hidden and hidden[0] == "_pdf":
        return _cmd_pdf_helper(hidden[1:])
    if hidden and hidden[0] == "_selftest":
        return _cmd_selftest(hidden[1:], allow_unsandboxed_pdf)
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
        return _cmd_db(_subcommand_args(argv, command), config_path=args.config)
    if command == "setup":
        return _cmd_setup(_subcommand_args(argv, command))
    if command == "definition":
        return _cmd_definition(_subcommand_args(argv, command))

    print(f"ate: unknown command '{command}'", file=sys.stderr)
    return 1


def _cmd_db(argv: list[str], config_path: str | None = None) -> int:
    """Dispatch ``ate db <status|migrate> [PATH] [--yes]``.

    The path is the first non-flag token after the subcommand, so `--yes` may appear anywhere
    in the db arguments without being taken as the path (30a review item 6). With no path the
    configured database is used — from ``--config`` or the platform default config — so
    ``ate --config X db migrate --yes`` touches the database that config names, not a stray
    ``./ate.db`` in the working directory (packet 30c). An explicit path still overrides it."""
    if not argv:
        print("ate db: no subcommand given\ntry 'ate db status' or 'ate db migrate'", file=sys.stderr)
        return 1

    sub = argv[0]
    rest = argv[1:]
    path = next((token for token in rest if not token.startswith("-")), None)
    hint = None  # the config to name in the migrate hint, when one is not the platform default
    if path is None:
        from .config import ConfigError, config_hint, read_config

        try:
            config = read_config(config_path)
        except ConfigError as exc:  # missing or invalid config: name it, don't guess a path
            print(f"ate db: {exc}\nrun 'ate setup' or pass --config PATH first", file=sys.stderr)
            return 1
        path = str(config.database_path)
        hint = config_hint(config.path)
    if sub == "status":
        return _db_status(path, hint)
    if sub == "migrate":
        return _db_migrate(path, rest, hint)
    print(f"ate db: unknown command '{sub}'\ntry 'ate db status' or 'ate db migrate'", file=sys.stderr)
    return 1


def _db_status(path: str, hint: Path | None = None) -> int:
    """Print the database's schema version (PRAGMA user_version); read-only.

    The refusal names the database and, when a non-default config is in use, the ``--config``
    to migrate it through (packet 30c review)."""
    from .storage import DbLaunchStatus, check_database_at_launch

    result = check_database_at_launch(path, config_file=hint)
    if result.status != DbLaunchStatus.OK:
        print(result.message, file=sys.stderr)
        return 1
    conn = __import__("sqlite3").connect(f"file:{path}?mode=ro", uri=True)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()
    print(version)
    return 0


def _db_migrate(path: str, extra: list[str], hint: Path | None = None) -> int:
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

    result = check_database_at_launch(path, config_file=hint)
    # Only a database built by a newer ATE must be refused; a missing or older one is
    # exactly what migrate exists to create or upgrade.
    if result.status == DbLaunchStatus.TOO_OLD:
        print(result.message, file=sys.stderr)
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
