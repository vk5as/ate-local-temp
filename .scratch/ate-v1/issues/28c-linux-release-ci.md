# 28c: Linux release CI

**Parent:** 28 (Linux packages, wheel and source build instructions) — `28-linux-packages.md`

**What to build:** Tagged releases build and smoke-test the Linux deliverables.

**Blocked by:** 28a (Wheel and source build instructions), 28b (Linux tarball and `.deb`), 27c (Windows release CI)

**Status:** resolved

- [ ] Tagged releases build all three and smoke-test each in a Debian stable container — written; never run; becomes done on the first green tag

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.

## What was written, and what has never run

Written on Linux machines with no Debian container and no way to run a workflow:

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
- `packaging/linux/smoke_linux.py` — **never run.** Its `apt-get install ./ate_*.deb`,
  `apt remove`, `apt purge`, `install.sh` and `pipx install` steps need a Debian container. The
  tests drive the same sequence against a fake container in a temporary directory, with `apt` and
  `dpkg` modelled and `install.sh` run for real through its `--prefix`/`--bin` seam.
- `ate --version`, `ate db status`, `ate _selftest --run` and `ate _selftest --scan <dir>` — these
  run here, on Linux, from the source; the fake container calls the real CLI, so a broken Media
  Release and a database at the wrong schema really do fail the scan.

The first time any Linux artifact is installed by `apt` or `pipx` is the first tag's release job.
