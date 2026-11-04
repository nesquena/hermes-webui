"""Regression: two tokenless initial creates plus one revoke leave no readable share.

Both creates resolve their request-owned session copy before either handler
publishes, so neither copy carries a ``share_token``. The shared per-session
lock and the in-lock durable re-read are what keep the second create from
minting a token the durable sidecar never records; without them one of the two
issued snapshots stays readable after ``/api/share/revoke`` answers ``ok``.
"""

import copy

import pytest

from tests.test_issue8255_share_lock_scope import (  # noqa: F401
    _make_session,
    _post,
    isolated_state,  # registered as a fixture for this module
)


@pytest.mark.usefixtures("isolated_state")
def test_two_pre_resolved_initial_creates_have_no_post_revoke_orphan(monkeypatch):
    import api.routes as routes
    from api.shares import load_share

    sid = _make_session("synthetic-initial-create-race")
    resolver = routes._resolve_share_session_pair
    # Freeze both request-owned copies before either request publishes.
    snapshot, stored, meta = resolver(sid, None)
    copies = [
        (copy.copy(snapshot), copy.copy(stored), copy.deepcopy(meta)),
        (copy.copy(snapshot), copy.copy(stored), copy.deepcopy(meta)),
    ]

    def resolve_preexisting(request_sid, handler):
        if copies:
            return copies.pop(0)
        return resolver(request_sid, handler)

    monkeypatch.setattr(routes, "_resolve_share_session_pair", resolve_preexisting)
    tokens = []
    for _ in range(2):
        status, payload = _post("/api/share/create", {"session_id": sid})
        assert status == 200, payload
        tokens.append(payload["share"]["token"])
        assert load_share(tokens[-1]) is not None

    status, payload = _post("/api/share/revoke", {"session_id": sid})
    assert status == 200, payload

    # A completed revoke must not leave either issued snapshot readable.
    observable = [token for token in tokens if load_share(token) is not None]
    assert not observable, (
        f"{len(observable)} issued snapshot(s) remain readable after a completed revoke"
    )

    # The second create reuses the token the durable sidecar already records, so
    # there is only ever one snapshot to revoke.
    assert tokens[0] == tokens[1]
