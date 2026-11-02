# 36: Build the manuals in the release flow

**Parent:** none. User decision, 2026-11-02: all manuals go in a build flow, and releases attach them as artifacts.

**What to build:** `release.yml` builds all three PDF manuals and attaches them, with the three Markdown manuals, as a release artifact. Not added to the installer, `.deb`, tarball or wheel: the user asked for artifacts.

**Blocked by:** 35 (it changes the manuals and `build.py`)

**Status:** queued

- [ ] New job in `.github/workflows/release.yml`, running on a tag and on `workflow_dispatch`, like the others: builds with `python docs/manuals/build.py` on `ubuntu-latest`. The whole run fails if any manual fails to build, including `build.py`'s gates for undefined references and overfull boxes.
- [ ] One artifact, `ate-manuals`, holding `test-writer.pdf`, `admin.pdf`, `developer.pdf` and `llm/test-writer.md`, `llm/admin.md`, `llm/developer.md`, with the same retention as the other release artifacts. The upload fails if a file is missing.
- [ ] Each PDF's title page shows the version being released, the same string `ate --version` gives on that tag (see `src/ate/_version.py` and how the release jobs derive it, including `v0.1.0-rc2` → `0.1.0rc2`). The job checks the title page text with `pdftotext`, and fails on a mismatch. The checkout has enough history and tags for that version.
- [ ] Pin the TeX image by tag or digest instead of `latest`, so a release can be rebuilt, in both `build.py` and the workflow. The image is about 9 GB: check the runner has the disk, freeing space if it doesn't, and say how long the job takes. Use a smaller image if it builds the manuals identically.
- [ ] Tests in the style of `tests/test_release_ci_28c.py`: the job exists, runs on the same triggers, uploads all six files, and fails on a build failure. Each is shown to fail when the workflow is broken. The file names come from `MANUALS` in `tests/test_manuals.py` or `build.py`, so a new manual can't be left out silently.
- [ ] `docs/building-from-source.md`, the admin and developer manuals in both forms say where a release's manuals are found.
- [ ] The orchestrator proves it with a real `release.yml` run: all jobs green, the artifact downloaded, the version on each title page read.

Definitions live in `.scratch/ate-v1-wayfinding/assets/packaging.md`. Keep to this packet's checkboxes.
