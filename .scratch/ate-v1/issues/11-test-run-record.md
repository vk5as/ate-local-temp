# What a Test Run records

Type: grilling
Status: open
Blocked by: 04, 07, 10, 14

## Question

What does a Test Run store: Project, UUT name and serial, Operator, Station, Media Release, per-file expected and computed checksums, step outcomes, measurements, inputs, log lines? What SQLite schema holds it without precluding a later central multi-site database?
Also from "Device connection details and v1 connection types" (06): the record holds the ATE version and the checksum of each bundled `ate.` module used, and Device trace lines when tracing is on.
