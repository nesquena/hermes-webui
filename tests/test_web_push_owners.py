"""Per-device Web Push targeting + strict CSRF on push routes."""
import json
import os
import stat
from urllib.parse import urlparse

import pytest

from api import web_push
from tests.test_web_push import (  # noqa: F401  (fixtures/helpers)
    APPLE, FCM, MOZILLA, DEV_A, DEV_B, OWNER_A, OWNER_B, PRIVATE_MARK,
    _H, _Capture, _enable, _sub, push_env,
)


@pytest.fixture
def env(push_env, monkeypatch):
    _enable(monkeypatch, push_env)
    monkeypatch.delenv("HERMES_WEBUI_PUSH_BROADCAST_UNOWNED", raising=False)
    monkeypatch.setattr(web_push, "_SEEN", {})
    sent = []

    def fake_webpush(**kw):
        sent.append((kw["subscription_info"]["endpoint"], json.loads(kw["data"])))

    monkeypatch.setattr(web_push, "_pywebpush", lambda: (fake_webpush, Exception))
    # run "enqueue" inline so tests are deterministic
    monkeypatch.setattr(web_push, "enqueue", lambda p: web_push._send_to_all(p) >= 0 and True)
    return sent


def test_owner_id_is_opaque_and_validated():
    assert OWNER_A != OWNER_B and len(OWNER_A) == 64 and DEV_A not in OWNER_A
    assert web_push.owner_for_device("short") == ""
    assert web_push.owner_for_device("bad chars!!!!!!!!!!!!!!!") == ""


def test_session_done_goes_only_to_owning_device(env):
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    web_push.add_subscription(_sub(FCM), owner=OWNER_B)
    web_push.register_session_owner("sess-a", OWNER_A)
    web_push.notify_session_done("sess-a", [{"role": "assistant", "content": "hi"}])
    assert [e for e, _ in env] == [APPLE]
    assert "_owners" not in env[0][1]


def test_session_opened_by_both_devices_notifies_both(env):
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    web_push.add_subscription(_sub(FCM), owner=OWNER_B)
    web_push.register_session_owner("s", OWNER_A)
    web_push.register_session_owner("s", OWNER_B)
    web_push.notify_bg_task_complete("s", {"message": "done"})
    assert sorted(e for e, _ in env) == sorted([APPLE, FCM])


@pytest.mark.parametrize("kind", ["done", "approval", "clarify", "bg"])
def test_unowned_session_is_not_broadcast_by_default(env, kind):
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    web_push.add_subscription(_sub(FCM), owner=OWNER_B)
    if kind == "done":
        web_push.notify_session_done("nobody", [{"role": "assistant", "content": "x"}])
    elif kind == "approval":
        web_push.notify_approval_required("nobody", {"approval_id": "1"})
    elif kind == "clarify":
        web_push.notify_clarify_required("nobody", {"clarify_id": "1"})
    else:
        web_push.notify_bg_task_complete("nobody", {})
    assert env == []


