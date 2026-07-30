from api.streaming import (
    _deduplicate_context_messages,
    _is_context_compression_marker,
    _merge_display_messages_after_agent_result,
)


MARKER = "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted."


def test_display_merge_drops_internal_compaction_markers_from_visible_delta():
    previous_display = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    previous_context = list(previous_display)
    result_messages = [
        {"role": "assistant", "content": MARKER, "_compressed_summary": True},
        {"role": "user", "content": MARKER, "_compressed_summary": True},
        {"role": "user", "content": "latest question"},
        {"role": "assistant", "content": "latest answer"},
    ]

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        "latest question",
    )

    assert [m["content"] for m in merged] == [
        "old question",
        "old answer",
        "latest question",
        "latest answer",
    ]
    assert not any(_is_context_compression_marker(m) for m in merged)


def test_context_dedup_keeps_one_reference_marker_without_user_role_duplicate():
    context = _deduplicate_context_messages([
        {"role": "assistant", "content": MARKER, "_compressed_summary": True},
        {"role": "user", "content": MARKER, "_compressed_summary": True},
        {"role": "user", "content": "latest question"},
    ])

    markers = [m for m in context if _is_context_compression_marker(m)]
    assert len(markers) == 1
    assert markers[0]["role"] == "assistant"
    assert [m["content"] for m in context if m.get("role") == "user"] == ["latest question"]


def test_real_user_marker_prefix_text_is_preserved():
    prompt = "context compaction is broken; explain what happened"
    previous_display = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    previous_context = list(previous_display)
    result_messages = [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": "It was treated as an internal marker."},
    ]

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        prompt,
    )
    context = _deduplicate_context_messages(result_messages)

    assert not _is_context_compression_marker(result_messages[0])
    assert [m["content"] for m in merged] == [
        "old question",
        "old answer",
        prompt,
        "It was treated as an internal marker.",
    ]
    assert context[0]["role"] == "user"
    assert context[0]["content"] == prompt


def test_unbracketed_marker_requires_internal_metadata():
    marker_text = "context compaction summary for internal recovery"

    assert not _is_context_compression_marker({
        "role": "user",
        "content": marker_text,
    })
    assert _is_context_compression_marker({
        "role": "assistant",
        "content": marker_text,
        "_compressed_summary": True,
    })
