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
import ast
import hashlib
import importlib
import importlib.util
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
from ate.engine.run_engine import RunEngine
from ate.run_session.headless import HeadlessRunSession

from examples.harness.peers import Peers
from examples.harness.run_examples import bench_env, dead_port, run_schedule, write_config

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

    def code_forms(self) -> list[tuple[str, str]]:
        """The manual's two forms, each reduced to the text that is set as code.

        A name the reader can only find in prose is not a name the manual teaches: the coverage
        check needs the code form, so ``ATE.flush`` counts and "the schedule can flush a Device"
        does not. The LaTeX form is inline ``\\texttt`` and ``\\verb``, every ``verbatim`` and
        ``lstlisting`` block, and every example file the manual pulls in with
        ``\\lstinputlisting`` - the PDF really shows those bytes. The Markdown form is inline
        backticks and fenced blocks."""
        return [("LaTeX", self._latex_code()), ("Markdown", self._markdown_code())]

    def _latex_code(self) -> str:
        chunks: list[str] = []
        for tex in self.tex_parts():
            text = tex.read_text(encoding="utf-8")
            for line in text.splitlines():
                match = re.search(r"\\lstinputlisting(?:\[[^\]]*\])?\{([^}]*)\}", line)
                if match:
                    included = (tex.parent / match.group(1)).resolve()
                    if included.exists():
                        chunks.append(included.read_text(encoding="utf-8"))
            for environment in ("verbatim", "lstlisting"):
                chunks.extend(_environment_body(text, environment))
            chunks.extend(_inline_code(text))
        return "\n".join(chunks)

    def _markdown_code(self) -> str:
        text = self.markdown.read_text(encoding="utf-8")
        return "\n".join(re.findall(r"```[^\n]*\n(.*?)```", text, re.S) + re.findall(r"`([^`\n]+)`", text))


#: LaTeX that only affects how a line is broken up. Stripped by :meth:`Manual.texts`. The lookahead
#: is not ceremony: an underscore is a word character, so ``\allowbreak_`` would not match ``\b``.
_LAYOUT_MARKUP = re.compile(r"\\(?:allowbreak|texttt|textbf|text|emph|verb|sbox|par)(?![a-zA-Z])|[{}]")


def plain(text: str) -> str:
    """The manual's text without its line-breaking markup, so a name split across two runs matches."""
    return _LAYOUT_MARKUP.sub("", text.replace("\\_", "_"))


def _environment_body(text: str, environment: str) -> list[str]:
    """The contents of every ``\\begin{environment} ... \\end{environment}`` block."""
    return re.findall(
        rf"\\begin\{{{environment}\}}(?:\[[^\]]*\])?(.*?)\\end\{{{environment}\}}", text, re.S
    )


def _inline_code(text: str) -> list[str]:
    """The argument of every ``\\texttt{...}``, braces matched by depth, plus every ``\\verb|...|``."""
    found: list[str] = []
    index = 0
    while True:
        start = text.find("\\texttt{", index)
        if start < 0:
            break
        depth = 0
        end = start + len("\\texttt{")
        while end < len(text):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                if depth == 0:
                    break
                depth -= 1
            end += 1
        found.append(text[start + len("\\texttt{"):end])
        index = end + 1
    found.extend(re.findall(r"\\verb\|([^|]*)\|", text))
    return [plain(chunk) for chunk in found]


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

DEVELOPER = Manual(
    name="developer",
    tex_dir=MANUALS_DIR / "developer",
    markdown=MANUALS_DIR / "llm/developer.md",
)

#: Every manual packet 34 built. Each has its own groups of code-derived names in COVERAGE below.
MANUALS: dict[str, Manual] = {"test-writer": TEST_WRITER, "admin": ADMIN, "developer": DEVELOPER}

#: The examples directory each manual is written against.
DEV_EXAMPLES_DIR = MANUALS_DIR / "examples/developer"
MANUAL_EXAMPLES: dict[str, Path] = {"test-writer": EXAMPLES_DIR, "developer": DEV_EXAMPLES_DIR}


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


def ate_modules() -> set[str]:
    """Every module under ``src/ate``, as the file path a reader would open.

    Walked from the package itself with :func:`pkgutil.walk_packages`, so a module added to the
    source is a module the developer manuals must name. A package is named by the file that makes it
    a package (``src/ate/devices/__init__.py``), which is how the manual's module table lists it.
    """
    import ate

    names: set[str] = set()
    for info in pkgutil.walk_packages(ate.__path__, "ate."):
        if info.name.endswith(".__init__"):
            continue
        dotted = info.name[len("ate."):] if info.name != "ate" else ""
        path = "src/ate/" + dotted.replace(".", "/")
        if info.name == "ate":
            names.add("src/ate/__init__.py")
            continue
        # A package is named by the file that makes it a package: `src/ate/devices/__init__.py`.
        names.add(path + "/__init__.py" if info.ispkg else path + ".py")
    names.add("src/ate/__init__.py")
    return names


def declared_signals(cls: type) -> set[str]:
    """The signals a class declares itself, not the ones Qt's ``QObject`` already has.

    ``vars(cls)`` is the class's own namespace, so ``finished``, ``started`` and ``destroyed`` -
    which every ``QThread`` and ``QObject`` carries - are not counted as ATE's signals. Those are
    real and used, but a manual that stops naming them is not behind the code.
    """
    from PySide6.QtCore import Signal

    return {name for name, member in vars(cls).items() if isinstance(member, Signal)}


def schema_tables() -> set[str]:
    """Every table the Test Run record has, read from the schema's own metadata."""
    from ate.storage.schema import metadata

    return set(metadata.tables)


def device_drivers() -> set[str]:
    """Every driver class, and the Device Type name it serves.

    Derived from the classes that subclass :class:`Driver`, so the Device Type a driver declares and
    the class that implements it both have to appear in the manuals.
    """
    import ate.devices

    names: set[str] = set()
    for info in pkgutil.iter_modules(ate.devices.__path__):
        module = importlib.import_module(f"ate.devices.{info.name}")
        for member in vars(module).values():
            if isinstance(member, type) and issubclass(member, Driver) and member is not Driver:
                names.add(member.name)
                names.add(member.__name__)
    return names


#: A group of code-derived names, the manual each must appear in, and the code form that counts.
#: ``ate_function`` wants the call as a reader types it, ``ATE.flush``; ``code`` is satisfied by
#: the name anywhere in a listing. 34c adds the developer manual's modules, signals, tables and
#: drivers to the same table. Same shape, same failure.
COVERAGE: list[tuple[str, str, Callable[[], set[str]], str]] = [
    ("test-writer", "ATE table functions", ate_table_functions, "ate_function"),
    ("test-writer", "enum tables and their members", enum_names, "code"),
    ("test-writer", "ScheduleParams and StepParams setters", builder_methods, "code"),
    ("test-writer", "Device Types", device_types, "code"),
    ("test-writer", "ATE.run fields", run_fields, "code"),
    ("admin", "config keys the validator reads", config_keys, "code"),
    ("admin", "ate subcommands and flags", cli_names, "code"),
    ("developer", "every module under src/ate", ate_modules, "code"),
    ("developer", "RunEngine signals", lambda: declared_signals(RunEngine), "code"),
    ("developer", "HeadlessRunSession signals", lambda: declared_signals(HeadlessRunSession), "code"),
    ("developer", "every table in the Test Run record", schema_tables, "code"),
    ("developer", "Device drivers and the Device Types they serve", device_drivers, "code"),
]


def _pattern(name: str, style: str) -> str:
    """How a name is recognised in code, including the trouble a flag causes.

    A flag is not a word: there is no word boundary before the dashes of ``--yes``, and the
    ``-y`` inside ``--yes`` is not the short flag, so a flag has to start after something that is
    neither a letter, a digit nor another dash.
    """
    if style == "ate_function":
        return rf"ATE\.{re.escape(name)}\b"
    start = r"(?<![-\w])" if name.startswith("-") else r"\b"
    return f"{start}{re.escape(name)}\\b"


