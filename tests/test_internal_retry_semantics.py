"""Retry/undo must target the last human turn, never internal transport."""

from api.session_ops import _is_semantic_user_message, _truncate_at_last_user


def test_semantic_user_filter_excludes_all_internal_markers():
    assert _is_semantic_user_message({"role": "user", "content": "hello"})
    assert not _is_semantic_user_message({
        "role": "user",
        "content": "wake",
        "_user_originated": False,
    })
    assert not _is_semantic_user_message({
        "role": "user",
        "content": "wake",
        "_source": "async_delegation",
    })
    assert not _is_semantic_user_message({
        "role": "user",
        "content": "wake",
        "_display_kind": "internal_event",
    })


def test_context_truncation_skips_internal_user_transport():
    rows = [
        {"role": "user", "content": "human"},
        {"role": "assistant", "content": "reply"},
        {
            "role": "user",
            "content": "internal completion",
            "_source": "async_delegation",
            "_user_originated": False,
        },
        {"role": "assistant", "content": "internal reply"},
    ]
    assert _truncate_at_last_user(rows) == []
