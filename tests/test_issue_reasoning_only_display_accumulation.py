"""Regression tests for provenance-scoped reasoning-row dedup.

A recovered WebUI sidecar accumulated 2,048 copies of one state.db-backed,
reasoning-only assistant row. Every copy carried the same positive ``_row_id``,
exact timestamp, empty content, and settled ``reasoning_content``; only the
transient incremental ``reasoning`` snapshot varied. Content-distinct rows may
legitimately share a row id in the display projection, so repair must be narrow.
"""

from api import streaming


def _reasoning_row(*, row_id: int | None = 73683, reasoning="partial", settled="settled", timestamp=10.0):
    return {
        "role": "assistant",
        "content": "",
        "reasoning": reasoning,
        "reasoning_content": settled,
        "finish_reason": "incomplete",
        "timestamp": timestamp,
        "_row_id": row_id,
        "_db_persisted": True,
    }


def test_collapses_same_durable_reasoning_row_despite_transient_reasoning_variants():
    rows = [
        {"role": "user", "content": "Q", "timestamp": 1.0},
        _reasoning_row(reasoning="short"),
        _reasoning_row(reasoning="a much longer streamed reasoning snapshot"),
        {"role": "assistant", "content": "A", "timestamp": 11.0},
    ]

    repaired = streaming._dedupe_state_backed_reasoning_rows(rows)

    assert len(repaired) == 3
    kept = repaired[1]
    assert kept["_row_id"] == 73683
    assert kept["reasoning"] == "a much longer streamed reasoning snapshot"


def test_preserves_same_reasoning_in_distinct_state_rows():
    rows = [
        _reasoning_row(row_id=1, reasoning="same", settled="same"),
        _reasoning_row(row_id=2, reasoning="same", settled="same"),
    ]
    assert len(streaming._dedupe_state_backed_reasoning_rows(rows)) == 2


def test_preserves_reasoning_rows_with_different_settled_content():
    rows = [
        _reasoning_row(row_id=7, reasoning="first", settled="settled one"),
        _reasoning_row(row_id=7, reasoning="second", settled="settled two"),
    ]
    assert streaming._dedupe_state_backed_reasoning_rows(rows) == rows


def test_preserves_content_distinct_rows_that_share_row_id():
    rows = [
        {"role": "tool", "content": "tool result", "timestamp": 10.0, "_row_id": 7},
        {"role": "tool", "content": "tool header", "timestamp": 10.0, "_row_id": 7},
    ]
    assert streaming._dedupe_state_backed_reasoning_rows(rows) == rows


def test_preserves_reasoning_rows_without_required_durable_provenance():
    base = _reasoning_row(row_id=9, reasoning="same", settled="same")
    cases = [
        [dict(base, _partial=True), dict(base, _partial=True)],
        [
            {k: v for k, v in base.items() if k != "_db_persisted"},
            {k: v for k, v in base.items() if k != "_db_persisted"},
        ],
        [
            {k: v for k, v in base.items() if k != "timestamp"},
            {k: v for k, v in base.items() if k != "timestamp"},
        ],
        [
            _reasoning_row(row_id=None, reasoning="same", settled="same"),
            _reasoning_row(row_id=None, reasoning="same", settled="same"),
        ],
        [
            _reasoning_row(row_id=9, reasoning="first", settled=""),
            _reasoning_row(row_id=9, reasoning="second", settled=""),
        ],
    ]
    for rows in cases:
        assert streaming._dedupe_state_backed_reasoning_rows(rows) == rows


def test_merge_scrubs_existing_and_replayed_duplicates_without_global_identity_change():
    duplicate_a = _reasoning_row(reasoning="short")
    duplicate_b = _reasoning_row(reasoning="the complete reasoning snapshot")
    previous_display = [
        {"role": "user", "content": "Q0"},
        duplicate_a,
        duplicate_b,
        {"role": "assistant", "content": "A0"},
    ]
    previous_context = [
        {"role": "user", "content": "Q0"},
        {"role": "assistant", "content": "A0"},
    ]
    result_messages = previous_context + [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
    ]

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display, previous_context, result_messages, "Q1"
    )

    copies = [m for m in merged if isinstance(m, dict) and m.get("_row_id") == 73683]
    assert len(copies) == 1
    assert copies[0]["reasoning"] == "the complete reasoning snapshot"
    assert [m.get("content") for m in merged if m.get("content")] == ["Q0", "A0", "Q1", "A1"]