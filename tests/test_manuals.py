"""Packet 34a and 34b: the manuals' examples really run, and the manuals cannot fall behind the code.

Four things are checked here:

1. Every Lua example under ``docs/manuals/examples/test-writer/`` runs headless through ATE
   (the same seam-1 harness the example Media Release uses) and records what the manual says
   it records.
2. Every example quoted in the Markdown manual is byte-for-byte the file on disk, every
   example is included by the LaTeX manual, and the ``.atedef`` beside them lists all of them.
3. Every ``ATE`` function, enum, ``ScheduleParams``/``StepParams`` setter, Device Type and
   ``ATE.run`` field the code defines is named in both test-writer manuals. The list is
   derived from the code, never hand-kept, so a manual that drifts fails the suite.
4. Packet 34b: the admin manual names every config key and every ``ate`` command the code
   accepts, runs every command it tells an administrator to run in a temporary Station, and
   quotes no error message that the code does not print.

The coverage check is a table of manuals and name groups so packets 34b (admin, config keys)
and 34c (developer, modules and seams) can add their own entries without touching this file.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import io
import json
import os
import pkgutil
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import pytest

from ate.devices.registry import Driver
from ate.engine.lua_host import _AteTable, _ENUMS, _RUN_FIELDS, _ScheduleParams, _StepParams

from examples.harness.peers import Peers
from examples.harness.run_examples import dead_port, run_schedule, write_config

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = ROOT / "docs/manuals/examples/test-writer"
MANUALS_DIR = ROOT / "docs/manuals"


# --- The manuals themselves ---------------------------------------------------------------

@dataclass(frozen=True)
class Manual:
    """One manual and the files that make it up, for the checks below."""

    name: str
    tex_dir: Path
    markdown: Path

    def tex_parts(self) -> list[Path]:
        """main.tex plus every chapter file it inputs."""
        return sorted(p for p in self.tex_dir.rglob("*.tex"))

    def sources(self) -> list[Path]:
        """Every source file of this manual: the LaTeX parts and the Markdown one."""
        return self.tex_parts() + [self.markdown]

    def texts(self) -> list[tuple[str, str]]:
        """``(path relative to the repository, text)`` for every source file.

        LaTeX that shapes a line rather than a word is removed first. A long key such as
        ``database.storeMismatchedFiles`` is typeset as two ``\\texttt`` runs joined by
        ``\\allowbreak``, and a hidden command is written ``\\_selftest``; read as printed,
        those are not the names the code defines, so the coverage check sees the plain text."""
        return [(str(p.relative_to(ROOT)), plain(p.read_text(encoding="utf-8"))) for p in self.sources()]


#: LaTeX that only affects how a line is broken up. Stripped by :meth:`Manual.texts`.
_LAYOUT_MARKUP = re.compile(r"\\(?:allowbreak|texttt|textbf|text|emph|verb|sbox|par)\b|[{}]")


def plain(text: str) -> str:
    """The manual's text without its line-breaking markup, so a name split across two runs matches."""
    return _LAYOUT_MARKUP.sub("", text.replace("\\_", "_"))


TEST_WRITER = Manual(
    name="test-writer",
    tex_dir=MANUALS_DIR / "test-writer",
    markdown=MANUALS_DIR / "llm/test-writer.md",
)

ADMIN = Manual(
    name="admin",
    tex_dir=MANUALS_DIR / "admin",
    markdown=MANUALS_DIR / "llm/admin.md",
)

#: Every manual this packet builds. 34c adds "developer" here, and a group of names for it in
#: COVERAGE below.
MANUALS: dict[str, Manual] = {"test-writer": TEST_WRITER, "admin": ADMIN}

#: The examples directory each manual is written against.
MANUAL_EXAMPLES: dict[str, Path] = {"test-writer": EXAMPLES_DIR}


# --- Names the code defines ---------------------------------------------------------------

def ate_table_functions() -> set[str]:
    """Every function the ``ATE`` table exposes to a Test Schedule.

    ``register`` and ``publish_run`` are the engine's own seams - they build the table rather
    than being calls a schedule makes - so they are excluded. ``run`` is a table rather than a
    function and is named explicitly, because it is reached through ``__getattr__`` and not the
    registry.
    """
    seams = {"register", "publish_run"}
    table = _AteTable()
    names = {name for name in table._extra}
    for name, member in inspect.getmembers(table, callable):
        if not name.startswith("_") and name not in seams:
            names.add(name)
    names.add("run")
    return names


def enum_names() -> set[str]:
    """Every enum table and every member of it."""
    return {name for name, members in _ENUMS.items()} | {m for members in _ENUMS.values() for m in members}


def builder_methods() -> set[str]:
    """Every setter ``ScheduleParams`` and ``StepParams`` offers."""
    return {
        name
        for cls in (_ScheduleParams, _StepParams)
        for name, _member in inspect.getmembers(cls, callable)
        if name.startswith("set")
    }


def device_types() -> set[str]:
    """Every Device Type ATE registers, taken from the drivers that serve them."""
    import ate.devices

    names = set()
    for module_info in pkgutil.iter_modules(ate.devices.__path__):
        module = importlib.import_module(f"ate.devices.{module_info.name}")
        for member in vars(module).values():
            if isinstance(member, type) and issubclass(member, Driver) and member is not Driver:
                names.add(member.name)
    return names


def run_fields() -> set[str]:
    """The fields ``ATE.run`` describes."""
    return set(_RUN_FIELDS)


def config_keys() -> set[str]:
    """Every config key :func:`ate.config.read_config` accepts, taken from its own schema.

    ``_SCHEMA`` is the list the validator checks against, so a key added to the code and not to
    the manual is caught. ``format`` and ``defaultProject`` are top-level and handled apart.
    """
    from ate.config import _SCHEMA

    return {f"{section}.{key}" for section, keys in _SCHEMA.items() for key in keys} | {"format", "defaultProject"}


def _usage_of(dispatch: Callable[[list[str]], int]) -> str:
    """The usage text a hidden helper prints for ``--help``, captured without running it."""
    out = io.StringIO()
    with redirect_stdout(out):
        try:
            dispatch(["--help"])
        except SystemExit:
            pass  # argparse exits after printing; the text is what we want
    return out.getvalue()


