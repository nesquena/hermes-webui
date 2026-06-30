"""Focused regression coverage for true Web Push closed-app delivery (#3196)."""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SW_JS = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
STREAMING_SRC = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
ROUTE_APPROVALS_SRC = (ROOT / "api" / "route_approvals.py").read_text(encoding="utf-8")
CLARIFY_SRC = (ROOT / "api" / "clarify.py").read_text(encoding="utf-8")
REQUIREMENTS = (ROOT / "requirements.txt").read_text(encoding="utf-8")


class _JSONHandler:
    def __init__(self, body: dict | None = None, *, headers: dict | None = None):
        raw = json.dumps(body or {}).encode("utf-8")
        self.headers = {"Content-Length": str(len(raw))}
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = []
        self.close_connection = False

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass


def _payload(handler: _JSONHandler) -> dict:
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def _subscription(endpoint: str) -> dict:
    return {
        "endpoint": endpoint,
        "keys": {
            "p256dh": "p256dh-key",
            "auth": "auth-key",
        },
    }


def test_subscription_store_round_trip_and_stale_prune(monkeypatch, tmp_path):
    import api.config as config
    import api.web_push as web_push

    store_path = tmp_path / "webui_push_subscriptions.json"
    monkeypatch.setattr(web_push, "_subscription_store_path", lambda: store_path)
    monkeypatch.setattr(config, "web_push_configured", lambda: True)
    monkeypatch.setattr(config, "web_push_private_key", lambda: "private-key")
    monkeypatch.setattr(config, "web_push_subject", lambda: "mailto:test@example.com")

    class _WebPushException(Exception):
        pass

    calls = []

    def _fake_webpush(*, subscription_info, data, vapid_private_key, vapid_claims, timeout):
        calls.append(
            {
                "endpoint": subscription_info["endpoint"],
                "data": json.loads(data),
                "vapid_private_key": vapid_private_key,
                "vapid_claims": dict(vapid_claims),
                "timeout": timeout,
            }
        )
        if subscription_info["endpoint"].endswith("/dead"):
            exc = _WebPushException("gone")
            exc.response = SimpleNamespace(status_code=410)
            raise exc

    monkeypatch.setattr(web_push, "_get_pywebpush_impl", lambda: (_fake_webpush, _WebPushException))

    web_push.upsert_subscription(_subscription("https://push.example/live"), owner_key="owner-a")
    web_push.upsert_subscription(_subscription("https://push.example/dead"), owner_key="owner-a")
    web_push.upsert_subscription(_subscription("https://push.example/other"), owner_key="owner-b")

    sent = web_push.send_web_push(
        web_push._notification_payload("Response complete", "Task finished", session_id="session-123"),
        owner_key="owner-a",
    )

    assert sent == 1
    assert [call["endpoint"] for call in calls] == [
        "https://push.example/live",
        "https://push.example/dead",
    ]
    assert calls[0]["data"]["options"]["data"]["url"] == "session/session-123"
    assert calls[0]["vapid_private_key"] == "private-key"
    assert calls[0]["vapid_claims"]["sub"] == "mailto:test@example.com"
    assert calls[0]["timeout"] == web_push._WEB_PUSH_TIMEOUT_SECONDS
    assert [sub["endpoint"] for sub in web_push.list_subscriptions(owner_key="owner-a")] == ["https://push.example/live"]
    assert [sub["endpoint"] for sub in web_push.list_subscriptions(owner_key="owner-b")] == ["https://push.example/other"]


