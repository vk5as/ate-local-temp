# 31d: A headless mismatch follows `database.storeMismatchedFiles`

**Parent:** none. User decision, 2026-10-31. Today a run with no display answers the run-start mismatch confirmation "start, store nothing", even when the config says `always`. (Corrected at review: `store_mismatched_files` already ignored that answer for `always` and `never`, so no bytes were ever lost. This packet makes the answer and the documents truthful; stored bytes do not change.)

**What to build:** With nobody to answer the confirmation, the run still starts, and the config decides whether the mismatched files' bytes are stored. An Operator at a display sees no change.

**Blocked by:** none

**Status:** resolved

- [x] Headless answer by setting: `always` stores the bytes, `never` stores nothing, `ask` stores nothing (nobody made the choice, and the ticked-by-default box is not a choice). The run starts in all three. `MismatchConfirmation.answer_without_storing` in `src/ate/ui/run_widgets.py` is renamed to fit, its docstring and the `_stored_override` comment rewritten, and the table in `src/ate/dialogs.py` docstring wording and `tests/test_modals_never_block_headless.py` header updated.
- [x] The `ATE_AUTO_DISMISS_DIALOG` seam gives the same answer as `offscreen`/`minimal`.
- [x] The mismatch is still logged, flagged and recorded with its checksums in all three cases.
- [x] Tests: `test_the_headless_answer_is_the_safe_branch` becomes one case per setting with the answer above. In a fresh headless process as in `test_a_mismatched_run_starts_in_a_fresh_headless_process`, a real run per setting asserts whether `run_file` rows carry content. Each test is shown to fail against the old behaviour where the behaviour changed.
- [x] `.scratch/ate-v1/spec.md` (Mismatch handling, ~232) gets one sentence stating the headless rule. `docs/building-from-source.md` is updated if it describes the old answer.

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes.
