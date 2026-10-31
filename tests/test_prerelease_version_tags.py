"""Pre-release tags: cutting `v0.1.0-rc1` end to end.

`src/ate/_version.py` is the one place a tag becomes a version, and every artifact is named or
stamped from it. A release tag is written with a dash (`v0.1.0-rc1`); the version the build reports
is PEP 440 (`0.1.0rc1`), and that is what the wheel's metadata, `ate/version.txt` in the frozen
bundle, `ate --version` and the `.deb`'s `Version` field carry. Before this, `v0.1.0-rc1` parsed to
`None`, so a release cut from it reported `0.0.0-dev` while the Linux smoke in
`.github/workflows/release.yml` expected `0.1.0-rc1`: the first tag would have failed its own check.

| Consumer | What tag `v0.1.0-rc1` produces |
| --- | --- |
| `_version.__version__`, `ate --version` | `0.1.0rc1` |
| wheel metadata (`pyproject.toml` dynamic version) | `0.1.0rc1` |
| `ate/version.txt` in the frozen bundle | `0.1.0rc1` |
| `.deb` version | `0.1.0~rc1`, which `dpkg` sorts before `0.1.0` |
| tarball | `ate-0.1.0rc1-linux-x86_64.tar.gz` |
| Inno `VersionInfoVersion` | `0.1.0.0` (four numbers, no room for `rc`) |
| smoke `--expect-version` | tag `0.1.0-rc1` normalised to `0.1.0rc1` before comparing |

Nothing here runs `apt`, `dpkg -i` or `sudo` on this machine: `dpkg --compare-versions` is the only
dpkg call, and the wheel is built in a temporary copy of the checkout.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BUILD_LINUX = REPO / "packaging" / "linux" / "build_linux.py"
BUILD_INSTALLER = REPO / "packaging" / "windows" / "build_installer.py"
SMOKE_LINUX = REPO / "packaging" / "linux" / "smoke_linux.py"


def _load(name: str, path: Path):
    """A packaging script as a module, without importing the package it packages.

    `sys.modules[name]` is set before `exec_module` because smoke_linux.py uses `@dataclass`,
    which looks the defining module up by name.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


build_linux = _load("ate_build_linux", BUILD_LINUX)
build_installer = _load("ate_build_installer", BUILD_INSTALLER)
smoke_linux = _load("ate_smoke_linux", SMOKE_LINUX)


def _git(root: Path, *args: str) -> str:
    """git in `root`, with an identity so a bare `git init` repo can commit."""
    env = dict(os.environ, GIT_AUTHOR_NAME="release", GIT_AUTHOR_EMAIL="release@example.com",
               GIT_COMMITTER_NAME="release", GIT_COMMITTER_EMAIL="release@example.com")
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"git {args} failed: {result.stderr[-300:]}"
    return result.stdout.strip()


def _describe_of_tagged_repo(tmp_path: Path, tag: str, commits_after: int = 0) -> str:
    """Raw `git describe --tags` for a throwaway repository tagged `tag` in a temporary directory.

    Not this repository: the point is that git really produced the string, so the parser is
    exercised on what `git describe` actually writes rather than on a string someone typed.
    """
    root = tmp_path / "checkout"
    root.mkdir(parents=True)
    (root / "file.txt").write_text("release build\n", encoding="utf-8", newline="\n")
    _git(root, "init", "-q")
    _git(root, "add", "file.txt")
    _git(root, "commit", "-q", "-m", "release build")
    _git(root, "tag", tag)
    for i in range(commits_after):
        (root / "file.txt").write_text(f"commit {i}\n", encoding="utf-8", newline="\n")
        _git(root, "add", "file.txt")
        _git(root, "commit", "-q", "-m", f"after the tag {i}")
    return _git(root, "describe", "--tags")


def test_rc_tag_parses_to_the_pep_440_version():
    """The two shapes a release tag reaches the parser in, from the tag and from git describe."""
    from ate._version import _parse

    assert _parse("v0.1.0-rc1") == "0.1.0rc1"
    assert _parse("v0.1.0-rc1-3-gabc1234") == "0.1.0rc1.dev3+gabc1234"
    assert _parse("0.1.0-rc1") == "0.1.0rc1", "the workflow passes the tag with the v stripped"
    assert _parse("1.2.3rc1") == "1.2.3rc1", "the PEP 440 spelling parses to itself"
    assert _parse("v0.1.0-alpha.1") == "0.1.0a1"
    assert _parse("v0.1.0-a1") == "0.1.0a1"
    assert _parse("v0.1.0-beta.2") == "0.1.0b2"
    assert _parse("v0.1.0-b2") == "0.1.0b2"
    assert _parse("v0.1.0-beta.2-4-gabc1234") == "0.1.0b2.dev4+gabc1234"


