"""Packet 28c: the Linux release CI.

Three things are checked here, and only one of them is the thing that ultimately matters:

* `.github/workflows/release.yml` — parsed with PyYAML and checked for the jobs, what they need,
  the container they run in, the artifact names that go up and come back down, and the fact that
  every script the workflow names is a script that exists. It says nothing about a real run: the
  first tag, `v0.1.0-rc1`, is what found a bad `iscc` call and a relative `apt-get` path.
* `packaging/linux/smoke_linux.py` — the script that runs inside `debian:stable`. Its install,
  upgrade, remove, purge and uninstall steps run against a fake container rooted in a temporary
  directory: `apt-get` and `dpkg` are modelled, `install.sh` is run for real with its documented
  `--prefix`/`--bin` seam, and every `ate` call is the real source CLI against the container's own
  config and database. So these tests prove the smoke script *notices* a broken install — not that
  a real Debian install works.
* `packaging/linux/docker_smoke.py` — the same sequence in three real `debian:stable` containers,
  gated on `ATE_DOCKER_TESTS=1`. That is the part that has actually run.

Nothing here executes `apt`, `dpkg -i`, `sudo`, `groupadd` or `chown` on this machine (containers
do, when the gate is on), and nothing is published anywhere: the artifacts only ever go to
`actions/upload-artifact`.

| Seam | What it does |
| --- | --- |
| `smoke.Station(root=...)` | Roots `/opt/ate`, `/usr/bin/ate`, `/etc/ate` and `/var/lib/ate` in a directory |
| `smoke.Runner` | Every command the script runs on the machine; the fake replaces it |
| `ATE_STUB_SKIPS_SETUP` | Read only by the test stub bundle: `ate setup` succeeds and does nothing |
"""

from __future__ import annotations

import ast
import importlib.metadata
import fnmatch
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "release.yml"
SMOKE = REPO / "packaging" / "linux" / "smoke_linux.py"
BUILD_LINUX = REPO / "packaging" / "linux" / "build_linux.py"
DOCKER_SMOKE = REPO / "packaging" / "linux" / "docker_smoke.py"
INSTALL_SH = REPO / "packaging" / "linux" / "install.sh"
POSTINST = REPO / "packaging" / "linux" / "deb" / "postinst"
POSTRM = REPO / "packaging" / "linux" / "deb" / "postrm"
EXAMPLE = REPO / "examples" / "media-release"
SRC = REPO / "src"
UPLOAD_NAME = "ate-linux-artifacts"

from ate import _version as version_module
from ate.cli import _report_problem
from ate.report.pdf import pdf_has_text, run_pdf_helper

_spec = importlib.util.spec_from_file_location("docker_smoke", DOCKER_SMOKE)
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)

try:  # pragma: no cover - depends on the environment's installs
    from PyQt6.QtWebEngineCore import QWebEnginePage  # noqa: F401

    WEBENGINE_AVAILABLE = True
except Exception:
    WEBENGINE_AVAILABLE = False


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


smoke = _load("smoke_linux", SMOKE)
packaging = _load("build_linux_for_28c", BUILD_LINUX)


# --- release.yml: parsed, not grepped ----------------------------------------------------------


def _upload_paths() -> list[str]:
    upload = next(u for u in _uploads(_workflow()) if u["name"] == UPLOAD_NAME)
    return [line.strip() for line in upload["path"].splitlines() if line.strip()]


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _workflow() -> dict:
    doc = yaml.safe_load(_workflow_text())
    assert isinstance(doc, dict), "release.yml is not a mapping"
    return doc


def _on(doc: dict) -> dict:
    return doc["on"] if "on" in doc else doc[True]


def _runs(doc: dict) -> list[str]:
    """Every `run:` command in the workflow, in file order."""
    return [step["run"] for job in doc["jobs"].values() for step in job["steps"] if step.get("run")]


def _uploads(doc: dict) -> list[dict]:
    return [
        step["with"]
        for job in doc["jobs"].values()
        for step in job["steps"]
        if "upload-artifact" in str(step.get("uses", ""))
    ]


def _downloads(doc: dict) -> list[dict]:
    return [
        step["with"]
        for job in doc["jobs"].values()
        for step in job["steps"]
        if "download-artifact" in str(step.get("uses", ""))
    ]


def test_the_release_workflow_still_triggers_on_tags_and_manual_runs_only():
    """28c must not add a trigger: an artifact is only ever made for a release."""
    on = _on(_workflow())
    assert set(on) == {"push", "workflow_dispatch"}, f"unexpected triggers: {sorted(on)}"
    assert on["push"] == {"tags": ["v*"]}, f"push must stay narrowed to v* tags, got {on['push']}"
    assert "branches" not in on["push"], "a branch push must not start a release build"
    assert _workflow().get("permissions") == {"contents": "read"}, "nothing here needs write access"


def test_the_windows_job_is_still_there_and_still_unchanged():
    doc = _workflow()
    job = doc["jobs"]["windows-installer"]
    assert job["runs-on"] == "windows-latest"
    assert job["steps"][0]["with"]["fetch-depth"] == 0
    upload = next(step["with"] for step in job["steps"] if "upload-artifact" in str(step.get("uses", "")))
    assert upload["name"] == "ate-windows-installer"
    assert upload["path"] == "dist/ate-*-windows-x64-setup.exe"


def test_the_linux_build_job_builds_all_three_artifacts_in_order():
    doc = _workflow()
    job = doc["jobs"]["linux-build"]
    commands = [line.strip() for step in job["steps"] for line in step.get("run", "").splitlines() if line.strip().startswith("python")]
    assert commands == [
        "python packaging/build_pyinstaller.py --smoke",
        "python packaging/linux/build_linux.py --dist dist",
        "python -m build --wheel",
    ], f"the bundle, then the tarball and .deb, then the wheel, in that order: {commands}"
    checkout = next(step for step in job["steps"] if "actions/checkout" in str(step.get("uses", "")))
    assert checkout["with"]["fetch-depth"] == 0, "the artifact names carry the tag: tags must be fetched"
    python = next(step for step in job["steps"] if "setup-python" in str(step.get("uses", "")))
    assert python["with"]["python-version"] == "3.14", "the frozen artifacts are required to carry CPython 3.14"
    assert job["env"]["QT_QPA_PLATFORM"] == "offscreen", "the bundle's _pdf helper and _selftest start QtWebEngine"


def test_the_build_job_installs_the_libraries_pyinstaller_needs_to_import_qt():
    """The build machine has to be able to import PyQt6, or the bundle ships a renderer that cannot start.

    PyInstaller's Qt hooks collect `QtWebEngineProcess`, its `.pak` resources and the Qt platform
    plugins only when PyQt6 imports in the build environment. In a bare Debian container the import
    fails on libGL/libglib, the hooks are skipped without a word, and every Test Report the Station
    writes is a 1.7 KB PDF with nothing in it. A debian:stable container caught this (see
    packaging/linux/docker_smoke.py), so the libraries the artifacts need at runtime are installed
    before the build runs.
    """
    doc = _workflow()
    job = doc["jobs"]["linux-build"]
    step = next((s for s in job["steps"] if "apt-get install" in s.get("run", "")), None)
    assert step is not None, "the build job must install the libraries PyInstaller's Qt hooks need"
    run = step["run"]
    # libc6 is in every Debian and Ubuntu image, so the step does not name it: everything else the
    # artifacts need at runtime has to be installed before PyInstaller runs.
    missing = [pkg for pkg in packaging.DEPENDS + packaging.WHEEL_SYSTEM_LIBS if pkg != "libc6" and pkg not in run]
    assert not missing, f"the build installs the runtime libraries the artifacts need; missing {missing}"
    assert any("build_pyinstaller.py --smoke" in s.get("run", "") for s in job["steps"]), "the smoke checks are the catch"
    names = [s.get("run", "") for s in job["steps"]]
    assert names.index(run) < next(i for i, s in enumerate(names) if "build_pyinstaller.py" in s), "the libraries have to be installed before PyInstaller runs"


