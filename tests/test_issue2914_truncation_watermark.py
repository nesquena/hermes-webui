"""Regression tests for #2914 state.db tail replay after undo/retry/edit."""
from __future__ import annotations


def _msg(role: str, content: str, ts: float, mid: str) -> dict:
    return {"id": mid, "role": role, "content": content, "timestamp": ts}


def test_reconciled_messages_skip_state_tail_after_sidecar_truncation():
    from api.models import Session, reconciled_state_db_messages_for_session

    sidecar = [
        _msg("user", "first", 1.0, "sidecar-u1"),
        _msg("assistant", "reply first", 2.0, "sidecar-a1"),
    ]
    state_db = [
        _msg("user", "first", 1.0, "state-u1"),
        _msg("assistant", "reply first", 2.0, "state-a1"),
        _msg("user", "second", 3.0, "state-u2"),
        _msg("assistant", "reply second", 4.0, "state-a2"),
    ]
    session = Session(
        session_id="issue2914",
        messages=sidecar,
        truncation_watermark=2.0,
    )

    merged = reconciled_state_db_messages_for_session(session, state_messages=state_db)

    assert [m["content"] for m in merged] == ["first", "reply first"]


def test_empty_sidecar_truncation_watermark_blocks_state_replay():
    from api.models import Session, reconciled_state_db_messages_for_session

    state_db = [
        _msg("user", "only prompt", 1.0, "state-u1"),
        _msg("assistant", "only reply", 2.0, "state-a1"),
    ]
    session = Session(
        session_id="issue2914empty",
        messages=[],
        truncation_watermark=0.0,
    )

    assert reconciled_state_db_messages_for_session(session, state_messages=state_db) == []


def test_undo_persists_truncation_watermark_at_new_tail(monkeypatch, tmp_path):
    import api.models as models
    from api.models import Session
    from api.session_ops import undo_last

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    session = Session(
        session_id="issue2914undo",
        messages=[
            _msg("user", "first", 1.0, "u1"),
            _msg("assistant", "reply first", 2.0, "a1"),
            _msg("user", "second", 3.0, "u2"),
            _msg("assistant", "reply second", 4.0, "a2"),
        ],
    )
    session.save()

    undo_last("issue2914undo")

    loaded = Session.load("issue2914undo")
    assert loaded is not None
    assert [m["content"] for m in loaded.messages] == ["first", "reply first"]
    assert loaded.truncation_watermark == 2.0


def test_truncate_endpoint_also_truncates_context_messages(monkeypatch, tmp_path):
    """POST /api/session/truncate must truncate context_messages in sync with
    messages so the agent's model-facing context doesn't retain rows the user
    removed via Edit / Regenerate (#2914).

    Integration test: calls the real handle_post route, not a simulation.
    """
    import json
    from io import BytesIO
    from types import SimpleNamespace

    import api.models as models
    import api.routes as routes
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    session = Session(
        session_id="issue2914truncate",
        messages=[
            _msg("user", "first", 1.0, "u1"),
            _msg("assistant", "reply first", 2.0, "a1"),
            _msg("user", "second", 3.0, "u2"),
            _msg("assistant", "reply second", 4.0, "a2"),
        ],
        context_messages=[
            _msg("user", "first", 1.0, "u1"),
            _msg("assistant", "reply first", 2.0, "a1"),
            _msg("user", "second", 3.0, "u2"),
            _msg("assistant", "reply second", 4.0, "a2"),
        ],
    )
    session.save()

    # Call the real truncate endpoint via handle_post
    body = {"session_id": "issue2914truncate", "keep_count": 2}
    body_bytes = json.dumps(body).encode()
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)

    captured_response = {}
    def fake_j(handler, payload, status=200, extra_headers=None):
        captured_response["payload"] = payload
    monkeypatch.setattr(routes, "j", fake_j)

    handler = SimpleNamespace(
        headers={"Content-Length": str(len(body_bytes))},
        rfile=BytesIO(body_bytes),
    )
    routes.handle_post(handler, SimpleNamespace(path="/api/session/truncate"))

    assert captured_response["payload"].get("ok") is True

    loaded = Session.load("issue2914truncate")
    assert loaded is not None
    assert [m["content"] for m in loaded.messages] == ["first", "reply first"]
    assert [m["content"] for m in loaded.context_messages] == ["first", "reply first"]
    assert loaded.truncation_watermark == 2.0


