"""#8255 regression: /api/share/revoke must revoke the durable share token.

Reported schedule (#8255): ``/api/share/create`` writes the share snapshot and
then pauses before the token reaches the session sidecar; an eviction plus an
ordinary session read re-caches the sidecar as it was *before* the token; the
create resumes and persists the token; ``/api/share/revoke`` then runs against
the stale cached object, which has no ``share_token``. The handler ignored
``revoke_share()``'s ``False`` result, cleared the cached metadata and returned
``ok: true`` while ``load_share(token)`` kept resolving the snapshot.

The test drives that schedule deterministically against the live test server:
the middlewares cache is primed with a real ``GET /api/session``, the token is
persisted afterwards exactly as the create handler does when it resumes, and
the revoke must still invalidate the published snapshot. The cache-freshness
check compares message/scene state and not share fields, which is what makes
the primed cache entry count as current.
"""

import json
import urllib.error
import urllib.request

from tests._pytest_port import BASE


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            data = r.read()
            content_type = r.headers.get("Content-Type", "")
            if "application/json" in content_type:
                return json.loads(data), r.status, dict(r.headers)
            return data.decode("utf-8"), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        data = e.read()
        content_type = e.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return json.loads(data), e.code, dict(e.headers)
        return data.decode("utf-8"), e.code, dict(e.headers)


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def _make_session_with_messages():
    from api.models import Session

    created, _ = post("/api/session/new", {})
    sid = created["session"]["session_id"]
    session = Session.load(sid) or Session(session_id=sid)
    session.title = "Revoke Authority Test"
    session.messages = [
        {"role": "user", "content": "Share this conversation."},
        {"role": "assistant", "content": "Sharing it now."},
    ]
    session.save()
    return sid


def test_revoke_uses_durable_share_token_when_cached_session_is_stale():
    from api.models import Session
    from api.shares import create_or_refresh_share, load_share

    sid = _make_session_with_messages()
    try:
        # 1. Create reaches its save(): the snapshot is published, the token is
        #    not in the session sidecar yet (the reported pause point).
        session = Session.load(sid)
        assert session is not None
        share_meta = create_or_refresh_share(session)
        token = share_meta["share_token"]
        assert load_share(token) is not None

        # 2. An ordinary read re-caches the sidecar as it was before the token.
        primed, status, _ = get(f"/api/session?session_id={sid}&msg_limit=all")
        assert status == 200
        assert primed["session"]["session_id"] == sid
        assert primed["session"]["share_token"] is None

        # 3. The create resumes and persists the token to the sidecar.
        resumed = Session.load(sid)
        assert resumed.share_token is None
        resumed.share_token = token
        resumed.share_created_at = share_meta["share_created_at"]
        resumed.save(touch_updated_at=False)

        # 4. Revoke runs while the cached copy still has no token.
        revoked, status = post("/api/share/revoke", {"session_id": sid})
        assert status == 200
        assert revoked["ok"] is True

        # 5. The published snapshot must actually be gone — the reported bug is
        #    that it survives while the response claims success.
        assert load_share(token) is None
        missing, status, _ = get(f"/api/share/{token}")
        assert status == 404
        assert missing["error"] == "Shared conversation not found"

        # The durable sidecar no longer names the revoked token either.
        assert Session.load(sid).share_token is None
    finally:
        post("/api/session/delete", {"session_id": sid})


def test_revoke_without_a_share_stays_idempotently_successful():
    """Normal idempotent revoke behaviour is preserved (#8255 review note)."""
    sid = _make_session_with_messages()
    try:
        payload, status = post("/api/share/revoke", {"session_id": sid})
        assert status == 200
        assert payload["ok"] is True
        assert payload["session"]["share_token"] is None
        assert payload["session"]["share_created_at"] is None
    finally:
        post("/api/session/delete", {"session_id": sid})
