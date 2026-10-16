# Test Definition format

ATE's definition of the Test Definition file, settled in "Test Definition format". Vocabulary follows `GLOSSARY.md`.

## What it is

A Test Definition is the file that describes one Media Release: the set of Lua files an installer delivers together. Several Media Releases can be installed side by side, one Test Definition each. A Media Release is not tied to a Project: some contain Test Schedules, others only helper modules for other releases to depend on.

## File

- A JSON file with the extension `.atedef`.
- Python reads and validates it before any Lua runs.

```json
{
  "format": 1,
  "mediaNumber": "MR-0107",
  "name": "Power board tests",
  "version": "2.3.0",
  "depends": [
    { "name": "Common helpers", "version": "^3.1" }
  ],
  "files": [
    { "path": "power_functional.tslua", "sha256": "9f2c…" },
    { "path": "rails.tslua", "sha256": "41aa…", "hidden": true },
    { "path": "lib/psu.lua", "sha256": "0b7e…" },
    { "path": "C:/ate/shared/limits.lua", "sha256": "d3c1…", "module": "limits" }
  ]
}
```

## Fields

| Field | Required | Meaning |
| --- | --- | --- |
| `format` | yes | Format version, currently `1`. An unknown version makes the Test Definition broken |
| `mediaNumber` | yes | The Media Release's configured media number, as used for artifact storage. Free-form string; the Media Release's unique identity. Not a version |
| `name` | yes | Descriptive name of the Media Release; what `depends` entries match against |
| `version` | yes | Semantic version (semver 2.0.0) of the Media Release |
| `depends` | no | Other Media Releases this one uses modules from (see Dependencies) |
| `files` | yes | Every Lua file in the Media Release |

### `files` entries

| Field | Applies to | Meaning |
| --- | --- | --- |
| `path` | all (required) | Location of the file (see Paths) |
| `sha256` | all (required) | Expected checksum: SHA-256 of the file's raw bytes, lowercase hex |
| `hidden` | `.tslua` only | If `true`, the Test Schedule is not listed for the Operator to pick. It can still be used as a Sub-schedule. Default `false` |
| `module` | `.lua` only | Module name `require` uses for this file (see Module names) |

The extension decides what a file is: `.tslua` is a Test Schedule (every Test Schedule is a complete, runnable schedule, including those used as Sub-schedules), and `.lua` is a module for `require`. Any other extension is an error.

## Paths

`path` may be:

- **relative**, resolved against the directory containing the `.atedef` file, and may use `..`; or
- **absolute**, such as `C:/ate/shared/limits.lua` or `/opt/ate/shared/limits.lua`.

Forward slashes work on both Windows and Linux.

## Module names

`require("x.y")` resolves to the `.lua` file whose module name is `x.y`:

- **Derived** for a relative path without `..`: the path minus `.lua`, with `/` replaced by `.`. `lib/psu.lua` is `lib.psu`.
- **Explicit** with the `module` field. It is **required** for an absolute path or a path containing `..`, and may be given for any `.lua` file to override the derived name.
- Names starting with `ate.` are reserved for modules bundled with ATE and may not be used.

## Dependencies

A `depends` entry is `{ "name": …, "version": … }`, where `version` is a semver range:

| Range | Matches |
| --- | --- |
| `3.1.4` | exactly 3.1.4 |
| `^3.1` | `>=3.1.0 <4.0.0` |
| `~3.1.2` | `>=3.1.2 <3.2.0` |
| `>=3.0 <4` | the stated bounds |

- ATE resolves each entry, at scan time, to the installed Media Release with that `name` and the highest `version` satisfying the range. Pre-release versions match only if the range itself names a pre-release.
- Dependencies are transitive: a dependency's own `depends` are resolved too.
- `require` looks in the Media Release's own files first, then in each dependency depth-first, in declaration order.

## Checksums

- SHA-256 over the file's raw bytes, written as lowercase hex. Installers copy files byte for byte, so files are identical on every platform and there is no line-ending normalisation.
- A mismatch, on a Test Schedule or on a module loaded by `require`, logs a warning and the file still loads. It is flagged on the Run screen and the Test Report.
- The Test Run records, for every file used, its expected and computed checksum, and for every Media Release in the resolved chain, its media number, name, version and the SHA-256 of its `.atedef` file.

`ate definition update <file.atedef>` recomputes every `sha256` in the file in place, filling in any that are missing.

## Finding and choosing Test Schedules

- ATE searches the Test Definition directories listed in the application config recursively for `.atedef` files at launch, and again when the Operator asks for a rescan.
- ATE loads every visible Test Schedule to read its `ScheduleParams` name and description.
- The Operator picks from one flat list of every visible Test Schedule across all Media Releases. Each row shows the schedule's name and its Media Release.
- The Project is not part of a Test Definition. The Operator enters it on the Run screen, prefilled from the application config's default, and it is recorded on the Test Run.

## Broken Test Definitions

These are detected at scan time. A broken Test Definition is shown with its error, and none of its Test Schedules can run:

- `format` unknown, a required field missing, or `version` not valid semver;
- a listed file missing on disk;
- a file with an extension other than `.tslua` or `.lua`;
- `hidden` on a `.lua` file, or `module` on a `.tslua` file;
- an absolute or `..` path to a `.lua` file without `module`;
- two entries with the same path, or two files with the same module name;
- a module name under `ate.`;
- a `depends` entry with no matching installed Media Release, a dependency cycle, or the same module name found in more than one Media Release of the resolved chain;
- a `depends` range matched by two installed Media Releases with the same `name` and `version` (ambiguous);
- two Test Definitions with the same `mediaNumber` (both are broken);
- a broken dependency.

A Test Schedule that raises an error while loading is listed as broken with that error; the rest of its Media Release is unaffected.
