# 28c: Linux release CI

**Parent:** 28 (Linux packages, wheel and source build instructions) — `28-linux-packages.md`

**What to build:** Tagged releases build and smoke-test the Linux deliverables.

**Blocked by:** 28a (Wheel and source build instructions), 28b (Linux tarball and `.deb`), 27c (Windows release CI)

**Status:** resolved

- [ ] Tagged releases build all three and smoke-test each in a Debian stable container — written; never run; becomes done on the first green tag

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.

## What was written, and what has never run

Written on Linux machines, and then run: the artifacts were built and installed in real `debian:stable`
containers by `packaging/linux/docker_smoke.py`, which is the same sequence the release job runs. What
has never run is the workflow itself.

- `.github/workflows/release.yml` — **never run.** `linux-build` on `ubuntu-24.04` builds the
  bundle, the tarball, the `.deb` and the wheel and uploads them as `ate-linux-artifacts`;
  `linux-smoke` runs `deb`, `tar` and `wheel` in three separate `debian:stable` containers, each
  `needs: linux-build`. No tag of this repository has been cut, so the workflow has never started.
  `ubuntu-24.04` is pinned, not `ubuntu-latest`: the bundle is linked against the glibc of the
  machine that built it, and glibc is only forward compatible — a bundle built against Ubuntu 24.04's
  2.39 runs on Debian stable, and one built against a future Ubuntu might not. The build records its
  own glibc in `dist/glibc-build.txt` and every smoke run refuses an older container.
  No `actionlint` is installed here and none was installed; the workflow is checked by
  `tests/test_release_ci_28c.py` parsing it.
- `packaging/linux/smoke_linux.py` — **run, in Debian containers.** Its `apt-get install ./ate_*.deb`,
  `apt remove`, `apt purge`, `install.sh` and `pipx install` steps executed in fresh `debian:stable`
  containers: 8 checks passed for the `.deb` and the tarball, 7 for the wheel (the wheel has no
  glibc pin to check, because nothing in it came from the build machine). Each run rendered a report
  with the installed program and read it back with `pdftotext`. `tests/test_release_ci_28c.py` also
  drives the same sequence against a fake container, with `apt` and `dpkg` modelled and `install.sh`
  run for real through its `--prefix`/`--bin` seam.
- `packaging/linux/docker_smoke.py` — the harness that made that possible: it tags a copy of the
  checkout `v1.2.3`, builds the bundle with the container's own `/usr/bin/python3`, packages all three
  artifacts, and runs the smoke script in three fresh containers. `ATE_DOCKER_TESTS=1 pytest -m build`
  runs it as a test.
- `ate --version`, `ate db status`, `ate _selftest --run` and `ate _selftest --scan <dir>` — these
  run here, on Linux, from the source; the fake container calls the real CLI, so a broken Media
  Release and a database at the wrong schema really do fail the scan.

The first time any Linux artifact is installed by `apt` or `pipx` on GitHub Actions is the first tag's
release job; the containers above ran the same commands locally.
