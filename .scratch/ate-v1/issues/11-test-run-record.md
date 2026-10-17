# What a Test Run records

Type: grilling
Status: open
Blocked by: 04, 07, 10, 14

## Question

What does a Test Run store: Project, UUT name and serial, Operator, Station, Media Release, per-file expected and computed checksums, step outcomes, measurements, inputs, log lines? What SQLite schema holds it without precluding a later central multi-site database?
Also from "Device connection details and v1 connection types" (06): the record holds the ATE version and the checksum of each bundled `ate.` module used, and Device trace lines when tracing is on.
Also from "Test Definition format" (07): the Project is per Test Run, entered by the Operator. The record holds the media number, name, version and `.atedef` SHA-256 of every Media Release in the resolved dependency chain, and flags checksum mismatches for the Test Report.
Also from "Flow control" (10): the run's stop-on-fail setting and end reason (`COMPLETED`, `PRETEST_FAILED`, `STOPPED_ON_FAIL`, `ABORTED`); every Attempt of a retried step (number, result, message, log lines), with the step's result being the last Attempt's; retried steps are flagged on the report.