def missing_from_code(forms: Iterable[tuple[str, str]], names: Iterable[str], style: str = "code") -> list[str]:
    """The names no form of the manual sets as code.

    Prose that says the word does not count: a name a reader can only find in a sentence is not a
    name the manual teaches, because the reader cannot copy it. ``forms`` is the output of
    :meth:`Manual.code_forms`, or one of its two entries when a single form is being tested."""
    remaining = set(names)
    for _label, text in forms:
        for name in list(remaining):
            if re.search(_pattern(name, style), text):
                remaining.discard(name)
    return sorted(remaining)


def test_no_name_the_code_defines_is_missing_from_its_manual() -> None:
    """Every ATE function, enum, setter, Device Type, run field, key and flag is in code, twice."""
    for manual_name, group, derive, style in COVERAGE:
        for label, code in MANUALS[manual_name].code_forms():
            gone = missing_from_code([(label, code)], derive(), style)
            if gone:
                raise AssertionError(
                    f"the {manual_name} manual names {group} only in prose ({label}): {', '.join(gone)}"
                )


def test_the_coverage_check_can_fail() -> None:
    """Show the check bites: the same names against a manual with one removed reports it.

    This is not a bug report; it is the proof that a manual quietly losing a function is caught,
    in the LaTeX form and in the Markdown form separately."""
    names = ate_table_functions()
    assert "addEquipment" in names
    for label, code in MANUALS["test-writer"].code_forms():
        assert not missing_from_code([(label, code)], names, "ate_function"), f"{label} has every name"
        stripped = code.replace("ATE.addEquipment", "ATE.somethingElse")
        assert missing_from_code([(label, stripped)], names, "ate_function") == ["addEquipment"]


def test_the_admin_coverage_check_can_fail() -> None:
    """The config-key check fails on a manual that drops a key, which is what it is for.

    Delete one line of the key table and the suite says which key went missing; the check is not
    a table nobody reads."""
    keys = config_keys()
    assert "ui.allowHistoryDuringRun" in keys, "the schema changed shape; the derivation is wrong"
    for label, code in MANUALS["admin"].code_forms():
        stripped = code.replace("allowHistoryDuringRun", "somethingElse")
        assert missing_from_code([(label, stripped)], keys) == ["ui.allowHistoryDuringRun"]
        assert not missing_from_code([(label, code)], keys)


def test_a_word_in_a_sentence_is_not_a_name_in_code() -> None:
    """``sleep`` and ``flush`` are ordinary English words, so only the code form proves them.

    The old check accepted the word anywhere, which meant a manual could cover ``ATE.flush`` by
    saying "flush the Device" and never show the call. Deleting the call from each form of the
    test-writer manual proves the sentence alone is no longer enough."""
    sentences = "Flushing the Device and sleeping between steps are both wise: flushing keeps the buffer clean."
    assert missing_from_code([("prose", sentences)], {"sleep", "flush"}) == ["flush", "sleep"]
    for label, code in MANUALS["test-writer"].code_forms():
        assert not missing_from_code([(label, code)], {"flush"}), f"{label} does not show flush at all"
        stripped = code.replace("flush", "somethingElse")
        assert "flush" in missing_from_code([(label, stripped)], {"flush"}), f"{label} kept flush from outside its code"


def test_the_agent_manual_names_every_key_and_command_on_its_own() -> None:
    """An agent working from ``docs/manuals/llm/admin.md`` alone must not need the PDF.

    The coverage table counts a name as covered if it appears in code anywhere in the manual;
    this checks the Markdown file's code by itself, because that is the file an agent is given."""
    agent = [(label, text) for label, text in MANUALS["admin"].code_forms() if label == "Markdown"]
    gone = missing_from_code(agent, config_keys() | cli_names())
    assert not gone, f"the agent manual alone is missing from its code: {', '.join(gone)}"


# --- Every command the admin manual shows is run, or explained --------------------------

#: A listing in a manual whose lines are commands a reader types: the LaTeX form and the Markdown form.
SHELL_LISTING = re.compile(r"\\begin\{lstlisting\}\[language=ATEShell\](.*?)\\end\{lstlisting\}", re.S)
MARKDOWN_SHELL = re.compile(r"```bash\n(.*?)```", re.S)

#: A listing line is a command when it starts with one of these, with an environment assignment such
#: as ``QT_QPA_PLATFORM=offscreen``, or with an artifact name such as `ate-1.2.3-windows-x64-setup.exe`.
#: Everything else in a listing is a message the machine printed.
COMMAND_TOKENS = {"ate", "sudo", "apt-get", "pipx", "python", "pytest", "./install.sh"}


def shell_commands(manual: Manual) -> list[str]:
    """Every command line in a manual's listings, LaTeX and Markdown, comment stripped.

    ``ate setup: running unprivileged ...`` looks like a command and is not: it is what setup
    printed. A token ending in a colon at the second position is the output of the command named
    first."""
    lines: list[str] = []
    for tex in manual.tex_parts():
        for block in SHELL_LISTING.findall(tex.read_text(encoding="utf-8")):
            lines.extend(block.splitlines())
    if manual.markdown.exists():
        for block in MARKDOWN_SHELL.findall(manual.markdown.read_text(encoding="utf-8")):
            lines.extend(block.splitlines())
    commands: list[str] = []
    for line in (line.split("#", 1)[0].strip() for line in lines):
        tokens = line.split()
        # An environment assignment in front of the command is part of the command, not the command:
        # `QT_QPA_PLATFORM=offscreen python -m pytest` is the pytest command with the platform pinned.
        while tokens and "=" in tokens[0] and not tokens[0].startswith("ate-"):
            tokens = tokens[1:]
        if not tokens or (tokens[0] not in COMMAND_TOKENS and not tokens[0].startswith("ate-")):
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
    ("ate db status --config /tmp/station/config/config.json",
     ["db", "status", "--config", "{config}"], 0, "", "{station}"),
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
        "Windows only. The flags are packaging/windows/smoke_installer.py's INSTALL_FLAGS, which the "
        "test below matches against the manual and tests/test_release_ci_27c.py asserts exist",
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
    "ate _pdf report.html report.pdf --paper A4":
        "not for humans to run, as the manual says; it is started by the report writer, and its "
        "messages are checked against src/ate/report/pdf.py below",
    # The Markdown manual writes its commands with placeholders (DIR, PATH, <file.atedef>) so an
    # agent can adapt them; each is the same command as one executed above, or a command that only
    # the packager or CI can run. Listing them keeps the accounting honest for both manuals.
    "ate --config PATH":
        "the placeholder form of `ate --config <file> db status`, which is run above",
    "ate setup --root DIR":
        "the placeholder form of the `--root` line above, which is run in a temporary Station",
    "ate setup --data-dir DIR --config-dir DIR":
        "run by tests/test_cli_setup.py, which points both directories at a temporary directory",
    "ate setup --no-permissions":
        "run, as part of the `--root` line above",
    "ate db status [PATH]":
        "both forms are run above: `ate db status` on a prepared database and `ate db status` on a "
        "missing one",
    "ate db migrate [PATH]":
        "the placeholder form of `ate db migrate /tmp/station/data/ate.db --yes`, which is run above",
    "ate definition update <file.atedef>":
        "the placeholder form of the example Media Release's `release.atedef` update run above",
    "ate _pdf <html> <pdf> --paper A4|Letter [--unsandboxed]":
        "the same helper as above, written with placeholders; started by the report writer and "
        "asserted in tests/test_pdf_sandbox_choice_30d.py",
    "ate _selftest --scan DIR [--config PATH]":
        "run above against the example release; the placeholder form is the same command",
    "python docs/manuals/build.py admin":
        "builds the admin manual's PDF in Docker with texlive. It is run by hand before anyone reads "
        "the PDF; the suite checks the sources, not the rendered pages",
    "QT_QPA_PLATFORM=offscreen python -m pytest tests/test_manuals.py -q":
        "the suite cannot run itself from inside a test. It is the command CI runs (the 'Run tests' "
        "step of .github/workflows/ci.yml)",
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
    code, _out, err = run_ate(["setup", "--root", str(root), "--no-permissions"], root)
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
    stale = sorted(set(COMMANDS_NOT_RUN) - shown)
    assert not stale, f"reasons kept for commands the manual no longer prints: {stale}"


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


