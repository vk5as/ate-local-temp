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
# Re-running is an upgrade. The bundle's own contents (ate and _internal) are cleared before the
# new copy is written, so no file of the previous version survives; anything else in the prefix,
# such as the bin/ link, is kept. Nothing under /etc/ate or /var/lib/ate is ever touched, so the
# Station keeps its config, database, reports and logs (assets/packaging.md: "Upgrades install in
# place and keep the config and all data"). Uninstalling removes only the program, the same as
# `apt remove`.
#
# Run by an ordinary user the link goes to <prefix>/bin/ate and "ate setup" cannot create the ate
# group or set ownership on /etc/ate and /var/lib/ate: the program is still installed, and the
# Station is finished with `sudo ate setup`.
#
# Test seams: --prefix and --bin aim the install at a temporary directory so the script can be
# exercised without root. Nothing else here is test-specific.
set -eu

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
        --prefix|--bin)
            flag="$1"
            if [ $# -lt 2 ] || [ -z "$2" ]; then
                echo "install.sh: $flag needs a value" >&2
                usage
                exit 2
            fi
            if [ "$flag" = "--prefix" ]; then PREFIX="$2"; else BIN="$2"; fi
            shift 2 ;;
        --uninstall) ACTION=uninstall; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install.sh: unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done

if [ -z "$PREFIX" ] || [ "$PREFIX" = "/" ]; then
    echo "install.sh: refusing to use the prefix '$PREFIX'" >&2
    usage
    exit 2
fi

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
    # A prefix that has no ate executable is not ours to delete, so --uninstall can never wipe a
    # directory the operator pointed it at by mistake.
    if [ ! -f "$PREFIX/ate" ]; then
        echo "install.sh: nothing to uninstall in $PREFIX: it has no ATE bundle" >&2
        exit 1
    fi
    rm -f "$BIN"
    rm -rf "$PREFIX/ate" "$PREFIX/_internal"
    # The prefix itself only goes if it is now empty; anything the operator put in it stays.
    rmdir "$PREFIX" 2>/dev/null || true
    echo "removed the ATE bundle in $PREFIX and the link $BIN; /etc/ate and /var/lib/ate are left alone"
    exit 0
fi

if [ ! -f "$SRC/ate" ]; then
    echo "install.sh: no ./ate bundle beside this script - extract the tarball and run it there" >&2
    exit 1
fi

# Replace, never merge: cp -a into an existing prefix leaves the previous version's files behind,
# so the bundle's own contents are cleared first. Only what the bundle is (ate and _internal) is
# removed - the prefix can hold the bin/ link, and deleting the prefix blindly would take it too.
# cp -a keeps the modes, so the executable stays executable and _internal stays intact.
mkdir -p "$PREFIX"
rm -rf "$PREFIX/ate" "$PREFIX/_internal"
cp -a "$SRC/." "$PREFIX/"
chmod +x "$PREFIX/ate"
mkdir -p "$(dirname "$BIN")"
ln -sf "$PREFIX/ate" "$BIN"
echo "ATE installed in $PREFIX, command at $BIN"

if [ "$(id -u)" -ne 0 ]; then
    echo "not running as root: setup cannot create the ate group or set the ownership of /etc/ate and /var/lib/ate"
fi

echo "preparing the Station (/etc/ate/config.json, /var/lib/ate):"
if "$PREFIX/ate" setup; then
    echo "Station ready: $BIN --version is the install check"
else
    echo "install.sh: ATE is installed in $PREFIX, but the Station is not set up." >&2
    echo "  finish it as root: sudo ate setup   (or sudo $PREFIX/ate setup)" >&2
    exit 1
fi
