# 05: Test Definition catalog and `ate definition update`

**What to build:** ATE scans the configured directories for Test Definitions, validates them fully, and shows every visible Test Schedule in one flat list, with broken definitions and schedules shown with their errors. Test authors can recompute checksums with `ate definition update`.

**Blocked by:** 02 (Tracer bullet: run a one-step Test Schedule and record it), 03 (Minimal staged Run screen)

**Status:** ready-for-agent

- [ ] Recursive scan at launch and on a Rescan action
- [ ] Full `.atedef` validation per `test-definition-format`: required fields, `format`, semver `version`, extensions, `hidden` only on `.tslua`, `module` only on `.lua` and required for absolute or `..` paths, duplicate paths and module names, reserved `ate.` names, duplicate Media Numbers (both broken), missing files
- [ ] Relative and absolute paths; module names derived from relative paths or set with `module`
- [ ] Each visible Test Schedule is loaded in its own fresh runtime at scan time to read its `ScheduleParams` name and description; one that raises is listed as broken with its error while the rest of its Media Release stays usable
- [ ] The picker shows broken Test Definitions and schedules with their errors and they can't be started
- [ ] `ate definition update <file.atedef>` recomputes every `sha256` and fills in missing ones in place
- [ ] Seam-1 tests cover each broken case; seam-2 tests cover `ate definition update`

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`.
