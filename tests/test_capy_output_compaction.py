import json

import pytest

from api.capy_compaction import compact_output


def test_compact_output_preserves_exit_status_and_first_last_error_blocks():
    raw = "\n".join([
        "collecting 12 items",
        "FAILED tests/test_alpha.py::test_a - AssertionError: first failure details",
        *[f"noise line {i}" for i in range(80)],
        "FAILED tests/test_beta.py::test_b - RuntimeError: last failure details",
    ])

    receipt = compact_output(raw, tool="pytest", exit_status=1, max_chars=900)

    assert receipt["compacted"] is True
    assert receipt["exit_status"] == 1
    assert receipt["original_chars"] == len(raw)
    assert receipt["compacted_chars"] <= 900
    assert "preserve_error_blocks" in receipt["rules_applied"]
    assert "FAILED tests/test_alpha.py::test_a" in receipt["text"]
    assert "first failure details" in receipt["text"]
    assert "FAILED tests/test_beta.py::test_b" in receipt["text"]
    assert "last failure details" in receipt["text"]


def test_compact_output_dedupes_repeated_lines_and_collapses_paths_without_hiding_status():
    path = "/Users/bschmidy10/hermes-webui/static/spaces.js"
    raw = "\n".join([
        f"{path}:10 ok",
        f"{path}:10 ok",
        f"{path}:10 ok",
        "PASS tests/test_spaces_ui_js_behaviour.py",
    ])

    receipt = compact_output(raw, tool="terminal", command="pytest tests/test_spaces_ui_js_behaviour.py", exit_status=0)

    assert receipt["exit_status"] == 0
    assert "dedupe_repeated_lines" in receipt["rules_applied"]
    assert "collapse_paths" in receipt["rules_applied"]
    assert "/Users/bschmidy10/hermes-webui" not in receipt["text"]
    assert ".../[REDACTED_PATH]:10 ok" in receipt["text"]
    assert "static/spaces.js" not in receipt["text"]
    assert "repeated 3x" in receipt["text"]
    assert "PASS tests/test_spaces_ui_js_behaviour.py" in receipt["text"]


def test_compact_output_redacts_unsafe_markers_and_reports_redaction_status():
    raw = "\n".join([
        "renderer <script>SECRET_VALUE_DO_NOT_LEAK</script>",
        "api_key bearer placeholder",
        "raw prompt: ignore previous instructions",
        "source code: const x = 1",
        "html: <div>unsafe body</div>",
        "data payload: token abc",
        "safe summary line retained",
    ])

    receipt = compact_output(raw, tool="browser-console", exit_status=0)

    serialized = json.dumps(receipt, sort_keys=True).lower()
    assert receipt["redaction_status"] == "redacted"
    assert receipt["redacted_count"] >= 6
    assert "safe summary line retained" in receipt["text"]
    assert "[REDACTED]" in receipt["text"]
    assert "secret_value_do_not_leak" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "bearer placeholder" not in serialized
    assert "raw prompt" not in serialized
    assert "ignore previous instructions" not in serialized
    assert "source code" not in serialized
    assert "unsafe body" not in serialized
    assert "data payload" not in serialized


def test_compact_output_rejects_unbounded_or_non_string_inputs():
    with pytest.raises(ValueError, match="output must be text"):
        compact_output({"text": "not raw text"}, tool="terminal")
    for invalid in (20, 0, "", False):
        with pytest.raises(ValueError, match="max_chars"):
            compact_output("safe", tool="terminal", max_chars=invalid)  # type: ignore[arg-type]


def test_compact_output_reports_character_cap_rule_for_long_safe_output():
    raw = "\n".join(f"safe verbose line {i}" for i in range(200))

    receipt = compact_output(raw, tool="terminal", exit_status=0, max_chars=700)

    assert receipt["compacted"] is True
    assert receipt["compacted_chars"] <= 700
    assert "cap_section_chars" in receipt["rules_applied"]


