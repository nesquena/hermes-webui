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
    # Context is SHORTER than display (a compaction summary replaced earlier
    # turns) and the display has trailing turns beyond the kept prefix. Keeping
    # display [u1, a1] must map to the context that represents [u1, a1] — the
    # leading compaction row PLUS both u1 and a1. The old behaviour sliced the
    # shorter context at the raw display index (returning [compact, u1]), which
    # dropped a1 — the assistant reply to a kept user turn — from the model
    # context while the display still showed it. That mismatch is the forked-
    # large-session disjointedness bug; the result must now match the sibling
    # ``preserves_leading_compaction_row`` case regardless of trailing turns.
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
    assert [row["content"] for row in out] == ["compact", "u1", "a1"]


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

def test_truncate_context_shorter_than_display_aligns_to_turn_boundary():
    """Large-session fork regression.

    Mirrors the live shape that broke forks: the model context has been trimmed
    so it is SHORTER than the display transcript, and its rows carry no ``id``
    (and mostly no ``timestamp``). The old ``len(ctx) <= len(msgs)``
    short-circuit returned ``ctx[:keep]`` verbatim — slicing the shorter context
    at the *display* index, which lands mid-turn on an assistant ``tool_use``
    whose result is past the cut. The fix routes the shorter-context case
    through the signature matcher, which cuts on the last aligned turn boundary
    (a completed ``tool`` result), so the context never ends on an unmatched
    ``tool_use``. (Any residual dangling ``tool_use`` on the harder no-match
    paths is stripped tool-id-aware by ``_sanitize_messages_for_api`` at send.)
    """
    msgs = [
        {"role": "user", "content": "u1", "timestamp": 1.0},
        {"role": "assistant", "content": "a1", "timestamp": 2.0,
         "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "content": "r1", "tool_call_id": "c1", "timestamp": 3.0},
        {"role": "assistant", "content": "done", "timestamp": 4.0},  # keep boundary
        {"role": "user", "content": "u2", "timestamp": 5.0},
        {"role": "assistant", "content": "a2", "timestamp": 6.0,
         "tool_calls": [{"id": "c2"}]},
    ]
    # Context: no ids, no timestamps (matcher must fall back to signature), the
    # "done" summary turn was compressed out, so ctx is shorter and diverges.
    ctx = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "content": "r1", "tool_call_id": "c1"},
        {"role": "assistant", "content": "a2", "tool_calls": [{"id": "c2"}]},
        {"role": "tool", "content": "r2", "tool_call_id": "c2"},
    ]
    out = truncate_context_for_display_keep(ctx, msgs, 4)
    contents = [row["content"] for row in out]
    # Old raw ctx[:4] would be ["u1","a1","r1","a2"] — "a2" is a dangling
    # tool_use. Alignment instead cuts at the last resolved turn boundary (r1).
    assert contents == ["u1", "a1", "r1"]
    assert not (out and out[-1].get("role") == "assistant" and out[-1].get("tool_calls"))


def test_shorter_context_ambiguous_boundary_keeps_forked_turn_via_weak_match():
    """Shorter context, forked boundary turn matches only ambiguously.

    The kept boundary turn (a1) has no id/timestamp and appears twice in the
    shorter context, so the matcher records it in ambiguous_matches (not
    matches). Both boundary rows are then unmatched, so the shorter-context
    fallback loop runs. It must accept the weak (ambiguous) match for the
    boundary turn and KEEP a1's context, rather than dropping back to the
    earlier exact match (u1) and losing the very turn the user forked at.
    """
    msgs = [
        {"role": "user", "content": "u1", "id": "u1"},
        {"role": "assistant", "content": "a1"},   # keep boundary, no id (ambiguous)
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    # Shorter context (3 < 4); a1 appears twice with no id → ambiguous match.
    ctx = [
        {"role": "user", "content": "u1", "id": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "assistant", "content": "a1"},
    ]
    out = truncate_context_for_display_keep(ctx, msgs, 2)
    # Must include the forked boundary turn a1 (weak match at ctx[1]),
    # not stop at the earlier exact match u1.
    assert [row["content"] for row in out] == ["u1", "a1"]


def test_shorter_context_zero_match_falls_back_to_best_effort_prefix():
    """Shorter context where NO kept-prefix row aligns (unalignable last resort).

    When the kept display prefix lies entirely inside a summarized region whose
    rows share no signature with the display, the matcher resolves nothing and
    neither the sibling branches nor the ambiguous-aware fallback loop fire.
    Control reaches the #5096 last-resort fallback, which returns a best-effort
    prefix of the (shorter) context. Alignment is genuinely impossible here;
    wire-safety of any dangling tool_use is handled at send time. This test
    pins the documented last-resort behavior so it is not changed unknowingly.
    """
    msgs = [
        {"role": "user", "content": "u1", "id": "u1"},
        {"role": "assistant", "content": "a1", "id": "a1"},
        {"role": "user", "content": "u2", "id": "u2"},
    ]
    ctx = [
        {"role": "user", "content": "summary-1"},
        {"role": "assistant", "content": "summary-2"},
    ]
    out = truncate_context_for_display_keep(ctx, msgs, 2)
    assert [row["content"] for row in out] == ["summary-1", "summary-2"]


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
