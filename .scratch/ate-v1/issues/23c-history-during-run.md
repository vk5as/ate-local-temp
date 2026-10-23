# 23c: History during a run

**Parent:** 23 (History screen) — `23-history-screen.md`

**What to build:** History behaves correctly while a run is in progress.

**Blocked by:** 23b (History screen)

**Status:** ready-for-agent

- [ ] Usable during a run unless `ui.allowHistoryDuringRun` is `false` (then shows "A Test Run is in progress"); a running run shows as "running" with report actions disabled
- [ ] A pytest-qt test covers both settings

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
