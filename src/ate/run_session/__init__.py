"""The run session: the UI-free object a layout binds to.

Role (see ``spec.md`` §Implementation Decisions → Modules):

* Holds all run state — setup fields, the schedule list, the expanded tree with
  enables and results, Attempts, input/prompt/timer status, log lines.
* Exposes commands (scan/rescan, select a schedule, set fields, toggle steps,
  start, answer inputs, choose Continue/Retry/Abort, abort) and emits Qt signals
  for state changes.

This package imports **no** widgets. It is the seam between the engine and any
layout: layouts render it and forward user actions to it, but nothing here runs
GUI code or holds run logic.
"""
