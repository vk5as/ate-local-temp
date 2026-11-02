# Running an ATE Station

What an agent needs to know to install, prepare, run, debug and upgrade an ATE Station. Every key,
command and message here is checked against the code by `tests/test_manuals.py`: the config keys
and the `ate` commands are derived from `src/ate/config.py` and `src/ate/cli.py`, every command
listed below is executed in a temporary Station, and each message this manual quotes is checked
against the file that prints it. The message check is a hand-written list, `QUOTED_MESSAGES` in
`tests/test_manuals.py`: it proves the messages it contains are still what the code prints, and it
only covers the messages someone thought to put in it. Facts the design specs under
`.scratch/ate-v1-wayfinding/assets/` state differently from the code are listed at the end.

The manual for a human is `docs/manuals/admin/main.tex` (eleven chapters, built to PDF by
`docs/manuals/build.py admin`). This file is not a conversion of it: it is written for an agent, in
the order an agent works.

## The job, in order

1. Install the artifact for the platform.
2. `ate setup` — directories, default config, database.
3. Set the config keys that describe the bench.
4. Put Media Releases where the config says ATE looks; keep their checksums honest.
5. Run. Read the Test Report and the two logs.
6. When something fails, start from the exact text the machine printed.

## What is where

| Thing | Windows | Linux |
| --- | --- | --- |
| Config | `%PROGRAMDATA%\ATE\config.json` | `/etc/ate/config.json` |
| Data directory (`<data>`) | `%PROGRAMDATA%\ATE` | `/var/lib/ate` |
| Database | `<data>/ate.db` | `/var/lib/ate/ate.db` |
| Test Reports | `<data>/reports/<UUT Type>/` | `/var/lib/ate/reports/<UUT Type>/` |
| Application log | `<data>/logs/ate-app-<launch time>.log` | same |
| Media Releases | `<data>/definitions/` | `/var/lib/ate/definitions/` |
| Program | `C:\Program Files\ATE` | `/opt/ate`, linked at `/usr/bin/ate` |
| Licence notices | `<program>\_internal\licences` | `<program>/_internal/licences` |
| Run log | table `log_line` in the database | same |

Windows: config and data are the same directory. Linux: they are deliberately apart, because the
config is admin-only and the data is group-writable.

## The commands

Everything below is run by `tests/test_manuals.py` in a temporary Station. The commands that are
not run here are marked, with what covers them instead.

```bash
ate                      # opens the application; exit 1 with a refusal if config or database is bad
ate --help               # usage at any level, exit 0
ate -V                   # same as --version
ate --version
ate --config PATH        # the platform config to read instead of the default; it may sit before
                         # the subcommand (`ate --config PATH db status`) or after it
                         # (`ate db status --config PATH`)
ate --allow-unsandboxed-pdf   # privileged only: render Test Report PDFs without Chromium's sandbox
```

`ate` with no subcommand opens the GUI, so the suite drives it through `launch()` with the dialogs
injected (`tests/test_gui_launch_30a.py`) rather than running it as a process.

```bash
ate setup                                    # as root on Linux, as admin on Windows
ate setup --root DIR                         # put the Station under DIR/data and DIR/config
ate setup --data-dir DIR --config-dir DIR    # override one of the two; packagers and tests
ate setup --config FILE                      # read FILE instead of <config-dir>/config.json
ate setup --no-permissions                   # skip the group/ownership/ACL treatment
```

`--config` works before the subcommand too — `ate --config X setup` — and a `--config` written on
both sides with different values is refused rather than resolved. `--config=` with nothing after it
is a usage error, not a default.

`ate setup` is idempotent: it creates nothing that exists and never overwrites an existing config.
It also runs the migration `ate db migrate` runs — the same routine, `migrate_database` in
`src/ate/storage/backup.py` — and it migrates the database the config names: it reads
`config.json`, takes `database.path` from it, and prints the path and the reason
(`database to migrate: /var/lib/ate/ate.db (database.path in /etc/ate/config.json)`). Setup falls
back to the data directory it just prepared when the config cannot be read (it does not exist yet,
or setup is not elevated and the config is admin-only) or when the config names no `database.path`.
Bare `ate setup` writes to the platform directories, which the test suite is not allowed to touch;
the `--root` form is what is executed.