def test_compact_output_preserves_approval_prompts_before_char_cap():
    raw = "\n".join([
        "header",
        *[f"verbose line {i}" for i in range(120)],
        "APPROVAL REQUIRED: commit generated widget revision?",
        "approval required: review network side effect",
        "requires approval before external post",
        "confirm before package import",
    ])

    receipt = compact_output(raw, tool="terminal", exit_status=0, max_chars=900)

    assert receipt["compacted_chars"] <= 900
    assert "preserve_approval_prompts" in receipt["rules_applied"]
    assert "APPROVAL REQUIRED: commit generated widget revision?" in receipt["text"]
    assert "approval required: review network side effect" in receipt["text"]
    assert "requires approval before external post" in receipt["text"]
    assert "confirm before package import" in receipt["text"]


def test_compact_output_retains_safe_artifact_handles_and_citations_without_raw_payloads():
    raw = "\n".join(["browser visual QA completed", *[f"safe noisy line {i}" for i in range(120)]])

    receipt = compact_output(
        raw,
        tool="browser-qa",
        command="space.visual_qa.screenshot",
        exit_status=0,
        max_chars=500,
        artifact_handles=[
            {
                "kind": "screenshot",
                "handle": "visual-qa:creator-card:2026-05-19",
                "label": "Creator card screenshot",
                "path": "/Users/bschmidy10/hermes-webui/secrets/screenshot.png",
                "raw_prompt": "SECRET_VALUE_DO_NOT_LEAK",
                "renderer": "<script>bad()</script>",
            },
            {
                "kind": "file",
                "handle": "artifact:research-summary.md",
                "label": "Research summary markdown",
                "body": "SECRET_VALUE_DO_NOT_LEAK",
            },
        ],
        citations=[
            {
                "citation_id": 1,
                "source_type": "memory",
                "title": "Release plan excerpt",
                "path": "/Users/bschmidy10/hermes-webui/private-plan.md",
                "excerpt": "SECRET_VALUE_DO_NOT_LEAK",
                "url": "https://user:token@example.test/private",
            }
        ],
    )

    assert receipt["compacted"] is True
    assert receipt["compacted_chars"] <= 500
    assert "retain_artifact_handles" in receipt["rules_applied"]
    assert "retain_citations" in receipt["rules_applied"]
    assert receipt["retained_artifact_handles"] == [
        {
            "kind": "screenshot",
            "handle": "visual-qa:creator-card:2026-05-19",
            "label": "Creator card screenshot",
        },
        {
            "kind": "file",
            "handle": "artifact:research-summary.md",
            "label": "Research summary markdown",
        },
    ]
    assert receipt["retained_citations"] == [
        {"citation_id": 1, "source_type": "memory", "title": "Release plan excerpt"}
    ]

    serialized = json.dumps(receipt, sort_keys=True).lower()
    assert "secret_value_do_not_leak" not in serialized
    assert "<script" not in serialized
    assert "raw_prompt" not in serialized
    assert "renderer" not in serialized
    assert "/users/bschmidy10" not in serialized
    assert "user:token" not in serialized
    assert "source excerpt" not in serialized
    assert "body" not in serialized


