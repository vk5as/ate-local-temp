"""Packet 36, part 1: the manuals job, the pinned TeX image, and the GitHub Release job.

Nothing here runs TeX, Docker or `gh`. Two files are checked, in the way 28c checks the Linux
jobs:

* `.github/workflows/release.yml` — parsed with PyYAML. Each rule is a function over the parsed
  document, and each function is shown to catch a broken workflow: the test runs it on the
  workflow as written, then on a copy of it with that one thing wrong.
* `packaging/release_assets.py` — the ten files a Release carries, assembled from fake artifact
  directories in a temporary directory, with hand-built PDFs whose Info dictionary is readable
  without a viewer. These prove the publish step *refuses* an incomplete release, not that a real
  release works: that needs a tag pushed, which only the orchestrator and the user can do.

The manuals are one list, `MANUAL_FILES` in `docs/manuals/build.py`. The upload globs, the title
page check, the publish job's count and these tests all read that list, so a manual added to the
TeX sources but not to the list fails the build instead of shipping a release one manual short.
"""

from __future__ import annotations

import copy
import fnmatch
import importlib.util
import re
import shutil
import subprocess
from tests import _fixtures
from pathlib import Path
from typing import Callable

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "release.yml"
MANUALS_BUILD = REPO / "docs" / "manuals" / "build.py"
RELEASE_ASSETS = REPO / "packaging" / "release_assets.py"
BUILD_INSTALLER = REPO / "packaging" / "windows" / "build_installer.py"
BUILD_LINUX = REPO / "packaging" / "linux" / "build_linux.py"

MANUAL_ARTIFACT = "ate-manuals"
PACKAGE_JOBS = ("windows-installer", "linux-build")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = _load("manuals_build_for_36", MANUALS_BUILD)
assets = _load("release_assets_for_36", RELEASE_ASSETS)
linux = _load("build_linux_for_36", BUILD_LINUX)

#: The ten names a Release carries, as the packaging scripts write them: the publish glob has to
#: match every one of them, not just the PDFs.
RELEASE_ASSET_NAMES = tuple(build.MANUAL_FILES) + (
    "ate-0.1.0rc2-windows-x64-setup.exe",
    "ate_0.1.0~rc2_amd64.deb",
    "ate-0.1.0rc2-linux-x86_64.tar.gz",
    "ate-0.1.0rc2-py3-none-any.whl",
)


def _workflow() -> dict:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), "release.yml is not a mapping"
    return doc


def _broken(doc: dict, job: str | None, change: Callable[[dict], None]) -> dict:
    """The workflow with one thing wrong, so a check can be shown to notice it."""
    copy_of = copy.deepcopy(doc)
    change(copy_of["jobs"][job] if job else copy_of)
    return copy_of


def _runs(doc: dict, job: str) -> list[str]:
    return [step["run"] for step in doc["jobs"][job]["steps"] if step.get("run")]


def _checkout(doc: dict, job: str) -> dict:
    """The `with:` of the job's checkout step."""
    return next(s["with"] for s in doc["jobs"][job]["steps"] if "actions/checkout" in str(s.get("uses", "")))


def _upload_index(job: dict) -> int:
    return next(i for i, step in enumerate(job["steps"]) if "upload-artifact" in str(step.get("uses", "")))


def _guard_index(job: dict) -> int:
    """The step just before the manuals upload: the one that counts the files."""
    return _upload_index(job) - 1


def check_full_history(doc: dict) -> None:
    """Every job whose version comes from the tag checks out the whole history.

    `linux-smoke` is the exception on purpose: its container has no git and the smoke script there
    reads no history.
    """
    for job in ("manuals", "windows-installer", "linux-build", "release"):
        depth = _checkout(doc, job).get("fetch-depth")
        if depth != 0:
            raise AssertionError(f"{job} checks out at fetch-depth {depth}: a shallow clone has no tag, so the version is 0.0.0-dev and the PDF stamps cannot be checked")


def _gh_lines(run: str) -> list[str]:
    """The run step's `gh` commands, continuations joined, comments removed."""
    logical, buffer = [], ""
    for line in run.splitlines():
        if line.lstrip().startswith("#"):
            line = ""
        else:
            line = line.split(" #")[0].rstrip()
        if line.endswith("\\"):
            buffer += line[:-1] + " "
        else:
            logical.append(buffer + line)
            buffer = ""
    if buffer:
        logical.append(buffer)
    return [line for line in logical if re.search(r"\bgh\s+\w", line)]


def _uses(doc: dict, job: str, needle: str) -> list[dict]:
    return [
        step["with"]
        for step in doc["jobs"][job]["steps"]
        if needle in str(step.get("uses", ""))
    ]


# --- the one list of manuals -------------------------------------------------------------------


def check_manual_list(doc: dict) -> None:
    """The six release files come from build.py, and the upload covers exactly those six."""
    if build.MANUAL_FILES != build.MANUAL_PDFS + build.MANUAL_MARKDOWN:
        raise AssertionError("the six release files are not the three PDFs plus the three Markdown manuals")
    for name in build.MANUAL_NAMES:
        if not (REPO / "docs" / "manuals" / name / "main.tex").is_file():
            raise AssertionError(f"{name} is in the list but has no main.tex to build")
        if not (REPO / "docs" / "manuals" / "llm" / f"{name}.md").is_file():
            raise AssertionError(f"{name} is in the list but docs/manuals/llm/{name}.md does not exist")

    upload = next((u for u in _uses(doc, "manuals", "upload-artifact") if u.get("name") == MANUAL_ARTIFACT), None)
    if upload is None:
        raise AssertionError(f"nothing is uploaded as {MANUAL_ARTIFACT}: the package jobs have no PDFs to take")
    globs = [line.strip() for line in str(upload["path"]).splitlines() if line.strip()]
    for name in build.MANUAL_FILES:
        if not any(fnmatch.fnmatch(name, glob.rsplit("/", 1)[-1]) for glob in globs):
            raise AssertionError(f"{name} is in build.py's list but no upload glob matches it: {globs}")
    if upload.get("if-no-files-found") != "error":
        raise AssertionError("an upload that matched nothing must fail, not warn: two manuals shipped instead of three is silent")
    steps = doc["jobs"]["manuals"]["steps"]
    position = next(i for i, s in enumerate(steps) if "upload-artifact" in str(s.get("uses", "")))
    counted = next((str(s.get("run", "")) for s in steps[:position] if "upload" not in str(s.get("uses", "")) and "-f " in str(s.get("run", "")) and "build.py --list" in str(s.get("run", "")) and "src/ate/manuals" in str(s.get("run", ""))), None)
    if counted is None:
        raise AssertionError("nothing counts the six files before the upload: if-no-files-found only fires when a glob matches nothing, so one missing PDF ships silently")
    if "exit 1" not in counted:
        raise AssertionError("the count of release files prints a warning instead of failing the job")
    if "retention-days" in upload:
        raise AssertionError("the manuals artifact carries a retention unlike every other artifact in this workflow")


