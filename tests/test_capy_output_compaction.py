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
    assert ".../static/spaces.js:10 ok" in receipt["text"]
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
