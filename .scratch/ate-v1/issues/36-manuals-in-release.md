# 36: Manuals in the release flow, on GitHub Releases and in the installed apps

**Parent:** none. User decisions, 2026-11-02: all manuals, PDF and Markdown, are attached to GitHub Releases, built as part of the workflow, and the PDFs are built into the installed apps.

**What to build:** `release.yml` builds the three PDF manuals first, feeds them into every package, and on a tag publishes a GitHub Release carrying all six manuals.

**Blocked by:** 35 (it changes the manuals and `build.py`)

**Status:** queued

## Build

- [ ] New first job in `.github/workflows/release.yml`, `manuals`, on `ubuntu-latest`: builds with `python docs/manuals/build.py`. The package jobs `need` it, so the whole run fails if any manual fails, including `build.py`'s gates for undefined references and overfull boxes.
- [ ] Artifact `ate-manuals` holds `test-writer.pdf`, `admin.pdf`, `developer.pdf`, `test-writer.md`, `admin.md`, `developer.md`, on tags and on `workflow_dispatch`, with the same retention as the other artifacts. The upload fails if a file is missing.
- [ ] Each PDF's title page shows the version being released, the same string `ate --version` gives (`src/ate/_version.py`; `v0.1.0-rc2` gives `0.1.0rc2`). The job checks it with `pdftotext` and fails on a mismatch. The checkout has enough history and tags for that version.
- [ ] Pin the TeX image by tag or digest instead of `latest`, in `build.py` and the workflow, so a release can be rebuilt. It is about 9 GB: check the runner's disk, free space if needed, and report the job's time. Use a smaller image only if it builds the manuals identically.

## In the installed apps

- [ ] All three PDFs ship in every package: the Windows installer, `.deb`, tarball and wheel. The Windows job needs them from the `manuals` job, since TeX is not built on Windows.
- [ ] One staged location the build fills, ignored by git, for example `src/ate/manuals/*.pdf`, which PyInstaller and the wheel both collect. A release build refuses to package without them: `verify_bundle()` fails on a missing or empty PDF, as it does for the licence notices. A local build from source without Docker or PDFs still works, says plainly that it has no manuals, and is not a release build.
- [ ] Installed location is documented and stable. `.deb` also makes them findable at `/usr/share/doc/ate/`, through a symlink or copy. Windows gets a Start Menu group entry per manual, if the installer already has a group, and uninstall removes them. No PDF viewer is shipped or required.
- [ ] `ate --help` says where the manuals are, from the running install's real path, whether frozen, wheel or source. No new menu or dialog: the interface waits for packet 33's decisions.
- [ ] The smoke tests for the installer, `.deb`, tarball and wheel each check the installed PDFs exist, are PDFs by their first bytes, and carry the right version. Bundle size change is reported.

## GitHub Release

- [ ] On a `v*` tag only, after every package job passes, a final job creates a GitHub Release for that tag and uploads all six manuals as release assets. `workflow_dispatch` never publishes. Needs `permissions: contents: write` on that job only. A tag with a pre-release suffix such as `-rc2` is marked pre-release. Re-running the job replaces the assets and does not fail on an existing Release.
- [ ] The workflow keeps working if the repo is private. Check what a private repo needs, and say what isn't provable until a tag is pushed.
- [ ] Tests in the style of `tests/test_release_ci_28c.py`: job order and `needs`, triggers, the publish job gated on tags, all six files, the permission scope. Each is shown to fail when the workflow is broken. File names come from one list in `build.py`, so a new manual can't be left out silently.

## Docs and proof

- [ ] `docs/building-from-source.md`, and the admin and developer manuals in both forms, say where a release's manuals are and where an install keeps them.
- [ ] The orchestrator proves the build with a real `workflow_dispatch` run: all jobs green, `ate-manuals` downloaded, versions read, PDFs seen by the smoke tests. Publishing is proven only by pushing a tag, which the user decides.

Definitions live in `.scratch/ate-v1-wayfinding/assets/packaging.md`. Keep to this packet's checkboxes.