def cli_names() -> set[str]:
    """Every ``ate`` subcommand, flag and hidden helper, derived from ``src/ate/cli.py``.

    The visible ones come from the parser ``--help`` builds. ``_pdf`` and ``_selftest`` are
    deliberately not in ``ate --help``, so their own usage text is asked for. The subcommands of
    ``db`` and ``definition`` are not in the parser at all: they are the strings the handlers
    compare against.
    """
    from ate import cli

    names: set[str] = set()

    def walk(parser: argparse.ArgumentParser) -> None:
        for action in parser._actions:
            names.update(action.option_strings)
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict):  # a subcommand table, not a list of allowed values
                for command, sub in choices.items():
                    names.add(command)
                    walk(sub)

    walk(cli._build_parser())
    for dispatch in (cli._cmd_pdf_helper, cli._cmd_selftest):
        names.add(re.search(r"usage: ate (\S+)", _usage_of(dispatch)).group(1))
        names.update(re.findall(r"(?<![\w-])(-{1,2}[A-Za-z][A-Za-z-]*)", _usage_of(dispatch)))
    for handler in (cli._cmd_db, cli._cmd_definition):
        names.update(re.findall(r'if sub == "([a-z]+)"', inspect.getsource(handler)))
    return names - {"-h"}  # `--help` is documented; its short form is the same flag


#: A group of code-derived names, and the manual each must appear in. 34c adds module and seam
#: names for the developer manual. Same shape, same failure.
COVERAGE: list[tuple[str, str, Callable[[], set[str]]]] = [
    ("test-writer", "ATE table functions", ate_table_functions),
    ("test-writer", "enum tables and their members", enum_names),
    ("test-writer", "ScheduleParams and StepParams setters", builder_methods),
    ("test-writer", "Device Types", device_types),
    ("test-writer", "ATE.run fields", run_fields),
    ("admin", "config keys the validator reads", config_keys),
    ("admin", "ate subcommands and flags", cli_names),
]


def missing_from(texts: Iterable[tuple[str, str]], names: Iterable[str]) -> list[str]:
    """The names that appear nowhere in ``texts``.

    A name counts when it appears as a whole word, so ``readTerm`` does not count as ``read``
    and prose that says the word counts. A flag is not a word: there is no word boundary before
    the dashes of ``--yes``, and the ``-y`` inside ``--yes`` is not the short flag, so a flag has
    to start after something that is neither a letter, a digit nor another dash.
    """
    remaining = set(names)
    for _label, text in texts:
        for name in list(remaining):
            start = r"(?<![-\w])" if name.startswith("-") else r"\b"
            if re.search(f"{start}{re.escape(name)}\\b", text):
                remaining.discard(name)
    return sorted(remaining)


def test_no_name_the_code_defines_is_missing_from_its_manual() -> None:
    """Every ATE function, enum, setter, Device Type and run field is named in both manuals."""
    for manual_name, group, derive in COVERAGE:
        gone = missing_from(MANUALS[manual_name].texts(), derive())
        assert not gone, f"the {manual_name} manual is missing from {group}: {', '.join(gone)}"


def test_the_coverage_check_can_fail() -> None:
    """Show the check bites: the same names against a manual with one removed reports it.

    This is not a bug report; it is the proof that a manual quietly losing a function is caught."""
    texts = MANUALS["test-writer"].texts()
    names = ate_table_functions()
    assert "addEquipment" in names
    stripped = [(label, text.replace("addEquipment", "somethingElse")) for label, text in texts]
    assert missing_from(stripped, names) == ["addEquipment"], "removing a name must show up as missing"
    assert not missing_from(texts, names), "the manual as written has every name"


def test_the_admin_coverage_check_can_fail() -> None:
    """The config-key check fails on a manual that drops a key, which is what it is for.

    Delete one line of the key table and the suite says which key went missing; the check is not
    a table nobody reads."""
    texts = MANUALS["admin"].texts()
    keys = config_keys()
    assert "ui.allowHistoryDuringRun" in keys, "the schema changed shape; the derivation is wrong"
    stripped = [(label, text.replace("allowHistoryDuringRun", "somethingElse")) for label, text in texts]
    assert missing_from(stripped, keys) == ["ui.allowHistoryDuringRun"], "removing a key must show up as missing"
    assert not missing_from(texts, keys), "the admin manual as written has every config key"


# --- Every command the admin manual shows is run, or explained --------------------------

#: A listing in the admin manual whose lines are commands an administrator types.
SHELL_LISTING = re.compile(r"\\begin\{lstlisting\}\[language=ATEShell\](.*?)\\end\{lstlisting\}", re.S)

#: A listing line is a command when it starts with one of these. Everything else in a listing is
#: a message the machine printed.
COMMAND_TOKENS = {"ate", "sudo", "apt-get", "pipx", "python", "./install.sh"}


def shell_commands(manual: Manual) -> list[str]:
    """Every command line in a manual's ATEShell listings, comment stripped.

    ``ate setup: running unprivileged ...`` looks like a command and is not: it is what setup
    printed. A token ending in a colon at the second position is the output of the command named
    first."""
    commands: list[str] = []
    for tex in manual.tex_parts():
        for block in SHELL_LISTING.findall(tex.read_text(encoding="utf-8")):
            for line in (line.split("#", 1)[0].strip() for line in block.splitlines()):
                tokens = line.split()
                if not tokens or tokens[0] not in COMMAND_TOKENS:
                    continue
                if len(tokens) > 1 and tokens[1].endswith(":"):
                    continue
                commands.append(line)
    return commands


#: The commands the suite runs, in a temporary Station, with the exit code the manual claims, a
#: string its output must contain, and the directory it is run from. ``{station}``, ``{config}``,
#: ``{db}``, ``{data}`` and ``{definitions}`` come from the fixture below.
COMMANDS_RUN: list[tuple[str, list[str], int, str, str]] = [
    ("ate --version", ["--version"], 0, "", "{station}"),
    ("ate -V", ["-V"], 0, "", "{station}"),
    ("ate --help", ["--help"], 0, "usage: ate", "{station}"),
    ("ate setup --root /tmp/station --no-permissions",
     ["setup", "--root", "{station}", "--no-permissions"], 0, "database:", "{station}"),
    ("ate --config /tmp/station/config/config.json db status",
     ["--config", "{config}", "db", "status"], 0, "", "{station}"),
    ("ate db status", ["db", "status"], 0, "", "{station}"),
    ("ate db migrate --yes", ["db", "migrate", "--yes"], 0, "created or upgraded database at", "{station}"),
    ("ate db migrate /tmp/station/data/ate.db --yes",
     ["db", "migrate", "{db}", "--yes"], 0, "schema version 5", "{station}"),
    ("ate db migrate", ["db", "migrate"], 0, "created or upgraded database at", "{station}"),  # answered "y"
    ("ate definition update definitions/MR-1043/release.atedef",
     ["definition", "update", "definitions/MR-1043/release.atedef"], 0, "checksums", "{data}"),
    ("ate _selftest", ["_selftest"], 0, "engine + report: ok", "{station}"),
    ("ate _selftest --run", ["_selftest", "--run"], 0, "run end-to-end: ok", "{station}"),
    ("ate _selftest --scan definitions/MR-1043",
     ["_selftest", "--scan", "definitions/MR-1043"], 1, "1 broken", "{data}"),
    ("python packaging/licence/notices.py --out build/licences",
     ["__not_ate__"], 0, "THIRD_PARTY_NOTICES.txt", "{station}"),
]

