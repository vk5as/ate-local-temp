#!/usr/bin/env python3
"""Compile ATE's Windows installer with Inno Setup (packet 27b).

    python packaging/windows/build_installer.py --dist dist                 # dist/ate already built
    python packaging/windows/build_installer.py --build                     # build dist/ate first (27a)
    python packaging/windows/build_installer.py --dry-run                   # print the iscc command, build nothing

`packaging/windows/ate.iss` is the installer script; this wrapper is the only way to
compile it. It finds `iscc` ($ISCC, then PATH, then the standard Inno Setup 6 location),
takes the version from the built bundle (`dist/ate/ate.exe --version`), and stamps it into
the script as both the textual `AppVersion` and the four-number Windows version.

**Never compiled or run.** These machines are Linux: there is no Inno Setup and no Wine,
and none is installed. `--dry-run` is the only thing here that has actually executed, and
the `.iss` it prints has never been through `iscc`. The first real compile is 27c's
Windows runner.

Code signing is optional. When `ATE_SIGN_CERT` and `ATE_SIGN_PASSWORD` are both set the
script is compiled with signing enabled — the installer itself, its uninstaller and
`ate.exe` are signed — otherwise it is built unsigned and nothing complains. The
password is read from the environment and never printed: it does sit on `iscc`'s command
line (visible in any process listing on the build machine), so every line this wrapper
prints, including a failed compile's stdout and stderr, is redacted first.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
ISS = HERE / "ate.iss"
FALLBACK_VERSION = "0.0.0-dev"

# Where Inno Setup 6 puts its command-line compiler on a stock Windows install.
ISCC_LOCATIONS = (
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
)


def _is_windows() -> bool:
    """Whether this machine can run `iscc` at all."""
    return os.name == "nt"


def _find_iscc() -> str | None:
    """The Inno Setup compiler: $ISCC, then PATH, then the standard install directories."""
    from_env = os.environ.get("ISCC")
    if from_env and Path(from_env).is_file():
        return from_env
    on_path = shutil.which("iscc") or shutil.which("iscc.exe")
    if on_path:
        return on_path
    for location in ISCC_LOCATIONS:
        if Path(location).is_file():
            return location
    return None


def _windows_version(version: str) -> str:
    """ATE's semver as Inno's VersionInfoVersion: up to four dot-separated numbers.

    Windows file versions have no room for a prerelease tag, so the suffix is dropped and
    the number is padded out: `0.0.0-dev` -> `0.0.0.0`, `1.2.3` -> `1.2.3.0`,
    `1.2.3-rc1` -> `1.2.3.0`. Whatever is left over (`1.2.3.4.5`) is truncated to four.
    """
    core = re.match(r"\d+(?:\.\d+)*", (version or "").strip())
    if not core:
        return "0.0.0.0"
    parts = core.group(0).split(".")[:4]
    return ".".join(parts + ["0"] * (4 - len(parts)))


def _bundle_version(exe: Path) -> str:
    """Ask the built bundle what version it is, the way 28b does."""
    if not exe.is_file():
        raise SystemExit(
            f"{exe} is not a built one-directory bundle yet: run packaging/build_pyinstaller.py first "
            "(or pass --version, or --dry-run to see the command without building)"
        )
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([str(exe), "--version"], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise SystemExit(f"{exe} --version failed with exit {proc.returncode}: {(proc.stdout + proc.stderr)[-500:]}")
    version = proc.stdout.strip()
    if not version:
        raise SystemExit(f"{exe} --version printed nothing: an installer nobody can identify is not worth building")
    return version


def _sign_tool(cert: str, password: str) -> str:
    """The signtool command Inno runs for every file it is asked to sign.

    `$f` is the file to sign and `$q` is a quote: Inno substitutes them before running
    the command, so the certificate and the password sit in the command itself.

    That means the password is on `iscc`'s command line and any process listing on the
    build machine can see it. Nothing here ever logs the real command: everything printed
    goes through _redact first.
    """
    return f'signtool.exe sign /fd SHA256 /f $q{cert}$q /p $q{password}$q /tr http://timestamp.digicert.com /td SHA256 $f'


def _signing() -> tuple[str, str] | None:
    """(certificate, password) when a certificate is configured, otherwise None.

    Half a configuration is a mistake worth stopping on: silently building unsigned when
    someone meant to sign is how an unsigned artifact ends up in a release.
    """
    cert = os.environ.get("ATE_SIGN_CERT", "").strip()
    password = os.environ.get("ATE_SIGN_PASSWORD", "")
    if cert and password:
        return cert, password
    if cert or password:
        raise SystemExit("signing needs both ATE_SIGN_CERT and ATE_SIGN_PASSWORD; only one of them is set")
    return None


def _iscc_command(iscc: str, iss: Path, version: str, source_dir: Path, out: Path, sign: str | None) -> list[str]:
    # ISCC's documented switches: /D<name>=<value> defines a preprocessor variable,
    # /S<name>=<command> defines a Sign Tool, /O<path> sets the output directory. All
    # three take a leading / and the short forms are case-insensitive. Paths must be
    # absolute: Inno resolves a relative SourceDir or OutputDir against the script's own
    # folder, not the working directory (see the ate.iss header).
    cmd = [
        iscc,
        f"/DAppVersion={version}",
        f"/DVersionInfoVersion={_windows_version(version)}",
        f"/DSourceDir={source_dir}",
        f"/O{out}",
    ]
    if sign:
        cmd.append("/DSignEnabled")
        cmd.append(f"/Ssigntool={sign}")
    cmd.append(str(iss))
    return cmd


REDACTED = "<from ATE_SIGN_PASSWORD>"


def _redact(text: str, secret: str) -> str:
    """Anything that could be the signing password, replaced before it is printed or logged.

    The password is in the `iscc` command line (and therefore in `iscc`'s own output and
    any log of the build); a failing compile must not echo it back.
    """
    if not secret:
        return text
    return text.replace(secret, REDACTED)


def _display(command: list[str], secret: str) -> str:
    """The command as it would be typed, with the signing password replaced by its source."""
    parts = [_redact(part, secret) for part in command]
    return " ".join(f'"{part}"' if " " in part else part for part in parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", default=str(REPO / "dist"), help="directory holding the built ate/ bundle")
    parser.add_argument("--out", default=None, help="where the installer lands (default: the --dist directory)")
    parser.add_argument("--iss", default=str(ISS), help="the Inno Setup script to compile")
    parser.add_argument("--version", default=None, help="version to stamp (default: the bundle's own --version)")
    parser.add_argument("--iscc", default=None, help="path to iscc.exe (default: $ISCC, then PATH, then Inno Setup 6)")
    parser.add_argument("--build", action="store_true", help="build the bundle with packaging/build_pyinstaller.py first")
    parser.add_argument("--dry-run", action="store_true", help="print the iscc command instead of running it")
    args = parser.parse_args(argv)

    # Absolute before iscc sees them: Inno resolves a relative SourceDir, Source or
    # OutputDir against the folder holding ate.iss, not this process's working directory.
    dist = Path(args.dist).resolve()
    out = (Path(args.out) if args.out else dist).resolve()
    iss = Path(args.iss).resolve()
    if not iss.is_file():
        raise SystemExit(f"{iss} does not exist")

    iscc = args.iscc or _find_iscc()
    if not args.dry_run:
        if not _is_windows():
            raise SystemExit(
                "iscc, the Inno Setup compiler, is a Windows program: the installer can only be built on Windows. "
                "Use --dry-run to see the exact command from here."
            )
        if not iscc:
            raise SystemExit(
                "iscc (Inno Setup compiler) was not found. Install Inno Setup 6, or point ISCC at iscc.exe, "
                "or pass --iscc. Use --dry-run to see the command without compiling."
            )

    bundle = dist / "ate"
    if args.build or not bundle.is_dir():
        if args.dry_run and not bundle.is_dir():
            print(f"warning: {bundle} is not built; the dry run stamps {FALLBACK_VERSION}", file=sys.stderr)
            version = args.version or FALLBACK_VERSION
        else:
            if not bundle.is_dir():
                print(f"{bundle} does not exist; building it with PyInstaller")
            subprocess.run([sys.executable, str(REPO / "packaging" / "build_pyinstaller.py"), "--dist", str(dist)], check=True)
            version = args.version or _bundle_version(bundle / ("ate.exe" if os.name == "nt" else "ate"))
    else:
        version = args.version or _bundle_version(bundle / ("ate.exe" if os.name == "nt" else "ate"))

    sign = _signing()
    command = _iscc_command(iscc or "iscc", iss, version, bundle, out, _sign_tool(*sign) if sign else None)

    if args.dry_run:
        print("$ " + _display(command, sign[1] if sign else ""))
        print(f"signing: {'yes' if sign else 'no'}")
        return 0

    secret = sign[1] if sign else ""
    print("$ " + _display(command, secret))
    proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    # iscc echoes the Sign Tool command it ran, password and all: redact before printing.
    print(_redact(proc.stdout, secret))
    if proc.returncode != 0:
        raise SystemExit(f"iscc failed with exit {proc.returncode}:\n{_redact(proc.stderr, secret)[-1500:]}")
    print(f"built {out / f'ate-{version}-windows-x64-setup.exe'} (version {version}, signing {'yes' if sign else 'no'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
