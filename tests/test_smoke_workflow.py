"""Regression coverage for the stable-image smoke workflow."""
import pathlib

REPO = pathlib.Path(__file__).parent.parent
SMOKE_WORKFLOW = (REPO / ".github" / "workflows" / "smoke-stable-image.yml").read_text(
    encoding="utf-8"
)


def test_smoke_workflow_retries_ghcr_pull():
    """The smoke workflow must tolerate short GHCR tag propagation lag."""
    pull_step = SMOKE_WORKFLOW[
        SMOKE_WORKFLOW.find("- name: Pull image"):
        SMOKE_WORKFLOW.find("- name: Run smoke test")
    ]
    assert "for attempt in $(seq 1 12)" in pull_step
    assert 'docker pull "${image}"' in pull_step
    assert "GHCR propagation" in pull_step
