"""Packet 28c: the Linux release CI.

Two things are checked here, and neither is the thing that ultimately matters:

* `.github/workflows/release.yml` — parsed with PyYAML and checked for the jobs, what they need,
  the container they run in, the artifact names that go up and come back down, and the fact that
  every script the workflow names is a script that exists. **No job has ever run**: no tag of this
  repository has been cut, and no Debian container has ever started on these machines.
* `packaging/linux/smoke_linux.py` — the script that runs inside `debian:stable`. Its install,
  upgrade, remove, purge and uninstall steps run against a fake container rooted in a temporary
  directory: `apt-get` and `dpkg` are modelled, `install.sh` is run for real with its documented
  `--prefix`/`--bin` seam, and every `ate` call is the real source CLI against the container's own
  config and database. So these tests prove the smoke script *notices* a broken install — not that
  a real Debian install works.

Nothing here executes `apt`, `dpkg -i`, `sudo`, `groupadd` or `chown` on this machine, and nothing
is published anywhere: the artifacts only ever go to `actions/upload-artifact`.

| Seam | What it does |
| --- | --- |
| `smoke.Station(root=...)` | Roots `/opt/ate`, `/usr/bin/ate`, `/etc/ate` and `/var/lib/ate` in a directory |
| `smoke.Runner` | Every command the script runs on the machine; the fake replaces it |
| `ATE_STUB_SKIPS_SETUP` | Read only by the test stub bundle: `ate setup` succeeds and does nothing |
"""

from __future__ import annotations

import fnmatch
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "release.yml"
SMOKE = REPO / "packaging" / "linux" / "smoke_linux.py"
BUILD_LINUX = REPO / "packaging" / "linux" / "build_linux.py"
INSTALL_SH = REPO / "packaging" / "linux" / "install.sh"
POSTINST = REPO / "packaging" / "linux" / "deb" / "postinst"
POSTRM = REPO / "packaging" / "linux" / "deb" / "postrm"
EXAMPLE = REPO / "examples" / "media-release"
SRC = REPO / "src"
UPLOAD_NAME = "ate-linux-artifacts"


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
    runs = [step["run"] for step in job["steps"] if step.get("run", "").strip().startswith("python3")]
    assert len(runs) == 1, runs
    assert "--kind ${{ matrix.kind }}" in runs[0] and "--dist artifacts" in runs[0], runs[0]


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
    for run in _runs(_workflow()):
        assert "sudo" not in run, "the container is already root: nothing here needs sudo"
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
    """A bundle shaped like `dist/ate`: the executable plus an `_internal` directory."""
    bundle = root / "dist" / "ate"
    (bundle / "_internal" / "PyQt6" / "Qt6" / "lib").mkdir(parents=True)
    exe = bundle / "ate"
    exe.write_text(_stub_ate(), encoding="utf-8")
    exe.chmod(0o755)
    (bundle / "_internal" / "base_library.zip").write_text("not really a zip", encoding="utf-8")
    (bundle / "_internal" / "PyQt6" / "Qt6" / "lib" / "libQt6Core.so.6").write_text("stub", encoding="utf-8")
    return bundle


_CACHE: dict[str, Path] = {}


def _artifacts() -> Path:
    """One set of artifacts (tarball, .deb, wheel, glibc marker) built once, from a stub bundle."""
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
        (dist / "glibc-build.txt").write_text("ldd (Ubuntu GLIBC 2.39-0ubuntu2) 2.39\n", encoding="utf-8")
        _CACHE["dist"] = dist
    return _CACHE["dist"]


