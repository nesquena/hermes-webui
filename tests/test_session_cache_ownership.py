import json
from io import BytesIO
from types import SimpleNamespace

import pytest

import api.config as config
import api.models as models
from api.models import Session, get_session


def test_get_session_evicts_cached_object_with_wrong_session_id(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    models.SESSIONS.clear()

    requested = Session(
        session_id="requested-tip",
        title="Requested Tip",
        messages=[{"role": "user", "content": "correct transcript"}],
    )
    requested.save()
    wrong = Session(
        session_id="older-segment",
        title="Older Segment",
        messages=[{"role": "user", "content": "wrong transcript"}],
    )

    models.SESSIONS["requested-tip"] = wrong

    loaded = get_session("requested-tip")

    assert loaded.session_id == "requested-tip"
    assert loaded.title == "Requested Tip"
    assert loaded.messages == [{"role": "user", "content": "correct transcript"}]
    assert models.SESSIONS["requested-tip"] is loaded

    models.SESSIONS.clear()


def test_get_session_metadata_only_evicts_cached_object_with_wrong_session_id(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    models.SESSIONS.clear()

    requested = Session(
        session_id="requested-tip",
        title="Requested Tip",
        messages=[{"role": "user", "content": "correct transcript"}],
    )
    requested.save()
    models.SESSIONS["requested-tip"] = Session(session_id="older-segment", title="Older Segment")

    loaded = get_session("requested-tip", metadata_only=True)

    assert loaded.session_id == "requested-tip"
    assert loaded.title == "Requested Tip"
    assert models.SESSIONS.get("requested-tip") is None

    models.SESSIONS.clear()


def test_compression_cache_migration_never_moves_unverified_cached_object_to_new_sid():
    streaming_src = open("api/streaming.py", encoding="utf-8").read()

    assert "SESSIONS[new_sid] = SESSIONS.pop(old_sid)" not in streaming_src
    assert "cached_old_session is not s" in streaming_src
    assert "SESSIONS[new_sid] = s" in streaming_src


def test_cached_agent_session_identity_matches_requested_sid():
    from api.streaming import _cached_agent_matches_session, _cached_agent_session_identity

    matching = SimpleNamespace(session_id="requested")
    mismatched = SimpleNamespace(session_id="other")
    legacy = SimpleNamespace()
    db_owned = SimpleNamespace(_session_db=SimpleNamespace(session_id="requested"))

    assert _cached_agent_session_identity(matching) == "requested"
    assert _cached_agent_matches_session(matching, "requested") is True
    assert _cached_agent_matches_session(mismatched, "requested") is False
    assert _cached_agent_matches_session(db_owned, "requested") is True
    assert _cached_agent_matches_session(legacy, "requested") is True


def test_handle_chat_steer_evicts_mismatched_cached_agent(monkeypatch):
    import api.streaming as streaming
    from api.streaming import _handle_chat_steer

    class Handler:
        headers = {}

        def __init__(self):
            self.status = None
            self.response_headers = []
            self.wfile = BytesIO()

        def send_response(self, status):
            self.status = status

        def send_header(self, key, value):
            self.response_headers.append((key, value))

        def end_headers(self):
            pass

    wrong_agent = SimpleNamespace(session_id="other-session", steer=lambda _text: True)
    closed_entries = []
    monkeypatch.setattr(
        streaming,
        "_close_cached_agent_entry_at_session_boundary",
        lambda session_id, entry: closed_entries.append((session_id, entry)),
    )
    config.SESSION_AGENT_CACHE.clear()
    config.SESSION_AGENT_CACHE["requested"] = (wrong_agent, "sig")
    handler = Handler()

    _handle_chat_steer(handler, {"session_id": "requested", "text": "please steer"})

    payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert handler.status == 200
    assert payload == {"accepted": False, "fallback": "no_cached_agent", "stream_id": None}
    assert "requested" not in config.SESSION_AGENT_CACHE
    assert closed_entries == [("requested", (wrong_agent, "sig"))]

    config.SESSION_AGENT_CACHE.clear()


# ── #6625: cache-poisoning eviction guard on terminal errors ──────────────


def _make_cached_agent(sid="session-1"):
    return SimpleNamespace(session_id=sid)


@pytest.mark.parametrize(
    "err_type",
    [
        "cancelled",
        "interrupted",
        "quota_exhausted",
        "rate_limit",
        "tool_limit_reached",
        "compression_exhausted",
    ],
)
def test_terminal_eviction_preserves_cached_agent_on_non_poisoning_err_types(monkeypatch, err_type):
    """#6625 review: user Stop, transient rate/quota, iteration budgets, and
    compression exhaustion must NOT evict the cached agent — evicting there
    would force a costly system-prompt rebuild on the next turn."""
    import api.streaming as streaming
    from api.streaming import _invalidate_cached_agent_on_terminal_error

    closed_entries = []
    monkeypatch.setattr(
        streaming,
        "_close_cached_agent_entry_at_session_boundary",
        lambda session_id, entry: closed_entries.append((session_id, entry)),
    )
    agent = _make_cached_agent()
    config.SESSION_AGENT_CACHE.clear()
    config.SESSION_AGENT_CACHE["session-1"] = (agent, "sig")

    _invalidate_cached_agent_on_terminal_error("session-1", err_type, agent=agent)

    assert "session-1" in config.SESSION_AGENT_CACHE
    assert closed_entries == []


def test_terminal_eviction_evicts_cached_agent_on_non_retryable_400(monkeypatch):
    """#6625 review: a genuinely non-retryable provider error (HTTP 400) must
    evict and close the poisoned cached agent so the next turn rebuilds fresh."""
    import api.streaming as streaming
    from api.streaming import _invalidate_cached_agent_on_terminal_error

    closed_entries = []
    monkeypatch.setattr(
        streaming,
        "_close_cached_agent_entry_at_session_boundary",
        lambda session_id, entry: closed_entries.append((session_id, entry)),
    )
    agent = _make_cached_agent()
    config.SESSION_AGENT_CACHE.clear()
    config.SESSION_AGENT_CACHE["session-1"] = (agent, "sig")

    _invalidate_cached_agent_on_terminal_error("session-1", "model_not_found", agent=agent)

    assert "session-1" not in config.SESSION_AGENT_CACHE
    assert closed_entries == [("session-1", (agent, "sig"))]


def test_terminal_eviction_skips_replaced_cache_entry(monkeypatch):
    """#6625 review: if a concurrent turn replaced the cached entry, never
    evict the healthy replacement — only evict when the entry still IS the
    agent used by this failing turn."""
    import api.streaming as streaming
    from api.streaming import _invalidate_cached_agent_on_terminal_error

    closed_entries = []
    monkeypatch.setattr(
        streaming,
        "_close_cached_agent_entry_at_session_boundary",
        lambda session_id, entry: closed_entries.append((session_id, entry)),
    )
    turn_agent = _make_cached_agent("turn-agent")
    replacement = _make_cached_agent("replacement")
    config.SESSION_AGENT_CACHE.clear()
    config.SESSION_AGENT_CACHE["session-1"] = (replacement, "new-sig")

    _invalidate_cached_agent_on_terminal_error("session-1", "model_not_found", agent=turn_agent)

    assert "session-1" in config.SESSION_AGENT_CACHE
    assert closed_entries == []


def test_terminal_eviction_keys_pop_off_payload_session_id(monkeypatch):
    """#6625 review: the pop must be keyed off the same session id reported in
    the error payload (s.session_id), not the local session_id variable — on
    the compression-continuation path the live agent is migrated under new_sid
    while the local variable still holds old_sid."""
    import api.streaming as streaming
    from api.streaming import _invalidate_cached_agent_on_terminal_error

    closed_entries = []
    monkeypatch.setattr(
        streaming,
        "_close_cached_agent_entry_at_session_boundary",
        lambda session_id, entry: closed_entries.append((session_id, entry)),
    )
    agent = _make_cached_agent("new_sid")
    config.SESSION_AGENT_CACHE.clear()
    # Live agent was migrated under new_sid (= payload/s.session_id).
    config.SESSION_AGENT_CACHE["new_sid"] = (agent, "sig")

    _invalidate_cached_agent_on_terminal_error("new_sid", "model_not_found", agent=agent)

    assert "new_sid" not in config.SESSION_AGENT_CACHE
    assert closed_entries == [("new_sid", (agent, "sig"))]


def test_terminal_eviction_noop_when_no_agent_used_this_turn(monkeypatch):
    """#6625 review: when no agent was used this turn (agent is None), any
    cached entry belongs to a previous healthy turn and must be preserved."""
    import api.streaming as streaming
    from api.streaming import _invalidate_cached_agent_on_terminal_error

    closed_entries = []
    monkeypatch.setattr(
        streaming,
        "_close_cached_agent_entry_at_session_boundary",
        lambda session_id, entry: closed_entries.append((session_id, entry)),
    )
    config.SESSION_AGENT_CACHE.clear()
    config.SESSION_AGENT_CACHE["session-1"] = (_make_cached_agent(), "sig")

    _invalidate_cached_agent_on_terminal_error("session-1", "model_not_found", agent=None)

    assert "session-1" in config.SESSION_AGENT_CACHE
    assert closed_entries == []
