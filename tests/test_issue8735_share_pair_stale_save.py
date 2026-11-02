"""#8735: a stale whole-session save must not write an older share token back.

A share pair (``share_token``/``share_created_at``) is one fact with one writer.
When a session object leaves the LRU cache while a writer still holds it — a
rename, or a create whose object picked the token up before it was persisted —
that writer's later whole-session save used to write its own copy of the pair
over the file. If the link had been revoked and re-created in the meantime, the
file went back to naming the OLD token:

* a revoke then reads the old token, kills it and returns 200, while the NEWER
  public URL stays readable and unreachable from the UI.

The fix is a single-writer rule: only the share patch may change the pair, and
every other save keeps the pair the FILE names (compared on disk, not in the
object). The tests below reproduce the eviction-then-save sequence end to end
and assert on what a visitor of the published URLs receives.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api import models, shares


@pytest.fixture()
def store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    share_dir = tmp_path / "shares"
    session_dir.mkdir()
    share_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(shares, "SHARES_DIR", share_dir)
    return session_dir, share_dir


def _make_session(sid, title, text, workspace):
    session = models.Session(
        session_id=sid,
        title=title,
        workspace=str(workspace),
        messages=[{"role": "user", "content": text}],
    )
    session.save()
    return models.Session.load(sid)


def _persist_pair(session, token, created_at=1000.0):
    """Write the pair through the production share patch.

    Tolerant on purpose: on the pre-fix head the patch does not exist yet and the
    pair is written by a plain save, which keeps the RED honest (the defect is in
    the *other* writers, not in how the pair is persisted here).
    """
    updater = getattr(session, "update_session_share_fields", None)
    if updater is not None:
        updater(token=token, created_at=created_at)
        return
    session.share_token = token
    session.share_created_at = created_at
    session.save(touch_updated_at=False)


def _persisted_pair(session_dir: Path, sid: str):
    payload = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    return payload.get("share_token"), payload.get("share_created_at")


def _evict_from_cache(sid):
    """Drop *sid* from the LRU while a caller keeps its object alive.

    Mirrors what ``_evict_sessions_over_cap()`` does to a clean, non-streaming
    session: the cache stops being the owner of the object, the writer is not.
    """
    models.SESSIONS[sid] = models.get_session(sid)
    filler = models.Session(
        session_id=f"{sid}-lru-filler",
        title="filler",
        messages=[{"role": "user", "content": "filler"}],
    )
    filler.save()
    models.SESSIONS[filler.session_id] = filler
    with models.LOCK:
        evicted = models._evict_sessions_over_cap(cap=1)
    models.SESSIONS.pop(filler.session_id, None)
    assert evicted >= 1
    assert sid not in models.SESSIONS


def _revoke_and_republish_with_evicted_writer(store):
    """Reproduce the issue's sequence and hand back its three actors.

    1. the session's link ``token_old`` is published and persisted;
    2. a writer holds an object that carries it and loses the LRU cache;
    3. the owner revokes and publishes ``token_new``, which the file names.

    The returned ``retained`` object still carries ``token_old``: it is the
    stale writer whose later save is the defect.
    """
    session_dir, share_dir = store
    _make_session("sess-a", "Session A", "A words", session_dir)

    first = shares.create_or_refresh_share(models.Session.load("sess-a"))
    token_old = first["share_token"]
    _persist_pair(models.Session.load("sess-a"), token_old)
    assert _persisted_pair(session_dir, "sess-a")[0] == token_old

    retained = models.get_session("sess-a")
    assert retained.share_token == token_old
    _evict_from_cache("sess-a")

    owner = models.Session.load("sess-a")
    assert shares.revoke_share(owner) is True
    _persist_pair(owner, None, None)
    refreshed = shares.create_or_refresh_share(models.Session.load("sess-a"))
    token_new = refreshed["share_token"]
    assert token_new != token_old
    _persist_pair(models.Session.load("sess-a"), token_new)
    assert _persisted_pair(session_dir, "sess-a")[0] == token_new
    return token_old, token_new, retained


def test_evicted_writer_cannot_put_an_old_share_token_back(store):
    session_dir, share_dir = store
    token_old, token_new, retained = _revoke_and_republish_with_evicted_writer(store)

    # The stale writer now saves its WHOLE session (rename + appended turn).
    retained.title = "Session A renamed"
    retained.messages.append({"role": "assistant", "content": "later answer"})
    retained.save(touch_updated_at=False)

    assert _persisted_pair(session_dir, "sess-a")[0] == token_new, (
        "the stale save wrote the old share token back over the newer one"
    )
    assert models.Session.load("sess-a").title == "Session A renamed"


def test_revoke_after_a_stale_save_leaves_no_live_url(store):
    """The symptom the issue names: revoke answers 200 but the new link lives."""
    session_dir, share_dir = store
    token_old, token_new, retained = _revoke_and_republish_with_evicted_writer(store)

    retained.save(touch_updated_at=False)

    final = models.Session.load("sess-a")
    assert shares.revoke_share(final) is True
    assert shares.load_share(token_old) is None
    assert shares.load_share(token_new) is None, (
        "a published URL survived the revoke: the sidecar still named the old token"
    )


def test_plain_save_cannot_authorize_a_share_on_its_own(store):
    """Single writer: an object that sets the pair and saves publishes nothing."""
    session_dir, share_dir = store
    _make_session("sess-b", "Session B", "B words", session_dir)
    session = models.Session.load("sess-b")

    session.share_token = "hand-set-token"
    session.share_created_at = 1.0
    session.save(touch_updated_at=False)

    assert _persisted_pair(session_dir, "sess-b") == (None, None)
    assert not (share_dir / "hand-set-token.json").exists()


def test_normal_save_keeps_the_pair_it_loaded(store):
    """The adoption never drops a pair the session legitimately holds."""
    session_dir, share_dir = store
    _make_session("sess-c", "Session C", "C words", session_dir)
    token = shares.create_or_refresh_share(models.Session.load("sess-c"))["share_token"]
    _persist_pair(models.Session.load("sess-c"), token, created_at=1234.0)

    session = models.Session.load("sess-c")
    assert session.share_token == token
    session.title = "Session C renamed"
    session.save(touch_updated_at=False)

    assert _persisted_pair(session_dir, "sess-c") == (token, 1234.0)
    assert shares.load_share(token) is not None
