# Application config file

ATE's application config, settled in "Application config file". Vocabulary follows `GLOSSARY.md`.

## Location

The config describes the Station, so it is machine-wide:

| Platform | Config file | Data directory |
| --- | --- | --- |
| Windows | `%PROGRAMDATA%\ATE\config.json` | `%PROGRAMDATA%\ATE` |
| Linux | `/etc/ate/config.json` | `/var/lib/ate` |

`--config <path>` on the command line overrides the config file location.

## Loading

- ATE reads the file once at launch; changes need a restart. A rescan re-reads Test Definitions, not the config. There is no per-user override layer.
- **Missing file**: ATE shows the expected path and offers "Create default config and launch" or "Abort". Creating writes the defaults below, including a new `station.uuid`. If the file cannot be written, ATE refuses to start, showing the path.
- **Invalid file**: invalid JSON, a value of the wrong type, an unknown key or an unknown `format` stops ATE from starting, with an error naming the file and key.

## Example

```json
{
  "format": 1,
  "station": {
    "name": "BENCH-03",
    "uuid": "3f6c1e2a-9b7d-4c1e-8a55-0d2f4b9e7c10"
  },
  "defaultProject": "",
  "testDefinitions": {
    "directories": ["definitions", "D:/releases"]
  },
  "database": { "type": "sqlite", "path": "ate.db", "storeMismatchedFiles": "ask" },
  "reports": { "directory": "reports", "organisation": "", "logo": null, "paperSize": "A4" },
  "log": { "directory": "logs", "level": "info" },
  "deviceTrace": { "enabled": false, "maxBytes": 256 },
  "ui": { "theme": "system" }
}
```

## Settings

Keys are camelCase, grouped in sections.

| Key | Default | Meaning |
| --- | --- | --- |
| `format` | `1` | Format version. Required |
| `station.name` | the computer's hostname | Descriptive name of the Station; may be renamed |
| `station.uuid` | generated | The Station's stable identity, recorded on every Test Run |
| `defaultProject` | `""` | Prefills the Project on the Run screen. A Test Run cannot start with an empty Project |
| `testDefinitions.directories` | `["<data>/definitions"]` | Directories searched recursively for `.atedef` files |
| `database.type` | `sqlite` | Database backend. v1 accepts only `sqlite`; any other value stops startup |
| `database.path` | `<data>/ate.db` | The SQLite database file. Created only by `ate db migrate` (see `test-run-record.md`) |
| `database.storeMismatchedFiles` | `ask` | `ask`, `always` or `never`: whether the raw bytes of a Lua file with a checksum mismatch are stored with the Test Run |
| `reports.directory` | `<data>/reports` | Where Test Reports are written |
| `reports.organisation` | `""` | Text in every Test Report page header |
| `reports.logo` | `null` | Image file embedded in every Test Report page header |
| `reports.paperSize` | `A4` | `A4` or `Letter` |
| `log.directory` | `<data>/logs` | Where the application log is written |
| `log.level` | `info` | Application log level |
| `deviceTrace.enabled` | `false` | Device tracing at `Log.DEBUG` (see `ate-table-reference.md`) |
| `deviceTrace.maxBytes` | `256` | Length past which traced bytes are truncated |
| `ui.theme` | `system` | `system`, `light` or `dark` |

## Station identity

- If `station.uuid` is missing, ATE generates one and writes it back into the file, keeping the other keys and their order. If the file cannot be written (for example, an Operator without admin rights), ATE refuses to start and shows the generated UUID for an admin to add.
- Never copy `station.uuid` between Stations. ATE does not detect a cloned UUID.

## Paths

- Absolute or relative. Relative paths resolve against the config file's directory.
- No environment variable or `~` expansion. Forward slashes are accepted on Windows.
- A configured Test Definition directory that does not exist is a warning on the Run screen, not a startup failure.

## Application log

The application log records ATE's own activity (startup, scans, crashes). It is separate from the log lines recorded with a Test Run.

- One file per application launch, named by its start time. It never rotates, so a Test Run is never split across files.
- Log files are never deleted automatically.

## Decided elsewhere

- The database schema, migrations and the `ate db` CLI: `test-run-record.md`.
- When Test Reports are written and how they are named: `test-report-format.md`.
