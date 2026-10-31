# 33: Appearance review against the Run screen mockup

**Parent:** none. User request, 2026-10-31: the HTML mockup looked a lot nicer than the application.

**What this is:** Two independent review-only passes, one by Opus and one by Strata, comparing ATE's appearance with the mockup. They produce recommendations and design choices. **No application code changes.** The user reviews the outcome first; nothing joins the implementation queue until they say so, and any implementation lands after 32a (PySide6).

**The goal to judge against:** a modern, attractive interface that stays intuitive and easy to use. A pixel match is not the goal: the mockup will not map perfectly to Qt, and its layout, not its browser effects, is what was agreed.

**Blocked by:** none (review only)

**Status:** ready-for-human (both reports in; see `design-review/33-summary.md`)

## Brief for each reviewer

You work alone. Don't look for or read another reviewer's report. Work only in your own worktree and in `/tmp/appearance-<your name>/`. Don't change anything under `src/` or `tests/`. Throwaway screenshot scripts stay in `/tmp`.

**Sources**
- Mockup: `git show prototype/run-screen:prototypes/run-screen/run-screen-prototype.html` (716 lines). Variant C is the staged layout, variant B the three-pane. Write it to `/tmp` and open it there.
- Agreed behaviour: `.scratch/ate-v1-wayfinding/assets/run-screen.md`.
- Code: `src/ate/app.py` (main window), `src/ate/ui/staged_run_screen.py`, `three_pane_run_screen.py`, `run_widgets.py`, `history_screen.py`, `export_log_dialog.py`. There are 21 `setStyleSheet`/`QPalette`/`setStyle` uses, with no shared theme that this packet's author knows of; confirm that.
- Vocabulary: `GLOSSARY.md`. Use its terms.

**See both for real.** Don't review from the code alone. Render the application with `xvfb-run` and `QWidget.grab()` at 1280x800, from a virtualenv at `/home/dev/projects/ate-ornith/.venv` and fixtures such as `write_ate_env` in `tests/_fixtures.py`. `tests/test_finished_run_layout_30b.py` shows how to drive a shown window through a run. Render every mockup state too: playwright is not installed, so use `QWebEngineView` under `xvfb-run` and grab it after load, switching variants and states through the page's own controls. Also look at 1920x1080 and at the smallest size the application allows.

**Cover at least:** both layouts; each setup stage; a schedule with broken and mismatched entries; a run in progress with a blocking input and a `POLL` button; a Stop on Fail prompt; a finished run, passed and failed; the mismatch confirmation; History; the export dialog; the menu and status bar. Note a fresh `Fusion` or platform style difference on Windows only if the code shows one; you cannot run Windows here, so say so.

**Look at:** visual hierarchy; colour and how result states (PASS, FAIL, ERROR, NOTRUN) read, including for colour-blind Operators; type scale; spacing and alignment; density; buttons and primary actions; icons; tables and step lists; empty, loading and error states; focus and keyboard use; contrast (give measured ratios where you claim a problem); and whether what an Operator must do next is obvious from across the bench. Ease of use outranks looks: flag any mockup idea that would make ATE slower or less clear on a production bench.

**Write** `.scratch/ate-v1/design-review/33-<your name>.md` with:
1. **Verdict:** a short paragraph on why the mockup looks better and how far the gap can close.
2. **Gaps:** mockup against application, screen by screen, with the screenshot paths.
3. **Design choices:** a proposed design system with concrete values: palette with result-state colours, type scale, spacing scale, radii, button and table styles, icon set with its licence, light or dark or both. Give the reasoning, and alternatives where the choice is really open.
4. **Recommendations:** a ranked list. Each has the problem, the change, impact, effort (S/M/L), and how Qt does it (a single application stylesheet, `QPalette`, item delegates, custom painting, and so on). Mark anything Qt cannot do well. Everything must work on PySide6 with Qt 6.11 on Windows and Linux, with no new runtime dependency unless you justify it and its licence is compatible.
5. **Mockup ideas not to copy,** and why.
6. **Decisions for the user:** the questions only they can answer, each with your recommendation.

Copy the screenshots your report cites to `.scratch/ate-v1/design-review/33-<your name>-shots/`, downscaled PNGs under 300 KB each. Commit the report and shots to your own branch `review-33-<your name>`, ending the message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never touch `integration/ate-v1` or `/home/dev/projects/ate-ornith`, never create worktrees, rebase or push.

**Final reply (short):** branch, head SHA, the verdict paragraph, your top five recommendations, and the decisions for the user.
