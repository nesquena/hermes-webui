from pathlib import Path


ROUTES = Path("api/routes.py").read_text(encoding="utf-8")
SESSION_EVENTS = Path("api/session_events.py").read_text(encoding="utf-8")
PROFILES = Path("api/profiles.py").read_text(encoding="utf-8")


def test_session_events_endpoint_and_bus_are_defined():
    assert "_SESSION_EVENTS_SUBSCRIBERS" in SESSION_EVENTS
    assert "def publish_session_list_changed" in SESSION_EVENTS
    assert "def _handle_session_events_stream" in ROUTES
    assert "parsed.path == '/api/sessions/events'" in ROUTES
    assert "Content-Type', 'text/event-stream; charset=utf-8'" in ROUTES


def test_session_events_publish_for_minimal_sidebar_mutations():
    for reason in (
        "session_new",
        "session_delete",
        "session_duplicate",
        "session_import",
        "session_import_cli",
        "session_archive",
        "session_move",
        "session_pin",
        "session_rename",
        "session_title_regenerate",
        "session_branch",
    ):
        if reason == "session_import_cli":
            assert f'publish_session_list_changed(\n        "{reason}",' in ROUTES, reason
        elif reason == "session_import":
            assert f'publish_session_list_changed("{reason}")' in ROUTES, reason
        else:
            assert f'publish_session_list_changed("{reason}",' in ROUTES, reason

    assert 'if worktree_info:\n            publish_session_list_changed("session_new", profile=getattr(s, "profile", None))' in ROUTES
    assert "was_hidden_empty_session = _is_hidden_empty_session(s)" in ROUTES
    assert 'if was_hidden_empty_session:\n        publish_session_list_changed("session_new", profile=getattr(s, "profile", None))' in ROUTES
    assert 'publish_session_list_changed("session_duplicate", profile=getattr(copied_session, "profile", None))' in ROUTES
    assert 'publish_session_list_changed("session_rename", profile=getattr(s, "profile", None))' in ROUTES
    assert 'publish_session_list_changed("session_title_regenerate", profile=getattr(s, "profile", None))' in ROUTES
    assert 'publish_session_list_changed("session_delete", profile=getattr(s, "profile", None))' in ROUTES
    assert 'publish_session_list_changed("session_branch", profile=getattr(branch, "profile", None))' in ROUTES
    assert 'publish_session_list_changed("session_pin", profile=getattr(s, "profile", None))' in ROUTES
    assert 'publish_session_list_changed("session_archive", profile=getattr(s, "profile", None))' in ROUTES
    assert 'publish_session_list_changed("session_move", profile=getattr(s, "profile", None))' in ROUTES
    assert 'profile=getattr(s, "profile", None)' in ROUTES
    assert 'publish_session_list_changed("chat_start")' not in ROUTES
    assert 'publish_session_list_changed("cron_complete",' in ROUTES
    assert 'publish_session_list_changed("cron_complete",' in PROFILES


def test_session_event_queue_is_bounded_and_latest_wins():
    from api import session_events

    q = session_events.subscribe_session_events()
    try:
        session_events.publish_session_list_changed("first")
        session_events.publish_session_list_changed("second")
        payload = q.get_nowait()
        assert payload["type"] == "sessions_changed"
        assert payload["reason"] == "second"
        assert q.empty()
    finally:
        session_events.unsubscribe_session_events(q)


def test_session_events_payload_tracks_profile_when_available():
    from api import session_events

    q = session_events.subscribe_session_events()
    try:
        session_events.publish_session_list_changed("no_profile")
        payload = q.get_nowait()
        assert payload["type"] == "sessions_changed"
        assert payload["reason"] == "no_profile"
        assert "profile" not in payload

        session_events.publish_session_list_changed("with_profile", profile="profile-b")
        payload = q.get_nowait()
        assert payload["type"] == "sessions_changed"
        assert payload["reason"] == "with_profile"
        assert payload["profile"] == "profile-b"
    finally:
        session_events.unsubscribe_session_events(q)
