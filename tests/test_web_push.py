"""Opt-in Web Push (closed-PWA notifications, iOS-compatible) — issue #3196."""
import base64
import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from api import web_push

ROOT = Path(__file__).resolve().parent.parent
APPLE = "https://web.push.apple.com/QGxyz"
FCM = "https://fcm.googleapis.com/fcm/send/abc"
MOZILLA = "https://updates.push.services.mozilla.com/wpush/v2/abc"
PRIVATE_MARK = "PRIVATE-KEY-MATERIAL-SHOULD-NEVER-LEAK"
DEV_A = "deviceAAAAAAAAAAAAAAAAAAAAAAAA"
DEV_B = "deviceBBBBBBBBBBBBBBBBBBBBBBBB"
OWNER_A = web_push.owner_for_device(DEV_A)
OWNER_B = web_push.owner_for_device(DEV_B)


class _H:
    def __init__(self, dev=DEV_A, extra=None):
        self.headers = {web_push.DEVICE_HEADER: dev} if dev else {}
        self.headers.update(extra or {})


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _sub(endpoint=APPLE):
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": _b64(b"\x04" + b"\x01" * 64), "auth": _b64(b"\x02" * 16)},
    }


def _fake_dns(monkeypatch, mapping):
    def fake(host, port, *a, **kw):
        if host not in mapping:
            import socket

            raise socket.gaierror("nope")
        out = []
        for ip in mapping[host]:
            fam = 10 if ":" in ip else 2
            out.append((fam, 1, 6, "", (ip, port)))
        return out

    monkeypatch.setattr(web_push.socket, "getaddrinfo", fake)

def _enable(monkeypatch, tmp_path):
    (tmp_path / "webui_vapid.json").write_text(
        json.dumps({"public_key": "PUBKEY", "private_key": PRIVATE_MARK, "subject": "mailto:a@b.co"})
    )
    monkeypatch.setattr(web_push, "_pywebpush", lambda: (lambda **kw: None, Exception))


# ── opt-in / silent no-op ────────────────────────────────────────────────────

def test_disabled_without_keys_is_silent_noop(push_env, monkeypatch):
    monkeypatch.setattr(web_push, "_pywebpush", lambda: (lambda **kw: None, Exception))
    assert web_push.status()["enabled"] is False
    assert web_push.enqueue({"title": "x"}) is False
    assert web_push.notify_response_complete("sid", "hi") is False


def test_disabled_without_pywebpush(push_env, monkeypatch):
    _enable(monkeypatch, push_env)
    monkeypatch.setattr(web_push, "_pywebpush", lambda: (None, None))
    st = web_push.status()
    assert st == {"configured": True, "dependency_available": False, "enabled": False}


def test_env_overrides_file(push_env, monkeypatch):
    _enable(monkeypatch, push_env)
    monkeypatch.setenv("HERMES_WEBUI_VAPID_PUBLIC_KEY", "ENVPUB")
    monkeypatch.setenv("HERMES_WEBUI_VAPID_SUBJECT", "me@example.com")
    assert web_push.public_key() == "ENVPUB"
    assert web_push.subject() == "mailto:me@example.com"


# ── subscribe / unsubscribe ──────────────────────────────────────────────────

def test_subscribe_unsubscribe_roundtrip(push_env):
    web_push.add_subscription(_sub(APPLE))
    web_push.add_subscription(_sub(FCM))
    assert {s["endpoint"] for s in web_push.list_subscriptions()} == {APPLE, FCM}
    assert web_push.has_subscription(APPLE)
    assert web_push.remove_subscription(APPLE) is True
    assert web_push.remove_subscription(APPLE) is False
    assert [s["endpoint"] for s in web_push.list_subscriptions()] == [FCM]


def test_subscribe_is_idempotent_and_replaces_previous_endpoint(push_env):
    web_push.add_subscription(_sub(APPLE))
    web_push.add_subscription(_sub(APPLE))
    assert web_push.subscription_count() == 1
    web_push.add_subscription(_sub(MOZILLA), previous_endpoint=APPLE)
    assert [s["endpoint"] for s in web_push.list_subscriptions()] == [MOZILLA]


def test_store_is_private_file(push_env):
    web_push.add_subscription(_sub())
    mode = stat.S_IMODE(os.stat(push_env / "webui_push_subscriptions.json").st_mode)
    assert mode == 0o600


