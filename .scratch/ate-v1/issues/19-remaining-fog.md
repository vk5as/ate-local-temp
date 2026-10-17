# Settle the remaining Not yet specified items

Type: grilling
Status: resolved
Blocked by: none

## Question

Settle the map's last Not yet specified items: what the History screen does, how a Device failing mid-run is handled, the Device receive buffer, packaging and installers, and the custom Lua build for lupa.

## Answer

- **History screen** (`assets/history-screen.md`): a flat, sortable, filterable, paged table of runs, newest first; a summary panel with Open report, Regenerate, Export log, Show all runs of this UUT and New run for this UUT; no delete. Usable during a run unless `ui.allowHistoryDuringRun` is `false`.
- **Device failing mid-run** (`assets/ate-table-reference.md`): a background reader error is logged at once and marks the Device failed; the next `read`/`write` fails through `onError` after buffered data is read. No automatic reconnect; a schedule can `close` and `open` again. `write` honours `timeout`, reporting the bytes actually sent.
- **Receive buffer**: capped by `devices.receiveBufferBytes` (16 MiB); overflow drops the oldest data, warns once and flags the next read with `overflow = true`. New `ATE.flush(dev)` empties it (a no-op on `VISA`).
- **Packaging** (`assets/packaging.md`): a Windows Inno Setup installer around a PyInstaller build (supported); a Linux `.tar.gz`, a self-contained `.deb` for Debian stable and a wheel (best effort); source build instructions. Frozen builds bundle Python 3.14. `ate setup` prepares every Station (directories, permissions, default config, `ate db migrate`). Uninstalling never deletes test records. pyvisa-py is bundled. Signing is optional (no certificate yet). CI builds and smoke-tests every artifact on tagged releases. ATE versions are semver from the git tag.
- **Custom Lua build**: out of scope.

## Comments

### Round 1, 2026-10-18

- **History, finding runs**: one flat table, newest first, sortable by start time, UUT Type, serial, result, End Reason, Test Schedule, Project and Operator. Filters for UUT Type, serial (substring), result, End Reason, date range and Project. Rows load page by page.
- **History, actions on a run**: a summary panel (run fields, `Result` counts, checksum mismatches) with Open report (newest file, system viewer), Regenerate (HTML and/or PDF, Include Run log toggle), Export log, Show all runs of this UUT, and New run for this UUT (prefills UUT Type, serial, Project and Test Schedule on the Run screen). No delete.
- **History during a run**: allowed by default, with a config option to disallow it. The running run shows as "running" with report and export disabled.
- **Background reader failure**: logged at once as a `WARNING` (source `device`); the Device is marked failed, and its next `read`/`write` returns the failure through `onError`. Already-buffered data can still be read first.
- **No automatic reconnect**: a schedule may `close` and `open` again with the same params for a new device object. No special Operator dialog.
- **Write timeout**: `write` honours the Device `timeout` with a per-call override; an incomplete write fails with `err = "timeout"` and `count` = bytes sent.
- **Receive buffer**: capped by `devices.receiveBufferBytes` (default 16 MiB; for `UDP`, total queued datagram bytes). Overflow drops the oldest data, logs one `WARNING` per overflow, and the next `read` result has `overflow = true`.
- **`ATE.flush(dev)`**: empties the buffer and returns `{ok = true, count = n}`; on `VISA` a no-op with `count = 0` (no instrument clear).
- **Windows**: PyInstaller one-directory build in an Inno Setup installer; per-machine, admin, `ate` on `PATH`, in-place upgrades keep config and data. Never contains Media Releases. A wheel is built as well.
- **Linux**: all three: the PyInstaller build as `.tar.gz` with `install.sh`, a `.deb`, and the wheel (pipx). Target distro: Debian stable.
- **Permissions**: the installer gives ordinary users modify rights on the data directories (database and WAL files, `reports`, `logs`); `config.json` stays admin-only. Linux uses an `ate` group.
- **Installer prepares the Station**: runs `ate db migrate` (create or upgrade, after backup) and creates a default `config.json` if missing; never overwrites an existing config.
- **pyvisa-py** is bundled; NI-VISA is preferred when installed, and GPIB still needs it.
- **Code signing**: an optional build step that signs when a certificate is configured. No certificate yet, so builds are unsigned for now.
- **Custom Lua build**: moves to Out of scope, keeping the pointer to the research branch.

### Round 2, 2026-10-18

- **`ui.allowHistoryDuringRun`** (default `true`); when `false`, History is disabled during a run with "A Test Run is in progress".
- **`ate setup`** (admin/root) prepares a Station: data directories, permissions, default config if missing, `ate db migrate`, and on Linux the `ate` group (adding Operators stays manual). The Inno Setup installer, `install.sh` and the `.deb` `postinst` call it; wheel users run it after `pipx install`.
- **`.deb`**: self-contained (PyInstaller build in `/opt/ate`, `/usr/bin/ate` link), `.deb` only, no `.rpm`. Best effort. Source build instructions are provided as well.
- **Frozen builds** bundle Python 3.14; the wheel supports 3.13 and 3.14 (Debian 13's system Python is 3.13).
- **Uninstall** removes only the program; `apt purge` removes the config but never the database or reports.
- **CI** builds all four artifacts on every tagged release and smoke-tests each in a clean Windows runner or Debian stable container (`ate setup`, then a headless launch that scans an example Media Release and checks the database version).
- **ATE version**: semver from the git tag at build time; schema changes are listed in release notes and don't force a major bump.
