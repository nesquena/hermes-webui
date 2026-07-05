"""Stable per-message ids keep the fork/truncate aligner from going blind.

Regression coverage for the systemic gap documented in
local-ops/docs/bug-context-messages-lost-timestamps.md: ``context_messages``
rows carried neither a stable ``id`` nor a ``timestamp``, so
``truncate_context_for_display_keep`` degraded to fragile content-signature
matching and mis-cut large sessions on fork/truncate.

The fix mints a stable, session-unique integer ``id`` on the per-turn result
rows that both ``messages`` (display) and ``context_messages`` (model) derive
from, and carries it forward across turns. These tests prove:

1. the minting helper is monotonic and non-destructive,
2. ``_restore_reasoning_metadata`` carries ids forward like timestamps,
3. the real turn transforms give a logical row the SAME id in both arrays,
4. the aligner resolves an otherwise-ambiguous boundary via id alone.
"""

from api.streaming import (
    _assign_stable_message_ids,
    _restore_reasoning_metadata,
    _deduplicate_context_messages,
    _dedupe_replayed_context_messages,
    _merge_display_messages_after_agent_result,
)
from api.session_ops import truncate_context_for_display_keep


def test_assign_stable_ids_mints_monotonic_ints():
    rows = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "tool", "content": "t1", "tool_call_id": "c1"},
    ]
    stamped = _assign_stable_message_ids(rows)
    assert stamped == 3
    assert [r["id"] for r in rows] == [1, 2, 3]


def test_assign_stable_ids_seeds_from_existing_max_and_skips_present():
    prev = [{"role": "user", "content": "old", "id": 41}]
    rows = [
        {"role": "assistant", "content": "keep", "id": 7},  # already has id -> untouched
        {"role": "tool", "content": "new", "tool_call_id": "c"},
    ]
    stamped = _assign_stable_message_ids(rows, prev)
    assert stamped == 1
    # kept its own id; new row minted above the global max (41), not above 7
    assert rows[0]["id"] == 7
    assert rows[1]["id"] == 42


def test_assign_stable_ids_ignores_bool_and_empty():
    assert _assign_stable_message_ids([]) == 0
    assert _assign_stable_message_ids(None) == 0
    rows = [{"role": "user", "content": "u", "id": True}]  # bool must not seed
    other = [{"role": "assistant", "content": "a"}]
    _assign_stable_message_ids(other, rows)
    assert other[0]["id"] == 1  # seed ignored the bool, started at 1


def test_restore_reasoning_metadata_carries_id_forward():
    previous = [
        {"role": "user", "content": "u1", "id": 10, "timestamp": 1},
        {"role": "assistant", "content": "a1", "id": 11, "timestamp": 2},
    ]
    # Agent rebuilds the same history without our id (and without timestamp).
    updated = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},  # genuinely new tail row
    ]
    out = _restore_reasoning_metadata(previous, updated)
    assert out[0]["id"] == 10
    assert out[1]["id"] == 11
    assert "id" not in out[2]  # new row left for the minting pass


def test_both_arrays_share_id_for_same_logical_row():
    """The real turn transforms must give a row the SAME id in both arrays."""
    prev_display = []
    prev_context = []
    # The two arrays are built from the SAME result dicts (as in streaming.py).
    result_messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "x"}]},
        {"role": "tool", "content": "out", "tool_call_id": "c1"},
        {"role": "assistant", "content": "done"},
    ]

    # --- context path (mirrors streaming.py commit site) ---
    next_ctx = _restore_reasoning_metadata(prev_context, result_messages)
    next_ctx = _dedupe_replayed_context_messages(prev_context, next_ctx, "hello")
    _assign_stable_message_ids(result_messages, prev_display, prev_context)
    context_out = _deduplicate_context_messages(next_ctx)

    # --- display path (mirrors streaming.py commit site) ---
    display_out = _merge_display_messages_after_agent_result(
        prev_display,
        prev_context,
        _restore_reasoning_metadata(prev_display, result_messages),
        "hello",
    )

    # every context row must have an id, and the matching display row (by
    # role+content) must carry the identical id.
    assert all(isinstance(m.get("id"), int) for m in context_out)
    disp_by_key = {(m["role"], str(m.get("content"))): m.get("id") for m in display_out}
    for m in context_out:
        assert disp_by_key.get((m["role"], str(m.get("content")))) == m["id"]