#: The commands the manual shows that cannot be run on this machine, and what covers them
#: instead. A command an admin is told to run that nobody runs and nobody explains is a bug in
#: this file, not a limitation of the test suite.
COMMANDS_NOT_RUN: dict[str, str] = {
    "ate-1.2.3-windows-x64-setup.exe /VERYSILENT /SUPPRESSMSGBOXES /CLOSEAPPLICATIONS":
        "Windows only. The same flags are packaging/windows/smoke_installer.py's INSTALL_FLAGS, "
        "which tests/test_inno_installer_27b.py reads and asserts the .iss uses",
    "./install.sh --prefix /opt/ate --bin /usr/bin/ate":
        "runs in a debian:stable container (tests/test_release_ci_28c.py, ATE_DOCKER_TESTS=1)",
    "apt-get install -y ./ate_1.2.3_amd64.deb":
        "same containers; this machine is not the Debian the artifact is for",
    "pipx install dist/ate-1.2.3-py3-none-any.whl":
        "same containers",
    "sudo ate setup":
        "needs root. The permissions it applies are checked through the seams named below, never "
        "by running groupadd or chown on this machine",
    "ate setup":
        "writes to the platform config and data directories. conftest pins the platform data "
        "directory for every test; the --root form above is the one that is executed",
    "ate": "opens the application. tests/test_gui_launch_30a.py drives the same launch() with the "
           "dialogs injected, so no window is ever shown",
    "ate --config /tmp/station/config/config.json":
        "opens the application with that config. The flag is exercised without opening a window by "
        "`ate --config PATH db status` above, and the launch itself by tests/test_gui_launch_30a.py",
    "ate --allow-unsandboxed-pdf":
        "also opens the application; the flag's effect on the PDF is asserted in "
        "tests/test_pdf_sandbox_choice_30d.py",
    "ate _pdf <html> <pdf> --paper A4|Letter [--unsandboxed]":
        "not for humans to run, as the manual says; it is started by the report writer, and its "
        "messages are checked against src/ate/report/pdf.py below",
}


def run_ate(argv: list[str], station: Path, cwd: Path | None = None, stdin: str = "") -> tuple[int, str, str]:
    """Run one ``ate`` command as its own process, in a temporary Station.

    The platform config and data directory are steered at the temporary Station with
    ``ATE_DEFAULT_CONFIG``/``ATE_DATA_DIR``, the test-only seam in :mod:`ate.config`, so nothing
    is written to ``/etc/ate`` or ``/var/lib/ate``. ``ATE_ELEVATED=0`` is inherited from conftest,
    so the command runs as an ordinary user wherever a real Station would not.
    """
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "ATE_DEFAULT_CONFIG": str(station / "config" / "config.json"),
        "ATE_DATA_DIR": str(station / "data"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "ate", *argv],
        cwd=str(cwd or station),
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    return proc.returncode, proc.stdout, proc.stderr


@pytest.fixture
def station(tmp_path) -> Path:
    """A Station prepared once in a temporary directory, ready for the commands above."""
    root = tmp_path / "station"
    root.mkdir()
    code, out, err = run_ate(["setup", "--root", str(root), "--no-permissions"], root)
    assert code == 0, f"setup failed: {err}"
    # The manual's Media Release example, so `ate definition update` and `--scan` have real files.
    # The example's definition is called manual.atedef; the manual's example is release.atedef.
    release = root / "data" / "definitions" / "MR-1043"
    shutil.copytree(EXAMPLES_DIR, release)
    (release / "manual.atedef").rename(release / "release.atedef")
    return root


def _fill(placeholder: str, station: Path) -> str:
    """Substitute the paths a command line refers to with the temporary Station's."""
    return (
        placeholder.replace("{station}", str(station))
        .replace("{config}", str(station / "config" / "config.json"))
        .replace("{db}", str(station / "data" / "ate.db"))
        .replace("{data}", str(station / "data"))
        .replace("{definitions}", str(station / "data" / "definitions"))
    )


def test_the_admin_manual_shows_no_command_that_is_never_run() -> None:
    """Every command an admin is told to type is either run by the suite or explained.

    Adding a line to a listing without adding it to one of the two tables fails this test, which
    is the point: the manual cannot quietly start telling people to run something nobody has
    ever executed."""
    shown = set(shell_commands(MANUALS["admin"]))
    unaccounted = sorted(shown - {line for line, *_ in COMMANDS_RUN} - set(COMMANDS_NOT_RUN))
    assert not unaccounted, f"the admin manual shows commands no test runs or explains: {unaccounted}"
    missing = sorted({line for line, *_ in COMMANDS_RUN} - shown)
    assert not missing, f"the suite runs commands the manual no longer shows: {missing}"


@pytest.mark.parametrize("line, argv, expected, wanted, cwd", COMMANDS_RUN)
def test_a_command_in_the_admin_manual_runs_in_a_temporary_station(station, line, argv, expected, wanted, cwd) -> None:
    """Run one command the manual shows, in a temporary Station, and check what it claims."""
    if argv == ["__not_ate__"]:  # the licence script, not an `ate` command
        out_dir = station / "licence-out"
        proc = subprocess.run(
            [sys.executable, "packaging/licence/notices.py", "--out", str(out_dir)],
            cwd=str(ROOT), capture_output=True, text=True, timeout=180,
        )
        assert proc.returncode == expected, proc.stderr[-500:]
        assert wanted in proc.stdout, proc.stdout
        assert {p.name for p in out_dir.iterdir()} == {
            "NOTICE-Qt-PySide6.txt", "THIRD_PARTY_NOTICES.txt", "LGPL-3.0.txt", "GPL-3.0.txt"
        }
        return

    code, out, err = run_ate([_fill(a, station) for a in argv], station, cwd=_fill(cwd, station), stdin="y\n")
    assert code == expected, f"{line}: exit {code}, expected {expected}\n{out}\n{err}"
    if wanted:
        assert wanted in out + err, f"{line}: output was {out + err!r}, expected {wanted!r}"
    if line in ("ate --version", "ate -V"):
        assert re.fullmatch(r"\d+\.\d+\.\d+[^\s]*", out.strip()), f"{line}: printed {out!r}"


