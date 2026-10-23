# 23b: History screen

**Parent:** 23 (History screen) — `23-history-screen.md`

**What to build:** Operators and quality engineers browse runs and act on them.

**Blocked by:** 23a (History session: query, sort and filter)

**Status:** ready-for-agent

- [ ] A flat table bound to the History session, plus a summary panel with run fields, `Result` counts and mismatches
- [ ] Actions: Open report (newest file in the system viewer), Regenerate (HTML and/or PDF, Include Run log toggle on by default), Show all runs of this UUT, New run for this UUT (prefills UUT Type, serial, Project and Test Schedule)
- [ ] The screen holds no logic beyond the session; a pytest-qt smoke test covers it

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