def test_the_six_manuals_come_from_one_list_in_build_py():
    check_manual_list(_workflow())
    assert build.MANUAL_FILES == (
        "test-writer.pdf", "admin.pdf", "developer.pdf", "test-writer.md", "admin.md", "developer.md"
    )
    assert build.manuals_left_out() == [], "manual directories the release list does not name"


def test_a_manual_the_upload_does_not_glob_is_caught():
    doc = _broken(_workflow(), "manuals", lambda job: job["steps"].pop(_upload_index(job)))
    doc["jobs"]["manuals"]["steps"].append(
        {"name": "Upload the manuals", "uses": "actions/upload-artifact@v4", "with": {"name": MANUAL_ARTIFACT, "path": "src/ate/manuals/*.pdf"}}
    )
    with pytest.raises(AssertionError, match="no upload glob matches"):
        check_manual_list(doc)


def test_the_upload_that_does_not_count_the_files_is_caught():
    doc = _broken(_workflow(), "manuals", lambda job: job["steps"].pop(_guard_index(job)))
    with pytest.raises(AssertionError, match="nothing counts the six files"):
        check_manual_list(doc)
    doc = _broken(_workflow(), "manuals", lambda job: job["steps"][_guard_index(job)].update({"run": "echo all six are there"}))
    with pytest.raises(AssertionError, match="nothing counts the six files"):
        check_manual_list(doc)


def test_a_manual_directory_not_in_the_list_is_reported():
    """build.py fails the build rather than quietly leaving a manual out of the release."""
    with _fixtures.temp_directory() as tmp:
        fake = Path(tmp)
        (fake / "test-writer").mkdir()
        (fake / "test-writer" / "main.tex").write_text("\\chapter{x}", encoding="utf-8", newline="\n")
        (fake / "operator").mkdir()
        (fake / "operator" / "main.tex").write_text("\\chapter{y}", encoding="utf-8", newline="\n")
        original = build.MANUALS_DIR
        build.MANUALS_DIR = fake
        try:
            left_out = build.manuals_left_out()
        finally:
            build.MANUALS_DIR = original
    assert left_out == ["operator"], f"a manual nobody added to the list must be named, got {left_out}"


# --- the manuals job ---------------------------------------------------------------------------


def check_manuals_job(doc: dict) -> None:
    """The manuals are built first, on Linux, from the image build.py pins, and staged for packaging."""
    check_full_history(doc)
    jobs = doc["jobs"]
    if "manuals" not in jobs:
        raise AssertionError("release.yml has no manuals job: the PDFs are built by nobody")
    if list(jobs)[0] != "manuals":
        raise AssertionError(f"the manuals job must come first; it is after {list(jobs)[0]}")
    job = jobs["manuals"]
    if "ubuntu" not in str(job.get("runs-on", "")):
        raise AssertionError("the manuals job needs a Linux runner: TeX Live is pulled as a Docker image")
    if not job.get("timeout-minutes"):
        raise AssertionError("a 9 GB pull can hang: the manuals job needs a timeout")

    runs = _runs(doc, "manuals")
    pull = next((r for r in runs if 'docker pull "$image"' in r and "build.py --image" in r), None)
    if pull is None:
        raise AssertionError("the job must pull the image build.py names, not guess which TeX the runner has")
    if pull.index("build.py --image") > pull.index("docker pull"):
        raise AssertionError("the image name is read after it is pulled")
    built = next((r for r in runs if any(line.strip() == "python docs/manuals/build.py" for line in r.splitlines())), None)
    if built is None:
        raise AssertionError("the manuals job never runs docs/manuals/build.py")
    if runs.index(pull) > runs.index(built):
        raise AssertionError("the image is pulled after the manuals are built")
    if not any("df -h" in r and "avail" in r for r in runs):
        raise AssertionError("the job must check the runner has room for the image before pulling it")
    room = next(r for r in runs if "df -h" in r and "avail" in r)
    floor = re.search(r"avail\"?\s*-lt\s*(\d+)", room)
    stated = re.search(r"image is (\d+(?:\.\d+)?) GB", room)
    if not floor or not stated:
        raise AssertionError("the disk check compares no byte floor with the size of the image it is making room for")
    floor_bytes, image_bytes = int(floor.group(1)), float(stated.group(1)) * 1_000_000_000
    if floor_bytes < image_bytes:
        raise AssertionError(f"the floor is {floor_bytes / 1e9:.1f} GB, below the {stated.group(1)} GB the pinned image takes: the pull fails either way")
    if floor_bytes > 2 * image_bytes:
        raise AssertionError(f"the floor is {floor_bytes / 1e9:.1f} GB, more than twice the {stated.group(1)} GB needed: runners that could build are refused")
    if not any("pdftotext" in r for r in runs):
        raise AssertionError("no title page is read: the PDFs could carry the last release's version")
    if not any("0.0.0-dev" in r for r in runs):
        raise AssertionError("a v* tag that yields 0.0.0-dev means no tags were fetched: nothing here is releasable")

    for name in PACKAGE_JOBS:
        needs = doc["jobs"][name].get("needs")
        needs = needs if isinstance(needs, list) else [needs]
        if "manuals" not in needs:
            raise AssertionError(f"{name} packages without the manuals job's PDFs")
        download = next((d for d in _uses(doc, name, "download-artifact") if d.get("name") == MANUAL_ARTIFACT), None)
        if download is None:
            raise AssertionError(f"{name} does not download {MANUAL_ARTIFACT}")
        staged = next((r for r in _runs(doc, name) if "--stage" in r), None)
        if staged is None:
            raise AssertionError(f"{name} downloads the manuals but never stages them where the bundle collects them")
        if download.get("path") not in staged:
            raise AssertionError(f"{name} stages {download['path']} which the download does not write to")
        steps = [s.get("run", "") for s in doc["jobs"][name]["steps"]]
        if steps.index(staged) > next(i for i, s in enumerate(steps) if "build_pyinstaller.py" in s):
            raise AssertionError(f"{name} stages the PDFs after PyInstaller ran: the bundle carries none")


