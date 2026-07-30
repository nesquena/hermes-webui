"""End-to-end regression tests for the edit/session-switch watermark bug.

Scenario: user edits a message, sends a new turn, switches to another session,
then switches back — the original (pre-edit) message must NOT reappear from
state.db because the truncation_watermark was advanced (not cleared to None)
when the new turn was committed.

This covers the exact reproduction path:
1. Send message A ("triangle")
2. Edit message A to "square" (truncate + re-send)
3. Send message B ("light speed") — watermark advances to B's timestamp
4. Switch session (evict from LRU cache)
5. Switch back (reload from disk, merge sidecar + state.db)
6. Assert: "triangle" is NOT in the merged transcript
"""
from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace

import api.models as models
import api.routes as routes
from api.models import Session


def _msg(role: str, content: str, ts: float, mid: str) -> dict:
    return {"id": mid, "role": role, "content": content, "timestamp": ts}


def _setup_session_dir(monkeypatch, tmp_path):
    """Configure models to use a temp session directory."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()


def _post_truncate(monkeypatch, sid, keep_count):
    """Call POST /api/session/truncate via the real route handler."""
    import io

    class _JSONHandler:
        def __init__(self, body_bytes: bytes):
            self.status = None
            self.response_headers = []
            self.rfile = BytesIO(body_bytes)
            self.headers = {"Content-Length": str(len(body_bytes))}
            self.wfile = io.BytesIO()

        def send_response(self, status):
            self.status = status

        def send_header(self, key, value):
            self.response_headers.append((key, value))

        def end_headers(self):
            pass

        def payload(self):
            return json.loads(self.wfile.getvalue().decode("utf-8"))

    body_bytes = json.dumps({"session_id": sid, "keep_count": keep_count}).encode()
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    handler = _JSONHandler(body_bytes)
    routes.handle_post(handler, SimpleNamespace(path="/api/session/truncate"))
    return {"status": handler.status, "payload": handler.payload()}


def _checkpoint_user(s, msg, started_at, monkeypatch):
    """Call the eager checkpoint path (simulates sending a new message)."""
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    routes._checkpoint_user_message_for_eager_session_save(
        s, msg, None, started_at=started_at
    )


def test_edit_then_new_turn_watermark_advances_not_clears(monkeypatch, tmp_path):
    """After edit → new turn, truncation_watermark should advance to the new
    message's timestamp (NOT be cleared to None)."""
    _setup_session_dir(monkeypatch, tmp_path)

    sid = "edit_watermark_advance"
    # Real edit scenario: message 1 + reply kept, message 2 edited
    session = Session(
        session_id=sid,
        messages=[
            _msg("user", "first msg", 50.0, "u0"),
            _msg("assistant", "first reply", 51.0, "a0"),
            _msg("user", "triangle", 100.0, "u1"),
            _msg("assistant", "180°", 101.0, "a1"),
        ],
    )
    session.save()

    # Step 1: Truncate (edit) — keep 2 messages (first exchange)
    captured = _post_truncate(monkeypatch, sid, 2)
    assert captured["status"] == 200

    loaded = Session.load(sid)
    assert loaded.truncation_watermark == 51.0  # positive watermark (not 0.0)

    # Step 2: Send new message "square" — checkpoint advances watermark
    _checkpoint_user(loaded, "square", started_at=200.0, monkeypatch=monkeypatch)
    loaded.save()

    reloaded = Session.load(sid)
    # Watermark should be advanced to 200.0, NOT None
    assert reloaded.truncation_watermark is not None, (
        "truncation_watermark was cleared to None instead of advanced"
    )
    assert reloaded.truncation_watermark == 200.0