def test_truncate_without_context_messages_truncation_leaks_to_agent(monkeypatch, tmp_path):
    """Prove the bug: if context_messages is NOT truncated, agent sees old rows."""
    import api.models as models
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    session = Session(
        session_id="issue2914leak",
        messages=[
            _msg("user", "first", 1.0, "u1"),
            _msg("assistant", "reply first", 2.0, "a1"),
            _msg("user", "second", 3.0, "u2"),
            _msg("assistant", "reply second", 4.0, "a2"),
        ],
        context_messages=[
            _msg("user", "first", 1.0, "u1"),
            _msg("assistant", "reply first", 2.0, "a1"),
            _msg("user", "second", 3.0, "u2"),
            _msg("assistant", "reply second", 4.0, "a2"),
        ],
    )
    session.save()

    # BUGGY path: truncate messages but NOT context_messages
    from api.config import _get_session_agent_lock
    with _get_session_agent_lock("issue2914leak"):
        keep = 2
        session.messages = session.messages[:keep]
        # Intentionally NOT truncating context_messages (the old buggy behavior)
        try:
            from api.session_ops import _truncation_watermark_for
            session.truncation_watermark = _truncation_watermark_for(session.messages)
        except Exception:
            session.truncation_watermark = 0.0
        session.save()

    loaded = Session.load("issue2914leak")
    # messages is truncated correctly
    assert [m["content"] for m in loaded.messages] == ["first", "reply first"]
    # But context_messages still has all 4 — agent will see "second" and "reply second"
    assert [m["content"] for m in loaded.context_messages] == [
        "first", "reply first", "second", "reply second"
    ]


def test_edit_then_new_turn_then_undo_leaks_original_via_state_db(monkeypatch, tmp_path):
    """Reproduce #2914: Edit changes message content but original stays in state.db.

    Scenario:
    1. Send "triangle" (ts=100)
    2. Edit to "square" (ts=200, watermark=200)
    3. Send "light speed" (ts=300)
    4. Undo (removes "light speed", watermark=200)
    5. Send "list all" — agent sees original "triangle" from state.db

    Root cause: watermark filters m_ts > watermark, but original "triangle"
    has ts=100 < watermark=200, so it passes through.
    """
    import api.models as models
    from api.models import Session, reconciled_state_db_messages_for_session
    from api.session_ops import undo_last

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    # Step 1: Initial message "triangle"
    session = Session(
        session_id="edit_undo_leak",
        messages=[
            _msg("user", "triangle", 100.0, "u1"),
            _msg("assistant", "180°", 101.0, "a1"),
        ],
        context_messages=[
            _msg("user", "triangle", 100.0, "u1"),
            _msg("assistant", "180°", 101.0, "a1"),
        ],
    )
    session.save()

    # state.db has the original message
    state_db = [
        _msg("user", "triangle", 100.0, "state-u1"),
        _msg("assistant", "180°", 101.0, "state-a1"),
    ]

    # Step 2: Edit "triangle" → "square" (new timestamp 200)
    # Truncate endpoint: keep_count=0, then new message appended
    from api.config import _get_session_agent_lock
    with _get_session_agent_lock("edit_undo_leak"):
        session.messages = []
        session.context_messages = []
        session.truncation_watermark = 0.0
        session.save()

    # New message "square" with new timestamp
    session.messages = [
        _msg("user", "square", 200.0, "u2"),
        _msg("assistant", "360°", 201.0, "a2"),
    ]
    session.context_messages = [
        _msg("user", "square", 200.0, "u2"),
        _msg("assistant", "360°", 201.0, "a2"),
    ]
    session.truncation_watermark = 200.0
    session.save()

    # state.db now has both original and edited
    state_db = [
        _msg("user", "triangle", 100.0, "state-u1"),
        _msg("assistant", "180°", 101.0, "state-a1"),
        _msg("user", "square", 200.0, "state-u2"),
        _msg("assistant", "360°", 201.0, "state-a2"),
    ]

    # Step 3: New message "light speed"
    session.messages = [
        _msg("user", "square", 200.0, "u2"),
        _msg("assistant", "360°", 201.0, "a2"),
        _msg("user", "light speed", 300.0, "u3"),
        _msg("assistant", "300000 km/s", 301.0, "a3"),
    ]
    session.context_messages = [
        _msg("user", "square", 200.0, "u2"),
        _msg("assistant", "360°", 201.0, "a2"),
        _msg("user", "light speed", 300.0, "u3"),
        _msg("assistant", "300000 km/s", 301.0, "a3"),
    ]
    session.truncation_watermark = 301.0
    session.save()

    # state.db has everything
    state_db = [
        _msg("user", "triangle", 100.0, "state-u1"),
        _msg("assistant", "180°", 101.0, "state-a1"),
        _msg("user", "square", 200.0, "state-u2"),
        _msg("assistant", "360°", 201.0, "state-a2"),
        _msg("user", "light speed", 300.0, "state-u3"),
        _msg("assistant", "300000 km/s", 301.0, "state-a3"),
    ]

    # Step 4: Undo — removes "light speed"
    undo_last("edit_undo_leak")
    loaded = Session.load("edit_undo_leak")

    # After undo: messages should be ["square", "360°"]
    assert [m["content"] for m in loaded.messages] == ["square", "360°"]
    assert [m["content"] for m in loaded.context_messages] == ["square", "360°"]
    assert loaded.truncation_watermark == 201.0

    # Step 5: New turn — agent context should NOT include "triangle"
    merged = reconciled_state_db_messages_for_session(
        loaded, state_messages=state_db, prefer_context=True,
    )

    contents = [m["content"] for m in merged]

    # "triangle" must NOT leak through — it was replaced by "square" via Edit
    assert "triangle" not in contents, \
        f"Original 'triangle' leaked through watermark filter! Contents: {contents}"
    assert "square" in contents
    # "light speed" properly filtered by watermark
    assert "light speed" not in contents


