# Export a Test Run's log from History

Type: grilling
Status: open
Blocked by: 11, 17

## Question

From History, a Test Run's log can be exported to a format other than the report. Which format or formats (for example CSV, JSON Lines, plain text)? What does an export contain: which levels (including `DEBUG`), Device trace, the owner of each line (run, action, step Attempt), the source, and local or UTC times with the offset? Is it the whole run, or can it be filtered? Where is it written, and how is it named, following the report's file-name rules in `assets/test-report-format.md`?

Also from "Render a sample Test Report to PDF" (17): this is the escape hatch for runs whose log is too large for the report, since History can regenerate a report without the Run log. The log record is defined in `assets/test-run-record.md`.
