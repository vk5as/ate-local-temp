"""The ``ate`` command-line entry point.

Ticket 01 wires up the entry point and its single implemented subcommand,
``--version``, which prints ATE's version (from :mod:`ate._version`) and exits 0.
Later tickets add ``ate setup``, ``ate db ...`` and friends; they call into this
module rather than reimplementing argument handling here.
"""

from __future__ import annotations

import sys

from ._version import __version__


def _print_version() -> int:
    """Print the version to stdout and return the process exit code."""
    print(__version__)
    return 0


def main(argv=None) -> int:
    """Run the ``ate`` CLI. Returns the intended process exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("-V", "--version"):
        return _print_version()

    prog = "ate"
    print(f"{prog}: no command given\ntry '{prog} --version' for now", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