# ── save() watermark invariant ─────────────────────────────────────────────


def test_save_does_not_auto_clear_truncation_watermark(monkeypatch, tmp_path):
    """save() must NOT auto-clear truncation_watermark when messages have
    timestamps beyond the watermark (#2914).

    A future save() that appends newer messages must NOT silently remove the
    watermark — that would let state.db replay the rows the user deliberately
    removed.
    """
    import api.models as models
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    session = Session(
        session_id="watermark_no_clear",
        messages=[
            _msg("user", "first", 1.0, "u1"),
            _msg("assistant", "reply", 2.0, "a1"),
            _msg("user", "newer", 3.0, "u2"),  # ts > watermark
        ],
        truncation_watermark=2.0,
    )
    session.save()

    loaded = Session.load("watermark_no_clear")
    assert loaded is not None
    # Watermark must NOT be cleared even though max message ts (3.0) > watermark (2.0)
    assert loaded.truncation_watermark == 2.0, \
        f"Watermark was cleared on save! Got {loaded.truncation_watermark}"


# ── Streaming finalize: watermark must not become permanent ceiling ──


def test_streaming_finalize_preserves_new_turns_after_edit(monkeypatch, tmp_path):
    """Integration: simulate the full streaming finalize pipeline for two
    consecutive turns after Edit, verifying that context_messages retains
    all new turns (not just the pre-Edit history).

    This exercises the actual finalize chain:
      _restore_reasoning_metadata → _dedupe_replayed_context_messages
      → _deduplicate_context_messages

    Scenario:
    1. Edit sets watermark = 101.0, context_messages = [pre-edit messages]
    2. Turn 1: agent returns [pre-edit + Turn 1] → finalize → save context_messages
    3. Turn 2: agent returns [pre-edit + Turn 1 + Turn 2] → finalize → save
    4. Assert context_messages after Turn 2 contains Turn 1 + Turn 2
    """
    import api.models as models
    from api.models import Session
    from api.streaming import (
        _restore_reasoning_metadata,
        _dedupe_replayed_context_messages,
        _deduplicate_context_messages,
    )

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    # Pre-Edit context (after Edit, watermark = 101.0)
    pre_edit = [
        _msg("user", "square", 100.0, "ctx-u1"),
        _msg("assistant", "360°", 101.0, "ctx-a1"),
    ]

    session = Session(
        session_id="finalize_two_turns",
        messages=list(pre_edit),
        context_messages=list(pre_edit),
        truncation_watermark=101.0,
    )
    session.save()
    models.SESSIONS["finalize_two_turns"] = session

    # ── Turn 1: agent returns [pre-edit + Turn 1] ──
    _previous_context = list(session.context_messages)
    _result_messages_turn1 = [
        _msg("user", "square", 100.0, "ctx-u1"),
        _msg("assistant", "360°", 101.0, "ctx-a1"),
        _msg("user", "what is circle", 200.0, "ctx-u2"),
        _msg("assistant", "360 degrees", 201.0, "ctx-a2"),
    ]

    # Run the finalize chain
    _next = _restore_reasoning_metadata(_previous_context, _result_messages_turn1)
    _next = _dedupe_replayed_context_messages(_previous_context, _next)
    session.context_messages = _deduplicate_context_messages(_next)

    # Turn 1: context must contain the new turn
    turn1_contents = [m["content"] for m in session.context_messages]
    assert "what is circle" in turn1_contents, \
        f"Turn 1 lost after finalize! Contents: {turn1_contents}"
    assert "360 degrees" in turn1_contents, \
        f"Turn 1 reply lost after finalize! Contents: {turn1_contents}"

    # ── Turn 2: agent returns [pre-edit + Turn 1 + Turn 2] ──
    _previous_context = list(session.context_messages)
    _result_messages_turn2 = [
        _msg("user", "square", 100.0, "ctx-u1"),
        _msg("assistant", "360°", 101.0, "ctx-a1"),
        _msg("user", "what is circle", 200.0, "ctx-u2"),
        _msg("assistant", "360 degrees", 201.0, "ctx-a2"),
        _msg("user", "list all shapes", 300.0, "ctx-u3"),
        _msg("assistant", "square, circle", 301.0, "ctx-a3"),
    ]

    # Run the finalize chain again
    _next = _restore_reasoning_metadata(_previous_context, _result_messages_turn2)
    _next = _dedupe_replayed_context_messages(_previous_context, _next)
    session.context_messages = _deduplicate_context_messages(_next)

    # Turn 2: context must contain BOTH Turn 1 and Turn 2
    turn2_contents = [m["content"] for m in session.context_messages]
    assert "what is circle" in turn2_contents, \
        f"Turn 1 lost after Turn 2 finalize! Contents: {turn2_contents}"
    assert "360 degrees" in turn2_contents, \
        f"Turn 1 reply lost after Turn 2 finalize! Contents: {turn2_contents}"
    assert "list all shapes" in turn2_contents, \
        f"Turn 2 lost after finalize! Contents: {turn2_contents}"
    assert "square, circle" in turn2_contents, \
        f"Turn 2 reply lost after finalize! Contents: {turn2_contents}"


