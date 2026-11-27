"""Probe/spec for the maintainer re-review of #6400 (fresh-session handoff, #6382).

Review: 2026-11-23T03:44:37Z (review 6016323756) on head e37f31ef03e8.
Each case below measures one defect the review reports against the current
handoff endpoint; every defect case is paired with a control that must keep
behaving as it does today.
"""
import copy

import pytest

import api.models as models
from api import routes
from api.models import Session, new_session


COMPRESSED = [
    {"role": "assistant", "content": "[context compaction] Summary of previous conversation."},
    {"role": "user", "content": "What is the capital of France?", "timestamp": 100.0},
    {"role": "assistant", "content": "The capital of France is Paris.", "timestamp": 101.0},
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    models.SESSIONS.clear()
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *a, **k: None)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *a, **k: True)
    caps = {}

    def _capture(handler, payload, status=200, **kw):
        caps.clear()
        caps.update(payload=payload, status=status)
        return True

    monkeypatch.setattr(routes, "j", _capture)
    monkeypatch.setattr("api.helpers.j", _capture)
    yield caps
    models.SESSIONS.clear()


def _source(messages=None, ctx=None, **over):
    s = new_session(**over)
    s.context_messages = copy.deepcopy(ctx if ctx is not None else COMPRESSED)
    s.messages = copy.deepcopy(messages if messages is not None else [
        {"role": "user", "content": "older question", "timestamp": 90.0},
        {"role": "assistant", "content": "older answer", "timestamp": 91.0},
        {"role": "user", "content": "latest question", "timestamp": 92.0},
        {"role": "assistant", "content": "latest answer", "timestamp": 93.0},
    ])
    s.save()
    return s


def _post(sid, caps):
    req = {"session_id": sid}
    routes._handle_session_handoff(object(), req)
    return caps.get("status"), caps.get("payload") or {}


def _cold(sid):
    models.SESSIONS.clear()
    return Session.load(sid)


# ── 1. the new session must not display the parent transcript ───────────────
def test_handoff_session_does_not_inherit_parent_display_rows(env):
    source = _source()
    status, payload = _post(source.session_id, env)
    assert status == 200, payload
    new_sid = payload["session"]["session_id"]
    fresh = _cold(new_sid)
    assert fresh is not None
    assert routes._merged_webui_lineage_messages_for_display(fresh) == []
    assert routes._merged_session_messages_for_display(fresh) == []


def test_genuine_compression_continuation_still_stitches_parent_rows(env):
    parent = _source()
    parent.pre_compression_snapshot = True
    parent.save()
    child = new_session()
    child.parent_session_id = parent.session_id
    child.messages = []
    child.save()
    merged = routes._merged_webui_lineage_messages_for_display(Session.load(child.session_id))
    assert len(merged) == len(parent.messages) > 0


# ── 2. the new session must be discoverable in history after eviction ──────
def test_handoff_session_survives_cold_history_listing(env):
    source = _source()
    status, payload = _post(source.session_id, env)
    assert status == 200, payload
    new_sid = payload["session"]["session_id"]
    models.SESSIONS.clear()
    listed = [row.get("session_id") for row in models.all_sessions()]
    assert new_sid in listed, "handoff session is missing from the sidebar listing"
    models.SESSIONS.clear()
    listed_again = [row.get("session_id") for row in models.all_sessions()]
    assert new_sid in listed_again, "handoff session is missing from the index path"


def test_handoff_session_row_survives_the_zero_message_prune(env):
    source = _source()
    status, payload = _post(source.session_id, env)
    assert status == 200, payload
    new_sid = payload["session"]["session_id"]
    fresh = _cold(new_sid)
    rows = [fresh.compact()]
    kept = [r.get("session_id") for r in routes._prune_orphaned_webui_zero_message_sessions(rows)]
    assert new_sid in kept


def test_plain_empty_untitled_session_stays_hidden(env):
    s = new_session()
    s.save()
    models.SESSIONS.clear()
    assert s.session_id not in [row.get("session_id") for row in models.all_sessions()]


# ── 3. provenance must survive a cold load + save ──────────────────────────
def test_handoff_provenance_survives_cold_load_and_save(env):
    source = _source()
    status, payload = _post(source.session_id, env)
    assert status == 200, payload
    new_sid = payload["session"]["session_id"]
    fresh = _cold(new_sid)
    assert getattr(fresh, "handoff_source_session_id", None) == source.session_id
    assert getattr(fresh, "handoff_created_at", None)
    assert getattr(fresh, "handoff_compressed", None) is True
    fresh.save()
    again = _cold(new_sid)
    assert getattr(again, "handoff_source_session_id", None) == source.session_id
    assert getattr(again, "handoff_created_at", None)
    assert getattr(again, "handoff_compressed", None) is True


# ── 4. a handoff session needs a new compression before another handoff ────
def test_repeat_handoff_after_cold_load_is_refused(env):
    source = _source()
    status, payload = _post(source.session_id, env)
    assert status == 200, payload
    new_sid = payload["session"]["session_id"]
    _cold(new_sid)
    status2, payload2 = _post(new_sid, env)
    assert status2 == 400, payload2
    assert "error" in payload2


def test_source_with_its_own_compression_is_still_eligible(env):
    source = _source()
    status, payload = _post(source.session_id, env)
    assert status == 200, payload


# ── 5. an incomplete newest turn must not create a session ─────────────────
def test_newest_unanswered_user_turn_is_refused(env):
    source = _source(messages=[
        {"role": "user", "content": "older question", "timestamp": 90.0},
        {"role": "assistant", "content": "older answer", "timestamp": 91.0},
        {"role": "user", "content": "still waiting", "timestamp": 92.0},
    ])
    status, payload = _post(source.session_id, env)
    assert status == 400, payload
    assert "error" in payload


def test_cancelled_partial_newest_turn_is_refused(env):
    source = _source(messages=[
        {"role": "user", "content": "older question", "timestamp": 90.0},
        {"role": "assistant", "content": "older answer", "timestamp": 91.0},
        {"role": "user", "content": "cancelled question", "timestamp": 92.0},
        {"role": "assistant", "content": "partial", "timestamp": 93.0, "_partial": True},
    ])
    status, payload = _post(source.session_id, env)
    assert status == 400, payload
    assert "error" in payload


def test_complete_newest_turn_is_accepted(env):
    source = _source()
    status, payload = _post(source.session_id, env)
    assert status == 200, payload


# ── 6. an assistant tool-use part is a declaration, not a result ───────────
def test_assistant_tool_use_part_does_not_satisfy_itself(env):
    messages = [
        {"role": "user", "content": "run it", "timestamp": 1.0},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "x", "tool_use_id": "x", "tool_name": "terminal", "input": {}},
        ], "timestamp": 2.0},
        {"role": "assistant", "content": "done", "timestamp": 3.0},
    ]
    assert routes._extract_latest_completed_exchange(messages) == (None, None)


def test_real_tool_result_still_completes_the_exchange(env):
    messages = [
        {"role": "user", "content": "run it", "timestamp": 1.0},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "x", "tool_use_id": "x", "tool_name": "terminal", "input": {}},
        ], "timestamp": 2.0},
        {"role": "tool", "tool_call_id": "x", "content": "ok", "timestamp": 2.5},
        {"role": "assistant", "content": "done", "timestamp": 3.0},
    ]
    user, assistant = routes._extract_latest_completed_exchange(messages)
    assert user is not None and assistant is not None
    assert assistant["content"] == "done"
