# The Test Report

ATE's definition of the Test Report, settled in "Test Report content and format". Vocabulary follows `GLOSSARY.md`. Every value shown comes from the Test Run record (`test-run-record.md`); a report never needs anything else.

## Producing reports

- **At run end** ATE writes the HTML and the PDF into `reports.directory` without asking. If PDF rendering fails, the HTML is still written and the error is logged.
- **Later**, the History screen regenerates either format for any finished Test Run, including an `INTERRUPTED` one. A run still in progress can't be reported.
- Generated files are outputs only; they are not stored in the database.
- A regenerated report uses the **current** ATE's template. The footer shows both versions, so a difference is visible.

### File names

`<reports.directory>/<UUT Type>/<UUT Type>_<serial>_<local start yyyymmdd-HHMMSS>_<PASS|FAIL>.html`, and the same with `.pdf`.

- Characters not allowed in Windows file names are replaced with `_`, on both platforms.
- An existing file is never overwritten: a regenerated report takes the first free suffix (`-2`, `-3`, …).

## Format

- **HTML** is the source: one self-contained file from a bundled Jinja2 template, with inline CSS and no external assets. A logo is embedded as a data URI.
- **PDF** is rendered from that HTML. The engine is chosen by "Render a sample Test Report to PDF".
- **Paper**: `reports.paperSize`, `A4` (default) or `Letter`, portrait.
- **Every page** has:
  - a header with `reports.organisation` and logo, the doc title, doc id and revision, and the UUT Type and serial;
  - a footer with the run UUID, "Run recorded by ATE x.y · Report generated `<local time>` by ATE a.b", and "Page X of Y".
- The step table's column headings repeat after every page break.
- **Times** are shown in the Station's local time from `utc_offset`, with the offset shown (`2026-10-17 14:03:22 +10:30`).
- The only customisation in v1 is `reports.organisation`, `reports.logo` and `reports.paperSize` (see `app-config-format.md`).

## Sections

In this order: Cover → Steps → Equipment → Configuration → Run log.

### Cover

- **Document**: doc title, doc id and doc revision from the top-level schedule (`setDocTitle`, `setDocId`, `setDocRev`). Without a title, the top-level `ScheduleParams` name is used. A missing id or revision is left out.
- **Result**: a large **PASS** or **FAIL**, with the End Reason under it.
  - `PRETEST_FAILED`: names the schedule whose Pre-test Action failed and its error message.
  - A Post-test Action that errored is named the same way.
- **Banners** (warnings) when:
  - any file's checksum didn't match;
  - the run is `INTERRUPTED`.
- **Run details**: UUT Type, serial, Project, Station name, start and end in local time, duration, ATE version, Stop on Fail as set at run start.
- **Schedule**: top-level schedule name, description and file, and its Media Release (Media Number, name, version).
- **Operator**: the Operator's name with a signature box beside it, big enough for a handwritten signature or a signature image applied later, and a date line. The box is only space on the page; the PDF has no signature form field.
- **Summary**: a count of steps for each of the seven `Result` values, zeros included. Include header rows are not counted. Also the number of retried steps and the number of Operator enable overrides.
- **Failures**: every step whose result is `FAIL`, `ERROR` or `NOREPORT`, with its Step Number, Step Name and message. Omitted when there are none.

### Steps

One row per node of the expanded tree, in tree order, disabled steps included.

| Step Number | Step Name / message | Expected | Actual | Result |
| --- | --- | --- | --- | --- |

- **Step Name / message**: the Step Name, with the message from the last Attempt under it in smaller type.
- **Expected** and **Actual** are printed exactly as stored. ATE adds no units or limits.
- **Result** is the step's result: the last Attempt's, or `NOTRUN`.
- **Input values** the Operator entered appear as a small line under the name and message: "Input 1 (LED colour): Green".
- **Operator overrides** are marked under the name: "disabled by Operator" or "enabled by Operator", whenever `enabled` differs from `default_enabled`.
- **Retried steps** show "↻ n" with the attempt count. Earlier Attempts follow as indented sub-rows with the same columns, with the prompt choice after the message ("→ RETRY"). The main row always shows the last Attempt.
- **Include header rows** are indented by nesting depth. The header's Step Name spans Step Name through Actual, after its optional Step Number. The Result column holds counts of the steps beneath ("4 steps: 3 PASS, 1 FAIL"), styled so they don't read as a `Result`.

### Equipment

The Equipment the schedule declared with `ATE.addEquipment`, in declaration order:

| Name | Serial | Connection | Cal due | Notes |
| --- | --- | --- | --- | --- |

Values are printed exactly as given; nil values are blank. With no Equipment, the section says "No equipment declared."

### Configuration

Only what the run actually used:

- **Media Releases**: the top-level release and every release that supplied at least one used file. Shows Media Number, name, version and `.atedef` SHA-256.
- **Files**: every `run_file` row (each `.tslua` loaded, each `.lua` `require`d, and each bundled `ate.` module). Shows path, owning Media Number, expected and computed SHA-256, and whether the bytes were stored. Mismatches are highlighted. `ate.` modules have no expected checksum.
- **Sub-schedule instances**: each instance in tree order, with its `ScheduleParams` name, file and prefix. `keyvals` are not shown.

There is no section for Pre-test and Post-test Actions. Their failures appear on the cover, and their log lines in the Run log.

### Run log

Every `INFO`, `WARNING` and `ERROR` line, grouped by owner: the run as a whole, then each Pre-test and Post-test Action, then each step Attempt. Lines appear in time order within each group. `DEBUG` lines and Device trace never appear in a report; they stay in the database.

## Not in v1

- Report generators scripted in Lua, and user-supplied templates.
- Cryptographic signing of reports, and PDF signature fields (v2).
