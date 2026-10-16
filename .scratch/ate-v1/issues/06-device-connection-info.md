# Device connection details and v1 connection types

Type: grilling
Status: open
Blocked by: 05

## Question

What is the structure of the connection-info table passed to `ATE.open`, and which Device connection types does v1 support (serial, VISA, UDP, TCP, …) with which parameters?

Answered since ("The ATE Lua API"): the enum is `DeviceType`, with string keys for every supported Device Type, and `params` is a plain table, built optionally with builders from a utility Lua file ATE ships. This ticket now means: which `DeviceType` values v1 supports; the `open`, `read`, `write` and `close` fields for each; the builder API; and how schedules load the utility file, given that `require` only resolves files listed in the Test Definition.
