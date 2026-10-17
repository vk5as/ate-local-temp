# Sub-schedules

Type: grilling
Status: open
Blocked by: 07

## Question

How do Sub-schedules work: how is the `setInclude(file, keyvals)` file resolved (it must be one listed in the Test Definition), how does a Sub-schedule read its `keyvals`, how does `setPrefix` combine with Step Numbers, and can Sub-schedules nest?

From "Test Definition format" (07): every `.tslua` is a complete, runnable Test Schedule. `hidden` only keeps one out of the picker. Decide whether `setInclude` can reach `.tslua` files in dependency Media Releases as well as the schedule's own.
Answered since ("Flow control", 10): Sub-schedules are in v1. Each include is its own instance. All Pre-test Actions run up front, depth first (parent first); all Post-test Actions run at the end in reverse; a Sub-schedule whose steps are all disabled has its actions skipped.
