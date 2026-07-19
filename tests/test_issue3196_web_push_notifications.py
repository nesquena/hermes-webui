"""Focused regression coverage for true Web Push closed-app delivery (#3196)."""

from __future__ import annotations

import io
import json
import re
import socket
import subprocess
import threading
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SW_JS = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
STREAMING_SRC = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
ROUTE_APPROVALS_SRC = (ROOT / "api" / "route_approvals.py").read_text(encoding="utf-8")
CLARIFY_SRC = (ROOT / "api" / "clarify.py").read_text(encoding="utf-8")
ROUTES_SRC = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
REQUIREMENTS = (ROOT / "requirements.txt").read_text(encoding="utf-8")
_VALID_P256DH = "BBERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERE"
_VALID_AUTH = "IiIiIiIiIiIiIiIiIiIiIg"
_OWNER_A = "a" * 64
_OWNER_B = "b" * 64
_OWNER_COOKIE = "c" * 64


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
            "p256dh": _VALID_P256DH,
            "auth": _VALID_AUTH,
        },
    }


def _validated_fake_requests_session_factory(web_push):
    def _factory(endpoint: str):
        web_push._reject_unsafe_push_endpoint(endpoint)
        return SimpleNamespace(trust_env=False)

    return _factory


def _function_body(src: str, name: str) -> str:
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\b", src)
    assert match, f"{name} function not found"
    sig_end = src.find(")", match.end())
    assert sig_end != -1, f"{name} signature not terminated"
    brace_start = src.find("{", sig_end)
    assert brace_start != -1, f"{name} body not found"
    depth = 0
    for idx in range(brace_start, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[match.start():idx + 1]
    raise AssertionError(f"{name} body not terminated")


def _run_node_json(script: str) -> dict:
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise AssertionError(f"node failed: {result.stderr}")
    return json.loads(result.stdout)


@pytest.fixture(autouse=True)
def _push_example_resolves_public(monkeypatch):
    real_getaddrinfo = socket.getaddrinfo

    def _getaddrinfo(host, port=None, *args, **kwargs):
        if str(host).lower() == "push.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)


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

    def _fake_webpush(*, subscription_info, data, vapid_private_key, vapid_claims, requests_session, timeout):
        calls.append(
            {
                "endpoint": subscription_info["endpoint"],
                "data": json.loads(data),
                "vapid_private_key": vapid_private_key,
                "vapid_claims": dict(vapid_claims),
                "requests_session": requests_session,
                "timeout": timeout,
            }
        )
        if subscription_info["endpoint"].endswith("/dead"):
            exc = _WebPushException("gone")
            exc.response = SimpleNamespace(status_code=410)
            raise exc

    monkeypatch.setattr(web_push, "_get_pywebpush_impl", lambda: (_fake_webpush, _WebPushException))
    monkeypatch.setattr(web_push, "_web_push_requests_session", _validated_fake_requests_session_factory(web_push))

    web_push.upsert_subscription(_subscription("https://push.example/live"), owner_key=_OWNER_A)
    web_push.upsert_subscription(_subscription("https://push.example/dead"), owner_key=_OWNER_A)
    web_push.upsert_subscription(_subscription("https://push.example/other"), owner_key=_OWNER_B)

    sent = web_push.send_web_push(
        web_push._notification_payload("Response complete", "Task finished", session_id="session-123"),
        owner_key=_OWNER_A,
    )

    assert sent == 1
    assert [call["endpoint"] for call in calls] == [
        "https://push.example/live",
        "https://push.example/dead",
    ]
    assert calls[0]["data"]["options"]["data"]["url"] == "session/session-123"
    assert calls[0]["vapid_private_key"] == "private-key"
    assert calls[0]["vapid_claims"]["sub"] == "mailto:test@example.com"
    assert calls[0]["requests_session"].trust_env is False
    assert calls[0]["timeout"] == web_push._WEB_PUSH_TIMEOUT_SECONDS
    assert [sub["endpoint"] for sub in web_push.list_subscriptions(owner_key=_OWNER_A)] == ["https://push.example/live"]
    assert [sub["endpoint"] for sub in web_push.list_subscriptions(owner_key=_OWNER_B)] == ["https://push.example/other"]


def test_push_routes_support_status_subscribe_and_delete(monkeypatch, tmp_path):
    import api.config as config
    import api.profiles as profiles
    import api.routes as routes
    import api.web_push as web_push

    store_path = tmp_path / "webui_push_subscriptions.json"
    active_home = tmp_path / "active-profile"
    captured_homes = []
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "_request_csrf_token", lambda handler: "csrf-live")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: active_home)
    monkeypatch.setattr(
        web_push,
        "_subscription_store_path",
        lambda profile_home=None: captured_homes.append(profile_home) or store_path,
    )
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
        "active_profile_subscribed": False,
        "any_profile_subscribed": False,
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
    assert "owner" not in _payload(subscribe_handler)["subscription"]

    active_status_handler = _JSONHandler(headers={"Cookie": cookie_header})
    assert routes.handle_get(
        active_status_handler,
        SimpleNamespace(path="/api/push/status", query="endpoint=https%3A%2F%2Fpush.example%2Fbrowser"),
    ) is not False
    assert active_status_handler.status == 200
    active_status_payload = _payload(active_status_handler)
    assert active_status_payload["active_profile_subscribed"] is True
    assert active_status_payload["any_profile_subscribed"] is True

    delete_handler = _JSONHandler(
        {"endpoint": "https://push.example/browser"},
        headers={"Cookie": cookie_header},
    )
    assert routes.handle_delete(delete_handler, SimpleNamespace(path="/api/push/subscribe")) is not False
    assert delete_handler.status == 200
    assert _payload(delete_handler) == {
        "ok": True,
        "removed": True,
        "any_profile_subscribed_after": False,
    }
    assert web_push.list_subscriptions() == []
    assert captured_homes.count(active_home) == 6