def test_the_manuals_job_runs_first_and_every_package_job_needs_it():
    check_manuals_job(_workflow())


def test_dropping_a_needs_line_is_caught():
    doc = _broken(_workflow(), "windows-installer", lambda job: job.pop("needs"))
    with pytest.raises(AssertionError, match="without the manuals job"):
        check_manuals_job(doc)
    doc = _broken(_workflow(), "linux-build", lambda job: job["steps"].pop(3))  # the staging step
    with pytest.raises(AssertionError, match="never stages"):
        check_manuals_job(doc)


def check_title_pages(doc: dict) -> None:
    step = next((r for r in _runs(doc, "manuals") if "pdftotext" in r), None)
    if step is None:
        raise AssertionError("no title page is read: the version on the PDF is nobody's guarantee")
    if "build.py --list" not in step:
        raise AssertionError("the title page check hard-codes the manual names; the list lives in build.py")
    if "_version import __version__" not in step:
        raise AssertionError("the version checked must be the one `ate --version` prints, from src/ate/_version.py")
    if "-f 1 -l 1" not in step:
        raise AssertionError("page 1 is the title page: a version on page 12 is not what the cover says")
    if not re.search(r'grep -Fxq "\$version"', step):
        raise AssertionError('the title page is not compared with the version as a whole: grep -Fq "ATE" passes any PDF TeX ever made')
    if "|-dev" not in step:
        raise AssertionError("the title page version check must accept the exact 0.0.0-dev stamp used by workflow_dispatch")
    if "exit 1" not in step:
        raise AssertionError("a title page without this release's version must fail the job, not print a note")
    checkout = _checkout(doc, "manuals")
    if checkout.get("fetch-depth") != 0:
        raise AssertionError(f"the manuals job checks out at fetch-depth {checkout.get('fetch-depth')}: the version comes from the tag, and a shallow clone has no tag")


def test_every_title_page_is_checked_against_the_version_being_released():
    check_title_pages(_workflow())
    doc = _broken(_workflow(), "manuals", lambda job: job["steps"].append({"name": "extra", "run": "echo fine"}))
    check_title_pages(doc)  # an unrelated extra step changes nothing
    doc = _broken(doc, "manuals", lambda job: job["steps"].pop(4))
    with pytest.raises(AssertionError, match="title page"):
        check_title_pages(doc)
    doc = _broken(_workflow(), "manuals", lambda job: job["steps"][4].update({"run": job["steps"][4]["run"].replace("exit 1", "echo looks wrong")}))
    with pytest.raises(AssertionError, match="must fail the job"):
        check_title_pages(doc)
    doc = _broken(_workflow(), "manuals", lambda job: job["steps"][4].update({"run": job["steps"][4]["run"].replace("|-dev", "")}))
    with pytest.raises(AssertionError, match="0.0.0-dev"):
        check_title_pages(doc)


# --- the pinned TeX image ----------------------------------------------------------------------


def check_tex_pin(doc: dict) -> None:
    """The image is pinned by digest in one place, and the workflow reads that pin."""
    if not DIGEST.match(build.TEXLIVE_DIGEST):
        raise AssertionError(f"the TeX image is not pinned by a 64-character digest: {build.TEXLIVE_DIGEST}")
    if build.IMAGE != f"texlive/texlive@{build.TEXLIVE_DIGEST}":
        raise AssertionError(f"the pinned image is {build.IMAGE}, not texlive/texlive@<digest>")
    text = yaml.dump(doc)
    if "texlive/texlive:latest" in text:
        raise AssertionError("the workflow names texlive/texlive:latest: `latest` moves, so a release cannot be rebuilt")
    if "texlive/texlive@" in text:
        raise AssertionError("the workflow carries its own copy of the digest; build.py is the only place it should live")
    if "build.py --image" not in text:
        raise AssertionError("the workflow never asks build.py which image to pull, so the two can disagree")


def test_the_tex_image_is_pinned_by_digest_and_in_one_file_only():
    check_tex_pin(_workflow())
    doc = _broken(_workflow(), "manuals", lambda job: job["steps"].insert(4, {"name": "pull", "run": 'docker pull "texlive/texlive:latest"'}))
    with pytest.raises(AssertionError, match="texlive/texlive:latest"):
        check_tex_pin(doc)
    doc = _broken(_workflow(), "manuals", lambda job: job["steps"].pop(3))  # the step that reads --image
    with pytest.raises(AssertionError, match="which image to pull"):
        check_tex_pin(doc)
    doc = _broken(_workflow(), "manuals", lambda job: job["steps"].pop(2))  # the disk check
    with pytest.raises(AssertionError, match="room for the image"):
        check_manuals_job(doc)


