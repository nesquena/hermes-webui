from __future__ import annotations

from api import models
from api.models import merge_session_messages_append_only


def test_prefix_replay_reuses_expected_visible_key_without_global_lookup(monkeypatch):
    """Replay-prefix matches must not rescan every sidecar visible key.

    The prefix check already knows the only sidecar key that can match the
    current replay row.  Calling _matching_visible_duplicate() again with the
    full sidecar key set turns repeated sidecar prefixes into an O(n²) hot path
    for long sessions with fuzzy-but-ordered state.db mirrors.
    """
    original = models._matching_visible_duplicate
    broad_lookup_calls = []

    def counted_matching_visible_duplicate(visible_key, visible_keys, lookup=None):
        if len(visible_keys) > 1:
            broad_lookup_calls.append((visible_key, len(visible_keys)))
        return original(visible_key, visible_keys, lookup)

    monkeypatch.setattr(models, "_matching_visible_duplicate", counted_matching_visible_duplicate)

    sidecar = [
        {"role": "assistant", "content": f"payload {i} abc def ghi", "timestamp": i}
        for i in range(64)
    ]
    # Same ordered prefix, but with extra suffix text so the fuzzy prefix path is
    # exercised instead of exact visible-key equality.
    state = [
        {"role": "assistant", "content": f"payload {i} abc def ghi extra", "timestamp": 1000 + i}
        for i in range(64)
    ]

    result = merge_session_messages_append_only(sidecar, state)

    assert len(result) == len(sidecar)
    assert broad_lookup_calls == []


def test_prefix_replay_counts_matched_sidecar_key_for_later_duplicate_skips():
    """A fuzzy prefix replay must debit the matched sidecar key's duplicate budget."""
    sidecar = [
        {"role": "assistant", "content": "Repeated visible payload", "timestamp": 1},
        {"role": "assistant", "content": "Repeated visible payload", "timestamp": 2},
    ]
    state = [
        # Fuzzy replay of the first sidecar row: should skip and count against
        # the matched sidecar visible key, not the state's longer visible key.
        {"role": "assistant", "content": "Repeated visible payload plus suffix", "timestamp": 10},
        # Same visible key as the sidecar duplicate.  Because the sidecar has two
        # visible copies and only one prefix replay was skipped so far, this row
        # must also be skipped rather than appended as a false delta.
        {"role": "assistant", "content": "Repeated visible payload", "timestamp": 11},
    ]

    result = merge_session_messages_append_only(sidecar, state)

    assert result == sidecar