def test_push_subscribe_stamps_visible_active_session_owner(monkeypatch, tmp_path):
    import api.profiles as profiles
    import api.routes as routes
    import api.web_push as web_push

    store_path = tmp_path / "webui_push_subscriptions.json"
    active_home = tmp_path / "active-profile"
    session = SimpleNamespace(
        session_id="session-123",
        profile="default",
        push_owner=None,
        messages=[],
        save_calls=[],
    )
    session.save = lambda touch_updated_at=False: session.save_calls.append(touch_updated_at)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *args, **kwargs: False)
    monkeypatch.setattr(routes, "_session_id_visible_to_request_profile", lambda handler, sid, **kwargs: sid in (None, session.session_id))
    monkeypatch.setattr(routes, "get_session", lambda sid: session)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: active_home)
    monkeypatch.setattr(web_push, "_subscription_store_path", lambda profile_home=None: store_path)
    monkeypatch.setattr(web_push, "web_push_status", lambda: {
        "enabled": True,
        "configured": True,
        "dependency_available": True,
    })

    subscribe_handler = _JSONHandler({
        "session_id": session.session_id,
        "subscription": _subscription("https://push.example/browser"),
    })
    assert routes.handle_post(subscribe_handler, SimpleNamespace(path="/api/push/subscribe")) is not False
    assert subscribe_handler.status == 200
    assert isinstance(session.push_owner, str) and len(session.push_owner) == 64
    assert session.save_calls == [False]


def test_push_routes_reject_expected_profile_mismatch(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes
    import api.web_push as web_push

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "profile-b")
    monkeypatch.setattr(web_push, "web_push_status", lambda: {
        "enabled": True,
        "configured": True,
        "dependency_available": True,
    })

    subscribe_handler = _JSONHandler({
        "expected_profile": "profile-a",
        "subscription": _subscription("https://push.example/browser"),
    })
    assert routes.handle_post(subscribe_handler, SimpleNamespace(path="/api/push/subscribe")) is False
    assert subscribe_handler.status == 409
    assert _payload(subscribe_handler)["error"] == "Active profile changed; retry the Web Push action"

    delete_handler = _JSONHandler({
        "expected_profile": "profile-a",
        "endpoint": "https://push.example/browser",
    })
    assert routes.handle_delete(delete_handler, SimpleNamespace(path="/api/push/subscribe")) is False
    assert delete_handler.status == 409
    assert _payload(delete_handler)["error"] == "Active profile changed; retry the Web Push action"


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

    web_push.upsert_subscription(_subscription("https://push.example/browser"), owner_key=_OWNER_A)

    delete_handler = _JSONHandler({"endpoint": "https://push.example/browser"})
    assert routes.handle_delete(delete_handler, SimpleNamespace(path="/api/push/subscribe")) is not False
    assert delete_handler.status == 400
    assert _payload(delete_handler)["error"] == "Web Push owner is required"


def test_push_unsubscribe_fails_closed_when_store_is_unreadable(monkeypatch, tmp_path):
    import api.profiles as profiles
    import api.routes as routes
    import api.web_push as web_push

    store_path = tmp_path / "webui_push_subscriptions.json"
    store_path.write_text("{bad json", encoding="utf-8")
    active_home = tmp_path / "active-profile"
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: active_home)
    monkeypatch.setattr(web_push, "_subscription_store_path", lambda profile_home=None: store_path)

    delete_handler = _JSONHandler(
        {"endpoint": "https://push.example/browser"},
        headers={"Cookie": "hermes_push_owner=" + _OWNER_A},
    )
    assert routes.handle_delete(delete_handler, SimpleNamespace(path="/api/push/subscribe")) is not False
    assert delete_handler.status == 503
    assert _payload(delete_handler)["error"] == "Web Push subscription store is unavailable"
    assert store_path.read_text(encoding="utf-8") == "{bad json"


def test_push_addr_blocks_cgnat_and_mapped_ipv6():
    import api.web_push as web_push

    assert web_push._push_addr_is_blocked("100.64.0.1") is True
    assert web_push._push_addr_is_blocked("100.100.100.100") is True
    assert web_push._push_addr_is_blocked("::ffff:100.64.0.1") is True


