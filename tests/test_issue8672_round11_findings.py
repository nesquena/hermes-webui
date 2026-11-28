"""Regression coverage for the 2026-11-18 #8672 review findings.

1. ``_clockless_duplicate_candidate()`` only re-offered the exhausted
   ``representative`` (and the anonymous wildcard). With one clocked turn and
   two identical turns without timestamps, the second clock-less copy never
   absorbed its replay, so ``_clockfree_duplicate_candidate()`` returned None
   and the merge appended an extra image row.
2. The identity-indexed walk rescanned every saved row whose durable identity
   conflicted with the incoming one. Those rows keep their replay capacity, so
   every NULL-clock replay paid for all of them again: quadratic loading work
   on transcripts with many repeated prompts.
"""

from __future__ import annotations

import api.models as models


def _key(occurrence: str, content: str = "img"):
    return ("assistant", content, "", occurrence)


def _group(keys):
    lookup = models._build_visible_duplicate_lookup(list(keys))
    first = keys[0]
    return lookup["by_clockfree"][models._visible_key_without_occurrence(first)]


def _capacity(keys):
    return {key: 1 for key in keys}


def test_second_clockless_copy_absorbs_the_replay():
    clocked = _key("1000|id:id:1")
    clockless_one = _key("id:id:2")
    clockless_two = _key("id:id:3")
    keys = [clocked, clockless_one, clockless_two]
    group = _group(keys)
    remaining = _capacity(keys)

    first = models._clockfree_duplicate_candidate(
        group, "2000|id:_state_db_row_id:8", remaining
    )
    assert first == clockless_one
    remaining[first] -= 1

    second = models._clockfree_duplicate_candidate(
        group, "2001|id:_state_db_row_id:9", remaining
    )
    assert second == clockless_two, (
        "the second clock-less copy must absorb its replay instead of the "
        "merge appending a duplicate image row"
    )
    remaining[second] -= 1

    assert models._clockfree_duplicate_candidate(
        group, "2002|id:_state_db_row_id:10", remaining
    ) is None


def test_conflicting_rows_are_not_rescanned_per_replay(monkeypatch):
    n = 200
    conflicting = [_key(f"id:_state_db_row_id:{i}") for i in range(n)]
    compatible = [_key("id:id:" + str(10_000 + i)) for i in range(n)]
    clocked = _key("1000|id:id:1")
    keys = [clocked, *conflicting, *compatible]
    group = _group(keys)
    remaining = _capacity(keys)

    calls = {"count": 0}
    real = models._visible_key_occurrence_compatible

    def counting(candidate, identities):
        calls["count"] += 1
        return real(candidate, identities)

    monkeypatch.setattr(models, "_visible_key_occurrence_compatible", counting)

    consumed = 0
    for i in range(n):
        candidate = models._clockfree_duplicate_candidate(
            group, f"id:_state_db_row_id:{50_000 + i}", remaining
        )
        assert candidate is not None
        remaining[candidate] -= 1
        consumed += 1

    assert consumed == n
    # The pre-fix walk inspected every conflicting row once per replay
    # (~n*n/2 compatibility calls). The compatible candidates are indexed by
    # signature, so each replay visits only the still-available sibling.
    assert calls["count"] <= 4 * n, calls["count"]