def test_explicit_opt_in_broadcasts_unowned_sessions_to_owned_subs(env, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PUSH_BROADCAST_UNOWNED", "1")
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    web_push.add_subscription(_sub(FCM), owner=OWNER_B)
    web_push.notify_session_done("nobody", [{"role": "assistant", "content": "x"}])
    assert sorted(e for e, _ in env) == sorted([APPLE, FCM])


def test_payload_without_owners_delivers_nothing(env):
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    assert web_push._send_to_all(web_push.notification_payload("t", "b")) == 0
    assert env == []


def test_send_test_only_to_caller(env):
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    web_push.add_subscription(_sub(FCM), owner=OWNER_B)
    assert web_push.send_test(OWNER_B) is True
    assert [e for e, _ in env] == [FCM]
    assert web_push.send_test("") is False


def test_session_owner_registry_persists_private_and_bounded(env, monkeypatch, push_env):
    web_push.register_session_owner("s1", OWNER_A)
    path = push_env / "webui_push_session_owners.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    monkeypatch.setattr(web_push, "_SESSION_CACHE", None)  # simulate restart
    assert web_push.session_owners("s1") == [OWNER_A]
    monkeypatch.setattr(web_push, "_SESSION_OWNERS_MAX", 3)
    for i in range(6):
        web_push.register_session_owner(f"x{i}", OWNER_A)
    assert len(json.loads(path.read_text())["sessions"]) == 3
    assert web_push.register_session_owner("", OWNER_A) is False
    assert web_push.register_session_owner("s", "not-an-owner") is False


# ── HTTP handlers: scoping + no leakage ──────────────────────────────────────

def test_status_and_unsubscribe_scoped_to_caller(env, monkeypatch):
    cap = _Capture(monkeypatch)
    r = cap.routes
    r._handle_push_subscribe(_H(DEV_A), {"subscription": _sub(APPLE)})
    r._handle_push_subscribe(_H(DEV_B), {"subscription": _sub(FCM)})
    r._handle_push_status(_H(DEV_B), urlparse("/api/push/status?endpoint=" + APPLE))
    assert cap.out[-1][1]["subscribed"] is False  # B can't see A's endpoint
    r._handle_push_status(_H(DEV_A), urlparse("/api/push/status?endpoint=" + APPLE))
    assert cap.out[-1][1]["subscribed"] is True
    r._handle_push_unsubscribe(_H(DEV_B), {"endpoint": APPLE})
    assert cap.out[-1][1] == {"ok": True, "removed": False}
    assert web_push.has_subscription(APPLE, OWNER_A)
    r._handle_push_unsubscribe(_H(DEV_A), {"endpoint": APPLE})
    assert cap.out[-1][1]["removed"] is True
    assert web_push.has_subscription(FCM, OWNER_B)


def test_test_push_only_caller_and_409_without_own_sub(env, monkeypatch):
    cap = _Capture(monkeypatch)
    r = cap.routes
    r._handle_push_subscribe(_H(DEV_A), {"subscription": _sub(APPLE)})
    r._handle_push_test(_H(DEV_B))
    assert cap.out[-1][0] == 409
    r._handle_push_test(_H(DEV_A))
    assert cap.out[-1] == (200, {"ok": True, "subscriptions": 1})
    assert [e for e, _ in env] == [APPLE]


def test_mutations_require_device_header(env, monkeypatch):
    cap = _Capture(monkeypatch)
    r = cap.routes
    r._handle_push_subscribe(_H(None), {"subscription": _sub(APPLE)})
    r._handle_push_unsubscribe(_H(None), {"endpoint": APPLE})
    r._handle_push_test(_H(None))
    r._handle_push_subscribe(_H("short"), {"subscription": _sub(APPLE)})
    assert [s for s, _ in cap.out] == [400, 400, 400, 400]
    assert web_push.subscription_count() == 0


def test_no_endpoint_or_owner_in_any_response(env, monkeypatch):
    cap = _Capture(monkeypatch)
    r = cap.routes
    r._handle_push_subscribe(_H(DEV_A), {"subscription": _sub(APPLE)})
    r._handle_push_subscribe(_H(DEV_B), {"subscription": _sub(FCM)})
    r._handle_push_status(_H(DEV_A), urlparse("/api/push/status"))
    r._handle_push_status(_H(DEV_B), urlparse("/api/push/status?endpoint=" + APPLE))
    r._handle_push_test(_H(DEV_A))
    r._handle_push_unsubscribe(_H(DEV_B), {"endpoint": APPLE})
    blob = json.dumps(cap.out)
    for secret in (APPLE, FCM, OWNER_A, OWNER_B, DEV_A, DEV_B, PRIVATE_MARK, "owner"):
        assert secret not in blob


def test_subscribe_rebinds_to_new_device_but_previous_endpoint_is_guarded(env):
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    # B cannot delete A's entry by naming it as "previous_endpoint"
    web_push.add_subscription(_sub(FCM), previous_endpoint=APPLE, owner=OWNER_B)
    assert web_push.has_subscription(APPLE, OWNER_A)
    # A may rotate its own
    web_push.add_subscription(_sub(MOZILLA), previous_endpoint=APPLE, owner=OWNER_A)
    assert not web_push.has_subscription(APPLE)


# ── migration of the pre-owner store ─────────────────────────────────────────

def _write_legacy(push_env):
    (push_env / "webui_push_subscriptions.json").write_text(json.dumps({"subscriptions": [_sub(APPLE)]}))


def test_legacy_subscription_loads_unowned_and_gets_no_pushes(env, push_env):
    _write_legacy(push_env)
    assert web_push.list_subscriptions()[0]["owner"] == ""
    web_push.register_session_owner("s", OWNER_A)
    web_push.notify_session_done("s", [{"role": "assistant", "content": "x"}])
    assert env == []


def test_legacy_subscription_binds_on_status_by_possessing_device(env, push_env, monkeypatch):
    _write_legacy(push_env)
    cap = _Capture(monkeypatch)
    r = cap.routes
    r._handle_push_status(_H(DEV_A), urlparse("/api/push/status"))  # no endpoint: no binding
    assert web_push.list_subscriptions()[0]["owner"] == ""
    r._handle_push_status(_H(DEV_A), urlparse("/api/push/status?endpoint=" + APPLE))
    assert cap.out[-1][1]["subscribed"] is True
    assert web_push.list_subscriptions()[0]["owner"] == OWNER_A
    # once owned, another device cannot steal it via status
    r._handle_push_status(_H(DEV_B), urlparse("/api/push/status?endpoint=" + APPLE))
    assert cap.out[-1][1]["subscribed"] is False
    assert web_push.list_subscriptions()[0]["owner"] == OWNER_A
    web_push.register_session_owner("s", OWNER_A)
    web_push.notify_session_done("s", [{"role": "assistant", "content": "x"}])
    assert [e for e, _ in env] == [APPLE]


def test_legacy_subscription_rebinds_on_resubscribe(env, push_env, monkeypatch):
    _write_legacy(push_env)
    cap = _Capture(monkeypatch)
    cap.routes._handle_push_subscribe(_H(DEV_B), {"subscription": _sub(APPLE)})
    assert web_push.list_subscriptions()[0]["owner"] == OWNER_B


# ── CSRF on push routes (real dispatch path) ─────────────────────────────────

class _Hdrs(dict):
    def get(self, k, d=None):
        for key, v in self.items():
            if key.lower() == k.lower():
                return v
        return d


class _Handler:
    def __init__(self, headers):
        self.headers = _Hdrs(headers)


def _auth_on(monkeypatch, valid_cookie="cookieVALID", token="goodtoken"):
    import api.auth as auth

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda h: h.headers.get("Cookie", "").replace("c=", ""))
    monkeypatch.setattr(auth, "verify_csrf_token", lambda c, t: c == valid_cookie and t == token)