def test_the_workflow_states_the_image_size_and_a_measured_time():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "2.8 GB" in text and "9.1 GB" in text, "the job should say how big the pinned image is: the runner's disk is what fails first"
    assert re.search(r"\d+ m \d+ s", text), "the job should record how long the three PDFs actually took"
    doc = _workflow()
    assert doc["jobs"]["manuals"]["timeout-minutes"] >= 30, "the pull plus three latexmk runs needs more than this timeout"
    assert "12000000000" in text, "the disk check needs a byte count, not a guess"


# --- the GitHub Release job --------------------------------------------------------------------


def smoke_jobs(doc: dict) -> set[str]:
    """Every job that runs a smoke test, worked out from what it runs rather than from its name."""
    found = set()
    for name, job in doc["jobs"].items():
        for step in job["steps"]:
            run = str(step.get("run", ""))
            if "smoke_installer.py" in run or "smoke_linux.py" in run or "build_pyinstaller.py --smoke" in run:
                found.add(name)
    return found


def check_release_job(doc: dict) -> None:
    check_full_history(doc)
    job = doc["jobs"].get("release")
    if job is None:
        raise AssertionError("no job publishes a GitHub Release: a tag produces artifacts nobody can download")
    if "refs/tags/v" not in str(job.get("if", "")):
        raise AssertionError(f"the publish job is gated on {job.get('if')!r}: a workflow_dispatch run must publish nothing")
    needs = job.get("needs")
    needs = needs if isinstance(needs, list) else [needs]
    missing = smoke_jobs(doc) - set(needs)
    if missing:
        raise AssertionError(f"the Release is published before {sorted(missing)} smoke-tested their artifacts")
    if doc.get("permissions") != {"contents": "read"}:
        raise AssertionError("the workflow's own permissions are not read-only")
    if job.get("permissions") != {"contents": "write"}:
        raise AssertionError("contents: write belongs on the publish job and nowhere else")
    for name, other in doc["jobs"].items():
        if name != "release" and "write" in str(other.get("permissions", "")):
            raise AssertionError(f"{name} has write permissions it does not need")

    downloads = _uses(doc, "release", "download-artifact")
    if not downloads or downloads[0].get("name") is not None:
        raise AssertionError("the publish job must download every artifact of this run, not one named artifact")
    if downloads[0].get("path") != "artifacts":
        raise AssertionError("the artifacts must land where the check step is told to look")

    runs = _runs(doc, "release")
    assemble = next((r for r in runs if "release_assets.py" in r), None)
    publish = next((r for r in runs if "gh release" in r), None)
    if assemble is None or publish is None:
        raise AssertionError("the publish job assembles nothing, or publishes nothing")
    for flag in ("--manuals", "--installer", "--packages", "--out"):
        if flag not in assemble:
            raise AssertionError(f"the assemble step names no {flag}: one of the ten files is unaccounted for")
    if runs.index(assemble) > runs.index(publish):
        raise AssertionError("the Release is created before every file is checked: nothing partial may be published")
    expected = len(build.MANUAL_FILES) + 4
    if f"expected $expected" not in publish and str(expected) not in publish:
        raise AssertionError(f"the publish step uploads files it never counted against the {expected} it should have")
    glob = re.search(r"assets=\(\s*([^)]+)\)", publish)
    out = re.search(r"--out\s+([^\s\\]+)", assemble)
    if not glob or not out:
        raise AssertionError("the publish step uploads assets nobody assembled")
    directory, pattern = glob.group(1).rsplit("/", 1)
    if directory != out.group(1):
        raise AssertionError(f"the publish glob reads {directory}, but the assets were assembled into {out.group(1)}: gh uploads an empty directory")
    for name in RELEASE_ASSET_NAMES:
        if not fnmatch.fnmatch(name, pattern):
            raise AssertionError(f"the publish glob {glob.group(1)} skips {name}: a Release with only the files the glob matches")
    if "--clobber" not in publish or "gh release view" not in publish:
        raise AssertionError("re-running the job for a tag that already has a Release must replace the assets, not fail")
    publish_code = "\n".join(
        line.split(" #", 1)[0]
        for line in publish.splitlines()
        if not line.lstrip().startswith("#")
    )
    if not re.search(r'if\s+\[\[\s*"\$tag"\s*==\s*\*-\*\s*\]\];\s*then\s+gh release edit "\$tag" --prerelease(?:\s|$)', publish_code):
        raise AssertionError("a tag with a prerelease suffix must run gh release edit with --prerelease")
    checkout = _checkout(doc, "release")
    if checkout.get("fetch-depth") != 0:
        raise AssertionError(f"the publish job checks out at fetch-depth {checkout.get('fetch-depth')}: a shallow clone has no tag to describe")


def check_publish_token(doc: dict) -> None:
    """The one step that runs `gh` names exactly the token its calls use."""
    steps = [s for s in doc["jobs"]["release"]["steps"] if "gh release" in str(s.get("run", ""))]
    if len(steps) != 1:
        raise AssertionError(f"the publish job runs gh in {len(steps)} steps: each of them needs the token named")
    env = steps[0].get("env") or {}
    if env.get("GH_TOKEN") != "${{ github.token }}":
        raise AssertionError(
            "the gh calls in the publish step are authenticated by "
            f"{env.get('GH_TOKEN')!r}: the step has to say GH_TOKEN: ${{ github.token }}"
        )
    if "secrets." in str(env):
        raise AssertionError("the publish step reaches for a secret: the run's own token is the one that works on a private repository")


def test_the_release_job_publishes_on_a_tag_after_every_smoke_run():
    doc = _workflow()
    check_release_job(doc)
    assert smoke_jobs(doc) == {"windows-installer", "linux-build", "linux-smoke"}, smoke_jobs(doc)


def test_publishing_on_a_manual_run_is_caught():
    doc = _broken(_workflow(), "release", lambda job: job.pop("if"))
    with pytest.raises(AssertionError, match="must publish nothing"):
        check_release_job(doc)


