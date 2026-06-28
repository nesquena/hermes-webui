"""Regression coverage for the fixed llm-proxy quota-stats extension bridge."""

from __future__ import annotations

import json
from contextlib import nullcontext
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import api.providers as providers
import api.routes as routes


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _set_llm_proxy_config(monkeypatch, *, base_url: str | None, api_key: str | None):
    monkeypatch.setattr(
        providers,
        "get_config",
        lambda: {
            "providers": {
                "llm-proxy": {
                    **({"base_url": base_url} if base_url is not None else {}),
                }
            }
        },
    )
    monkeypatch.setattr(
        providers,
        "_get_provider_api_key",
        lambda provider_id: api_key if provider_id == "llm-proxy" else None,
    )


def test_get_quota_stats_forwards_provider_query_and_bearer_auth(monkeypatch):
    _set_llm_proxy_config(
        monkeypatch,
        base_url="https://llm-proxy.example.test",
        api_key="server-held-secret",
    )
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["authorization"] = req.headers.get("Authorization")
        seen["accept"] = req.headers.get("Accept")
        seen["data"] = req.data
        seen["timeout"] = timeout
        return _FakeResponse(json.dumps({"ok": True, "quota": {"limit_remaining": 7}}).encode("utf-8"))

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)

    status, payload = providers.get_llm_proxy_quota_stats(query={"provider": ["anthropic"]})

    assert status == 200
    assert payload == {"ok": True, "quota": {"limit_remaining": 7}}
    assert seen == {
        "url": "https://llm-proxy.example.test/v1/quota-stats?provider=anthropic",
        "method": "GET",
        "authorization": "Bearer server-held-secret",
        "accept": "application/json",
        "data": None,
        "timeout": 3.0,
    }


def test_post_quota_stats_forwards_validated_body(monkeypatch):
    _set_llm_proxy_config(
        monkeypatch,
        base_url="https://llm-proxy.example.test/api",
        api_key="server-held-secret",
    )
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["authorization"] = req.headers.get("Authorization")
        seen["body"] = json.loads(req.data.decode("utf-8"))
        seen["timeout"] = timeout
        return _FakeResponse(json.dumps({"quota": {"limit_remaining": 6}, "reload": True}).encode("utf-8"))

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)

    status, payload = providers.get_llm_proxy_quota_stats(
        body={
            "action": "force-refresh",
            "scope": "provider",
            "provider": "anthropic",
            "credential": "primary",
        }
    )

    assert status == 200
    assert payload == {"quota": {"limit_remaining": 6}, "reload": True}
    assert seen == {
        "url": "https://llm-proxy.example.test/api/v1/quota-stats",
        "method": "POST",
        "authorization": "Bearer server-held-secret",
        "body": {
            "action": "force-refresh",
            "scope": "provider",
            "provider": "anthropic",
            "credential": "primary",
        },
        "timeout": 3.0,
    }


def test_post_quota_stats_rejects_invalid_action_or_scope_without_network(monkeypatch):
    _set_llm_proxy_config(
        monkeypatch,
        base_url="https://llm-proxy.example.test",
        api_key="server-held-secret",
    )
    called = False

    def explode(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("network should not be reached for invalid llm-proxy quota requests")

    monkeypatch.setattr(providers.urllib.request, "urlopen", explode)

    for body in (
        {"action": "", "scope": "provider"},
        {"action": "force refresh", "scope": "provider"},
        {"action": "force-refresh", "scope": ""},
        {"action": "force-refresh", "scope": "provider scope"},
    ):
        status, payload = providers.get_llm_proxy_quota_stats(body=body)
        assert status == 400
        assert payload["ok"] is False
        assert payload["error"] == "llm_proxy_quota_stats_invalid_request"
        assert payload["message"]

    assert called is False


def test_post_quota_stats_null_body_fails_before_network(monkeypatch):
    _set_llm_proxy_config(
        monkeypatch,
        base_url="https://llm-proxy.example.test",
        api_key="server-held-secret",
    )
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: None)
    monkeypatch.setattr(routes, "bad", lambda handler, message, status=400: {
        "message": message,
        "status": status,
    })
    called = False

    def explode(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("network should not be reached for null llm-proxy quota requests")

    monkeypatch.setattr(providers.urllib.request, "urlopen", explode)

    result = routes.handle_post(
        SimpleNamespace(headers={}, rfile=BytesIO()),
        SimpleNamespace(path="/api/extensions/proxies/llm-proxy/quota-stats"),
    )

    assert result == {
        "message": "Invalid llm-proxy quota-stats request.",
        "status": 400,
    }
    assert called is False


def test_quota_stats_route_reports_unconfigured_proxy_without_secret_leak(monkeypatch):
    monkeypatch.setattr(
        providers,
        "get_config",
        lambda: {"providers": {"llm-proxy": {}}},
    )
    monkeypatch.setattr(
        providers,
        "_get_provider_api_key",
        lambda provider_id: "server-held-secret" if provider_id == "llm-proxy" else None,
    )
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200, headers=None: {
        "payload": payload,
        "status": status,
    })
    monkeypatch.setattr(
        "api.profiles.profile_env_for_active_request_readonly",
        lambda *_args, **_kwargs: nullcontext(),
    )

    result = routes.handle_get(
        SimpleNamespace(),
        SimpleNamespace(path="/api/extensions/proxies/llm-proxy/quota-stats", query="provider=anthropic"),
    )

    assert result == {
        "payload": {
            "ok": False,
            "error": "llm_proxy_quota_stats_unconfigured",
            "message": "llm-proxy quota stats is not configured.",
        },
        "status": 503,
    }
    assert "server-held-secret" not in repr(result)


def test_non_allowlisted_extension_proxy_paths_remain_unavailable(monkeypatch):
    monkeypatch.setattr(routes, "read_body", lambda handler: {})
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)

    assert routes.handle_get(
        SimpleNamespace(),
        SimpleNamespace(path="/api/extensions/proxies/llm-proxy"),
    ) is False
    assert routes.handle_get(
        SimpleNamespace(),
        SimpleNamespace(path="/api/extensions/proxies/llm-proxy/quota-stats/extra"),
    ) is False
    assert routes.handle_post(
        SimpleNamespace(headers={}, rfile=BytesIO()),
        SimpleNamespace(path="/api/extensions/proxies/llm-proxy"),
    ) is False
    assert routes.handle_post(
        SimpleNamespace(headers={}, rfile=BytesIO()),
        SimpleNamespace(path="/api/extensions/proxies/llm-proxy/quota-stats/extra"),
    ) is False


def test_extensions_docs_describe_fixed_quota_stats_bridge_only():
    docs = Path(__file__).resolve().parents[1].joinpath("docs", "EXTENSIONS.md").read_text(encoding="utf-8")

    assert "/api/extensions/proxies/llm-proxy/quota-stats" in docs
    assert "optional `provider` query parameter" in docs
    assert "validated `action` and `scope` fields" in docs
    assert "server-side" in docs
    assert "/v1/quota-stats" in docs
    assert "Generic extension proxying, caller-chosen hosts, and caller-chosen paths stay out of scope." in docs
