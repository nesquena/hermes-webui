"""
Regression coverage for shared compression-anchor visibility helpers (#2028).
"""

from pathlib import Path

from api.compression_anchor import visible_messages_for_anchor


def test_legacy_duplicate_anchor_helpers_are_removed():
    routes_src = Path("api/routes.py").read_text(encoding="utf-8")
    streaming_src = Path("api/streaming.py").read_text(encoding="utf-8")

    assert "def _visible_messages_for_anchor" not in routes_src
    assert "def _visible_messages_for_compression_anchor" not in streaming_src
    assert "visible_messages_for_anchor(compressed, auto_compression=False)" in routes_src
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
