"""Regression tests — /api/chat/start must canonicalize a pre-compression
snapshot id to its live continuation.

Root cause: after context compression, the archived session sidecar is marked
``pre_compression_snapshot`` and the gateway refuses appends to it ("Session
'X' is closed by compression; adopt its live continuation before appending
messages"). The GET /api/session path already surfaces a
``continuation_session_id`` hint, but the chat/start path passed the stale id
straight through: the agent ran a full turn and then failed to persist
(response_len=0) — a silent token + UX failure for any client (e.g. a mobile
app) still holding the archived id.

Fix: in ``_handle_chat_start``, after materializing the requested session,
resolve pre-compression snapshots to their live continuation (the same
``_pre_compression_continuation_session_id`` helper the read path uses) and
start the turn on the continuation.

These tests fail on origin/master (the turn starts on the snapshot) and pass
with the fix.
"""
from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace

SNAPSHOT_ID = "snap123"
CONT_ID = "cont456"
MID_ID = "mid789"


def _seed_session_dir(monkeypatch, tmp_path):
    """Point the session store at an isolated tmp dir (mirror #5532 harness).

    The resolver in api/routes.py reads SESSION_DIR / SESSION_INDEX_FILE from
    ITS OWN api.config imports (routes.SESSION_DIR), not from api.models — so
    both module attributes must be patched or the resolver checks the real
    session store and finds no backing files for the fixtures.
    """
    import api.models as models
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    return models


def _call_chat_start(monkeypatch, session_id, message="continue this work"):
    """Invoke the real POST /api/chat/start handler and capture which session
    the agent turn would be started on (via the _start_run seam)."""
    import api.routes as routes

    captured = {}

    def fake_start_run(session, **kwargs):
        captured["session_id"] = session.session_id
        return {"_status": 200}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    # Keep the turn hermetic: stub the heavy resolution/launch seams.
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
    monkeypatch.setattr(
        routes,
        "_resolve_chat_workspace_with_recovery",
        lambda s, ws: "/tmp/ws",
    )
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *a, **kw: ("deepseek/deepseek-v4-flash", "deepseek", "deepseek/deepseek-v4-flash"),
    )
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda s, p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_repair_foreign_session_model_provider",
        lambda *a, **kw: "deepseek",
    )
    monkeypatch.setattr(routes, "_start_run", fake_start_run)
    monkeypatch.setattr(routes, "j", fake_j)

    body = {"session_id": session_id, "message": message}
    body_bytes = json.dumps(body).encode()
    handler = SimpleNamespace(
        headers={"Content-Length": str(len(body_bytes))},
        rfile=BytesIO(body_bytes),
    )
    routes._handle_chat_start(handler, body)
    return captured


def _make_session(session_id, **kwargs):
    from api.models import Session

    session = Session(
        session_id=session_id,
        profile="default",
        messages=[{"id": "u1", "role": "user", "content": "old", "timestamp": 1.0}],
        **kwargs,
    )
    session.save()
    return session


def test_chat_start_on_snapshot_starts_turn_on_live_continuation(monkeypatch, tmp_path):
    """A pre-compression snapshot id must resolve to its live continuation.

    Fail-without-fix: _start_run receives the snapshot id and the turn would
    then be rejected at persistence by the gateway ("closed by compression").
    """
    _seed_session_dir(monkeypatch, tmp_path)
    _make_session(
        SNAPSHOT_ID,
        pre_compression_snapshot=True,
    )
    _make_session(
        CONT_ID,
        parent_session_id=SNAPSHOT_ID,
        pre_compression_snapshot=False,
    )

    captured = _call_chat_start(monkeypatch, SNAPSHOT_ID)

    assert captured["session_id"] == CONT_ID


def test_chat_start_follows_multi_hop_snapshot_chain(monkeypatch, tmp_path):
    """Repeated compression (snapshot -> snapshot -> live) must still land on
    the newest visible continuation, not an intermediate archived session."""
    _seed_session_dir(monkeypatch, tmp_path)
    _make_session(SNAPSHOT_ID, pre_compression_snapshot=True)
    _make_session(
        MID_ID,
        parent_session_id=SNAPSHOT_ID,
        pre_compression_snapshot=True,
    )
    _make_session(
        CONT_ID,
        parent_session_id=MID_ID,
        pre_compression_snapshot=False,
    )

    captured = _call_chat_start(monkeypatch, SNAPSHOT_ID)

    assert captured["session_id"] == CONT_ID


def test_chat_start_snapshot_without_continuation_falls_through(monkeypatch, tmp_path):
    """A snapshot with no visible continuation degrades to the requested
    session instead of erroring — the gateway still owns the final refusal."""
    _seed_session_dir(monkeypatch, tmp_path)
    _make_session(SNAPSHOT_ID, pre_compression_snapshot=True)

    captured = _call_chat_start(monkeypatch, SNAPSHOT_ID)

    assert captured["session_id"] == SNAPSHOT_ID