def test_edit_then_new_turn_then_reload_filters_pre_edit_state_db(monkeypatch, tmp_path):
    """After edit → new turn → reload from disk, pre-edit state.db rows must
    NOT appear in the merged transcript."""
    _setup_session_dir(monkeypatch, tmp_path)

    sid = "edit_reload_filter"
    # Real edit scenario: first exchange kept, second message edited
    session = Session(
        session_id=sid,
        messages=[
            _msg("user", "first msg", 50.0, "u0"),
            _msg("assistant", "first reply", 51.0, "a0"),
            _msg("user", "triangle", 100.0, "u1"),
            _msg("assistant", "180°", 101.0, "a1"),
        ],
    )
    session.save()

    # Step 1: Truncate (edit) — keep 2 messages (first exchange)
    captured = _post_truncate(monkeypatch, sid, 2)
    assert captured["status"] == 200

    # Step 2: Send new message "square"
    loaded = Session.load(sid)
    _checkpoint_user(loaded, "square", started_at=200.0, monkeypatch=monkeypatch)
    # Simulate assistant reply being appended to sidecar
    loaded.messages.append(_msg("assistant", "360°", 201.0, "a2"))
    loaded.save()

    # Step 3: Simulate state.db containing both original and edited messages
    state_db = [
        _msg("user", "first msg", 50.0, "state-u0"),
        _msg("assistant", "first reply", 51.0, "state-a0"),
        _msg("user", "triangle", 100.0, "state-u1"),
        _msg("assistant", "180°", 101.0, "state-a1"),
        _msg("user", "square", 200.0, "state-u2"),
        _msg("assistant", "360°", 201.0, "state-a2"),
    ]

    # Step 4: Reload from disk (simulates session switch → switch back)
    # Evict from cache to force disk reload
    with models.LOCK:
        models.SESSIONS.pop(sid, None)

    reloaded = Session.load(sid)
    assert reloaded.truncation_watermark is not None
    assert reloaded.truncation_watermark == 200.0

    # Step 5: Merge sidecar + state.db (what GET /api/session does)
    merged = models.merge_session_messages_append_only(
        reloaded.messages,
        state_db,
        truncation_watermark=reloaded.truncation_watermark,
    )

    contents = [m["content"] for m in merged]
    # "triangle" must NOT leak through
    assert "triangle" not in contents, (
        f"Pre-edit 'triangle' leaked from state.db! Contents: {contents}"
    )
    assert "180°" not in contents, (
        f"Pre-edit assistant reply leaked from state.db! Contents: {contents}"
    )
    # "square" must be present
    assert "square" in contents
    assert "360°" in contents


def test_edit_then_two_turns_then_reload_all_visible(monkeypatch, tmp_path):
    """After edit → two new turns → reload, all post-edit turns are visible
    and pre-edit rows are filtered."""
    _setup_session_dir(monkeypatch, tmp_path)

    sid = "edit_two_turns"
    # Real edit scenario: first exchange kept, second message edited
    session = Session(
        session_id=sid,
        messages=[
            _msg("user", "first msg", 50.0, "u0"),
            _msg("assistant", "first reply", 51.0, "a0"),
            _msg("user", "original", 100.0, "u1"),
            _msg("assistant", "original reply", 101.0, "a1"),
        ],
    )
    session.save()

    # Step 1: Truncate (edit) — keep 2 messages
    captured = _post_truncate(monkeypatch, sid, 2)
    assert captured["status"] == 200

    # Step 2: Send edited message
    loaded = Session.load(sid)
    _checkpoint_user(loaded, "edited", started_at=200.0, monkeypatch=monkeypatch)
    loaded.save()

    # Step 3: Simulate assistant reply
    loaded.messages.append(_msg("assistant", "edited reply", 201.0, "a2"))
    loaded.save()

    # Step 4: Send second new turn
    _checkpoint_user(loaded, "second question", started_at=300.0, monkeypatch=monkeypatch)
    # Simulate assistant reply to second question
    loaded.messages.append(_msg("assistant", "second reply", 301.0, "a3"))
    loaded.save()

    # Step 5: Simulate state.db with all messages
    state_db = [
        _msg("user", "first msg", 50.0, "state-u0"),
        _msg("assistant", "first reply", 51.0, "state-a0"),
        _msg("user", "original", 100.0, "state-u1"),
        _msg("assistant", "original reply", 101.0, "state-a1"),
        _msg("user", "edited", 200.0, "state-u2"),
        _msg("assistant", "edited reply", 201.0, "state-a2"),
        _msg("user", "second question", 300.0, "state-u3"),
        _msg("assistant", "second reply", 301.0, "state-a3"),
    ]

    # Step 6: Reload from disk
    with models.LOCK:
        models.SESSIONS.pop(sid, None)

    reloaded = Session.load(sid)
    assert reloaded.truncation_watermark is not None
    assert reloaded.truncation_watermark == 300.0

    # Step 7: Merge
    merged = models.merge_session_messages_append_only(
        reloaded.messages,
        state_db,
        truncation_watermark=reloaded.truncation_watermark,
    )

    contents = [m["content"] for m in merged]
    # Pre-edit rows filtered
    assert "original" not in contents
    assert "original reply" not in contents
    # Post-edit rows present
    assert "edited" in contents
    assert "edited reply" in contents
    assert "second question" in contents
    assert "second reply" in contents


