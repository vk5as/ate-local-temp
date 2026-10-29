# 25b: Layout setting and switching

**Parent:** 25 (Three-pane layout and layout switching) — `25-three-pane-layout.md`

**What to build:** The layout is selectable per Station and switchable mid-run.

**Blocked by:** 25a (Three-pane Run screen), 30a (GUI launch: the main window the View menu lives in)

**Status:** resolved

- [x] `ui.layout` (`staged` default or `threePane`) sets the default; a View menu switches layouts for the session, including mid-run, without affecting the run; `ui.allowLayoutSwitch` can disable the switch
- [x] A pytest-qt test switches layouts mid-run

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
