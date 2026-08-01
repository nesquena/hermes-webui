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
        "_normalizeArtifactFilePath",
        "_normalizeArtifactMediaRef",
        "_normalizeArtifactWorkspacePath",
        "_parseArtifactJson",
        "_artifactToolId",
        "_artifactToolName",
        "_artifactToolArgs",
        "_artifactResultValues",
        "_artifactTextFromValue",
        "_artifactPartialFieldValues",
        "_artifactCandidatesFromText",
        "_artifactCandidatesFromToolCall",
        "_artifactToolResultPayload",
        "_artifactToolResultsById",
        "_artifactToolSources",
        "collectSessionArtifacts",
    )
    functions = "\n".join(consts) + "\n" + "\n".join(
        _extract_fn(name)
        for name in function_names
        if f"function {name}(" in WORKSPACE_JS
    )
    driver = (
        "const toolCalls = JSON.parse(process.argv[1]); "
        "const messages = JSON.parse(process.argv[2]); "
        "const sessionToolCalls = JSON.parse(process.argv[3]); "
        "const S = { toolCalls, messages, session: { workspace: '/workspace', tool_calls: sessionToolCalls } };\n"
        + functions
        + "\nprocess.stdout.write(JSON.stringify(collectSessionArtifacts()));\n"
    )
    result = subprocess.run(
        [NODE, "-e", driver, json.dumps(payload["tool_calls"]), json.dumps(payload["messages"]), json.dumps(payload.get("session_tool_calls", []))],
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
        ("read", "C:/work/src/app.py"),
        ("read", "C:/work/src/README.md"),
        ("read", "C:/work/Makefile"),
        ("read", "C:/work/partial.md"),
        ("read", "C:/work/preview.md"),
        ("web", "https://example.com/docs"),
        ("web", "https://example.org/visited"),
        ("media", "assets/chart.png"),
        ("media", "file:///tmp/shot.png"),
        ("media", "assets/output.png"),
    ]


def test_structured_inputs_reject_unknown_and_raw_values():
    items = _collect(json.loads(FIXTURE.read_text(encoding="utf-8")))
    values = {item["path"] for item in items}
    assert "secrets.txt" not in values
    assert "https://private.example/hidden" not in values
    assert "javascript:alert(1)" not in values
    assert "do not render" not in values
    assert "src" not in values
    assert "assets/user.png" not in values
    assert "assets/user-image.png" not in values
    assert "unsupported.png" not in values
    assert "assets/tool.png" not in values
    assert "chart.png" not in values
    assert "../secret.png" not in values


def test_extensionless_read_targets_and_path_traversal_boundaries():
    items = _collect({
        "tool_calls": [
            {"name": "read_file", "args": {"path": name}}
            for name in ("LICENSE", "Makefile", "Dockerfile", "../secret", "src/../../secret")
        ],
        "messages": [],
    })
    assert [item["path"] for item in items] == ["LICENSE", "Makefile", "Dockerfile"]


def test_search_files_projects_paths_from_grouped_match_text():
    items = _collect({
        "tool_calls": [{
            "name": "search_files",
            "args": {"path": "src", "pattern": "artifact"},
            "result": json.dumps({
                "matches_text": "src/README.md\n  12: artifact\nsrc/Makefile\n  4: artifact"
            }),
        }],
        "messages": [],
    })
    assert [item["path"] for item in items] == ["src/README.md", "src/Makefile"]


def test_diff_fences_from_non_allowlisted_results_keep_mutation_projection():
    items = _collect({
        "tool_calls": [{
            "name": "custom_patch_wrapper",
            "result": "```diff\n--- a/src/generated.py\n+++ b/src/generated.py\n@@\n```",
        }],
        "messages": [],
    })
    assert [(item["category"], item["path"]) for item in items] == [
        ("modified", "src/generated.py")
    ]


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


def test_settled_tool_results_are_correlated_by_provider_tool_id():
    payload = {
        "tool_calls": [],
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "web-1",
                    "function": {"name": "web_extract", "arguments": "{}"},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "web-1",
                "content": '{"results":[{"href":"https://example.com/correlated"}]}',
            },
            {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": "anthropic-1",
                    "name": "web_extract",
                    "input": {},
                }],
            },
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "anthropic-1",
                    "content": '{"results":[{"url":"https://example.com/anthropic"}]}',
                }],
            },
        ],
    }
    assert [item["path"] for item in _collect(payload)] == [
        "https://example.com/correlated",
        "https://example.com/anthropic",
    ]


def test_session_tool_call_summaries_join_settled_tool_results_by_provider_id():
    payload = {
        "tool_calls": [
            {"id": "summary-openai", "name": "web_extract", "args": {}},
            {"id": "summary-anthropic", "name": "web_extract", "input": {}},
        ],
        "messages": [
            {
                "role": "tool",
                "tool_call_id": "summary-openai",
                "content": '{"results":[{"href":"https://example.com/session-summary"}]}',
            },
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "summary-anthropic",
                    "content": '{"results":[{"url":"https://example.org/session-summary"}]}',
                }],
            },
        ],
    }
    assert [item["path"] for item in _collect(payload)] == [
        "https://example.com/session-summary",
        "https://example.org/session-summary",
    ]


def test_tool_result_payload_handles_content_blocks_without_numeric_merge_keys():
    payload = {
        "tool_calls": [{"id": "array-result", "name": "web_extract", "args": {}}],
        "messages": [{
            "role": "tool",
            "tool_call_id": "array-result",
            "content": [{
                "type": "tool_result",
                "content": '{"results":[{"url":"https://example.com/array-result"}]}',
            }],
        }],
    }
    assert [item["path"] for item in _collect(payload)] == [
        "https://example.com/array-result"
    ]


def test_windows_file_media_keeps_drive_semantics_in_the_projection():
    items = _collect({
        "tool_calls": [],
        "messages": [{"role": "assistant", "content": "MEDIA:file:///C:/work/chart.png"}],
    })
    assert [item["path"] for item in items] == ["file:///C:/work/chart.png"]


def test_media_uses_shipped_media_contract_without_image_grammar():
    items = _collect({
        "tool_calls": [],
        "messages": [
            {"role": "assistant", "content": "MEDIA:C:\\work\\chart.png [IMAGE:C:\\work\\false.png]"},
            {"role": "user", "content": "MEDIA:C:\\work\\user.png"},
        ],
    })
    assert [item["path"] for item in items] == ["C:/work/chart.png"]


def test_session_summary_survives_truncated_window_and_tool_call_clear():
    items = _collect({
        "tool_calls": [],
        "session_tool_calls": [{"id": "cold-read", "name": "read_file", "args": {"path": "src/cold.md"}}],
        "messages": [{"role": "assistant", "content": "latest visible message"}],
    })
    assert [item["path"] for item in items] == ["src/cold.md"]


def test_non_assistant_structured_tool_metadata_is_ignored():
    items = _collect({
        "tool_calls": [],
        "messages": [
            {"role": "user", "tool_calls": [{"name": "read_file", "args": {"path": "spoofed.md"}}]},
            {"role": "tool", "tool_calls": [{"name": "web_extract", "args": {"url": "https://spoofed.example"}}]},
            {"role": "user", "content": [{"type": "tool_use", "name": "read_file", "input": {"path": "spoofed2.md"}}]},
        ],
    })
    assert items == []