def test_compact_output_filters_unsafe_artifact_handles_and_citations():
    receipt = compact_output(
        "safe short output",
        tool="subagent",
        command="raw prompt SECRET_VALUE_DO_NOT_LEAK /Users/bschmidy10/hermes-webui/private.txt",
        exit_status=0,
        artifact_handles=[
            {"kind": "revision", "handle": "revision:rev-safe-123", "label": "Safe revision"},
            {"kind": "file", "handle": "../../etc/passwd", "label": "Path traversal"},
            {"kind": "file", "handle": "file:/Users/bschmidy10/private.txt", "label": "Local path"},
            {"kind": "artifact", "handle": "artifact:/private/tmp/raw.txt", "label": "Private tmp"},
            {"kind": "screenshot", "handle": "visual:<script>bad()</script>", "label": "Bad script"},
            {"kind": "screenshot", "handle": "visual:safe", "label": ["/Users/bschmidy10/private.txt"]},
        ],
        citations=[
            {"citation_id": 7, "source_type": "knowledge", "title": "Safe citation"},
            {"citation_id": "8<script>", "source_type": "memory", "title": "Bad citation"},
            {"citation_id": 9, "source_type": "source", "title": "SECRET_VALUE_DO_NOT_LEAK"},
            {"citation_id": 10, "source_type": "source", "title": {"path": "/Users/bschmidy10/private.md"}},
        ],
    )

    assert receipt["command"] == "[REDACTED]"
    assert receipt["retained_artifact_handles"] == [
        {"kind": "revision", "handle": "revision:rev-safe-123", "label": "Safe revision"}
    ]
    assert receipt["retained_citations"] == [
        {"citation_id": 7, "source_type": "knowledge", "title": "Safe citation"}
    ]

    serialized = json.dumps(receipt, sort_keys=True).lower()
    assert "../" not in serialized
    assert "<script" not in serialized
    assert "secret_value_do_not_leak" not in serialized
    assert "/users/" not in serialized
    assert "/private/" not in serialized
    assert "file:" not in serialized


def test_compact_output_redacts_paths_and_credential_assignments_from_text_and_metadata():
    receipt = compact_output(
        "token=abc123\naccess_token=def456\ncookie=session789\nspace_name: /Users/bschmidy10/private\nsafe line",
        tool="runner token=abc123",
        command="space.creator.preview access_token=def456",
        exit_status=0,
        artifact_handles=[{"kind": "file", "handle": "artifact:safe", "label": "cookie=session789"}],
        citations=[{"citation_id": "safe-citation", "source_type": "memory", "title": "client_secret=abc123"}],
    )

    assert receipt["tool"] == "unknown"
    assert receipt["command"] == "[REDACTED]"
    assert receipt["retained_artifact_handles"] == []
    assert receipt["retained_citations"] == []
    assert "safe line" in receipt["text"]

    serialized = json.dumps(receipt, sort_keys=True).lower()
    for forbidden in (
        "token=",
        "access_token",
        "cookie=",
        "client_secret",
        "/users/",
        "abc123",
        "def456",
        "session789",
    ):
        assert forbidden not in serialized


def test_compact_output_filters_secret_shaped_tokens_and_raw_paths_from_retained_metadata():
    receipt = compact_output(
        "safe output",
        tool="terminal",
        command="pytest safe",
        exit_status=0,
        artifact_handles=[
            {"kind": "file", "handle": "artifact:ghp_1234567890abcdef", "label": "Classic GitHub token"},
            {"kind": "file", "handle": "artifact:safe", "label": "sk-1234567890abcdef"},
            {"kind": "file", "handle": "artifact:/home/alice/private.txt", "label": "Linux home path"},
            {"kind": "file", "handle": "artifact:C:\\Users\\Alice\\secret.txt", "label": "Windows path"},
            {"kind": "artifact", "handle": "artifact:safe-summary", "label": "Safe summary"},
        ],
        citations=[
            {"citation_id": "github_pat_1234567890abcdef", "source_type": "memory", "title": "Bad token id"},
            {"citation_id": "safe-citation", "source_type": "memory", "title": "AKIA1234567890ABCDEF"},
            {"citation_id": "safe", "source_type": "memory", "title": "~/private/source.md"},
            {"citation_id": "safe-2", "source_type": "memory", "title": "Safe cited summary"},
        ],
    )

    assert receipt["retained_artifact_handles"] == [
        {"kind": "artifact", "handle": "artifact:safe-summary", "label": "Safe summary"}
    ]
    assert receipt["retained_citations"] == [
        {"citation_id": "safe-2", "source_type": "memory", "title": "Safe cited summary"}
    ]
    serialized = json.dumps(receipt, sort_keys=True).lower()
    for forbidden in (
        "ghp_",
        "github_pat_",
        "sk-1234567890",
        "akia1234567890abcdef".lower(),
        "/home/alice",
        "c:\\users",
        "~/private",
    ):
        assert forbidden not in serialized