def test_subscription_validation_rejects_malformed_key_material(monkeypatch):
    import api.web_push as web_push

    monkeypatch.setattr(web_push, "_reject_unsafe_push_endpoint", lambda endpoint: endpoint)

    with pytest.raises(ValueError, match="p256dh"):
        web_push._normalize_subscription(
            {"endpoint": "https://push.example/browser", "keys": {"p256dh": "!", "auth": _VALID_AUTH}},
            owner_key="a" * 64,
        )
    with pytest.raises(ValueError, match="auth"):
        web_push._normalize_subscription(
            {"endpoint": "https://push.example/browser", "keys": {"p256dh": _VALID_P256DH, "auth": "bad"}},
            owner_key="a" * 64,
        )
    with pytest.raises(ValueError, match="expirationTime"):
        web_push._normalize_subscription(
            {
                "endpoint": "https://push.example/browser",
                "keys": {"p256dh": _VALID_P256DH, "auth": _VALID_AUTH},
                "expirationTime": {},
            },
            owner_key="a" * 64,
        )
    for invalid_expiration in ("", float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="expirationTime"):
            web_push._normalize_subscription(
                {
                    "endpoint": "https://push.example/browser",
                    "keys": {"p256dh": _VALID_P256DH, "auth": _VALID_AUTH},
                    "expirationTime": invalid_expiration,
                },
                owner_key="a" * 64,
            )


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
        "active_profile_subscribed": False,
        "any_profile_subscribed": False,
        "csrf_token": "csrf-live",
    }

    subscribe_handler = _JSONHandler({"subscription": _subscription("https://push.example/browser")})
    assert routes.handle_post(subscribe_handler, SimpleNamespace(path="/api/push/subscribe")) is not False
    assert subscribe_handler.status == 409
    assert _payload(subscribe_handler)["error"] == "Web Push is not configured"

    sent = web_push.send_web_push(
        web_push._notification_payload("Response complete", "Task finished", session_id="session-123"),
        owner_key=_OWNER_A,
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
                "owner": _OWNER_A,
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

    monkeypatch.setattr(web_push, "_subscription_store_path", lambda profile_home=None: store_path)

    def _slow_save(store: dict, *, profile_home=None) -> None:
        entered_save.set()
        release_save.wait(timeout=5)
        real_save_store(store, profile_home=profile_home)

    web_push._save_store({
        "subscriptions": [
            {
                **_subscription("https://push.example/dead"),
                "owner": _OWNER_A,
            }
        ]
    })
    monkeypatch.setattr(web_push, "_save_store", _slow_save)

    upsert_thread = threading.Thread(
        target=lambda: web_push.upsert_subscription(_subscription("https://push.example/live"), owner_key=_OWNER_A)
    )
    remove_thread = threading.Thread(
        target=lambda: (web_push.remove_subscription("https://push.example/dead", owner_key=_OWNER_A), removal_done.set())
    )

    upsert_thread.start()
    assert entered_save.wait(timeout=1), "upsert_subscription never reached the locked save path"
    remove_thread.start()
    assert not removal_done.wait(timeout=0.2), "remove_subscription should block behind the store lock"
    release_save.set()
    upsert_thread.join(timeout=5)
    remove_thread.join(timeout=5)

    assert sorted(sub["endpoint"] for sub in web_push.list_subscriptions(owner_key=_OWNER_A)) == ["https://push.example/live"]


def test_send_web_push_skips_other_browser_owners(monkeypatch, tmp_path):
    import api.config as config
    import api.web_push as web_push

    store_path = tmp_path / "webui_push_subscriptions.json"
    monkeypatch.setattr(web_push, "_subscription_store_path", lambda: store_path)
    monkeypatch.setattr(config, "web_push_configured", lambda: True)
    monkeypatch.setattr(config, "web_push_private_key", lambda: "private-key")
    monkeypatch.setattr(config, "web_push_subject", lambda: "mailto:test@example.com")

    seen = []

    def _fake_webpush(*, subscription_info, data, vapid_private_key, vapid_claims, requests_session, timeout):
        seen.append(subscription_info["endpoint"])

    monkeypatch.setattr(web_push, "_get_pywebpush_impl", lambda: (_fake_webpush, RuntimeError))
    monkeypatch.setattr(web_push, "_web_push_requests_session", _validated_fake_requests_session_factory(web_push))

    web_push.upsert_subscription(_subscription("https://push.example/a"), owner_key=_OWNER_A)
    web_push.upsert_subscription(_subscription("https://push.example/b"), owner_key=_OWNER_B)

    sent = web_push.send_web_push(
        web_push._notification_payload("Response complete", "Task finished", session_id="session-123"),
        owner_key=_OWNER_A,
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

    def _fake_webpush(*, subscription_info, data, vapid_private_key, vapid_claims, requests_session, timeout):
        seen.append(subscription_info["endpoint"])

    monkeypatch.setattr(web_push, "_get_pywebpush_impl", lambda: (_fake_webpush, RuntimeError))
    monkeypatch.setattr(web_push, "_web_push_requests_session", _validated_fake_requests_session_factory(web_push))

    web_push._save_store({
        "subscriptions": [
            {
                **_subscription("https://127.0.0.1/browser"),
                "owner": _OWNER_A,
            },
            {
                **_subscription("https://push.example/live"),
                "owner": _OWNER_A,
            },
        ]
    })

    sent = web_push.send_web_push(
        web_push._notification_payload("Response complete", "Task finished", session_id="session-123"),
        owner_key=_OWNER_A,
    )

    assert sent == 1
    assert seen == ["https://push.example/live"]
    assert [sub["endpoint"] for sub in web_push.list_subscriptions(owner_key=_OWNER_A)] == [
        "https://push.example/live"
    ]


def test_send_web_push_keeps_subscription_on_transient_dns_failure(monkeypatch, tmp_path):
    import api.config as config
    import api.web_push as web_push

    store_path = tmp_path / "webui_push_subscriptions.json"
    seen = []

    monkeypatch.setattr(web_push, "_subscription_store_path", lambda profile_home=None: store_path)
    monkeypatch.setattr(config, "web_push_configured", lambda: True)
    monkeypatch.setattr(config, "web_push_private_key", lambda: "private-key")
    monkeypatch.setattr(config, "web_push_subject", lambda: "mailto:test@example.com")

    def _getaddrinfo(host, port=None, *args, **kwargs):
        if str(host).lower() == "offline.example":
            raise socket.gaierror("temporary failure")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]

    def _fake_webpush(*, subscription_info, data, vapid_private_key, vapid_claims, requests_session, timeout):
        seen.append(subscription_info["endpoint"])

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    monkeypatch.setattr(web_push, "_get_pywebpush_impl", lambda: (_fake_webpush, RuntimeError))
    web_push._save_store({
        "subscriptions": [
            {
                **_subscription("https://offline.example/push"),
                "owner": _OWNER_A,
            }
        ]
    })

    sent = web_push.send_web_push(
        web_push._notification_payload("Response complete", "Task finished", session_id="session-123"),
        owner_key=_OWNER_A,
    )

    assert sent == 0
    assert seen == []
    assert [sub["endpoint"] for sub in web_push.list_subscriptions(owner_key=_OWNER_A)] == [
        "https://offline.example/push"
    ]


def test_web_push_transport_unavailable_after_endpoint_validation(monkeypatch):
    import builtins
    import api.web_push as web_push

    real_import = builtins.__import__

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "requests" or name.startswith("requests."):
            raise ImportError("requests intentionally unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _import)

    with pytest.raises(ValueError, match="private IP"):
        web_push._web_push_requests_session("https://127.0.0.1/push")
    with pytest.raises(web_push._PushTransportUnavailable):
        web_push._web_push_requests_session("https://push.example/path")


def test_send_web_push_pins_transport_against_dns_rebind(monkeypatch, tmp_path):
    pytest.importorskip("requests")
    import api.config as config
    import api.web_push as web_push

    store_path = tmp_path / "webui_push_subscriptions.json"
    dns_answers = iter(["93.184.216.34", "93.184.216.34"])
    resolved_hosts = []
    dialed_hosts = []
    webpush_calls = []

    monkeypatch.setattr(web_push, "_subscription_store_path", lambda: store_path)
    monkeypatch.setattr(config, "web_push_configured", lambda: True)
    monkeypatch.setattr(config, "web_push_private_key", lambda: "private-key")
    monkeypatch.setattr(config, "web_push_subject", lambda: "mailto:test@example.com")

    def _getaddrinfo(host, port=None, *args, **kwargs):
        if str(host).lower() == "rebind.example":
            resolved_hosts.append(str(host))
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(dns_answers), port or 443))]
        raise AssertionError(f"unexpected DNS lookup for {host}")

    def _create_connection(pinned_host, port, timeout, source_address, socket_options):
        dialed_hosts.append(pinned_host)
        raise OSError("stop before network")

    def _fake_webpush(*, subscription_info, data, vapid_private_key, vapid_claims, requests_session, timeout):
        webpush_calls.append(subscription_info["endpoint"])
        requests_session.post(subscription_info["endpoint"], data=b"{}", timeout=0.1)

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    monkeypatch.setattr(web_push, "_create_pinned_push_connection", _create_connection)
    monkeypatch.setattr(web_push, "_get_pywebpush_impl", lambda: (_fake_webpush, RuntimeError))

    web_push.upsert_subscription(_subscription("https://rebind.example/push"), owner_key=_OWNER_A)

    sent = web_push.send_web_push(
        web_push._notification_payload("Response complete", "Task finished", session_id="session-123"),
        owner_key=_OWNER_A,
    )

    assert sent == 0
    assert webpush_calls == ["https://rebind.example/push"]
    assert resolved_hosts == ["rebind.example", "rebind.example"]
    assert dialed_hosts == ["93.184.216.34"]
    assert [sub["endpoint"] for sub in web_push.list_subscriptions(owner_key=_OWNER_A)] == [
        "https://rebind.example/push"
    ]