def test_push_csrf_rejects_missing_or_bad_token_even_without_origin(monkeypatch):
    from api import routes

    _auth_on(monkeypatch)
    ok = routes._push_csrf_ok
    assert ok(_Handler({"Cookie": "c=cookieVALID"})) is False  # curl-style, no header
    assert ok(_Handler({"Cookie": "c=cookieVALID", "X-Hermes-CSRF-Token": "bad"})) is False
    assert ok(_Handler({"X-Hermes-CSRF-Token": "goodtoken"})) is False  # no session
    assert ok(_Handler({"Cookie": "c=cookieVALID", "X-Hermes-CSRF-Token": "goodtoken"})) is True


def test_push_csrf_not_required_when_auth_disabled(monkeypatch):
    import api.auth as auth
    from api import routes

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    assert routes._push_csrf_ok(_Handler({})) is True


def _dispatch(monkeypatch, method, path, headers, calls):
    from api import routes

    out = []
    monkeypatch.setattr(routes, "j", lambda h, payload, status=200, **kw: out.append((status, payload)) or True)
    monkeypatch.setattr(routes, "arm_connection_close_if_body_pending", lambda h: None)
    for name in ("_handle_push_subscribe", "_handle_push_unsubscribe", "_handle_push_test"):
        monkeypatch.setattr(routes, name, lambda *a, _n=name, **k: calls.append(_n) or True)
    monkeypatch.setattr(routes, "_check_csrf", lambda h: True)  # generic gate passes (non-browser)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *a, **k: False)
    monkeypatch.setattr(routes, "read_body", lambda h: {})
    fn = {"POST": routes.handle_post, "DELETE": routes.handle_delete}[method]
    h = _Handler(headers)
    h.path = path
    try:
        fn(h, urlparse(path))
    except Exception:
        pass
    return out


@pytest.mark.parametrize(
    "method,path,handler_name",
    [
        ("POST", "/api/push/subscribe", "_handle_push_subscribe"),
        ("POST", "/api/push/test", "_handle_push_test"),
        ("DELETE", "/api/push/subscribe", "_handle_push_unsubscribe"),
    ],
)
def test_dispatch_rejects_then_accepts(monkeypatch, method, path, handler_name):
    _auth_on(monkeypatch)
    calls = []
    out = _dispatch(monkeypatch, method, path, {"Cookie": "c=cookieVALID"}, calls)
    assert (403, {"error": "Session expired - reload the page"}) in out
    assert calls == []
    calls = []
    _dispatch(
        monkeypatch, method, path,
        {"Cookie": "c=cookieVALID", "X-Hermes-CSRF-Token": "goodtoken"}, calls,
    )
    assert calls == [handler_name]


# ── client wiring ────────────────────────────────────────────────────────────

def test_client_sends_csrf_and_device_headers():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    panels = (root / "static" / "panels.js").read_text()
    html = (root / "static" / "index.html").read_text()
    assert "X-Hermes-CSRF-Token" in panels and "X-Hermes-Push-Device" in panels
    assert panels.count("headers:_webPushHeaders()") >= 4
    assert "hermes-webui-push-device" in html and "X-Hermes-Push-Device" in html
