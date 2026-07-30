from pathlib import Path


def test_readme_has_compatibility_section():
    repo_root = Path(__file__).resolve().parents[1]
    readme = (repo_root / "README.md").read_text(encoding="utf-8")

    assert "## Compatibility" in readme, (
        "README.md must contain a ## Compatibility section documenting the "
        "tested hermes-agent compatibility policy"
    )

    assert "Upgrade both together" in readme, (
        "README.md Compatibility section must include upgrade-together guidance "
        '("Upgrade both together")'
    )

    assert "pin both image tags" in readme, (
        "README.md Compatibility section must include Docker pin guidance "
        '("pin both image tags")'
    )

    assert "docs/docker.md" in readme, (
        "README.md Compatibility section must cross-link to docs/docker.md"
    )

    assert "docs/rfcs/agent-source-boundary.md" in readme, (
        "README.md Compatibility section must cross-link to "
        "docs/rfcs/agent-source-boundary.md"
    )

    assert "#2491" in readme, (
        "README.md Compatibility section must reference issue #2491"
    )
