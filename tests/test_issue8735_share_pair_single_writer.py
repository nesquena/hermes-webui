"""#8735: a stale whole-session save must not write an old share pair back.

A writer (a rename, say) holds a session object R. The share lifecycle moves
the durable ``(share_token, share_created_at)`` pair while R is held: R may get
the pre-save mirror, leave the LRU cache before or after that, or never be
cached at all; the link may be revoked and a newer one created. R's whole-session
save must then never put a revoked token back in the sidecar, and a later revoke
must leave every link ever published unreadable.

Every ordering of those events is enumerated against the real route handlers,
the real ``Session.save`` adoption rule, the real LRU eviction check and real
snapshot files in a disposable session/share store.
"""
from __future__ import annotations

import itertools

import pytest

from tests.test_issue8399_share_revocation import (  # noqa: F401  (isolated_state is a fixture)
    _make_session,
    _post,
    _sidecar,
    isolated_state,
)

SID = "s_8735_holder"
FILLER = "s_8735_filler"

# Where R comes from and when it leaves the cache relative to the first share
# operation: never cached (a plain load), evicted before the operation (no
# mirror), evicted between the pre-save mirror and the persist (the mirror lands,
# the post-persist commit misses), evicted after the operation, or kept resident.
HOLDERS = ("uncached", "evict-before", "evict-mid", "evict-after", "resident")

# The first share operation. ``*-direct`` forces the route's reload to come back
# empty, so the handler writes the new pair onto the resident object it resolved
# (R itself, when R is resident) before the pair is persisted.
FIRST_OPS = ("create", "revoke", "create-direct", "revoke-direct")


def _tails():
    """Every ordering of R's save against a later revoke and/or newer create."""
    tails = []
    for others in ((), ("V",), ("C",), ("V", "C")):
        for perm in itertools.permutations(others + ("S",)):
            tails.append("".join(perm))
    return tuple(tails)


TAILS = _tails()  # S, VS, SV, CS, SC, and the six orderings of V, C, S

# The ``*-direct`` first ops force the route's reload fallback. Before #8735 the
# create handler then set the new pair on the resident object itself (the
# mirror skips its own source), so an eviction before the post-persist commit
# left the pair as that object's own change, and its later save wrote a revoked
# token back. The fallback now works on a copy, so every ordering below passes.


def _share_pair(sid):
    data = _sidecar(sid)
    token = str(data.get("share_token") or "").strip() or None
    return token, data.get("share_created_at")


def _evict(sid):
    import api.models as models

    models.get_session(FILLER)
    with models.LOCK:
        models._evict_sessions_over_cap(cap=1)
    assert sid not in models.SESSIONS


def _run_ordering(monkeypatch, initial, holder, first_op, tail):
    import api.models as models
    import api.routes as routes
    from api.shares import load_share

    _make_session(SID)
    _make_session(FILLER)
    published = []

    def create():
        status, body = _post("/api/share/create", {"session_id": SID})
        assert status == 200, body
        token = body["share"]["token"]
        if token not in published:
            published.append(token)
        return token

    def revoke():
        status, body = _post("/api/share/revoke", {"session_id": SID})
        assert status == 200, body

    if initial == "shared":
        create()

    if holder == "uncached":
        R = models.Session.load(SID)
        assert models.SESSIONS.get(SID) is not R
    else:
        R = models.get_session(SID)
        assert models.SESSIONS.get(SID) is R
    if holder == "evict-before":
        _evict(SID)

    op = first_op.split("-")[0]
    restore = []
    if first_op.endswith("-direct"):
        real_reload = routes._reload_session_for_share
        monkeypatch.setattr(routes, "_reload_session_for_share", lambda sid: None)
        restore.append(lambda: monkeypatch.setattr(routes, "_reload_session_for_share", real_reload))
    if holder == "evict-mid":
        real_persist = routes._persist_share_fields

        def persist(*args, **kwargs):
            _evict(SID)
            return real_persist(*args, **kwargs)

        monkeypatch.setattr(routes, "_persist_share_fields", persist)
        restore.append(lambda: monkeypatch.setattr(routes, "_persist_share_fields", real_persist))
    try:
        create() if op == "create" else revoke()
    finally:
        for undo in restore:
            undo()
    if holder == "evict-after":
        _evict(SID)

    row = {"R_own_before_save": None, "R_baseline_before_save": None}
    for step in tail:
        if step == "V":
            revoke()
        elif step == "C":
            create()
        else:
            row["R_own_before_save"] = R.share_token
            row["R_baseline_before_save"] = getattr(R, "_share_token_saved", None)
            row["disk_before_save"] = _share_pair(SID)[0]
            revoked_now = {t for t in published if load_share(t) is None}
            R.title = "renamed while held"
            R.save()
            row["pair_after_save"] = _share_pair(SID)[0]
            row["file_names_revoked_token"] = row["pair_after_save"] in revoked_now
            assert _sidecar(SID)["title"] == "renamed while held"

    revoke()
    row["published"] = list(published)
    row["readable_after_final_revoke"] = [t for t in published if load_share(t) is not None]
    row["pair_after_final_revoke"] = _share_pair(SID)[0]
    return row


@pytest.mark.usefixtures("isolated_state")
@pytest.mark.parametrize("tail", TAILS)
@pytest.mark.parametrize("first_op", FIRST_OPS)
@pytest.mark.parametrize("holder", HOLDERS)
@pytest.mark.parametrize("initial", ("never-shared", "shared"))
def test_a_held_object_save_never_writes_a_revoked_pair_back(
    monkeypatch, request, initial, holder, first_op, tail,
):
    row = _run_ordering(monkeypatch, initial, holder, first_op, tail)
    for key, value in row.items():
        request.node.user_properties.append((key, repr(value)))
    assert not row["file_names_revoked_token"], row
    assert row["readable_after_final_revoke"] == [], row
    assert row["pair_after_final_revoke"] is None, row
