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