# Flow control

Type: grilling
Status: resolved
Blocked by: 04

## Question

How do stop-on-failure versus continue, step retry, and Operator abort behave, and where is each configured (per step, per Test Schedule, per Test Run)?

Also from "Choose the Lua runtime" (09): an abort raises the Lua error `"aborted"` from blocking `ATE` calls and from a count hook, and every later `ATE` call raises again. Decide what the aborted step records and whether the Post-test Action still runs.

## Answer

- **Stop on fail** is a Test Run setting. `ScheduleParams:setStopOnFail(bool)` sets its default, and when unset it defaults to stop on fail. The Run screen's pre-run configuration has a radio button (stop on fail / continue on fail) for the Operator to change before the run starts. `FAIL` and `ERROR` both count; nothing is per step.
- **Failure prompt** (stop on fail): the run pauses with the failed step highlighted and offers **Continue** (keep the result, move on), **Retry** (run the step's action again) or **Abort**, with no timeout. With continue on fail, failures never prompt and retry isn't offered.
- **Retry** resets `POLL`, clears blocking inputs and keeps Devices open; there is no limit. Every Attempt is recorded, the step's result is its last Attempt's, and the run result uses final results, so a retried step can make the run pass. The report and History flag every retried step.
- **Steps that never run** after an abort or Pre-test failure record `NOTRUN`. An aborted step records `ERROR` "Aborted by Operator"; a step aborted at the failure prompt keeps its own `FAIL`/`ERROR`. Abort is whole-run only: no pause, resume or skip.
- **Pre-test Action failure** aborts automatically (no prompt): every step `NOTRUN`, and the log says which Pre-test Action failed and that this is why.
- **End reason**, recorded alongside `PASS`/`FAIL` and shown on the report and History: `COMPLETED`, `PRETEST_FAILED`, `STOPPED_ON_FAIL`, `ABORTED`.
- **Pre-test and Post-test Actions with Sub-schedules**: all Pre-test Actions run up front, depth first (parent first); all Post-test Actions run at the end in exact reverse. Each include is its own instance. A Sub-schedule whose steps are all disabled has its actions skipped. After a Pre-test failure, only the Post-test Actions of schedules whose Pre-test started run. Every Post-test Action receives the run's `passFail`.
- **Post-test chain always runs**, after a stop, a Pre-test failure or an abort. ATE clears the abort flag first; a second abort ends only the current Post-test Action, and an error in one is logged, fails the run, and the chain continues.
- **Run screen** gets a status bar: Pre-test/Post-test progress, total run time, and time in the current action or step.

## Comments

### Round 1, 2026-10-17

- **Stop on failure**: `ScheduleParams:setStopOnFail(bool)` sets the default; when the schedule doesn't set it, the default is **stop on fail**. The Run screen's pre-run configuration has a radio button (stop on fail / continue on fail) prefilled from that default, which the Operator can change before the run starts. `FAIL` and `ERROR` both trigger it. Nothing per step.
- **When the run stops on a failure**, the Operator chooses to **Continue**, **Retry** the step, or **Abort**.
- **Step retry is in v1**, through that prompt (replaces the "no retry" recommendation).
- **Steps that never ran** after an abort record `Result.NOTRUN`; `FORCED_NOTRUN` stays for test writers.
- **Pre-test Action failure**: every step records `NOTRUN` and the run goes straight to the Post-test Action with `FAIL`, ignoring stop on fail. The log states the Pre-test failure and its error, and says that this is why every step is `NOTRUN`.
- **Aborted step** records `Result.ERROR` with the message "Aborted by Operator"; no new enum value.
- **Run end reason** is recorded separately from `PASS`/`FAIL` and shown on the report and the History screen (values settled in round 2).
- **Post-test Action always runs**, called with `FAIL` after a stop, a Pre-test failure or an abort. ATE clears the abort flag before it starts so its `ATE` calls work; a second abort ends it immediately. This narrows "every later `ATE` call raises" from "Choose the Lua runtime" to "until the Post-test Action starts".
- **Abort scope**: only whole-run abort; no pause/resume and no skip-current-step.

### Round 2, 2026-10-17

- **Failure prompt** (stop on fail on): the run pauses with the failed step highlighted and offers Continue, Retry or Abort, with no timeout. Continue keeps the step's result and moves on, and the next failure prompts again. Abort makes the remaining steps `NOTRUN` and runs the Post-test Action; the failed step keeps its own `FAIL` or `ERROR`.
- **Retry** calls the step's action again from the start, resets `POLL` and clears blocking inputs; Devices stay open; no retry limit.
- **Attempts**: every attempt is recorded (number, result, message, log lines). The step's result is the last attempt's. Report and History show retried steps and how earlier attempts ended; storage detail goes to "What a Test Run records".
- **A retried step can make the run pass**: the run result uses each step's final result, and the report flags every retried step.
- **Pre-test Action failure** never prompts: the run aborts automatically (every step `NOTRUN`, then the Post-test Action with `FAIL`), whatever stop on fail is set to.
- **Run end reason**: `COMPLETED` (every step ran), `PRETEST_FAILED`, `STOPPED_ON_FAIL` (Abort chosen at a failure prompt), `ABORTED` (Abort pressed while a step ran). A Post-test Action failure doesn't change it; it's logged as an error and fails the run.
- **Continue on fail**: failures never prompt and retry isn't offered.
- **Sub-schedule Pre-test and Post-test Actions** (raised by the user): Pre-test Actions run top down, depth first (parent first, then each included Sub-schedule in order); Post-test Actions run in reverse. Details in round 3.

### Round 3, 2026-10-17

- **Ordering**: every Pre-test Action runs once, up front, before the first step, depth first in tree order (parent, then each included Sub-schedule in step order, recursively). Every Post-test Action runs once after the last step, in exactly the reverse order.
- **Sub-schedule Pre-test failure**: same automatic abort as the top level (`PRETEST_FAILED`). Later Pre-test Actions never run; Post-test Actions run in reverse only for schedules whose Pre-test Action started, including the failed one. The log names the failed Pre-test Action.
- **Every Post-test Action** receives the whole run's `passFail`.
- **Post-test chain**: an error in one is logged, fails the run, and the chain carries on. A second abort ends only the current Post-test Action; the rest still run.
- **Status bar** (user addition): a small bar at the bottom of the Run screen shows progress through the Pre-test and Post-test Actions, the total run time, and the time spent in the current action or step, so the Operator can judge whether to abort. Passed to the Run screen prototype (12).
- **Each include is its own instance**, with its own Pre-test and Post-test calls and `keyvals`, placed by its position in the tree.
- **Fully disabled Sub-schedule**: if every step of an included Sub-schedule (including its nested ones) is disabled, its Pre-test and Post-test Actions are skipped.
- **Sub-schedules are in v1.** "Sub-schedules" (14) gets this ordering as a settled input.
