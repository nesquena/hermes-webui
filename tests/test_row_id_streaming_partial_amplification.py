"""Regression test: streaming partial replays of one durable row_id must not amplify.

Synthetic reproduction of a real incident (kept generic, no production data):
a reconnect while an assistant row is still streaming (``finish_reason``
``incomplete``) persists multiple snapshots of the *same* durable
``_row_id`` with divergent ``api_content``.  Because the merge dedup key
extends with the provider sidecar, the snapshots never match each other, and
because ``_row_id_fast_path_allowed`` disables the fast path once a row id
counts more than one occurrence, every later replay appends another copy.
The session file grows unboundedly (observed in the wild: 108,454 copies of
one row) and full deserialization eventually OOM-kills the server.
"""


def _partial_assistant(api_text: str, first_token_ms: int) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "timestamp": 1000.0,
        "finish_reason": "incomplete",
        "api_content": api_text,
        "_row_id": 24318,
        "_firstTokenMs": first_token_ms,
        "_turnTps": 1.0,
        "_db_persisted": True,
    }


def _user_row() -> dict:
    return {
        "role": "user",
        "content": "canonical prompt",
        "timestamp": 999.0,
        "_row_id": 24317,
        "_db_persisted": True,
    }


def _row_id_multiset(merged):
    return [
        message.get("_row_id")
        for message in merged
        if isinstance(message, dict) and message.get("_row_id") is not None
    ]


def test_single_partial_snapshot_replays_without_appending():
    """Baseline: with only one copy present the fast path already dedups."""
    import api.models as models

    sidecar = [_user_row(), _partial_assistant("Reas:", 10)]
    state = [_user_row(), _partial_assistant("Reas:", 10)]

    merged = models.merge_session_messages_append_only(sidecar, state)

    assert _row_id_multiset(merged).count(24318) == 1


def test_divergent_streaming_snapshots_do_not_amplify_row_id():
    """Two sidecar snapshots + one state replay of the same durable row.

    Expectation: the merge keeps exactly one row per durable ``_row_id`` and
    retains the most advanced snapshot, instead of appending a third copy.
    """
    import api.models as models

    sidecar = [
        _user_row(),
        _partial_assistant("Reas:", 10),
        _partial_assistant("Reas: partial", 12),
    ]
    state = [
        _user_row(),
        _partial_assistant("Reas: partial longer", 14),
    ]

    merged = models.merge_session_messages_append_only(sidecar, state)

    rows = _row_id_multiset(merged)
    assert rows.count(24318) == 1, (
        f"durable row 24318 amplified to {rows.count(24318)} copies"
    )
    kept = next(m for m in merged if m.get("_row_id") == 24318)
    assert kept["api_content"] == "Reas: partial longer", (
        "merge must retain the most advanced streaming snapshot"
    )


def test_repeated_merges_stay_stable_under_amplification_pressure():
    """The amplification is self-reinforcing across calls: each persist feeds
    the next merge.  Merging the same advanced state row repeatedly must not
    grow the row count either time."""
    import api.models as models

    merged = [
        _user_row(),
        _partial_assistant("Reas:", 10),
        _partial_assistant("Reas: partial", 12),
    ]
    state = [_user_row(), _partial_assistant("Reas: partial longer", 14)]

    for _ in range(5):
        merged = models.merge_session_messages_append_only(merged, state)

    rows = _row_id_multiset(merged)
    assert rows.count(24318) == 1, (
        f"row 24318 grew to {rows.count(24318)} copies across 5 merges"
    )