def test_the_upload_globs_match_the_artifacts_the_builds_produce():
    """Each glob has to match the name the packaging actually writes, or the release is empty."""
    upload = next(u for u in _uploads(_workflow()) if u["name"] == UPLOAD_NAME)
    paths = [line.strip() for line in upload["path"].splitlines() if line.strip()]
    produced = {
        "tarball": f"dist/ate-{packaging._deb_version('1.2.3')}-linux-x86_64.tar.gz",
        "deb": f"dist/ate_{packaging._deb_version('1.2.3-rc1')}_amd64.deb",
        "wheel": "dist/ate-1.2.3-py3-none-any.whl",
        "glibc": "dist/glibc-build.txt",
    }
    for kind, name in produced.items():
        assert any(fnmatch.fnmatch(name, glob) for glob in paths), f"no upload glob matches the {kind} {name}: {paths}"
    assert upload.get("if-no-files-found") == "error", "an artifact glob that matches nothing must fail, not warn"
    assert "dist/ate-*-linux-x86_64.tar.gz" in paths and "dist/ate_*_amd64.deb" in paths, paths


def test_the_smoke_jobs_need_the_build_and_run_in_debian_stable():
    doc = _workflow()
    job = doc["jobs"]["linux-smoke"]
    needs = job["needs"]
    assert (needs if isinstance(needs, list) else [needs]) == ["linux-build"], "the artifacts come from the build job, nothing else"
    assert job["container"] == {"image": "debian:stable"}, job["container"]
    kinds = job["strategy"]["matrix"]["kind"]
    assert kinds == ["deb", "tar", "wheel"], f"one smoke run per artifact: {kinds}"
    assert job["strategy"].get("fail-fast") is False, "a broken .deb must not cancel the wheel's run"
    assert job["env"]["QT_QPA_PLATFORM"] == "offscreen"
    checkout = next(step for step in job["steps"] if "actions/checkout" in str(step.get("uses", "")))
    assert "fetch-depth" not in (checkout.get("with") or {}), (
        "the smoke container has no git and reads no history: fetching tags there buys nothing "
        "and the comment claiming it does is false"
    )
    assert "fetch-depth" not in str(checkout), "the smoke job's checkout must not fetch history"
    runs = [step["run"] for step in job["steps"] if "smoke_linux.py" in step.get("run", "")]
    assert len(runs) == 1, runs
    assert "--kind ${{ matrix.kind }}" in runs[0] and "--dist artifacts" in runs[0], runs[0]
    assert "--expect-version" in runs[0], "the smoke run has to check the artifact reports the tag it was built from"
    assert "GITHUB_REF_NAME" in runs[0], "the expected version comes from the tag, not a hard-coded string"


def test_the_smoke_jobs_download_exactly_what_the_build_uploads():
    doc = _workflow()
    assert [d["name"] for d in _uploads(doc)] == ["ate-windows-installer", UPLOAD_NAME]
    assert [d["name"] for d in _downloads(doc)] == [UPLOAD_NAME], "only the Linux artifacts are downloaded"
    assert _downloads(doc)[0]["path"] == "artifacts", "the download lands where the smoke script is told to look"
    for upload in _uploads(doc):
        assert upload.get("if-no-files-found") == "error", "every upload must fail on an empty glob"


def test_the_linux_runner_is_pinned_to_the_glibc_that_debian_stable_can_run():
    """glibc is forward compatible only: a bundle built against a newer glibc will not start in Debian.

    `ubuntu-latest` moves; the pin is what keeps the baseline below Debian stable's, and the build
    records its own glibc so the smoke run checks the assumption instead of trusting it.
    """
    doc = _workflow()
    assert doc["jobs"]["linux-build"]["runs-on"] == "ubuntu-24.04", doc["jobs"]["linux-build"]["runs-on"]
    assert doc["jobs"]["linux-smoke"]["runs-on"] == "ubuntu-24.04"
    for job in doc["jobs"].values():
        if job["runs-on"] == "ubuntu-latest":
            raise AssertionError("a Linux job must pin the runner, not follow ubuntu-latest")
    record = next(step for step in doc["jobs"]["linux-build"]["steps"] if "glibc-build.txt" in step.get("run", ""))
    assert "ldd --version" in record["run"], "the build has to record the glibc it linked against"
    assert any(path.endswith("glibc-build.txt") for path in _upload_paths()), "the marker has to be uploaded for the smoke run to read it"


def test_every_script_the_workflow_runs_exists():
    """A step that names a script the repository does not have fails on the runner, not here."""
    for run in _runs(_workflow()):
        for script in re.findall(r"\bpython3?\s+([^\s{}]+\.(?:py|sh))", run):
            assert (REPO / script).is_file(), f"the workflow runs {script}, which is not in the repository"
    assert smoke.parser().parse_args(["--kind", "deb", "--dist", "artifacts"]).kind == "deb"


def test_the_placeholder_comment_is_gone_and_nothing_is_published():
    text = _workflow_text()
    assert "deliberately not written" not in text, "27c's placeholder must be replaced by the real jobs"
    assert "linux-artifacts:" not in text, "the commented placeholder is still in the file"
    assert "workflow_run" not in text and "actions/trigger" not in text, "a release job must not start another workflow"
    for forbidden in ("gh release create", "create-release", "s3.amazonaws", "curl -T"):
        assert forbidden not in text, f"the workflow publishes an artifact: {forbidden}"
    smoke_runs = [step["run"] for step in _workflow()["jobs"]["linux-smoke"]["steps"] if step.get("run")]
    for run in smoke_runs:
        assert "sudo" not in run, "the smoke container is already root: nothing in it needs sudo"
        assert "dpkg -i" not in run, "apt-get install ./ate_*.deb is what proves the Depends resolve"
        assert "groupadd" not in run and "chown" not in run, "ate setup does the privileged work, not the workflow"


# --- the fake Debian container ----------------------------------------------------------------


def _stub_ate() -> str:
    """A bundle executable that is really this repository's CLI.

    Two test-only seams, read by the stub and by nobody else: `ATE_STUB_SKIPS_SETUP` makes
    `ate setup` succeed and do nothing (the way to show the smoke script notices a postinst that
    never prepared the Station), and `ATE_DATA_DIR`/`ATE_CONFIG_DIR` aim setup at the fake
    container's root instead of the real `/var/lib/ate` and `/etc/ate`.
    """
    return (
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "from ate.cli import main\n"
        "argv = sys.argv[1:]\n"
        "if argv and argv[0] == 'setup':\n"
        "    if os.environ.get('ATE_STUB_SKIPS_SETUP'):\n"
        "        print('ate setup: nothing done')\n"
        "        sys.exit(0)\n"
        "    argv += ['--data-dir', os.environ['ATE_DATA_DIR'], '--config-dir', os.environ['ATE_CONFIG_DIR']]\n"
        "sys.exit(main(argv))\n"
    )


def _fake_bundle(root: Path) -> Path:
    """A bundle shaped like `dist/ate`: the executable, an `_internal` directory, and the parts
    `packaging/build_pyinstaller.py --verify` requires, so packaging has something to accept."""
    bundle = root / "dist" / "ate"
    internal = bundle / "_internal"
    (internal / "PyQt6" / "Qt6" / "lib").mkdir(parents=True)
    (internal / "PyQt6" / "Qt6" / "plugins" / "platforms").mkdir(parents=True)
    (internal / "PyQt6" / "Qt6" / "libexec").mkdir(parents=True)
    (internal / "PyQt6" / "Qt6" / "resources").mkdir(parents=True)
    (internal / "ate" / "lua").mkdir(parents=True)
    (internal / "ate" / "report" / "templates").mkdir(parents=True)
    exe = bundle / "ate"
    exe.write_text(_stub_ate(), encoding="utf-8", newline="\n")
    exe.chmod(0o755)
    (internal / "base_library.zip").write_text("not really a zip", encoding="utf-8", newline="\n")
    (internal / "PyQt6" / "Qt6" / "lib" / "libQt6Core.so.6").write_text("stub", encoding="utf-8", newline="\n")
    (internal / "PyQt6" / "Qt6" / "plugins" / "platforms" / "libqoffscreen.so").write_text("stub", encoding="utf-8", newline="\n")
    (internal / "PyQt6" / "Qt6" / "libexec" / "QtWebEngineProcess").write_text("stub", encoding="utf-8", newline="\n")
    (internal / "PyQt6" / "Qt6" / "resources" / "icudtl.dat").write_text("stub", encoding="utf-8", newline="\n")
    (internal / "PyQt6" / "Qt6" / "resources" / "qtwebengine_resources.pak").write_text("stub", encoding="utf-8", newline="\n")
    (internal / "ate" / "lua" / "devices.lua").write_text("-- bundled", encoding="utf-8", newline="\n")
    (internal / "ate" / "report" / "templates" / "report.html.j2").write_text("<html>", encoding="utf-8", newline="\n")
    return bundle