def test_retry_then_new_turn_watermark_advances(monkeypatch, tmp_path):
    """After /retry → new turn, watermark should advance (not clear to None)."""
    _setup_session_dir(monkeypatch, tmp_path)

    from api.session_ops import retry_last

    sid = "retry_watermark_advance"
    session = Session(
        session_id=sid,
        messages=[
            _msg("user", "first", 100.0, "u1"),
            _msg("assistant", "reply", 101.0, "a1"),
            _msg("user", "second", 200.0, "u2"),
            _msg("assistant", "reply2", 201.0, "a2"),
        ],
    )
    session.save()

    # Step 1: Retry — truncates to before "second"
    retry_last(sid)
    loaded = Session.load(sid)
    assert loaded.truncation_watermark == 101.0
    assert [m["content"] for m in loaded.messages] == ["first", "reply"]

    # Step 2: Send new message
    _checkpoint_user(loaded, "new question", started_at=300.0, monkeypatch=monkeypatch)
    loaded.save()

    reloaded = Session.load(sid)
    assert reloaded.truncation_watermark is not None
    assert reloaded.truncation_watermark == 300.0


def test_undo_then_new_turn_watermark_advances(monkeypatch, tmp_path):
    """After /undo → new turn, watermark should advance (not clear to None)."""
    _setup_session_dir(monkeypatch, tmp_path)

    from api.session_ops import undo_last

    sid = "undo_watermark_advance"
    session = Session(
        session_id=sid,
        messages=[
            _msg("user", "first", 100.0, "u1"),
            _msg("assistant", "reply", 101.0, "a1"),
            _msg("user", "second", 200.0, "u2"),
            _msg("assistant", "reply2", 201.0, "a2"),
        ],
    )
    session.save()

    # Step 1: Undo — removes "second" and "reply2"
    undo_last(sid)
    loaded = Session.load(sid)
    assert loaded.truncation_watermark == 101.0
    assert [m["content"] for m in loaded.messages] == ["first", "reply"]

    # Step 2: Send new message
    _checkpoint_user(loaded, "new question", started_at=300.0, monkeypatch=monkeypatch)
    loaded.save()

    reloaded = Session.load(sid)
    assert reloaded.truncation_watermark is not None
    assert reloaded.truncation_watermark == 300.0


def test_zero_watermark_preserved_through_new_turn(monkeypatch, tmp_path):
    """The 0.0 truncate-to-empty sentinel (#2914) must survive a new turn —
    the falsy guard prevents advancing it."""
    _setup_session_dir(monkeypatch, tmp_path)

    sid = "zero_watermark_preserved"
    session = Session(
        session_id=sid,
        messages=[],
        truncation_watermark=0.0,
    )
    session.save()

    loaded = Session.load(sid)
    assert loaded.truncation_watermark == 0.0

    # Send new message — 0.0 must NOT be advanced
    _checkpoint_user(loaded, "new msg", started_at=100.0, monkeypatch=monkeypatch)
    loaded.save()

    reloaded = Session.load(sid)
    assert reloaded.truncation_watermark == 0.0, (
        "0.0 truncate-to-empty sentinel was advanced — #2914 regression!"
    )


def test_empty_sidecar_advanced_watermark_no_ghost_no_data_loss():
    """Empty sidecar + advanced watermark must keep post-edit rows without
    resurrecting stale pre-edit ghosts (#4767).

    Reproduction: cold reload / crash recovery before sidecar persists.
    state.db has the full lineage including replaced pre-edit rows.

    A genuinely-advanced session persists the original cutoff as
    ``truncation_boundary`` (here @51, the last kept reply) while the watermark
    is advanced to the new committed turn (@200). The reconstruction keeps the
    legitimate prefix (ts <= boundary) plus the post-edit tail (ts >= watermark)
    and drops the replaced (boundary, watermark) suffix. Passing the boundary is
    required — an advanced watermark with NO boundary is the unreachable
    legacy/frozen state (see the not-advanced regression tests in
    test_core_data_loss_cases.py)."""
    state = [
        {"role": "user", "content": "first msg", "timestamp": 50},
        {"role": "assistant", "content": "first reply", "timestamp": 51},
        {"role": "user", "content": "original pre-edit", "timestamp": 100},
        {"role": "assistant", "content": "original reply", "timestamp": 101},
        {"role": "user", "content": "edited/new turn", "timestamp": 200},
        {"role": "assistant", "content": "post-edit reply", "timestamp": 201},
    ]
    merged = models.merge_session_messages_append_only(
        [], state, truncation_watermark=200.0, truncation_boundary=51.0
    )
    contents = [m["content"] for m in merged]
    wanted = ["first msg", "first reply", "edited/new turn", "post-edit reply"]
    assert contents == wanted, (
        f"empty-sidecar + advanced watermark broken: got {contents}, wanted {wanted}"
    )
    # Ghost must NOT appear
    assert "original pre-edit" not in contents
    assert "original reply" not in contents
    # Post-edit reply must NOT be dropped
    assert "post-edit reply" in contents