def test_the_config_flag_works_before_or_after_the_subcommand(station) -> None:
    """The manual says `--config` may sit before or after the subcommand, and the code proves it.

    Packet 35: written after the subcommand the token used to be read as a database path, so
    `ate db status --config X` opened X as the database and died on an unhandled
    `sqlite3.DatabaseError`. Both positions now read the config, and a path that is not a
    database is refused in one line."""
    from ate.storage.schema import SCHEMA_VERSION

    config = station / "config" / "config.json"
    code, out, err = run_ate(["db", "status", "--config", str(config)], station)
    assert code == 0 and out.strip() == str(SCHEMA_VERSION), f"expected the config to be read, got {err[-300:]}"

    not_a_db = station / "data" / "not-a-database.db"
    not_a_db.write_text("this is a text file, not a database", encoding="utf-8", newline="\n")
    code, out, err = run_ate(["db", "status", str(not_a_db)], station)
    assert code == 1 and "is not a SQLite database" in err, f"a text file is not a database: {err[-300:]}"
    assert "Traceback" not in err, err

    manual = "\n".join(text for _label, text in MANUALS["admin"].texts())
    assert "ate db status --config PATH" in manual, "the manual no longer shows the flag after the subcommand"


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


def test_the_windows_silent_install_line_matches_what_ci_passes() -> None:
    """The one command the manual shows that this machine can never run is still checked.

    The silent-install line is compared with the flags `packaging/windows/smoke_installer.py`
    actually passes to the installer on the Windows runner: a manual that changes the flags, or
    the artifact name, is caught here rather than by someone reading a PDF."""
    script = ROOT / "packaging" / "windows" / "smoke_installer.py"
    spec = importlib.util.spec_from_file_location("smoke_installer", script)
    smoke = importlib.util.module_from_spec(spec)
    sys.modules["smoke_installer"] = smoke  # the script's @dataclass classes need their module to exist
    spec.loader.exec_module(smoke)

    line = next(
        (line for line in shell_commands(MANUALS["admin"]) if line.endswith(" ".join(smoke.INSTALL_FLAGS))),
        None,
    )
    assert line, f"the manual no longer shows the flags the CI uses: {smoke.INSTALL_FLAGS}"
    name, _, flags = line.partition(" ")
    assert re.fullmatch(r"ate-\d+\.\d+\.\d+-windows-x64-setup\.exe", name), f"{name} is not an ATE installer name"
    assert flags == " ".join(smoke.INSTALL_FLAGS)


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
    ("'reports.paperSize' must be 'A4' or 'Letter'", "'reports.paperSize' must be 'A4' or 'Letter'", "src/ate/config.py"),
    ("has unknown placeholder '<datat>'", "has unknown placeholder {token!r}", "src/ate/config.py"),
    ("invalid JSON in config file", "invalid JSON in config file", "src/ate/config.py"),
    ("cannot write station.uuid to /etc/ate/config.json", "cannot write station.uuid to", "src/ate/config.py"),
    ("run `ate db migrate` to create or upgrade the database", "run `{hint}` to create or upgrade the database", "src/ate/storage/store.py"),
    ("this ATE is too old for the database", "this ATE is too old for", "src/ate/storage/store.py"),
    ("cannot open the database at /var/lib/ate/ate.db", "cannot open the database at {self.path}", "src/ate/storage/store.py"),
    ("is not a SQLite database", "is not a SQLite database", "src/ate/storage/store.py"),
    ("this ATE has no migration from version 4 to 5", "no migration from version {stored} to {SCHEMA_VERSION}", "src/ate/storage/store.py"),
    ("backed up /var/lib/ate/ate.db to", "backed up {path} to", "src/ate/storage/backup.py"),
    ("refusing to migrate: cannot write a backup of", "cannot write a backup of {path} to", "src/ate/storage/backup.py"),
    ("nothing in the database was changed", "nothing in the database was changed", "src/ate/storage/backup.py"),
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


def test_the_field_tables_name_format_rather_than_a_listing_body() -> None:
    """`format` has to be explained as a field, not merely seen inside a listing.

    ``config_keys()`` counts a key as covered when the manual sets it as code anywhere, and every
    JSON listing body carries ``"format": 1``, so a manual that never explained the key would pass
    on the bytes of a listing. Requiring it in the table or the field list is simple, so it is
    required here; the other keys are left to the coverage check, which cannot tell a table row
    from a listing without more machinery than this file wants."""
    tex = (MANUALS_DIR / "admin/04-the-config-file.tex").read_text(encoding="utf-8")
    md = MANUALS["admin"].markdown.read_text(encoding="utf-8")
    assert re.search(r"\\texttt\{format\}\s*&", tex), "the admin manual's config table has no row for format"
    assert re.search(r"\|\s*`format`\s*\|", md), "the agent manual's config table has no row for format"

    # The same for the `.atedef` key: outside the verbatim or fenced JSON, the manual must say what
    # `format` is.
    atedef_tex = (MANUALS_DIR / "test-writer/09-modules-and-test-definitions.tex").read_text(encoding="utf-8")
    outside = re.sub(r"\\begin\{verbatim\}.*?\\end\{verbatim\}", "", atedef_tex, flags=re.S)
    assert "\\texttt{format}" in outside, "the test-writer manual explains format only inside a listing"
    outside_md = re.sub(r"```[^\n]*\n.*?```", "", MANUALS["test-writer"].markdown.read_text(encoding="utf-8"), flags=re.S)
    assert "`format`" in outside_md, "the agent manual explains format only inside a listing"


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
    (work / "check.py").write_text(CONTAINER_PERMISSIONS_SCRIPT, encoding="utf-8", newline="\n")

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
LISTING_FENCE = re.compile(r"^```(?:lua|python|json|ateshell|text):(\S+)\s*$")


def markdown_listings(text: str) -> dict[str, str]:
    """Every fenced listing in the Markdown manual that names its file, keyed by that path."""
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
    """Every file in the manual's example directory that the manual quotes.

    Schedules, Lua modules, the Test Definition beside them, and the scripts a reader runs: all of
    them appear in both forms of the manual, so all of them are compared with what is on disk."""
    return sorted(
        p for p in MANUAL_EXAMPLES[manual.name].rglob("*")
        if p.suffix in (".tslua", ".lua", ".py", ".atedef")
    )


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
        for match in re.findall(r"\\lstinputlisting(?:\[[^\]]*\])?\{([^}]*)\}", tex.read_text(encoding="utf-8")):
            included.add((tex.parent / match).resolve())
    expected = {p.resolve() for p in example_files(manual)}
    assert included == expected, (
        f"the LaTeX manual includes {sorted(str(p) for p in included - expected)} that are not examples, "
        f"and omits {sorted(str(p) for p in expected - included)}"
    )

# --- The script the test-writer manual hands a reader actually runs ----------------------

#: A Media Release the manuals never quote, built for this test: an input keyed by Step Name, a
#: step that fails once and passes on Retry, and a step the Operator turns off. The script in
#: chapter 2 has to work on a release nobody has ever seen, otherwise it is a decoration.
SCRATCH_SCHEDULE = """-- scratch.tslua: somebody else's schedule, run by the manual's script.
local tries = 0

return ScheduleParams("Scratch: a release of my own", "inputs, a Retry and a disabled step")
  :setStopOnFail(true)
  :setSchedule({
    StepParams("Check the rail"):setStepNo("1.")
      :setInputs({ [1] = { desc = "rail voltage, V", type = "FLOAT", range = { min = 0.5, max = 2.5 } } })
      :setAction(function()
        ATE.report(Result.PASS, "the rail is at " .. ATE.input(1) .. " V")
      end),

    StepParams("A check that needs a retry"):setStepNo("2.")
      :setAction(function()
        tries = tries + 1
        if tries == 1 then
          ATE.report(Result.FAIL, "the first attempt chatter")
        else
          ATE.report(Result.PASS, "the second attempt is clean")
        end
      end),

    StepParams("Not run today"):setStepNo("3.")
      :setAction(function()
        ATE.report(Result.PASS, "this step ran only because the Operator left it on")
      end),
  })
"""


