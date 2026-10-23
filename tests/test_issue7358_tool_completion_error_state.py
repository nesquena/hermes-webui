import json

from api import streaming


def _structured_failure_detector(_tool_name, result):
    data = json.loads(result)
    failed = bool(
        isinstance(data, dict)
        and (
            data.get("success") is False
            or data.get("ok") is False
            or data.get("status") in {"error", "failed"}
        )
    )
    return failed, " [error]" if failed else ""


def test_object_and_json_string_results_use_the_same_canonical_failure_contract():
    payload = {"success": False, "error": "HTTP 433: usage allowance exceeded"}

    assert streaming._canonical_tool_result_is_error(
        "web_search", payload, detector=_structured_failure_detector
    ) is True
    assert streaming._canonical_tool_result_is_error(
        "web_search", json.dumps(payload), detector=_structured_failure_detector
    ) is True


def test_successful_result_with_error_like_prose_is_not_misclassified():
    payload = {"success": True, "output": "The error budget is healthy"}

    assert streaming._canonical_tool_result_is_error(
        "status", payload, detector=_structured_failure_detector
    ) is False


def test_classifier_failure_does_not_break_tool_completion():
    def unavailable_classifier(_tool_name, _result):
        raise ImportError("agent classifier unavailable")

    assert streaming._canonical_tool_result_is_error(
        "status", {"success": False}, detector=unavailable_classifier
    ) is False


def test_settle_live_tool_completion_updates_only_the_matching_call():
    calls = [
        {"name": "search", "tid": "call-1", "done": False},
        {"name": "search", "tid": "call-2", "done": False},
    ]

    assert streaming._settle_live_tool_completion(
        calls,
        tool_call_id="call-1",
        name="search",
        snippet="HTTP 433",
        is_error=True,
    ) is True
    assert calls == [
        {
            "name": "search",
            "tid": "call-1",
            "done": True,
            "snippet": "HTTP 433",
            "is_error": True,
        },
        {"name": "search", "tid": "call-2", "done": False},
    ]


def test_persisted_tool_summary_keeps_structured_completion_error_state():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "search", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": '{"success": false, "error": "HTTP 433"}',
        },
    ]

    summaries = streaming._extract_tool_calls_from_messages(
        messages,
        live_tool_calls=[
            {
                "name": "search",
                "tid": "call-1",
                "done": True,
                "is_error": True,
            }
        ],
    )

    assert summaries[0]["is_error"] is True


def test_live_result_preview_remains_bounded_and_redacted():
    preview = streaming._tool_result_snippet(
        {"error": "API_KEY=result-secret-123 " + ("x" * 3000)},
        limit=80,
    )

    assert len(preview) <= 80
    assert "result-secret-123" not in preview
    assert "API_KEY=" in preview
