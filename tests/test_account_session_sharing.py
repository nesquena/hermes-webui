from types import SimpleNamespace
from urllib.parse import urlparse

import api.auth as auth
import api.config as config
import api.models as models
import api.routes as routes
from api.models import Session, get_session


def _row(session_id, *, profile="default", owner=None, shared=None, title=None):
    return {
        "session_id": session_id,
        "title": title or session_id,
        "workspace": "/tmp",
        "model": "test-model",
        "message_count": 1,
        "created_at": 10,
        "updated_at": 10,
        "last_message_at": 10,
        "profile": profile,
        "owner_account": owner,
        "shared_with_accounts": list(shared or []),
    }


def _capture_json(monkeypatch):
    def fake_j(_handler, payload, status=200, **_kwargs):
        return {"status": status, "payload": payload}

    def fake_bad(_handler, message, status=400, **_kwargs):
        return {"status": status, "payload": {"error": message}}

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "bad", fake_bad)


def test_session_model_roundtrips_account_owner_and_shared_accounts(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    models.SESSIONS.clear()

    session = Session(
        session_id="shared-session",
        title="Shared",
        messages=[{"role": "user", "content": "hello"}],
        profile="default",
        owner_account="main",
        shared_with_accounts=["sub", "sub", "  ", "guest"],
    )
    session.save()

    loaded = get_session("shared-session")
    assert loaded.owner_account == "main"
    assert loaded.shared_with_accounts == ["sub", "guest"]
    compact = loaded.compact()
    assert compact["owner_account"] == "main"
    assert compact["shared_with_accounts"] == ["sub", "guest"]

    models.SESSIONS.clear()


def test_auth_session_binds_verified_account(monkeypatch):
    monkeypatch.setenv(
        "HERMES_WEBUI_ACCOUNTS",
        '{"main":{"password":"main-pw"},"sub":{"password":"sub-pw"}}',
    )
    auth._sessions.clear()
    monkeypatch.setattr(auth, "_save_sessions", lambda _sessions: None)

    assert auth.accounts_configured() is True
    assert auth.verify_account_credentials("sub", "sub-pw") == "sub"
    assert auth.verify_account_credentials("sub", "main-pw") is None

    cookie = auth.create_session(account="sub")
    assert auth.session_account(cookie) == "sub"

    auth._sessions.clear()


def test_account_scoped_sidebar_shows_shared_cross_profile_sessions(monkeypatch):
    rows = [
        _row("main-private", profile="default", owner="main"),
        _row("sub-owned", profile="sub", owner="sub"),
        _row("shared-default", profile="default", owner="main", shared=["sub"]),
        _row("other-owned", profile="other", owner="other"),
    ]
    monkeypatch.setattr(routes, "all_sessions", lambda **_kwargs: list(rows))
    monkeypatch.setattr(routes, "_current_webui_account", lambda _handler=None: "sub", raising=False)
    monkeypatch.setattr(routes, "_account_session_sharing_enabled", lambda: True, raising=False)

    payload = routes._build_session_list_cache_payload(
        active_profile="sub",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        visible_only=True,
    )

    assert [row["session_id"] for row in payload["sessions"]] == ["sub-owned", "shared-default"]
    assert payload["other_profile_count"] == 0


def test_main_account_sidebar_sees_all_account_managed_sessions(monkeypatch):
    rows = [
        _row("main-private", profile="default", owner="main"),
        _row("sub-owned", profile="sub", owner="sub"),
        _row("shared-default", profile="default", owner="main", shared=["sub"]),
        _row("other-owned", profile="other", owner="other"),
    ]
    monkeypatch.setattr(routes, "all_sessions", lambda **_kwargs: list(rows))
    monkeypatch.setattr(routes, "_current_webui_account", lambda _handler=None: "main", raising=False)
    monkeypatch.setattr(routes, "_account_session_sharing_enabled", lambda: True, raising=False)

    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        visible_only=True,
    )

    assert [row["session_id"] for row in payload["sessions"]] == [
        "main-private",
        "sub-owned",
        "shared-default",
        "other-owned",
    ]
    assert payload["other_profile_count"] == 0


def test_account_sharing_does_not_treat_missing_auth_cookie_as_main(monkeypatch):
    session = Session(
        session_id="private-session",
        title="Private",
        messages=[{"role": "user", "content": "hello"}],
        owner_account="main",
        shared_with_accounts=[],
    )
    monkeypatch.setattr(routes, "_account_session_sharing_enabled", lambda: True, raising=False)
    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: True)

    assert routes._current_webui_account(SimpleNamespace(headers={})) is None
    assert routes._session_account_visible(session, handler=SimpleNamespace(headers={})) is False


