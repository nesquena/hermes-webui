"""
Regression coverage for shared compression-anchor visibility helpers (#2028).
"""

from pathlib import Path

from api.compression_anchor import is_context_compression_marker, visible_messages_for_anchor
from api.streaming import _compression_summary_from_messages, _is_context_compression_marker


def test_legacy_duplicate_anchor_helpers_are_removed():
    routes_src = Path("api/routes.py").read_text(encoding="utf-8")
    streaming_src = Path("api/streaming.py").read_text(encoding="utf-8")

    assert "def _visible_messages_for_anchor" not in routes_src
    assert "def _visible_messages_for_compression_anchor" not in streaming_src
    assert "visible_messages_for_anchor(s.messages, auto_compression=False)" in routes_src
    assert "visible_messages_for_anchor(s.messages, auto_compression=True)" in streaming_src


def test_visible_messages_for_anchor_preserves_manual_text_part_filter():
    text_only = {"role": "assistant", "content": [{"type": "text", "text": "Visible"}]}
    input_only = {"role": "assistant", "content": [{"type": "input_text", "text": "Model input"}]}
    reasoning_only = {"role": "assistant", "content": [{"type": "thinking", "text": "hidden"}]}
    tool_use_only = {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1"}]}
    tool_message = {"role": "tool", "content": "tool output"}

    assert visible_messages_for_anchor(
        [text_only, input_only, reasoning_only, tool_use_only, tool_message],
        auto_compression=False,
    ) == [text_only, reasoning_only, tool_use_only]


def test_visible_messages_for_anchor_preserves_auto_compression_text_part_filter():
    text_only = {"role": "assistant", "content": [{"type": "text", "text": "Visible"}]}
    input_only = {"role": "assistant", "content": [{"type": "input_text", "text": "Model input"}]}
    output_only = {"role": "assistant", "content": [{"type": "output_text", "text": "Model output"}]}
    reasoning_only = {"role": "assistant", "content": [{"type": "reasoning", "text": "hidden"}]}
    tool_message = {"role": "tool", "content": "tool output"}

    assert visible_messages_for_anchor(
        [text_only, input_only, output_only, reasoning_only, tool_message],
        auto_compression=True,
    ) == [text_only, input_only, output_only, reasoning_only]


def test_visible_messages_for_anchor_keeps_manual_user_messages_simple():
    user_tool_metadata = {"role": "user", "content": [], "tool_calls": [{"id": "call_1"}]}
    user_attachment = {"role": "user", "content": [], "attachments": [{"name": "screenshot.png"}]}
    assistant_tool_metadata = {"role": "assistant", "content": [], "tool_calls": [{"id": "call_2"}]}

    assert visible_messages_for_anchor(
        [user_tool_metadata, user_attachment, assistant_tool_metadata],
        auto_compression=False,
    ) == [user_attachment, assistant_tool_metadata]

    assert visible_messages_for_anchor(
        [user_tool_metadata, user_attachment, assistant_tool_metadata],
        auto_compression=True,
    ) == [user_tool_metadata, user_attachment, assistant_tool_metadata]


def test_context_compression_marker_detection_is_prefix_and_role_scoped():
    real_marker = {
        "role": "assistant",
        "content": "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted.",
    }
    preserved_tasks_marker = {
        "role": "user",
        "content": "[Your active task list was preserved across context compression] - [ ] follow up",
    }
    tool_noise = {
        "role": "tool",
        "content": "{\"description\": \"Troubleshoot frequent context compression indicators\"}",
    }
    user_discussion = {
        "role": "user",
        "content": "Why do I see context compression after every message?",
    }

    assert is_context_compression_marker(real_marker)
    assert is_context_compression_marker(preserved_tasks_marker)
    assert _is_context_compression_marker(real_marker)
    assert not is_context_compression_marker(tool_noise)
    assert not is_context_compression_marker(user_discussion)


def test_compression_summary_ignores_tool_output_that_mentions_compression():
    marker = {
        "role": "assistant",
        "content": "[CONTEXT COMPACTION — REFERENCE ONLY] Keep this handoff as reference.",
    }
    skill_tool_output = {
        "role": "tool",
        "content": "{\"name\": \"hermes-webui-operations\", \"content\": \"Troubleshooting frequent context compression indicators...\"}",
    }

    assert _compression_summary_from_messages([marker, skill_tool_output]) == marker["content"]
    assert _compression_summary_from_messages([skill_tool_output]) is None


def test_marker_based_anchor_calculation():
    """Verify that the marker-based anchor logic used in streaming.py's done
    handler correctly computes compression_anchor_visible_idx from the last
    [CONTEXT COMPACTION] marker position. This prevents the reference card
    from being pushed behind the render window after subsequent turns."""
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "first reply"},
        {"role": "tool", "content": "tool output"},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "second reply"},
        {"role": "assistant", "content": "[CONTEXT COMPACTION \u2014 REFERENCE ONLY] summary"},
        {"role": "user", "content": "third question"},
        {"role": "assistant", "content": "third reply"},
        {"role": "tool", "content": "more tool output"},
        {"role": "user", "content": "fourth question"},
        {"role": "assistant", "content": "fourth reply"},
    ]

    # Simulate the fix logic from streaming.py
    _last_marker_raw_idx = None
    for _mi, _m in enumerate(messages):
        if is_context_compression_marker(_m):
            _last_marker_raw_idx = _mi
    assert _last_marker_raw_idx == 5, "expected marker at index 5"

    _visible_before_marker = visible_messages_for_anchor(
        messages[:_last_marker_raw_idx], auto_compression=True,
    )
    anchor = max(0, len(_visible_before_marker) - 1)
    # Visible before marker: 4 messages (hello, first reply, second question, second reply)
    # tool at index 2 is filtered out. Anchor = 3 (last visible before marker)
    assert anchor == 3, f"expected anchor 3, got {anchor}"

    # Verify the anchor points to the right message
    full_vis = visible_messages_for_anchor(messages, auto_compression=True)
    # full_vis: hello, first reply, second question, second reply, third question, third reply, fourth question, fourth reply = 8
    assert len(full_vis) == 8, f"expected 8, got {len(full_vis)}"
    assert full_vis[anchor]["content"] == "second reply"

    # After additional turns, anchor stays at marker boundary
    messages_extended = messages + [
        {"role": "user", "content": "fifth question"},
        {"role": "assistant", "content": "fifth reply"},
        {"role": "tool", "content": "hidden"},
    ]
    full_vis_ext = visible_messages_for_anchor(messages_extended, auto_compression=True)
    # The anchor should still point to the same position in full_vis_ext
    assert full_vis_ext[anchor]["content"] == "second reply"
    assert anchor < len(full_vis_ext) - 3  # anchor is not at the end


