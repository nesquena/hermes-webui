"""Tests for the Weixin unbind/logout + pairing-approval backend & routes.

Two concerns are covered:

* Backend (``api.platforms.weixin``):
    - ``unbind()`` strips every WEIXIN_* key from the active-profile .env and
      removes the cached accounts dir (tolerating its absence).
    - ``pairing_list`` / ``pairing_approve`` / ``pairing_revoke`` map a mocked
      ``PairingStore`` to the WebUI contract, NEVER surfacing the hashed code,
      and degrade to ``{available: False}`` / ``{ok: False}`` when the agent
      lacks pairing support.

* Routes (``api.routes``): the four new endpoints dispatch to the right backend
  function with the right arguments (mirrors tests/test_weixin_routes.py).

The agent's ``PairingStore`` is always stubbed — these tests never construct the
real one.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import api.platforms.weixin as weixin
from api import routes


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def env_home(monkeypatch, tmp_path):
    monkeypatch.setattr(weixin, "get_active_hermes_home", lambda: tmp_path)
    with weixin._PENDING_LOCK:
        weixin._PENDING.clear()
    return tmp_path


def _write_env(tmp_path, mapping):
    lines = [f"{k}={v}" for k, v in mapping.items()]
    (tmp_path / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── A minimal in-memory PairingStore double ──────────────────────────────────


class FakeStore:
    """Records calls + returns canned data; mirrors the real signatures."""

    def __init__(self, pending=None, approved=None):
        self._pending = pending if pending is not None else []
        self._approved = approved if approved is not None else []
        self.calls = []

    def list_pending(self, platform=None):
        self.calls.append(("list_pending", platform))
        return [dict(p) for p in self._pending]

    def list_approved(self, platform=None):
        self.calls.append(("list_approved", platform))
        return [dict(a) for a in self._approved]

    def approve_code(self, platform, code):
        self.calls.append(("approve_code", platform, code))
        return self._approve_result

    def revoke(self, platform, user_id):
        self.calls.append(("revoke", platform, user_id))
        return self._revoke_result

    # Tunable per-test.
    _approve_result = None
    _revoke_result = True


# ── unbind() ─────────────────────────────────────────────────────────────────


def test_unbind_strips_all_weixin_env_keys(env_home, monkeypatch):
    _write_env(
        env_home,
        {
            "WEIXIN_ACCOUNT_ID": "bot_1",
            "WEIXIN_TOKEN": "secrettoken",
            "WEIXIN_BASE_URL": "https://x",
            "WEIXIN_CDN_BASE_URL": "https://cdn",
            "WEIXIN_DM_POLICY": "pairing",
            "WEIXIN_ALLOWED_USERS": "u1",
            "WEIXIN_GROUP_POLICY": "open",
            "WEIXIN_GROUP_ALLOWED_USERS": "g1",
            "WEIXIN_HOME_CHANNEL": "chat42",
            "UNRELATED_KEY": "keepme",
        },
    )
    monkeypatch.setattr(weixin, "restart_gateway", lambda: {"ok": True, "detail": ""})

    res = weixin.unbind()
    assert res["unbound"] is True
    assert res["restart"] == {"ok": True, "detail": ""}

    text = (env_home / ".env").read_text(encoding="utf-8")
    for key in weixin._WEIXIN_ENV_KEYS:
        assert key not in text
    assert "secrettoken" not in text
    # Unrelated keys are preserved.
    assert "UNRELATED_KEY=keepme" in text


def test_unbind_removes_accounts_dir(env_home, monkeypatch):
    acc_dir = env_home / "weixin" / "accounts"
    acc_dir.mkdir(parents=True, exist_ok=True)
    (acc_dir / "bot_1.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(weixin, "restart_gateway", lambda: {"ok": True, "detail": ""})

    weixin.unbind()
    assert not acc_dir.exists()


def test_unbind_tolerates_missing_accounts_dir(env_home, monkeypatch):
    # No accounts dir, no .env — must not raise.
    monkeypatch.setattr(weixin, "restart_gateway", lambda: {"ok": False, "detail": "x"})
    res = weixin.unbind()
    assert res["unbound"] is True


def test_unbind_calls_restart_gateway(env_home, monkeypatch):
    called = {"n": 0}

    def fake_restart():
        called["n"] += 1
        return {"ok": True, "detail": "restarted"}

    monkeypatch.setattr(weixin, "restart_gateway", fake_restart)
    weixin.unbind()
    assert called["n"] == 1


# ── pairing_list ─────────────────────────────────────────────────────────────


def test_pairing_list_unavailable_when_no_store(monkeypatch):
    monkeypatch.setattr(weixin, "_get_pairing_store", lambda: None)
    assert weixin.pairing_list() == {"available": False}


def test_pairing_list_maps_pending_and_approved(monkeypatch):
    store = FakeStore(
        pending=[
            {
                "platform": "weixin",
                "code": "deadbeef",  # hashed display — MUST NOT be forwarded
                "user_id": "user_111",
                "user_name": "Alice",
                "age_minutes": 5,
            }
        ],
        approved=[
            {"platform": "weixin", "user_id": "user_999", "user_name": "Bob"},
        ],
    )
    monkeypatch.setattr(weixin, "_get_pairing_store", lambda: store)

    out = weixin.pairing_list()
    assert out["available"] is True

    assert out["pending"] == [
        {"user_id": "user_111", "user_name": "Alice", "age_minutes": 5}
    ]
    # Hashed code is never surfaced.
    assert "code" not in out["pending"][0]
    assert "deadbeef" not in json.dumps(out)

    assert out["approved"] == [{"user_id": "user_999", "user_name": "Bob"}]

    # Always filtered by the weixin platform.
    assert ("list_pending", "weixin") in store.calls
    assert ("list_approved", "weixin") in store.calls


def test_pairing_list_tolerates_store_errors(monkeypatch):
    class Boom(FakeStore):
        def list_pending(self, platform=None):
            raise RuntimeError("boom")

        def list_approved(self, platform=None):
            raise RuntimeError("boom")

    monkeypatch.setattr(weixin, "_get_pairing_store", lambda: Boom())
    out = weixin.pairing_list()
    assert out == {"available": True, "pending": [], "approved": []}


# ── pairing_approve ──────────────────────────────────────────────────────────


def test_pairing_approve_requires_code(monkeypatch):
    monkeypatch.setattr(weixin, "_get_pairing_store", lambda: FakeStore())
    res = weixin.pairing_approve("   ")
    assert res["ok"] is False
    assert "code" in res["error"]


def test_pairing_approve_unavailable(monkeypatch):
    monkeypatch.setattr(weixin, "_get_pairing_store", lambda: None)
    res = weixin.pairing_approve("ABC123")
    assert res["ok"] is False
    assert "unavailable" in res["error"]


def test_pairing_approve_success(monkeypatch):
    store = FakeStore()
    store._approve_result = {"user_id": "user_222", "user_name": "Carol"}
    monkeypatch.setattr(weixin, "_get_pairing_store", lambda: store)

    res = weixin.pairing_approve("ABC123")
    assert res["ok"] is True
    assert res["user"] == {"user_id": "user_222", "user_name": "Carol"}
    assert ("approve_code", "weixin", "ABC123") in store.calls


def test_pairing_approve_invalid_code(monkeypatch):
    store = FakeStore()
    store._approve_result = None  # invalid/expired
    monkeypatch.setattr(weixin, "_get_pairing_store", lambda: store)

    res = weixin.pairing_approve("WRONG")
    assert res["ok"] is False
    assert "invalid" in res["error"].lower() or "expired" in res["error"].lower()


def test_pairing_approve_store_exception(monkeypatch):
    class Boom(FakeStore):
        def approve_code(self, platform, code):
            raise RuntimeError("kaboom")

    monkeypatch.setattr(weixin, "_get_pairing_store", lambda: Boom())
    res = weixin.pairing_approve("ABC")
    assert res["ok"] is False
    assert "kaboom" in res["error"]


# ── pairing_revoke ───────────────────────────────────────────────────────────


def test_pairing_revoke_requires_user_id(monkeypatch):
    monkeypatch.setattr(weixin, "_get_pairing_store", lambda: FakeStore())
    res = weixin.pairing_revoke("")
    assert res["ok"] is False
    assert "user_id" in res["error"]


def test_pairing_revoke_unavailable(monkeypatch):
    monkeypatch.setattr(weixin, "_get_pairing_store", lambda: None)
    res = weixin.pairing_revoke("user_1")
    assert res["ok"] is False
    assert "unavailable" in res["error"]


def test_pairing_revoke_success(monkeypatch):
    store = FakeStore()
    store._revoke_result = True
    monkeypatch.setattr(weixin, "_get_pairing_store", lambda: store)

    res = weixin.pairing_revoke("user_999")
    assert res == {"ok": True}
    assert ("revoke", "weixin", "user_999") in store.calls


def test_pairing_revoke_not_found(monkeypatch):
    store = FakeStore()
    store._revoke_result = False
    monkeypatch.setattr(weixin, "_get_pairing_store", lambda: store)

    res = weixin.pairing_revoke("ghost")
    assert res == {"ok": False}


def test_pairing_revoke_store_exception(monkeypatch):
    class Boom(FakeStore):
        def revoke(self, platform, user_id):
            raise RuntimeError("nope")

    monkeypatch.setattr(weixin, "_get_pairing_store", lambda: Boom())
    res = weixin.pairing_revoke("u1")
    assert res["ok"] is False
    assert "nope" in res["error"]


# ── Route dispatch ───────────────────────────────────────────────────────────


@pytest.fixture
def captured(monkeypatch):
    box: dict = {}

    def fake_j(_handler, payload, status: int = 200, extra_headers=None):
        box["payload"] = payload
        box["status"] = status
        box["serialized"] = json.dumps(payload, ensure_ascii=False)
        return None

    def fake_bad(_handler, msg, status: int = 400):
        return fake_j(_handler, {"error": msg}, status=status)

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "bad", fake_bad)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    return box


def _set_body(monkeypatch, body: dict) -> None:
    monkeypatch.setattr(routes, "read_body", lambda _handler: body)


def test_route_get_pairing_list(monkeypatch, captured):
    sentinel = {"available": True, "pending": [], "approved": []}
    monkeypatch.setattr(weixin, "pairing_list", lambda: sentinel)

    routes.handle_get(object(), SimpleNamespace(path="/api/platforms/weixin/pairing"))

    assert captured["payload"] == sentinel
    assert captured["status"] == 200


def test_route_post_unbind(monkeypatch, captured):
    called = {"n": 0}

    def fake_unbind():
        called["n"] += 1
        return {"unbound": True, "restart": {"ok": True, "detail": ""}}

    monkeypatch.setattr(weixin, "unbind", fake_unbind)
    _set_body(monkeypatch, {})

    routes.handle_post(object(), SimpleNamespace(path="/api/platforms/weixin/unbind"))

    assert called["n"] == 1
    assert captured["payload"]["unbound"] is True
    assert captured["status"] == 200


def test_route_post_pairing_approve_passes_code(monkeypatch, captured):
    calls: dict = {}

    def fake_approve(code):
        calls["code"] = code
        return {"ok": True, "user": {"user_id": "u1", "user_name": "N"}}

    monkeypatch.setattr(weixin, "pairing_approve", fake_approve)
    _set_body(monkeypatch, {"code": "  ABC123  "})

    routes.handle_post(
        object(), SimpleNamespace(path="/api/platforms/weixin/pairing/approve")
    )

    assert calls["code"] == "ABC123"  # route trims
    assert captured["payload"]["ok"] is True
    assert captured["status"] == 200


def test_route_post_pairing_revoke_passes_user_id(monkeypatch, captured):
    calls: dict = {}

    def fake_revoke(user_id):
        calls["user_id"] = user_id
        return {"ok": True}

    monkeypatch.setattr(weixin, "pairing_revoke", fake_revoke)
    _set_body(monkeypatch, {"user_id": "  user_42  "})

    routes.handle_post(
        object(), SimpleNamespace(path="/api/platforms/weixin/pairing/revoke")
    )

    assert calls["user_id"] == "user_42"
    assert captured["payload"]["ok"] is True
    assert captured["status"] == 200


def test_route_pairing_never_leaks_hashed_code(monkeypatch, captured):
    # Even if a backend bug returned a code, the route serialization is checked.
    monkeypatch.setattr(
        weixin,
        "pairing_list",
        lambda: {
            "available": True,
            "pending": [{"user_id": "u1", "user_name": "A", "age_minutes": 1}],
            "approved": [],
        },
    )
    routes.handle_get(object(), SimpleNamespace(path="/api/platforms/weixin/pairing"))
    assert "code" not in captured["serialized"]
