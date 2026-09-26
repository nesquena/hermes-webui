"""
Regression coverage for shared compression-anchor visibility helpers (#2028).
"""

from pathlib import Path

import pytest

from api.compression_anchor import (
    is_context_compression_marker,
    is_lcm_context_recovery_marker,
    visible_messages_for_anchor,
)
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


@pytest.mark.parametrize("role", ["user", "assistant"])
@pytest.mark.parametrize("as_list", [False, True])
@pytest.mark.parametrize(
    "heading",
    [
        "[Recent Summary (d0, node 418)]",
        "[Current user objective preserved from compacted history]",
    ],
)
@pytest.mark.parametrize(
    "actual_marker",
    [
        None,
        "[CONTEXT COMPACTION — REFERENCE ONLY] actual compacted history",
        "[SESSION ARC SUMMARY — REFERENCE ONLY] actual session arc",
    ],
)
def test_compression_summary_skips_lcm_envelopes(role, as_list, heading, actual_marker):
    envelope_text = f"{heading}\nPrivate recovered context"
    envelope_content = (
        [{"type": "input_text", "text": envelope_text}]
        if as_list
        else envelope_text
    )
    messages = [
        {"role": "user", "content": "Regular user message"},
        {"role": role, "content": envelope_content},
        {"role": "assistant", "content": "Regular assistant message"},
    ]

    expected = None
    if actual_marker:
        messages.append({"role": "assistant", "content": actual_marker})
        expected = actual_marker

    summary = _compression_summary_from_messages(messages)

    assert summary == expected
    assert summary not in {
        "[Recent Summary (d0, node 418)]",
        "[Current user objective preserved from compacted history]",
    }


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




@pytest.mark.parametrize("role", ["user", "assistant"])
@pytest.mark.parametrize(
    "marker",
    [
        "[Recent Summary (d0, node 418)]\n...",
        "[Current user objective preserved from compacted history]\n...",
    ],
)
def test_lcm_recovery_markers_are_detected_for_non_tool_roles(role, marker):
    message = {"role": role, "content": marker}

    assert is_context_compression_marker(message)
    assert is_lcm_context_recovery_marker(message)
    assert _is_context_compression_marker(message)


@pytest.mark.parametrize(
    "marker",
    [
        "[Recent Summary (d0, node 418)]",
        "[Current user objective preserved from compacted history]",
    ],
)
def test_lcm_recovery_marker_active_turn_token_is_not_classified(marker):
    token = "stream-01f4c8d2:1779348286.3954952"
    current_user = {
        "role": "user",
        "content": marker,
        "_active_turn_token": token,
    }
    recovery_envelope = {"role": "user", "content": marker}

    assert not is_lcm_context_recovery_marker(current_user)
    assert not is_context_compression_marker(current_user)
    assert is_lcm_context_recovery_marker(recovery_envelope)
    assert is_context_compression_marker(recovery_envelope)


def test_token_owned_assistant_lcm_text_survives_display_projection():
    from types import SimpleNamespace
    from api.models import reconciled_state_db_messages_for_session

    partial = {
        "role": "assistant",
        "content": "[Recent Summary (d0, node 418)]\npartial output",
        "_active_turn_token": "stream:123",
    }
    session = SimpleNamespace(messages=[partial], context_messages=[partial])

    assert not is_lcm_context_recovery_marker(partial)
    assert reconciled_state_db_messages_for_session(session, state_messages=[]) == [partial]
    assert is_lcm_context_recovery_marker({"role": "assistant", "content": partial["content"]})


@pytest.mark.parametrize("role", ["system", "custom", None, ["user"]])
def test_lcm_recovery_marker_requires_hashable_provider_role(role):
    from api.streaming import _deduplicate_context_messages

    message = {
        "role": role,
        "content": "[Recent Summary (d0, node 418)]",
    }

    assert not is_lcm_context_recovery_marker(message)
    assert _deduplicate_context_messages([message, dict(message)]) == [message]


@pytest.mark.parametrize(
    "heading",
    [
        "[Recent Summary (d0, node 418)]",
        "[Current user objective preserved from compacted history]",
    ],
)
def test_lcm_recovery_marker_requires_heading_boundary(heading):
    assert not is_lcm_context_recovery_marker({
        "role": "user",
        "content": f"{heading}suffix",
    })
    assert is_lcm_context_recovery_marker({
        "role": "user",
        "content": f"{heading}\nsummary text",
    })
    assert is_lcm_context_recovery_marker({
        "role": "user",
        "content": f"{heading} summary text",
    })


def test_lcm_recovery_markers_keep_conservative_and_list_content_rules():
    markers = [
        "[Recent Summary (d0, node 418)]\n...",
        "[Current user objective preserved from compacted history]\n...",
    ]

    assert not is_context_compression_marker({
        "role": "user",
        "content": "[Recent Summary (Q1 meeting notes)]",
    })

    for marker in markers:
        tool_message = {"role": "tool", "content": marker}
        assert not is_context_compression_marker(tool_message)
        assert not is_lcm_context_recovery_marker(tool_message)

        for part_type in ("text", "input_text", "output_text"):
            assert is_context_compression_marker({
                "role": "user",
                "content": [{"type": part_type, "text": marker}],
            })


@pytest.mark.parametrize('part_type', ['input_text', 'output_text'])
@pytest.mark.parametrize('payload_key', ['text', 'typed'])
@pytest.mark.parametrize('role', ['user', 'assistant'])
def test_lcm_type_named_payload_detection_and_display(part_type, payload_key, role):
    from types import SimpleNamespace
    from api.models import reconciled_state_db_messages_for_session

    heading = '[Recent Summary (d0, node 418)]'
    key = part_type if payload_key == 'typed' else payload_key
    marker = {'role': role, 'content': [{'type': part_type, key: heading}]}
    answer = {'role': 'assistant', 'content': 'Answer'}
    session = SimpleNamespace(messages=[marker, answer], context_messages=[marker, answer])
    assert is_lcm_context_recovery_marker(marker)
    assert reconciled_state_db_messages_for_session(session, state_messages=[]) == [answer]
    assert reconciled_state_db_messages_for_session(session, prefer_context=True, state_messages=[]) == [marker, answer]
    assert not is_lcm_context_recovery_marker({
        'role': role, 'content': [{'type': 'image', key: heading}],
    })


@pytest.mark.parametrize('part_type', [[], {}, 1, None])
def test_malformed_marker_part_keeps_answer_retrievable(part_type):
    malformed = dict(role='assistant', content=[dict(type=part_type, text='[Recent Summary (d0, node 418)]')])
    answer = dict(role='assistant', content='Actual answer')
    assert not is_lcm_context_recovery_marker(malformed)
    assert answer in visible_messages_for_anchor([malformed, answer], auto_compression=True)
