"""Regression tests for #6242: draft clear must fail closed on sidecar unlink failure.

The authoritative clear path (``POST /api/session/draft`` with an emptied
composer) removes the per-session draft sidecar via
``delete_composer_draft_sidecar()``.  If that unlink raises ``OSError`` the
route returns a server error (never ``{"ok": true}``) and leaves the
recoverable draft untouched both in the sidecar and in the session JSON, so the
client can retain/recover it instead of a false clear.  Success and
absent-sidecar clears stay idempotent, and a retry after a failed unlink still
converges.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.requires_agent_modules


# ── isolated session environment (mirrors test_issue5532 harness) ──────────


def _install_isolated_session_env(monkeypatch, tmp_path):
    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.routes as routes

    monkeypatch.setattr(config, "STATE_DIR", tmp_path, raising=False)
    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(
        config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False
    )
    monkeypatch.setattr(models, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(
        models, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False
    )
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(
        profiles, "get_active_hermes_home", lambda: tmp_path, raising=False
    )
    monkeypatch.setattr(
        models, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False
    )
    monkeypatch.setattr(
        routes, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False
    )
    monkeypatch.setattr(config, "_evict_session_agent", lambda _sid: None, raising=False)
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def _seed_session(tmp_path, sid, draft):
    from api.models import Session

    session = Session(
        session_id=sid,
        title="Draft fail-closed",
        workspace=str(tmp_path),
        model="test-model",
        messages=[],
        created_at=1000.0,
        updated_at=1001.0,
        composer_draft=draft,
    )
    session.save(touch_updated_at=False)
    return session


def _seed_sidecar(session_dir, sid, draft):
    """Create the authoritative per-session draft sidecar on disk."""
    sidecar_dir = session_dir / "_drafts"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    sidecar = sidecar_dir / f"{sid}.json"
    sidecar.write_text(json.dumps(draft), encoding="utf-8")
    return sidecar


def _fail_sidecar_unlink(monkeypatch, sid):
    """Make unlink of the *sid* sidecar raise OSError; all other unlinks pass.

    Returns a mutable state dict; set ``state["fail"] = False`` to restore
    normal unlink behaviour (used by the retry/idempotency test).
    """
    real_unlink = Path.unlink
    state = {"fail": True}

    def raising_unlink(self, *args, **kwargs):
        if state["fail"] and "_drafts" in self.parts and self.name == f"{sid}.json":
            raise OSError(13, "Permission denied", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", raising_unlink)
    return state


def _post_draft(monkeypatch, sid, text="", files=None):
    """POST /api/session/draft and capture the route's JSON response."""
    import api.helpers as helpers
    import api.routes as routes

    payload = {"session_id": sid}
    if text is not None:
        payload["text"] = text
    if files is not None:
        payload["files"] = files
    body = json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(
        routes, "_guard_request_session_visibility", lambda *a, **k: True
    )

    captured = {}

    def fake_j(_handler, payload, status=200, extra_headers=None):
        captured["payload"] = payload
        captured["status"] = status

    # The route calls `j` directly AND indirectly through `bad()` (which
    # resolves `j` inside api.helpers) — patch both bindings so the 500
    # fail-closed response is captured instead of hitting a real socket.
    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(helpers, "j", fake_j)

    handler = SimpleNamespace(
        command="POST",
        headers={"Content-Length": str(len(body))},
        rfile=BytesIO(body),
    )
    routes.handle_post(handler, SimpleNamespace(path="/api/session/draft"))
    return captured


# ── helper-level tests ──────────────────────────────────────────────────────


def test_helper_removes_existing_sidecar(monkeypatch, tmp_path):
    import api.models as models

    session_dir = _install_isolated_session_env(monkeypatch, tmp_path)
    sidecar = _seed_sidecar(session_dir, "helper_existing", {"text": "hello", "files": []})
    assert sidecar.exists()

    models.delete_composer_draft_sidecar("helper_existing")

    assert not sidecar.exists()


def test_helper_absent_sidecar_is_noop(monkeypatch, tmp_path):
    """Deleting an absent sidecar must succeed silently (idempotent no-op)."""
    import api.models as models

    _install_isolated_session_env(monkeypatch, tmp_path)

    models.delete_composer_draft_sidecar("helper_absent")  # must not raise


