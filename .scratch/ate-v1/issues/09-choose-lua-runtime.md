# Choose the Lua runtime

Type: grilling
Status: open
Blocked by: 01, 02, 05

## Question

Given the target Lua version (5.5), the required ATE API, and the binding survey, which Python ↔ Lua implementation does ATE use?

Inputs: the survey points to lupa. Still to settle: the coroutine vs worker-thread model for `ATE.input`, given the Lua 5.1 `pcall` limitation and that coroutines lose error details. Also check lupa with `QThread` on Windows.
Answered since: target Lua 5.5, which lupa bundles; its `pcall` limitation applied only to 5.1. `POLL` inputs must stay clickable while a step runs, so the GUI must never be blocked by Lua execution. That points to a worker thread, or to frequent yields.
Answered since: Test Schedules' own use of coroutines need not be supported. So ATE may use coroutines internally to pause steps, as an alternative to a worker thread.
Also from "Device connection details and v1 connection types" (06): ATE reads every open Device in the background and buffers incoming data until `ATE.read`, so the runtime's threading model must allow background I/O alongside a paused step.
