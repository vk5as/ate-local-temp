# 12d: Failure bar on the Run screen

**Parent:** 12 (Stop on Fail, the failure prompt and Retry) — `12-stop-on-fail-and-retry.md`

**What to build:** The staged Run screen shows the failure prompt.

**Blocked by:** 12c (Retry and Attempt recording)

**Status:** resolved

- [x] On a pause the failed step is highlighted and a full-width Continue / Retry / Abort bar appears; buttons call the run session
- [x] A pytest-qt test drives each button

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.