def test_db_status_prints_the_schema_version_the_manual_gives(station) -> None:
    """``ate db status`` on a Station ``ate setup`` prepared prints the current schema version."""
    from ate.storage.schema import SCHEMA_VERSION

    code, out, _ = run_ate(["db", "status"], station)
    assert code == 0, out
    assert out.strip() == str(SCHEMA_VERSION), f"the manual says {SCHEMA_VERSION}, status said {out.strip()}"

    code, out, err = run_ate(["db", "status", str(station / "data" / "missing.db")], station)
    assert code == 1 and "run `ate db migrate`" in err, f"a missing database should refuse: {err}"


def test_the_config_flag_belongs_to_ate_not_to_db(station) -> None:
    """The manual warns that `--config` must come before `db`, and the code proves it.

    After the subcommand the path is read as a token, so `ate db status --config X` opens X as the
    database. The manual tells an admin this, so the suite has to show it is true."""
    code, out, err = run_ate(["db", "status", "--config", str(station / "config" / "config.json")], station)
    assert code == 1 and "file is not a database" in err, f"expected the documented trap, got {err[-300:]}"
    assert "ate --config PATH db status" in "\n".join(text for _label, text in MANUALS["admin"].texts())


def test_migrate_refuses_a_database_from_a_newer_ate(station) -> None:
    """The refusal the manual quotes for a newer database is what the command prints."""
    from ate.storage.schema import SCHEMA_VERSION

    code, out, _ = run_ate(["db", "migrate", "--yes"], station)
    assert code == 0, out
    db = station / "data" / "ate.db"
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    code, out, err = run_ate(["db", "migrate", str(db), "--yes"], station)
    assert code == 1 and "this ATE is too old for" in err, f"expected the refusal, got {err}"
    # The same refusal when the path is not given, since the configured database is that one.
    code, out, err = run_ate(["db", "migrate", "--yes"], station)
    assert code == 1 and "this ATE is too old for" in err, f"the configured database was migrated anyway: {out}{err}"


def test_definition_update_fixes_the_checksums_the_manual_says_it_fixes(station) -> None:
    """Break a checksum, run the command the manual shows, and the file matches again."""
    atedef = station / "data" / "definitions" / "MR-1043" / "release.atedef"
    data = json.loads(atedef.read_text(encoding="utf-8"))
    for entry in data["files"]:
        if entry["path"] == "first.tslua":
            entry["sha256"] = "0" * 64
    atedef.write_text(json.dumps(data), encoding="utf-8", newline="\n")

    code, out, err = run_ate(["definition", "update", "definitions/MR-1043/release.atedef"], station / "data")
    assert code == 0, err
    updated = json.loads(atedef.read_text(encoding="utf-8"))
    for entry in updated["files"]:
        computed = hashlib.sha256((station / "data" / "definitions" / "MR-1043" / entry["path"]).read_bytes()).hexdigest()
        assert entry["sha256"] == computed, f"{entry['path']} still does not match after the update"


#: Every message the admin manual quotes, and where the code prints it: (text in the manual,
#: text in the source, file). If the code changes a message and the manual does not, this fails.
QUOTED_MESSAGES: list[tuple[str, str, str]] = [
    ("No config file at /etc/ate/config.json", "No config file at {path}", "src/ate/app.py"),
    ("unknown config key 'defnitions'", "unknown config key '{key}' in", "src/ate/config.py"),
    ("'log.level' must be a string", "'{full}' must be a {kind}, got", "src/ate/config.py"),
    ("'format' is required (expected 1)", "'format' is required (expected 1)", "src/ate/config.py"),
    ("unknown 'format' value 2 (expected 1)", "unknown 'format' value", "src/ate/config.py"),
    ("'database.type' must be 'sqlite', got 'postgres'", "'database.type' must be 'sqlite'", "src/ate/config.py"),
    ("'ui.layout' must be 'staged' or 'threePane', got 'wide'", "'ui.layout' must be 'staged' or 'threePane'", "src/ate/config.py"),
    ("'devices.receiveBufferBytes' must be a positive integer", "'devices.receiveBufferBytes' must be a positive integer", "src/ate/config.py"),
    ("has unknown placeholder '<datat>'", "has unknown placeholder {token!r}", "src/ate/config.py"),
    ("invalid JSON in config file", "invalid JSON in config file", "src/ate/config.py"),
    ("cannot write station.uuid to /etc/ate/config.json", "cannot write station.uuid to", "src/ate/config.py"),
    ("run `ate db migrate` to create or upgrade the database", "run `{hint}` to create or upgrade the database", "src/ate/storage/store.py"),
    ("this ATE is too old for the database", "this ATE is too old for", "src/ate/storage/store.py"),
    ("Run 'ate setup' as admin, or point", "Run 'ate setup' as admin, or point the", "src/ate/app.py"),
    ("running unprivileged; skipping group/ownership/ACL setup", "running unprivileged; skipping group/ownership/ACL setup", "src/ate/cli.py"),
    ("checksum mismatch for lib/scpi.lua (expected", "checksum mismatch for {rel_path} (expected", "src/ate/engine/run_engine.py"),
    ("Test Report PDF skipped", "Test Report PDF skipped", "src/ate/run_session/headless.py"),
    ("the PDF has no fonts", "the PDF has no fonts:", "src/ate/cli.py"),
    ("refusing to render as root or an administrator without --unsandboxed",
     "refusing to render as root or an administrator without --unsandboxed", "src/ate/report/pdf.py"),
    ("PDF helper crashed or timed out after 60 s", "PDF helper crashed or timed out after", "src/ate/report/pdf_job.py"),
    ("PDF helper wrote no PDF", "PDF helper wrote no PDF", "src/ate/report/pdf_job.py"),
    ("paper must be 'A4' or 'Letter'", "paper must be 'A4' or 'Letter'", "src/ate/report/pdf.py"),
    ("Internal error: this window may have stopped updating", "Internal error: this window may have stopped updating", "src/ate/app.py"),
    ("Unhandled exception on thread", "Unhandled exception on thread", "src/ate/exception_log.py"),
    ("interrupted run(s) at launch.", "interrupted run(s) at launch.", "src/ate/run_session/headless.py"),
    ("receive buffer overflow - dropped 4096 oldest byte(s)", "receive buffer overflow - dropped", "src/ate/engine/lua_host.py"),
    ("left open at end of run", "left open at end of run", "src/ate/engine/run_engine.py"),
    ("was still closing when the aborted run ended", "was still closing when the aborted run ended", "src/ate/engine/run_engine.py"),
    ("unknown .atedef format: 2", "unknown .atedef format: {fmt!r}", "src/ate/catalog.py"),
    ("required 'mediaNumber' is missing or empty", "required '{field_name}' is missing or empty", "src/ate/catalog.py"),
    ("is not valid semver 2.0.0", "is not valid semver 2.0.0", "src/ate/catalog.py"),
    ("listed file is missing: lib/scpi.lua", "listed file is missing: {rel}", "src/ate/catalog.py"),
    ("duplicate path 'lib/scpi.lua' listed more than once", "duplicate path '{rel}' listed more than once", "src/ate/catalog.py"),
    ("unknown file extension '.py' for lib/helper.py", "unknown file extension '{suffix}' for {rel}", "src/ate/catalog.py"),
    ("depends[0] needs 'name' and 'version'", "depends[{index}] needs 'name' and 'version'", "src/ate/catalog.py"),
    ("WARNING: Test Definition directory does not exist", "WARNING: Test Definition directory does not exist", "src/ate/catalog.py"),
]