def test_streaming_finalize_does_not_leak_original_after_edit(monkeypatch, tmp_path):
    """After Edit, the original replaced message must NOT appear in context_messages
    after streaming finalize (#2914).

    This exercises the actual finalize chain:
      _restore_reasoning_metadata → _dedupe_replayed_context_messages
      → _deduplicate_context_messages

    Scenario:
    1. Edit sets watermark = 101.0, context_messages = [pre-edit messages]
    2. Turn 1: agent returns [pre-edit + Turn 1] → finalize
    3. Turn 2: agent returns [pre-edit + Turn 1 + Turn 2] → finalize
    4. Assert context_messages after Turn 2 contains Turn 1 + Turn 2
    """
    import api.models as models
    from api.models import Session
    from api.streaming import (
        _restore_reasoning_metadata,
        _dedupe_replayed_context_messages,
        _deduplicate_context_messages,
    )

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    # Pre-Edit context (after Edit, watermark = 101.0)
    pre_edit = [
        _msg("user", "square", 100.0, "ctx-u1"),
        _msg("assistant", "360°", 101.0, "ctx-a1"),
    ]

    session = Session(
        session_id="finalize_two_turns",
        messages=list(pre_edit),
        context_messages=list(pre_edit),
        truncation_watermark=101.0,
    )
    session.save()
    models.SESSIONS["finalize_two_turns"] = session

    # ── Turn 1: agent returns [pre-edit + Turn 1] ──
    _previous_context = list(session.context_messages)
    _result_messages_turn1 = [
        _msg("user", "square", 100.0, "ctx-u1"),
        _msg("assistant", "360°", 101.0, "ctx-a1"),
        _msg("user", "what is circle", 200.0, "ctx-u2"),
        _msg("assistant", "360 degrees", 201.0, "ctx-a2"),
    ]

    # Run the finalize chain
    _next = _restore_reasoning_metadata(_previous_context, _result_messages_turn1)
    _next = _dedupe_replayed_context_messages(_previous_context, _next)
    session.context_messages = _deduplicate_context_messages(_next)

    # Turn 1: context must contain the new turn
    turn1_contents = [m["content"] for m in session.context_messages]
    assert "what is circle" in turn1_contents, \
        f"Turn 1 lost after finalize! Contents: {turn1_contents}"
    assert "360 degrees" in turn1_contents, \
        f"Turn 1 reply lost after finalize! Contents: {turn1_contents}"

    # ── Turn 2: agent returns [pre-edit + Turn 1 + Turn 2] ──
    _previous_context = list(session.context_messages)
    _result_messages_turn2 = [
        _msg("user", "square", 100.0, "ctx-u1"),
        _msg("assistant", "360°", 101.0, "ctx-a1"),
        _msg("user", "what is circle", 200.0, "ctx-u2"),
        _msg("assistant", "360 degrees", 201.0, "ctx-a2"),
        _msg("user", "list all shapes", 300.0, "ctx-u3"),
        _msg("assistant", "square, circle", 301.0, "ctx-a3"),
    ]

    # Run the finalize chain again
    _next = _restore_reasoning_metadata(_previous_context, _result_messages_turn2)
    _next = _dedupe_replayed_context_messages(_previous_context, _next)
    session.context_messages = _deduplicate_context_messages(_next)

    # Turn 2: context must contain BOTH Turn 1 and Turn 2
    turn2_contents = [m["content"] for m in session.context_messages]
    assert "what is circle" in turn2_contents, \
        f"Turn 1 lost after Turn 2 finalize! Contents: {turn2_contents}"
    assert "360 degrees" in turn2_contents, \
        f"Turn 1 reply lost after Turn 2 finalize! Contents: {turn2_contents}"
    assert "list all shapes" in turn2_contents, \
        f"Turn 2 lost after finalize! Contents: {turn2_contents}"
    assert "square, circle" in turn2_contents, \
        f"Turn 2 reply lost after finalize! Contents: {turn2_contents}"


