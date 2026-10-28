# 30a: GUI launch — `ate` opens the application

**Parent:** none. This is a gap found in the 28a review on 2026-10-29: no packet wires the screens into a runnable application. Bare `ate` prints the version, and nothing creates a `QApplication` (only the PDF helper does).

**What to build:** Running `ate` starts the desktop application. It does the launch checks the spec requires, then opens a main window holding the Run screen and the History screen.

**Blocked by:** 23d (History screen fixes), 26b (`ate setup`: permissions)

**Status:** ready-for-agent

- [ ] `ate` with no subcommand (optionally `--config <path>`) starts the GUI. `ate --version` still prints the version and exits; `ate --help` prints usage and exits 0. The existing subcommands are unchanged.
- [ ] Launch sequence, each step reusing the existing code:
  - read the config once, showing the missing-config prompt as a dialog ("Create default config and launch" / "Abort");
  - open the per-launch application log;
  - refuse with a dialog for a missing or older database ("run `ate db migrate`") or a newer one ("this ATE is too old"), never migrating;
  - close interrupted runs;
  - scan the configured directories.
- [ ] A main window (`QMainWindow`) with the staged Run screen as the default view and a way to open the History screen, honouring `ui.allowHistoryDuringRun`. Include the debug window if one exists. Menus are minimal; 25b adds the View menu's layout switch to this window.
- [ ] Tests (pytest-qt, temporary config and data dirs):
  - `ate` builds the main window without entering the event loop (an injectable `exec`);
  - the missing-config Abort exits non-zero;
  - each database refusal shows its message and exits non-zero;
  - `--version` and `--help` behave as above.
- [ ] `docs/building-from-source.md` says `ate` opens the GUI.

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md` (CLI around line 220, the launch user stories 17, 22, 23, 31, 98, and the Layouts module around line 219). Keep to this packet's checkboxes.
