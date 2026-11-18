"""Regression tests for bounded sidebar session-list caching."""

import json
import threading

from api import route_session_list_cache as cache
from api import models
from api import routes
from api.models import Session


def _key():
    return cache._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )


def test_cache_drops_full_transcript_fields_before_storing(monkeypatch):
    cache._session_list_cache_clear()
    payload = {
        "sessions": [{
            "session_id": "huge",
            "title": "Large session",
            "message_count": 1,
            "messages": [{"role": "assistant", "content": "x" * 1000000}],
            "tool_calls": [{"arguments": "y" * 1000000}],
            "context_messages": ["z" * 1000000],
            "gateway_routing_history": [{"provider": "old"}] * 1000,
        }],
        "sidebar_reference_sessions": [],
        "settings": {"show_cli_sessions": False},
    }
    monkeypatch.setattr(cache, "_session_list_cache_resolved_source_stamp", lambda _key: ("stable",))

    cache._session_list_cache_set(_key(), payload)
    cached, fresh = cache._session_list_cache_get(_key(), allow_stale=False)

    assert fresh is True
    assert cached["sessions"] == [{
        "session_id": "huge",
        "title": "Large session",
        "message_count": 1,
    }]
    assert "messages" not in cached["sessions"][0]
    assert "tool_calls" not in cached["sessions"][0]
    assert "context_messages" not in cached["sessions"][0]
    assert "gateway_routing_history" not in cached["sessions"][0]


def test_cache_read_returns_independent_bounded_rows(monkeypatch):
    cache._session_list_cache_clear()
    monkeypatch.setattr(cache, "_session_list_cache_resolved_source_stamp", lambda _key: ("stable",))
    key = _key()
    cache._session_list_cache_set(key, {"sessions": [{"session_id": "s1", "title": "One"}]})

    first, _ = cache._session_list_cache_get(key)
    first["sessions"][0]["title"] = "mutated"
    second, _ = cache._session_list_cache_get(key)

    assert second["sessions"][0]["title"] == "One"


def test_cache_read_isolates_nested_rows_and_settings(monkeypatch):
    cache._session_list_cache_clear()
    monkeypatch.setattr(cache, "_session_list_cache_resolved_source_stamp", lambda _key: ("stable",))
    key = _key()
    cache._session_list_cache_set(key, {
        "sessions": [{
            "session_id": "s1",
            "gateway_routing": {
                "routing": [{"provider": "first", "model": "model-a"}],
            },
        }],
        "settings": {"show_cli_sessions": False},
    })

    first, _ = cache._session_list_cache_get(key)
    assert first is not None
    first["sessions"][0]["gateway_routing"]["routing"][0]["provider"] = "mutated"
    first["settings"]["show_cli_sessions"] = True
    second, _ = cache._session_list_cache_get(key)
    assert second is not None

    assert second["sessions"][0]["gateway_routing"]["routing"][0]["provider"] == "first"
    assert second["settings"]["show_cli_sessions"] is False


def test_invalidation_during_projection_cannot_reinsert_stale_payload(monkeypatch):
    cache._session_list_cache_clear()
    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    stale_payload = {"sessions": [{"session_id": "before-clear", "title": "stale"}]}
    fresh_payload = {"sessions": [{"session_id": "after-clear", "title": "fresh"}]}
    projection_started = threading.Event()
    release_projection = threading.Event()
    real_project = cache._session_list_cache_bounded_payload

    def blocked_project(value):
        if value == stale_payload:
            projection_started.set()
            assert release_projection.wait(5), "projection release timed out"
        return real_project(value)

    monkeypatch.setattr(cache, "_session_list_cache_bounded_payload", blocked_project)
    result = {}
    build_count = 0

    def builder():
        nonlocal build_count
        build_count += 1
        return stale_payload if build_count == 1 else fresh_payload

    def owner():
        result["payload"] = routes._get_cached_session_list_payload(
            key=key,
            builder=builder,
        )

    thread = threading.Thread(target=owner)
    thread.start()
    assert projection_started.wait(5), "cache set never reached projection"
    cache._session_list_cache_clear()
    release_projection.set()
    thread.join(5)

    assert not thread.is_alive()
    assert build_count == 2
    assert result["payload"] == fresh_payload
    cached, fresh = cache._session_list_cache_get(key)
    assert fresh is True
    assert cached == fresh_payload


def test_sidebar_field_allowlist_has_one_authority():
    fields = getattr(cache, "_SIDEBAR_SESSION_RESPONSE_FIELDS", None)
    assert fields is not None
    assert routes._SIDEBAR_SESSION_RESPONSE_FIELDS is fields
    assert cache._session_list_cache_sidebar_fields() is fields


def test_sidebar_compact_omits_heavy_session_metadata():
    session = Session(
        session_id="heavy",
        title="Heavy",
        messages=[{"role": "user", "content": "hello"}],
        compression_anchor_summary="s" * 1000000,
        compression_anchor_details={"detail": "d" * 1000000},
        context_engine_state={"state": "x" * 1000000},
        compression_recovery={"recovery": "r" * 1000000},
        gateway_routing_history=[{"provider": "old"}] * 10000,
        composer_draft={"draft": "c" * 1000000},
        process_wakeup_pause={"pause": "p" * 1000000},
        share_token="token-value",
    )

    normal = session.compact()
    sidebar = session.compact(sidebar_metadata_only=True)

    assert normal["gateway_routing_history"]
    assert normal["compression_anchor_summary"]
    for field in (
        "compression_anchor_summary", "compression_anchor_details",
        "context_engine_state", "compression_recovery",
        "gateway_routing_history", "composer_draft",
        "process_wakeup_pause", "share_token",
    ):
        assert field not in sidebar


def test_sidebar_metadata_only_projects_existing_index_rows(monkeypatch, tmp_path):
    heavy_fields = {
        "compression_anchor_summary": "s" * 1000000,
        "compression_anchor_details": {"detail": "d" * 1000000},
        "context_engine_state": {"state": "x" * 1000000},
        "compression_recovery": {"recovery": "r" * 1000000},
        "gateway_routing_history": [{"provider": "old"}] * 1000,
        "composer_draft": {"draft": "c" * 1000000},
        "process_wakeup_pause": {"pause": "p" * 1000000},
        "share_token": "token-value",
    }
    index_path = tmp_path / "_index.json"
    index_path.write_text(json.dumps([{
        "session_id": "indexed",
        "title": "Indexed",
        "message_count": 1,
        "user_message_count": 1,
        "created_at": 1.0,
        "updated_at": 2.0,
        "last_message_at": 2.0,
        "profile": "default",
        **heavy_fields,
    }]))
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_path)
    monkeypatch.setattr(models, "SESSIONS", {})
    monkeypatch.setattr(models, "_persisted_session_ids_snapshot", lambda: {"indexed"})
    monkeypatch.setattr(models, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(models, "_apply_sidebar_state_db_overrides", lambda _rows: None)

    rows = models.all_sessions(
        include_lineage_metadata=False,
        sidebar_metadata_only=True,
    )

    assert [row["session_id"] for row in rows] == ["indexed"]
    for field in heavy_fields:
        assert field not in rows[0]
