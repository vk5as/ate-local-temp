# Sub-schedules

Type: grilling
Status: open
Blocked by: 07

## Question

Does ATE v1 support Sub-schedules at all? If so, how do they work: how is the `setInclude(file, keyvals)` file resolved (it must be one listed in the Test Definition), how does a Sub-schedule read its `keyvals`, how does `setPrefix` combine with Step Numbers, can Sub-schedules nest, and are their Pre-test and Post-test Actions run?

From "Test Definition format" (07): every `.tslua` is a complete, runnable Test Schedule. `hidden` only keeps one out of the picker. Decide whether `setInclude` can reach `.tslua` files in dependency Media Releases as well as the schedule's own.
