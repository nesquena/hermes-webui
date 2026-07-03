"""Branch must not inherit full parent context_messages (#5096 A)."""

import copy

from api.session_ops import truncate_context_for_display_keep


def test_truncate_context_for_display_keep_drops_parent_tail():
    msgs = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "after-fork"},
    ]
    # Same-length context as display — fork at keep=2 matches messages[:2]
    ctx = copy.deepcopy(msgs)
    out = truncate_context_for_display_keep(ctx, msgs, 2)
    assert len(out) == 2
    assert out[-1]["content"] == "a1"
    assert "after-fork" not in [m["content"] for m in out]


def test_truncate_context_preserves_leading_compaction_row():
    msgs = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    ctx = [
        {"role": "user", "content": "compaction-ref-only"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    out = truncate_context_for_display_keep(ctx, msgs, 2)
    assert len(out) == 3
    assert out[0]["content"] == "compaction-ref-only"


def test_truncate_context_for_display_keep_drops_interleaved_tool_tail():
    msgs = [
        {"role": "user", "content": "u1", "id": "u1", "timestamp": 1.0},
        {"role": "assistant", "content": "a1", "id": "a1", "timestamp": 2.0},
        {"role": "user", "content": "u2", "id": "u2", "timestamp": 3.0},
        {"role": "assistant", "content": "a2", "id": "a2", "timestamp": 4.0},
    ]
    ctx = [
        {"role": "user", "content": "u1", "id": "u1", "timestamp": 1.0},
        {"role": "assistant", "content": "a1", "id": "a1", "timestamp": 2.0},
        {"role": "user", "content": "u2", "id": "u2", "timestamp": 3.0},
        {"role": "assistant", "content": "tool-call-after-cut", "id": "tool-call-after-cut"},
        {"role": "tool", "content": "tool-result-after-cut", "id": "tool-result-after-cut"},
        {"role": "assistant", "content": "a2", "id": "a2", "timestamp": 4.0},
    ]
    out = truncate_context_for_display_keep(ctx, msgs, 2)
    contents = [m["content"] for m in out]
    assert contents == ["u1", "a1"]
    assert "u2" not in contents
    assert "tool-call-after-cut" not in contents
    assert "tool-result-after-cut" not in contents


def test_truncate_context_for_display_keep_keeps_tool_rows_before_next_display_anchor():
    msgs = [
        {"role": "user", "content": "u1", "id": "u1", "timestamp": 1.0},
        {"role": "assistant", "content": "a1", "id": "a1", "timestamp": 2.0},
        {"role": "user", "content": "u2", "id": "u2", "timestamp": 3.0},
        {"role": "assistant", "content": "a2", "id": "a2", "timestamp": 4.0},
    ]
    ctx = [
        {"role": "user", "content": "u1", "id": "u1", "timestamp": 1.0},
        {"role": "assistant", "content": "a1", "id": "a1", "timestamp": 2.0},
        {"role": "assistant", "content": "assistant-think", "id": "a1-think"},
        {"role": "assistant", "content": "assistant-tool-result", "id": "a1-tool", "timestamp": 2.5},
        {"role": "user", "content": "u2", "id": "u2", "timestamp": 3.0},
        {"role": "assistant", "content": "a2", "id": "a2", "timestamp": 4.0},
    ]
    out = truncate_context_for_display_keep(ctx, msgs, 2)
    contents = [m["content"] for m in out]
    assert contents == [
        "u1",
        "a1",
        "assistant-think",
        "assistant-tool-result",
    ]
    assert contents[-1] != "u2"


def test_truncate_context_for_display_keep_drops_unkept_tool_rows_after_user_boundary():
    msgs = [
        {"role": "user", "content": "u1", "id": "u1", "timestamp": 1.0},
        {"role": "assistant", "content": "a1", "id": "a1", "timestamp": 2.0},
        {"role": "user", "content": "u2", "id": "u2", "timestamp": 3.0},
        {"role": "assistant", "content": "a2", "id": "a2", "timestamp": 4.0},
    ]
    ctx = [
        {"role": "user", "content": "u1", "id": "u1", "timestamp": 1.0},
        {"role": "assistant", "content": "a1", "id": "a1", "timestamp": 2.0},
        {"role": "user", "content": "u2", "id": "u2", "timestamp": 3.0},
        {"role": "assistant", "content": "tool-call-after-user-keep", "id": "tool-call-after-user-keep"},
        {"role": "tool", "content": "tool-result-after-user-keep", "id": "tool-result-after-user-keep"},
        {"role": "assistant", "content": "a2", "id": "a2", "timestamp": 4.0},
    ]
    out = truncate_context_for_display_keep(ctx, msgs, 3)
    contents = [m["content"] for m in out]
    assert contents == ["u1", "a1", "u2"]
    assert "tool-call-after-user-keep" not in contents
    assert "tool-result-after-user-keep" not in contents
    assert "a2" not in contents


def test_truncate_context_for_display_keep_prefers_compact_summary_fallback():
    msgs = [
        {"role": "user", "content": "u1", "id": "u1", "timestamp": 1.0},
        {"role": "assistant", "content": "a1", "id": "a1", "timestamp": 2.0},
        {"role": "user", "content": "u2", "id": "u2", "timestamp": 3.0},
        {"role": "assistant", "content": "a2", "id": "a2", "timestamp": 4.0},
    ]
    ctx = [
        {"role": "user", "content": "compact", "id": "summary"},
        {"role": "user", "content": "u1", "id": "u1", "timestamp": 1.0},
        {"role": "assistant", "content": "a1", "id": "a1", "timestamp": 2.0},
    ]
    out = truncate_context_for_display_keep(ctx, msgs, 2)
    assert out == ctx[:2]


def test_truncate_context_for_display_keep_keeps_real_user_turn_when_duplicate_rows_lack_identity():
    msgs = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    ctx = [
        {"role": "user", "content": "u1"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    out = truncate_context_for_display_keep(ctx, msgs, 1)
    assert [row["content"] for row in out] == ["u1", "u1"]

def test_truncate_context_for_display_keep_keeps_tool_tail_before_ambiguous_unkept_anchor():
    msgs = [
        {"role": "user", "content": "u1", "id": "u1"},
        {"role": "assistant", "content": "a1", "id": "a1", "tool_calls": [{"id": "kept-tool"}]},
        {"role": "user", "content": "u2"},
    ]
    ctx = [
        {"role": "user", "content": "u1", "id": "u1"},
        {"role": "assistant", "content": "a1", "id": "a1", "tool_calls": [{"id": "kept-tool"}]},
        {"role": "tool", "content": "kept-tool-result", "tool_call_id": "kept-tool"},
        {"role": "user", "content": "u2"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2", "id": "a2"},
    ]
    out = truncate_context_for_display_keep(ctx, msgs, 2)
    assert [row["content"] for row in out] == ["u1", "a1", "kept-tool-result"]