Moving a Station is copying its config file and its data directory: setup writes every path in the
config out in full, not as `<data>`, so a copied config still names the old locations. Edit
`database.path`, `reports.directory`, `log.directory` and `testDefinitions.directories`, or run
`ate setup --root DIR` at the new place and copy the database into what it made. Setup will not
rewrite paths for you, because it never overwrites a config that exists.

Cloning needs one more edit: the copied config carries the original `station.uuid`, and ATE never
detects two Stations with the same one. Blank it (`"uuid": ""`) or delete the key on the copy; the
next launch generates a fresh uuid and writes it back.

```bash
ate db status [PATH]                         # print PRAGMA user_version, exit 0; read-only
ate db migrate [PATH]                        # copy the database if a migration will run, then upgrade
ate db migrate --yes                         # no prompt; -y is the same
ate definition update <file.atedef>          # recompute every listed sha256, in place
```

Hidden helpers, never in `ate --help`, and not for a person to type:

```bash
ate _pdf <html> <pdf> --paper A4|Letter [--unsandboxed]
ate _selftest                                # a frozen bundle inventories what it carries
ate _selftest --run                          # run a built-in one-step schedule headless
ate _selftest --scan DIR [--config PATH]     # release CI's headless launch
```

`_pdf` is started by the report writer; its behaviour is asserted in
`tests/test_pdf_sandbox_choice_30d.py`. `_selftest`, `_selftest --run` and `_selftest --scan` are all
run by the suite. `--scan` on the shipped example release exits 1, because that release contains one
deliberately broken schedule, and its output says `1 broken`.

The licence notices are generated by:

```bash
python packaging/licence/notices.py --out build/licences
```

which writes `NOTICE-Qt-PySide6.txt`, `THIRD_PARTY_NOTICES.txt`, `LGPL-3.0.txt` and `GPL-3.0.txt`.

## The config file

One file, read once at launch. Restart ATE for a change to take effect. There is no per-user
override, no environment expansion in values, no `~` expansion. Relative paths are resolved against
the config file's own directory. `<data>` is the only placeholder; any other `<token>` is an error
naming the file and key.

Every key the validator knows, with its default:

| Key | Default | Note |
| --- | --- | --- |
| `format` | required | must be exactly `1` |
| `defaultProject` | `""` | prefills the Project field; a run cannot start with an empty Project |
| `station.name` | hostname | recorded on every Test Run; may be renamed |
| `station.uuid` | generated | if missing or empty ATE generates one and writes it back; a cloned UUID is not detected |
| `testDefinitions.directories` | `<data>/definitions` | searched recursively; a missing directory warns, it does not fail |
| `database.type` | `sqlite` | v1 accepts only `sqlite` |
| `database.path` | `<data>/ate.db` | created and upgraded by `ate db migrate` |
| `database.storeMismatchedFiles` | `ask` | `ask`, `always` or `never`; anything else is refused |
| `reports.directory` | `<data>/reports` | where Test Reports are written |
| `reports.organisation` | `""` | text in the Test Report page header |
| `reports.logo` | `null` | the validator accepts only `null`, though the renderer can embed a logo — see the disagreements at the end |
| `reports.paperSize` | `A4` | `A4` or `Letter`; the config validates it, so a typo stops startup rather than failing at render time |
| `log.directory` | `<data>/logs` | the application log |
| `log.level` | `info` | `debug`, `info`, `warning`, `error`; an unknown name is treated as `info` |
| `devices.receiveBufferBytes` | 16777216 | per-Device receive buffer; must be a positive integer |
| `deviceTrace.enabled` | `false` | one `DEBUG` Run-log line per Device operation |
| `deviceTrace.maxBytes` | 256 | traced bytes are shortened past this |
| `ui.theme` | `system` | stored, never read by the shipped code |
| `ui.layout` | `staged` | `staged` or `threePane`; anything else is refused |
| `ui.allowLayoutSwitch` | `true` | whether the layout can be changed for the session |
| `ui.allowHistoryDuringRun` | `true` | whether History can be browsed during a run |