def test_web_push_transport_refuses_redirects(monkeypatch):
    requests = pytest.importorskip("requests")
    import api.web_push as web_push

    session = web_push._web_push_requests_session("https://push.example/path")
    adapter = session.get_adapter("https://push.example/path")

    def _redirect_response(self, request, **kwargs):
        response = requests.Response()
        response.status_code = 307
        response.headers["Location"] = "https://127.0.0.1/push"
        response.url = request.url
        response.request = request
        return response

    monkeypatch.setattr(type(adapter), "send", _redirect_response)

    with pytest.raises(ValueError, match="does not allow redirects"):
        session.post("https://push.example/path", data=b"{}", timeout=0.1)


def test_web_push_transport_ignores_environment_proxies(monkeypatch):
    pytest.importorskip("requests")
    import api.web_push as web_push

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")

    session = web_push._web_push_requests_session("https://push.example/path")
    settings = session.merge_environment_settings("https://push.example/path", {}, None, None, None)

    assert session.trust_env is False
    assert settings["proxies"] == {}


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


def test_notify_response_complete_enqueues_background_session_lookup(monkeypatch):
    import api.web_push as web_push

    seen = []
    monkeypatch.setattr(
        web_push,
        "_session_push_target",
        lambda session_id: (_ for _ in ()).throw(AssertionError("session lookup must stay in the background job")),
    )
    monkeypatch.setattr(
        web_push,
        "_enqueue_web_push",
        lambda payload, *, session_id=None, owner_key=None, profile_home=None: seen.append((session_id, payload["title"])) or 1,
    )

    assert web_push.notify_response_complete("session-123", "Final answer") == 1
    assert seen == [
        ("session-123", "Response complete"),
    ]


def test_worker_delivery_uses_persisted_profile_home_without_tls(monkeypatch, tmp_path):
    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.web_push as web_push

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "session-a.json").write_text(json.dumps({
        "session_id": "session-a", "title": "A", "created_at": 1, "updated_at": 1,
        "profile": "profile-a", "push_owner": _OWNER_A, "messages": [],
    }), encoding="utf-8")
    monkeypatch.setattr(models, "SESSION_DIR", sessions)
    monkeypatch.setattr(config, "web_push_configured", lambda: True)
    monkeypatch.setattr(config, "web_push_private_key", lambda: "private-key")
    monkeypatch.setattr(config, "web_push_subject", lambda: "mailto:test@example.com")
    home_a, home_b = tmp_path / "profile-a", tmp_path / "profile-b"
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: home_a if name == "profile-a" else home_b)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: (_ for _ in ()).throw(AssertionError("worker used TLS")))
    sent = []

    def _fake_webpush(*, subscription_info, **kwargs):
        sent.append(subscription_info["endpoint"])
        exc = RuntimeError("gone")
        exc.response = SimpleNamespace(status_code=410)
        if subscription_info["endpoint"].endswith("/dead"):
            raise exc

    monkeypatch.setattr(web_push, "_get_pywebpush_impl", lambda: (_fake_webpush, RuntimeError))
    monkeypatch.setattr(web_push, "_web_push_requests_session", _validated_fake_requests_session_factory(web_push))
    monkeypatch.setattr(
        web_push,
        "_enqueue_web_push",
        lambda payload, *, session_id=None, owner_key=None, profile_home=None: (
            lambda result: (
                web_push._prune_stale_endpoints(
                    result["stale_endpoints"],
                    owner_key=result["owner_key"],
                    profile_home=Path(result["profile_home"]).expanduser() if result["profile_home"] else None,
                ),
                result["sent"],
            )[1]
        )(web_push._deliver_web_push_job_result({
            "payload": payload,
            "session_id": session_id,
            "owner_key": owner_key,
            "profile_home": profile_home,
        })),
    )
    web_push.upsert_subscription(_subscription("https://push.example/live"), owner_key=_OWNER_A, profile_home=home_a)
    web_push.upsert_subscription(_subscription("https://push.example/dead"), owner_key=_OWNER_A, profile_home=home_a)
    web_push.upsert_subscription(_subscription("https://push.example/other"), owner_key=_OWNER_A, profile_home=home_b)

    assert web_push.notify_response_complete("session-a", "done") == 1
    assert sent == ["https://push.example/live", "https://push.example/dead"]
    assert [sub["endpoint"] for sub in web_push.list_subscriptions(owner_key=_OWNER_A, profile_home=home_a)] == ["https://push.example/live"]
    assert [sub["endpoint"] for sub in web_push.list_subscriptions(owner_key=_OWNER_A, profile_home=home_b)] == ["https://push.example/other"]