def test_every_message_the_manual_quotes_is_one_the_code_prints() -> None:
    """The troubleshooting chapter says its text is copied from the code. This is where that is checked.

    A message reworded in the code without the manual following it, or a message the manual invented,
    both fail here."""
    manual = "\n".join(text for _label, text in MANUALS["admin"].texts())
    for quoted, source, path in QUOTED_MESSAGES:
        code = (ROOT / path).read_text(encoding="utf-8")
        assert quoted in manual, f"the manual no longer quotes {quoted!r}"
        assert source in code, f"{path} no longer prints {source!r}, which {quoted!r} came from"""


def test_the_pdf_helper_timeout_is_the_sixty_seconds_the_manual_says() -> None:
    """The manual says 60 s; that number comes from PDF_TIMEOUT_MS, not from prose."""
    from ate.report.pdf_job import PDF_TIMEOUT_MS

    assert PDF_TIMEOUT_MS == 60_000, f"the manual says 60 s, the code says {PDF_TIMEOUT_MS / 1000} s"
    assert "timed out after 60 s" in "\n".join(text for _label, text in MANUALS["admin"].texts())


#: Numbers the admin manual states that are constants in the code: (module, attribute, value, the
#: words the manual uses). If the code changes one and the manual does not, this fails.
NUMBERS_IN_THE_MANUAL: list[tuple[str, str, object, str]] = [
    ("ate.engine.run_engine", "_ABORT_CLOSE_SECONDS", 3.0, "three seconds"),
    ("ate.devices.abort", "POLL_SECONDS", 0.05, "50 ms"),
    ("ate.storage.schema", "SCHEMA_VERSION", 5, "the current one is 5"),
    ("ate.report.pdf_job", "PDF_TIMEOUT_MS", 60_000, "60 s"),
]


def test_the_numbers_the_manual_gives_are_the_numbers_the_code_has() -> None:
    """Every figure in the admin manual that is a constant somewhere in the code is checked against it."""
    manual = "\n".join(text for _label, text in MANUALS["admin"].texts())
    for module, attribute, value, as_written in NUMBERS_IN_THE_MANUAL:
        actual = getattr(importlib.import_module(module), attribute)
        assert actual == value, f"{module}.{attribute} is now {actual}, the manual says {value}"
        assert as_written in manual, f"the manual no longer says {as_written!r}"
    from ate.config import _DEFAULTS

    assert _DEFAULTS["devices.receiveBufferBytes"] == 16_777_216
    assert "16777216" in manual, "the manual no longer prints the buffer default the code uses"


@pytest.mark.skipif(sys.platform != "linux", reason="the pseudo-terminal probe needs a Linux pty")
def test_ate_takes_no_exclusive_lock_on_a_linux_serial_port() -> None:
    """Chapter 10 tells an administrator that another program can open the same tty while a Test Run
    holds it. ATE never asks for the lock, and the probe shows the lock is not implied either: with
    one descriptor already on the port, an open that asks for exclusive access still succeeds.

    A pseudo-terminal stands in for a real tty; no instrument is touched."""
    import os
    import pty
    import tty

    import serial

    serial_source = (ROOT / "src" / "ate" / "devices" / "serial.py").read_text(encoding="utf-8")
    assert "exclusive" not in serial_source, "the SERIAL driver now asks for exclusive access"

    master, slave = pty.openpty()
    tty.setraw(slave)
    port = os.ttyname(slave)
    os.close(slave)  # the master still holds the port, as a Device's descriptor would
    try:
        second = serial.Serial(port, exclusive=True, timeout=0.2)
        assert second.is_open, f"the exclusive open on {port} did not succeed"
        second.close()
    finally:
        os.close(master)

    manual = "\n".join(text for _label, text in MANUALS["admin"].texts())
    assert "serial.Serial(exclusive=True)" in manual, "the manual stopped naming the probe"


# --- setup's permissions, proven where they can be ---------------------------------------

#: The container script that runs the real `groupadd`/`chgrp`/`chmod` that `ate setup` uses. It
#: only ever runs inside debian:stable, never on this machine.
CONTAINER_PERMISSIONS_SCRIPT = r"""
import grp, os, sys
sys.path.insert(0, "/work")
from permissions import ATE_GROUP, LinuxPermissions

root = "/tmp/station"
data, config = root + "/data", root + "/config"
os.makedirs(data + "/reports", exist_ok=True)
os.makedirs(config, exist_ok=True)
open(data + "/ate.db", "w").close()
open(config + "/config.json", "w").write("{}")

perms = LinuxPermissions()
perms.ensure_group(ATE_GROUP)
perms.ensure_group(ATE_GROUP)          # a second run must not fail on a group that exists
perms.set_group_writable(data, ATE_GROUP, recursive=True)
perms.set_admin_only(config + "/config.json")
perms.set_admin_only(config)

def entry(path):
    # 0o7777 keeps the setgid bit but drops the file-type bits.
    st = os.stat(path)
    return f"{path} mode={st.st_mode & 0o7777:o} group={grp.getgrgid(st.st_gid).gr_name}"

open(data + "/ate.db-wal", "w").close()   # created after setup, like a WAL file during a run
print("data " + entry(data))
print("reports " + entry(data + "/reports"))
print("db " + entry(data + "/ate.db"))
print("wal " + entry(data + "/ate.db-wal"))
print("configdir " + entry(config))
print("config " + entry(config + "/config.json"))
"""


