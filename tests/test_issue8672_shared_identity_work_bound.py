"""Regression test for greptile P2 (#8672): repeated turns slow loading.

``_shared_identity_candidate()`` scanned every saved member whenever the
incoming identity missed ``by_identity``, and its cache was keyed by the FULL
identity value.  Every distinct SQLite row id therefore started a fresh scan:
N saved repeated turns against N incoming rows that each carry a new clock
and a new durable id cost N^2 member visits, which slows large chats down
once the transcript passes the fuzzy bound.

The fix returns immediately when none of the incoming identities exists in the
group's index (no saved member establishes them, so the scan could only
produce an empty list) and keys the per-identity candidate list by the
identity value alone.

This test counts the member visits directly instead of timing the merge: a
timing assertion is flaky, a visit count is the actual work bound.
"""

import pytest

pytestmark = pytest.mark.requires_agent_modules

import api.models as models


def _visible_key(role, content, occurrence, sidecar=None):
    """Build a visible key in the product's five-slot shape."""
    if sidecar is None:
        return (role, content, "", occurrence)
    return (role, content, "", occurrence, sidecar)


def _group_with_n_saved_members(n):
    """A clock-free group holding N saved occurrences of one repeated turn."""
    group = models._build_visible_duplicate_lookup({
        _visible_key("user", "Q", "1000|id:id:1", "provider-Q"),
        *{
            _visible_key("user", "Q", "%d|id:id:%d" % (1000 + i, i + 2), "provider-Q")
            for i in range(n - 1)
        },
    })
    return group["by_clockfree"][
        models._visible_key_without_occurrence(
            _visible_key("user", "Q", "", "provider-Q")
        )
    ]


def test_unseen_identity_returns_without_scanning_members():
    """An identity no saved member owns costs zero member visits."""
    group = _group_with_n_saved_members(50)
    visits = {"count": 0}
    original = models._visible_key_field

    def counting_field(key, index, default):
        visits["count"] += 1
        return original(key, index, default)

    models._visible_key_field = counting_field
    try:
        result = models._shared_identity_candidate(
            group, ("id:_state_db_row_id:9999",), remaining=None
        )
    finally:
        models._visible_key_field = original

    assert result is None, (
        "an identity no saved member establishes must not be offered: "
        f"got {result!r}"
    )
    assert visits["count"] == 0, (
        "the member scan ran for an identity the group index does not carry: "
        f"{visits['count']} member visits"
    )


def test_member_visits_stay_linear_in_the_number_of_saved_members():
    """N saved members cost O(1) visits for an unseen identity, not O(N)."""
    counts = {}
    for n in (10, 100, 1000):
        group = _group_with_n_saved_members(n)
        visits = {"count": 0}
        original = models._visible_key_field

        def counting_field(key, index, default, _original=original, _visits=visits):
            _visits["count"] += 1
            return _original(key, index, default)

        models._visible_key_field = counting_field
        try:
            models._shared_identity_candidate(
                group, ("id:_state_db_row_id:%d" % (90000 + n),), remaining=None
            )
        finally:
            models._visible_key_field = original
        counts[n] = visits["count"]

    assert counts[10] == counts[100] == counts[1000] == 0, (
        "the member scan for an unseen identity must not grow with the member "
        f"count: {counts}"
    )


def test_a_known_identity_still_absorbs_its_replay():
    """The early return must not break the shared-identity collapse."""
    group = _group_with_n_saved_members(3)
    # Row 2 of the group carries the stable identity id:id:2.
    candidate = models._shared_identity_candidate(
        group, ("id:id:2",), remaining=None
    )
    assert candidate is not None, (
        "a saved occurrence that establishes the incoming identity must still "
        "absorb its replay"
    )
    occurrence = models._visible_key_field(
        candidate, models._VISIBLE_KEY_OCCURRENCE_INDEX, ""
    )
    assert "id:id:2" in occurrence, (
        f"the wrong candidate was offered: {occurrence!r}"
    )


def test_an_identity_the_index_knows_but_whose_candidate_is_exhausted_is_found():
    """An exhausted representative must not hide a sibling with the same id."""
    group = _group_with_n_saved_members(3)
    # id:id:2 is established by exactly one member here; the index points at
    # it.  Exhaust its capacity and confirm the helper still answers.
    remaining = {}
    first = models._shared_identity_candidate(
        group, ("id:id:2",), remaining=remaining
    )
    assert first is not None
    remaining[first] = 0
    second = models._shared_identity_candidate(
        group, ("id:id:2",), remaining=remaining
    )
    assert second is None or second is not first, (
        "an exhausted candidate must not be offered again"
    )