_CACHE: dict[str, Path] = {}


@pytest.fixture(scope="session", autouse=True)
def _clean_the_fake_artifacts():
    """The stub artifacts are built once for the session and removed at the end: they are not
    pytest `tmp_path` files, so nothing else would take them out of `/tmp`."""
    yield
    for path in _CACHE.values():
        shutil.rmtree(path, ignore_errors=True)
    _CACHE.clear()


def _artifacts() -> Path:
    """One set of artifacts (tarball, .deb, wheel, glibc marker) built once, from a stub bundle."""
    if os.name == "nt":
        pytest.skip("build_linux.py makes a .deb and a Linux tarball from a Linux bundle")
    if "dist" not in _CACHE:
        where = Path(tempfile.mkdtemp(prefix="ate-28c-artifacts-"))
        bundle = _fake_bundle(where)
        dist = where / "artifacts"
        proc = subprocess.run(
            [sys.executable, str(BUILD_LINUX), "--dist", str(bundle.parent), "--out", str(dist)],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert proc.returncode == 0, f"build_linux.py failed: {proc.stdout}\n{proc.stderr}"
        version = subprocess.run(
            [sys.executable, "-c", "from ate._version import __version__; print(__version__)", "--"],
            capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(SRC)), timeout=60,
        ).stdout.strip() or "0.0.0-dev"
        wheel = dist / f"ate-{version.replace('-dev', '.dev0')}-py3-none-any.whl"
        with __import__("zipfile").ZipFile(wheel, "w") as zf:
            zf.writestr("ate/__init__.py", "")
            zf.writestr(f"ate-{version.replace('-dev', '.dev0')}.dist-info/METADATA", "Metadata-Version: 2.1\nName: ate\n")
            zf.writestr(f"ate-{version.replace('-dev', '.dev0')}.dist-info/WHEEL", "Wheel-Version: 1.0\n")
        (dist / "glibc-build.txt").write_text("ldd (Ubuntu GLIBC 2.39-0ubuntu2) 2.39\n", encoding="utf-8", newline="\n")
        _CACHE["dist"] = dist
        _CACHE["where"] = where
    return _CACHE["dist"]


DEBIAN_STABLE = {
    "python3",
    "python3-venv",
    "pipx",
    "tar",
    "dpkg",
    *packaging.DEPENDS,
    *packaging.WHEEL_SYSTEM_LIBS,
}
DEBIAN_STABLE_GLIBC = "ldd (Debian GLIBC 2.41-6) 2.41"