A key that is missing is filled with its default. A key that is present is type-checked. An unknown
top-level key, a section that is not an object, bad JSON, a bad `format`, a `database.type` that is
not `sqlite`, a `storeMismatchedFiles` outside `ask`/`always`/`never`, a `ui.layout` that is neither
shipped layout, and a `receiveBufferBytes` of zero or less all stop startup with a message naming the
file and the key.

`ATE_DEFAULT_CONFIG` and `ATE_DATA_DIR` move the platform defaults. They exist for the test suite
(`tests/test_manuals.py` uses them to run `ate db status` against a temporary Station) and nothing
else reads them. Do not use them on a Station.

## The database

One SQLite file, in WAL mode, schema version 5 (`SCHEMA_VERSION` in `src/ate/storage/schema.py`).
Every Test Run is a record; nothing is ever edited or deleted afterwards. There is no delete and no
purge.

- `ate db status` on a current database prints `5` and exits 0.
- A missing, empty or older database exits 1 with: `run \`ate db migrate\` to create or upgrade the database at <path>`.
- A database built by a newer ATE exits 1 with: `this ATE is too old for the database at <path>; update ATE`. There is no downgrade.
- A file that is not a database exits 1 with `the file at <path> is not a SQLite database`; a database this process cannot open exits 1 with `cannot open the database at <path>: <reason>`. Neither prints a traceback.
- A database storing an older version that already has every table this build declares has its own
  refusal, not a migrate hint: `the database at <path> stores schema version 4 and already holds every
  table this ATE declares: this ATE has no migration from version 4 to 5, so it is not stamped as
  version 5, and `ate db migrate` on it changes nothing`.
  `ate db status`, `ate db migrate` and the launch dialog print that one line and exit 1; the database is
  not copied, opened or written.
- Migrations are forward only and idempotent: they create the missing tables and stamp the version, and never rewrite or drop anything.
- A migration copies the database first. `ate db migrate` and `ate setup` both call
  `src/ate/storage/backup.py`'s `backup_for_migration`, which writes
  `<db>.backup-<YYYYMMDD-HHMMSS>` (UTC) beside the file with SQLite's backup API, so the copy
  includes what is still in `-wal`. A copy is taken exactly when this build has a table to create:
  no copy when the file does not exist or holds no tables at all (a zero-byte file included), a copy
  when a table is missing even if the version is already current. The copy is written as
  `<name>.partial`, checked (`integrity_check`, the same tables, the same version), then renamed; a
  copy that makes no progress for ten seconds is refused, naming the stall. The ten seconds bound
  lack of progress, not total time, so a big database that keeps copying is never refused however
  long the copy takes. A name
  that is taken gets a `-2`, `-3` counter; nothing is overwritten. If the copy cannot be written,
  the migration is refused: `refusing to migrate: <reason>`, `nothing in the database was changed`,
  exit 1.
- Copies are never deleted. They pile up beside the database until the Administrator removes them.
- What is still backed up by hand: the config (`apt purge` deletes it), the reports directory, and
  the data directory as a whole now and then. A database that never migrates never gets a copy.
- Restore is the copy backwards: stop ATE, put the copy at `database.path`, start again. A migration
  copy is one file: the backup API folds `-wal` in and the copy is set back to the plain journal
  before it is renamed. A copy made with `cp` needs its `-wal` and `-shm` companions as well, or the
  latest writes are missing. There is no undo of one run and no partial restore.

At launch the application also closes any run with no end time as `INTERRUPTED` with result `FAIL`
and logs `Closed N interrupted run(s) at launch.`

## Media Releases

ATE does not install Media Releases; they have their own installers. Put the directory next to its
`.atedef` in one of `testDefinitions.directories`. A `.atedef` must have `format: 1`, non-empty
`mediaNumber`, `name` and `version` (valid semver 2.0.0), and `files` listing each `.tslua` or
`.lua` with its `sha256`.

A structural defect breaks that one definition and the rest of the scan continues. The messages are
literal:

