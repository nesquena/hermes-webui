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


class _FakeHandler:
    """Minimal BaseHTTPRequestHandler-shaped fake: bad() writes the status
    via handler.send_response, so the error-path tests can assert the real
    status code the client would receive."""

    def __init__(self):
        self.wfile = BytesIO()
        self.response_status = None
        self.headers = {}
        self.rfile = BytesIO()

    def send_response(self, status):
        self.response_status = status

    def send_header(self, *args, **kwargs):
        pass

    def end_headers(self):
        pass


def _call_chat_start(monkeypatch, session_id, message="continue this work"):
    """Invoke the real POST /api/chat/start handler and capture which session
    the agent turn would be started on (via the _start_run seam) plus the
    response status (via the fake handler / j seam)."""
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
    handler = _FakeHandler()
    handler.headers = {"Content-Length": str(len(body_bytes))}
    handler.rfile = BytesIO(body_bytes)
    routes._handle_chat_start(handler, body)
    captured["handler"] = handler
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


def test_chat_start_snapshot_without_continuation_is_refused(monkeypatch, tmp_path):
    """A sealed snapshot with no uniquely resolvable continuation must NOT
    start a turn on the snapshot — the gateway rejects appends to archived
    sessions, so the turn would run and then fail to persist. Fail closed
    with a retryable 409 (#6745 review: the previous fallthrough enshrined
    the defect)."""
    _seed_session_dir(monkeypatch, tmp_path)
    _make_session(SNAPSHOT_ID, pre_compression_snapshot=True)

    captured = _call_chat_start(monkeypatch, SNAPSHOT_ID)

    assert captured["handler"].response_status == 409
    assert "session_id" not in captured  # _start_run must never be reached


def test_chat_start_unloadable_continuation_is_refused(monkeypatch, tmp_path):
    """Resolver found a continuation, but materializing it raises KeyError
    (stale/racy lineage): refuse with a retryable 409 carrying both ids
    instead of falling through to the sealed snapshot (#6745 review)."""
    import api.routes as routes

    _seed_session_dir(monkeypatch, tmp_path)
    _make_session(SNAPSHOT_ID, pre_compression_snapshot=True)
    _make_session(CONT_ID, parent_session_id=SNAPSHOT_ID)

    real = routes._get_or_materialize_session

    def selective_materialize(sid, **kwargs):
        if sid == CONT_ID:
            raise KeyError(sid)
        return real(sid, **kwargs)

    monkeypatch.setattr(routes, "_get_or_materialize_session", selective_materialize)

    captured = _call_chat_start(monkeypatch, SNAPSHOT_ID)

    assert captured["handler"].response_status == 409
    assert "session_id" not in captured  # _start_run must never be reached


def test_chat_start_read_only_continuation_is_refused(monkeypatch, tmp_path):
    """A read-only continuation (imported/subagent) must return the same 403
    as the direct-id path, not escape to a 500 (#6745 review finding 2)."""
    import api.routes as routes

    _seed_session_dir(monkeypatch, tmp_path)
    _make_session(SNAPSHOT_ID, pre_compression_snapshot=True)
    _make_session(CONT_ID, parent_session_id=SNAPSHOT_ID)

    real = routes._get_or_materialize_session

    def selective_materialize(sid, **kwargs):
        if sid == CONT_ID:
            raise PermissionError("read-only imported session")
        return real(sid, **kwargs)

    monkeypatch.setattr(routes, "_get_or_materialize_session", selective_materialize)

    captured = _call_chat_start(monkeypatch, SNAPSHOT_ID)

    assert captured["handler"].response_status == 403
    assert "session_id" not in captured  # _start_run must never be reached


def test_chat_start_ambiguous_continuations_are_refused(monkeypatch, tmp_path):
    """Two non-fork children of the snapshot = ambiguous lineage: the
    resolver must not guess. Refuse with 409 (#6745 review)."""
    _seed_session_dir(monkeypatch, tmp_path)
    _make_session(SNAPSHOT_ID, pre_compression_snapshot=True)
    _make_session("cont456", parent_session_id=SNAPSHOT_ID)
    _make_session("cont789", parent_session_id=SNAPSHOT_ID)

    captured = _call_chat_start(monkeypatch, SNAPSHOT_ID)

    assert captured["handler"].response_status == 409
    assert "session_id" not in captured


def test_chat_start_fork_child_is_not_used_as_continuation(monkeypatch, tmp_path):
    """A user fork of the snapshot (session_source="fork") is not a
    compression continuation: the turn must land on the real continuation,
    even when the fork is the newest child (#6745 review).

    The fork is saved AFTER the continuation so the pre-fix resolver (which
    picked the newest candidate) would choose the fork — this test is red
    without the fork exclusion.
    """
    _seed_session_dir(monkeypatch, tmp_path)
    _make_session(SNAPSHOT_ID, pre_compression_snapshot=True)
    _make_session(CONT_ID, parent_session_id=SNAPSHOT_ID)
    _make_session(
        "fork999",
        parent_session_id=SNAPSHOT_ID,
        session_source="fork",
    )

    captured = _call_chat_start(monkeypatch, SNAPSHOT_ID)

    assert captured["session_id"] == CONT_ID


def test_chat_start_fork_only_child_does_not_unblock_snapshot(monkeypatch, tmp_path):
    """A snapshot whose only child is a fork still has no continuation: 409,
    no run. The fork must not be mistaken for the live lineage."""
    _seed_session_dir(monkeypatch, tmp_path)
    _make_session(SNAPSHOT_ID, pre_compression_snapshot=True)
    _make_session(
        "fork999",
        parent_session_id=SNAPSHOT_ID,
        session_source="fork",
    )

    captured = _call_chat_start(monkeypatch, SNAPSHOT_ID)

    assert captured["handler"].response_status == 409
    assert "session_id" not in captured


def test_chat_start_compressed_fork_lineage_still_resolves(monkeypatch, tmp_path):
    """A fork that itself underwent compression (fork sidecar sealed as a
    snapshot) must still resolve to its live continuation.

    Greptile P1 concern: the continuation of a compressed fork would inherit
    session_source=\"fork\" and be wrongly excluded. In this data model that
    cannot happen — the gateway has no session_source column and the webui
    materializer reads it from gateway metadata (always None); the only
    writer of session_source=\"fork\" is /api/session/branch. This test pins
    the behavior so the lineage stays resolvable.
    """
    _seed_session_dir(monkeypatch, tmp_path)
    _make_session(
        SNAPSHOT_ID,
        pre_compression_snapshot=True,
        session_source="fork",
    )
    _make_session(CONT_ID, parent_session_id=SNAPSHOT_ID)

    captured = _call_chat_start(monkeypatch, SNAPSHOT_ID)

    assert captured["session_id"] == CONT_ID