class FakeContainer:
    """A Debian stable container in a directory.

    `apt-get`, `dpkg` and `pipx` are modelled; `tar` and `install.sh` really run, inside the root.
    Every `ate` call is the real source CLI against this container's config and database, so the
    Station checks are genuine. `_selftest --run` needs a frozen bundle and QtWebEngine, so it is
    canned. Nothing here touches the real system: no apt, no dpkg -i, no sudo, no groupadd, no chown.

    Every keyword below is a way the packaging could break its promise; each one is a test.
    """

    def __init__(
        self,
        root: Path,
        station: smoke.Station,
        *,
        glibc: str = DEBIAN_STABLE_GLIBC,
        available: set[str] | None = None,
        version: str = "0.0.0-dev",
        postinst_runs_setup: bool = True,
        schema: int | None = None,
        remove_keeps_config: bool = True,
        remove_removes_program: bool = True,
        purge_removes_config: bool = True,
        purge_keeps_database: bool = True,
        purge_keeps_reports: bool = True,
        purge_keeps_logs: bool = True,
        upgrade_keeps_data: bool = True,
        upgrade_clears_internal: bool = True,
        uninstall_keeps_records: bool = True,
        install_exit: int = 0,
        version_exit: int = 0,
        selftest_exit: int = 0,
        selftest_output: str = "run end-to-end: ok - _pdf wrote the report\n",
    ) -> None:
        self.root = Path(root)
        self.station = station
        self.glibc = glibc
        self.available = set(available) if available is not None else set(DEBIAN_STABLE)
        self.version = version
        self.postinst_runs_setup = postinst_runs_setup
        self.schema = schema
        self.remove_keeps_config = remove_keeps_config
        self.remove_removes_program = remove_removes_program
        self.purge_removes_config = purge_removes_config
        self.purge_keeps_database = purge_keeps_database
        self.purge_keeps_reports = purge_keeps_reports
        self.purge_keeps_logs = purge_keeps_logs
        self.upgrade_keeps_data = upgrade_keeps_data
        self.upgrade_clears_internal = upgrade_clears_internal
        self.uninstall_keeps_records = uninstall_keeps_records
        self.install_exit = install_exit
        self.version_exit = version_exit
        self.selftest_exit = selftest_exit
        self.selftest_output = selftest_output
        self.installs = 0
        self.calls: list[list[str]] = []
        self.env = dict(
            os.environ,
            QT_QPA_PLATFORM="offscreen",
            ATE_DEFAULT_CONFIG=str(station.config),
            ATE_DATA_DIR=str(station.data_dir),
            ATE_CONFIG_DIR=str(station.config_dir),
        )

    def run(self, argv: list[str]) -> smoke.CommandResult:
        argv = [str(part) for part in argv]
        self.calls.append(argv)
        exe = Path(argv[0]).name
        if exe == "apt-get":
            return self._apt(argv[1:])
        if exe == "tar":
            return self._subprocess(argv)
        if exe == "install.sh":
            return self._install_sh(argv)
        if exe == "pipx":
            return self._pipx(argv[1:])
        if exe == "ldd":
            return smoke.CommandResult(0, self.glibc + "\n", "")
        if exe == "ate":
            return self._ate(argv)
        return smoke.CommandResult(127, "", f"the fake container does not run {argv[0]}")

    def _subprocess(self, argv: list[str]) -> smoke.CommandResult:
        proc = subprocess.run(argv, capture_output=True, text=True, env=self.env, timeout=180)
        return smoke.CommandResult(proc.returncode, proc.stdout, proc.stderr)

    def _apt(self, args: list[str]) -> smoke.CommandResult:
        if args == ["update"]:
            return smoke.CommandResult(0, "Get:1 http://deb.debian.org Debian stable InRelease\n", "")
        if args[:2] == ["install", "-y"]:
            packages = args[2:]
            # Real apt takes an argument as a local file only with a leading /, ./ or ../: it reads
            # `artifacts/ate.deb` as package `artifacts` from a release called `ate.deb`.
            for package in packages:
                if package.endswith(".deb") and not package.startswith(("/", "./", "../")):
                    return smoke.CommandResult(100, "", f"E: Unable to locate package {package.split('/')[0]}")
            deb = [p for p in packages if p.endswith(".deb")]
            if deb:
                return self._install_deb(Path(deb[0]))
            missing = [p for p in packages if p not in self.available]
            if missing:
                return smoke.CommandResult(100, "", f"Unable to locate package: {', '.join(missing)}")
            return smoke.CommandResult(0, f"Setting up {len(packages)} packages\n", "")
        if args[:2] == ["remove", "-y"] and args[2:] == ["ate"]:
            self._remove_package(purge=False)
            return smoke.CommandResult(0, "", "")
        if args[:2] == ["purge", "-y"] and args[2:] == ["ate"]:
            self._remove_package(purge=True)
            return smoke.CommandResult(0, "", "")
        return smoke.CommandResult(1, "", f"apt-get: cannot handle {' '.join(args)}")

    def _install_deb(self, deb: Path) -> smoke.CommandResult:
        if not deb.is_file():
            return smoke.CommandResult(1, "", f"cannot install {deb}: the artifact is not there")
        control = subprocess.run(["dpkg-deb", "-I", str(deb)], capture_output=True, text=True).stdout
        depends = [p.strip() for p in re.search(r"^\s*Depends: (.+)$", control, re.MULTILINE).group(1).split(",")]
        missing = [p for p in depends if p not in self.available]
        if missing:
            return smoke.CommandResult(100, "", f"apt-get: unable to satisfy Depends: {', '.join(missing)}")
        extracted = subprocess.run(["dpkg-deb", "-x", str(deb), str(self.root)], capture_output=True, text=True)
        if extracted.returncode != 0:
            return smoke.CommandResult(extracted.returncode, "", extracted.stderr)
        self._link_bin()
        if not self.postinst_runs_setup:
            self.env["ATE_STUB_SKIPS_SETUP"] = "1"
        result = self._maintainer("postinst", "configure")
        if result.returncode != 0:
            return smoke.CommandResult(result.returncode, "", result.stderr)
        self._apply_schema()
        return smoke.CommandResult(0, "Setting up ate\n", "")

    def _link_bin(self) -> None:
        self.station.bin.parent.mkdir(parents=True, exist_ok=True)
        if self.station.bin.is_symlink() or self.station.bin.exists():
            self.station.bin.unlink()
        self.station.bin.symlink_to(self.station.app_dir / "ate")

    def _maintainer(self, script: str, action: str) -> smoke.CommandResult:
        """Run dpkg's maintainer script against this root, as dpkg would.

        The shipped scripts name the real paths and have no test seam, so a copy with the paths
        rewritten to the container root is run, exactly like tests/test_linux_packages_28b.py does.
        """
        source = self.root / "DEBIAN" / script
        if not source.is_file():
            source = POSTINST if script == "postinst" else POSTRM
        text = source.read_text(encoding="utf-8")
        for real in ("/opt/ate", "/etc/ate", "/var/lib/ate"):
            text = text.replace(real, str(self.root / real.lstrip("/")))
        copy = self.root / f"{script}-test"
        copy.write_text(text, encoding="utf-8", newline="\n")
        copy.chmod(0o755)
        return self._subprocess(["/bin/sh", str(copy), action])

    def _remove_package(self, *, purge: bool) -> None:
        # `remove_removes_program=False` models dpkg leaving the bundle and its link behind after
        # `apt remove`, which `check_remove_keeps_the_station` is supposed to notice.
        if self.remove_removes_program:
            shutil.rmtree(self.station.app_dir, ignore_errors=True)
            if self.station.bin.exists() or self.station.bin.is_symlink():
                self.station.bin.unlink()
        result = self._maintainer("postrm", "purge" if purge else "remove")
        if result.returncode != 0:
            return
        if purge:
            if not self.purge_removes_config:
                self.station.config_dir.mkdir(parents=True, exist_ok=True)
                self.station.config.write_text('{"format": 1}', encoding="utf-8", newline="\n")
            # Each record is a separate promise: purge may delete none of them.
            if not self.purge_keeps_database:
                self.station.data_dir.joinpath("ate.db").unlink(missing_ok=True)
            if not self.purge_keeps_reports:
                shutil.rmtree(self.station.data_dir / "reports", ignore_errors=True)
            if not self.purge_keeps_logs:
                shutil.rmtree(self.station.data_dir / "logs", ignore_errors=True)
        elif not self.remove_keeps_config:
            shutil.rmtree(self.station.config_dir, ignore_errors=True)

    def _install_sh(self, argv: list[str]) -> smoke.CommandResult:
        """Run the shipped install.sh for real, aimed at this root through its --prefix/--bin seam.

        The flags below then break one of the promises the script makes, which is what the checks
        are supposed to catch.
        """
        if self.install_exit:
            return smoke.CommandResult(self.install_exit, "", "install.sh: the install failed")
        result = self._subprocess(["/bin/sh", *argv])
        if result.returncode != 0:
            return result
        if "--uninstall" in argv:
            if not self.uninstall_keeps_records:
                shutil.rmtree(self.station.data_dir, ignore_errors=True)
            return result
        self.installs += 1
        self._apply_schema()
        if self.installs > 1:
            if not self.upgrade_keeps_data:
                # The Station's records survive an upgrade; wiping the data is what the check must
                # find, so the database itself is left in place for the later checks to see.
                for child in self.station.data_dir.iterdir():
                    if child.name != "ate.db":
                        shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink()
            if not self.upgrade_clears_internal:
                stale = self.station.app_dir / "_internal" / "stale-from-the-previous-build.py"
                stale.parent.mkdir(parents=True, exist_ok=True)
                stale.write_text("a file the previous build shipped", encoding="utf-8", newline="\n")
        return result

    def _pipx(self, args: list[str]) -> smoke.CommandResult:
        wheel = Path(args[-1])
        if args[:1] != ["install"] or not wheel.is_file():
            return smoke.CommandResult(1, "", f"pipx: {wheel} is not a wheel")
        self.station.wheel_bin.parent.mkdir(parents=True, exist_ok=True)
        self.station.wheel_bin.write_text(_stub_ate(), encoding="utf-8", newline="\n")
        self.station.wheel_bin.chmod(0o755)
        return smoke.CommandResult(0, "pipx installed ate\n", "")

    def _apply_schema(self) -> None:
        """Set the database to the schema version the container's ATE should be at (5 normally)."""
        import sqlite3

        database = self.station.data_dir / "ate.db"
        if database.is_file():
            conn = sqlite3.connect(database)
            conn.execute(f"PRAGMA user_version={5 if self.schema is None else self.schema}")
            conn.commit()
            conn.close()

    def _ate(self, argv: list[str]) -> smoke.CommandResult:
        args = argv[1:]
        if args and args[0] in ("--version", "-V"):
            # The version an installed artifact prints is the one stamped into it at build time;
            # the fake prints `version`, and `version_exit` makes the program unrunnable.
            if self.version_exit:
                return smoke.CommandResult(self.version_exit, "", "ate: the installed program would not start")
            return smoke.CommandResult(0, f"{self.version}\n", "")
        if args[:1] == ["--allow-unsandboxed-pdf"] and args[1:3] == ["_selftest", "--run"]:
            # The frozen bundle's end-to-end run needs QtWebEngine; the CI runs it, the fake says
            # what 27a's smoke prints when it passes, and `selftest_exit` makes it fail.
            return smoke.CommandResult(self.selftest_exit, self.selftest_output, "")
        if args[:2] == ["_selftest", "--run"]:
            # Packet 30d: this container is root, so a selftest that was not given the flag reports
            # its PDF as skipped and exits non-zero. A smoke run that forgets the flag fails here.
            return smoke.CommandResult(
                1,
                "run end-to-end: MISSING - the PDF was skipped: ATE is running as root or an "
                "administrator and no --allow-unsandboxed-pdf was given\n",
                "",
            )
        return self._subprocess(argv)


def _container(tmp_path: Path, **flags) -> tuple[FakeContainer, smoke.Station]:
    root = tmp_path / "container"
    root.mkdir(parents=True, exist_ok=True)
    station = smoke.Station(root=root, home=root / "home")
    return FakeContainer(root, station, **flags), station


def _broken_release(tmp_path: Path) -> Path:
    """A Media Release whose .atedef is not a valid one: the scan has to report it as broken."""
    release = tmp_path / "broken-release"
    release.mkdir()
    (release / "oops.tslua").write_text("return ScheduleParams('broken', 'no params')\n", encoding="utf-8", newline="\n")
    (release / "broken.atedef").write_text(
        json.dumps({"format": 1, "mediaNumber": "MR-BROKEN", "name": "Broken", "version": "not semver", "files": []}),
        encoding="utf-8",
        newline="\n",
    )
    return release


