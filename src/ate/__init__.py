"""ATE — Automated Test Execution.

A desktop application that runs Lua 5.5 *Test Schedules* against hardware UUTs,
records each run in a local SQLite database, and renders an HTML/PDF report.

The package is organised in three layers (see ``spec.md`` §Implementation
Decisions → Architecture):

- :mod:`ate.engine` — the run engine's flow control and Lua host. Imports no
  widgets.
- :mod:`ate.run_session` — the UI-free object a layout binds to (state +
  commands). Imports no widgets.
- :mod:`ate.ui` — layouts and screens (QtWidgets). This is where GUI code lives;
  nothing here runs logic.

This module only exposes the package version and, later, top-level helpers. It
deliberately imports neither the engine nor the UI so that importing ATE stays
cheap and widget-free.
"""

from ._version import __version__, version

__all__ = ["__version__", "version"]