def test_subscription_limit(push_env, monkeypatch):
    monkeypatch.setattr(web_push, "_MAX_SUBSCRIPTIONS", 2)
    web_push.add_subscription(_sub(APPLE))
    web_push.add_subscription(_sub(FCM))
    with pytest.raises(ValueError, match="too many"):
        web_push.add_subscription(_sub(MOZILLA))


@pytest.mark.parametrize(
    "keys",
    [
        {},
        {"p256dh": "AAAA", "auth": "AAAA"},
        {"p256dh": _b64(b"\x04" + b"\x01" * 64), "auth": _b64(b"\x02" * 8)},
        {"p256dh": _b64(b"\x03" + b"\x01" * 64), "auth": _b64(b"\x02" * 16)},
        {"p256dh": "***", "auth": _b64(b"\x02" * 16)},
    ],
)
def test_bad_keys_rejected(push_env, keys):
    with pytest.raises(ValueError):
        web_push.add_subscription({"endpoint": APPLE, "keys": keys})


def test_corrupt_store_fails_closed_and_is_not_overwritten(push_env):
    path = push_env / "webui_push_subscriptions.json"
    path.write_text("{not json")
    with pytest.raises(web_push.PushStoreUnavailable):
        web_push.add_subscription(_sub())
    with pytest.raises(web_push.PushStoreUnavailable):
        web_push.list_subscriptions()
    assert path.read_text() == "{not json"
    assert web_push._send_to_all({"title": "x"}) == 0


# ── SSRF guard ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("endpoint", [APPLE, FCM, MOZILLA])
def test_real_push_services_allowed(push_env, endpoint):
    assert web_push.validate_endpoint(endpoint) == endpoint


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://web.push.apple.com/x",
        "ftp://web.push.apple.com/x",
        "https://user:pw@web.push.apple.com/x",
        "https://localhost/x",
        "https://foo.localhost/x",
        "https://127.0.0.1/x",
        "https://[::1]/x",
        "https://10.1.2.3/x",
        "https://192.168.0.9/x",
        "https://169.254.169.254/latest/meta-data",
        "https://100.64.0.1/x",
        "https://100.127.255.254/x",
        "https://[::ffff:127.0.0.1]/x",
        "https://[::ffff:100.64.0.1]/x",
        "https://rebind.example/x",
        "https://mapped.example/x",
        "https://cgnat.example/x",
        "https://ts.example/x",
        "https://mixed.example/x",
        "https://unresolvable.example/x",
        "",
        "https://" + "a" * 2100,
    ],
)
def test_unsafe_endpoints_rejected(push_env, endpoint):
    with pytest.raises(ValueError):
        web_push.add_subscription(_sub(endpoint))
    assert web_push.subscription_count() == 0


def test_delivery_rechecks_endpoint_and_skips_rebound_dns(push_env, monkeypatch):
    """Stored endpoint that later resolves to a private IP must not be contacted."""
    _enable(monkeypatch, push_env)
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    calls = []
    monkeypatch.setattr(web_push, "_pywebpush", lambda: (lambda **kw: calls.append(kw), Exception))
    _fake_dns(monkeypatch, {"web.push.apple.com": ["10.0.0.9"]})
    assert web_push._send_to_all({"title": "x", "_owners": [OWNER_A]}) == 0
    assert calls == []


def test_pinned_session_refuses_redirects_proxies_and_http(push_env):
    session = web_push._pinned_requests_session(APPLE)
    assert session.trust_env is False
    with pytest.raises(ValueError):
        session.get("http://web.push.apple.com/x")
    adapter = session.get_adapter("https://web.push.apple.com/")
    with pytest.raises(ValueError):
        adapter.proxy_manager_for("http://proxy:1")


# ── delivery ─────────────────────────────────────────────────────────────────