@pytest.mark.build
@pytest.mark.slow
def test_the_real_linux_permission_commands_work_in_debian_stable(tmp_path: Path) -> None:
    """Run :class:`ate.permissions.LinuxPermissions` for real, in a fresh debian:stable container.

    Skipped unless ``ATE_DOCKER_TESTS=1``: it needs Docker and takes a couple of minutes. On this
    machine the permissions are only ever asserted through ``RecordingPermissions``, which records
    the calls instead of making them (tests/test_cli_setup.py); this is the check that the commands
    themselves do what the manual says they do.
    """
    if os.environ.get("ATE_DOCKER_TESTS") != "1":
        pytest.skip("the Debian container takes minutes; set ATE_DOCKER_TESTS=1 to run it")
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed: the container cannot start")

    work = tmp_path / "permissions"
    work.mkdir()
    shutil.copy(ROOT / "src" / "ate" / "permissions.py", work / "permissions.py")
    (work / "check.py").write_text(CONTAINER_PERMISSIONS_SCRIPT, encoding="utf-8")

    proc = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{work}:/work", "-w", "/work",
         "-e", "PYTHONDONTWRITEBYTECODE=1", "debian:stable", "sh", "-c",
         "apt-get update -qq && apt-get install -y -qq python3 >/dev/null && python3 -B /work/check.py"],
        capture_output=True, text=True, timeout=900,
    )
    assert proc.returncode == 0, f"the container run failed: {proc.stderr[-1000:]}"
    lines = {line.split()[0]: line for line in proc.stdout.splitlines() if line}

    def mode_and_group(key):
        match = re.search(r"mode=(\d+) group=(\w+)", lines[key])
        assert match, f"{key}: {lines[key]}"
        return int(match.group(1), 8), match.group(2)

    # The data tree is group-owned by `ate`, and its directories carry setgid so a file created
    # afterwards (a WAL file, a new report) inherits the group rather than dropping it. If the
    # `ate` group had not been created, `chgrp` would have failed and the run would not be 0.
    for key in ("data", "reports", "db"):
        assert mode_and_group(key)[1] == "ate", f"{key}: {lines[key]}"
    data_mode = mode_and_group("data")[0]
    assert data_mode & 0o2000, f"the data directory must carry the setgid bit: {lines['data']}"
    assert data_mode & 0o070, f"the data directory must be group-writable: {lines['data']}"
    assert mode_and_group("wal")[1] == "ate", f"a file created after setup must inherit the group: {lines['wal']}"
    # The config and its directory stay root-owned and not group-writable.
    config_mode, config_group = mode_and_group("config")
    dir_mode, dir_group = mode_and_group("configdir")
    assert dir_group == "root" and dir_mode & 0o777 == 0o755, lines["configdir"]
    assert config_group == "root" and config_mode & 0o777 == 0o644, lines["config"]

@dataclass
class Example:
    """One manual example, the run the harness makes of it, and what the manual claims."""

    file: str
    schedule: str
    result: str
    end_reason: str
    #: Fully prefixed Step Number -> Result, for ordinary steps only.
    steps: dict[str, str]
    inputs: dict[str, Any] = field(default_factory=dict)
    bad_inputs: dict[str, Any] = field(default_factory=dict)
    choices: list[str] = field(default_factory=list)
    enable: dict[str, bool] = field(default_factory=dict)
    polls: dict[str, tuple[int, str]] = field(default_factory=dict)
    #: Substrings that must appear in a WARNING line the run records.
    warns: list[str] = field(default_factory=list)
    equipment: int = 0
    #: (phase, outcome) for every Action row, in the order they ran.
    actions: tuple[tuple[str, str], ...] = ()


EXAMPLES_RUN: list[Example] = [
    Example(
        file="first.tslua",
        schedule="Manual: first schedule",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS"},
    ),
    Example(
        file="steps.tslua",
        schedule="Manual: steps",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS", "2.": "PASS", "3.": "NOTRUN", "4.": "NOREPORT"},
        warns=["never reported"],
    ),
    Example(
        file="inputs.tslua",
        schedule="Manual: step inputs",
        result="PASS",
        end_reason="COMPLETED",
        steps={n: "PASS" for n in ("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.")},
        inputs={
            "Pick one": "Green",
            "Tick several": ["A", "B"],
            "Press a button": "RETEST",
            "Whole number": 42,
            "Decimal": 1.25,
            "Text with a pattern": "AB-1234",
            "Blocking prompt": "CONTINUE",
        },
        bad_inputs={"Whole number": 500, "Decimal": 9.0, "Text with a pattern": "not a part number"},
        polls={"POLL in a loop": (1, "STOP")},
    ),
    Example(
        file="actions.tslua",
        schedule="Manual: Actions",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS"},
        equipment=1,
        actions=(("pre", "ok"), ("post", "ok")),
    ),
    Example(
        file="retry.tslua",
        schedule="Manual: retry",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS", "2.": "PASS"},
        choices=["RETRY"],
    ),
    Example(
        file="results.tslua",
        schedule="Manual: every Result",
        result="FAIL",
        end_reason="COMPLETED",
        steps={
            "1.": "PASS", "2.": "FAIL", "3.": "ERROR", "4.": "SKIPPED",
            "5.": "NOTRUN", "6.": "FORCED_NOTRUN", "7.": "NOREPORT",
        },
        warns=["never reported"],
    ),
    Example(
        file="main.tslua",
        schedule="Manual: sub-schedules",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS", "2.1.": "PASS", "2.2.1.": "PASS", "3.1.": "NOTRUN", "3.2.1.": "NOTRUN"},
        # main.tslua declares no Actions of its own; the two channel instances each have a
        # Pre-test and Post-test Action, and the disabled header's instance is skipped, so the
        # run records the enabled instance's pair only.
        actions=(("pre", "ok"), ("post", "ok")),
    ),
    Example(
        file="modules.tslua",
        schedule="Manual: modules",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS", "2.": "PASS"},
    ),
    Example(
        file="tcp.tslua",
        schedule="Manual: TCP",
        result="PASS",
        end_reason="COMPLETED",
        steps={n: "PASS" for n in ("1.", "2.", "3.", "4.", "5.")},
        equipment=1,
        warns=["timed out after", "no reply from the instrument"],
    ),
    Example(
        file="udp.tslua",
        schedule="Manual: UDP",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS", "2.": "PASS"},
        warns=["datagram truncated"],
    ),
    Example(
        file="serial.tslua",
        schedule="Manual: SERIAL",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS"},
    ),
    Example(
        file="visa.tslua",
        schedule="Manual: VISA",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS"},
    ),
    Example(
        file="errors.tslua",
        schedule="Manual: Device errors",
        result="FAIL",
        end_reason="COMPLETED",
        steps={"1.": "PASS", "2.": "ERROR", "3.": "ERROR", "4.": "PASS"},
        warns=["nothing is listening", "the Device is gone"],
    ),
    Example(
        file="left_open.tslua",
        schedule="Manual: a Device left open",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS"},
        warns=["left open at end of run"],
    ),
    Example(
        file="logging.tslua",
        schedule="Manual: logging",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS", "2.": "PASS"},
    ),
    Example(
        file="sandbox.tslua",
        schedule="Manual: the sandbox",
        result="PASS",
        end_reason="COMPLETED",
        steps={"1.": "PASS", "2.": "PASS"},
    ),
]