```
unknown .atedef format: 2
required 'mediaNumber' is missing or empty
'1.2' is not valid semver 2.0.0
listed file is missing: lib/scpi.lua
duplicate path 'lib/scpi.lua' listed more than once
unknown file extension '.py' for lib/helper.py
depends[0] needs 'name' and 'version'
```

A checksum mismatch warns; it never stops a run. `ate definition update <file.atedef>` rewrites the
recorded checksums to match the bytes on disk, filling in any that were absent, and leaves an
already-correct file byte-for-byte unchanged.

`database.storeMismatchedFiles` decides what happens to the *bytes* of a mismatched file:

| Value | Behaviour |
| --- | --- |
| `always` | stored with the Test Run, no question |
| `never` | not stored, no question |
| `ask` | the Run screen shows a confirmation with a checkbox, ticked by default |

Headless (`QT_QPA_PLATFORM=offscreen`, CI, a container) nobody clicks: the bytes are stored only if
the setting says `always`. A programmatic run with `ask` and mismatches pending and no answer given
refuses with `database.storeMismatchedFiles is 'ask' and this schedule has checksum mismatches: start() needs store_mismatched=True or False`.

The stored bytes go into `run_file.content`, permanently. `always` is a storage decision as much as
a debugging one.

## Running a Test Run, and the PDF question

A Test Report is written at run end under `reports.directory/<UUT Type>/<UUT Type>_<serial>_<yyyymmdd-HHMMSS>_<PASS|FAIL>.html`. If that name is taken — either the HTML or the PDF of it exists — the next one gets `-2`, then `-3`.

The HTML is always written. The PDF is not, and this is the part that surprises people. ATE never
renders a PDF in its own process: it starts a hidden child, `ate _pdf <html> <pdf> --paper A4`, and
waits. Chromium's renderer sandbox cannot start under root or an administrator, so a privileged ATE
must be told to give up that isolation, and ATE never decides that by itself:

| Situation | What happens |
| --- | --- |
| ordinary user | PDF rendered with the sandbox on; `--allow-unsandboxed-pdf` does nothing and that is logged |
| privileged, no flag | a dialog per PDF: "Render PDF without sandbox?", buttons `Generate PDF without sandbox` and `Skip PDF`; `Skip PDF` is the default |
| privileged, `--allow-unsandboxed-pdf` | no dialog, rendered unsandboxed, the choice logged as a WARNING |
| privileged, headless | nobody to ask, so the PDF is skipped and the log says so — unless the flag was given |

The flag belongs to `ate`, so `ate --allow-unsandboxed-pdf _selftest --run` is the same decision as
putting it at the end. It applies to every Test Report PDF of that launch, including one regenerated
from History.

Inherited environment cannot switch the sandbox off: a `QTWEBENGINE_DISABLE_SANDBOX`, or a
`--no-sandbox` in `QTWEBENGINE_CHROMIUM_FLAGS`, is stripped from the helper's environment and logged
as a WARNING, because nobody chose it.

A PDF can be a valid PDF with no text in it. Debian containers ship no fonts; with none Chromium
writes about 1.7 KB of empty page. `ate _selftest --run` checks for text precisely so this is caught
as a packaging failure: `the PDF has no fonts: 1742 bytes with no text in it. Install fontconfig and
a font (for example fonts-dejavu-core) so QtWebEngine has something to draw with`. That is why the
`.deb` depends on `fontconfig` and `fonts-dejavu-core`.

The helper's timeout is 60 s (`PDF_TIMEOUT_MS = 60000`).

## The two logs

**Application log** — one file per launch in `log.directory`, named `ate-app-2026-11-01T09-02-31.log`,
append mode, never rotated, never deleted. Levels below `log.level` are dropped. It holds ATE's own
activity: the launch, database refusals, interrupted runs closed, buffer overflows, the PDF sandbox
decision, and any exception no code caught.

**Run log** — every line a Test Run produces is a row in the `log_line` table, not a file, with a
level, a source (`schedule`, `ate`, `device`) and an owner (the run, an Action, or a step and
Attempt). The Test Report prints only `INFO`, `WARNING` and `ERROR`. `DEBUG` and Device trace stay in
the database; export the Run log from History (CSV, JSON Lines or text) to see them.

