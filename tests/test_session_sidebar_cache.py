import threading
from types import SimpleNamespace

import api.routes as routes
from api import session_events


class _StageRecorder:
    def __init__(self):
        self.stages = []

    def stage(self, name):
        self.stages.append(str(name))


def _session_cache_payload(marker: str, *, all_profiles: bool = False) -> dict:
    return {
        "sessions": [{"session_id": marker}],
        "cli_count": 0,
        "all_profiles": all_profiles,
        "active_profile": None,
        "other_profile_count": 0,
    }


def test_session_list_cache_key_separates_profile_and_all_profiles():
    routes._session_list_cache_clear()

    calls = []

    def builder_profile_a():
        calls.append("default")
        return _session_cache_payload("a")

    def builder_profile_a_all():
        calls.append("default_all")
        return _session_cache_payload("a_all", all_profiles=True)

    def builder_profile_b():
        calls.append("other")
        return _session_cache_payload("b")

    key_a = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    key_a_all = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=True,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    key_b = routes._session_list_cache_key(
        active_profile="other",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    assert routes._get_cached_session_list_payload(key=key_a, builder=builder_profile_a) == _session_cache_payload("a")
    assert calls == ["default"]
    assert routes._get_cached_session_list_payload(key=key_a, builder=builder_profile_a) == _session_cache_payload("a")
    assert calls == ["default"]
    assert routes._get_cached_session_list_payload(key=key_b, builder=builder_profile_b) == _session_cache_payload("b")
    assert calls == ["default", "other"]
    assert routes._get_cached_session_list_payload(key=key_a_all, builder=builder_profile_a_all) == _session_cache_payload("a_all", all_profiles=True)
    assert calls == ["default", "other", "default_all"]
    assert routes._get_cached_session_list_payload(key=key_a, builder=builder_profile_a) == _session_cache_payload("a")
    assert calls == ["default", "other", "default_all"]


def test_session_list_cache_singleflight_rebuild_once():
    routes._session_list_cache_clear()

    started = threading.Event()
    release = threading.Event()
    calls = 0
    lock = threading.Lock()

    def builder():
        nonlocal calls
        with lock:
            calls += 1
        started.set()
        release.wait()
        return _session_cache_payload("singleflight")

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    results = []
    errors = []

    def reader():
        try:
            results.append(routes._get_cached_session_list_payload(key=key, builder=builder))
        except Exception as exc:
            errors.append(exc)

    owner = threading.Thread(target=reader)
    follower = threading.Thread(target=reader)
    owner.start()
    assert started.wait(1.0)
    follower.start()
    release.set()
    owner.join(2)
    follower.join(2)
    assert not errors
    assert len(results) == 2
    assert results[0] == _session_cache_payload("singleflight")
    assert results[1] == _session_cache_payload("singleflight")
    assert calls == 1


def test_session_list_cache_follower_wait_stage_when_rebuild_inflight():
    routes._session_list_cache_clear()

    started = threading.Event()
    release = threading.Event()

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    def builder():
        started.set()
        release.wait()
        return _session_cache_payload("wait")

    follower_diag = _StageRecorder()
    owner_diag = _StageRecorder()

    def owner():
        routes._get_cached_session_list_payload(
            key=key,
            builder=builder,
            diag=owner_diag,
        )

    def follower():
        routes._get_cached_session_list_payload(
            key=key,
            builder=builder,
            diag=follower_diag,
        )

    owner_thread = threading.Thread(target=owner)
    follower_thread = threading.Thread(target=follower)
    owner_thread.start()
    assert started.wait(1.0)
    follower_thread.start()
    release.set()
    owner_thread.join(2)
    follower_thread.join(2)

    assert "session_list_cache_wait" in follower_diag.stages
    assert "session_list_cache_hit" in owner_diag.stages or "session_list_cache_stored" in owner_diag.stages


def test_session_list_cache_invalidated_on_session_list_publish():
    routes._session_list_cache_clear()

    key_a = routes._session_list_cache_key(
        active_profile="profile-a",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    key_b = routes._session_list_cache_key(
        active_profile="profile-b",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    key_a_all = routes._session_list_cache_key(
        active_profile="profile-a",
        all_profiles=True,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    routes._session_list_cache_set(key_a, _session_cache_payload("a"))
    routes._session_list_cache_set(key_b, _session_cache_payload("b"))
    routes._session_list_cache_set(key_a_all, _session_cache_payload("a_all", all_profiles=True))

    session_events.publish_session_list_changed("session_pin", profile="profile-a")

    assert routes._session_list_cache_get(key_a)[0] is None
    assert routes._session_list_cache_get(key_a_all)[0] is None
    assert routes._session_list_cache_get(key_b)[0] is not None


def test_session_list_cache_rebuild_retries_after_invalidation():
    routes._session_list_cache_clear()

    key = routes._session_list_cache_key(
        active_profile="profile-a",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    calls = []

    def builder():
        calls.append("build")
        if len(calls) == 1:
            routes._session_list_cache_clear("profile-a")
            return _session_cache_payload("stale")
        return _session_cache_payload("fresh")

    payload = routes._get_cached_session_list_payload(key=key, builder=builder)

    assert payload == _session_cache_payload("fresh")
    assert calls == ["build", "build"]


def test_session_list_payload_to_response_overlays_live_stream_runtime(monkeypatch):
    payload = {
        "sessions": [
            {
                "session_id": "streaming-session",
                "title": "Live title",
                "active_stream_id": None,
                "is_streaming": False,
                "has_pending_user_message": False,
            },
            {
                "session_id": "stale-session",
                "title": "Old title",
                "active_stream_id": "stale-stream",
                "is_streaming": True,
                "has_pending_user_message": True,
            },
        ],
        "cli_count": 0,
        "all_profiles": False,
        "active_profile": "default",
        "other_profile_count": 0,
    }

    monkeypatch.setattr(routes, "_active_stream_ids", lambda: {"live-stream"})
    with routes.LOCK:
        original = dict(routes.SESSIONS)
        routes.SESSIONS.clear()
        routes.SESSIONS["streaming-session"] = SimpleNamespace(
            active_stream_id="live-stream",
            pending_user_message="queued prompt",
        )
        routes.SESSIONS["stale-session"] = SimpleNamespace(
            active_stream_id=None,
            pending_user_message=None,
        )
    try:
        response = routes._session_list_payload_to_response(payload)
    finally:
        with routes.LOCK:
            routes.SESSIONS.clear()
            routes.SESSIONS.update(original)

    by_id = {row["session_id"]: row for row in response["sessions"]}
    assert by_id["streaming-session"]["active_stream_id"] == "live-stream"
    assert by_id["streaming-session"]["is_streaming"] is True
    assert by_id["streaming-session"]["has_pending_user_message"] is True
    assert by_id["stale-session"]["active_stream_id"] is None
    assert by_id["stale-session"]["is_streaming"] is False
    assert by_id["stale-session"]["has_pending_user_message"] is False
