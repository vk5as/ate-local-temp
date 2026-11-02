from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def test_ci_runs_on_master_pushes_and_pull_requests_only():
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = document.get("on", document.get(True))
    assert triggers["push"]["branches"] == ["master"]
    assert triggers["pull_request"]["branches"] == ["master"]
    assert set(triggers) == {"push", "pull_request"}