def test_push_routes_support_status_subscribe_and_delete(monkeypatch, tmp_path):
    import api.config as config
    import api.routes as routes
    import api.web_push as web_push

    store_path = tmp_path / "webui_push_subscriptions.json"
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "_request_csrf_token", lambda handler: "csrf-live")
    monkeypatch.setattr(web_push, "_subscription_store_path", lambda: store_path)
    monkeypatch.setattr(web_push, "web_push_status", lambda: {
        "enabled": True,
        "configured": True,
        "dependency_available": True,
    })
    monkeypatch.setattr(config, "web_push_public_key", lambda: "PUBLIC-KEY")

    status_handler = _JSONHandler()
    assert routes.handle_get(status_handler, SimpleNamespace(path="/api/push/status", query="")) is not False
    assert status_handler.status == 200
    assert _payload(status_handler) == {
        "enabled": True,
        "configured": True,
        "dependency_available": True,
        "csrf_token": "csrf-live",
    }

    key_handler = _JSONHandler()
    assert routes.handle_get(key_handler, SimpleNamespace(path="/api/push/vapid-public-key", query="")) is not False
    assert key_handler.status == 200
    assert _payload(key_handler) == {"public_key": "PUBLIC-KEY"}

    subscribe_handler = _JSONHandler({"subscription": _subscription("https://push.example/browser")})
    assert routes.handle_post(subscribe_handler, SimpleNamespace(path="/api/push/subscribe")) is not False
    assert subscribe_handler.status == 200
    assert _payload(subscribe_handler)["subscription"]["endpoint"] == "https://push.example/browser"
    set_cookie = dict(subscribe_handler.response_headers).get("Set-Cookie")
    assert set_cookie and "hermes_push_owner=" in set_cookie
    assert f"Max-Age={web_push._PUSH_OWNER_COOKIE_MAX_AGE_SECONDS}" in set_cookie
    cookie_header = set_cookie.split(";", 1)[0]
    assert [sub["endpoint"] for sub in web_push.list_subscriptions()] == ["https://push.example/browser"]
    assert _payload(subscribe_handler)["subscription"]["owner"]

    delete_handler = _JSONHandler(
        {"endpoint": "https://push.example/browser"},
        headers={"Cookie": cookie_header},
    )
    assert routes.handle_delete(delete_handler, SimpleNamespace(path="/api/push/subscribe")) is not False
    assert delete_handler.status == 200
    assert _payload(delete_handler) == {"ok": True, "removed": True}
    assert web_push.list_subscriptions() == []


def test_push_routes_fail_closed_when_server_not_ready(monkeypatch):
    import api.routes as routes
    import api.web_push as web_push

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(web_push, "web_push_status", lambda: {
        "enabled": False,
        "configured": False,
        "dependency_available": False,
    })

    key_handler = _JSONHandler()
    assert routes.handle_get(key_handler, SimpleNamespace(path="/api/push/vapid-public-key", query="")) is not False
    assert key_handler.status == 404
    assert _payload(key_handler)["error"] == "Web Push is not configured"

    subscribe_handler = _JSONHandler({"subscription": _subscription("https://push.example/browser")})
    assert routes.handle_post(subscribe_handler, SimpleNamespace(path="/api/push/subscribe")) is not False
    assert subscribe_handler.status == 409
    assert _payload(subscribe_handler)["error"] == "Web Push is not configured"


def test_push_routes_reject_unsafe_subscription_endpoints(monkeypatch, tmp_path):
    import api.routes as routes
    import api.web_push as web_push

    store_path = tmp_path / "webui_push_subscriptions.json"
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(web_push, "_subscription_store_path", lambda: store_path)
    monkeypatch.setattr(web_push, "web_push_status", lambda: {
        "enabled": True,
        "configured": True,
        "dependency_available": True,
    })

    insecure_handler = _JSONHandler({"subscription": _subscription("http://push.example/browser")})
    assert routes.handle_post(insecure_handler, SimpleNamespace(path="/api/push/subscribe")) is not False
    assert insecure_handler.status == 400
    assert _payload(insecure_handler)["error"] == "subscription endpoint must use https"

    local_handler = _JSONHandler({"subscription": _subscription("https://127.0.0.1/browser")})
    assert routes.handle_post(local_handler, SimpleNamespace(path="/api/push/subscribe")) is not False
    assert local_handler.status == 400
    assert _payload(local_handler)["error"] == "subscription endpoint resolved to a private IP: 127.0.0.1"
    assert web_push.list_subscriptions() == []


def test_push_unsubscribe_requires_owner_cookie(monkeypatch, tmp_path):
    import api.routes as routes
    import api.web_push as web_push

    store_path = tmp_path / "webui_push_subscriptions.json"
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(web_push, "_subscription_store_path", lambda: store_path)

    web_push.upsert_subscription(_subscription("https://push.example/browser"), owner_key="owner-a")

    delete_handler = _JSONHandler({"endpoint": "https://push.example/browser"})
    assert routes.handle_delete(delete_handler, SimpleNamespace(path="/api/push/subscribe")) is not False
    assert delete_handler.status == 400
    assert _payload(delete_handler)["error"] == "Web Push owner is required"
    assert [sub["endpoint"] for sub in web_push.list_subscriptions(owner_key="owner-a")] == [
        "https://push.example/browser"
    ]


