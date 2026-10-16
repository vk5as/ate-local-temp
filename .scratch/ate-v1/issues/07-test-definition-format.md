# Test Definition format

Type: grilling
Status: resolved
Blocked by: none

## Question

What is the Test Definition file format: fields (Media Release number and name, file list), checksum algorithm, behaviour on missing files or checksum mismatch, how Test Definitions relate to Projects, and how the Operator selects a Test Schedule from those found in the configured directories?

## Answer

Full definition: `assets/test-definition-format.md`.

- **File**: JSON, extension `.atedef`, with `"format": 1`. Python validates it before any Lua runs.
- **Media Release vs Test Definition**: distinct, one-to-one. The Media Release is the installed set of Lua files; the Test Definition is the file describing it. Several can be installed side by side.
- **Fields**: `mediaNumber` (the configured artifact-storage number and the Media Release's unique identity, not a version), `name` (descriptive), `version` (required semver), optional `depends`, and `files`.
- **Files**: one list of `{path, sha256, hidden?, module?}`. `.tslua` entries are Test Schedules and `.lua` entries are modules. `hidden` (`.tslua` only) keeps a schedule out of the picker; every `.tslua` is a complete schedule, including those used as Sub-schedules. Paths may be relative (including `..`) or absolute. A module's name is derived from its relative path, or given by `module`, which is required for absolute and `..` paths.
- **Projects**: not part of a Test Definition, and a Media Release may hold only helpers. The Project is entered per Test Run, prefilled from an application config default.
- **Dependencies**: `{name, version}` with an npm-style semver range, resolved at scan time to the highest matching installed release. Transitive; `require` searches the release's own files first, then its dependencies depth-first in declaration order.
- **Checksums**: SHA-256 of the raw bytes, lowercase hex. Installers copy files byte for byte, so there is no line-ending normalisation. A mismatch warns, still loads, and is flagged on the Run screen and the Test Report. The Test Run records the expected and computed checksum of every file used, plus each resolved Media Release's media number, name, version and `.atedef` SHA-256. `ate definition update` recomputes checksums.
- **Broken at scan time, not runnable**: missing files, bad extensions or fields, duplicate paths or module names, unresolved, cyclic or ambiguous dependencies, and duplicate `mediaNumber`s (both copies). A `.tslua` that errors on load is listed as broken.
- **Selection**: ATE searches the configured directories recursively at launch and on rescan, loads each visible `.tslua` for its `ScheduleParams` name, and lists every visible schedule from every release in one flat list showing its Media Release.