def test_subscription_rotation_updates_all_owner_profile_stores(monkeypatch, tmp_path):
    import api.web_push as web_push

    def _store_path(profile_home=None):
        home = Path(profile_home or tmp_path)
        home.mkdir(parents=True, exist_ok=True)
        return home / "webui_push_subscriptions.json"

    monkeypatch.setattr(web_push, "_subscription_store_path", _store_path)

    home_a = tmp_path / "profiles" / "a"
    home_b = tmp_path / "profiles" / "b"
    old_endpoint = "https://push.example/old"
    new_endpoint = "https://push.example/new"
    monkeypatch.setattr(web_push, "_iter_push_store_homes", lambda: iter([home_a, home_b]))

    web_push.upsert_subscription(_subscription(old_endpoint), owner_key=_OWNER_A, profile_home=home_a)
    web_push.upsert_subscription(_subscription(old_endpoint), owner_key=_OWNER_A, profile_home=home_b)
    web_push.upsert_subscription(_subscription("https://push.example/other"), owner_key=_OWNER_B, profile_home=home_b)

    saved = web_push.upsert_subscription_for_owner_profiles(
        _subscription(new_endpoint),
        owner_key=_OWNER_A,
        previous_endpoint=old_endpoint,
        profile_home=home_b,
    )

    assert saved["endpoint"] == new_endpoint
    assert [sub["endpoint"] for sub in web_push.list_subscriptions(owner_key=_OWNER_A, profile_home=home_a)] == [new_endpoint]
    assert [sub["endpoint"] for sub in web_push.list_subscriptions(owner_key=_OWNER_A, profile_home=home_b)] == [new_endpoint]
    assert [sub["endpoint"] for sub in web_push.list_subscriptions(owner_key=_OWNER_B, profile_home=home_b)] == ["https://push.example/other"]


def test_malformed_subscription_entry_fails_closed_without_rewriting(monkeypatch, tmp_path):
    import api.web_push as web_push

    store_path = tmp_path / "webui_push_subscriptions.json"
    monkeypatch.setattr(web_push, "_subscription_store_path", lambda profile_home=None: store_path)
    store_path.write_text(json.dumps({
        "subscriptions": [
            {
                "endpoint": "https://push.example/corrupt",
                "keys": {"p256dh": "!", "auth": _VALID_AUTH},
                "owner": _OWNER_A,
            },
            _subscription("https://push.example/live") | {"owner": _OWNER_A},
        ]
    }), encoding="utf-8")
    original = store_path.read_text(encoding="utf-8")

    with pytest.raises(web_push._PushStoreUnavailable):
        web_push.list_subscriptions(owner_key=_OWNER_A)
    with pytest.raises(web_push._PushStoreUnavailable):
        web_push.upsert_subscription(_subscription("https://push.example/new"), owner_key=_OWNER_A)

    assert store_path.read_text(encoding="utf-8") == original


def test_remove_subscription_for_owner_profiles_cleans_all_associations(monkeypatch, tmp_path):
    import api.web_push as web_push

    def _store_path(profile_home=None):
        home = Path(profile_home or tmp_path)
        home.mkdir(parents=True, exist_ok=True)
        return home / "webui_push_subscriptions.json"

    monkeypatch.setattr(web_push, "_subscription_store_path", _store_path)

    home_a = tmp_path / "profiles" / "a"
    home_b = tmp_path / "profiles" / "b"
    endpoint = "https://push.example/old"
    monkeypatch.setattr(web_push, "_iter_push_store_homes", lambda: iter([home_a, home_b]))

    web_push.upsert_subscription(_subscription(endpoint), owner_key=_OWNER_A, profile_home=home_a)
    web_push.upsert_subscription(_subscription(endpoint), owner_key=_OWNER_A, profile_home=home_b)

    assert web_push.remove_subscription_for_owner_profiles(endpoint, owner_key=_OWNER_A) is True
    assert web_push.list_subscriptions(owner_key=_OWNER_A, profile_home=home_a) == []
    assert web_push.list_subscriptions(owner_key=_OWNER_A, profile_home=home_b) == []


def test_iter_push_store_homes_clamps_to_startup_home_in_isolated_mode(monkeypatch, tmp_path):
    import api.profiles as profiles
    import api.web_push as web_push

    isolated_home = tmp_path / "profiles" / "isolated"
    other_home = tmp_path / "profiles" / "other"
    isolated_home.mkdir(parents=True)
    other_home.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path)
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(isolated_home))
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: True)

    homes = list(web_push._iter_push_store_homes())

    assert homes == [isolated_home]


def test_gateway_mirror_notifies_only_on_new_head_after_lock_release(monkeypatch):
    import api.route_approvals as route_approvals
    import api.web_push as web_push

    session_id = "gateway-push"
    seen = []
    monkeypatch.setattr(route_approvals, "publish_session_list_changed", lambda *_: None)
    monkeypatch.setattr(web_push, "notify_approval_required", lambda sid, head: seen.append((sid, head["description"])) or 1)
    with route_approvals._lock:
        route_approvals._pending.pop(session_id, None)
        route_approvals._gateway_queues[session_id] = [SimpleNamespace(data={"description": "Approve gateway action"})]
    route_approvals.submit_gateway_pending_mirror(session_id, {})
    route_approvals.submit_gateway_pending_mirror(session_id, {})
    with route_approvals._lock:
        route_approvals._pending.pop(session_id, None)
        route_approvals._gateway_queues.pop(session_id, None)
    assert seen == [(session_id, "Approve gateway action")]