def test_push_status_reports_unavailable_when_pywebpush_missing(monkeypatch):
    import api.config as config
    import api.routes as routes
    import api.web_push as web_push

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "_request_csrf_token", lambda handler: "csrf-live")
    monkeypatch.setattr(config, "web_push_configured", lambda: True)
    monkeypatch.setattr(web_push, "_get_pywebpush_impl", lambda: (None, None))

    status_handler = _JSONHandler()
    assert routes.handle_get(status_handler, SimpleNamespace(path="/api/push/status", query="")) is not False
    assert status_handler.status == 200
    assert _payload(status_handler) == {
        "enabled": False,
        "configured": True,
        "dependency_available": False,
        "csrf_token": "csrf-live",
    }

    subscribe_handler = _JSONHandler({"subscription": _subscription("https://push.example/browser")})
    assert routes.handle_post(subscribe_handler, SimpleNamespace(path="/api/push/subscribe")) is not False
    assert subscribe_handler.status == 409
    assert _payload(subscribe_handler)["error"] == "Web Push is not configured"

    sent = web_push.send_web_push(
        web_push._notification_payload("Response complete", "Task finished", session_id="session-123"),
        owner_key="owner-a",
    )
    assert sent == 0


def test_subscription_store_save_uses_atomic_replace(monkeypatch, tmp_path):
    import api.web_push as web_push

    store_path = tmp_path / "webui_push_subscriptions.json"
    replace_calls = []
    mkstemp_calls = []
    real_mkstemp = web_push.tempfile.mkstemp
    real_replace = web_push.os.replace

    monkeypatch.setattr(web_push, "_subscription_store_path", lambda: store_path)

    def _mkstemp(*args, **kwargs):
        mkstemp_calls.append({
            "dir": kwargs.get("dir"),
            "suffix": kwargs.get("suffix"),
        })
        return real_mkstemp(*args, **kwargs)

    def _replace(src, dst):
        replace_calls.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(web_push.tempfile, "mkstemp", _mkstemp)
    monkeypatch.setattr(web_push.os, "replace", _replace)

    web_push._save_store({
        "subscriptions": [
            {
                **_subscription("https://push.example/live"),
                "owner": "owner-a",
            }
        ]
    })

    assert mkstemp_calls == [{"dir": store_path.parent, "suffix": ".web_push.tmp"}]
    assert replace_calls and replace_calls[0][1] == store_path
    assert replace_calls[0][0] != store_path


def test_subscription_store_mutations_hold_lock_across_read_modify_write(monkeypatch, tmp_path):
    import api.web_push as web_push

    store_path = tmp_path / "webui_push_subscriptions.json"
    entered_save = threading.Event()
    release_save = threading.Event()
    removal_done = threading.Event()
    real_save_store = web_push._save_store

    monkeypatch.setattr(web_push, "_subscription_store_path", lambda: store_path)

    def _slow_save(store: dict) -> None:
        entered_save.set()
        release_save.wait(timeout=5)
        real_save_store(store)

    web_push._save_store({
        "subscriptions": [
            {
                **_subscription("https://push.example/dead"),
                "owner": "owner-a",
            }
        ]
    })
    monkeypatch.setattr(web_push, "_save_store", _slow_save)

    upsert_thread = threading.Thread(
        target=lambda: web_push.upsert_subscription(_subscription("https://push.example/live"), owner_key="owner-a")
    )
    remove_thread = threading.Thread(
        target=lambda: (web_push.remove_subscription("https://push.example/dead", owner_key="owner-a"), removal_done.set())
    )

    upsert_thread.start()
    assert entered_save.wait(timeout=1), "upsert_subscription never reached the locked save path"
    remove_thread.start()
    assert not removal_done.wait(timeout=0.2), "remove_subscription should block behind the store lock"
    release_save.set()
    upsert_thread.join(timeout=5)
    remove_thread.join(timeout=5)

    assert sorted(sub["endpoint"] for sub in web_push.list_subscriptions(owner_key="owner-a")) == ["https://push.example/live"]


def test_send_web_push_skips_other_browser_owners(monkeypatch, tmp_path):
    import api.config as config
    import api.web_push as web_push

    store_path = tmp_path / "webui_push_subscriptions.json"
    monkeypatch.setattr(web_push, "_subscription_store_path", lambda: store_path)
    monkeypatch.setattr(config, "web_push_configured", lambda: True)
    monkeypatch.setattr(config, "web_push_private_key", lambda: "private-key")
    monkeypatch.setattr(config, "web_push_subject", lambda: "mailto:test@example.com")

    seen = []

    def _fake_webpush(*, subscription_info, data, vapid_private_key, vapid_claims, timeout):
        seen.append(subscription_info["endpoint"])

    monkeypatch.setattr(web_push, "_get_pywebpush_impl", lambda: (_fake_webpush, RuntimeError))

    web_push.upsert_subscription(_subscription("https://push.example/a"), owner_key="owner-a")
    web_push.upsert_subscription(_subscription("https://push.example/b"), owner_key="owner-b")

    sent = web_push.send_web_push(
        web_push._notification_payload("Response complete", "Task finished", session_id="session-123"),
        owner_key="owner-a",
    )

    assert sent == 1
    assert seen == ["https://push.example/a"]