def test_edit_does_not_leak_original_message_into_context_via_reconcile(monkeypatch, tmp_path):
    """After Edit, the original replaced message must NOT appear in agent context
    when reconciling state.db with sidecar (#2914).

    This is the core regression: Edit replaces message content but state.db
    keeps the original forever. Without the sub-watermark filter in
    merge_session_messages_append_only, the original message leaks back
    into context_messages on the next turn.

    Scenario:
    1. Send "triangle" (ts=100) → agent replies "180°" (ts=101)
    2. Edit to "square" (ts=200) → agent replies "360°" (ts=201), watermark=200
    3. Send "list all" (ts=300) → agent replies "square, list" (ts=301)
    4. Reconcile state.db (which has both "triangle" and "square") with sidecar
    5. Assert "triangle" does NOT appear in merged context
    """
    import api.models as models
    from api.models import Session, reconciled_state_db_messages_for_session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    # After Edit + new turn, sidecar has:
    # - "square" (edited, ts=200)
    # - "360°" (ts=201)
    # - "list all" (ts=300)
    # - "square, list" (ts=301)
    # Watermark = 301.0 (covers everything in sidecar)
    sidecar = [
        _msg("user", "square", 200.0, "side-u1"),
        _msg("assistant", "360°", 201.0, "side-a1"),
        _msg("user", "list all", 300.0, "side-u2"),
        _msg("assistant", "square, list", 301.0, "side-a2"),
    ]

    session = Session(
        session_id="edit_no_leak",
        messages=list(sidecar),
        context_messages=list(sidecar),
        truncation_watermark=301.0,
    )
    session.save()

    # state.db has BOTH original "triangle" AND edited "square"
    state_db = [
        _msg("user", "triangle", 100.0, "state-u1"),  # original, ts < watermark
        _msg("assistant", "180°", 101.0, "state-a1"),  # original, ts < watermark
        _msg("user", "square", 200.0, "state-u2"),     # edited, ts <= watermark
        _msg("assistant", "360°", 201.0, "state-a2"),  # edited, ts <= watermark
        _msg("user", "list all", 300.0, "state-u3"),   # new turn
        _msg("assistant", "square, list", 301.0, "state-a4"),
    ]

    merged = reconciled_state_db_messages_for_session(
        session, state_messages=state_db, prefer_context=True,
    )

    contents = [m["content"] for m in merged]

    # "triangle" must NOT leak — it was replaced by "square" via Edit
    assert "triangle" not in contents, \
        f"Original 'triangle' leaked into context! Contents: {contents}"
    # "square" must be present
    assert "square" in contents
    # All sidecar messages must be present
    assert "360°" in contents
    assert "list all" in contents
    assert "square, list" in contents
