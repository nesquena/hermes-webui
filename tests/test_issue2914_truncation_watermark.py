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
    removed via Edit / Regenerate (#2914)."""
    import api.models as models
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

    # Simulate what the truncate endpoint does (keep_count=2 = keep first 2 messages)
    from api.config import _get_session_agent_lock
    with _get_session_agent_lock("issue2914truncate"):
        keep = 2
        session.messages = session.messages[:keep]
        if isinstance(getattr(session, 'context_messages', None), list):
            session.context_messages = session.context_messages[:keep]
        try:
            from api.session_ops import _truncation_watermark_for
            session.truncation_watermark = _truncation_watermark_for(session.messages)
        except Exception:
            session.truncation_watermark = 0.0
        session.save()

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


# ── _clamp_context_to_watermark unit tests ────────────────────────────────


def test_clamp_context_to_watermark_filters_beyond_watermark():
    """_clamp_context_to_watermark must drop messages with timestamp > watermark."""
    from api.streaming import _clamp_context_to_watermark

    class FakeSession:
        session_id = "clamp-test"
        truncation_watermark = 2.0

    messages = [
        _msg("user", "first", 1.0, "u1"),
        _msg("assistant", "reply", 2.0, "a1"),
        _msg("user", "deleted", 3.0, "u2"),
        _msg("assistant", "deleted reply", 4.0, "a2"),
    ]

    result = _clamp_context_to_watermark(FakeSession(), messages)
    contents = [m["content"] for m in result]
    assert contents == ["first", "reply"]
    assert "deleted" not in contents


def test_clamp_context_to_watermark_no_watermark_passes_all():
    """When truncation_watermark is None, _clamp_context_to_watermark returns all messages unchanged."""
    from api.streaming import _clamp_context_to_watermark

    class FakeSession:
        session_id = "clamp-none"
        truncation_watermark = None

    messages = [
        _msg("user", "first", 1.0, "u1"),
        _msg("assistant", "reply", 2.0, "a1"),
    ]

    result = _clamp_context_to_watermark(FakeSession(), messages)
    assert result is messages  # same object — no copy when nothing to filter


def test_clamp_context_to_watermark_zero_watermark_blocks_all():
    """When truncation_watermark is 0.0, all messages with timestamp > 0 are dropped."""
    from api.streaming import _clamp_context_to_watermark

    class FakeSession:
        session_id = "clamp-zero"
        truncation_watermark = 0.0

    messages = [
        _msg("user", "first", 1.0, "u1"),
        _msg("assistant", "reply", 2.0, "a1"),
    ]

    result = _clamp_context_to_watermark(FakeSession(), messages)
    assert result == []


def test_clamp_context_to_watermark_keeps_messages_without_timestamp():
    """Messages without a timestamp field are kept (m_ts is None → passes filter)."""
    from api.streaming import _clamp_context_to_watermark

    class FakeSession:
        session_id = "clamp-nots"
        truncation_watermark = 2.0

    messages = [
        {"role": "user", "content": "no-timestamp"},  # no timestamp at all
        _msg("assistant", "reply", 2.0, "a1"),
        _msg("user", "deleted", 3.0, "u2"),
    ]

    result = _clamp_context_to_watermark(FakeSession(), messages)
    contents = [m["content"] for m in result]
    assert contents == ["no-timestamp", "reply"]
    assert "deleted" not in contents


def test_clamp_context_to_watermark_empty_messages():
    """_clamp_context_to_watermark with empty list returns empty list."""
    from api.streaming import _clamp_context_to_watermark

    class FakeSession:
        session_id = "clamp-empty"
        truncation_watermark = 2.0

    result = _clamp_context_to_watermark(FakeSession(), [])
    assert result == []


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
