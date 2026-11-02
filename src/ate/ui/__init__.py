"""Layouts and screens (QtWidgets). This is where GUI code lives.

Role (see ``spec.md`` §Implementation Decisions → Modules):

* Renders the run session and forwards user actions back to it: the staged Run
  screen, the three-pane Run screen, the History screen and the debug window.
* Chooses which layout to show (`ui.layout`) and drives Qt widgets.

This package **is** allowed to import widgets — that is its whole point. The
architectural rule only forbids widget imports in :mod:`ate.engine` and
:mod:`ate.run_session`; nothing here runs logic, so layouts stay interchangeable
and the engine stays testable without a GUI.
"""
