"""Regression coverage for legacy state-message deduplication."""

from api.models import merge_session_messages_append_only


def test_merge_deduplicates_repeated_legacy_state_messages():
    """A state row appended once must not be appended again with the same key."""
    sidecar = [{"role": "system", "content": "sys", "id": "s1"}]
    state = [
        {"role": "user", "content": "hello"},
        {"role": "user", "content": "hello"},
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert merged == [
        {"role": "system", "content": "sys", "id": "s1"},
        {"role": "user", "content": "hello"},
    ]
