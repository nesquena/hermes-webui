"""Regression test for the indexed branch of greptile's P1 (#8672).

The clock-free indexed branch offers a saved occurrence that "establishes
some private identity".  That is not an occurrence-level proof: a saved
occurrence carrying only its stable WebUI id and an incoming row carrying a
NEW durable row id establish two DIFFERENT things, so collapsing them lets a
genuinely new turn disappear.  The branch reaches that helper only after the
shared-identity probe already missed, so without a shared-durable-row
requirement it undoes the same loss by the back door.

The two tests pin both directions:

* an incoming row whose durable id no saved member owns is NOT absorbed;
* an incoming row naming a durable row a saved occurrence DOES own is still
  absorbed by that occurrence.
"""

import pytest

pytestmark = pytest.mark.requires_agent_modules

import api.models as models


def _visible_key(role, content, occurrence, sidecar=None):
    if sidecar is None:
        return (role, content, "", occurrence)
    return (role, content, "", occurrence, sidecar)


def _group():
    """A group whose members carry a stable id, a durable id, or both.

    Both members share the provider payload, so they land in ONE clock-free
    group -- the group the indexed branch walks.
    """
    keys = {
        # stable WebUI id only -- it names no state.db row
        _visible_key("user", "Q", "1000|id:id:2", "provider-Q"),
        # durable row id 9 -- the same row the incoming replay names
        _visible_key("user", "Q", "1001|id:id:8,id:_state_db_row_id:9", "provider-Q"),
    }
    lookup = models._build_visible_duplicate_lookup(keys)
    return lookup["by_clockfree"][
        models._visible_key_without_occurrence(
            _visible_key("user", "Q", "", "provider-Q")
        )
    ]


def test_unseen_durable_id_is_not_absorbed():
    """An incoming row whose durable id no saved member owns stays separate."""
    group = _group()
    candidate = models._identity_bearing_candidate(
        group, ("id:_state_db_row_id:51",), remaining=None
    )
    assert candidate is None, (
        "the indexed branch must not absorb a row whose durable id no saved "
        f"member owns: {candidate!r}"
    )


def test_the_same_durable_id_is_absorbed():
    """A saved occurrence that owns the incoming durable row still absorbs it."""
    group = _group()
    candidate = models._identity_bearing_candidate(
        group, ("id:_state_db_row_id:9",), remaining=None
    )
    assert candidate is not None, (
        "a saved occurrence that owns the incoming durable row id must still "
        "absorb the replay"
    )
    occurrence = models._visible_key_field(
        candidate, models._VISIBLE_KEY_OCCURRENCE_INDEX, ""
    )
    assert "id:_state_db_row_id:9" in occurrence, (
        f"the wrong candidate was offered: {occurrence!r}"
    )
