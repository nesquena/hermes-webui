"""#8450 re-gate round 4: the `webui.workspace_path_maps` key is documented.

The key is a deployment-facing configuration key: an operator in a
multi-container setup has to be able to find what it is and what it does.
A doc that drifts from the code is worse than no doc, so the test reads the
key out of the code and requires the documentation to describe that key.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKSPACE_PY = REPO / "api" / "workspace.py"
MAPS_DOC = REPO / "docs" / "workspace-path-maps.md"
README = REPO / "README.md"
GIT_DOC = REPO / "docs" / "workspace-git.md"


def _declared_map_key() -> str:
    """The dotted config path the code actually reads."""
    source = WORKSPACE_PY.read_text(encoding="utf-8")
    match = re.search(
        r"_WORKSPACE_PATH_MAPS_KEYS\s*=\s*\(\s*\"([^\"]+)\"\s*,\s*\"([^\"]+)\"\s*\)",
        source,
    )
    assert match, "the map key tuple moved -- update this test and the doc"
    return f"{match.group(1)}.{match.group(2)}"


def test_the_map_key_is_documented():
    key = _declared_map_key()
    assert MAPS_DOC.exists(), "docs/workspace-path-maps.md is missing"
    doc = MAPS_DOC.read_text(encoding="utf-8")
    assert key in doc, f"the doc must name the config key {key}"
    # Both halves of an entry, so the operator can write one.
    assert "agent:" in doc and "webui:" in doc


def test_the_doc_is_linked_from_the_readme():
    assert MAPS_DOC.exists()
    readme = README.read_text(encoding="utf-8")
    assert "docs/workspace-path-maps.md" in readme, (
        "the README docs index must point at the map documentation"
    )


def test_the_doc_is_linked_from_the_git_controls_doc():
    assert MAPS_DOC.exists()
    git_doc = GIT_DOC.read_text(encoding="utf-8")
    assert "workspace-path-maps.md" in git_doc, (
        "the workspace Git doc must point at the map documentation"
    )


def test_the_doc_describes_the_trust_rules_it_cannot_omit():
    """The map must not read as a way around the trust rules."""
    doc = MAPS_DOC.read_text(encoding="utf-8")
    lowered = doc.lower()
    for needle in ("trust", "saved workspace", "re-validate"):
        assert needle in lowered, f"the doc must mention {needle!r}"


def test_every_entry_key_the_code_reads_is_documented():
    """The code reads ``agent`` and ``webui``; the doc has to name both."""
    source = WORKSPACE_PY.read_text(encoding="utf-8")
    body = source[source.index("_WORKSPACE_PATH_MAPS_KEYS"):]
    body = body[: body.index("def ", 10)]
    doc = MAPS_DOC.read_text(encoding="utf-8")
    for key in ("agent", "webui"):
        if f'entry.get("{key}")' in body:
            assert f"{key}:" in doc, f"the entry key {key!r} is read but not documented"
