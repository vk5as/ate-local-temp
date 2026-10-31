# 28b: Linux tarball and `.deb`

**Parent:** 28 (Linux packages, wheel and source build instructions) — `28-linux-packages.md`

**What to build:** Best-effort Linux packages.

**Blocked by:** 27a (PyInstaller build)

**Status:** resolved

- [x] A `.tar.gz` of the PyInstaller build with `install.sh` calling `ate setup`
  `packaging/linux/build_linux.py` writes `ate-<version>-linux-x86_64.tar.gz`: the onedir bundle
  as `ate/` plus `install.sh` beside it. `install.sh` copies the bundle to `--prefix` (default
  `/opt/ate`), links `ate` (`/usr/bin/ate` as root, `<prefix>/bin/ate` otherwise), and runs
  `ate setup`. Re-running is an upgrade: config and data are never touched, and `--uninstall`
  removes the program only.
- [x] A self-contained `.deb` for Debian stable (`/opt/ate`, `/usr/bin/ate` link, `postinst` calls
  `ate setup`); `apt remove` keeps config and data, `apt purge` removes config but never the
  database, reports or logs
  Built with `dpkg-deb --root-owner-group --build`. `Depends` is only what the bundle links
  against and cannot carry: `libc6, libgl1, libegl1, libdrm2, libxcb1, libxcb-cursor0,
  libxcb-dri3-0, libwayland-client0, libwayland-cursor0, libwayland-egl1` (found by `ldd` over a
  built `_internal`; `libxcb-cursor0` is the cursor extension Qt's xcb platform plugin needs to
  open a display, and the gated test asserts `Depends` covers every soname `ldd` cannot resolve).
  `/etc/ate/config.json` is not a conffile — setup writes it — so `postrm` removes
  it on purge. `postinst`/`postrm` have no test seam at all: the tests run a copy of each script
  with `/opt/ate`, `/etc/ate` and `/var/lib/ate` rewritten to a temporary root.

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.

**Built here:** CPython 3.13 (the only Python on this machine) — `ATE_BUILD_TESTS=1 pytest -m build
tests/test_linux_packages_28b.py` ran for real: it built the bundle with PyInstaller, packaged it
both ways (`ate-0.0.0-dev-linux-x86_64.tar.gz` 196,933,904 bytes, `ate_0.0.0~dev_amd64.deb`
145,504,876 bytes), extracted the `.deb` with `dpkg-deb -x`, ran the extracted `/opt/ate/ate
--version` and `_selftest`, and checked every soname `ldd` reports as not found over the bundle
against `Depends`. Nothing has installed the package with dpkg on this machine: no `apt install`
and no Debian-stable container run. The 3.14 bundle and the Debian-stable container smoke test are
28c's job.