When something inside ATE raises, PySide6 does not end the process; the installed excepthook writes
the traceback to the application log as `Unhandled exception on thread` and the status bar says:

```
Internal error: this window may have stopped updating. A Test Run in progress keeps recording. Details: <path>
```

If the log could not be written the line says `The details could not be written to the application
log` instead of naming a path. The run's record is written by the worker, not the window: trust the
database when the screen is frozen.

Device warnings worth reading:

```
TCP 127.0.0.1:5025: receive buffer overflow - dropped 4096 oldest byte(s)
```

```
device TCP 127.0.0.1:5025 left open at end of run
device TCP 127.0.0.1:5025 was still closing when the aborted run ended
```

An abort frees the worker, not the instrument. The worker stops waiting for a Device call in about
50 ms; the call itself carries on on a daemon thread and holds its transport until it finishes -
a connect or DNS lookup, a `viRead` on a non-SOCKET VISA session, a pyserial write, an
`ATE.close` on a stuck port. All the closes left behind at the end of an aborted run get three
seconds together (`_ABORT_CLOSE_SECONDS = 3.0`), one WARNING each. A Post-test Action on the same
Device waits for the call left behind, and a new run opening the same instrument while an abandoned
call is still running finds it busy. This is the boundary of an abort, not a fault: it is why the
`was still closing when the aborted run ended` line is not a bug.

## Upgrades, uninstalling, licences

Order: back up the config; install the new artifact over the old; migrate if the artifact did not;
check; then `ate definition update` on any Media Release whose files changed. The Windows installer,
`install.sh` and the `.deb` postinst run `ate setup` at the end, and that runs the migration, so
those three leave the database current — the database the config names, whichever path
`database.path` holds. Setup prints which file it migrated and why. It can only read the config if
it is allowed to: a config that exists but cannot be read is a refusal, not a fallback (setup names
the file and exits 1); only a missing config, or one with no `database.path`, falls back to
`<data>/ate.db`, and a custom database then still needs `ate db migrate`; `ate db status` reads the
configured path and catches it. A `pip`/`pipx` wheel install runs nothing, so its database stays at
the old schema and ATE refuses to start against it until you migrate.

## When the install left the Station unprepared

Every format runs `ate setup` as its last step, and each reports a failure of that step differently.

- **Windows:** the installer calls `ate.exe setup` through `Exec` with `SW_HIDE`, so the setup step
  has no window and nothing it prints reaches the screen; the message box is the only sign. Run
  `ate setup` in an elevated command prompt to see the lines it prints.
- **.deb:** `postinst` exits 1 when `ate setup` fails, and dpkg treats that as an interrupted
  install: `/opt/ate` and the `ate` link are in place, the data directory, the config and the
  database are not, and other packages will not install until this one is configured. Fix the cause
  (an unwritable `/etc/ate` or `/var/lib/ate` is the usual one), then `sudo /opt/ate/ate setup` and
  `sudo dpkg --configure -a`.
- **`install.sh`:** run by an ordinary user the link lands in the prefix's own `bin`, and setup
  prints `running unprivileged; skipping group/ownership/ACL setup` and skips the group and
  ownership pass. That is not a failure: on a Station a root run has already prepared, setup
  succeeds and `install.sh` exits 0. It exits 1 only when setup cannot do its job at all — it
  cannot create `/etc/ate` or `/var/lib/ate` — and then it says so and tells you to finish with
  `sudo ate setup`.

`ate db status` on a current database prints `5` and exits 0. `5` is the schema version, not the ATE
version: the two numbers are unrelated, so comparing them means nothing. A missing or older database
exits 1 with the refusal, which is the migration not having happened.

Each artifact clears its own program files before writing new ones, so nothing
from the previous build survives. Config and data live outside the install directory on every
platform.

Uninstall removes the program only. `apt remove` keeps config and data; `apt purge` also deletes
`/etc/ate/config.json` and the config directory if it is then empty, and never touches
`/var/lib/ate`. `./install.sh --uninstall` refuses to remove a prefix that has no ATE executable in
it. The wheel is uninstalled with `pipx uninstall ate`; whatever `ate setup` created stays.

