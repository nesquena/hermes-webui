"""Regression tests for model-facing context-history sanitization."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

from api.streaming import (
    _compact_image_parts_for_persistence,
    _compact_session_image_parts_for_persistence,
    _restore_reasoning_metadata,
    _sanitize_messages_for_api,
    _strip_oob_blocks,
)


OOB_BLOCK = (
    "[OUT-OF-BAND USER MESSAGE — consumed steer]\n"
    "internal control text that must not reach the model\n"
    "[/OUT-OF-BAND USER MESSAGE]"
)


def _contains_oob(value) -> bool:
    return "OUT-OF-BAND USER MESSAGE" in json.dumps(value)



def test_compact_image_parts_for_persistence_keeps_text_and_drops_base64():
    image_data_url = "data:image/png;base64," + ("A" * 1_000_000)
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "vision-1"}],
        },
        {
            "role": "tool",
            "tool_call_id": "vision-1",
            "content": [
                {"type": "text", "text": "Image loaded into your context."},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ],
        },
    ]

    copied, changed = _compact_image_parts_for_persistence(messages)

    assert changed == 1
    assert copied[1]["content"] == [
        {"type": "text", "text": "Image loaded into your context."},
        {"type": "text", "text": "[screenshot]"},
    ]
    assert "data:image" not in json.dumps(copied)
    assert copied[0]["tool_calls"] == [{"id": "vision-1"}]
    # Original is not mutated
    assert messages[1]["content"][1]["type"] == "image_url"




def test_compact_image_parts_for_persistence_counts_each_image_part():
    messages = [
        {
            "role": "tool",
            "content": [
                {"type": "text", "text": "Before and after"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                {"type": "input_image", "image_url": {"url": "data:image/png;base64,BBBB"}},
            ],
        }
    ]

    copied, changed = _compact_image_parts_for_persistence(messages)

    assert changed == 2
    assert copied[0]["content"] == [
        {"type": "text", "text": "Before and after"},
        {"type": "text", "text": "[screenshot]"},
        {"type": "text", "text": "[screenshot]"},
    ]


def test_compact_image_parts_for_persistence_preserves_interleaved_non_image_parts():
    document = {"type": "document", "document": {"name": "report.pdf", "id": "doc-1"}}
    scalar = "provider extension payload"
    messages = [
        {
            "role": "tool",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                {"type": "text", "text": "between"},
                document,
                scalar,
                {"type": "input_image", "image_url": {"url": "data:image/png;base64,BBBB"}},
                {"type": "input_text", "content": "after"},
            ],
        }
    ]

    copied, changed = _compact_image_parts_for_persistence(messages)

    assert changed == 2
    assert copied[0]["content"] == [
        {"type": "text", "text": "[screenshot]"},
        {"type": "text", "text": "between"},
        document,
        scalar,
        {"type": "text", "text": "[screenshot]"},
        {"type": "input_text", "content": "after"},
    ]
    assert "data:image" not in json.dumps(copied)
    # Idempotent: a second pass changes nothing
    second_copy, changed2 = _compact_image_parts_for_persistence(copied)
    assert changed2 == 0


def test_compact_image_parts_for_persistence_survives_unhashable_part_type():
    # A JSON-valid part can carry a non-string (unhashable) ``type`` such as a
    # list or dict. ``part.get("type") in {...}`` would raise TypeError on an
    # unhashable value, turning an otherwise-complete streaming send into the
    # error path before the session is saved. Such parts must be preserved
    # unchanged, and real image parts alongside them must still compact.
    list_type = {"type": ["provider-extension"], "data": "x"}
    dict_type = {"type": {"nested": "kind"}, "data": "y"}
    messages = [
        {
            "role": "tool",
            "content": [
                list_type,
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                dict_type,
            ],
        }
    ]

    copied, changed = _compact_image_parts_for_persistence(messages)

    assert changed == 1
    assert copied[0]["content"] == [
        list_type,
        {"type": "text", "text": "[screenshot]"},
        dict_type,
    ]
    assert "data:image" not in json.dumps(copied)
    # Idempotent: a second pass changes nothing and still does not raise.
    second_copy, changed2 = _compact_image_parts_for_persistence(copied)
    assert changed2 == 0


def test_compact_session_image_parts_for_persistence_compacts_both_histories():
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + ("A" * 4096)}}
    session = SimpleNamespace(
        session_id="vision-turn",
        messages=[{"role": "tool", "content": [{"type": "text", "text": "display"}, image.copy()]}],
        context_messages=[{"role": "tool", "content": [{"type": "text", "text": "context"}, image.copy()]}],
    )

    changed = _compact_session_image_parts_for_persistence(session)

    assert changed == 2
    assert session.messages[0]["content"] == [
        {"type": "text", "text": "display"},
        {"type": "text", "text": "[screenshot]"},
    ]
    assert session.context_messages[0]["content"] == [
        {"type": "text", "text": "context"},
        {"type": "text", "text": "[screenshot]"},
    ]


def test_compact_image_parts_for_persistence_keeps_user_image_attachment():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Inspect this attachment"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }
    ]

    copied, changed = _compact_image_parts_for_persistence(messages)

    assert changed == 0
    assert copied[0]["content"][1]["type"] == "image_url"
    # Original is not mutated
    assert messages[0]["content"][1]["type"] == "image_url"


def test_compact_image_parts_for_persistence_leaves_text_history_unchanged():
    messages = [{"role": "tool", "content": "plain tool output"}]

    copied, changed = _compact_image_parts_for_persistence(messages)

    assert changed == 0
    assert copied == [{"role": "tool", "content": "plain tool output"}]
    # Original is not mutated
    assert messages == [{"role": "tool", "content": "plain tool output"}]


def test_strip_oob_blocks_string():
    content = f"visible before\n{OOB_BLOCK}\nvisible after"

    cleaned = _strip_oob_blocks(content)

    assert "visible before" in cleaned
    assert "visible after" in cleaned
    assert "internal control text" not in cleaned
    assert "OUT-OF-BAND USER MESSAGE" not in cleaned


def test_strip_oob_blocks_list_content():
    content = [
        {"type": "text", "text": f"keep\n{OOB_BLOCK}\nkeep later"},
        {"type": "input_text", "content": f"prefix\n{OOB_BLOCK}\nsuffix"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
    ]

    cleaned = _strip_oob_blocks(content)

    assert cleaned[0]["text"].startswith("keep")
    assert "keep later" in cleaned[0]["text"]
    assert "suffix" in cleaned[1]["content"]
    assert cleaned[2] == content[2]
    assert not _contains_oob(cleaned)


def test_sanitize_messages_for_api_no_oob_in_output():
    messages = [
        {"role": "user", "content": f"question\n{OOB_BLOCK}\nvisible question"},
        {
            "role": "assistant",
            "content": f"answer\n{OOB_BLOCK}\nvisible answer",
            "reasoning": "display-only reasoning",
            "thinking": "display-only thinking",
            "_reasoning": "display-only private reasoning",
            "reasoning_content": "provider-facing reasoning metadata",
        },
    ]

    sanitized = _sanitize_messages_for_api(messages)

    assert not _contains_oob(sanitized)
    assert sanitized[0]["content"].startswith("question")
    assert "visible question" in sanitized[0]["content"]
    assert "visible answer" in sanitized[1]["content"]
    assert "reasoning" not in sanitized[1]
    assert "thinking" not in sanitized[1]
    assert "_reasoning" not in sanitized[1]
    assert sanitized[1]["reasoning_content"] == "provider-facing reasoning metadata"


def test_sanitize_messages_for_api_preserves_tool_chains():
    messages = [
        {"role": "user", "content": f"please inspect\n{OOB_BLOCK}\nvisible request"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "type": "function",
                    "id": "call-1",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": f"tool output\n{OOB_BLOCK}\nresult"},
        {"role": "assistant", "content": "done"},
    ]

    sanitized = _sanitize_messages_for_api(messages)

    assert [msg["role"] for msg in sanitized] == ["user", "assistant", "tool", "assistant"]
    assert sanitized[1]["tool_calls"][0]["id"] == "call-1"
    assert sanitized[2]["tool_call_id"] == "call-1"
    assert "result" in sanitized[2]["content"]
    assert not _contains_oob(sanitized)


def test_sanitize_messages_drops_empty_tool_calls_array():
    """#5737: strict providers (DeepSeek v4, newer OpenAI) reject an assistant
    message carrying `tool_calls: []` with HTTP 400. A stored assistant message
    can literally have an empty list (not None, not missing), which the orphan
    linker doesn't catch. The sanitizer must drop the empty key while leaving a
    populated tool_calls chain intact."""
    messages = [
        {"role": "user", "content": "hi"},
        # Empty tool_calls: [] must be dropped (would 400 on strict providers).
        {"role": "assistant", "content": "thinking out loud", "tool_calls": []},
        {"role": "user", "content": "go on"},
        # Populated tool_calls (+ its tool result) must be preserved untouched.
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"type": "function", "id": "call-9",
                 "function": {"name": "terminal", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call-9", "content": "ok"},
    ]

    sanitized = _sanitize_messages_for_api(messages)

    # The empty-tool_calls assistant message survives, but WITHOUT the key.
    empty_asst = next(m for m in sanitized if m.get("content") == "thinking out loud")
    assert empty_asst["role"] == "assistant"
    assert "tool_calls" not in empty_asst, "empty tool_calls: [] must be dropped (strict-provider 400)"
    # The populated chain is preserved.
    populated = next(m for m in sanitized if m.get("role") == "assistant" and m.get("tool_calls"))
    assert populated["tool_calls"][0]["id"] == "call-9"
    assert any(m.get("tool_call_id") == "call-9" for m in sanitized)
    # No message that reaches the API carries an empty tool_calls array.
    for m in sanitized:
        assert not ("tool_calls" in m and not m["tool_calls"]), \
            "no message may ship an empty tool_calls array"


def test_gateway_conversation_history_no_oob():
    from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT
    from api.gateway_chat import _STREAM_RUN_IDS, _run_gateway_runs_api_streaming

    requests = []
    stream_id = "stream-oob-sanitization"
    STREAM_PARTIAL_TEXT[stream_id] = ""
    STREAM_REASONING_TEXT[stream_id] = ""

    class _JsonResponse:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode("utf-8")

        def read(self, _limit=None):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class _SseResponse:
        def __iter__(self):
            return iter([
                b'data: {"event":"run.completed","output":"done","usage":{"input_tokens":1,"output_tokens":1}}\n',
                b'\n',
            ])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_urlopen(req, *, timeout=None):
        requests.append(req)
        if req.full_url.endswith("/v1/runs"):
            return _JsonResponse({"run_id": "run-oob"})
        return _SseResponse()

    session = SimpleNamespace(context_messages=[
        {"role": "system", "content": f"ignored system\n{OOB_BLOCK}"},
        {"role": "user", "content": f"history request\n{OOB_BLOCK}\nvisible request"},
        {"role": "assistant", "content": [{"type": "text", "text": f"history reply\n{OOB_BLOCK}\nvisible reply"}]},
        {"role": "tool", "tool_call_id": "call-ignored", "content": f"ignored\n{OOB_BLOCK}"},
    ])

    try:
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            final_text, usage = _run_gateway_runs_api_streaming(
                session_id="sess-oob",
                msg_text="current user",
                model="test-model",
                workspace="/tmp",
                stream_id=stream_id,
                base_url="http://gw:8642",
                api_key="secret",
                prefill_messages=[],
                body_extras={},
                put_gateway_event=lambda *_args, **_kwargs: None,
                cancel_event=threading.Event(),
                session=session,
            )
    finally:
        STREAM_PARTIAL_TEXT.pop(stream_id, None)
        STREAM_REASONING_TEXT.pop(stream_id, None)
        _STREAM_RUN_IDS.pop(stream_id, None)

    run_body = json.loads(requests[0].data.decode("utf-8"))
    assert final_text == "done"
    assert usage["input_tokens"] == 1
    assert usage["output_tokens"] == 1
    assert not _contains_oob(run_body["conversation_history"])
    assert run_body["conversation_history"] == [
        {"role": "user", "content": "history request\n\nvisible request"},
        {"role": "assistant", "content": [{"type": "text", "text": "history reply\n\nvisible reply"}]},
    ]


# ── Regex variant coverage tests ─────────────────────────────────────────────

OOB_HYPHEN = (
    "[OUT-OF-BAND USER MESSAGE - consumed steer]\n"
    "internal control text that must not reach the model\n"
    "[/OUT-OF-BAND USER MESSAGE]"
)


def test_strip_oob_blocks_hyphen_marker():
    """Hyphen variant of opening marker should be stripped."""
    content = f"visible before\n{OOB_HYPHEN}\nvisible after"

    cleaned = _strip_oob_blocks(content)

    assert "visible before" in cleaned
    assert "visible after" in cleaned
    assert "internal control text" not in cleaned
    assert "OUT-OF-BAND USER MESSAGE" not in cleaned


OOB_CRLF = (
    "[OUT-OF-BAND USER MESSAGE — consumed steer]\r\n"
    "internal control text that must not reach the model\r\n"
    "[/OUT-OF-BAND USER MESSAGE]"
)


def test_strip_oob_blocks_crlf_line_endings():
    """Windows CRLF line endings should be handled."""
    content = f"visible before\r\n{OOB_CRLF}\r\nvisible after"

    cleaned = _strip_oob_blocks(content)

    assert "visible before" in cleaned
    assert "visible after" in cleaned
    assert "internal control text" not in cleaned
    assert "OUT-OF-BAND USER MESSAGE" not in cleaned


OOB_NO_SUFFIX = (
    "[OUT-OF-BAND USER MESSAGE]\n"
    "internal control text that must not reach the model\n"
    "[/OUT-OF-BAND USER MESSAGE]"
)


def test_strip_oob_blocks_no_suffix_marker():
    """Opening marker without dash/suffix should be stripped."""
    content = f"visible before\n{OOB_NO_SUFFIX}\nvisible after"

    cleaned = _strip_oob_blocks(content)

    assert "visible before" in cleaned
    assert "visible after" in cleaned
    assert "internal control text" not in cleaned
    assert "OUT-OF-BAND USER MESSAGE" not in cleaned


def test_strip_oob_blocks_multiple_in_one_string():
    """Multiple OOB blocks in a single string should all be stripped."""
    content = (
        f"before {OOB_BLOCK} middle {OOB_HYPHEN} end"
    )

    cleaned = _strip_oob_blocks(content)

    assert "before" in cleaned
    assert "middle" in cleaned
    assert "end" in cleaned
    # Count occurrences — should be zero after stripping
    assert cleaned.count("OUT-OF-BAND USER MESSAGE") == 0


def test_strip_oob_blocks_incomplete_block_preserved():
    """Incomplete/unclosed OOB blocks should NOT be stripped."""
    content = "visible before\n[OUT-OF-BAND USER MESSAGE — incomplete"

    cleaned = _strip_oob_blocks(content)

    # Incomplete block should remain untouched
    assert "[OUT-OF-BAND USER MESSAGE" in cleaned


def test_restore_metadata_aligns_row_with_empty_tool_calls():
    """#5737 third site: _restore_reasoning_metadata aligns previous vs updated
    rows by their API-safe projection. A row stored with tool_calls: [] must
    project identically on both sides (empty key dropped) so its reasoning /
    stable id / timestamp still carry forward — otherwise the projections diverge
    and the row silently loses its metadata on every turn."""
    previous = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "answer",
            "tool_calls": [],            # empty — the strict-provider case
            "reasoning": "prior reasoning",
            "id": "msg-42",
            "timestamp": 1234,
        },
    ]
    # The agent echoes the row back WITHOUT our metadata (and without tool_calls).
    updated = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "answer"},
    ]

    restored = _restore_reasoning_metadata(previous, updated)

    asst = restored[1]
    assert asst["reasoning"] == "prior reasoning", "reasoning must carry forward across the empty-tool_calls row"
    assert asst["id"] == "msg-42", "stable id must carry forward"
    assert asst["timestamp"] == 1234, "timestamp must carry forward (row must not re-mint / drift)"
