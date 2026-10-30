#!/bin/sh
# ATE installer for the Linux tarball (packet 28b). It ships at the root of
# ate-<version>-linux-x86_64.tar.gz, next to the extracted bundle directory `ate/`.
#
#   ./install.sh                          install to /opt/ate, link /usr/bin/ate, run "ate setup"
#   ./install.sh --prefix /opt/ate        where the bundle goes (PREFIX is honoured too)
#   ./install.sh --bin /usr/local/bin/ate where the "ate" command is linked; the default is
#                                         /usr/bin/ate as root, <prefix>/bin/ate for anyone else
#   ./install.sh --uninstall              remove the installed bundle and its link
#
# Re-running is an upgrade: the bundle in the prefix is replaced and setup is run again.
# Nothing under /etc/ate or /var/lib/ate is ever touched, so the Station keeps its config,
# database, reports and logs (assets/packaging.md: "Upgrades install in place and keep the
# config and all data"). Uninstalling removes only the program, the same as `apt remove`.
#
# Test seams: --prefix and --bin aim the install at a temporary directory so the script can be
# exercised without root. Nothing else here is test-specific.
set -e

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SRC="$HERE/ate"
PREFIX=${PREFIX:-/opt/ate}
BIN=""
ACTION=install

usage() {
    echo "usage: install.sh [--prefix DIR] [--bin PATH] [--uninstall]"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="$2"; shift 2 ;;
        --bin) BIN="$2"; shift 2 ;;
        --uninstall) ACTION=uninstall; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install.sh: unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done

if [ -z "$BIN" ]; then
    # A Station's command belongs in /usr/bin; an unprivileged install cannot write there, so it
    # lands in the prefix's own bin instead and the operator puts it on PATH.
    if [ "$(id -u)" -eq 0 ]; then
        BIN=/usr/bin/ate
    else
        BIN="$PREFIX/bin/ate"
    fi
fi

if [ "$ACTION" = uninstall ]; then
    rm -f "$BIN"
    rm -rf "$PREFIX"
    echo "removed $PREFIX and the link $BIN; /etc/ate and /var/lib/ate are left alone"
    exit 0
fi

if [ ! -f "$SRC/ate" ]; then
    echo "install.sh: no ./ate bundle beside this script - extract the tarball and run it there" >&2
    exit 1
fi

# cp -a keeps the modes, so the executable stays executable and _internal stays intact.
mkdir -p "$PREFIX"
cp -a "$SRC/." "$PREFIX/"
chmod +x "$PREFIX/ate"
mkdir -p "$(dirname "$BIN")"
ln -sf "$PREFIX/ate" "$BIN"
echo "ATE installed in $PREFIX, command at $BIN"

echo "preparing the Station (/etc/ate/config.json, /var/lib/ate):"
if "$PREFIX/ate" setup; then
    echo "Station ready: $BIN --version is the install check"
else
    echo "install.sh: 'ate setup' failed; run it as root: $PREFIX/ate setup" >&2
    exit 1
fi
