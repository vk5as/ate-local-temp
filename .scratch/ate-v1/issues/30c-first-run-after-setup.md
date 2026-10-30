# 30c: First run after `ate setup`

**Parent:** none. This is a gap found in the 27a review on 2026-10-30, reproduced from source and in the frozen app, and also present on integration.

**What to build:** An Operator who runs `ate setup` and then `ate` (or `ate db status`) gets a working Station. Today three bugs break that path:
1. **`<data>` is never resolved.** `ate setup` writes the config defaults from `app-config-format.md` literally: `<data>/definitions`, `<data>/ate.db`, `<data>/reports`, `<data>/logs`. `src/ate/config.py` (~60-61) says `Config.from_resolved` resolves `<data>`, but nothing does (~310-340), so the GUI creates a literal `config/<data>/logs`.
2. **`ate db status` / `ate db migrate` ignore `--config`.** `src/ate/cli.py` (~167) takes the database path from the arguments and defaults to `./ate.db` in the working directory. `ate --config X db migrate --yes`, run from `/tmp`, created `/tmp/ate.db`.
3. **A launch refusal aborts instead of showing its message.** `src/ate/app.py` (~321, ~334, ~342) does `QApplication.instance() or QApplication([])` as a bare statement, so the new QApplication is garbage-collected at once. A missing or older database, or an invalid config, then dies with `QWidget: Must construct a QApplication before a QWidget` (exit 134) instead of showing the dialog and exiting 1.

**Blocked by:** 30a (GUI launch), 26b (`ate setup`: permissions)

**Status:** resolved

- [x] `<data>` (and any other placeholder the config format defines) is resolved to the data directory the spec defines for the platform, or the one `ate setup --root` chose. `ate setup` writes either resolved paths or placeholders that the reader resolves; the spec decides which. Paths in a config written by `ate setup` work when `ate` starts.
- [x] `ate db status` and `ate db migrate` use the configured database when no path is given (`--config`, or the default config location); an explicit path still overrides it.
- [x] Every launch-refusal path shows its dialog and exits 1, with no abort. The QApplication is kept alive for the whole launch.
- [x] Tests: `ate setup --root <tmp>`, then `ate --config <tmp>/config/config.json db status` prints the schema version; then a GUI launch (`exec_=False`) succeeds with no literal `<data>` directory created. Each refusal (missing, older or newer database; invalid config) shows its message and exits 1, run in a subprocess so an abort would fail the test.

Definitions live in `.scratch/ate-v1-wayfinding/assets/` (`app-config-format.md`, `packaging.md`); the spec is `.scratch/ate-v1/spec.md` (CLI ~220, Config ~190). Keep to this packet's checkboxes.
- [x] (added at the 27a merge) `tests/test_report_steps_21b.py::test_autoescape_does_not_change_the_cover_bytes` is time-dependent since 29e added the "Report generated <local time>" footer: make the comparison exclude or freeze that timestamp so it is deterministic.

## Review rework (30c review, 2026-10-30)

- [x] `_platform_default()` returns a `Path`, so bare `ate` and `ate db status`/`migrate` with no
      `--config` no longer raise `AttributeError`. A missing default config prints
      `ate db: config file not found at <path>` / `run 'ate setup' or pass --config PATH first`,
      exits 1, and creates nothing. `ATE_DEFAULT_CONFIG` is the subprocess seam.
- [x] "Create default config and launch" against an unwritable data directory refuses with the
      path and the fix instead of a `PermissionError` traceback; `write_config` stages its JSON,
      so a failed write leaves no half-written file. A config whose write succeeded is kept — the
      format refuses to start only when the write itself fails. `ATE_DATA_DIR` is the data-dir seam.
- [x] The migrate hint names `--config <path>` when a non-default config is in use, and every
      refusal names the database path.
- [x] `ate setup` creates `<data>/definitions`, so a fresh Station does not warn on every scan.
- [x] `ATE_AUTO_DISMISS_DIALOG` treats `""`/`"0"`/`"false"`/`"no"`/`"off"` as unset; the docstring
      now says the prompts answer their negative branch; documented in `docs/building-from-source.md`.
- [x] `tests/test_gui_launch_30a.py` again asserts no stray `./ate.db` and the exact
      "created or upgraded database at <path>" line.
- [x] The 21b fake clock carries a real `timezone`, so removing the `generated_at` seam is caught
      by the second-crossing assertion rather than an `AttributeError`.