def test_unrecognised_tags_still_parse_as_before_or_not_at_all():
    """The behaviour a release already depends on is unchanged, and anything else is None."""
    from ate._version import _parse

    assert _parse("v0.1.0") == "0.1.0"
    assert _parse("v0.1.0-3-gabc1234") == "0.1.0.dev3+gabc1234"
    assert _parse("v1.2.3") == "1.2.3"
    for junk in ("main", "v0.0.0-dev", "not-a-version", "v0.1.0-rc1-2-x", "v0.1.0-4-gXYZ", "v0.1.0-", ""):
        assert _parse(junk) is None, f"{junk!r} is not a version and must fall back"


def test_a_real_git_tag_v0_1_0_rc1_describes_to_something_the_parser_reads(tmp_path: Path):
    """`git describe --tags` on a real `v0.1.0-rc1` gives exactly the string the parser must read."""
    from ate._version import _parse

    on_tag = _describe_of_tagged_repo(tmp_path, "v0.1.0-rc1")
    assert on_tag == "v0.1.0-rc1", f"git describe said {on_tag!r}"
    assert _parse(on_tag) == "0.1.0rc1"

    after = _describe_of_tagged_repo(tmp_path / "later", "v0.1.0-rc1", commits_after=2)
    assert after.startswith("v0.1.0-rc1-2-g"), f"git describe said {after!r}"
    assert _parse(after) == f"0.1.0rc1.dev2+g{after.split('-g')[1]}"


def test_the_version_a_build_reports_is_the_tag_in_pep_440_form(monkeypatch):
    """`pyproject.toml`'s dynamic version, `ate/version.txt` and `ate --version` all read this one value."""
    from ate import _version

    monkeypatch.setattr(_version, "_metadata_version", lambda: None)
    monkeypatch.setattr(_version, "_describe", lambda: "v0.1.0-rc1")
    assert _version._compute_version() == "0.1.0rc1"
    monkeypatch.setattr(_version, "_describe", lambda: "v0.1.0-rc1-3-gabc1234")
    assert _version._compute_version() == "0.1.0rc1.dev3+gabc1234"
    monkeypatch.setattr(_version, "_describe", lambda: "v0.1.0-rc1-3-nope")
    assert _version._compute_version() == _version.FALLBACK, "a tag git half-describes must not crash the build"