def test_helper_propagates_unlink_oserror(monkeypatch, tmp_path):
    """A forced unlink failure must propagate as OSError, not be swallowed."""
    import api.models as models

    session_dir = _install_isolated_session_env(monkeypatch, tmp_path)
    _seed_sidecar(session_dir, "helper_oserror", {"text": "recoverable", "files": []})
    _fail_sidecar_unlink(monkeypatch, "helper_oserror")

    with pytest.raises(OSError):
        models.delete_composer_draft_sidecar("helper_oserror")

    # the sidecar must remain on disk (recoverable)
    assert (session_dir / "_drafts" / "helper_oserror.json").exists()


def test_helper_rejects_unsafe_session_id(monkeypatch, tmp_path):
    """Traversal-style session ids must be ignored, never acted on."""
    import api.models as models

    session_dir = _install_isolated_session_env(monkeypatch, tmp_path)
    outside = tmp_path / "escape.json"
    # "..%2F..%2Fescape" fails is_safe_session_id → early return, nothing created
    models.delete_composer_draft_sidecar("..%2F..%2Fescape")
    assert not outside.exists()
    assert not (session_dir / "_drafts").exists()


# ── route-level tests ───────────────────────────────────────────────────────


def test_route_clear_fails_closed_when_sidecar_unlink_fails(monkeypatch, tmp_path):
    """Clear must return a server error (never {"ok": true}) on unlink failure
    and leave the recoverable draft untouched both in the sidecar and in the
    session JSON."""
    from api.models import Session

    session_dir = _install_isolated_session_env(monkeypatch, tmp_path)
    sid = "issue6242_fail_closed"
    draft = {"text": "recoverable draft", "files": []}
    _seed_session(tmp_path, sid, draft)
    sidecar = _seed_sidecar(session_dir, sid, draft)
    _fail_sidecar_unlink(monkeypatch, sid)

    captured = _post_draft(monkeypatch, sid, text="")

    # fail closed: server error, never a false success
    assert captured["status"] == 500
    assert "ok" not in captured["payload"]
    assert "error" in captured["payload"]

    # the authoritative sidecar remains on disk and recoverable
    assert sidecar.exists()
    assert json.loads(sidecar.read_text(encoding="utf-8")) == draft

    # the session draft is still intact — the client can retain/recover it
    loaded = Session.load(sid)
    assert (loaded.composer_draft or {}).get("text") == "recoverable draft"


def test_route_clear_success_removes_sidecar_and_persists_empty(monkeypatch, tmp_path):
    """A successful clear removes the sidecar and persists the emptied draft."""
    from api.models import Session

    session_dir = _install_isolated_session_env(monkeypatch, tmp_path)
    sid = "issue6242_clear_ok"
    draft = {"text": "clear me", "files": []}
    _seed_session(tmp_path, sid, draft)
    sidecar = _seed_sidecar(session_dir, sid, draft)

    captured = _post_draft(monkeypatch, sid, text="")

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert not sidecar.exists()
    loaded = Session.load(sid)
    assert (loaded.composer_draft or {}).get("text") == ""


def test_route_clear_absent_sidecar_is_idempotent_success(monkeypatch, tmp_path):
    """Clearing when no sidecar exists is still a success (idempotent no-op)."""
    from api.models import Session

    _install_isolated_session_env(monkeypatch, tmp_path)
    sid = "issue6242_absent_sidecar"
    _seed_session(tmp_path, sid, {"text": "old", "files": []})
    # no sidecar seeded

    captured = _post_draft(monkeypatch, sid, text="")

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    loaded = Session.load(sid)
    assert (loaded.composer_draft or {}).get("text") == ""


def test_route_clear_retry_after_failed_unlink_converges(monkeypatch, tmp_path):
    """A retry after a failed unlink must still remove the leftover sidecar and
    report success — retry/idempotency stays defined after a failure."""
    from api.models import Session

    session_dir = _install_isolated_session_env(monkeypatch, tmp_path)
    sid = "issue6242_retry"
    draft = {"text": "recoverable draft", "files": []}
    _seed_session(tmp_path, sid, draft)
    sidecar = _seed_sidecar(session_dir, sid, draft)
    state = _fail_sidecar_unlink(monkeypatch, sid)

    # first attempt: unlink fails → 500, sidecar still recoverable
    captured_fail = _post_draft(monkeypatch, sid, text="")
    assert captured_fail["status"] == 500
    assert sidecar.exists()

    # retry with unlink healthy → sidecar removed, ok:true
    state["fail"] = False
    captured_ok = _post_draft(monkeypatch, sid, text="")
    assert captured_ok["status"] == 200
    assert captured_ok["payload"]["ok"] is True
    assert not sidecar.exists()
    loaded = Session.load(sid)
    assert (loaded.composer_draft or {}).get("text") == ""
