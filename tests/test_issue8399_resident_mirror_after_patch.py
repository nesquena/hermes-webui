"""#8399 review, release fold: no dead token on the resident copy after a revoke.

The revoke handler mirrors ``share_token=None`` onto the resident session, then
patches the sidecar. When the resident's stale-save baseline equalled the
mirrored value, a save landing between the mirror and the patch counted as
"unchanged" and adopted the on-disk pair, which still named the revoked token.
Disk ended correct (the patch runs after), but the cached object and
``/api/session`` kept reporting the dead link until that copy's next save.
``_persist_share_fields`` now commits the pair to the resident copy (fields and
baseline together, under the persistence lock) once it is on disk; the
delayed-rename case that rules out moving the fields alone is in
tests/test_issue8399_delayed_rename_after_share.py.
"""

from __future__ import annotations

import json
import urllib.parse
from collections import OrderedDict

import pytest

from tests.test_issue8399_share_revocation import _FakeHandler


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    import api.models as models
    import api.routes as routes
    import api.shares as shares

    sessions = OrderedDict()
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSIONS", sessions)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "index.json")
    monkeypatch.setattr(routes, "SESSIONS", sessions)
    monkeypatch.setattr(shares, "SHARES_DIR", tmp_path / "shares")
    yield tmp_path


def _post(path, body):
    from api.routes import handle_post

    handler = _FakeHandler(json.dumps(body).encode())
    handle_post(handler, urllib.parse.urlparse(path))
    return handler.status, handler.json_body()


def _sidecar_token(sid):
    import api.models as models

    return json.loads((models.SESSION_DIR / f"{sid}.json").read_text(encoding="utf-8")).get("share_token")


@pytest.mark.parametrize("skip_index", [True, False], ids=["checkpoint-save", "ordinary-save"])
def test_resident_saved_between_mirror_and_patch_does_not_keep_the_revoked_token(
    isolated_state, monkeypatch, skip_index,
):
    import api.models as models
    import api.routes as routes
    import api.shares as shares
    from api.models import Session

    sid = "s_8399_mirror_after_patch"
    session = Session(session_id=sid, title="t")
    session.messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    session.save(touch_updated_at=False)
    resident = models.get_session(sid)  # the cached copy a stream would checkpoint

    status, body = _post("/api/share/create", {"session_id": sid})
    assert status == 200, body
    token = body["share"]["token"]
    assert resident.share_token == token  # the create reached the resident copy

    real_persist = routes._persist_share_fields
    landed = {}

    def _save_in_the_window(sid_, session_, *, share_token, share_created_at):
        if share_token is None and not landed:
            # The revoke's first mirror has run; a resident save lands before the
            # sidecar patch. Without the committed baseline this save adopted the
            # on-disk (revoked) token.
            assert resident.share_token is None
            resident.messages = [*resident.messages, {"role": "assistant", "content": "ck", "partial": True}]
            resident.save(touch_updated_at=False, skip_index=skip_index)
            landed["saved"] = True
        return real_persist(sid_, session_, share_token=share_token, share_created_at=share_created_at)

    monkeypatch.setattr(routes, "_persist_share_fields", _save_in_the_window)

    status, body = _post("/api/share/revoke", {"session_id": sid})
    assert status == 200, body
    assert landed.get("saved"), "the window save never ran"
    assert shares.load_share(token) is None
    assert _sidecar_token(sid) is None
    with models.LOCK:
        cached = models.SESSIONS.get(sid)
    assert cached is resident
    assert cached.share_token is None, "the resident copy still reports the revoked link"
    assert cached.share_created_at is None
    # The baseline moved with the fields, so the next save keeps the cleared pair.
    resident.messages = [*resident.messages, {"role": "user", "content": "next"}]
    resident.save(touch_updated_at=False)
    assert resident.share_token is None and _sidecar_token(sid) is None