def test_the_publish_step_names_the_token_its_gh_calls_use():
    doc = _workflow()
    check_publish_token(doc)
    doc = _broken(doc, "release", lambda job: job["steps"][-1].pop("env"))
    with pytest.raises(AssertionError, match="GH_TOKEN"):
        check_publish_token(doc)
    for wrong in ("${{ secrets.NOPE }}", "${{ GITHUB_TOKEN }}", "${{ secrets.GITHUB_TOKEN }}", "gh" + "p_placeholder"):
        doc = _broken(_workflow(), "release", lambda job, wrong=wrong: job["steps"][-1].update({"env": {"GH_TOKEN": wrong}}))
        with pytest.raises(AssertionError, match="github.token"):
            check_publish_token(doc)
    # the token on the job is not the token on the step: gh runs in the step
    doc = _broken(_workflow(), "release", lambda job: (job["steps"][-1].pop("env"), job.update({"env": {"GH_TOKEN": "${{ github.token }}"}})))
    with pytest.raises(AssertionError, match="GH_TOKEN"):
        check_publish_token(doc)
    # a second step running gh would not inherit the first step's env
    doc = _broken(_workflow(), "release", lambda job: job["steps"].insert(4, {"name": "extra gh", "run": 'gh release list --limit 1'}))
    with pytest.raises(AssertionError, match="runs gh in 2 steps"):
        check_publish_token(doc)


def test_a_smoke_job_missing_from_needs_is_caught():
    doc = _broken(_workflow(), "release", lambda job: job["needs"].remove("linux-smoke"))
    with pytest.raises(AssertionError, match="before"):
        check_release_job(doc)


def test_write_permission_anywhere_else_is_caught():
    doc = _broken(_workflow(), None, lambda d: d["permissions"].update({"contents": "write"}))
    with pytest.raises(AssertionError, match="read-only"):
        check_release_job(doc)
    doc = _broken(_workflow(), "linux-build", lambda job: job.update({"permissions": {"contents": "write"}}))
    with pytest.raises(AssertionError, match="write permissions it does not need"):
        check_release_job(doc)


def test_the_asset_count_check_comes_before_the_publish():
    doc = _broken(_workflow(), "release", lambda job: job["steps"].insert(3, job["steps"].pop(4)))
    with pytest.raises(AssertionError, match="before every file"):
        check_release_job(doc)
    doc = _broken(_workflow(), "release", lambda job: job["steps"].pop(3))
    with pytest.raises(AssertionError, match="assembles nothing"):
        check_release_job(doc)
    doc = _broken(_workflow(), "release", lambda job: job["steps"][-1].update({"run": job["steps"][-1]["run"].replace("--clobber", "")}))
    with pytest.raises(AssertionError, match="replace the assets"):
        check_release_job(doc)
    doc = _broken(_workflow(), "release", lambda job: job["steps"][-1].update({"run": job["steps"][-1]["run"].replace("--prerelease", "")}))
    with pytest.raises(AssertionError, match="prerelease"):
        check_release_job(doc)
    doc = _broken(_workflow(), "release", lambda job: job["steps"][-1].update({"run": job["steps"][-1]["run"].replace('gh release edit "$tag" --prerelease', "true")}))
    with pytest.raises(AssertionError, match="prerelease"):
        check_release_job(doc)
    doc = _broken(_workflow(), "release", lambda job: job["steps"][-1].update({"run": job["steps"][-1]["run"].replace('"$tag" == *-*', "true")}))
    with pytest.raises(AssertionError, match="prerelease suffix"):
        check_release_job(doc)


def test_every_job_that_needs_the_tag_fetches_the_whole_history():
    check_full_history(_workflow())
    for job in ("manuals", "windows-installer", "linux-build", "release"):
        doc = _broken(_workflow(), job, lambda job: job["steps"][0]["with"].update({"fetch-depth": 1}))
        with pytest.raises(AssertionError, match=f"{job} checks out at fetch-depth 1"):
            check_full_history(doc)
        with pytest.raises(AssertionError, match=f"{job} checks out at fetch-depth 1"):
            check_manuals_job(doc)
        with pytest.raises(AssertionError, match=f"{job} checks out at fetch-depth 1"):
            check_release_job(doc)


def test_a_shallow_manuals_checkout_is_caught_by_the_title_page_check():
    doc = _broken(_workflow(), "manuals", lambda job: job["steps"][0]["with"].update({"fetch-depth": 1}))
    with pytest.raises(AssertionError, match="fetch-depth 1"):
        check_title_pages(doc)


def test_the_title_page_is_grepped_for_the_version_and_nothing_looser():
    doc = _broken(_workflow(), "manuals", lambda job: job["steps"][4].update({"run": job["steps"][4]["run"].replace('grep -Fxq "$version"', 'grep -Fq "ATE"')}))
    with pytest.raises(AssertionError, match="not compared with the version as a whole"):
        check_title_pages(doc)
    doc = _broken(_workflow(), "manuals", lambda job: job["steps"][4].update({"run": job["steps"][4]["run"].replace('grep -Fxq "$version"', 'grep -Fq "$version"')}))
    with pytest.raises(AssertionError, match="not compared with the version as a whole"):
        check_title_pages(doc)


def test_a_disk_floor_that_does_not_match_the_image_it_describes_is_caught():
    doc = _broken(_workflow(), "manuals", lambda job: job["steps"][2].update({"run": job["steps"][2]["run"].replace("12000000000", "1000000000")}))
    with pytest.raises(AssertionError, match="below the 9.1 GB"):
        check_manuals_job(doc)
    doc = _broken(_workflow(), "manuals", lambda job: job["steps"][2].update({"run": job["steps"][2]["run"].replace("12000000000", "120000000000")}))
    with pytest.raises(AssertionError, match="more than twice"):
        check_manuals_job(doc)
    doc = _broken(_workflow(), "manuals", lambda job: job["steps"][2].update({"run": job["steps"][2]["run"].replace("image is 9.1 GB", "image is 30 GB")}))
    with pytest.raises(AssertionError, match="below the 30 GB"):
        check_manuals_job(doc)