def _tampered_release(tmp_path: Path) -> Path:
    """A copy of the shipped example with one schedule edited: its checksum no longer matches."""
    release = tmp_path / "tampered"
    shutil.copytree(EXAMPLE, release)
    (release / "schedule_basics.tslua").write_text(
        (release / "schedule_basics.tslua").read_text(encoding="utf-8") + "\n-- touched by the smoke test\n",
        encoding="utf-8",
        newline="\n",
    )
    return release


# --- the smoke sequence, per kind --------------------------------------------------------------


def test_deb_happy_path(tmp_path: Path) -> None:
    runner, station = _container(tmp_path)
    passed = smoke.run_smoke("deb", _artifacts(), runner, station, EXAMPLE)
    assert len(passed) == 8, passed
    assert not (station.app_dir / "ate").exists() and not station.bin.exists(), "remove and purge removed the program"
    assert not station.config.exists(), "purge removed the config setup wrote"
    assert station.data_dir.joinpath("ate.db").is_file(), "purge never touches the records"
    assert ["apt-get", "update"] in runner.calls, runner.calls
    assert any(c[:3] == ["apt-get", "install", "-y"] and c[3].endswith(".deb") for c in runner.calls)
    assert ["apt-get", "remove", "-y", "ate"] in runner.calls and ["apt-get", "purge", "-y", "ate"] in runner.calls


def test_deb_from_a_relative_dist_reaches_apt_as_a_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """release.yml passes `--dist artifacts`, and apt reads `artifacts/ate.deb` as a package name."""
    artifacts = _artifacts()
    monkeypatch.chdir(artifacts.parent)
    runner, station = _container(tmp_path)
    assert len(smoke.run_smoke("deb", Path(artifacts.name), runner, station, EXAMPLE)) == 8


def test_deb_leaves_the_station_after_remove_and_before_purge(tmp_path: Path) -> None:
    """The two lifecycle promises, checked at the moment each one is made."""
    runner, station = _container(tmp_path)
    ate = smoke.install_deb(runner, station, smoke.find_artifact(_artifacts(), "deb"))
    prepared = smoke.check_station_prepared(runner, station, ate)
    smoke.check_remove_keeps_the_station(runner, station, prepared)
    assert station.config.is_file() and prepared["database"].is_file(), "apt remove keeps config and data"
    smoke.check_purge_removes_only_the_config(runner, station, prepared)
    assert not station.config.exists(), "purge removed the config setup wrote"
    assert prepared["database"].is_file(), "purge kept the database"
    for key in ("reports", "log"):
        assert Path(str(prepared["config"][key]["directory"])).is_dir(), f"purge kept the {key} directory"


def test_tar_happy_path(tmp_path: Path) -> None:
    runner, station = _container(tmp_path)
    passed = smoke.run_smoke("tar", _artifacts(), runner, station, EXAMPLE)
    assert len(passed) == 8, passed
    assert not (station.app_dir / "ate").exists() and not station.bin.exists(), "uninstall removed the program"
    assert station.config.is_file() and station.data_dir.joinpath("ate.db").is_file(), "the Station survived the uninstall"
    installed_libs = next(c for c in runner.calls if c[:2] == ["apt-get", "install"] and not c[2].endswith(".deb"))
    assert set(installed_libs[3:]) == set(packaging.DEPENDS), "the tarball's runtime libs are the .deb's Depends"
    assert sum(1 for c in runner.calls if Path(c[0]).name == "install.sh") == 3, "install, upgrade, uninstall"


def test_install_sh_installs_and_sets_up(tmp_path: Path) -> None:
    """What the tarball does before any of the lifecycle checks: the bundle lands in /opt/ate."""
    runner, station = _container(tmp_path)
    ate, work = smoke.install_tar(runner, station, smoke.find_artifact(_artifacts(), "tar"))
    assert (station.app_dir / "ate").is_file() and station.bin.is_file(), "install.sh put the bundle and the link there"
    assert station.config.is_file() and station.data_dir.joinpath("ate.db").is_file(), "install.sh ran 'ate setup'"
    assert Path(ate) == station.bin


def test_wheel_happy_path(tmp_path: Path) -> None:
    runner, station = _container(tmp_path)
    passed = smoke.run_smoke("wheel", _artifacts(), runner, station, EXAMPLE)
    assert len(passed) >= 6, passed
    assert station.wheel_bin.is_file() and station.config.is_file()
    installed = next(c for c in runner.calls if c[:2] == ["apt-get", "install"])
    assert {"python3", "python3-venv", "pipx"} <= set(installed[2:]), installed
    assert set(packaging.WHEEL_SYSTEM_LIBS) <= set(installed[2:]), "PyQt6 from PyPI needs its own system libraries"


def test_a_missing_artifact_fails_loudly(tmp_path: Path) -> None:
    runner, station = _container(tmp_path)
    empty = tmp_path / "nothing"
    empty.mkdir()
    with pytest.raises(smoke.SmokeError, match="no deb artifact"):
        smoke.run_smoke("deb", empty, runner, station, EXAMPLE)


def test_postinst_that_never_ran_setup_fails(tmp_path: Path) -> None:
    """The .deb installs, but the Station is not prepared: that is exactly what the run must catch."""
    runner, station = _container(tmp_path, postinst_runs_setup=False)
    with pytest.raises(smoke.SmokeError, match="config.json does not exist"):
        smoke.run_smoke("deb", _artifacts(), runner, station, EXAMPLE)


def test_a_database_at_the_wrong_schema_fails(tmp_path: Path) -> None:
    runner, station = _container(tmp_path, schema=4)
    with pytest.raises(smoke.SmokeError, match="did not print a schema version"):
        smoke.run_smoke("deb", _artifacts(), runner, station, EXAMPLE)


def test_a_scan_with_broken_entries_fails(tmp_path: Path) -> None:
    runner, station = _container(tmp_path)
    with pytest.raises(smoke.SmokeError, match="1 broken Test Definitions"):
        smoke.run_smoke("deb", _artifacts(), runner, station, _broken_release(tmp_path))


def test_a_scan_with_a_checksum_mismatch_fails(tmp_path: Path) -> None:
    runner, station = _container(tmp_path)
    with pytest.raises(smoke.SmokeError, match="1 mismatched"):
        smoke.run_smoke("deb", _artifacts(), runner, station, _tampered_release(tmp_path))


def test_purge_that_keeps_the_config_fails(tmp_path: Path) -> None:
    runner, station = _container(tmp_path, purge_removes_config=False)
    with pytest.raises(smoke.SmokeError, match="apt purge left"):
        smoke.run_smoke("deb", _artifacts(), runner, station, EXAMPLE)
    assert station.config.is_file(), "the fake left the config behind, which is what the check must find"


def test_purge_that_removes_the_database_fails(tmp_path: Path) -> None:
    runner, station = _container(tmp_path, purge_keeps_records=False)
    with pytest.raises(smoke.SmokeError, match="purge removed the database"):
        smoke.run_smoke("deb", _artifacts(), runner, station, EXAMPLE)
    assert not station.data_dir.exists(), "the records went, and the run said so"


def test_remove_that_deletes_the_config_fails(tmp_path: Path) -> None:
    runner, station = _container(tmp_path, remove_keeps_config=False)
    with pytest.raises(smoke.SmokeError, match="apt remove deleted"):
        smoke.run_smoke("deb", _artifacts(), runner, station, EXAMPLE)


def test_an_upgrade_that_wipes_the_data_fails(tmp_path: Path) -> None:
    runner, station = _container(tmp_path, upgrade_keeps_data=False)
    with pytest.raises(smoke.SmokeError, match="the upgrade deleted"):
        smoke.run_smoke("tar", _artifacts(), runner, station, EXAMPLE)


def test_an_upgrade_that_keeps_the_previous_internal_fails(tmp_path: Path) -> None:
    runner, station = _container(tmp_path, upgrade_clears_internal=False)
    with pytest.raises(smoke.SmokeError, match="the upgrade left"):
        smoke.run_smoke("tar", _artifacts(), runner, station, EXAMPLE)


