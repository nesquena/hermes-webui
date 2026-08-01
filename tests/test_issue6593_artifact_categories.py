"""Regression coverage for the client-derived artifact categories in #6593."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")
FIXTURE = REPO / "tests" / "fixtures" / "issue6593_artifact_categories.json"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_fn(name: str) -> str:
    start = WORKSPACE_JS.index(f"function {name}(")
    brace = WORKSPACE_JS.index("{", start)
    depth = 0
    for index in range(brace, len(WORKSPACE_JS)):
        if WORKSPACE_JS[index] == "{":
            depth += 1
        elif WORKSPACE_JS[index] == "}":
            depth -= 1
            if depth == 0:
                return WORKSPACE_JS[start:index + 1]
    raise AssertionError(f"function {name} did not close")


def _collect(payload):
    consts = []
    for name in (
        "ARTIFACT_IGNORE_RE",
        "ARTIFACT_MUTATION_TOOLS",
        "ARTIFACT_READ_TOOLS",
        "ARTIFACT_WEB_TOOLS",
        "ARTIFACT_CATEGORY_ORDER",
        "ARTIFACT_CATEGORY_LIMITS",
    ):
        match = re.search(rf"const {name} = .*?;", WORKSPACE_JS)
        if match:
            consts.append(match.group(0))
    function_names = (
            "_normalizeArtifactPath",
            "_normalizeArtifactUrl",
            "_normalizeArtifactTarget",
        "_artifactCandidatesFromText",
        "_artifactCandidatesFromToolCall",
        "collectSessionArtifacts",
    )
    functions = "\n".join(consts) + "\n" + "\n".join(
        _extract_fn(name)
        for name in function_names
        if f"function {name}(" in WORKSPACE_JS
    )
    driver = (
        "const S = { toolCalls: JSON.parse(process.argv[1]), "
        "messages: JSON.parse(process.argv[2]), session: { workspace: '/workspace' } };\n"
        + functions
        + "\nprocess.stdout.write(JSON.stringify(collectSessionArtifacts()));\n"
    )
    result = subprocess.run(
        [NODE, "-e", driver, json.dumps(payload["tool_calls"]), json.dumps(payload["messages"])],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_reported_read_web_media_fixture_projects_all_categories():
    items = _collect(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert [(item.get("category", "modified"), item["path"]) for item in items] == [
        ("modified", "src/app.py"),
        ("read", "src/app.py"),
        ("read", "README.md"),
        ("web", "https://example.com/search?q=artifacts"),
        ("web", "https://example.com/docs"),
        ("web", "https://example.org/visited"),
        ("media", "assets/chart.png"),
        ("media", "assets/diagram.png"),
        ("media", "assets/array.png"),
    ]


def test_structured_inputs_reject_unknown_and_raw_values():
    items = _collect(json.loads(FIXTURE.read_text(encoding="utf-8")))
    values = {item["path"] for item in items}
    assert "secrets.txt" not in values
    assert "https://private.example/hidden" not in values
    assert "javascript:alert(1)" not in values
    assert "do not render" not in values


def test_category_bounds_and_dedup_are_deterministic():
    payload = {
        "tool_calls": [
            {"name": "write_file", "args": {"path": "src/app.py"}},
            *[
                {"name": "read_file", "args": {"path": f"src/read-{index}.py"}}
                for index in range(80)
            ],
            {"name": "read_file", "args": {"path": "src/read-0.py"}},
        ],
        "messages": [
            {"role": "assistant", "content": " ".join(f"MEDIA:assets/{index}.png" for index in range(80))},
        ],
    }
    items = _collect(payload)
    by_category = {}
    for item in items:
        by_category.setdefault(item["category"], []).append(item["path"])
    assert all(len(values) <= 50 for values in by_category.values())
    assert len(by_category["read"]) == 50
    assert len(by_category["media"]) == 50
    assert by_category["read"] == [f"src/read-{index}.py" for index in range(50)]
    assert by_category["media"] == [f"assets/{index}.png" for index in range(50)]