def scratch_release(directory: Path, body: str = SCRATCH_SCHEDULE, name: str = "my") -> Path:
    """A one-file Media Release in ``directory``, with a Test Definition listing it."""
    release = directory / f"{name}-release"
    release.mkdir()
    (release / "scratch.tslua").write_text(body, encoding="utf-8", newline="\n")
    definition = {
        "format": 1,
        "mediaNumber": f"MR-{name.upper()}",
        "name": "A release of my own",
        "version": "1.0.0",
        "files": [{"path": "scratch.tslua", "sha256": hashlib.sha256(body.encode()).hexdigest()}],
    }
    (release / f"{name}.atedef").write_text(json.dumps(definition, indent=2), encoding="utf-8", newline="\n")
    return release


def _run_manual_script(args: list[str]) -> subprocess.CompletedProcess:
    """Run ``run_your_schedule.py`` exactly as the manual tells a reader to run it."""
    return subprocess.run(
        [sys.executable, str(EXAMPLES_DIR / "run_your_schedule.py"), *args],
        capture_output=True, text=True, timeout=180, cwd=str(ROOT),
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )


def test_the_manuals_script_runs_a_release_the_manuals_never_quote(tmp_path: Path) -> None:
    """Chapter 2's script answers an input, takes a Retry, and turns a step off."""
    proc = _run_manual_script([
        "--release", str(scratch_release(tmp_path)),
        "--schedule", "Scratch: a release of my own",
        "--input", "Check the rail=1.25",
        "--choice", "RETRY",
        "--enable", "Not run today=false",
    ])
    assert proc.returncode == 0, proc.stderr[-600:]
    assert "result=PASS  end_reason=COMPLETED" in proc.stdout, proc.stdout
    assert '"1."' in proc.stdout and "PASS" in proc.stdout, proc.stdout
    assert "NOTRUN" in proc.stdout, "the disabled step is not in the printed step results"
    assert "report:" in proc.stdout, proc.stdout


def test_the_manuals_script_says_so_when_an_input_is_never_answered(tmp_path: Path) -> None:
    """The manual warns that an unanswered Step Input hangs the worker; the script proves it.

    No ``--input`` is given, so the step blocks in ``ATE.input`` and the script exits 1 saying the
    schedule never finished, exactly as the manual tells the reader to expect."""
    proc = _run_manual_script([
        "--release", str(scratch_release(tmp_path)),
        "--schedule", "Scratch: a release of my own",
        "--choice", "RETRY",
    ])
    assert proc.returncode == 1, proc.stdout[-400:]
    assert "never finished" in proc.stderr, proc.stderr[-400:]
    assert "unanswered Step Input" in proc.stderr, proc.stderr[-400:]


#: A release with an INTEGER input and a step whose loop reads its **second** POLL button: the two
#: things chapter 2's script has to get right are what it says when a declaration rejects an answer,
#: and which POLL input `--poll` means when a step declares more than one.
POLL_AND_WHOLE_NUMBER_SCHEDULE = """-- scratch.tslua: an INTEGER input and a POLL step with two buttons.
return ScheduleParams("Scratch: a whole number and two buttons", "refused answers and POLL indexes")
  :setSchedule({
    StepParams("Whole number"):setStepNo("1.")
      :setInputs({ [1] = { desc = "how many", type = "INTEGER", range = { min = 1, max = 10 } } })
      :setAction(function()
        ATE.report(Result.PASS, "the number is " .. ATE.input(1))
      end),

    StepParams("Two buttons"):setStepNo("2.")
      :setInputs({ [1] = { desc = "the button nobody reads", type = "POLL", prompt = {"A", "B"}, orient = "HORIZ" },
                   [2] = { desc = "the button the loop reads", type = "POLL", prompt = {"C", "STOP"}, orient = "HORIZ" } })
      :setAction(function()
        local clicked, reads = nil, 0
        while reads < 12 do
          reads = reads + 1
          clicked = ATE.input(2)
          if clicked then break end
          ATE.sleep(0.1)
        end
        if clicked then
          ATE.report(Result.PASS, "the second button said " .. clicked)
        else
          ATE.report(Result.FAIL, "the second button was never clicked")
        end
      end),
  })
"""


def test_a_refused_answer_is_reported_and_not_traced_back(tmp_path: Path) -> None:
    """``--input`` with a value the declaration rejects exits 1 naming the value and the rule.

    The refusal is raised in the ``input_asked`` slot, in the worker thread, so the script has to
    catch it there: uncaught it is a traceback and a run that only times out."""
    proc = _run_manual_script([
        "--release", str(scratch_release(tmp_path, POLL_AND_WHOLE_NUMBER_SCHEDULE, name="poll")),
        "--schedule", "Scratch: a whole number and two buttons",
        "--input", "Whole number=42.0",
    ])
    assert proc.returncode == 1, proc.stdout[-400:]
    assert "refused" in proc.stderr and "Enter a whole number" in proc.stderr, proc.stderr[-400:]
    assert "Traceback" not in proc.stderr, proc.stderr[-400:]


def test_a_poll_can_name_which_input_of_the_step_it_clicks(tmp_path: Path) -> None:
    """``--poll "Step name=N=BUTTON"`` clicks that step's Nth POLL input; a bare name is input 1."""
    release = str(scratch_release(tmp_path, POLL_AND_WHOLE_NUMBER_SCHEDULE, name="poll"))
    named = _run_manual_script([
        "--release", release,
        "--schedule", "Scratch: a whole number and two buttons",
        "--input", "Whole number=7",
        "--poll", "Two buttons=2=STOP",
    ])
    assert named.returncode == 0, named.stderr[-400:]
    assert '"2.": "PASS"' in named.stdout, named.stdout

    unnamed = _run_manual_script([
        "--release", release,
        "--schedule", "Scratch: a whole number and two buttons",
        "--input", "Whole number=7",
        "--poll", "Two buttons=B",
    ])
    assert unnamed.returncode == 0, unnamed.stderr[-400:]
    assert '"2.": "FAIL"' in unnamed.stdout, unnamed.stdout

    bad = _run_manual_script([
        "--release", release,
        "--schedule", "Scratch: a whole number and two buttons",
        "--poll", "Two buttons=x=STOP",
    ])
    assert bad.returncode == 1, bad.stdout[-300:]
    assert "must be an integer" in bad.stderr, bad.stderr[-300:]


def test_the_developer_runner_reports_a_refused_input(tmp_path: Path) -> None:
    """``runner.py`` catches the refusal in the answering slot instead of printing a traceback."""
    src = _copy_src(tmp_path / "src")
    config = _echo_config(tmp_path, src)
    proc = subprocess.run(
        [sys.executable, str(RUNNER), str(config), "Developer: echo Device",
         "--input", "Operator reading=42.0"],
        capture_output=True, text=True, timeout=180, cwd=str(ROOT),
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(src)},
    )
    assert proc.returncode == 1, proc.stdout[-400:]
    assert "refused" in proc.stderr and "Enter a whole number" in proc.stderr, proc.stderr[-400:]
    assert "Traceback" not in proc.stderr, proc.stderr[-400:]


# --- The three things an abort does not stop, as the manual says ---------------------------