def _dup_ambiguous_shapes(with_ids):
    """Large-session shape: display longer than context, full of structurally
    identical assistant(tool_call)/tool(result) pairs so content signatures
    collide. Context is the compressed/shorter model view.
    """
    dup_call = [{"id": "c", "name": "run"}]

    def row(role, content, **kw):
        r = {"role": role, "content": content}
        r.update(kw)
        return r

    def maybe_id(r, i):
        if with_ids and i is not None:
            r["id"] = i
        return r

    # 8 display rows, three identical assistant/tool pairs then a trailing user.
    display = [
        maybe_id(row("user", "u1"), 1),
        maybe_id(row("assistant", "", tool_calls=dup_call), 2),
        maybe_id(row("tool", "same", tool_call_id="c"), 3),
        maybe_id(row("assistant", "", tool_calls=dup_call), 4),
        maybe_id(row("tool", "same", tool_call_id="c"), 5),
        maybe_id(row("assistant", "", tool_calls=dup_call), 6),
        maybe_id(row("tool", "same", tool_call_id="c"), 7),
        maybe_id(row("user", "u2"), 8),
    ]
    # 7 context rows (shorter -> len(ctx) < len(msgs), the documented signature):
    # a leading compaction summary with no display match, then display ids 1..6.
    context = [
        row("assistant", "[compaction summary]"),  # leading, unmatched, no id
        maybe_id(row("user", "u1"), 1),
        maybe_id(row("assistant", "", tool_calls=dup_call), 2),
        maybe_id(row("tool", "same", tool_call_id="c"), 3),
        maybe_id(row("assistant", "", tool_calls=dup_call), 4),
        maybe_id(row("tool", "same", tool_call_id="c"), 5),
        maybe_id(row("assistant", "", tool_calls=dup_call), 6),
    ]
    return display, context


def test_aligner_resolves_ambiguous_boundary_via_id():
    """With shared ids the aligner cuts at the exact boundary even though the
    duplicate rows make content signatures ambiguous."""
    display, context = _dup_ambiguous_shapes(with_ids=True)
    # Keep the first 4 display rows (u1, a, t, a=id4). Correct cut keeps the
    # leading summary + ids 1..4 (5 rows), stopping before id5.
    out = truncate_context_for_display_keep(context, display, keep=4)
    assert len(out) == 5
    assert [m.get("id") for m in out] == [None, 1, 2, 3, 4]


def test_aligner_without_ids_underkeeps_baseline():
    """Same shape with ids/timestamps stripped (pre-fix live data): the duplicate
    rows defeat signature matching, so the aligner cannot resolve the exact
    boundary and errs toward UNDER-keeping (the #5563 matcher no longer raw-slices
    the compacted case). It keeps fewer rows than the id-aware path, which is why
    the shared stable id is the durable fix — with ids the same shape resolves to
    the exact 5-row cut (see test_aligner_resolves_ambiguous_boundary_via_id)."""
    display, context = _dup_ambiguous_shapes(with_ids=False)
    out = truncate_context_for_display_keep(context, display, keep=4)
    # Without ids the matcher can't disambiguate the identical assistant/tool
    # pairs, so it under-keeps (3 rows) rather than reaching the exact id-aware
    # boundary (5 rows). It never over-keeps or slices mid-turn at the raw index.
    assert len(out) < 5
    assert [m.get("content") for m in out] == [
        "[compaction summary]",
        "u1",
        "",
    ]