def test_data_lost_on_uninstall_fails(tmp_path: Path) -> None:
    runner, station = _container(tmp_path, uninstall_keeps_records=False)
    with pytest.raises(smoke.SmokeError, match="uninstall deleted the database"):
        smoke.run_smoke("tar", _artifacts(), runner, station, EXAMPLE)
    assert not station.bin.exists(), "the program itself is gone, as uninstall promises"


def test_install_sh_failing_is_reported(tmp_path: Path) -> None:
    runner, station = _container(tmp_path, install_exit=1)
    with pytest.raises(smoke.SmokeError, match="install.sh failed"):
        smoke.run_smoke("tar", _artifacts(), runner, station, EXAMPLE)


def test_a_depends_entry_debian_does_not_have_fails(tmp_path: Path) -> None:
    """apt resolving the Depends is the point of installing the .deb with apt."""
    runner, station = _container(tmp_path, available=DEBIAN_STABLE - {"libegl1"})
    with pytest.raises(smoke.SmokeError, match="unable to satisfy Depends: libegl1"):
        smoke.run_smoke("deb", _artifacts(), runner, station, EXAMPLE)


def test_a_container_with_an_older_glibc_than_the_build_fails(tmp_path: Path) -> None:
    runner, station = _container(tmp_path, glibc="ldd (Debian GLIBC 2.36-8) 2.36")
    with pytest.raises(smoke.SmokeError, match="linked against glibc 2.39"):
        smoke.run_smoke("deb", _artifacts(), runner, station, EXAMPLE)


def test_the_glibc_check_does_not_apply_to_the_wheel(tmp_path: Path) -> None:
    """The wheel runs on the container's own CPython: nothing it links against came from the build.

    A wheel is not a frozen binary, so a container with an older glibc than the one the bundle was
    built against still runs it: the glibc pin guards the frozen artifacts only.
    """
    runner, station = _container(tmp_path, glibc="ldd (Debian GLIBC 2.36-8) 2.36")
    passed = smoke.run_smoke("wheel", _artifacts(), runner, station, EXAMPLE)
    assert "this container's glibc is not older than the one the bundle was linked against" not in passed, passed
    assert len(passed) == 7, passed  # eight checks for the frozen kinds, minus the glibc one


def test_selftest_run_that_fails_is_reported(tmp_path: Path) -> None:
    """`ate _selftest --run` exiting non-zero stops the run, whatever it printed."""
    runner, station = _container(tmp_path, selftest_exit=1, selftest_output="run end-to-end: MISSING - the PDF has no fonts\n")
    with pytest.raises(smoke.SmokeError, match="ate _selftest --run failed"):
        smoke.run_smoke("deb", _artifacts(), runner, station, EXAMPLE)
    assert any(c[1:] == ["--allow-unsandboxed-pdf", "_selftest", "--run"] for c in runner.calls), runner.calls


def test_the_smoke_passes_the_sandbox_flag_because_the_container_is_root(tmp_path: Path) -> None:
    """Packet 30d: the container runs as root, so a selftest without the flag reports its PDF as
    skipped and exits non-zero. If the smoke script stopped passing the flag, this run would fail
    before it ever printed the selftest line."""
    runner, station = _container(tmp_path)
    smoke.run_smoke("deb", _artifacts(), runner, station, EXAMPLE)
    assert any(c[1:] == ["--allow-unsandboxed-pdf", "_selftest", "--run"] for c in runner.calls), runner.calls


def test_the_pdf_probe_tells_its_root_helper_to_render_unsandboxed() -> None:
    """The probe container is root too, so its bare `ate _pdf` call would refuse (packet 30d)."""
    source = DOCKER_SMOKE.read_text(encoding="utf-8")
    assert "--paper A4 --unsandboxed" in source, "the probe would run a privileged _pdf with no decision"
    assert "apt-get install -y -qq tar poppler-utils" in source


def test_an_ate_that_will_not_start_is_reported(tmp_path: Path) -> None:
    runner, station = _container(tmp_path, version_exit=127)
    with pytest.raises(smoke.SmokeError, match="ate is not runnable"):
        smoke.run_smoke("deb", _artifacts(), runner, station, EXAMPLE)


def test_ate_printing_no_version_is_reported(tmp_path: Path) -> None:
    runner, station = _container(tmp_path, version="")
    with pytest.raises(smoke.SmokeError, match="--version printed nothing"):
        smoke.run_smoke("deb", _artifacts(), runner, station, EXAMPLE)


def test_a_version_that_is_not_the_tag_fails_every_kind(tmp_path: Path) -> None:
    """The tag the release was cut from is the version the artifact must report, for all three kinds."""
    for kind in smoke.KINDS:
        runner, station = _container(tmp_path, version="0.0.0-dev")
        with pytest.raises(smoke.SmokeError, match="was built for '1.2.3'"):
            smoke.run_smoke(kind, _artifacts(), runner, station, EXAMPLE, expect_version="1.2.3")
        assert kind in "deb tar wheel"


def test_the_expected_version_is_checked_when_it_is_given(tmp_path: Path) -> None:
    runner, station = _container(tmp_path, version="1.2.3")
    passed = smoke.run_smoke("deb", _artifacts(), runner, station, EXAMPLE, expect_version="1.2.3")
    assert any("printing the version the tag built" in name for name in passed), passed


def test_purge_that_removes_the_reports_fails(tmp_path: Path) -> None:
    runner, station = _container(tmp_path, purge_keeps_reports=False)
    with pytest.raises(smoke.SmokeError, match="purge removed the reports"):
        smoke.run_smoke("deb", _artifacts(), runner, station, EXAMPLE)
    assert not (station.data_dir / "reports").exists(), "the fake deleted them, and the run said so"


def test_purge_that_removes_the_logs_fails(tmp_path: Path) -> None:
    runner, station = _container(tmp_path, purge_keeps_logs=False)
    with pytest.raises(smoke.SmokeError, match="purge removed the logs"):
        smoke.run_smoke("deb", _artifacts(), runner, station, EXAMPLE)
    assert not (station.data_dir / "logs").exists(), "the fake deleted them, and the run said so"


def test_purge_that_removes_the_database_fails(tmp_path: Path) -> None:
    runner, station = _container(tmp_path, purge_keeps_database=False)
    with pytest.raises(smoke.SmokeError, match="purge removed the database"):
        smoke.run_smoke("deb", _artifacts(), runner, station, EXAMPLE)
    assert not station.data_dir.joinpath("ate.db").exists(), "the records went, and the run said so"


def test_remove_that_leaves_the_program_behind_fails(tmp_path: Path) -> None:
    """`apt remove` must actually remove the bundle and its `/usr/bin/ate` link."""
    runner, station = _container(tmp_path, remove_removes_program=False)
    with pytest.raises(smoke.SmokeError, match="apt remove left .* behind"):
        smoke.run_smoke("deb", _artifacts(), runner, station, EXAMPLE)
    assert (station.app_dir / "ate").exists() and station.bin.exists(), "the program stayed, which is what the check found"