#: The driver the abort tests run in their own process: a headless session on a scratch release,
#: an Abort asked for a moment after the run starts, the step results printed. A schedule that
#: never returns cannot be watched from inside pytest, so these tests run it as a subprocess they
#: can kill, and read the database the killed process left behind.
ABORT_DRIVER = r"""
import sys, time
from pathlib import Path

root, release, work, schedule, abort_after = sys.argv[1:6]
for path in (Path(root) / "src", Path(root)):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from PySide6.QtCore import QCoreApplication
from ate.config import read_config
from ate.run_session.headless import HeadlessRunSession, SessionState
from examples.harness.peers import Peers
from examples.harness.run_examples import bench_env, pdf_stub, write_config

config = write_config(Path(work), Path(release))
peers = Peers()
peers.start()
session = HeadlessRunSession(read_config(config))
states = []
session.state_changed.connect(states.append)
session.set_operator("Abort probe")
session.set_project("Abort probe")
session.set_uut_type("Probe board")
session.set_uut_serial("PROBE-1")
session.select_schedule(schedule)
with bench_env(peers), pdf_stub():
    session.start()
    app = QCoreApplication.instance() or QCoreApplication([])
    t0 = time.monotonic()
    while SessionState.STARTED not in states and time.monotonic() - t0 < 10:
        app.processEvents()
        time.sleep(0.01)
    time.sleep(float(abort_after))
    t0 = time.monotonic()
    session.request_abort()
    while SessionState.FINISHED not in states and time.monotonic() - t0 < 60:
        app.processEvents()
        time.sleep(0.02)
    run = session.store.get_test_run(session.run_uuid)
    steps = {n["step_number"]: n["result"] for n in session.store.nodes(session.run_uuid) if n["kind"] == "step"}
    print(f"elapsed={time.monotonic() - t0:.2f} run={run['result']}/{run['end_reason']} steps={steps}")
session.close()
peers.stop()
"""

#: The loop the manual measures: 120 million iterations, the same work written plainly and written
#: inside a coroutine. A plain loop is cut off by the count hook; a coroutine is not hooked.
LOOP = "local acc = 0 for i = 1, 120000000 do acc = acc + i end ATE.report(Result.PASS, 'the loop finished on its own')"
COROUTINE_LOOP = ("local run = coroutine.wrap(function() " + LOOP + " end) run() "
                  "ATE.report(Result.PASS, 'the coroutine finished on its own')")

#: One long step and one after it: the abort's effect is visible in which of the two ran.
ABORT_SCHEDULE = """-- probe.tslua: what an abort can and cannot interrupt.
return ScheduleParams("Abort probe", "one long step, then a step after it")
  :setSchedule({
    StepParams("The long step"):setStepNo("1.")
      :setAction(function()
        %s
      end),
    StepParams("Afterwards"):setStepNo("2.")
      :setAction(function() ATE.report(Result.PASS, "the step after it ran") end),
  })
"""


def _run_abort_probe(tmp_path: Path, action: str, *, abort_after: float = 0.4, timeout: int = 90) -> subprocess.CompletedProcess:
    """Run one schedule through the driver above and return the process."""
    release = scratch_release(tmp_path, ABORT_SCHEDULE % action, name="abort")
    work = tmp_path / "station"
    work.mkdir()
    driver = tmp_path / "abort_driver.py"
    driver.write_text(ABORT_DRIVER, encoding="utf-8", newline="\n")
    return subprocess.run(
        [sys.executable, str(driver), str(ROOT), str(release), str(work), "Abort probe", str(abort_after)],
        capture_output=True, text=True, timeout=timeout, cwd=str(ROOT),
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )


def _open_run_row(database: Path) -> dict[str, object]:
    """The newest Test Run row in a database a probe left behind, read in its own process.

    The pytest process must not hold that file: the driver subprocess died with the handle still
    open, and a handle here would trip conftest's leak check on Windows."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sqlite3,sys; row=sqlite3.connect(sys.argv[1]).execute("
         "'SELECT result, end_reason, ended_at FROM test_run ORDER BY started_at DESC LIMIT 1').fetchone(); "
         "print(row)", str(database)],
        capture_output=True, text=True, timeout=30, cwd=str(ROOT),
    )
    row = eval(proc.stdout.strip()) if proc.stdout.strip() else None
    return dict(zip(("result", "end_reason", "ended_at"), row)) if row else {}


def test_a_plain_busy_loop_gives_way_to_an_abort(tmp_path: Path) -> None:
    """The manual's ten seconds hold for a step that is only computing: the hook reaches it."""
    proc = _run_abort_probe(tmp_path, LOOP)
    assert proc.returncode == 0, proc.stderr[-400:]
    elapsed = float(re.search(r"elapsed=(\d+\.\d+)", proc.stdout).group(1))
    assert elapsed < 1.0, f"a plain loop took {elapsed} s to stop after an abort: {proc.stdout}"
    assert "run=FAIL/ABORTED" in proc.stdout, proc.stdout
    assert "'1.': 'ERROR'" in proc.stdout and "'2.': 'NOTRUN'" in proc.stdout, proc.stdout


def test_a_coroutine_loop_runs_to_completion_after_an_abort(tmp_path: Path) -> None:
    """The count hook does not reach a coroutine, so the step finishes its work and the abort is
    recorded on the next step - the limit the manual tells a test writer to avoid."""
    proc = _run_abort_probe(tmp_path, COROUTINE_LOOP)
    assert proc.returncode == 0, proc.stderr[-400:]
    elapsed = float(re.search(r"elapsed=(\d+\.\d+)", proc.stdout).group(1))
    assert elapsed > 0.5, f"the coroutine was cut off in {elapsed} s; the manual says it is not: {proc.stdout}"
    assert "run=FAIL/ABORTED" in proc.stdout, proc.stdout
    assert "'1.': 'PASS'" in proc.stdout and "'2.': 'ERROR'" in proc.stdout, proc.stdout