# --- the gh calls themselves ---------------------------------------------------------------------


def _section(text: str, start: str, end: str) -> str:
    """The part of a help page between two headings."""
    head = text.find(start)
    if head < 0:
        return ""
    tail = text.find(end, head + len(start))
    return text[head + len(start) : tail if tail > 0 else len(text)]


def check_gh_calls(doc: dict) -> None:
    """Every command, flag and `--json` field the publish step gives `gh` is one `gh` accepts."""
    publish = next((r for r in _runs(doc, "release") if "gh release" in r), None)
    if publish is None:
        raise AssertionError("the release job never calls gh: nothing is published")
    wrong = []
    for line in _gh_lines(publish):
        match = re.search(r"\bgh\s+([a-z][a-z-]*(?:\s+[a-z][a-z-]*)?)", line)
        if not match:
            continue
        words = match.group(1).split()
        help_text = None
        for depth in range(len(words)):
            proc = subprocess.run(["gh"] + words[: depth + 1] + ["--help"], capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                wrong.append(f"gh has no command {' '.join(words[: depth + 1])}")
                break
            if depth + 1 < len(words):
                # `gh release eddit --help` prints the parent's help and exits 0: the subcommand
                # has to be one the parent actually lists.
                listed = set(re.findall(r"^\s+([a-z][a-z-]*):", proc.stdout, re.M))
                if words[depth + 1] not in listed:
                    wrong.append(f"gh {' '.join(words[: depth + 1])} has no {words[depth + 1]}")
                    break
            help_text = proc.stdout
        if help_text is None:
            continue
        flags = set(re.findall(r"--([a-z][a-z-]+)", line[match.end():]))
        known = set(re.findall(r"^\s*-?[A-Za-z]?,?\s*--([a-z][a-z-]+)", _section(help_text, "FLAGS", "INHERITED FLAGS"), re.M))
        for flag in sorted(flags - known):
            wrong.append(f"gh {' '.join(words)} has no --{flag}")
        fields = re.search(r"--json\s+([a-zA-Z,]+)", line)
        if fields:
            available = set(re.split(r"[,\s]+", _section(help_text, "JSON FIELDS", "LEARN MORE")))
            for field in fields.group(1).split(","):
                if field and field not in available:
                    wrong.append(f"gh {' '.join(words)} --json has no field {field}")
    if wrong:
        raise AssertionError("the workflow calls gh with things gh does not accept: " + "; ".join(sorted(wrong)))


def test_a_publish_glob_that_matches_only_some_of_the_assets_is_caught():
    doc = _broken(_workflow(), "release", lambda job: job["steps"][-1].update({"run": job["steps"][-1]["run"].replace("assets=(release-assets/*)", "assets=(release-assets/*.pdf)")}))
    with pytest.raises(AssertionError, match=r"skips test-writer\.md"):
        check_release_job(doc)
    doc = _broken(_workflow(), "release", lambda job: job["steps"][-1].update({"run": job["steps"][-1]["run"].replace("assets=(release-assets/*)", "assets=(release-assets/*.md)")}))
    with pytest.raises(AssertionError, match="skips test-writer.pdf"):
        check_release_job(doc)


def test_an_assemble_out_directory_the_publish_glob_does_not_read_is_caught():
    doc = _broken(_workflow(), "release", lambda job: job["steps"][-2].update({"run": job["steps"][-2]["run"].replace("--out release-assets", "--out staged-assets")}))
    with pytest.raises(AssertionError, match="assembled into staged-assets"):
        check_release_job(doc)
    doc = _broken(_workflow(), "release", lambda job: job["steps"][-1].update({"run": job["steps"][-1]["run"].replace("assets=(release-assets/*)", "assets=(artifacts/*)")}))
    with pytest.raises(AssertionError, match="reads artifacts"):
        check_release_job(doc)


def check_expected_version_is_gated_on_a_tag(doc: dict) -> None:
    """The smoke steps expect a version only when the ref is a tag, not when the ref name starts with v."""
    for job, needle in (("windows-installer", "smoke_installer.py"), ("linux-smoke", "smoke_linux.py")):
        step = next((s for s in doc["jobs"][job]["steps"] if needle in str(s.get("run", ""))), None)
        if step is None:
            raise AssertionError(f"{job} runs no smoke step: nothing checks the manuals it shipped")
        run = str(step["run"])
        if not re.search(r"(?:GITHUB_REF\b|github\.ref\b|\$\{?ref\}?)[^#\n]{0,60}refs/tags/v", run):
            raise AssertionError(f"{job} decides the expected version from the ref name: a dispatch run on a branch called v2-fix would expect release 2-fix")


def test_the_expected_version_is_decided_by_a_tag_and_not_a_branch_name():
    doc = _workflow()
    check_expected_version_is_gated_on_a_tag(doc)
    doc = _broken(doc, "linux-smoke", lambda job: job["steps"][-1].update({"run": 'tag="${GITHUB_REF_NAME#v}"\npython3 packaging/linux/smoke_linux.py --kind ${{ matrix.kind }} --dist artifacts --expect-version "$tag"'}))
    with pytest.raises(AssertionError, match="decides the expected version from the ref name"):
        check_expected_version_is_gated_on_a_tag(doc)
    doc = _broken(_workflow(), "windows-installer", lambda job: job["steps"][-2].update({"run": '$tag = $env:GITHUB_REF_NAME\nif ($tag -match \'^v\') { $tag = $tag.Substring(1) } else { $tag = "" }\npython packaging/windows/smoke_installer.py --expect-version "$tag"'}))
    with pytest.raises(AssertionError, match="decides the expected version from the ref name"):
        check_expected_version_is_gated_on_a_tag(doc)
    doc = _broken(_workflow(), "linux-smoke", lambda job: job["steps"][-1].update({"run": job["steps"][-1]["run"].replace("${GITHUB_REF}", "${{ github.ref_name }}")}))
    with pytest.raises(AssertionError, match="decides the expected version from the ref name"):
        check_expected_version_is_gated_on_a_tag(doc)


def test_the_release_publishes_nothing_when_the_run_is_not_a_tag():
    doc = _workflow()
    check_expected_version_is_gated_on_a_tag(doc)
    doc = _broken(doc, "release", lambda job: job.update({"if": "true"}))
    with pytest.raises(AssertionError, match="must publish nothing"):
        check_release_job(doc)
    doc = _broken(_workflow(), "release", lambda job: job.update({"if": "startsWith(github.ref_name, 'v')"}))
    with pytest.raises(AssertionError, match="must publish nothing"):
        check_release_job(doc)


def test_gh_gets_only_flags_and_fields_gh_has():
    if shutil.which("gh") is None:
        pytest.skip("gh is not installed here: there is no help output to check the flags against")
    doc = _workflow()
    check_gh_calls(doc)
    doc = _broken(doc, "release", lambda job: job["steps"][-1].update({"run": job["steps"][-1]["run"].replace("--clobber", "--force")}))
    with pytest.raises(AssertionError, match="no --force"):
        check_gh_calls(doc)
    doc = _broken(_workflow(), "release", lambda job: job["steps"][-1].update({"run": job["steps"][-1]["run"].replace("--json name,isPrerelease,assets", "--json name,prerelease,assets")}))
    with pytest.raises(AssertionError, match="no field prerelease"):
        check_gh_calls(doc)
    # the subcommand itself: a word gh does not have is not a command, however well the flags read
    doc = _broken(_workflow(), "release", lambda job: job["steps"][-1].update({"run": job["steps"][-1]["run"].replace("gh release create", "gh realese create")}))
    with pytest.raises(AssertionError, match="no command realese"):
        check_gh_calls(doc)
    doc = _broken(_workflow(), "release", lambda job: job["steps"][-1].update({"run": job["steps"][-1]["run"].replace("gh release edit", "gh release eddit")}))
    with pytest.raises(AssertionError, match="gh release has no eddit"):
        check_gh_calls(doc)


# --- the ten files, assembled from this run's artifacts ------------------------------------------


def _fake_pdf(title: str) -> bytes:
    """A PDF whose Info dictionary is readable without a viewer, the way TeX writes one."""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"4 0 obj << /Type /Info /Title(" + title.encode("ascii") + b") >> endobj\n"
        b"trailer << /Root 1 0 R /Info 4 0 R >>\n"
    )


