# 04: Application config in full, and interrupted-run recovery

**What to build:** The machine-wide config behaves as specified (see `app-config-format`), and a Test Run left unfinished by a crash is closed out at the next launch.

**Blocked by:** 02 (Tracer bullet: run a one-step Test Schedule and record it)

**Status:** ready-for-agent

- [ ] Config is found at `%PROGRAMDATA%\ATE\config.json` / `/etc/ate/config.json`, or `--config <path>`, and read once at launch
- [ ] A missing config prompts with the path: "Create default config and launch" or "Abort"; creating writes defaults and a fresh `station.uuid`; a failed write refuses to start
- [ ] Invalid JSON, a wrong type, an unknown key or an unknown `format` refuses to start, naming the file and key; `database.type` other than `sqlite` is rejected
- [ ] `station.name` defaults to the hostname; a missing `station.uuid` is generated and written back preserving key order, or startup is refused showing the UUID if the file can't be written
- [ ] Relative paths resolve against the config file's directory, absolute paths work, forward slashes work on Windows, no variable or `~` expansion; a missing Test Definition directory is a warning
- [ ] Every documented key exists with its default (including `defaultProject`, `ui.*`, `devices.receiveBufferBytes`, `deviceTrace.*`, `reports.*`, `database.storeMismatchedFiles`)
- [ ] The application log is one file per launch named by start time, at `log.level`, never rotated or deleted
- [ ] At launch any Test Run with no end time is closed with End Reason `INTERRUPTED` and result `FAIL`, and this is logged
- [ ] Tests cover each refusal, the prompt path, UUID write-back and interrupted-run recovery (kill a run mid-way, relaunch)

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