@pytest.fixture(scope="module")
def bench(tmp_path_factory):
    """The simulator peers and a config for the manual's Media Release, for the whole module.

    Same shape as ``examples/harness``: the peers bind ephemeral ports, their addresses go into
    the environment, and the shipped example files run unchanged. ``ATE_DEAD_PORT`` is a port
    nothing listens on, for the schedule that shows an unreachable Device.
    """
    import os

    peers = Peers()
    peers.start()
    env = {
        "ATE_SCPI_HOST": "127.0.0.1",
        "ATE_SCPI_PORT": str(peers.scpi.port),
        "ATE_UDP_HOST": "127.0.0.1",
        "ATE_UDP_PORT": str(peers.udp.port),
        "ATE_DEAD_PORT": str(dead_port()),
    }
    saved = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        work = tmp_path_factory.mktemp("manual-examples")
        yield work, write_config(work, EXAMPLES_DIR)
    finally:
        peers.stop()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _step_results(record: dict[str, Any]) -> dict[str, str]:
    """Step Number -> Result for ordinary steps; header rows have no Result."""
    return {node["step_number"]: node["result"] for node in record["nodes"] if node["kind"] == "step"}


@pytest.mark.parametrize("example", EXAMPLES_RUN, ids=lambda e: e.file)
def test_a_manual_example_runs_as_the_manual_says(bench, example: Example) -> None:
    """Run one example headless and check the Test Run record against what the manual says."""
    _work, config = bench
    record = run_schedule(
        config,
        example.schedule,
        inputs=example.inputs or None,
        bad_inputs=example.bad_inputs or None,
        choices=example.choices or None,
        enable=example.enable or None,
        polls=example.polls or None,
        release_dir=EXAMPLES_DIR,
    )
    run = record["run"]
    assert run["result"] == example.result, f"{example.file}: run result {run['result']}"
    assert run["end_reason"] == example.end_reason, f"{example.file}: end reason {run['end_reason']}"
    assert _step_results(record) == example.steps, f"{example.file}: step results"
    assert len(record["equipment"]) == example.equipment, f"{example.file}: Equipment rows"
    if example.actions:
        got = tuple((action["phase"], action["outcome"]) for action in record["actions"])
        assert got == example.actions, f"{example.file}: Actions recorded {got}"
    for want in example.warns:
        logged = [line["text"] for line in record["logs"] if line["level"] == "WARNING"]
        assert any(want in text for text in logged), f"{example.file}: no WARNING containing {want!r}"


def test_retry_records_two_attempts_and_the_operators_choice(bench) -> None:
    """The retry example's step has two Attempts, and the Operator's Retry is recorded on the first."""
    _work, config = bench
    record = run_schedule(config, "Manual: retry", choices=["RETRY"], release_dir=EXAMPLES_DIR)
    retried = next(n for n in record["nodes"] if n["step_number"] == "1.")
    attempts = [a for a in record["attempts"] if a["node_seq"] == retried["node_seq"]]
    assert len(attempts) == 2, f"expected two Attempts, got {len(attempts)}"
    assert attempts[0]["pass_fail"] == "FAIL" and attempts[0]["prompt_choice"] == "RETRY"
    assert attempts[1]["pass_fail"] == "PASS" and attempts[1]["prompt_choice"] is None
    assert retried["attempt_count"] == 2 and retried["result"] == "PASS"


def test_an_abort_ends_the_run_and_the_posttest_action_still_runs(bench) -> None:
    """Story 83: an abort stops the run (within 10 s, aiming at 0.5 s), the Post-test Action runs
    afterwards all the same, and every step that never ran records NOTRUN."""
    from ate.config import read_config
    from ate.run_session.headless import HeadlessRunSession, SessionState
    from PySide6.QtCore import QCoreApplication
    from tests._fixtures import ABORT_MUST_STOP_S, wait_for_aborted_run

    _work, config = bench
    session = HeadlessRunSession(read_config(config))
    states: list[SessionState] = []
    session.state_changed.connect(states.append)
    session.set_operator("Ada Lovelace")
    session.set_project("Manual examples")
    session.set_uut_type("Example board")
    session.set_uut_serial("EX-MANUAL-1")
    session.select_schedule("Manual: abort")
    session.start()

    app = QCoreApplication.instance() or QCoreApplication([])
    deadline = time.monotonic() + ABORT_MUST_STOP_S
    while SessionState.STARTED not in states and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert SessionState.STARTED in states, "the abort example never started its run"

    finished = threading.Event()
    session.state_changed.connect(lambda state: finished.set() if state is SessionState.FINISHED else None)
    t0 = time.monotonic()
    session.request_abort()
    elapsed = wait_for_aborted_run(app, finished, t0)

    run_uuid = session.run_uuid
    run = session.store.get_test_run(run_uuid)
    assert run["result"] == "FAIL" and run["end_reason"] == "ABORTED", f"run row: {run}"
    nodes = {node["step_number"]: node for node in session.store.nodes(run_uuid) if node["kind"] == "step"}
    assert nodes["1."]["result"] == "ERROR", "the step running when the abort landed records ERROR"
    assert nodes["1."]["attempt_count"] == 1
    assert nodes["2."]["result"] == "NOTRUN", "a step that never ran records NOTRUN"
    actions = session.store.actions(run_uuid)
    assert len(actions) == 1 and actions[0]["phase"] == "post" and actions[0]["outcome"] == "ok", actions
    logged = [line["text"] for line in session.store.log_lines(run_uuid)]
    assert any("post-test action ran after the abort" in text for text in logged), logged
    session.close()