def test_send_web_push_stops_after_shutdown_signal(monkeypatch, tmp_path):
    import api.config as config
    import api.web_push as web_push

    store_path = tmp_path / "webui_push_subscriptions.json"
    stop_event = threading.Event()
    seen = []
    monkeypatch.setattr(web_push, "_subscription_store_path", lambda profile_home=None: store_path)
    monkeypatch.setattr(config, "web_push_configured", lambda: True)
    monkeypatch.setattr(config, "web_push_private_key", lambda: "private-key")
    monkeypatch.setattr(config, "web_push_subject", lambda: "mailto:test@example.com")
    monkeypatch.setattr(web_push, "_WEB_PUSH_DELIVERY_STOP_EVENT", stop_event)
    monkeypatch.setattr(web_push, "_web_push_requests_session", _validated_fake_requests_session_factory(web_push))

    def _fake_webpush(*, subscription_info, **kwargs):
        seen.append(subscription_info["endpoint"])
        stop_event.set()

    monkeypatch.setattr(web_push, "_get_pywebpush_impl", lambda: (_fake_webpush, RuntimeError))

    web_push.upsert_subscription(_subscription("https://push.example/one"), owner_key=_OWNER_A)
    web_push.upsert_subscription(_subscription("https://push.example/two"), owner_key=_OWNER_A)

    sent = web_push.send_web_push(
        web_push._notification_payload("Response complete", "Task finished", session_id="session-123"),
        owner_key=_OWNER_A,
    )

    assert sent == 1
    assert seen == ["https://push.example/one"]


def test_run_web_push_delivery_job_terminates_overdue_worker(monkeypatch):
    import api.web_push as web_push

    class _FakeProcess:
        def __init__(self):
            self.pid = 17
            self.joins = []
            self.terminated = 0
            self._alive = True

        def join(self, timeout=None):
            self.joins.append(timeout)

        def is_alive(self):
            return self._alive

        def terminate(self):
            self.terminated += 1
            self._alive = False

    process = _FakeProcess()
    monkeypatch.setattr(web_push, "_start_web_push_delivery_process", lambda job: process)
    monkeypatch.setattr(web_push, "_WEB_PUSH_DELIVERY_LOCK", threading.Lock())
    monkeypatch.setattr(web_push, "_WEB_PUSH_DELIVERY_ACTIVE_PROCESSES", {})
    monkeypatch.setattr(web_push, "_WEB_PUSH_DELIVERY_WALL_CLOCK_SECONDS", 15)
    monkeypatch.setattr(web_push, "_WEB_PUSH_DELIVERY_STOP_EVENT", threading.Event())
    monkeypatch.setattr(web_push, "_WEB_PUSH_DELIVERY_SHUTDOWN_DEADLINE", None)

    web_push._run_web_push_delivery_job({"session_id": "session-123", "payload": {"title": "Response complete"}})

    assert process.joins == [15, 1.0]
    assert process.terminated == 1


def test_run_web_push_delivery_job_prunes_stale_endpoints_in_parent_process(monkeypatch, tmp_path):
    import api.web_push as web_push
    import queue as _queue

    class _FakeResultQueue:
        def __init__(self, result):
            self._result = result
            self.closed = 0
            self.joined = 0

        def get_nowait(self):
            if self._result is None:
                raise _queue.Empty()
            result = self._result
            self._result = None
            return result

        def close(self):
            self.closed += 1

        def join_thread(self):
            self.joined += 1

    class _FakeProcess:
        def __init__(self, result_queue):
            self.pid = 23
            self.joins = []
            self.terminated = 0
            self._alive = False
            self._web_push_result_queue = result_queue

        def join(self, timeout=None):
            self.joins.append(timeout)

        def is_alive(self):
            return self._alive

        def terminate(self):
            self.terminated += 1
            self._alive = False

    result_queue = _FakeResultQueue({
        "owner_key": _OWNER_A,
        "profile_home": str(tmp_path),
        "sent": 0,
        "stale_endpoints": ["https://push.example/dead"],
    })
    process = _FakeProcess(result_queue)
    removals = []
    monkeypatch.setattr(web_push, "_start_web_push_delivery_process", lambda job: process)
    monkeypatch.setattr(web_push, "_WEB_PUSH_DELIVERY_LOCK", threading.Lock())
    monkeypatch.setattr(web_push, "_WEB_PUSH_DELIVERY_ACTIVE_PROCESSES", {})
    monkeypatch.setattr(web_push, "_WEB_PUSH_DELIVERY_WALL_CLOCK_SECONDS", 15)
    monkeypatch.setattr(web_push, "_WEB_PUSH_DELIVERY_STOP_EVENT", threading.Event())
    monkeypatch.setattr(web_push, "_WEB_PUSH_DELIVERY_SHUTDOWN_DEADLINE", None)
    monkeypatch.setattr(
        web_push,
        "remove_subscription",
        lambda endpoint, *, owner_key=None, profile_home=None: removals.append(
            (endpoint, owner_key, profile_home)
        ) or True,
    )

    web_push._run_web_push_delivery_job({"session_id": "session-123", "payload": {"title": "Response complete"}})

    assert process.joins == [15]
    assert process.terminated == 0
    assert removals == [("https://push.example/dead", _OWNER_A, tmp_path)]
    assert result_queue.closed == 1
    assert result_queue.joined == 1