def test_send_web_push_prunes_invalid_stored_subscriptions(monkeypatch, tmp_path):
    import api.config as config
    import api.web_push as web_push

    store_path = tmp_path / "webui_push_subscriptions.json"
    monkeypatch.setattr(web_push, "_subscription_store_path", lambda: store_path)
    monkeypatch.setattr(config, "web_push_configured", lambda: True)
    monkeypatch.setattr(config, "web_push_private_key", lambda: "private-key")
    monkeypatch.setattr(config, "web_push_subject", lambda: "mailto:test@example.com")

    seen = []

    def _fake_webpush(*, subscription_info, data, vapid_private_key, vapid_claims, timeout):
        seen.append(subscription_info["endpoint"])

    monkeypatch.setattr(web_push, "_get_pywebpush_impl", lambda: (_fake_webpush, RuntimeError))

    web_push._save_store({
        "subscriptions": [
            {
                **_subscription("https://127.0.0.1/browser"),
                "owner": "owner-a",
            },
            {
                **_subscription("https://push.example/live"),
                "owner": "owner-a",
            },
        ]
    })

    sent = web_push.send_web_push(
        web_push._notification_payload("Response complete", "Task finished", session_id="session-123"),
        owner_key="owner-a",
    )

    assert sent == 1
    assert seen == ["https://push.example/live"]
    assert [sub["endpoint"] for sub in web_push.list_subscriptions(owner_key="owner-a")] == [
        "https://push.example/live"
    ]


def test_bg_task_complete_producer_fans_out_web_push(monkeypatch):
    import api.background_process as background_process
    import api.web_push as web_push

    seen = []
    monkeypatch.setattr(
        background_process,
        "_emit_to_session_streams",
        lambda session_id, event, data: 1,
    )
    monkeypatch.setattr(
        web_push,
        "notify_bg_task_complete",
        lambda session_id, payload: seen.append((session_id, dict(payload))) or 1,
    )

    emitted = background_process._emit_bg_task_complete_events_now(
        "session-123",
        {"message": "Task finished", "title": "Background task complete"},
    )

    assert emitted == 2
    assert seen == [("session-123", {"message": "Task finished", "title": "Background task complete"})]


def test_response_complete_bridge_calls_web_push(monkeypatch):
    import api.streaming as streaming
    import api.web_push as web_push

    seen = []
    monkeypatch.setattr(
        web_push,
        "notify_response_complete",
        lambda session_id, answer: seen.append((session_id, answer)) or 1,
    )

    streaming._notify_response_complete_web_push("session-123", "Final answer")

    assert seen == [("session-123", "Final answer")]


def test_notify_response_complete_scopes_delivery_to_session_owner(monkeypatch):
    import api.web_push as web_push

    seen = []
    monkeypatch.setattr(
        web_push,
        "_session_push_owner",
        lambda session_id: "owner-a" if session_id == "session-123" else None,
    )
    monkeypatch.setattr(
        web_push,
        "send_web_push",
        lambda payload, *, owner_key: seen.append((owner_key, payload["title"])) or 1,
    )

    assert web_push.notify_response_complete("session-123", "Final answer") == 1
    assert web_push.notify_response_complete("session-456", "Other answer") == 1
    assert seen == [
        ("owner-a", "Response complete"),
        (None, "Response complete"),
    ]


