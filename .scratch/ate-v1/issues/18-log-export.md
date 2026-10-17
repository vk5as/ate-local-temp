# Export a Test Run's log from History

Type: grilling
Status: resolved
Blocked by: 11, 17

## Question

From History, a Test Run's log can be exported to a format other than the report. Which format or formats (for example CSV, JSON Lines, plain text)? What does an export contain: which levels (including `DEBUG`), Device trace, the owner of each line (run, action, step Attempt), the source, and local or UTC times with the offset? Is it the whole run, or can it be filtered? Where is it written, and how is it named, following the report's file-name rules in `assets/test-report-format.md`?

Also from "Render a sample Test Report to PDF" (17): this is the escape hatch for runs whose log is too large for the report, since History can regenerate a report without the Run log. The log record is defined in `assets/test-run-record.md`.

## Answer

Full definition: `assets/log-export.md`.

- **Where**: History only, for any finished Test Run (including `INTERRUPTED`), one run per export. ATE has **no log viewer**; external tools do the drilling down.
- **Export dialog**: checkboxes for Level (`DEBUG`…`ERROR`), Source (`schedule`, `ate`, `device`) and Owner (a tree of Run, actions and steps; a Sub-schedule header ticks its subtree, and a step brings all its Attempts). All are ticked each time, and Export is disabled when nothing would be exported. They may be refined after hands-on use.
- **Formats**: CSV (default; UTF-8 with a BOM), JSON Lines and Text, chosen in the Save As file-type list. The default name is the report's with `_log` added.
- **Rows**: one per line, in recorded order, with `time` (local, milliseconds, offset), `level`, `source`, `owner`, `schedule`, `step_number`, `step_name`, `attempt` and `text`.
- **Filters recorded** only in the Text format's header. CSV and JSON Lines are pure data.
- **New term**: Run log.

## Comments

### Round 1, 2026-10-18

- All three formats: CSV, JSON Lines and Text.
- Choosing what gets exported with checkboxes, for drilling down into logs.
- Accepted: one row per line in recorded order with the proposed columns; local time with offset and milliseconds; Save As with the report-based default name; one run per export; the same runs as report regeneration; History only; "Run log" as a glossary term.

### Round 2, 2026-10-18

- No viewer built into ATE. The checkboxes only choose what goes into the export, and external apps do the drilling down.
- Level, Source and Owner checkbox groups accepted. They may be refined after hands-on experience.
- Accepted: the format chosen in the Save As file-type list, the JSON Lines shape, the Text layout with a header, and no record of the filters in CSV or JSON Lines.
- Agreed in the final summary: the checkboxes reset to all ticked each time, and Export is disabled when nothing would be exported.