def test_shutdown_web_push_delivery_sets_stop_flag_and_terminates_stragglers(monkeypatch):
    import api.web_push as web_push
    import queue as _queue

    stop_event = threading.Event()
    delivery_queue = _queue.Queue()
    delivery_queue.put({"payload": {"title": "queued"}})
    joined = []

    class _FakeThread:
        def join(self, timeout=None):
            joined.append(timeout)

    class _FakeProcess:
        def __init__(self):
            self.pid = 19
            self.terminated = 0
            self.joins = []
            self._alive = True

        def is_alive(self):
            return self._alive

        def terminate(self):
            self.terminated += 1
            self._alive = False

        def join(self, timeout=None):
            self.joins.append(timeout)

    process = _FakeProcess()
    monkeypatch.setattr(web_push, "_WEB_PUSH_DELIVERY_STOP_EVENT", stop_event)
    monkeypatch.setattr(web_push, "_WEB_PUSH_DELIVERY_QUEUE", delivery_queue)
    monkeypatch.setattr(web_push, "_WEB_PUSH_DELIVERY_WORKERS", [_FakeThread()])
    monkeypatch.setattr(web_push, "_WEB_PUSH_DELIVERY_ACTIVE_PROCESSES", {process.pid: process})
    monkeypatch.setattr(web_push, "_WEB_PUSH_DELIVERY_LOCK", threading.Lock())
    monkeypatch.setattr(web_push, "_WEB_PUSH_DELIVERY_SHUTDOWN_WAIT_SECONDS", 5)

    web_push.shutdown_web_push_delivery()

    assert stop_event.is_set() is True
    assert delivery_queue.get_nowait() is None
    assert joined and joined[0] <= 5
    assert process.terminated == 1
    assert process.joins == [1.0]


def test_send_web_push_uses_fresh_vapid_claims_per_subscription(monkeypatch, tmp_path):
    import api.config as config
    import api.web_push as web_push

    store_path = tmp_path / "webui_push_subscriptions.json"
    monkeypatch.setattr(web_push, "_subscription_store_path", lambda: store_path)
    monkeypatch.setattr(config, "web_push_configured", lambda: True)
    monkeypatch.setattr(config, "web_push_private_key", lambda: "private-key")
    monkeypatch.setattr(config, "web_push_subject", lambda: "mailto:test@example.com")
    monkeypatch.setattr(web_push, "_web_push_requests_session", _validated_fake_requests_session_factory(web_push))

    claims_seen = []

    def _fake_webpush(*, subscription_info, vapid_claims, **kwargs):
        claims_seen.append(dict(vapid_claims))
        vapid_claims["aud"] = subscription_info["endpoint"]

    monkeypatch.setattr(web_push, "_get_pywebpush_impl", lambda: (_fake_webpush, RuntimeError))

    web_push.upsert_subscription(_subscription("https://push.example/one"), owner_key=_OWNER_A)
    web_push.upsert_subscription(_subscription("https://push.example/two"), owner_key=_OWNER_A)

    sent = web_push.send_web_push(
        web_push._notification_payload("Response complete", "Task finished", session_id="session-123"),
        owner_key=_OWNER_A,
    )

    assert sent == 2
    assert claims_seen == [
        {"sub": "mailto:test@example.com"},
        {"sub": "mailto:test@example.com"},
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


def test_clarify_push_tracks_the_actionable_head(monkeypatch):
    import api.clarify as clarify
    import api.web_push as web_push

    session_id = "clarify-head-push"
    seen = []
    monkeypatch.setattr(clarify, "publish_session_list_changed", lambda *_: None)
    monkeypatch.setattr(web_push, "notify_clarify_required", lambda sid, head: seen.append((sid, head["question"])) or 1)

    try:
        first = clarify.submit_pending(
            session_id,
            {"question": "Clarify A?", "choices_offered": ["yes", "no"]},
        )
        clarify.submit_pending(
            session_id,
            {"question": "Clarify B?", "choices_offered": ["yes", "no"]},
        )
        assert clarify.resolve_clarify_by_id(session_id, first.clarify_id, "yes") is True
    finally:
        clarify.clear_pending(session_id)

    assert seen == [
        (session_id, "Clarify A?"),
        (session_id, "Clarify B?"),
    ]


def test_local_approval_push_notifies_new_head_once_and_then_next_head_on_resolution(monkeypatch):
    import api.route_approvals as route_approvals
    import api.routes as routes
    import api.web_push as web_push

    session_id = "local-approval-push"
    seen = []
    monkeypatch.setattr(route_approvals, "publish_session_list_changed", lambda *_: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_: None)
    monkeypatch.setattr(web_push, "notify_approval_required", lambda sid, head: seen.append((sid, head["description"])) or 1)

    try:
        route_approvals.submit_pending(
            session_id,
            {
                "command": "cmd-a",
                "pattern_key": "dangerous-a",
                "pattern_keys": ["dangerous-a"],
                "description": "Approve A",
            },
        )
        route_approvals.submit_pending(
            session_id,
            {
                "command": "cmd-b",
                "pattern_key": "dangerous-b",
                "pattern_keys": ["dangerous-b"],
                "description": "Approve B",
            },
        )
        with route_approvals._lock:
            first_id = route_approvals._pending[session_id][0]["approval_id"]
        assert routes._resolve_approval_legacy(session_id, first_id, "once") is True
    finally:
        with route_approvals._lock:
            route_approvals._pending.pop(session_id, None)

    assert seen == [
        (session_id, "Approve A"),
        (session_id, "Approve B"),
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

    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda session_id, **kwargs: session)
    monkeypatch.setattr(routes, "_profiles_match", lambda left, right: left == right)
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda s, workspace: workspace or s.workspace)
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda s, requested_provider: (None, None, None))
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
        headers={"Cookie": "hermes_push_owner=" + _OWNER_COOKIE},
    )

    assert routes._handle_chat_start(handler, json.loads(handler.rfile.getvalue())) is None
    assert handler.status == 200
    assert seen == [_OWNER_COOKIE]