DEBIAN_STABLE = {
    "python3",
    "python3-venv",
    "pipx",
    "tar",
    "dpkg",
    *packaging.DEPENDS,
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
        postinst_runs_setup: bool = True,
        schema: int | None = None,
        remove_keeps_config: bool = True,
        purge_removes_config: bool = True,
        purge_keeps_records: bool = True,
        upgrade_keeps_data: bool = True,
        upgrade_clears_internal: bool = True,
        uninstall_keeps_records: bool = True,
        install_exit: int = 0,
    ) -> None:
        self.root = Path(root)
        self.station = station
        self.glibc = glibc
        self.available = set(available) if available is not None else set(DEBIAN_STABLE)
        self.postinst_runs_setup = postinst_runs_setup
        self.schema = schema
        self.remove_keeps_config = remove_keeps_config
        self.purge_removes_config = purge_removes_config
        self.purge_keeps_records = purge_keeps_records
        self.upgrade_keeps_data = upgrade_keeps_data
        self.upgrade_clears_internal = upgrade_clears_internal
        self.uninstall_keeps_records = uninstall_keeps_records
        self.install_exit = install_exit
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
        copy.write_text(text, encoding="utf-8")
        copy.chmod(0o755)
        return self._subprocess(["/bin/sh", str(copy), action])

    def _remove_package(self, *, purge: bool) -> None:
        shutil.rmtree(self.station.app_dir, ignore_errors=True)
        if self.station.bin.exists() or self.station.bin.is_symlink():
            self.station.bin.unlink()
        result = self._maintainer("postrm", "purge" if purge else "remove")
        if result.returncode != 0:
            return
        if purge:
            if not self.purge_removes_config:
                self.station.config_dir.mkdir(parents=True, exist_ok=True)
                self.station.config.write_text('{"format": 1}', encoding="utf-8")
            if not self.purge_keeps_records:
                shutil.rmtree(self.station.data_dir, ignore_errors=True)
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
                stale.write_text("a file the previous build shipped", encoding="utf-8")
        return result

    def _pipx(self, args: list[str]) -> smoke.CommandResult:
        wheel = Path(args[-1])
        if args[:1] != ["install"] or not wheel.is_file():
            return smoke.CommandResult(1, "", f"pipx: {wheel} is not a wheel")
        self.station.wheel_bin.parent.mkdir(parents=True, exist_ok=True)
        self.station.wheel_bin.write_text(_stub_ate(), encoding="utf-8")
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
        if args[:2] == ["_selftest", "--run"]:
            # The frozen bundle's end-to-end run needs QtWebEngine; the CI runs it, the fake says
            # what 27a's smoke prints when it passes.
            return smoke.CommandResult(0, "run end-to-end: ok - _pdf wrote the report\n", "")
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
    (release / "oops.tslua").write_text("return ScheduleParams('broken', 'no params')\n", encoding="utf-8")
    (release / "broken.atedef").write_text(
        json.dumps({"format": 1, "mediaNumber": "MR-BROKEN", "name": "Broken", "version": "not semver", "files": []}),
        encoding="utf-8",
    )
    return release


def _tampered_release(tmp_path: Path) -> Path:
    """A copy of the shipped example with one schedule edited: its checksum no longer matches."""
    release = tmp_path / "tampered"
    shutil.copytree(EXAMPLE, release)
    (release / "schedule_basics.tslua").write_text(
        (release / "schedule_basics.tslua").read_text(encoding="utf-8") + "\n-- touched by the smoke test\n",
        encoding="utf-8",
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
    assert set(packaging.DEPENDS) <= set(installed[2:]), "PyQt6 from PyPI needs the GL/X libraries too"


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
    runner, station = _container(tmp_path, available=DEBIAN_STABLE - {"libxcb-cursor0"})
    with pytest.raises(smoke.SmokeError, match="unable to satisfy Depends: libxcb-cursor0"):
        smoke.run_smoke("deb", _artifacts(), runner, station, EXAMPLE)


def test_a_container_with_an_older_glibc_than_the_build_fails(tmp_path: Path) -> None:
    runner, station = _container(tmp_path, glibc="ldd (Debian GLIBC 2.36-8) 2.36")
    with pytest.raises(smoke.SmokeError, match="linked against glibc 2.39"):
        smoke.run_smoke("deb", _artifacts(), runner, station, EXAMPLE)


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


def test_runtime_libs_are_read_from_the_packaging_script():
    assert smoke.runtime_libs() == packaging.DEPENDS, "the tarball and wheel install the .deb's Depends, not a copy of them"


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