def test_approval_and_clarify_submit_pending_fan_out(monkeypatch):
    import api.clarify as clarify
    import api.route_approvals as route_approvals
    import api.web_push as web_push

    seen = []
    monkeypatch.setattr(route_approvals, "publish_session_list_changed", lambda *_: None)
    monkeypatch.setattr(clarify, "publish_session_list_changed", lambda *_: None)
    monkeypatch.setattr(
        web_push,
        "notify_approval_required",
        lambda session_id, approval: seen.append(("approval", session_id, approval["description"])) or 1,
    )
    monkeypatch.setattr(
        web_push,
        "notify_clarify_required",
        lambda session_id, clarify_data: seen.append(("clarify", session_id, clarify_data["question"])) or 1,
    )

    approval_sid = "push-approval"
    with route_approvals._lock:
        route_approvals._pending.pop(approval_sid, None)
    route_approvals.submit_pending(
        approval_sid,
        {
            "command": "dangerous command",
            "pattern_key": "dangerous command",
            "pattern_keys": ["dangerous command"],
            "description": "Tool approval needed",
        },
    )
    with route_approvals._lock:
        route_approvals._pending.pop(approval_sid, None)

    clarify_sid = "push-clarify"
    clarify.submit_pending(
        clarify_sid,
        {
            "question": "Need more detail?",
            "choices_offered": ["yes", "no"],
        },
    )
    clarify.clear_pending(clarify_sid)

    assert seen == [
        ("approval", "push-approval", "Tool approval needed"),
        ("clarify", "push-clarify", "Need more detail?"),
    ]


def test_chat_start_stamps_session_push_owner_from_cookie(monkeypatch):
    import api.routes as routes

    session = SimpleNamespace(
        session_id="session-123",
        profile="default",
        push_owner=None,
        messages=[],
        context_messages=[],
        pending_user_message=None,
        workspace="D:/Repos",
        model="test-model",
        model_provider=None,
    )
    seen = []

    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda session_id: session)
    monkeypatch.setattr(routes, "_profiles_match", lambda left, right: left == right)
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda s, workspace: workspace or s.workspace)
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda s, requested_provider: (None, None))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda model, requested_provider, **kwargs: (model, requested_provider, False),
    )
    monkeypatch.setattr(
        routes,
        "_start_run",
        lambda s, **kwargs: seen.append(getattr(s, "push_owner", None)) or {
            "stream_id": "stream-123",
            "session_id": s.session_id,
            "_status": 200,
        },
    )

    handler = _JSONHandler(
        {
            "session_id": "session-123",
            "message": "hello",
            "workspace": "D:/Repos",
            "model": "test-model",
            "profile": "default",
        },
        headers={"Cookie": "hermes_push_owner=owner-cookie"},
    )

    assert routes._handle_chat_start(handler, json.loads(handler.rfile.getvalue())) is None
    assert handler.status == 200
    assert seen == ["owner-cookie"]


def test_static_sources_cover_closed_app_push_flow():
    assert "self.addEventListener('push', (event) => {" in SW_JS
    assert "self.addEventListener('pushsubscriptionchange', (event) => {" in SW_JS
    assert "__CSRF_TOKEN_JSON__" not in SW_JS
    assert "const csrfToken = typeof status.csrf_token === 'string' ? status.csrf_token : '';" in SW_JS
    assert "if (csrfToken) headers['X-Hermes-CSRF-Token'] = csrfToken;" in SW_JS
    assert "self.registration.showNotification(payload.title, payload.options)" in SW_JS
    assert "self.clients.matchAll({type: 'window', includeUncontrolled: true})" in SW_JS
    assert "client.visibilityState === 'visible' || client.focused === true" in SW_JS
    assert "clientUrl.pathname.startsWith(scopePath)" in SW_JS
    assert "/api/push/status" in SW_JS
    assert "/api/push/vapid-public-key" in SW_JS
    assert "method: 'DELETE'" in SW_JS
    assert "pushSubscriptionButton" in INDEX_HTML
    assert "pushSubscriptionStatus" in INDEX_HTML
    assert "toggleWebPushSubscription()" in INDEX_HTML
    assert "/api/push/status" in MESSAGES_JS
    assert "/api/push/vapid-public-key" in MESSAGES_JS
    assert "/api/push/subscribe" in MESSAGES_JS
    assert "method:'DELETE'" in MESSAGES_JS
    assert "_notify_response_complete_web_push(session_id, _answer)" in STREAMING_SRC
    assert STREAMING_SRC.count("_notify_response_complete_web_push(session_id, _answer)") == 1
    assert "notify_approval_required(session_key, head)" in ROUTE_APPROVALS_SRC
    assert "notify_clarify_required(session_key, entry.data)" in CLARIFY_SRC
    assert "pip install pywebpush" in REQUIREMENTS
    assert "\npywebpush>=2.0\n" not in REQUIREMENTS
    for key in [
        "web_push_enable_btn",
        "web_push_disable_btn",
        "web_push_enabled_toast",
        "web_push_disabled_toast",
        "web_push_unsupported",
        "web_push_server_not_configured",
        "web_push_server_unavailable",
        "web_push_status_active",
        "web_push_status_available",
        "web_push_error_prefix",
    ]:
        assert key in I18N_JS