def test_unsubscribe_from_web_push_does_not_globally_unsubscribe_on_delete_failure():
    function_src = _function_body(MESSAGES_JS, "unsubscribeFromWebPush")
    script = textwrap.dedent(
        f"""
        let unsubscribed = false;
        let refreshed = 0;
        const toasts = [];
        global._webPushRegistration = async () => ({{pushManager: {{getSubscription: async () => null}}}});
        global._getWebPushSubscription = async () => ({{
          endpoint: 'https://push.example/live',
          unsubscribe: async () => {{ unsubscribed = true; }},
        }});
        global.api = async () => {{ throw new Error('delete failed'); }};
        global.refreshWebPushUi = async () => {{ refreshed += 1; }};
        global.showToast = (...args) => toasts.push(args);
        global.t = (key) => key;
        {function_src}
        unsubscribeFromWebPush()
          .then(() => process.stdout.write(JSON.stringify({{status: 'resolved', unsubscribed, refreshed, toasts}})))
          .catch((err) => process.stdout.write(JSON.stringify({{
            status: 'rejected',
            message: String(err && err.message || err),
            unsubscribed,
            refreshed,
            toasts,
          }})));
        """
    )
    result = _run_node_json(script)
    assert result["status"] == "rejected"
    assert result["message"] == "delete failed"
    assert result["unsubscribed"] is False
    assert result["refreshed"] == 0
    assert result["toasts"] == []


def test_refresh_web_push_ui_ignores_stale_profile_switch_response():
    helper_src = _function_body(MESSAGES_JS, "_isCurrentWebPushUiRefresh")
    function_src = _function_body(MESSAGES_JS, "refreshWebPushUi")
    script = textwrap.dedent(
        f"""
        const elements = {{
          pushSubscriptionStatus: {{ textContent: '', style: {{ display: '' }} }},
          pushSubscriptionButton: {{
            textContent: '',
            attrs: {{}},
            setAttribute(name, value) {{ this.attrs[name] = value; }},
            onclick: null,
          }},
          pushSubscriptionButtonWrap: {{ style: {{ display: '' }} }},
        }};
        global.$ = (id) => elements[id];
        global.S = {{ activeProfile: 'profile-a' }};
        global.window = {{ PushManager: function PushManager(){{}} }};
        Object.defineProperty(globalThis, 'navigator', {{ value: {{ serviceWorker: {{}} }}, configurable: true }});
        Object.defineProperty(globalThis, 'Notification', {{ value: {{ permission: 'granted' }}, configurable: true }});
        global.toggleWebPushSubscription = async () => {{}};
        global.t = (key) => key;
        let _webPushUiRefreshGeneration = 0;
        let statusCall = 0;
        global._getWebPushSubscription = async () => ({{ endpoint: 'https://push.example/live' }});
        global._getWebPushServerStatus = async () => {{
          statusCall += 1;
          const call = statusCall;
          await new Promise((resolve) => setTimeout(resolve, call === 1 ? 20 : 0));
          return {{
            enabled: true,
            configured: true,
            dependency_available: true,
            active_profile_subscribed: call === 1,
            any_profile_subscribed: call === 1,
          }};
        }};
        {helper_src}
        {function_src}
        (async () => {{
          const first = refreshWebPushUi();
          await new Promise((resolve) => setTimeout(resolve, 0));
          S.activeProfile = 'profile-b';
          const second = refreshWebPushUi();
          await Promise.all([first, second]);
          process.stdout.write(JSON.stringify({{
            buttonText: elements.pushSubscriptionButton.textContent,
            statusText: elements.pushSubscriptionStatus.textContent,
            generation: _webPushUiRefreshGeneration,
          }}));
        }})();
        """
    )
    result = _run_node_json(script)
    assert result["buttonText"] == "web_push_enable_btn"
    assert result["statusText"] == "web_push_status_available"
    assert result["generation"] == 2


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
    assert "all_profiles: true" in SW_JS
    assert "previous_endpoint: oldEndpoint || ''" in SW_JS
    assert "pushSubscriptionButton" in INDEX_HTML
    assert "pushSubscriptionStatus" in INDEX_HTML
    assert "toggleWebPushSubscription()" in INDEX_HTML
    assert "/api/push/status" in MESSAGES_JS
    assert "/api/push/vapid-public-key" in MESSAGES_JS
    assert "/api/push/subscribe" in MESSAGES_JS
    assert "method:'DELETE'" in MESSAGES_JS
    assert "payload && !payload.any_profile_subscribed_after" in MESSAGES_JS
    assert "expected_profile:mutation&&mutation.profile||''" in MESSAGES_JS
    assert "session_id:mutation&&mutation.sessionId||''" in MESSAGES_JS
    assert "_notify_response_complete_web_push(session_id, _answer)" in STREAMING_SRC
    assert STREAMING_SRC.count("_notify_response_complete_web_push(session_id, _answer)") == 1
    assert "notify_approval_required(session_key, head)" in ROUTE_APPROVALS_SRC
    assert "notify_clarify_required(session_key, head_to_push)" in CLARIFY_SRC
    assert "notify_clarify_required(session_key, next_head_to_push)" in CLARIFY_SRC
    assert "_notify_response_complete_web_push(session_id, assistant_text)" in (ROOT / "api" / "gateway_chat.py").read_text(encoding="utf-8")
    assert "get_hermes_home_for_profile" in (ROOT / "api" / "web_push.py").read_text(encoding="utf-8")
    assert "def _stamp_session_push_owner_from_request(handler, session, *, log_label: str) -> None:" in ROUTES_SRC
    assert "def _web_push_expected_profile_matches_request(handler, body) -> bool:" in ROUTES_SRC
    assert ROUTES_SRC.count("_stamp_session_push_owner_from_request(") >= 4
    assert ROUTES_SRC.count('push_owner=getattr(source, "push_owner", None)') >= 2
    assert "https://github.com/nesquena/hermes-webui#web-push-setup" in MESSAGES_JS
    assert I18N_JS.count("iOS 16.4") == 14
    assert "HERMES_WEBUI_VAPID_PUBLIC_KEY" in (ROOT / "README.md").read_text(encoding="utf-8")
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