def test_the_wheel_takes_its_version_from_the_one_parser():
    """`pyproject.toml` has no version of its own: setuptools reads `ate._version.__version__`.

    So the wheel built from a tagged checkout carries what `_parse` produced, and the frozen
    bundle's `ate/version.txt` is written from the same value (packet 27a). A hard-coded version
    in pyproject.toml would drift from the tag the moment a prerelease is cut.
    """
    import tomllib

    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["project"].get("dynamic") == ["version"], "the version must not be hard-coded"
    assert config["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "ate._version.__version__"}

    from ate import _version

    assert _version.normalise("v0.1.0-rc1") == "0.1.0rc1", "the value the wheel and version.txt carry"


def test_the_deb_version_of_a_prerelease_sorts_before_the_release(tmp_path: Path):
    """`0.1.0rc1` has to become `0.1.0~rc1`: dpkg reads a bare `rc1` as *after* `0.1.0`.

    `~` sorts before everything else in a Debian version, so the prerelease precedes the release
    it is a prerelease of, and commits after the rc tag sort above the rc tag but below the release.
    """
    deb = build_linux._deb_version
    assert deb("0.1.0rc1") == "0.1.0~rc1"
    assert deb("0.1.0rc1.dev3+gabc1234") == "0.1.0~rc1.dev3+gabc1234"
    assert deb("0.1.0a1") == "0.1.0~a1"
    assert deb("0.1.0b2") == "0.1.0~b2"
    assert deb("0.1.0") == "0.1.0", "a final release is untouched"
    assert deb("0.0.0-dev") == "0.0.0~dev", "the untagged fallback keeps its spelling"

    def dpkg_lt(a: str, b: str) -> bool:
        return subprocess.run(["dpkg", "--compare-versions", a, "lt", b], check=False).returncode == 0

    assert dpkg_lt("0.1.0~rc1", "0.1.0"), "0.1.0~rc1 must sort before 0.1.0"
    assert dpkg_lt("0.1.0~rc1.dev3+gabc1234", "0.1.0"), "a build after the rc tag is still below the release"
    assert dpkg_lt("0.1.0~rc1", "0.1.0~rc1.dev3+gabc1234"), "the rc tag precedes commits made after it"
    assert dpkg_lt("0.1.0~rc1", "0.1.0~rc2"), "rc1 before rc2"
    assert not dpkg_lt("0.1.0rc1", "0.1.0"), "which is exactly why the bare PEP 440 string cannot go in a .deb"


def test_the_prerelease_bundle_is_named_after_it(tmp_path: Path):
    """The tarball and the .deb built from a bundle that prints `0.1.0rc1`."""
    bundle = tmp_path / "dist" / "ate"
    bundle.mkdir(parents=True)
    (bundle / "ate").write_text("#!/bin/sh\necho 0.1.0rc1\n", encoding="utf-8", newline="\n")
    out = tmp_path / "out"
    out.mkdir()

    tarball = build_linux._tarball(bundle, "0.1.0rc1", out)
    assert tarball.name == "ate-0.1.0rc1-linux-x86_64.tar.gz", tarball.name
    assert build_linux._deb_version("0.1.0rc1") == "0.1.0~rc1"


def test_the_frozen_build_writes_the_tag_and_its_smoke_accepts_it():
    """`build_pyinstaller.py` stamps `ate/version.txt` with `_version(python)`, and its own smoke
    check is `SEMVER.search(...)`: a prerelease has to pass that or the bundle is refused."""
    pyinstaller = _load("ate_build_pyinstaller", REPO / "packaging" / "build_pyinstaller.py")
    assert pyinstaller.SEMVER.search("0.1.0rc1"), "the frozen smoke rejects a prerelease it built"
    assert pyinstaller.SEMVER.search("0.1.0rc1.dev3+gabc1234"), "and a build made after the rc tag"
    source = (REPO / "packaging" / "build_pyinstaller.py").read_text(encoding="utf-8")
    assert "from ate._version import __version__" in source, "version.txt is written from the one parser"


def test_the_debian_harness_expects_the_tag_as_the_artifact_reports_it():
    """`docker_smoke.py` tags a copy of the checkout and then checks the build against that tag.

    `ATE_SMOKE_TAG=v0.1.0-rc1` must make it expect `0.1.0rc1`, the version the bundle and the wheel
    report, not the raw `0.1.0-rc1` it would otherwise compare against and call a good build broken.
    """
    harness = _load("docker_smoke", REPO / "packaging" / "linux" / "docker_smoke.py")
    assert harness._version_of("v1.2.3") == "1.2.3", "the tag that has actually been run is unchanged"
    assert harness.VERSION == harness._version_of(harness.TAG)

    previous = os.environ.get("ATE_SMOKE_TAG")
    os.environ["ATE_SMOKE_TAG"] = "v0.1.0-rc1"
    try:
        rc = _load("docker_smoke_rc", REPO / "packaging" / "linux" / "docker_smoke.py")
        assert rc.TAG == "v0.1.0-rc1" and rc.VERSION == "0.1.0rc1", (
            f"tag {rc.TAG} is expected as {rc.VERSION!r}, not {rc.TAG[1:]!r}"
        )
    finally:
        if previous is None:
            del os.environ["ATE_SMOKE_TAG"]
        else:
            os.environ["ATE_SMOKE_TAG"] = previous


def test_the_windows_file_version_of_a_prerelease_has_four_numbers():
    """Inno's `VersionInfoVersion` has no room for `rc`, so the prerelease is dropped, not mangled."""
    for version in ("0.1.0rc1", "0.1.0rc1.dev3+gabc1234", "0.1.0-rc1", "0.1.0"):
        assert build_installer._windows_version(version) == "0.1.0.0", f"{version!r} -> {build_installer._windows_version(version)}"
    assert build_installer._windows_version("0.1.0rc1").count(".") == 3
    assert all(part.isdigit() for part in build_installer._windows_version("0.1.0rc1").split("."))


def _printed_versions(*versions: str):
    """A Runner that answers `ate --version` with each version in turn."""

    class FakeRunner:
        def __init__(self) -> None:
            self.i = 0

        def run(self, argv):
            out = versions[self.i % len(versions)]
            self.i += 1
            return smoke_linux.CommandResult(0, out + "\n", "")

    return FakeRunner()


def test_the_smoke_reads_the_tag_and_compares_it_with_what_the_artifact_prints():
    """`--expect-version 0.1.0-rc1` is the tag; `ate --version` prints `0.1.0rc1`."""
    smoke_linux.check_ate_runs(_printed_versions("0.1.0rc1"), "ate", "0.1.0-rc1")
    smoke_linux.check_ate_runs(_printed_versions("0.1.0rc1"), "ate", "v0.1.0-rc1")
    smoke_linux.check_ate_runs(_printed_versions("1.2.3"), "ate", "1.2.3")

    with pytest.raises(smoke_linux.SmokeError, match="was built for '0.1.0-rc1'"):
        smoke_linux.check_ate_runs(_printed_versions("0.1.0"), "ate", "0.1.0-rc1")
    with pytest.raises(smoke_linux.SmokeError, match="was built for '0.1.0-rc1'"):
        smoke_linux.check_ate_runs(_printed_versions("0.0.0-dev"), "ate", "0.1.0-rc1")


def test_the_workflow_passes_the_tag_it_cut(tmp_path: Path):
    """`release.yml` passes the tag minus the `v`; the smoke script is what turns it into a version."""
    import yaml

    text = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    run = next(step["run"] for step in doc["jobs"]["linux-smoke"]["steps"] if "smoke_linux.py" in step.get("run", ""))
    assert "GITHUB_REF_NAME" in run and "--expect-version" in run
    assert smoke_linux.expected_version("0.1.0-rc1") == "0.1.0rc1"
    assert smoke_linux.expected_version("") is None, "workflow_dispatch has no tag to expect"
    assert smoke_linux.expected_version("main") == "main", "an expectation that is not a version is compared as written"