def test_send_passes_pinned_session_and_vapid_and_prunes_410(push_env, monkeypatch):
    _enable(monkeypatch, push_env)
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    web_push.add_subscription(_sub(FCM), owner=OWNER_A)
    seen = []

    class Gone(Exception):
        response = type("R", (), {"status_code": 410})()

    def fake_webpush(**kw):
        seen.append(kw)
        if kw["subscription_info"]["endpoint"] == FCM:
            raise Gone()

    monkeypatch.setattr(web_push, "_pywebpush", lambda: (fake_webpush, Exception))
    sent = web_push._send_to_all(web_push.notification_payload("T", "B", session_id="s1", owners=[OWNER_A]))
    assert sent == 1 and len(seen) == 2
    for kw in seen:
        assert kw["vapid_claims"] == {"sub": "mailto:a@b.co"}
        assert kw["requests_session"].trust_env is False
        assert json.loads(kw["data"])["options"]["data"]["url"] == "session/s1"
    assert [s["endpoint"] for s in web_push.list_subscriptions()] == [APPLE]


def test_delivery_is_off_thread_and_enqueue_never_blocks(push_env, monkeypatch):
    _enable(monkeypatch, push_env)
    started, release = threading.Event(), threading.Event()
    threads = []

    def slow_send(payload):
        threads.append(threading.current_thread())
        started.set()
        release.wait(5)
        return 0

    monkeypatch.setattr(web_push, "_send_to_all", slow_send)
    monkeypatch.setattr(web_push, "_WORKERS", [])
    monkeypatch.setattr(web_push, "_STOP", threading.Event())
    import queue as _q

    monkeypatch.setattr(web_push, "_QUEUE", _q.Queue(maxsize=3))
    web_push.register_session_owner("sid", OWNER_A)
    try:
        t0 = time.monotonic()
        assert web_push.notify_response_complete("sid", "done") is True
        assert time.monotonic() - t0 < 0.5
        assert started.wait(3)
        assert threads[0] is not threading.current_thread()
        assert threads[0].daemon and threads[0].name.startswith("web-push-")
        # Workers are blocked: flood the bounded queue. Producers must never block.
        t0 = time.monotonic()
        results = [web_push.enqueue({"i": i}) for i in range(50)]
        assert time.monotonic() - t0 < 0.5
        assert results.count(True) <= 3 + web_push._MAX_WORKERS
        assert False in results  # overflow dropped, not blocked
    finally:
        release.set()
        web_push._STOP.set()


def test_approval_and_clarify_deduped(push_env, monkeypatch):
    _enable(monkeypatch, push_env)
    queued = []
    monkeypatch.setattr(web_push, "enqueue", lambda p: queued.append(p) or True)
    monkeypatch.setattr(web_push, "_SEEN", {})
    for sid in ("s",):
        web_push.register_session_owner(sid, OWNER_A)
    a = {"approval_id": "a1", "description": "rm -rf /tmp/x"}
    assert web_push.notify_approval_required("s", a) is True
    assert web_push.notify_approval_required("s", a) is False
    assert web_push.notify_clarify_required("s", {"clarify_id": "c1", "question": "Which?"}) is True
    assert web_push.notify_clarify_required("s", {"clarify_id": "c1", "question": "Which?"}) is False
    assert [q["title"] for q in queued] == ["Approval required", "Clarification needed"]


def test_session_done_uses_last_assistant_text(push_env, monkeypatch):
    queued = []
    monkeypatch.setattr(web_push, "is_enabled", lambda: True)
    monkeypatch.setattr(web_push, "enqueue", lambda p: queued.append(p) or True)
    web_push.register_session_owner("sid/1", OWNER_A)
    msgs = [
        {"role": "assistant", "content": "old"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [{"type": "text", "text": "final   answer"}]},
    ]
    web_push.notify_session_done("sid/1", msgs)
    assert queued[0]["options"]["body"] == "final answer"
    assert queued[0]["options"]["data"]["url"] == "session/sid%2F1"


# ── key / secret non-disclosure via the HTTP handlers ────────────────────────

class _Capture:
    def __init__(self, monkeypatch):
        from api import routes

        self.out = []
        monkeypatch.setattr(routes, "j", lambda h, payload, status=200, **kw: self.out.append((status, payload)) or True)
        monkeypatch.setattr(routes, "bad", lambda h, msg, status=400: self.out.append((status, {"error": msg})) or True)
        self.routes = routes


