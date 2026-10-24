# 23: History screen

**What to build:** Operators and quality engineers can find any past Test Run, open or regenerate its report, and start a new run for the same UUT.

**Blocked by:** 22 (PDF via the helper process)

**Status:** split

**Split into:** 23a, 23b, 23c, 23d — work happens in those packets; this ticket is resolved when all of them are.

- [ ] A flat table of runs, newest first, sortable by start time, UUT Type, serial, result, End Reason, Test Schedule, Project and Operator; filters for UUT Type, serial (substring), result, End Reason, date range and Project; loaded page by page
- [ ] A summary panel with run fields, `Result` counts and mismatches, and actions Open report (newest file in the system viewer), Regenerate (HTML and/or PDF, Include Run log toggle on by default), Show all runs of this UUT, New run for this UUT (prefills UUT Type, serial, Project and Test Schedule)
- [ ] No delete or edit
- [ ] Usable during a run unless `ui.allowHistoryDuringRun` is `false` (then shows "A Test Run is in progress"); a running run shows as "running" with report actions disabled
- [ ] The screen holds no logic beyond the run session / a History session object; a pytest-qt smoke test covers it

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
