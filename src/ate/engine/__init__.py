"""The run engine: flow control and the Lua host.

Role (see ``spec.md`` §Implementation Decisions → Modules):

* Runs each Test Run on one worker, stepping through the expanded schedule tree
  in order and applying flow control (stop-on-fail, abort, end reasons).
* Owns the Lua host: a fresh :mod:`lupa` runtime per load, the ``ATE`` table,
  the ``ScheduleParams``/``StepParams`` builders and the enums.

This package imports **no** widgets. It is driven only through Qt signals by the
run session; it never touches the GUI directly.
"""
