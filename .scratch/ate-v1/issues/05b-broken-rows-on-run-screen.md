# 05b: Broken Test Definitions and schedules shown on the Run screen

**Parent:** 05 (Test Definition catalog) — `05-test-definition-catalog.md`

**What to build:** The Run screen's schedule list shows every broken Test Definition and broken Test Schedule as a row with its error, which can't be started (spec: `test-definition-format.md` line 103, `run-screen.md` lines 24 and 48).

**Blocked by:** 07c (dependency errors break the Test Definition)

**Status:** resolved

**Found in:** the 07c review. Ticket 05 ticked "the picker shows broken Test Definitions and schedules with their errors", but `list_schedule_rows` (`src/ate/catalog.py`) is only called by tests; `StagedRunScreen._refresh_schedule_list` builds the list from `list_schedules`, which skips broken definitions. Probe: a cycle MR-X⇄MR-Y plus one good release gives three rows from `list_schedule_rows`, while the live list has one.

- [x] The schedule list on the Run screen includes a row for each broken Test Definition (`(Test Definition broken)` with its release, `MR · name version`) and each broken schedule (its file and load error), styled as in the mockup: `.b-sched .it.broken` (`#fff5f5` background, not-allowed cursor) with the error in `.err` (`#b71c1c`, 12px). See `prototype/run-screen:prototypes/run-screen/run-screen-prototype.html` lines 97, 167-169.
- [x] A broken row can't be selected to start a run; Rescan refreshes broken rows too; the filter matches broken rows by name and release.
- [x] Seam tests drive the real screen offscreen with real `.atedef` trees (a dependency cycle, a missing file, a schedule that raises on load) and assert the rows, their errors and that Start stays disabled; a screenshot is compared with the mockup.