def test_an_unbounded_coroutine_loop_never_ends_the_run(tmp_path: Path) -> None:
    """``while true do end`` in a coroutine outruns every timeout the driver has."""
    release = scratch_release(tmp_path, ABORT_SCHEDULE % "local run = coroutine.wrap(function() while true do end end) run()", name="abort")
    work = tmp_path / "station"
    work.mkdir()
    driver = tmp_path / "abort_driver.py"
    driver.write_text(ABORT_DRIVER, encoding="utf-8", newline="\n")
    proc = subprocess.Popen(
        [sys.executable, str(driver), str(ROOT), str(release), str(work), "Abort probe", "0.4"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(ROOT),
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
        row = _open_run_row(work / "ate.db")
        assert row and row["ended_at"] is None, f"the run row should still be open: {row}"
        return
    raise AssertionError(f"the unbounded coroutine loop ended after {proc.returncode}: {proc.stdout}")


def test_a_command_in_a_step_holds_the_abort_until_it_finishes(tmp_path: Path) -> None:
    """``os.execute`` is outside the hook: the run ends when the command does, not when the
    Operator asks."""
    proc = _run_abort_probe(tmp_path, 'os.execute("sleep 3") ATE.report(Result.PASS, "the command finished")')
    assert proc.returncode == 0, proc.stderr[-400:]
    elapsed = float(re.search(r"elapsed=(\d+\.\d+)", proc.stdout).group(1))
    assert elapsed > 2.5, f"the abort ended the run in {elapsed} s while a 3 s command was running: {proc.stdout}"
    assert "'1.': 'PASS'" in proc.stdout, proc.stdout


def test_os_exit_in_a_step_is_not_in_the_sandbox_and_ends_the_step_with_an_error(tmp_path: Path) -> None:
    """``os.exit`` is nil in a schedule: the step ERRORs, the run is recorded, ATE keeps going.

    Measured before packet 35, ``os.exit(7)`` in a step ended the whole process with status 7 and
    left the run row open with nothing recorded. The lockdown removes it the way it removes
    ``dofile``, ``loadfile`` and ``load``, so the call is an ordinary Lua error."""
    proc = _run_abort_probe(tmp_path, "os.exit(7)", abort_after=0.2)
    assert proc.returncode == 0, f"os.exit ended the process: {proc.stdout} {proc.stderr[-300:]}"
    assert "'1.': 'ERROR'" in proc.stdout, proc.stdout
    row = _open_run_row(tmp_path / "station" / "ate.db")
    assert row and row["ended_at"] is not None and row["result"] is not None, f"the run row is not closed: {row}"


# --- Packet 34c: the developer manuals are held to the code ------------------------------

#: A worked example the manual shows, and the file on disk it came from. The LaTeX manual includes
#: each with \lstinputlisting and the Markdown manual quotes it in a fenced block; both are compared
#: with the file below, so a listing cannot drift from the code it shows.
DEV_EXAMPLE_FILES = sorted(
    p.relative_to(ROOT).as_posix()
    for p in DEV_EXAMPLES_DIR.rglob("*")
    if p.suffix in (".tslua", ".py")
)

#: The three edits chapter 16 tells a developer to make to ``src/ate/engine/lua_host.py``. The test
#: applies exactly these strings, so the text in the manual is the change that is proven.
DEVICE_EDITS = {
    "from ..devices.abort import DeviceAborted": (
        "from ..devices.abort import DeviceAborted\n"
        "from ..devices.echo import EchoDriver  # the developer manual's worked example"
    ),
    '        self._devices.register("VISA", VisaDriver)  # packet 19: pyvisa resources, no background reader':
        '        self._devices.register("VISA", VisaDriver)  # packet 19: pyvisa resources, no background reader\n'
        '        self._devices.register("ECHO", EchoDriver)  # the developer manual\'s worked example',
    '    "DeviceType": {\n        "SERIAL": "SERIAL",': (
        '    "DeviceType": {\n'
        '        "ECHO": "ECHO",  # the developer manual\'s worked example\n'
        '        "SERIAL": "SERIAL",'
    ),
}

#: Paths a manual names that do not exist in the repository, with why that is allowed. A path in a
#: worked example is created by the test that runs the example, not by the checkout.
MANUAL_PATHS_NOT_ON_DISK = {
    "src/ate/devices/echo.py": "the worked example's driver: tests/test_manuals.py copies "
                               "docs/manuals/examples/developer/echo_driver.py into a temporary copy "
                               "of src/ under exactly this name and runs it"
}

#: A command a developer is shown running, with the exit code and the output the manual claims.
#: ``{station}``, ``{db}``, ``{dist}`` and ``{out}`` are filled from a fixture's temporary directory.
DEV_COMMANDS_RUN: list[tuple[str, list[str], int, str, str]] = [
    ("python packaging/windows/build_installer.py --dry-run --dist dist",
     ["packaging/windows/build_installer.py", "--dry-run", "--dist", "{dist}"], 0, "iscc", "{repo}"),
    ("python -m build --wheel --no-isolation -o dist",
     ["-m", "build", "--wheel", "--no-isolation", "-o", "{dist}"], 0, "Successfully built", "{repo}"),
    ("QT_QPA_PLATFORM=offscreen python -m examples.harness.run_examples",
     ["-m", "examples.harness.run_examples"], 0, "ran 13 headless runs", "{repo}"),
    ("python -c \"import sqlite3; rows=sqlite3.connect('/tmp/station/data/ate.db').execute('SELECT step_number,result,attempt_count FROM node ORDER BY node_seq'); print(rows.fetchall())\"",
     ["-c", "import sqlite3; rows=sqlite3.connect('{db}').execute('SELECT step_number,result,attempt_count FROM node ORDER BY node_seq'); print(rows.fetchall())"],
     0, "", "{repo}"),
    ("python -m ate db migrate /tmp/station/data/ate.db --yes",
     ["-m", "ate", "db", "migrate", "{db}", "--yes"], 0, "schema version", "{repo}"),
]

#: Commands the developer manual shows that this machine cannot run, and what covers them instead.
#: A command with no test and no reason is a bug in this file.
DEV_COMMANDS_NOT_RUN: dict[str, str] = {
    "QT_QPA_PLATFORM=offscreen python -m pytest -q -p no:cacheprovider":
        "the suite cannot run itself from inside a test. This is exactly the command CI runs (the "
        "'Run tests' step of .github/workflows/ci.yml), and the counts quoted in chapter 1 come from "
        "running it: 1586 passed, 5 skipped in 336.65 s on this machine",
    "ATE_BUILD_TESTS=1 pytest -m build":
        "by design not part of a normal run: it builds a real PyInstaller bundle, which takes minutes. "
        "It was run by hand for this manual (2 passed, 2 skipped, 1587 deselected in 223.72 s). "
        "tests/test_pyinstaller_build_27a.py is the test it runs, and it loads "
        "packaging/build_pyinstaller.py itself",
    "ATE_DOCKER_TESTS=1 pytest -m build":
        "needs Docker and a network inside a debian:stable container and takes minutes. Run by hand for "
        "this manual, with TMPDIR on a disk-backed directory: 1 passed in 533.95 s. With the machine's "
        "/tmp (a 7.7 GB tmpfs) it fails with 'No space left on device'; chapter 19 says which is which",
    "ATE_BUILD_TESTS=1 pytest -m build -q":
        "the same gate as above, with the quiet flag; the -q is what the manual tells a developer to "
        "type and the same command was run without it",
    "ATE_DOCKER_TESTS=1 pytest -m build -q":
        "same as above",
    "python packaging/build_pyinstaller.py --smoke":
        "builds a frozen bundle. tests/test_pyinstaller_build_27a.py (the ATE_BUILD_TESTS gate) calls "
        "this same script's build_bundle() and verify_bundle()",
    "python packaging/build_pyinstaller.py --verify dist/ate":
        "needs a built bundle. tests/test_linux_packages_28b.py runs verify_bundle() against a real "
        "bundle and against a deliberately incomplete one, so the gate itself is tested",
    "python docs/manuals/build.py developer":
        "builds this manual's PDF in Docker with texlive. It is run by hand before anyone reads the PDF; "
        "the suite checks the sources, not the rendered pages",
    "python runner.py /tmp/station/config/config.json \"Developer: echo Device\" --input \"Operator reading=42\"":
        "executed by test_adding_a_device_type_the_way_the_manual_says_works, which runs the runner file "
        "itself against a temporary copy of src/ that has the ECHO driver in it, and answers the "
        "schedule's Step Input with the same --input the manual shows",
}


def _fill_dev(placeholder: str, station: Path) -> str:
    """The temporary directories a developer-manual command line refers to."""
    return (
        placeholder.replace("{repo}", str(ROOT))
        .replace("{station}", str(station))
        .replace("{db}", str(station / "data" / "ate.db"))
        .replace("{dist}", str(station / "dist"))
        .replace("{out}", str(station / "out"))
    )


def test_the_developer_manual_shows_no_command_that_is_never_run() -> None:
    """Every command in the developer manual is run by the suite or explained.

    The same rule as the admin manual, the same shape: add a line to a listing without adding it to one
    of the two tables and this fails."""
    shown = set(shell_commands(MANUALS["developer"]))
    accounted = {line for line, *_ in DEV_COMMANDS_RUN} | set(DEV_COMMANDS_NOT_RUN)
    unaccounted = sorted(shown - accounted)
    assert not unaccounted, f"the developer manual shows commands no test runs or explains: {unaccounted}"
    stale = sorted(({line for line, *_ in DEV_COMMANDS_RUN} | set(DEV_COMMANDS_NOT_RUN)) - shown)
    assert not stale, f"reasons kept for commands the manual no longer prints: {stale}"


@pytest.mark.parametrize("line, argv, expected, wanted, cwd", DEV_COMMANDS_RUN)
def test_a_command_in_the_developer_manual_runs(line, argv, expected, wanted, cwd, station) -> None:
    """Run one command the developer manual shows and check what it claims."""
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(ROOT / "src")}
    proc = subprocess.run(
        [sys.executable, *[_fill_dev(a, station) for a in argv]],
        cwd=_fill_dev(cwd, station),
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    assert proc.returncode == expected, f"{line}: exit {proc.returncode}\n{proc.stderr[-500:]}"
    if wanted:
        assert wanted in proc.stdout + proc.stderr, f"{line}: output was {(proc.stdout + proc.stderr)[-300:]!r}"


def test_the_failed_run_query_in_the_manual_reads_a_run_that_happened(station) -> None:
    """The query chapter 15 gives is run against a real Test Run, not against an empty database.

    ``examples/harness`` runs the example schedule into the fixture's Station database, then the exact
    command line from the manual is executed as its own process."""
    config = write_config(station / "data")  # the fixture's Station database, where {db} points
    with bench_env(Peers()):
        record = run_schedule(config, "Example basics", inputs={"Operator input": 1.5})
    assert record["run"]["result"] == "PASS", record["run"]
    line, argv, expected, wanted, cwd = next(
        entry for entry in DEV_COMMANDS_RUN if entry[0].startswith("python -c \"import sqlite3")
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    proc = subprocess.run(
        [sys.executable, *[_fill_dev(a, station) for a in argv]],
        cwd=_fill_dev(cwd, station), capture_output=True, text=True, timeout=120, env=env,
    )
    assert proc.returncode == expected, proc.stderr[-500:]
    rows = ast.literal_eval(proc.stdout.strip())
    assert any(result == "PASS" for _number, result, _attempts in rows), rows
    assert wanted in proc.stdout, proc.stdout


def test_the_developer_coverage_check_can_fail() -> None:
    """Prove the module, signal, table and driver checks bite, one each.

    Each group is derived from the code, so a manual that quietly drops a name is caught. This test
    removes one name from the manual's own code and shows the derivation reports it as missing."""
    for label, code in MANUALS["developer"].code_forms():
        for names, dropped in (
            (ate_modules(), "src/ate/devices/stream.py"),
            (declared_signals(RunEngine), "run_uuid_reported"),
            (declared_signals(HeadlessRunSession), "report_written"),
            (schema_tables(), "schedule_instance"),
            (device_drivers(), "VisaDriver"),
        ):
            assert dropped in names, f"the derivation no longer contains {dropped}"
            stripped = code.replace(dropped, "somethingElse")
            assert missing_from_code([(label, stripped)], names) == [dropped], f"removing {dropped} must show up as missing"
            assert not missing_from_code([(label, code)], names), (
                f"the developer manual's {label} code is missing {sorted(missing_from_code([(label, code)], names))}"
            )


def test_the_developer_agent_manual_names_everything_on_its_own() -> None:
    """An agent given only ``docs/manuals/llm/developer.md`` must not need the PDF."""
    names = (
        ate_modules()
        | declared_signals(RunEngine)
        | declared_signals(HeadlessRunSession)
        | schema_tables()
        | device_drivers()
    )
    gone = missing_from_code(MANUALS["developer"].code_forms()[1:2], names)
    assert not gone, f"the developer agent manual alone has none of it in code: {', '.join(gone)}"


#: A dotted name in the manuals is a thing the code has. `ate db`, `ate _pdf` and `ate _selftest` are
#: subcommands, not attributes, and `ate.spec` and `ate.iss` are file names, not symbols.
_NOT_SYMBOLS = {"ate.db", "ate._pdf", "ate._selftest", "ate._version", "ate.spec", "ate.iss"}

SRC_PATH = re.compile(r"src/[\w/._-]+\.(?:py|lua|j2|spec|sty|tex|md)")
# A name ends at an identifier, not at a sentence's full stop: `ate.devices.` is read as `ate.devices`.
QUALIFIED = re.compile(r"(?<![\w/.-])ate\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


def _resolves(name: str) -> bool:
    """Whether ``ate.a.b.c`` names a real module, class, function or constant."""
    parts = name.split(".")
    for cut in range(len(parts), 0, -1):
        module = ".".join(parts[:cut])
        try:
            obj = importlib.import_module(module)
        except ImportError:
            continue
        for attr in parts[cut:]:
            obj = getattr(obj, attr, None)
            if obj is None:
                return False
        return True
    return False


def test_every_path_and_dotted_name_in_the_developer_manuals_exists() -> None:
    """Every ``src/...`` path and every ``ate.x.y`` in the developer manuals is real.

    A path that does not exist, or a name the module does not have, is a manual that has drifted from
    the code. Checked in both the LaTeX chapters and the Markdown manual; the only paths allowed to be
    absent are the ones a worked example creates, listed with their reason."""
    missing_paths: list[str] = []
    missing_names: list[str] = []
    for label, text in MANUALS["developer"].texts():
        for path in set(SRC_PATH.findall(text)):
            if path in MANUAL_PATHS_NOT_ON_DISK:
                continue
            if not (ROOT / path).exists():
                missing_paths.append(f"{label}: {path}")
        for name in set(QUALIFIED.findall(text)):
            if name in _NOT_SYMBOLS or _resolves(name):
                continue
            missing_names.append(f"{label}: {name}")
    assert not missing_paths, f"the developer manual names files that do not exist: {sorted(missing_paths)}"
    assert not missing_names, f"the developer manual names things the code does not have: {sorted(missing_names)}"


def test_the_worked_examples_are_quoted_exactly() -> None:
    """The driver and schedule the manual shows are byte-for-byte the files the test runs."""
    markdown = DEVELOPER.markdown.read_text(encoding="utf-8")
    blocks = re.findall(r"```(?:python|lua):(\S+)\n(.*?)```", markdown, re.S)
    quoted = {path: body for path, body in blocks}
    expected = {
        path: Path(path).read_text(encoding="utf-8") for path in DEV_EXAMPLE_FILES
    }
    assert set(quoted) == set(expected), (
        f"developer.md quotes {sorted(set(quoted) - set(expected))} and omits {sorted(set(expected) - set(quoted))}"
    )
    for path, body in expected.items():
        assert quoted[path] == body, f"{path}: the listing in developer.md differs from the file on disk"

    tex = plain("\n".join(p.read_text(encoding="utf-8") for p in DEVELOPER.tex_parts()))
    for path in expected:
        assert path in tex, f"the LaTeX manual never includes {path}"


#: The script a developer uses to drive one headless run. It is a file in the repository, quoted
#: by both forms of the manual, and the worked-example test below runs that file itself rather
#: than a copy of it written out here.
RUNNER = DEV_EXAMPLES_DIR / "runner.py"


def _copy_src(dest: Path) -> Path:
    """A working copy of ``src/``, without the pycache the checkout may have accumulated."""
    shutil.copytree(ROOT / "src", dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def _echo_release(tmp: Path) -> Path:
    """The Media Release chapter 16 builds around the worked example, checksummed as ATE requires."""
    release = tmp / "release"
    release.mkdir()
    shutil.copyfile(DEV_EXAMPLES_DIR / "echo.tslua", release / "echo.tslua")
    definition = {
        "format": 1,
        "mediaNumber": "MR-DEV-34C",
        "name": "Developer manual worked example",
        "version": "1.0.0",
        "files": [{"path": "echo.tslua", "sha256": hashlib.sha256((release / "echo.tslua").read_bytes()).hexdigest()}],
    }
    (release / "developer.atedef").write_text(json.dumps(definition), encoding="utf-8", newline="\n")
    return release


def _echo_config(tmp: Path, src: Path) -> Path:
    """A config for one headless run of the worked example, with Device tracing on."""
    config = tmp / "config.json"
    config.write_text(
        json.dumps(
            {
                "format": 1,
                "station": {"name": "DEV-34C", "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
                "database": {"type": "sqlite", "path": str(tmp / "ate.db")},
                "reports": {"directory": str(tmp / "reports")},
                "testDefinitions": {"directories": [str(_echo_release(tmp))]},
                "log": {"directory": str(tmp / "logs")},
                "deviceTrace": {"enabled": True},
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    return config


def _run_echo(tmp: Path, src: Path) -> dict[str, Any]:
    """Run the worked example against ``src``, in its own process, and return its record.

    The schedule's Step Input has to be answered or the run never ends, so the runner is called
    with the same ``--input`` the manual shows; the runner exits non-zero unless the session
    reached FINISHED."""
    proc = subprocess.run(
        [sys.executable, str(RUNNER), str(_echo_config(tmp, src)), "Developer: echo Device",
         "--input", "Operator reading=42"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=180,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(src)},
    )
    assert proc.returncode == 0, f"the worked example failed: {proc.stderr[-800:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_adding_a_device_type_the_way_the_manual_says_works(tmp_path) -> None:
    """Chapter 16's four steps, applied to a copy of the source, really produce a Device Type.

    The driver and the schedule are the manual's own files; the edits are the strings printed in the
    manual. The run then opens the Device, writes to it, reads it back, cuts a read with ``size`` and
    leaves one Device open when the run ends."""
    src = _copy_src(tmp_path / "src")
    (src / "ate/devices/echo.py").write_text(
        (DEV_EXAMPLES_DIR / "echo_driver.py").read_text(encoding="utf-8"),
        encoding="utf-8", newline="\n",
    )
    host = src / "ate/engine/lua_host.py"
    text = host.read_text(encoding="utf-8")
    for old, new in DEVICE_EDITS.items():
        assert old in text, f"the manual's edit no longer applies: {old!r} is not in lua_host.py"
        text = text.replace(old, new, 1)
    host.write_text(text, encoding="utf-8", newline="\n")

    record = _run_echo(tmp_path, src)
    assert record["result"] == "PASS" and record["end_reason"] == "COMPLETED", record
    assert record["steps"] == {"1.": "PASS", "2.": "PASS", "3.": "PASS", "4.": "PASS"}, record["steps"]
    # The runner answered the Step Input, which is the only way this run reaches FINISHED.
    assert record["inputs"] == ["42"], record["inputs"]
    # The trace lines are what the registry wrapped around the new driver, and the WARNING is the
    # Device the schedule never closed being closed by the engine at run end.
    assert any("open ECHO ok" in line for line in record["logs"]), record["logs"]
    assert any('write ECHO manual-example 6 bytes "hello\\n"' in line for line in record["logs"]), record["logs"]
    assert any('read ECHO sized 4 bytes "ABCD" partial' in line for line in record["logs"]), record["logs"]
    assert any("left open at end of run" in line for line in record["logs"]), record["logs"]
    assert any("truncated" in line for line in record["logs"]), record["logs"]

    # A Device Type the registry has never heard of is refused, which is what the register line is for.
    unknown = subprocess.run(
        [sys.executable, "-c", "from ate.devices.registry import DeviceRegistry; DeviceRegistry().open_device('ECHOX', {})"],
        capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONPATH": str(src)},
    )
    assert "unknown DeviceType 'ECHOX'" in unknown.stderr, unknown.stderr


def test_a_device_type_registered_but_not_in_the_enum_is_nil(tmp_path) -> None:
    """With no edit at all, ``DeviceType.ECHO`` is nil and the step records an ERROR.

    This is the first failure a developer meets: the name does not exist, so the schedule passes nil
    and :func:`_device_type_name` refuses it."""
    src = _copy_src(tmp_path / "src")
    record = _run_echo(tmp_path, src)
    assert record["result"] == "FAIL", f"an unknown Device Type should fail the run, got {record}"
    assert "deviceType must be a DeviceType enum member" in "\n".join(record["logs"]), record["logs"]


def test_an_enum_member_without_a_register_line_is_refused_by_the_registry(tmp_path) -> None:
    """The enum member gives the schedule a name; without the register line nothing can open it.

    Chapter 16's warning, executed: adding only the enum entry fails the run with
    ``unknown DeviceType 'ECHO'``, from :meth:`DeviceRegistry.open_device`."""
    src = _copy_src(tmp_path / "src")
    host = src / "ate/engine/lua_host.py"
    host.write_text(
        host.read_text(encoding="utf-8").replace(
            '    "DeviceType": {\n        "SERIAL": "SERIAL",',
            '    "DeviceType": {\n        "ECHO": "ECHO",  # the developer manual\'s worked example\n        "SERIAL": "SERIAL",',
            1,
        ),
        encoding="utf-8",
        newline="\n",
    )
    record = _run_echo(tmp_path, src)
    assert record["result"] == "FAIL", f"an unregistered Device Type should fail the run, got {record}"
    assert "unknown DeviceType 'ECHO'" in "\n".join(record["logs"]), record["logs"]


#: The version bump and the table chapter 17 shows, applied to a copy of schema.py.
SCHEMA_EDITS = {
    "SCHEMA_VERSION = 5": "SCHEMA_VERSION = 6",
    "schedule_instance = Table(": (
        "review_note = Table(\n"
        '    "review_note",\n'
        "    metadata,\n"
        '    Column("run_uuid", Text, primary_key=True),\n'
        '    Column("seq", Integer, primary_key=True),  # the composite key, as every other run table\n'
        '    Column("text", Text, nullable=True),\n'
        '    Column("at", Text, nullable=True),\n'
        ")\n\n"
        "schedule_instance = Table("
    ),
}


def test_adding_a_schema_version_the_way_the_manual_says_works(tmp_path) -> None:
    """Chapter 17, run for real: migrate a version 5 database with a copy of the code that declares a
    sixth version and one more table, then prove the shipped code refuses what it cannot read."""
    src = _copy_src(tmp_path / "src")
    schema = src / "ate/storage/schema.py"
    text = schema.read_text(encoding="utf-8")
    for old, new in SCHEMA_EDITS.items():
        assert old in text, f"the manual's edit no longer applies: {old!r} is not in schema.py"
        text = text.replace(old, new, 1)
    schema.write_text(text, encoding="utf-8", newline="\n")

    db = tmp_path / "ate.db"

    def migrate(python_src: Path) -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "ate", "db", "migrate", str(db), "--yes"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONPATH": str(python_src)},
        )
        return proc.returncode, proc.stdout + proc.stderr

    code, out = migrate(ROOT / "src")
    assert code == 0 and "schema version 5" in out, out
    conn = sqlite3.connect(db)
    try:
        before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "schedule_instance" in before, sorted(before)
    # A database that did not exist has nothing to copy: the first migration makes no backup.
    assert not [p for p in tmp_path.iterdir() if ".backup-" in p.name], sorted(tmp_path.iterdir())

    # Chapter 17's warning, executed: a version bump with no table to create is refused, not stamped.
    bump_only = tmp_path / "bump-only"
    bump_src = _copy_src(bump_only)
    bump_schema = bump_src / "ate/storage/schema.py"
    bump_schema.write_text(
        bump_schema.read_text(encoding="utf-8").replace("SCHEMA_VERSION = 5", "SCHEMA_VERSION = 6", 1),
        encoding="utf-8",
        newline="\n",
    )
    code, out = migrate(bump_src)
    assert code == 1 and "this ATE has no migration from version 5 to 6" in out, out
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    finally:
        conn.close()
    assert not [p for p in tmp_path.iterdir() if ".backup-" in p.name], "nothing to migrate makes no copy"

    code, out = migrate(src)
    assert code == 0 and "schema version 6" in out, out
    # The upgrade of an existing database copies it first, and says where the copy is.
    copies = [p for p in tmp_path.iterdir() if ".backup-" in p.name]
    assert len(copies) == 1, sorted(tmp_path.iterdir())
    assert str(copies[0]) in out, out
    conn = sqlite3.connect(copies[0])
    try:
        copied_version = conn.execute("PRAGMA user_version").fetchone()[0]
        copied_tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert copied_version == 5 and "review_note" not in copied_tables, sorted(copied_tables)

    conn = sqlite3.connect(db)
    try:
        after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "review_note" in after, sorted(after)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    finally:
        conn.close()
    # A migration adds; it never rewrites what is already there.
    assert before <= after, sorted(before ^ after)

    # The shipped code refuses a database it cannot read, which is why the version check exists.
    code, out = migrate(ROOT / "src")
    assert code == 1 and "this ATE is too old for" in out, out