def test_the_tarball_extraction_directory_is_removed(tmp_path: Path, monkeypatch) -> None:
    """The smoke run unpacks the tarball in a temporary directory and takes it away when it is done."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    runner, station = _container(tmp_path)
    smoke.run_smoke("tar", _artifacts(), runner, station, EXAMPLE)
    leftover = [p for p in tmp_path.iterdir() if p.name.startswith("ate-smoke-")]
    assert not leftover, f"the extraction directory was left in {tmp_path}: {leftover}"


# --- fonts: a Debian container has none, and Chromium writes a PDF with nothing in it ------------


def _page_pdf(content: bytes, font: bytes = b"") -> bytes:
    """A one-page PDF with `content` as its content stream, and `font` appended as an extra object."""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R >> endobj\n"
        b"4 0 obj << /Length " + str(len(content)).encode() + b" >> stream\n" + content + b"\nendstream endobj\n"
        + font
        + b"trailer << /Root 1 0 R >>\n"
    )


def _flate_page_pdf(text_operators: bytes) -> bytes:
    """The same one-page PDF with the content stream compressed, and no font object at all."""
    raw = zlib.compress(text_operators)
    return _page_pdf(b"", b"") if not raw else (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R >> endobj\n"
        b"4 0 obj << /Length " + str(len(raw)).encode() + b" /Filter /FlateDecode >> stream\n" + raw + b"\nendstream endobj\n"
        b"trailer << /Root 1 0 R >>\n"
    )


# What QtWebEngine writes when the system has no font: a page with an empty content stream. The
# bytes below are that shape, hand-built so the check can be tested without a container.
BLANK_PDF = _page_pdf(b"")
BLANK_PDF_WITH_GRAPHICS = _page_pdf(b"q 1 0 0 1 20 700 cm 100 20 re f Q")
TEXT_PDF = _page_pdf(b"BT /F1 12 Tf 20 700 Td (ATE Test Report) Tj ET", b"6 0 obj << /Type /Font /BaseFont /Helvetica /FontDescriptor 7 0 R >> endobj\n7 0 obj << /Type /FontDescriptor /FontFile 8 0 R >> endobj\n")
COMPRESSED_TEXT_PDF = _flate_page_pdf(b"BT /F1 12 Tf (ATE Test Report) Tj ET")
COMPRESSED_BLANK_PDF = _flate_page_pdf(b"q 1 0 0 1 0 0 cm 5 5 re f Q")
NOT_A_PDF = b"PK\x03\x04 this is a zip, not a PDF"


def test_pdf_has_text_tells_a_blank_page_from_a_rendered_report() -> None:
    assert pdf_has_text(TEXT_PDF), "a PDF with an embedded font program is text"
    assert pdf_has_text(COMPRESSED_TEXT_PDF), "text in a compressed content stream is still text"
    assert not pdf_has_text(BLANK_PDF), "an empty content stream is not a report"
    assert not pdf_has_text(BLANK_PDF_WITH_GRAPHICS), "a drawn rectangle is not a report"
    assert not pdf_has_text(COMPRESSED_BLANK_PDF), "a compressed empty page is not a report either"
    assert not pdf_has_text(NOT_A_PDF), "not a PDF at all"


@pytest.mark.skipif(not WEBENGINE_AVAILABLE, reason="PyQt6-WebEngine not installed")
def test_pdf_has_text_accepts_a_real_webengine_report(tmp_path: Path) -> None:
    """The real thing QtWebEngine prints passes the check, so it never fails a good build."""
    html = tmp_path / "report.html"
    html.write_text("<html><body><h1>Test Report</h1><p>1. Check the fuse: PASS</p></body></html>", encoding="utf-8", newline="\n")
    pdf = tmp_path / "report.pdf"
    run_pdf_helper(html, pdf, "A4")
    data = pdf.read_bytes()
    assert data.startswith(b"%PDF") and pdf_has_text(data), f"the check rejected a real report PDF ({len(data)} bytes)"


def test_the_selftest_refuses_a_pdf_with_no_fonts() -> None:
    """The check the release CI depends on: a valid PDF with nothing in it is a failure."""
    assert _report_problem(b"<html><body>Report</body></html>", TEXT_PDF) is None, "a real report must pass"
    problem = _report_problem(b"<html><body>Report</body></html>", BLANK_PDF)
    assert problem and "no fonts" in problem and "fontconfig" in problem, problem
    assert _report_problem(b"<html>", COMPRESSED_BLANK_PDF) is not None, "a compressed empty page is caught too"
    assert _report_problem(b"", TEXT_PDF) == "the HTML report is not HTML"
    assert _report_problem(b"<html>", NOT_A_PDF) == "the file the _pdf helper wrote is not a PDF"


def test_the_selftest_run_reports_a_fontless_pdf_as_a_failure(capsys, monkeypatch) -> None:
    """The check is wired into the run, not just sitting next to it.

    `ate _selftest --run` is what the release CI's smoke step runs, so the PDF check has to be on the
    path that decides its exit code. Here the `--run` goes through the real seams with a helper that
    writes exactly what a fontless container writes: a valid PDF whose page is empty. The run must
    exit non-zero and say why.
    """
    from ate.cli import main
    from tests._fixtures import fake_pdf_helper

    monkeypatch.setattr("ate.report.pdf_job.pdf_helper_command", fake_pdf_helper(f"import sys; open(sys.argv[1], 'wb').write({BLANK_PDF!r})"))
    code = main(["_selftest", "--run"])
    out = capsys.readouterr().out
    assert code != 0, f"the selftest accepted a PDF with nothing in it: {out}"
    assert "no fonts" in out, out
    assert "fontconfig" in out, out


def test_the_package_and_the_tarball_install_a_font() -> None:
    """A stock Debian image ships no fonts: without these Depends the report PDF is empty."""
    assert {"fontconfig", "fonts-dejavu-core"} <= set(packaging.DEPENDS), packaging.DEPENDS


def test_a_deb_that_does_not_depend_on_a_font_installs_into_a_fontless_container(tmp_path: Path) -> None:
    """If the Depends stop naming a font, apt is content and the run installs a bundle that writes a blank PDF."""
    runner, station = _container(tmp_path, available=DEBIAN_STABLE - {"fontconfig", "fonts-dejavu-core"})
    with pytest.raises(smoke.SmokeError, match="unable to satisfy Depends: fontconfig"):
        smoke.run_smoke("deb", _artifacts(), runner, station, EXAMPLE)


def test_the_tarball_installs_the_font_too(tmp_path: Path) -> None:
    runner, station = _container(tmp_path)
    smoke.install_tar(runner, station, smoke.find_artifact(_artifacts(), "tar"))
    installed = next(c for c in runner.calls if c[:2] == ["apt-get", "install"] and not c[2].endswith(".deb"))
    assert {"fontconfig", "fonts-dejavu-core"} <= set(installed[3:]), installed


def test_runtime_libs_are_read_from_the_packaging_script():
    assert smoke.runtime_libs() == packaging.DEPENDS, "the tarball installs the .deb's Depends, not a copy of them"


def test_wheel_libs_are_read_from_the_packaging_script():
    assert smoke.wheel_libs() == packaging.WHEEL_SYSTEM_LIBS, "the wheel installs the list in build_linux.py, not a copy"
    assert packaging.WHEEL_SYSTEM_LIBS, "the wheel needs its own system library list"
    assert "libfontconfig1" in packaging.WHEEL_SYSTEM_LIBS, "PyPI's QtWebEngine renders text through the system fontconfig"


def test_the_wheel_needs_libraries_the_deb_does_not() -> None:
    """PyPI's PyQt6 is linked against more than the bundle's own Qt, so the two lists differ."""
    only_wheel = set(packaging.WHEEL_SYSTEM_LIBS) - set(packaging.DEPENDS)
    assert only_wheel, "the wheel uses the container's Python, not the bundle"
    assert {"libglib2.0-0t64", "libdbus-1-3", "libxkbcommon0"} <= set(packaging.WHEEL_SYSTEM_LIBS)
    # libQt6WebEngineCore.so.6 is linked against libnspr4.so, libnss3.so, libnssutil3.so and
    # libsmime3.so, and libQt6Network.so.6 against libgssapi_krb5.so.2, so without them QtWebEngine
    # does not even import (the v0.1.0-rc1 wheel smoke).
    assert {"libnss3", "libnspr4", "libgssapi-krb5-2"} <= set(packaging.WHEEL_SYSTEM_LIBS), "the dynamic linker loads them on every import"


def test_docker_smoke_runs_the_smoke_script_in_a_container_like_the_release_job() -> None:
    """poppler-utils pulls in NSS, NSPR and GSSAPI, which is how the container hid that gap.

    A relative --dist as well, because the absolute one hid the `apt-get` path bug.
    """
    source = DOCKER_SMOKE.read_text(encoding="utf-8")
    assert "apt-get install -y -qq --no-install-recommends python3 tar\"" in source
    assert "python3 tar poppler-utils" not in source
    assert "--dist artifacts" in source and "--dist /work/artifacts" not in source


def test_ate_reports_the_version_baked_into_the_wheel(tmp_path: Path) -> None:
    """An installed wheel knows the tag it was built from: it is in the wheel's own metadata.

    `git describe` finds nothing beside an installed wheel, so without the metadata lookup a
    released wheel prints `0.0.0-dev` while its METADATA says the tag's version.
    """
    site = tmp_path / "site"
    (site / "ate-1.2.3.dist-info").mkdir(parents=True)
    (site / "ate-1.2.3.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: ate\nVersion: 1.2.3\n", encoding="utf-8", newline="\n")
    sys.path.insert(0, str(site))
    try:
        assert version_module._metadata_version() == "1.2.3", "the version is read from the installed distribution"
        assert version_module._compute_version() == "1.2.3", "the metadata beats git describe"
    finally:
        sys.path.remove(str(site))


def test_the_version_of_a_plain_source_checkout_still_comes_from_git(monkeypatch) -> None:
    """Nothing installed, a tag reachable: the checkout's tag is the version.

    The metadata lookup must not replace `git describe` for a source run, and the git branch must
    not be dropped either: with the metadata missing and the tag reachable, the version is the tag.
    """
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda name: (_ for _ in ()).throw(importlib.metadata.PackageNotFoundError(name)),
    )
    monkeypatch.setattr(version_module, "_describe", lambda: "v1.2.3-4-gabc1234")
    assert version_module._compute_version() == "1.2.3.dev4+gabc1234", "the version still comes from the tag"
    monkeypatch.setattr(version_module, "_describe", lambda: None)
    assert version_module._compute_version() == version_module.FALLBACK, "no git and nothing installed is a dev version"


def test_the_glibc_marker_is_required(tmp_path: Path) -> None:
    runner, station = _container(tmp_path)
    dist = tmp_path / "artifacts"
    dist.mkdir()
    shutil.copyfile(next(p for p in _artifacts().iterdir() if p.suffix == ".deb"), dist / "ate_1.2.3_amd64.deb")
    with pytest.raises(smoke.SmokeError, match="glibc-build.txt is not in the artifacts"):
        smoke.run_smoke("deb", dist, runner, station, EXAMPLE)


def test_the_smoke_script_only_shells_out_through_the_runner(tmp_path: Path) -> None:
    """Every command the script runs goes through the injected `Runner`, so the tests can replace it."""
    text = SMOKE.read_text(encoding="utf-8")
    assert text.count("subprocess.run") == 1, "only SubprocessRunner may spawn a process"
    for call in ("groupadd", "chown", "sudo", "dpkg -i"):
        assert call not in text, f"{call} appears outside the Runner's argument lists"


def test_station_paths_are_the_paths_packaging_advertises():
    station = smoke.Station()
    assert station.app_dir == Path("/opt/ate")
    assert station.bin == Path("/usr/bin/ate")
    assert station.config == Path("/etc/ate/config.json")
    assert station.data_dir == Path("/var/lib/ate")
    rooted = smoke.Station(root=Path("/tmp/station"))
    assert rooted.config == Path("/tmp/station/etc/ate/config.json")


def test_run_smoke_rejects_an_unknown_kind(tmp_path: Path) -> None:
    runner, station = _container(tmp_path)
    with pytest.raises(smoke.SmokeError, match="unknown --kind"):
        smoke.run_smoke("exe", _artifacts(), runner, station, EXAMPLE)


# --- the containers themselves: `ATE_DOCKER_TESTS=1` runs the real thing -------------------------


def _passing_log() -> str:
    """What a run that went well prints: the three logs and the closing line."""
    return "".join(
        f"{kind} smoke: 8 checks passed\n"
        "run end-to-end: ok - ate ran the schedule: /tmp/r.html (8290 bytes HTML), /tmp/r.pdf (65792 bytes PDF, with text)\n"
        "=== pdftotext probe (fresh container, same artifact) ===\nprobe pdf 11908 bytes\nText must survive the render.\n"
        for kind in ("deb", "tar", "wheel")
    )


def test_the_harness_check_passes_a_good_run():
    assert harness.check_run("".join(f"--- {kind} container log ---\n{report}\n" for kind, report in zip(harness.KINDS, [_passing_log()] * 3))) == []


def test_the_harness_check_catches_a_blank_report_pdf():
    """The whole point of the container run is the PDF having text in it, so dropping that line fails."""
    report = _passing_log().replace("bytes PDF, with text", "bytes PDF").replace("Text must survive the render.", "")
    problems = harness.check_run("".join(f"--- {kind} container log ---\n{report}\n" for kind in harness.KINDS))
    assert len(problems) == 6, problems  # three kinds x (no selftest text, no pdftotext text)


def test_the_harness_check_catches_a_missing_kind():
    reports = dict(zip(harness.KINDS, [_passing_log()] * 3))
    reports["wheel"] = "wheel container died before the smoke script started"
    problems = harness.check_run("".join(f"--- {kind} container log ---\n{reports[kind]}\n" for kind in harness.KINDS))
    assert any("wheel" in problem for problem in problems), problems


def test_the_harness_check_catches_a_failed_smoke():
    report = _passing_log().replace("deb smoke: 8 checks passed", "deb smoke failed: SMOKE FAILED: apt-get could not resolve Depends")
    problems = harness.check_run("".join(f"--- {kind} container log ---\n{report}\n" for kind in harness.KINDS))
    assert any("deb" in problem and "failure" in problem for problem in problems), problems


def test_a_run_with_a_problem_never_prints_the_success_line(capsys):
    """`finish` is what prints the closing line, and it refuses a run that did not prove anything."""
    report = _passing_log().replace("bytes PDF, with text", "bytes PDF")
    assert harness.finish(list(harness.KINDS), [report, report, report]) == 1
    out = capsys.readouterr()
    assert "docker smoke: all kinds ran in debian:stable" not in out.out
    assert "DOCKER SMOKE INCOMPLETE" in out.err


def test_a_clean_run_prints_the_success_line(capsys):
    assert harness.finish(list(harness.KINDS), [_passing_log()] * 3) == 0
    assert "docker smoke: all kinds ran in debian:stable" in capsys.readouterr().out


def test_the_container_test_is_gated_on_the_flag():
    """The real container run is opt-in: without the guard it would run in every CI job."""
    source = (REPO / "tests" / "test_release_ci_28c.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    body = next((node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "test_the_three_artifacts_smoke_in_real_debian_containers"), None)
    assert body is not None, "the container test is gone"
    lines = ast.get_source_segment(source, body) or ""
    assert 'os.environ.get("ATE_DOCKER_TESTS") != "1"' in lines, "the container test runs whenever Docker is around"
    assert "pytest.skip" in lines, "the guard must skip, not silently pass"
    marks = {decorator.attr for decorator in body.decorator_list if isinstance(decorator, ast.Attribute)}
    assert {"build", "slow"} <= marks, f"the container test is marked {marks}"


@pytest.mark.build
@pytest.mark.slow
def test_the_three_artifacts_smoke_in_real_debian_containers(tmp_path: Path) -> None:
    """Build the artifacts and run the smoke script in three fresh `debian:stable` containers.

    Skipped by default: it needs Docker, the image and network, and takes several minutes. With
    `ATE_DOCKER_TESTS=1` it is the real proof that `apt-get install ./ate_*.deb`, `install.sh` and
    `pipx install` work on Debian stable, and that the report PDF the bundle writes has text in it.
    """
    if os.environ.get("ATE_DOCKER_TESTS") != "1":
        pytest.skip("the Debian containers take minutes; set ATE_DOCKER_TESTS=1 to run them")
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed: the containers cannot start")

    out = tmp_path / "docker"
    proc = subprocess.run(
        [sys.executable, str(DOCKER_SMOKE), "--out", str(out)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    assert proc.returncode == 0, f"the container run failed:\n{proc.stdout[-2500:]}\n{proc.stderr[-2500:]}"
    problems = harness.check_run(proc.stdout)
    assert not problems, f"the container run did not prove what it claims: {problems}\n{proc.stdout[-1500:]}"
