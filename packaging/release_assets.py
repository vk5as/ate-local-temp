#!/usr/bin/env python3
"""The ten files a GitHub Release carries, checked before anything is published (packet 36).

    python packaging/release_assets.py --manuals artifacts/ate-manuals \
                                      --installer artifacts/ate-windows-installer \
                                      --packages artifacts/ate-linux-artifacts \
                                      --out release-assets

The `release` job of `.github/workflows/release.yml` downloads the artifacts this same run
uploaded and hands those directories here. Nothing is rebuilt: every asset is the byte-for-byte
file the smoke tests installed and ran. A release that ships half its files is worse than no
release, so all ten names are verified before the Release is created, and this exits non-zero
naming what is missing rather than letting a partial upload go out.

The six manuals come from the one list in `docs/manuals/build.py`; the four packages from the
names the packaging scripts write. `tests/test_release_ci_36.py` checks the globs below against
the file names the builds actually produce, and runs this function against fake artifact
directories to show it fails when a manual or a package is missing.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANUALS_HELP = REPO / "packaging" / "manuals.py"

#: One glob per artifact the release carries, matched against what packaging writes.
PACKAGE_GLOBS = {
    "Windows installer": "ate-*-windows-x64-setup.exe",
    "deb": "ate_*_amd64.deb",
    "tarball": "ate-*-linux-x86_64.tar.gz",
    "wheel": "ate-*.whl",
}


class AssetError(Exception):
    """A release that cannot be published. The publish step stops here."""


def asset_name(name: str, version: str | None) -> str:
    """What the Release calls this file.

    The four packages already say which release they are: `ate_0.1.0~rc1_amd64.deb`,
    `ate-0.1.0rc2-windows-x64-setup.exe`. A manual does not: `admin.pdf` on a Release page three
    months old says nothing about which version it describes, so the manuals get the version
    appended. The file on disk keeps its real name; only the asset name changes.
    """
    if not version or not name.endswith((".pdf", ".md")):
        return name
    stem, ext = name.rsplit(".", 1)
    return f"{stem}-{version}.{ext}"


def fail(message: str) -> None:
    raise AssetError(message)


def _helpers():
    spec = importlib.util.spec_from_file_location("packaging_manuals", MANUALS_HELP)
    if spec is None or spec.loader is None:
        fail(f"cannot read {MANUALS_HELP}: the list of release files lives in docs/manuals/build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _one(directory: Path, glob: str, label: str, missing: str | None = None) -> Path:
    """Exactly one file matching `glob` in `directory`: zero is a missing artifact, two is a run
    that built more than one release and cannot say which one this is."""
    found = sorted(p for p in directory.rglob(glob) if p.is_file())
    if not found:
        fail(missing or f"no {label} in {directory}: nothing matches {glob}, so this run produced no {label}")
    if len(found) > 1:
        fail(f"{directory} holds {len(found)} {label} files ({', '.join(p.name for p in found)}): a tag builds one")
    return found[0]


def assemble(manuals: Path, installer: Path, packages: Path, out: Path, version: str | None = None) -> list[Path]:
    """Copy the ten assets into `out`, refusing to write a release that is missing any of them.

    Returns the paths in the order they are named: the three PDFs, the three Markdown manuals,
    then the installer, the `.deb`, the tarball and the wheel.
    """
    helpers = _helpers()
    names = helpers.manual_file_names()
    for directory in (manuals, installer, packages):
        if not directory.is_dir():
            fail(f"{directory} is not a directory: the artifact was never downloaded")

    assets: list[Path] = []
    for name in names:
        asset = _one(manuals, name, name, missing=f"{name} is not in the ate-manuals artifact ({manuals}): the upload or the build failed")
        if name.endswith(".pdf") and not helpers.is_pdf(asset):
            fail(f"{name} is not a PDF: it starts with {asset.read_bytes()[:4]!r}")
        if not name.endswith(".pdf") and asset.stat().st_size == 0:
            fail(f"{name} is empty: a manual nobody wrote cannot be a release asset")
        if name.endswith(".pdf") and version and not helpers.carries_version(asset, version):
            fail(f"{name} carries {helpers.pdf_title(asset)!r}, not the version being released ({version})")
        assets.append(asset)

    for label, glob in PACKAGE_GLOBS.items():
        assets.append(_one(installer if label == "Windows installer" else packages, glob, label))

    out.mkdir(parents=True, exist_ok=True)
    copies = []
    for asset in assets:
        target = out / asset_name(asset.name, version)
        if target.is_file() and target.stat().st_size != asset.stat().st_size:
            fail(f"{target} is not the file this run built: {asset} is {asset.stat().st_size} bytes")
        shutil.copyfile(asset, target)
        copies.append(target)
    return copies


def report(assets: list[Path]) -> str:
    """The line the publish job prints: what is going out, and how big it is."""
    total = sum(p.stat().st_size for p in assets)
    manuals = [p for p in assets if p.suffix in (".pdf", ".md")]
    pdfs = sum(p.stat().st_size for p in manuals if p.suffix == ".pdf")
    return (
        f"{len(assets)} assets, {total / 1e6:.1f} MB total: "
        f"{len(manuals)} manuals ({len([p for p in manuals if p.suffix == '.pdf'])} PDFs, {pdfs / 1e6:.1f} MB), "
        f"{len(assets) - len(manuals)} packages"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manuals", required=True, help="the directory the ate-manuals artifact downloaded into")
    parser.add_argument("--installer", required=True, help="the directory the ate-windows-installer artifact downloaded into")
    parser.add_argument("--packages", required=True, help="the directory the ate-linux-artifacts artifact downloaded into")
    parser.add_argument("--out", required=True, help="where the assets are gathered for `gh release`")
    parser.add_argument("--version", default=None, help="the version the PDFs must carry (the tag's, normally)")
    args = parser.parse_args(argv)

    try:
        assets = assemble(Path(args.manuals), Path(args.installer), Path(args.packages), Path(args.out), args.version)
    except AssetError as exc:
        print(f"release assets incomplete: {exc}", file=sys.stderr)
        return 1

    for asset in assets:
        print(f"  {asset.name} ({asset.stat().st_size} bytes)")
    print(report(assets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