def test_a_schedule_that_loads_bad_is_listed_as_broken() -> None:
    """``forbidden.tslua`` opens a Device while the file is loading, which the host refuses: the
    schedule is listed as broken, and the rest of its Media Release is unaffected."""
    from ate.catalog import list_schedule_rows

    rows = list_schedule_rows(EXAMPLES_DIR)
    broken = [row for row in rows if row.error]
    assert len(broken) == 1, f"expected one broken schedule, got {[(r.name, r.error) for r in broken]}"
    assert "only legal after the schedule has loaded" in broken[0].error
    listed = {row.name for row in rows} - {row.name for row in broken}
    ran = {example.schedule for example in EXAMPLES_RUN} | {"Manual: abort"}
    assert listed == ran, f"schedules listed but never run, or run but not listed: {listed ^ ran}"


def test_a_checksum_mismatch_is_recorded_and_warned(bench) -> None:
    """Change one sha256 in a scratch copy of the Test Definition: the file still loads, the run
    records the mismatch, and the Run log says so."""
    work, _config = bench
    release = work / "mismatch-release"
    shutil.copytree(EXAMPLES_DIR, release)
    atedef = json.loads((release / "manual.atedef").read_text(encoding="utf-8"))
    for entry in atedef["files"]:
        if entry["path"] == "first.tslua":
            entry["sha256"] = "0" * 64
    (release / "manual.atedef").write_text(json.dumps(atedef), encoding="utf-8", newline="\n")

    mismatch_dir = work / "mismatch-config"
    mismatch_dir.mkdir()
    config = json.loads(write_config(mismatch_dir, release).read_text(encoding="utf-8"))
    # A mismatch only asks when the config says "ask"; this run stores the bytes so the record can
    # be checked for the stored content as well as the warning.
    config["database"]["storeMismatchedFiles"] = "always"
    (mismatch_dir / "config.json").write_text(json.dumps(config), encoding="utf-8", newline="\n")
    record = run_schedule(mismatch_dir / "config.json", "Manual: first schedule", release_dir=release)
    row = next(f for f in record["run_files"] if f["path"] == "first.tslua")
    assert row["mismatch"], f"first.tslua recorded no mismatch: {row}"
    logged = [line["text"] for line in record["logs"] if line["level"] == "WARNING"]
    assert any("checksum mismatch for first.tslua" in text for text in logged), logged
    assert record["run"]["result"] == "PASS", "a mismatched file still loads, and the run still passes"


def test_the_test_definition_lists_every_example_file() -> None:
    """Every Lua file beside the manual's ``.atedef`` is listed by it, and only those files."""
    atedef = json.loads((EXAMPLES_DIR / "manual.atedef").read_text(encoding="utf-8"))
    listed = {entry["path"] for entry in atedef["files"]}
    on_disk = {
        p.relative_to(EXAMPLES_DIR).as_posix()
        for p in EXAMPLES_DIR.rglob("*")
        if p.suffix in (".tslua", ".lua")
    }
    assert listed == on_disk, f"Test Definition and directory disagree: {listed ^ on_disk}"
    hidden = {entry["path"] for entry in atedef["files"] if entry.get("hidden")}
    assert hidden == {"channel.tslua", "led.tslua"}, "only the Sub-schedules are hidden"
    for entry in atedef["files"]:
        computed = hashlib.sha256((EXAMPLES_DIR / entry["path"]).read_bytes()).hexdigest()
        assert entry["sha256"] == computed, f"{entry['path']} checksum is stale: run 'ate definition update'"


# --- The manuals quote the examples exactly --------------------------------------------

#: A listing in the Markdown manual opens with the file it came from:
#: ```lua:docs/manuals/examples/test-writer/first.tslua
LISTING_FENCE = re.compile(r"^```lua:(\S+)\s*$")


def markdown_listings(text: str) -> dict[str, str]:
    """Every fenced Lua block in the Markdown manual, keyed by the file it came from."""
    blocks: dict[str, str] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = LISTING_FENCE.match(lines[index])
        if match:
            path = match.group(1)
            body: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                body.append(lines[index])
                index += 1
            blocks[path] = "\n".join(body) + "\n"
        index += 1
    return blocks


def example_files(manual: Manual) -> list[Path]:
    """Every Lua file in the manual's example directory."""
    return sorted(p for p in MANUAL_EXAMPLES[manual.name].rglob("*") if p.suffix in (".tslua", ".lua"))


def test_every_example_is_quoted_exactly_in_the_markdown_manual() -> None:
    """No listing in the Markdown manual may differ from the file it came from."""
    manual = MANUALS["test-writer"]
    blocks = markdown_listings(manual.markdown.read_text(encoding="utf-8"))
    expected = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in example_files(manual)}
    assert set(blocks) == set(expected), (
        f"the Markdown manual quotes {sorted(set(blocks) - set(expected))} that are not examples, "
        f"and omits {sorted(set(expected) - set(blocks))}"
    )
    for path, body in expected.items():
        assert blocks[path] == body, f"{path}: the listing in {manual.markdown} differs from the file"


def test_every_example_is_included_by_the_latex_manual() -> None:
    """The LaTeX manual includes every example with \\lstinputlisting, so the PDF shows the file
    as it is rather than a retyped copy of it."""
    manual = MANUALS["test-writer"]
    included: set[Path] = set()
    for tex in manual.tex_parts():
        for line in tex.read_text(encoding="utf-8").splitlines():
            if "\\lstinputlisting" in line:
                argument = line.split("{", 1)[1].rsplit("}", 1)[0]
                included.add((tex.parent / argument).resolve())
    expected = {p.resolve() for p in example_files(manual)}
    assert included == expected, (
        f"the LaTeX manual includes {sorted(str(p) for p in included - expected)} that are not examples, "
        f"and omits {sorted(str(p) for p in expected - included)}"
    )