def _artifacts(
    root: Path,
    version: str = "0.1.0rc2",
    *,
    no_manual: str | None = None,
    empty_md: str | None = None,
    bad_pdf: bool = False,
    old_stamp: bool = False,
    two_installers: bool = False,
    no_wheel: bool = False,
) -> dict[str, Path]:
    """Fake artifact directories shaped like what `download-artifact` writes."""
    manuals_dir = root / "ate-manuals"
    manuals_dir.mkdir(parents=True, exist_ok=True)
    for name in build.MANUAL_FILES:
        if name == no_manual:
            continue
        target = manuals_dir / name
        if name.endswith(".pdf"):
            target.write_bytes(b"not a pdf" if bad_pdf else _fake_pdf(f"{name[:-4]} manual, ATE {version if not old_stamp else '0.1.0'}"))
        else:
            target.write_text("" if name == empty_md else "# ATE manual\n", encoding="utf-8", newline="\n")

    installer = root / "ate-windows-installer"
    installer.mkdir(parents=True, exist_ok=True)
    (installer / f"ate-{version}-windows-x64-setup.exe").write_bytes(b"MZ")
    if two_installers:
        # a run that built two installers cannot say which one the Release is for
        (installer / "ate-0.1.0rc1-windows-x64-setup.exe").write_bytes(b"MZ")

    packages = root / "ate-linux-artifacts"
    packages.mkdir(parents=True, exist_ok=True)
    (packages / f"ate-{version}-linux-x86_64.tar.gz").write_bytes(b"gzip")
    (packages / f"ate_{linux._deb_version(version)}_amd64.deb").write_bytes(b"!<arch>")
    if not no_wheel:
        (packages / f"ate-{version}-py3-none-any.whl").write_bytes(b"PK")
    (packages / "glibc-build.txt").write_text("ldd (Ubuntu GLIBC 2.39)\n", encoding="utf-8", newline="\n")
    return {"manuals": manuals_dir, "installer": installer, "packages": packages}


def test_a_complete_run_assembles_ten_assets_and_every_name_carries_the_version():
    with _fixtures.temp_directory() as tmp:
        root = Path(tmp)
        dirs = _artifacts(root)
        out = root / "release-assets"
        found = assets.assemble(dirs["manuals"], dirs["installer"], dirs["packages"], out, "0.1.0rc2")
        assert len(found) == 10, found
        names = sorted(p.name for p in found)
        expected = sorted(
            [assets.asset_name(name, "0.1.0rc2") for name in build.MANUAL_FILES]
            + ["ate-0.1.0rc2-windows-x64-setup.exe", "ate_0.1.0~rc2_amd64.deb", "ate-0.1.0rc2-linux-x86_64.tar.gz", "ate-0.1.0rc2-py3-none-any.whl"]
        )
        assert names == expected, names
        for name in names:
            assert "0.1.0rc2" in name.replace("~", ""), f"asset {name} does not say which release it belongs to"
        assert "6 manuals" in assets.report(found) and "4 packages" in assets.report(found)


