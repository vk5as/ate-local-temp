# Application config file

Type: grilling
Status: resolved
Blocked by: 07

## Question

Which settings does the application config JSON hold (Test Definition search directories, database location, Station identity, report output directory, log settings, …), with what defaults, and where does the file live on each platform?

Inputs: "Device connection details and v1 connection types" (06) needs a Device tracing setting, off by default, and the length at which traced bytes are truncated.
From "Test Definition format" (07): a default Project, and the Test Definition directories, which are searched recursively.

## Answer

Full definition: `assets/app-config-format.md`.

- **Location**: machine-wide. Windows `%PROGRAMDATA%\ATE\config.json`, Linux `/etc/ate/config.json`; `--config <path>` overrides it. Read once at launch; no per-user layer.
- **Missing file**: a prompt shows the path and offers "Create default config and launch" or "Abort". Creating writes the defaults and a new `station.uuid`; if the write fails, ATE refuses to start.
- **Invalid file**: bad JSON, wrong types, unknown keys or an unknown `format` stop startup, naming the file and key.
- **Station**: `station.name` (defaults to the hostname) and `station.uuid`, generated if missing and written back to the file. If it cannot be written, ATE refuses to start and shows the UUID for an admin to add. The UUID lives in the file, not only in the database, so the Station knows its own identity. A cloned UUID is not detected; the setup notes say never to copy it.
- **Paths**: defaults live under one data directory (`%PROGRAMDATA%\ATE`, `/var/lib/ate`): `database.path`, `reports.directory`, `log.directory`, and `testDefinitions.directories`. Paths may be absolute or relative to the config file. There is no variable expansion. A missing definitions directory is a warning.
- **Application log**: `log.level` (default `info`). One file per launch, never rotated (so a multi-day Test Run is never split), never deleted automatically.
- **Other settings**: `deviceTrace.enabled` (`false`), `deviceTrace.maxBytes` (`256`), `defaultProject` (`""`; a run cannot start with an empty Project), and `ui.theme` (`system`/`light`/`dark`).
- **Shape**: `"format": 1`, with camelCase sectioned keys.
- **Left to other tickets**: the database engine, and Test Report auto-save and naming.