def test_shared_account_can_open_cross_profile_session_detail(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    models.SESSIONS.clear()
    _capture_json(monkeypatch)
    monkeypatch.setattr(routes, "_current_webui_account", lambda _handler=None: "sub", raising=False)
    monkeypatch.setattr(routes, "_account_session_sharing_enabled", lambda: True, raising=False)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "sub")
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda _session: False)

    Session(
        session_id="shared-session",
        title="Shared",
        messages=[{"role": "user", "content": "hello"}],
        profile="default",
        owner_account="main",
        shared_with_accounts=["sub"],
    ).save()

    response = routes.handle_get(
        SimpleNamespace(headers={}, path="/api/session", client_address=("127.0.0.1", 1)),
        urlparse("/api/session?session_id=shared-session&messages=0&resolve_model=0"),
    )

    assert response["status"] == 200
    assert response["payload"]["session"]["session_id"] == "shared-session"
    assert response["payload"]["session"]["profile"] == "default"

    models.SESSIONS.clear()


def test_shared_account_can_continue_using_original_session_profile(monkeypatch):
    _capture_json(monkeypatch)
    session = Session(
        session_id="shared-session",
        title="Shared",
        messages=[{"role": "user", "content": "hello"}],
        workspace="/tmp",
        model="test-model",
        profile="default",
        owner_account="main",
        shared_with_accounts=["sub"],
    )
    captured = {}

    monkeypatch.setattr(routes, "_current_webui_account", lambda _handler=None: "sub", raising=False)
    monkeypatch.setattr(routes, "_account_session_sharing_enabled", lambda: True, raising=False)
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda _sid: session)
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _workspace: _s.workspace)
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _provider: (None, "test-model"))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda requested_model, requested_provider, **_kwargs: (requested_model, requested_provider, requested_model),
    )

    def fake_start_run(s, **kwargs):
        captured["session_profile"] = s.profile
        captured["message"] = kwargs["msg"]
        return {"_status": 200, "stream_id": "stream-1", "session_id": s.session_id}

    monkeypatch.setattr(routes, "_start_run", fake_start_run)

    response = routes._handle_chat_start(
        SimpleNamespace(headers={}, client_address=("127.0.0.1", 1)),
        {"session_id": "shared-session", "message": "continue", "profile": "sub"},
    )

    assert response["status"] == 200
    assert response["payload"]["session_id"] == "shared-session"
    assert captured == {"session_profile": "default", "message": "continue"}


def test_unshared_account_cannot_continue_cross_profile_session(monkeypatch):
    _capture_json(monkeypatch)
    session = Session(
        session_id="private-session",
        title="Private",
        messages=[{"role": "user", "content": "hello"}],
        workspace="/tmp",
        model="test-model",
        profile="default",
        owner_account="main",
        shared_with_accounts=[],
    )
    monkeypatch.setattr(routes, "_current_webui_account", lambda _handler=None: "sub", raising=False)
    monkeypatch.setattr(routes, "_account_session_sharing_enabled", lambda: True, raising=False)
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda _sid: session)

    response = routes._handle_chat_start(
        SimpleNamespace(headers={}, client_address=("127.0.0.1", 1)),
        {"session_id": "private-session", "message": "continue", "profile": "sub"},
    )

    assert response["status"] == 404
    assert response["payload"]["error"] == "Session not found"


def test_main_account_can_share_and_unshare_session(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    models.SESSIONS.clear()
    _capture_json(monkeypatch)
    monkeypatch.setattr(routes, "_current_webui_account", lambda _handler=None: "main", raising=False)
    monkeypatch.setattr(routes, "_account_session_sharing_enabled", lambda: True, raising=False)

    Session(
        session_id="share-me",
        title="Share me",
        messages=[{"role": "user", "content": "hello"}],
        owner_account="main",
        shared_with_accounts=[],
    ).save()

    handler = SimpleNamespace(headers={}, client_address=("127.0.0.1", 1))
    shared = routes._handle_session_share(handler, {"session_id": "share-me", "account": "sub"})
    assert shared["status"] == 200
    assert shared["payload"]["session"]["shared_with_accounts"] == ["sub"]
    assert get_session("share-me").shared_with_accounts == ["sub"]

    unshared = routes._handle_session_share(
        handler,
        {"session_id": "share-me", "account": "sub", "shared": False},
    )
    assert unshared["status"] == 200
    assert unshared["payload"]["session"]["shared_with_accounts"] == []
    assert get_session("share-me").shared_with_accounts == []

    models.SESSIONS.clear()


def test_non_main_account_cannot_share_session(monkeypatch):
    _capture_json(monkeypatch)
    monkeypatch.setattr(routes, "_current_webui_account", lambda _handler=None: "sub", raising=False)
    monkeypatch.setattr(routes, "_account_session_sharing_enabled", lambda: True, raising=False)

    response = routes._handle_session_share(
        SimpleNamespace(headers={}, client_address=("127.0.0.1", 1)),
        {"session_id": "share-me", "account": "guest"},
    )

    assert response["status"] == 403
    assert response["payload"]["error"] == "Only main can share sessions"