def test_a_manual_missing_from_the_artifact_publishes_nothing():
    with _fixtures.temp_directory() as tmp:
        root = Path(tmp)
        dirs = _artifacts(root, no_manual="developer.pdf")
        out = root / "release-assets"
        with pytest.raises(assets.AssetError, match="developer.pdf is not in the ate-manuals artifact"):
            assets.assemble(dirs["manuals"], dirs["installer"], dirs["packages"], out, "0.1.0rc2")
        assert not out.exists(), "an incomplete release must not get as far as making a directory"


def test_a_manual_that_is_empty_or_not_a_pdf_is_refused():
    with _fixtures.temp_directory() as tmp:
        root = Path(tmp)
        dirs = _artifacts(root, bad_pdf=True)
        with pytest.raises(assets.AssetError, match="is not a PDF"):
            assets.assemble(dirs["manuals"], dirs["installer"], dirs["packages"], root / "out1", "0.1.0rc2")
        dirs = _artifacts(root, empty_md="admin.md")
        with pytest.raises(assets.AssetError, match="admin.md is empty"):
            assets.assemble(dirs["manuals"], dirs["installer"], dirs["packages"], root / "out2", "0.1.0rc2")


def test_a_pdf_stamped_with_the_previous_version_is_refused():
    with _fixtures.temp_directory() as tmp:
        root = Path(tmp)
        dirs = _artifacts(root, old_stamp=True)
        with pytest.raises(assets.AssetError, match="not the version being released"):
            assets.assemble(dirs["manuals"], dirs["installer"], dirs["packages"], root / "out", "0.1.0rc2")


def test_a_pdf_stamped_with_a_prerelease_is_not_read_as_the_release_it_precedes():
    helpers = assets._helpers()
    with _fixtures.temp_directory() as tmp:
        pdf = Path(tmp) / "admin.pdf"
        pdf.write_bytes(_fake_pdf("ATE admin manual 0.1.0rc3"))
        assert helpers.carries_version(pdf, "0.1.0rc3"), "the manual carries the prerelease it was built for"
        assert not helpers.carries_version(pdf, "0.1.0"), "0.1.0 is inside 0.1.0rc3 as text, not as a version"
        assert not helpers.carries_version(pdf, "0.1.0rc33"), "a longer stamp is not this version either"
        out = Path(tmp) / "assets"
        dirs = _artifacts(Path(tmp), version="0.1.0rc3")
        with pytest.raises(assets.AssetError, match="not the version being released"):
            assets.assemble(dirs["manuals"], dirs["installer"], dirs["packages"], out, "0.1.0")


def test_a_manual_that_applies_twice_in_the_artifact_is_refused():
    """The manuals get the same one-file rule the packages do: two copies is not one release."""
    with _fixtures.temp_directory() as tmp:
        root = Path(tmp)
        dirs = _artifacts(root)
        nested = dirs["manuals"] / "docs" / "manuals" / "llm"
        nested.mkdir(parents=True)
        (nested / "admin.md").write_text("# ATE manual\n", encoding="utf-8", newline="\n")
        with pytest.raises(assets.AssetError, match="holds 2 admin.md files"):
            assets.assemble(dirs["manuals"], dirs["installer"], dirs["packages"], root / "out", "0.1.0rc2")
        # a PDF duplicated the same way is refused too, not just a Markdown manual
        dirs = _artifacts(root)
        (dirs["manuals"] / "leftover").mkdir()
        (dirs["manuals"] / "leftover" / "admin.pdf").write_bytes(_fake_pdf("ATE admin manual 0.1.0rc1"))
        with pytest.raises(assets.AssetError, match="holds 2 admin.pdf files"):
            assets.assemble(dirs["manuals"], dirs["installer"], dirs["packages"], root / "out2", "0.1.0rc2")


def test_a_package_the_builds_did_not_produce_is_refused():
    with _fixtures.temp_directory() as tmp:
        root = Path(tmp)
        dirs = _artifacts(root, no_wheel=True)
        with pytest.raises(assets.AssetError, match="no wheel"):
            assets.assemble(dirs["manuals"], dirs["installer"], dirs["packages"], root / "out", "0.1.0rc2")
        dirs = _artifacts(root, two_installers=True)
        with pytest.raises(assets.AssetError, match="2 Windows installer files"):
            assets.assemble(dirs["manuals"], dirs["installer"], dirs["packages"], root / "out2", "0.1.0rc2")
        dirs = _artifacts(root)
        with pytest.raises(assets.AssetError, match="is not a directory"):
            assets.assemble(dirs["manuals"], dirs["installer"], root / "ate-linux-artifacts-not-downloaded", root / "out3", "0.1.0rc2")


def test_the_asset_globs_match_the_names_the_packaging_scripts_write():
    version = "0.1.0rc2"
    produced = {
        "Windows installer": f"ate-{version}-windows-x64-setup.exe",
        "deb": f"ate_{linux._deb_version(version)}_amd64.deb",
        "tarball": f"ate-{version}-linux-x86_64.tar.gz",
        "wheel": f"ate-{version}-py3-none-any.whl",
    }
    for label, name in produced.items():
        assert fnmatch.fnmatch(name, assets.PACKAGE_GLOBS[label]), f"{label} is written as {name}, which {assets.PACKAGE_GLOBS[label]} does not match"
    assert len(assets.PACKAGE_GLOBS) == 4, "four packages plus six manuals is the ten files a Release carries"
    assert "windows-x64-setup.exe" in BUILD_INSTALLER.read_text(encoding="utf-8"), "the installer's name is not the glob's"


def test_the_deb_comment_gets_dpkg_remove_and_purge_right():
    """`dpkg --remove` deletes every file the package installed, the doc PDFs with them."""
    text = BUILD_LINUX.read_text(encoding="utf-8")
    block = text[text.index("Debian's policy") : text.index("doc = stage")]
    assert re.search(r"dpkg --remove`? deletes them", block), block
    assert "conffile" in block and "purge" in block, block
    assert "leaves it on a plain remove" not in block, "documentation is package content, not a conffile"
