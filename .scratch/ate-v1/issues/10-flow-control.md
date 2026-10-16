# Flow control

Type: grilling
Status: open
Blocked by: 04

## Question

How do stop-on-failure versus continue, step retry, and Operator abort behave, and where is each configured (per step, per Test Schedule, per Test Run)?

Also from "Choose the Lua runtime" (09): an abort raises the Lua error `"aborted"` from blocking `ATE` calls and from a count hook, and every later `ATE` call raises again. Decide what the aborted step records and whether the Post-test Action still runs.
