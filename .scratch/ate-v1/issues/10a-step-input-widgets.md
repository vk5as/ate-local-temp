# 10a: Step Input widgets on the Run screen

**Parent:** 10 (Step Inputs) — `10-step-inputs.md`

**What to build:** The staged Run screen shows each step's Step Inputs under it and lets the Operator answer them; the engine, recording and run-session side already exist (ticket 10, `83467fc`).

**Blocked by:** 10 (Step Inputs)

**Status:** resolved

- [x] Inputs show under their step at all times, one widget per declared type (`ENUM` as `RADIOBUT`/`CHECKBOX`/`PUSHBUT`, `INTEGER`, `FLOAT`, `STRING`, `PROMPT`, `POLL`) honouring `tooltip` and `orient`; widgets only call the run session (no logic in the widget)
- [x] A blocking input is enabled only while its `ATE.input` call waits, then disabled showing the value; `POLL` buttons are clickable while their step runs
- [x] The staged pytest-qt smoke test answers one blocking input by interacting with the widget, not the session

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes.

## Comments

- `StepInputWidget` (`src/ate/ui/staged_run_screen.py`) renders one declaration the way the mockup's
  `inputHTML` does and is attached to its own list row under the step (`setItemWidget`, as packet 05b
  does for broken rows). Both the Steps & options stage and the Run view build them.
- The session gained the thin commands the layout needs: `supply_input(node_seq, attempt_no, index, value)`
  and `click_poll(node_seq, attempt_no, index, text)`, both 1-based on `index` (the `n` in `ATE.input(n)`,
  the same number `input_asked` carries) and both emitting `input_answered` so the layout closes the widget.
- Value validation lives in `step_inputs.value_error`, next to the declaration rules it uses; the widget
  only asks it and shows the result as the mockup's inline `.err`.
- The engine emits no "step finished" event, so a POLL is opened when its step announces its inputs and
  closed when it is clicked or the run ends — the closest reading of "clickable while its step runs".
