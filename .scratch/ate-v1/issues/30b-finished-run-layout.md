# 30b: Staged Run screen layout after a run finishes

**Parent:** none. This is a gap seen in the review screenshots of 10a, 29c and 29d (2026-10-29/30).

**What to build:** When a run reaches FINISHED, the staged layout re-shows the setup stages above the run view. The step list then shrinks to about 68 px (seen in 10a), and the main window grows past its size: 1280x830 in 29c, 1280x998 in 29d. That pushes the status bar off-screen. A finished run should stay readable inside the window, and starting the next run should still be obvious.

**Blocked by:** 30a (GUI launch), 25b (Layout switching)

**Status:** resolved

- [x] After FINISHED, the staged layout keeps the window at its size. At 1280x800 the step list, the log/summary tabs and the status bar stay visible, and the step list is at least as tall as it was during the run. Follow the mockup (`prototype/run-screen`, variant C, "New run" in the finished state) for how the next run is offered.
- [x] The three-pane layout is checked for the same problem and keeps its size too.
- [x] pytest-qt tests: after a 20-step run finishes in a shown 1280x800 main window, the window size is unchanged, the status bar is inside the window, and the step list viewport is at least its running height.

Definitions live in `.scratch/ate-v1-wayfinding/assets/` (`run-screen.md`); the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes.
