from urllib.parse import urlparse


def test_webui_session_metadata_load_skips_cli_metadata_scan(monkeypatch):
    """Opening a normal WebUI session should not scan imported CLI sessions."""
    import api.routes as routes
    from api.models import Session

    session = Session(
        session_id="webui_normal",
        title="Normal WebUI chat",
        messages=[{"role": "user", "content": "hello"}],
    )

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: session)
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda _session: None)
    monkeypatch.setattr(routes, "redact_session_data", lambda payload: payload)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload)
    monkeypatch.setattr(
        routes,
        "_lookup_cli_session_metadata",
        lambda _sid: (_ for _ in ()).throw(AssertionError("normal WebUI loads should not scan CLI sessions")),
    )

    response = routes.handle_get(
        object(),
        urlparse("/api/session?session_id=webui_normal&messages=0&resolve_model=0"),
    )

    assert response["session"]["session_id"] == "webui_normal"
    assert response["session"]["messages"] == []


def test_read_only_session_metadata_load_preserves_cli_metadata_lookup(monkeypatch):
    """Read-only imported sidecars still need CLI metadata for source identity."""
    import api.routes as routes
    from api.models import Session

    session = Session(
        session_id="readonly_sidecar",
        title="Imported chat",
        messages=[{"role": "user", "content": "hello"}],
        read_only=True,
    )
    looked_up = []

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: session)
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda _session: None)
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda _sid: [])
    monkeypatch.setattr(routes, "redact_session_data", lambda payload: payload)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload)

    def fake_lookup(sid):
        looked_up.append(sid)
        return {
            "session_id": sid,
            "read_only": True,
            "source_label": "External Agent",
        }

    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", fake_lookup)

    response = routes.handle_get(
        object(),
        urlparse("/api/session?session_id=readonly_sidecar&messages=0&resolve_model=0"),
    )

    assert looked_up == ["readonly_sidecar"]
    assert response["session"]["read_only"] is True


def test_messaging_session_metadata_load_preserves_cli_metadata_lookup(monkeypatch):
    """Messaging/imported sidecars still need CLI metadata for source identity."""
    import api.routes as routes
    from api.models import Session

    session = Session(
        session_id="messaging_sidecar",
        title="Telegram chat",
        messages=[{"role": "user", "content": "hello"}],
        session_source="messaging",
        raw_source="telegram",
    )
    looked_up = []

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: session)
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda _session: None)
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda _sid: [])
    monkeypatch.setattr(routes, "redact_session_data", lambda payload: payload)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload)

    def fake_lookup(sid):
        looked_up.append(sid)
        return {
            "session_id": sid,
            "session_source": "messaging",
            "raw_source": "telegram",
            "source_label": "Telegram",
        }

    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", fake_lookup)

    response = routes.handle_get(
        object(),
        urlparse("/api/session?session_id=messaging_sidecar&messages=0&resolve_model=0"),
    )

    assert looked_up == ["messaging_sidecar"]
    assert response["session"]["source_label"] == "Telegram"