Going backwards is not supported: a schema version is only ever increased. If you must roll back an
ATE version, roll the database back with the backup you took.

Four licence files are generated from the wheels actually installed and sit under
`_internal/licences/`; `verify_bundle` refuses to release a bundle missing any of them, and
`tests/test_linux_packages_28b.py` checks the tarball and the `.deb` still carry them. The wheel is
not checked for them.

## Checks to run

```bash
QT_QPA_PLATFORM=offscreen python -m pytest tests/test_manuals.py -q
```

That is the whole set: the config-key and command coverage, the commands run against a temporary
Station, the quoted-message checks, and the Debian container test (skipped unless
`ATE_DOCKER_TESTS=1`). To build the PDF manual:

```bash
python docs/manuals/build.py admin      # Docker, texlive/texlive:latest
```

The build fails on an overfull box wider than 5 pt or an undefined reference.

## Never

- Never run `groupadd`, `chown`, `chmod` or `icacls` on the test machine for a permission claim. Use
  `RecordingPermissions` (see `tests/test_cli_setup.py`) or the gated `debian:stable` container.
- Never trust an error message you have not grepped in `src/ate/`.
- Never assume `--config` has to come before the subcommand: `ate --config PATH db status` and
  `ate db status --config PATH` both read that config. Before packet 35 the second form read `PATH`
  as the database and died on a JSON file.
- Never assume a checksum mismatch stops a run; it warns.
- Never assume a PDF was written; check for one, and check it has text.
- Never expect a migration to be undoable without the copy it took: `ate db migrate` and `ate setup`
  copy the database first and refuse to migrate if the copy cannot be written, but they are not a
  backup policy. The config and the reports are still yours, and a database that never migrates
  never gets a copy.
- Never set `reports.paperSize` to anything but `A4` or `Letter`; the config refuses it at startup.
- Never point two Stations at the same database file; nothing prevents a cloned `station.uuid`.
- Never rely on `ATE_DEFAULT_CONFIG` or `ATE_DATA_DIR` on a real Station.

## What is proven, and what is not

Proven by the suite: config parsing and every refusal it produces; database status, migration,
migration refusal and the copy a migration takes before it runs; `ate setup`'s directories, config
and database; `ate definition update`; the PDF
sandbox decision and its messages; the abort path that leaves a Device closing; the
full test suite on Windows and Linux with Python 3.13 and 3.14, headless; the Windows installer
compiled, silently installed, upgraded and uninstalled on a CI runner; the Linux artifacts smoke
tested in `debian:stable`; `verify_bundle` refusing a bundle missing the licence files.

Not proven: any real desktop session with a display a human watched; what a given Windows machine
makes of an unsigned installer (there is no certificate); any real GPIB, USBTMC or RS-232 instrument
— every Device test uses a loopback peer, a `socket://` URL, or pyvisa-py's backends, and NI-VISA is
not shipped; the cause of the Windows CI stall, which is a hypothesis about the runner's
disk; whether the wheel carries the licence notices.

One established fact worth repeating: ATE takes no exclusive lock on a serial port on Linux. The
suite tried `serial.Serial(exclusive=True)` against a port ATE was holding and the open succeeded, so
sharing is the behaviour.

## Where the design specs and the code disagree

The specs are the files under `.scratch/ate-v1-wayfinding/assets/`, named in each line below.
Two of the disagreements this list used to hold are closed: `test-run-record.md`'s timestamped
backup before an upgrade is now taken by the code, and `app-config-format.md`'s `reports.logo` and
`reports.paperSize` match what the validator accepts.

- `ui.theme` is stored and never read by the shipped code.
- `test-run-record.md`'s wording for a launch refusal differs from `DbLaunchResult.message`, which names the
  database path and adds `--config <path>` when a non-default config is in use.
- `packaging.md` says unsigned installers "may trigger SmartScreen warnings". Nothing in this
  repository establishes that; the installer is unsigned, and it has only ever been installed
  headlessly on a CI runner.
- The manual describes no appearance at all. The appearance work is a separate packet of the project
  (`.scratch/ate-v1/issues/33-appearance-review.md`), so anything about how a screen looks is
  unverified here.
