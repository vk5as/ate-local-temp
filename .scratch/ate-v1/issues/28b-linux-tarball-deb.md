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
  against and cannot carry: `libc6, libgl1, libegl1, libdrm2, libxcb1, libxcb-dri3-0,
  libwayland-client0, libwayland-cursor0, libwayland-egl1` (found by `ldd` over a built
  `_internal`). `/etc/ate/config.json` is not a conffile — setup writes it — so `postrm` removes
  it on purge. `postinst`/`postrm` honour `ATE_DEB_ROOT` so the tests can run them against a
  temporary root.

Definitions live in `.scratch/ate-v1-wayfinding/assets/`; the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes; neighbouring packets cover the rest of the parent ticket.

**Built here:** CPython 3.13 (the only Python on this machine) — `ATE_BUILD_TESTS=1 pytest -m build -q`
packaged the real bundle, extracted the `.deb` with `dpkg-deb -x` and ran the extracted
`/opt/ate/ate --version` and `_selftest`. The 3.14 bundle and the Debian-stable container smoke
test are 28c's job.
