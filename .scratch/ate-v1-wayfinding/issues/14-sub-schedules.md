# Sub-schedules

Type: grilling
Status: resolved
Blocked by: 07

## Question

How do Sub-schedules work: how is the `setInclude(file, keyvals)` file resolved (it must be one listed in the Test Definition), how does a Sub-schedule read its `keyvals`, how does `setPrefix` combine with Step Numbers, and can Sub-schedules nest?

From "Test Definition format" (07): every `.tslua` is a complete, runnable Test Schedule. `hidden` only keeps one out of the picker. Decide whether `setInclude` can reach `.tslua` files in dependency Media Releases as well as the schedule's own.
Answered since ("Flow control", 10): Sub-schedules are in v1. Each include is its own instance. All Pre-test Actions run up front, depth first (parent first); all Post-test Actions run at the end in reverse; a Sub-schedule whose steps are all disabled has its actions skipped.

## Answer

**Resolving `file`.** `file` is a path resolved against the directory holding the including schedule's `.atedef`, using the same rules as `path` entries, and it must match a listed `.tslua` file. If it isn't in the including release's own files, ATE searches its dependency Media Releases depth first, in declaration order, as `require` does. A path matched in more than one release of the chain is an error. The Sub-schedule's checksum is checked and recorded as for `require`: a mismatch warns and the file still loads.

**Expansion.** ATE expands the whole Sub-schedule tree every time it loads a schedule: at scan time, so problems show in the picker, and again at run start in the fresh runtime. A Sub-schedule that is missing, not listed, ambiguous, cyclic or raises while loading breaks the *including* Test Schedule, listed with the error; the rest of the Media Release is unaffected.

**`keyvals`.** The chunk receives it as its argument: `local kv = ...`. ATE runs the `.tslua` chunk again for every include, so each instance has its own locals and closures; modules loaded with `require` stay shared, cached once per Test Run. A top-level schedule, or an include without `keyvals`, receives `{}`. `keyvals` is plain data only (strings, numbers, booleans and nested tables of those); anything else is a load error. It is recorded per instance.

**Step Numbers.** `setPrefix(p)` prepends `p` as it is, with no separator (`"4."` + `"1."` gives `"4.1."`); without it the numbers are unchanged. Prefixes stack through nesting from the outside in. The duplicate Step Number check runs on the fully expanded tree.

**Nesting.** Any depth. A file that appears twice on one include chain is a cycle; the same file in sibling branches is fine.

**The include step is a header row.** It is not a Test Step: it has no `Result` and is not counted in the summary. It shows its own Step Name (from `StepParams(name)`, never the Sub-schedule's `ScheduleParams` name) and an optional `setStepNo`. That Step Number lives in the parent's numbering, so outer prefixes apply but not its own `setPrefix`, and it takes part in the duplicate check. The prefix is never derived from it. It accepts `setInclude`, `setPrefix`, `setStepNo` and `setEnable`. `setInputs` or `setAction` with `setInclude`, or `setPrefix` without it, is a load error.

**Enabling.** `setEnable` on an include step, if called, forces the default of every step beneath it, nested ones included; if not called, those steps keep their own defaults. Before the run, toggling the header sets everything beneath it to its state, and individual steps beneath can still be toggled afterwards. A header over mixed states shows a partial (tri-state) mark. A fully disabled Sub-schedule skips its Pre-test and Post-test Actions (from "Flow control").

**The Sub-schedule's own `ScheduleParams`.** When it is included, only its Pre-test Action, Post-test Action and steps take part. Its doc fields and stop-on-fail are ignored without warning. `ATE.run` describes the top-level schedule. The instance's `ScheduleParams` name, file, prefix and `keyvals` are recorded.

**Step Names (amends "Test Step structure and outcome model").** `StepParams(name)` takes an optional Step Name for every step, ordinary steps and include headers alike. It is shown on the Run screen and the Test Report and recorded on the Test Run.

Handed on: how headers and roll-ups look goes to "Run screen prototype" and "Test Report format"; storing headers, instances and Step Names goes to "The Test Run record".