def test_handlers_never_disclose_private_key_or_subscriptions(push_env, monkeypatch):
    from urllib.parse import urlparse

    _enable(monkeypatch, push_env)
    cap = _Capture(monkeypatch)
    r = cap.routes
    r._handle_push_status(_H(), urlparse("/api/push/status"))
    r._handle_push_vapid_public_key(_H())
    r._handle_push_subscribe(_H(), {"subscription": _sub(APPLE)})
    r._handle_push_status(_H(), urlparse("/api/push/status?endpoint=" + APPLE))
    r._handle_push_unsubscribe(_H(), {"endpoint": APPLE})
    r._handle_push_subscribe(_H(), {"subscription": _sub("https://127.0.0.1/x")})
    blob = json.dumps(cap.out)
    assert PRIVATE_MARK not in blob
    assert "private" not in blob.lower()
    assert cap.out[0][1] == {"configured": True, "dependency_available": True, "enabled": True, "subscribed": False}
    assert cap.out[1] == (200, {"public_key": "PUBKEY"})
    assert cap.out[3][1]["subscribed"] is True
    assert cap.out[4][1] == {"ok": True, "removed": True}
    assert cap.out[5][0] == 400


def test_handlers_404_when_not_configured(push_env, monkeypatch):
    cap = _Capture(monkeypatch)
    r = cap.routes
    r._handle_push_vapid_public_key(_H())
    r._handle_push_subscribe(_H(), {"subscription": _sub()})
    r._handle_push_test(_H())
    assert [s for s, _ in cap.out] == [404, 404, 404]


def test_corrupt_store_gives_503_from_handlers(push_env, monkeypatch):
    from urllib.parse import urlparse

    _enable(monkeypatch, push_env)
    (push_env / "webui_push_subscriptions.json").write_text("garbage")
    cap = _Capture(monkeypatch)
    r = cap.routes
    r._handle_push_status(_H(), urlparse("/api/push/status?endpoint=" + APPLE))
    r._handle_push_subscribe(_H(), {"subscription": _sub()})
    r._handle_push_test(_H())
    assert [s for s, _ in cap.out] == [503, 503, 503]


def test_push_routes_are_not_public():
    from api.auth import PUBLIC_PATHS

    assert not any(p.startswith("/api/push") for p in PUBLIC_PATHS)


def test_vapid_generator_writes_private_file_and_never_prints_private(tmp_path, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("gen_vapid", ROOT / "scripts" / "generate_vapid_keys.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["--state-dir", str(tmp_path), "--subject", "mailto:a@b.co"]) == 0
    data = json.loads((tmp_path / "webui_vapid.json").read_text())
    shown = capsys.readouterr().out
    assert data["private_key"] not in shown
    assert data["public_key"] in shown
    assert stat.S_IMODE(os.stat(tmp_path / "webui_vapid.json").st_mode) == 0o600
    assert len(base64.urlsafe_b64decode(data["public_key"] + "==")) == 65
    assert mod.main(["--state-dir", str(tmp_path), "--subject", "mailto:a@b.co"]) == 1  # no clobber


# ── static wiring ────────────────────────────────────────────────────────────

def test_service_worker_has_push_and_notificationclick():
    sw = (ROOT / "static" / "sw.js").read_text()
    assert "addEventListener('push'" in sw
    assert "addEventListener('notificationclick'" in sw
    push_block = sw[sw.index("addEventListener('push'"):]
    assert "showNotification" in push_block and "event.waitUntil" in push_block


def test_producers_are_wired():
    src = {n: (ROOT / "api" / n).read_text() for n in (
        "streaming.py", "gateway_chat.py", "route_approvals.py", "clarify.py", "background_process.py")}
    assert "notify_session_done" in src["streaming.py"]
    assert "notify_session_done" in src["gateway_chat.py"]
    assert "notify_approval_required" in src["route_approvals.py"]
    assert "notify_clarify_required" in src["clarify.py"]
    assert "notify_bg_task_complete" in src["background_process.py"]
    assert "atexit.register(shutdown" in (ROOT / "api" / "web_push.py").read_text()


def test_settings_ui_opt_in_wiring():
    html = (ROOT / "static" / "index.html").read_text()
    js = (ROOT / "static" / "panels.js").read_text()
    assert 'id="webPushSettings"' in html and "display:none" in html[html.index('id="webPushSettings"'):][:80]
    for needle in ("userVisibleOnly:true", "/api/push/vapid-public-key", "/api/push/subscribe", "updateWebPushStatus"):
        assert needle in js