def test_marker_based_anchor_multiple_compressions():
    """With multiple compression markers, the anchor uses the LAST marker."""
    messages = [
        {"role": "user", "content": "A"},
        {"role": "assistant", "content": "B"},
        {"role": "assistant", "content": "[CONTEXT COMPACTION \u2014 REFERENCE ONLY] first"},
        {"role": "user", "content": "C"},
        {"role": "assistant", "content": "D"},
        {"role": "assistant", "content": "[CONTEXT COMPACTION \u2014 REFERENCE ONLY] second"},
        {"role": "user", "content": "E"},
        {"role": "assistant", "content": "F"},
    ]
    # Find last marker
    _last_marker_raw_idx = None
    for _mi, _m in enumerate(messages):
        if is_context_compression_marker(_m):
            _last_marker_raw_idx = _mi
    assert _last_marker_raw_idx == 5

    _visible_before_marker = visible_messages_for_anchor(
        messages[:_last_marker_raw_idx], auto_compression=True,
    )
    anchor = max(0, len(_visible_before_marker) - 1)
    # Visible before last marker at raw[5]: messages[0:5] = [A, B, marker, C, D]
    # After filtering: A, B, C, D = 4 visible. Anchor = 3 (last visible = D)
    assert anchor == 3, f"expected anchor 3, got {anchor}"
    assert _visible_before_marker[anchor]["content"] == "D"


def test_marker_based_anchor_fallback_when_no_marker():
    """When no compression marker exists, fall back to old behavior."""
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "reply"},
    ]
    _last_marker_raw_idx = None
    for _mi, _m in enumerate(messages):
        if is_context_compression_marker(_m):
            _last_marker_raw_idx = _mi
    assert _last_marker_raw_idx is None

    # Should fall through (not crash) - verified by the old path
    visible_after = visible_messages_for_anchor(messages, auto_compression=True)
    assert len(visible_after) > 0
