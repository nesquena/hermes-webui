"""Regression coverage for active-provider quota status (#706)."""

from __future__ import annotations

import base64
import json
import inspect
import os
import re
import sys
import threading
import types
import urllib.error
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import api.config as config
import api.profiles as profiles

ROOT = Path(__file__).resolve().parents[1]


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _with_config(model=None, providers=None):
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = model or {}
    if providers is not None:
        config.cfg["providers"] = providers
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    return old_cfg, old_mtime


def _restore_config(old_cfg, old_mtime):
    config.cfg.clear()
    config.cfg.update(old_cfg)
    config._cfg_mtime = old_mtime


def test_openrouter_quota_fetches_key_endpoint_and_sanitizes_response(monkeypatch, tmp_path):
    """OpenRouter's documented key endpoint should be called server-side only."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=test-openrouter-key-private\n", encoding="utf-8")
    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        seen["authorization"] = req.headers.get("Authorization")
        payload = {"data": {"limit_remaining": "12.5", "usage": 3, "limit": 20, "key": "must-not-leak"}}
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    try:
        result = providers.get_provider_quota()
    finally:
        _restore_config(old_cfg, old_mtime)

    assert seen == {
        "url": "https://openrouter.ai/api/v1/key",
        "timeout": 3.0,
        "authorization": "Bearer test-openrouter-key-private",
    }
    assert result == {
        "ok": True,
        "provider": "openrouter",
        "display_name": "OpenRouter",
        "supported": True,
        "status": "available",
        "label": "OpenRouter credits",
        "quota": {"limit_remaining": 12.5, "usage": 3, "limit": 20},
        "message": "OpenRouter quota status loaded.",
    }
    assert "test-openrouter-key-private" not in repr(result)
    assert "must-not-leak" not in repr(result)


def test_openrouter_quota_no_key_returns_safe_no_key_without_network(monkeypatch, tmp_path):
    """No-key state must not call OpenRouter or leak environment details."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers

    def explode(*_args, **_kwargs):
        raise AssertionError("quota lookup should not call the network without a key")

    monkeypatch.setattr(providers.urllib.request, "urlopen", explode)
    try:
        result = providers.get_provider_quota()
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["ok"] is False
    assert result["provider"] == "openrouter"
    assert result["supported"] is True
    assert result["status"] == "no_key"
    assert result["quota"] is None
    assert "OPENROUTER_API_KEY" in result["message"]


def test_openrouter_quota_invalid_key_and_timeout_are_sanitized(monkeypatch, tmp_path):
    """Invalid-key and timeout/error paths should expose statuses, not secrets."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=test-openrouter-key-private\n", encoding="utf-8")
    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers

    req = providers.urllib.request.Request("https://openrouter.ai/api/v1/key")
    invalid = urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, BytesIO(b"secret body"))
    errors = [invalid, TimeoutError("slow secret")]

    try:
        for expected in ("invalid_key", "unavailable"):
            def fake_urlopen(_req, timeout=None, *, _err=errors.pop(0)):
                raise _err

            monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
            result = providers.get_provider_quota("openrouter")
            assert result["ok"] is False
            assert result["status"] == expected
            assert result["quota"] is None
            assert "test-openrouter-key-private" not in repr(result)
            assert "secret" not in repr(result).lower()
    finally:
        _restore_config(old_cfg, old_mtime)


def test_unsupported_provider_reports_followup_state(monkeypatch, tmp_path):
    """Providers without safe quota APIs should return a clear unsupported state."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    old_cfg, old_mtime = _with_config(model={"provider": "openai"})

    import api.providers as providers
    try:
        result = providers.get_provider_quota()
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["ok"] is False
    assert result["provider"] == "openai"
    assert result["supported"] is False
    assert result["status"] == "unsupported"
    assert result["quota"] is None
    assert "follow-up" in result["message"]


def test_codex_account_usage_is_fetched_under_active_profile_home(monkeypatch, tmp_path):
    """Codex account limits must use the selected WebUI profile's HERMES_HOME."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    old_cfg, old_mtime = _with_config(model={"provider": "openai-codex"})

    import api.providers as providers
    seen = {}
    previous_home = os.environ.get("HERMES_HOME")

    def fake_fetch(provider, home, api_key=None):
        seen["provider"] = provider
        seen["home"] = str(home)
        seen["api_key"] = api_key
        return SimpleNamespace(
            provider="openai-codex",
            source="usage_api",
            title="Account limits",
            plan="Pro",
            fetched_at=datetime(2030, 3, 17, 12, 30, tzinfo=timezone.utc),
            available=True,
            windows=(
                SimpleNamespace(
                    label="Session",
                    used_percent=15.0,
                    reset_at=datetime(2030, 3, 17, 17, 30, tzinfo=timezone.utc),
                    detail=None,
                ),
                SimpleNamespace(
                    label="Weekly",
                    used_percent=40.0,
                    reset_at=datetime(2030, 3, 24, 12, 30, tzinfo=timezone.utc),
                    detail=None,
                ),
            ),
            details=("Credits balance: $12.50",),
            unavailable_reason=None,
        )

    monkeypatch.setattr(providers, "_agent_fetch_account_usage_for_home", fake_fetch)
    try:
        result = providers.get_provider_quota()
    finally:
        _restore_config(old_cfg, old_mtime)

    assert seen == {
        "provider": "openai-codex",
        "home": str(tmp_path),
        "api_key": None,
    }
    assert os.environ.get("HERMES_HOME") == previous_home
    assert result["ok"] is True
    assert result["provider"] == "openai-codex"
    assert result["supported"] is True
    assert result["status"] == "available"
    assert result["quota"] is None
    assert result["account_limits"] == {
        "provider": "openai-codex",
        "source": "usage_api",
        "title": "Account limits",
        "plan": "Pro",
        "windows": [
            {
                "label": "Session",
                "used_percent": 15.0,
                "remaining_percent": 85.0,
                "reset_at": "2030-03-17T17:30:00Z",
                "detail": None,
            },
            {
                "label": "Weekly",
                "used_percent": 40.0,
                "remaining_percent": 60.0,
                "reset_at": "2030-03-24T12:30:00Z",
                "detail": None,
            },
        ],
        "details": ["Credits balance: $12.50"],
        "available": True,
        "unavailable_reason": None,
        "fetched_at": "2030-03-17T12:30:00Z",
    }


def test_codex_account_usage_unavailable_is_sanitized(monkeypatch, tmp_path):
    """Auth/network failures should not leak raw token or exception details."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    old_cfg, old_mtime = _with_config(model={"provider": "openai-codex"})

    import api.providers as providers

    def fake_fetch(*_args, **_kwargs):
        raise RuntimeError("secret access token should not leak")

    monkeypatch.setattr(providers, "_agent_fetch_account_usage_for_home", fake_fetch)
    try:
        result = providers.get_provider_quota()
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["ok"] is False
    assert result["provider"] == "openai-codex"
    assert result["supported"] is True
    assert result["status"] == "unavailable"
    assert result["account_limits"] is None
    assert "Confirm provider authentication" in result["message"]
    assert "secret" not in repr(result).lower()


def test_codex_account_usage_subprocess_reports_read_only_credential_pool(monkeypatch, capsys):
    """Codex quota probes should inspect pool entries without mutating selection order."""
    import api.providers as providers

    def b64url(payload: bytes) -> str:
        return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")

    def token_for(account_id: str) -> str:
        return ".".join((
            b64url(b'{"alg":"none","typ":"JWT"}'),
            b64url(json.dumps({
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": account_id,
                },
            }).encode("utf-8")),
            b64url(b"signature"),
        ))

    primary_token = token_for("acct-primary")
    exhausted_token = token_for("acct-exhausted")

    fetch_calls = []
    load_pool_calls = []
    entries_called = []
    seen = []

    agent_mod = types.ModuleType("agent")
    agent_mod.__path__ = []
    account_usage_mod = types.ModuleType("agent.account_usage")
    credential_pool_mod = types.ModuleType("agent.credential_pool")

    def fake_fetch_account_usage(provider, *, base_url=None, api_key=None):
        fetch_calls.append((provider, base_url, api_key))
        return None

    class FakePool:
        def entries(self):
            entries_called.append(True)
            return [
                SimpleNamespace(
                    label="Team primary",
                    runtime_api_key=primary_token,
                    runtime_base_url="https://chatgpt.com/backend-api/codex",
                    last_status=None,
                ),
                SimpleNamespace(
                    label="Plus backup",
                    runtime_api_key=exhausted_token,
                    runtime_base_url="https://chatgpt.com/backend-api/codex",
                    last_status="exhausted",
                    last_status_at=1_900_000_000,
                ),
            ]

        def select(self):
            raise AssertionError("quota display must not rotate credential_pool selection")

    def fake_load_pool(provider):
        load_pool_calls.append(provider)
        return FakePool()

    def fake_urlopen(req, timeout):
        headers = {key.lower(): value for key, value in req.header_items()}
        seen.append({
            "url": req.full_url,
            "timeout": timeout,
            "headers": headers,
        })
        payload = {
            "plan_type": "pro" if headers.get("chatgpt-account-id") == "acct-primary" else "plus",
            "rate_limit": {
                "primary_window": {"used_percent": 15 if headers.get("chatgpt-account-id") == "acct-primary" else 95, "reset_at": 1_900_000_000},
                "secondary_window": {"used_percent": 40, "reset_at": "2030-03-24T12:30:00Z"},
            },
            "credits": {"has_credits": True, "balance": 12.5},
        }
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    account_usage_mod.fetch_account_usage = fake_fetch_account_usage
    credential_pool_mod.load_pool = fake_load_pool
    monkeypatch.setitem(sys.modules, "agent", agent_mod)
    monkeypatch.setitem(sys.modules, "agent.account_usage", account_usage_mod)
    monkeypatch.setitem(sys.modules, "agent.credential_pool", credential_pool_mod)
    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sys, "argv", ["quota-probe", "openai-codex", ""])

    exec(providers._ACCOUNT_USAGE_SUBPROCESS_CODE, {"__name__": "__main__"})

    output = capsys.readouterr().out.strip()
    snapshot = json.loads(output)

    assert fetch_calls == [("openai-codex", None, None)]
    assert load_pool_calls == ["openai-codex"]
    assert entries_called == [True]
    assert [call["url"] for call in seen] == [
        "https://chatgpt.com/backend-api/wham/usage",
    ]
    assert [call["timeout"] for call in seen] == [4.0]
    assert seen[0]["headers"]["authorization"] == f"Bearer {primary_token}"
    assert seen[0]["headers"]["chatgpt-account-id"] == "acct-primary"
    assert snapshot["provider"] == "openai-codex"
    assert snapshot["source"] == "usage_api_pool"
    assert snapshot["windows"][0]["label"] == "Session"
    assert snapshot["windows"][0]["used_percent"] == 15
    assert snapshot["details"] == ["1/2 credentials available", "1 exhausted", "Plans: Pro"]
    assert snapshot["available"] is True
    assert snapshot["pool"] == {
        "total_credentials": 2,
        "queried_credentials": 1,
        "available_credentials": 1,
        "exhausted_credentials": 1,
        "failed_credentials": 0,
        "plans": ["Pro"],
        "next_reset_at": "2030-03-17T17:46:40Z",
        "best_remaining_by_window": [
            {
                "label": "Session",
                "remaining_percent": 85.0,
                "used_percent": 15.0,
                "reset_at": "2030-03-17T17:46:40Z",
                "detail": None,
                "credential_label": "Team primary",
            },
            {
                "label": "Weekly",
                "remaining_percent": 60.0,
                "used_percent": 40.0,
                "reset_at": "2030-03-24T12:30:00Z",
                "detail": None,
                "credential_label": "Team primary",
            },
        ],
        "credentials": [
            {
                "label": "Team primary",
                "status": "available",
                "plan": "Pro",
                "windows": [
                    {
                        "label": "Session",
                        "used_percent": 15.0,
                        "remaining_percent": 85.0,
                        "reset_at": "2030-03-17T17:46:40Z",
                        "detail": None,
                    },
                    {
                        "label": "Weekly",
                        "used_percent": 40.0,
                        "remaining_percent": 60.0,
                        "reset_at": "2030-03-24T12:30:00Z",
                        "detail": None,
                    },
                ],
                "details": ["Credits balance: $12.50"],
                "unavailable_reason": None,
                "fetched_at": snapshot["pool"]["credentials"][0]["fetched_at"],
            },
            {
                "label": "Plus backup",
                "status": "exhausted",
                "plan": None,
                "windows": [],
                "details": [],
                "unavailable_reason": "Credential pool marked this credential exhausted; retry after 2030-03-17T18:46:40Z.",
                "retry_after": "2030-03-17T18:46:40Z",
                "fetched_at": None,
            },
        ],
    }
    assert primary_token not in output
    assert exhausted_token not in output


def test_codex_account_usage_subprocess_retries_expired_pool_exhaustion(monkeypatch, capsys):
    """Expired pool cooldowns should be probed instead of shown as still exhausted."""
    import api.providers as providers

    def b64url(payload: bytes) -> str:
        return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")

    token = ".".join((
        b64url(b'{"alg":"none","typ":"JWT"}'),
        b64url(json.dumps({
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct-expired",
            },
        }).encode("utf-8")),
        b64url(b"signature"),
    ))
    seen = []

    agent_mod = types.ModuleType("agent")
    agent_mod.__path__ = []
    account_usage_mod = types.ModuleType("agent.account_usage")
    credential_pool_mod = types.ModuleType("agent.credential_pool")

    def fake_fetch_account_usage(provider, *, base_url=None, api_key=None):
        return None

    class FakePool:
        def entries(self):
            return [
                SimpleNamespace(
                    label="Expired cooldown",
                    runtime_api_key=token,
                    runtime_base_url="https://chatgpt.com/backend-api/codex",
                    last_status="exhausted",
                    last_status_at=1,
                    last_error_code=None,
                    last_error_reset_at=None,
                ),
            ]

        def select(self):
            raise AssertionError("quota display must not rotate credential_pool selection")

    def fake_load_pool(provider):
        return FakePool()

    def fake_urlopen(req, timeout):
        seen.append(req.full_url)
        payload = {
            "plan_type": "team",
            "rate_limit": {
                "primary_window": {"used_percent": 10, "reset_at": "2030-03-17T17:30:00Z"},
            },
        }
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    account_usage_mod.fetch_account_usage = fake_fetch_account_usage
    credential_pool_mod.load_pool = fake_load_pool
    monkeypatch.setitem(sys.modules, "agent", agent_mod)
    monkeypatch.setitem(sys.modules, "agent.account_usage", account_usage_mod)
    monkeypatch.setitem(sys.modules, "agent.credential_pool", credential_pool_mod)
    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sys, "argv", ["quota-probe", "openai-codex", ""])

    exec(providers._ACCOUNT_USAGE_SUBPROCESS_CODE, {"__name__": "__main__"})

    output = capsys.readouterr().out.strip()
    snapshot = json.loads(output)

    assert seen == ["https://chatgpt.com/backend-api/wham/usage"]
    assert snapshot["pool"]["queried_credentials"] == 1
    assert snapshot["pool"]["exhausted_credentials"] == 0
    assert snapshot["pool"]["credentials"][0]["status"] == "available"
    assert snapshot["pool"]["credentials"][0]["unavailable_reason"] is None
    assert token not in output


def test_codex_account_usage_subprocess_probes_pool_entries_concurrently(monkeypatch, capsys):
    """Eligible pool credentials should be probed concurrently and reported in pool order."""
    import api.providers as providers

    def b64url(payload: bytes) -> str:
        return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")

    def token_for(account_id: str) -> str:
        return ".".join((
            b64url(b'{"alg":"none","typ":"JWT"}'),
            b64url(json.dumps({
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": account_id,
                },
            }).encode("utf-8")),
            b64url(b"signature"),
        ))

    token_a = token_for("acct-a")
    token_b = token_for("acct-b")
    events = []
    events_lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=2)

    agent_mod = types.ModuleType("agent")
    agent_mod.__path__ = []
    account_usage_mod = types.ModuleType("agent.account_usage")
    credential_pool_mod = types.ModuleType("agent.credential_pool")

    def fake_fetch_account_usage(provider, *, base_url=None, api_key=None):
        return None

    class FakePool:
        def entries(self):
            return [
                SimpleNamespace(
                    label="Slow A",
                    runtime_api_key=token_a,
                    runtime_base_url="https://chatgpt.com/backend-api/codex",
                    last_status=None,
                ),
                SimpleNamespace(
                    label="Slow B",
                    runtime_api_key=token_b,
                    runtime_base_url="https://chatgpt.com/backend-api/codex",
                    last_status=None,
                ),
            ]

        def select(self):
            raise AssertionError("quota display must not rotate credential_pool selection")

    def fake_load_pool(provider):
        return FakePool()

    def fake_urlopen(req, timeout):
        headers = {key.lower(): value for key, value in req.header_items()}
        account_id = headers.get("chatgpt-account-id")
        with events_lock:
            events.append(("enter", account_id))
        barrier.wait()
        with events_lock:
            events.append(("exit", account_id))
        used = 80 if account_id == "acct-a" else 10
        payload = {
            "plan_type": "team",
            "rate_limit": {
                "primary_window": {"used_percent": used, "reset_at": "2030-03-17T17:30:00Z"},
            },
        }
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    account_usage_mod.fetch_account_usage = fake_fetch_account_usage
    credential_pool_mod.load_pool = fake_load_pool
    monkeypatch.setitem(sys.modules, "agent", agent_mod)
    monkeypatch.setitem(sys.modules, "agent.account_usage", account_usage_mod)
    monkeypatch.setitem(sys.modules, "agent.credential_pool", credential_pool_mod)
    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sys, "argv", ["quota-probe", "openai-codex", ""])

    exec(providers._ACCOUNT_USAGE_SUBPROCESS_CODE, {"__name__": "__main__"})

    output = capsys.readouterr().out.strip()
    snapshot = json.loads(output)
    first_exit = next(index for index, event in enumerate(events) if event[0] == "exit")

    assert [event[0] for event in events[:first_exit]] == ["enter", "enter"]
    assert snapshot["pool"]["queried_credentials"] == 2
    assert [row["label"] for row in snapshot["pool"]["credentials"]] == ["Slow A", "Slow B"]
    assert snapshot["pool"]["best_remaining_by_window"][0]["credential_label"] == "Slow B"
    assert snapshot["pool"]["best_remaining_by_window"][0]["remaining_percent"] == 90.0
    assert token_a not in output
    assert token_b not in output


def test_codex_account_usage_subprocess_sanitizes_pool_entry_errors(monkeypatch, capsys):
    """Pool per-entry failures must not leak bearer/JWT-like exception text."""
    import api.providers as providers

    fetch_calls = []
    agent_mod = types.ModuleType("agent")
    agent_mod.__path__ = []
    account_usage_mod = types.ModuleType("agent.account_usage")
    credential_pool_mod = types.ModuleType("agent.credential_pool")

    def fake_fetch_account_usage(provider, *, base_url=None, api_key=None):
        fetch_calls.append((provider, base_url, api_key))
        return None

    class FakePool:
        def entries(self):
            return [
                SimpleNamespace(
                    label="Bad token",
                    runtime_api_key="header.payload.signature",
                    runtime_base_url="https://chatgpt.com/backend-api/codex",
                    last_status=None,
                ),
            ]

        def select(self):
            raise AssertionError("quota display must not rotate credential_pool selection")

    def fake_load_pool(provider):
        return FakePool()

    def fake_urlopen(_req, timeout):
        raise RuntimeError("Bearer eyJsecret-token-like-value should not leak")

    account_usage_mod.fetch_account_usage = fake_fetch_account_usage
    credential_pool_mod.load_pool = fake_load_pool
    monkeypatch.setitem(sys.modules, "agent", agent_mod)
    monkeypatch.setitem(sys.modules, "agent.account_usage", account_usage_mod)
    monkeypatch.setitem(sys.modules, "agent.credential_pool", credential_pool_mod)
    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sys, "argv", ["quota-probe", "openai-codex", ""])

    exec(providers._ACCOUNT_USAGE_SUBPROCESS_CODE, {"__name__": "__main__"})

    output = capsys.readouterr().out.strip()
    snapshot = json.loads(output)

    assert fetch_calls == [("openai-codex", None, None)]
    assert snapshot["available"] is False
    assert snapshot["pool"]["failed_credentials"] == 1
    assert snapshot["pool"]["credentials"][0]["unavailable_reason"] == "Usage unavailable for this credential."
    assert "eyJsecret" not in output
    assert "Bearer" not in output


def test_codex_account_usage_subprocess_keeps_legacy_reason_when_pool_misses(monkeypatch, capsys):
    """A failed pool fallback should not discard the legacy unavailable reason."""
    import api.providers as providers

    fetch_calls = []
    load_pool_calls = []

    agent_mod = types.ModuleType("agent")
    agent_mod.__path__ = []
    account_usage_mod = types.ModuleType("agent.account_usage")
    credential_pool_mod = types.ModuleType("agent.credential_pool")

    def fake_fetch_account_usage(provider, *, base_url=None, api_key=None):
        fetch_calls.append((provider, base_url, api_key))
        return SimpleNamespace(
            provider="openai-codex",
            source="usage_api",
            title="Account limits",
            plan=None,
            windows=(),
            details=(),
            available=False,
            unavailable_reason="Codex account limits are not available for this credential.",
            fetched_at=datetime(2030, 3, 17, 12, 30, tzinfo=timezone.utc),
        )

    class EmptyPool:
        def select(self):
            return None

    def fake_load_pool(provider):
        load_pool_calls.append(provider)
        return EmptyPool()

    def explode_urlopen(*_args, **_kwargs):
        raise AssertionError("no network call should happen when the pool has no selected entry")

    account_usage_mod.fetch_account_usage = fake_fetch_account_usage
    credential_pool_mod.load_pool = fake_load_pool
    monkeypatch.setitem(sys.modules, "agent", agent_mod)
    monkeypatch.setitem(sys.modules, "agent.account_usage", account_usage_mod)
    monkeypatch.setitem(sys.modules, "agent.credential_pool", credential_pool_mod)
    monkeypatch.setattr(providers.urllib.request, "urlopen", explode_urlopen)
    monkeypatch.setattr(sys, "argv", ["quota-probe", "openai-codex", ""])

    exec(providers._ACCOUNT_USAGE_SUBPROCESS_CODE, {"__name__": "__main__"})

    snapshot = json.loads(capsys.readouterr().out.strip())

    assert fetch_calls == [("openai-codex", None, None)]
    assert load_pool_calls == ["openai-codex"]
    assert snapshot["available"] is False
    assert snapshot["unavailable_reason"] == "Codex account limits are not available for this credential."
    assert snapshot["fetched_at"] == "2030-03-17T12:30:00Z"


def test_account_usage_pool_payload_round_trips_to_provider_quota_status():
    """Parent process serialization must preserve pooled credential summaries."""
    import api.providers as providers

    payload = {
        "provider": "openai-codex",
        "source": "usage_api_pool",
        "title": "Account limits",
        "plan": None,
        "windows": [
            {"label": "Session", "used_percent": 25, "reset_at": "2030-03-17T17:30:00Z", "detail": "Best of 2"},
        ],
        "details": ["2/3 credentials available"],
        "available": True,
        "unavailable_reason": None,
        "fetched_at": "2030-03-17T12:30:00Z",
        "pool": {
            "total_credentials": 3,
            "available_credentials": 2,
            "exhausted_credentials": 1,
            "failed_credentials": 0,
            "credentials": [
                {"label": "Credential 1", "status": "available", "windows": []},
                {
                    "label": "Credential 2",
                    "status": "exhausted",
                    "windows": [],
                    "retry_after": "2030-03-17T18:46:40Z",
                },
            ],
        },
    }

    snapshot = providers._account_usage_payload_to_snapshot(payload)
    serialized = providers._serialize_account_usage_snapshot(snapshot)

    assert serialized["windows"][0]["remaining_percent"] == 75.0
    assert serialized["pool"] == payload["pool"]


def test_anthropic_oauth_usage_unavailable_reason_is_reported(monkeypatch, tmp_path):
    """Hermes Agent can report why account limits are not available."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    old_cfg, old_mtime = _with_config(model={"provider": "anthropic"})

    import api.providers as providers

    monkeypatch.setattr(
        providers,
        "_agent_fetch_account_usage_for_home",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider="anthropic",
            source="oauth_usage_api",
            title="Account limits",
            plan=None,
            fetched_at=datetime(2030, 3, 17, 12, 30, tzinfo=timezone.utc),
            available=False,
            windows=(),
            details=(),
            unavailable_reason="Anthropic account limits are only available for OAuth-backed Claude accounts.",
        ),
    )
    try:
        result = providers.get_provider_quota()
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["ok"] is False
    assert result["provider"] == "anthropic"
    assert result["supported"] is True
    assert result["status"] == "unavailable"
    assert result["account_limits"]["unavailable_reason"].startswith("Anthropic account limits")
    assert "OAuth-backed Claude accounts" in result["message"]


def test_account_usage_profile_fetch_does_not_enter_cron_env_context():
    """Quota probes must not reuse cron's process-global env/module swapper."""
    import api.providers as providers

    body = inspect.getsource(providers._fetch_account_usage_with_profile_context)
    assert "cron_profile_context_for_home" not in body
    assert "_agent_fetch_account_usage_for_home" in body


def test_account_usage_profile_env_is_child_scoped(monkeypatch, tmp_path):
    """Profile .env values should be passed to the child probe only."""
    import api.providers as providers

    home = tmp_path / "profile-a"
    home.mkdir()
    (home / ".env").write_text("ANTHROPIC_API_KEY=profile-key\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "process-key")

    env = providers._account_usage_subprocess_env(home, "anthropic", None)

    assert env["HERMES_HOME"] == str(home)
    assert env["ANTHROPIC_API_KEY"] == "profile-key"
    assert os.environ["ANTHROPIC_API_KEY"] == "process-key"


def test_account_usage_profile_fetch_uses_short_lived_cache(monkeypatch, tmp_path):
    """Repeated Settings refreshes should not re-query pooled account usage immediately."""
    import api.providers as providers

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    old_cfg, old_mtime = _with_config(model={"provider": "openai-codex"})
    providers._account_usage_status_cache.clear()
    calls = []
    snapshots = [
        SimpleNamespace(
            provider="openai-codex",
            source="usage_api_pool",
            title="Account limits",
            plan=None,
            windows=(),
            details=(),
            available=True,
            unavailable_reason=None,
            fetched_at=datetime(2030, 3, 17, 12, 30, tzinfo=timezone.utc),
            pool={"total_credentials": 1, "credentials": []},
        ),
        SimpleNamespace(
            provider="openai-codex",
            source="usage_api_pool",
            title="Account limits",
            plan=None,
            windows=(),
            details=(),
            available=True,
            unavailable_reason=None,
            fetched_at=datetime(2030, 3, 17, 12, 31, tzinfo=timezone.utc),
            pool={"total_credentials": 1, "credentials": []},
        ),
    ]

    def fake_fetch(provider, home, api_key=None):
        calls.append((provider, str(home), api_key))
        return snapshots[len(calls) - 1]

    monkeypatch.setattr(providers, "_agent_fetch_account_usage_for_home", fake_fetch)
    try:
        first = providers._fetch_account_usage_with_profile_context("openai-codex")
        second = providers._fetch_account_usage_with_profile_context("openai-codex")
        refreshed_status = providers.get_provider_quota("openai-codex", refresh=True)
        after_refresh = providers._fetch_account_usage_with_profile_context("openai-codex")
    finally:
        providers._account_usage_status_cache.clear()
        _restore_config(old_cfg, old_mtime)

    assert first is snapshots[0]
    assert second is snapshots[0]
    assert refreshed_status["account_limits"]["fetched_at"] == "2030-03-17T12:31:00Z"
    assert after_refresh is snapshots[1]
    assert calls == [
        ("openai-codex", str(tmp_path), None),
        ("openai-codex", str(tmp_path), None),
    ]


def test_account_usage_forced_refresh_failure_preserves_warm_snapshot(monkeypatch, tmp_path):
    """A failed manual refresh should not discard the last usable account snapshot."""
    import api.providers as providers

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    old_cfg, old_mtime = _with_config(model={"provider": "openai-codex"})
    providers._account_usage_status_cache.clear()
    calls = []
    good_snapshot = SimpleNamespace(
        provider="openai-codex",
        source="usage_api_pool",
        title="Account limits",
        plan=None,
        windows=(),
        details=(),
        available=True,
        unavailable_reason=None,
        fetched_at=datetime(2030, 3, 17, 12, 30, tzinfo=timezone.utc),
        pool={"total_credentials": 1, "credentials": []},
    )

    def fake_fetch(provider, home, api_key=None):
        calls.append((provider, str(home), api_key))
        return good_snapshot if len(calls) == 1 else None

    monkeypatch.setattr(providers, "_agent_fetch_account_usage_for_home", fake_fetch)
    try:
        first = providers._fetch_account_usage_with_profile_context("openai-codex")
        refreshed = providers._fetch_account_usage_with_profile_context(
            "openai-codex",
            refresh=True,
        )
        after_refresh_failure = providers._fetch_account_usage_with_profile_context(
            "openai-codex",
        )
    finally:
        providers._account_usage_status_cache.clear()
        _restore_config(old_cfg, old_mtime)

    assert first is good_snapshot
    assert refreshed is None
    assert after_refresh_failure is good_snapshot
    assert calls == [
        ("openai-codex", str(tmp_path), None),
        ("openai-codex", str(tmp_path), None),
    ]


def test_account_usage_profile_cache_invalidates_with_credential_pool_cache(monkeypatch, tmp_path):
    """Credential-pool invalidation should also clear pooled account usage."""
    import api.providers as providers

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    old_cfg, old_mtime = _with_config(model={"provider": "openai-codex"})
    providers._account_usage_status_cache.clear()
    calls = []
    snapshots = [
        SimpleNamespace(
            provider="openai-codex",
            source="usage_api_pool",
            title="Account limits",
            plan=None,
            windows=(),
            details=(),
            available=True,
            unavailable_reason=None,
            fetched_at=datetime(2030, 3, 17, 12, 30, tzinfo=timezone.utc),
            pool={"total_credentials": 1, "credentials": []},
        ),
        SimpleNamespace(
            provider="openai-codex",
            source="usage_api_pool",
            title="Account limits",
            plan=None,
            windows=(),
            details=(),
            available=True,
            unavailable_reason=None,
            fetched_at=datetime(2030, 3, 17, 12, 31, tzinfo=timezone.utc),
            pool={"total_credentials": 1, "credentials": []},
        ),
    ]

    def fake_fetch(provider, home, api_key=None):
        calls.append((provider, str(home), api_key))
        return snapshots[len(calls) - 1]

    monkeypatch.setattr(providers, "_agent_fetch_account_usage_for_home", fake_fetch)
    try:
        first = providers._fetch_account_usage_with_profile_context("openai-codex")
        config.invalidate_credential_pool_cache("openai-codex")
        second = providers._fetch_account_usage_with_profile_context("openai-codex")
    finally:
        providers._account_usage_status_cache.clear()
        _restore_config(old_cfg, old_mtime)

    assert first is snapshots[0]
    assert second is snapshots[1]
    assert calls == [
        ("openai-codex", str(tmp_path), None),
        ("openai-codex", str(tmp_path), None),
    ]



def test_account_usage_profile_fetch_caches_unavailable_snapshots(monkeypatch, tmp_path):
    """Known unavailable account snapshots should be cached like available ones."""
    import api.providers as providers

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    old_cfg, old_mtime = _with_config(model={"provider": "openai-codex"})
    providers._account_usage_status_cache.clear()
    calls = []
    unavailable_snapshot = SimpleNamespace(
        provider="openai-codex",
        source="usage_api_pool",
        title="Account limits",
        plan=None,
        windows=(),
        details=("0/1 credentials available", "1 exhausted"),
        available=False,
        unavailable_reason="Credential pool exhausted until 2030-03-17T18:46:40Z.",
        fetched_at=datetime(2030, 3, 17, 12, 30, tzinfo=timezone.utc),
        pool={"total_credentials": 1, "available_credentials": 0, "exhausted_credentials": 1, "credentials": []},
    )

    def fake_fetch(provider, home, api_key=None):
        calls.append((provider, str(home), api_key))
        return unavailable_snapshot

    monkeypatch.setattr(providers, "_agent_fetch_account_usage_for_home", fake_fetch)
    try:
        first = providers._fetch_account_usage_with_profile_context("openai-codex")
        second = providers._fetch_account_usage_with_profile_context("openai-codex")
    finally:
        providers._account_usage_status_cache.clear()
        _restore_config(old_cfg, old_mtime)

    assert first is unavailable_snapshot
    assert second is unavailable_snapshot
    assert calls == [("openai-codex", str(tmp_path), None)]


def test_account_usage_profile_fetch_does_not_cache_transient_none_results(monkeypatch, tmp_path):
    """Transient None probe results should not mask the next successful status check."""
    import api.providers as providers

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    old_cfg, old_mtime = _with_config(model={"provider": "openai-codex"})
    providers._account_usage_status_cache.clear()
    calls = []
    recovered_snapshot = SimpleNamespace(
        provider="openai-codex",
        source="usage_api_pool",
        title="Account limits",
        plan=None,
        windows=(),
        details=(),
        available=True,
        unavailable_reason=None,
        fetched_at=datetime(2030, 3, 17, 12, 31, tzinfo=timezone.utc),
        pool={"total_credentials": 1, "credentials": []},
    )

    def fake_fetch(provider, home, api_key=None):
        calls.append((provider, str(home), api_key))
        return None if len(calls) == 1 else recovered_snapshot

    monkeypatch.setattr(providers, "_agent_fetch_account_usage_for_home", fake_fetch)
    try:
        first = providers._fetch_account_usage_with_profile_context("openai-codex")
        second = providers._fetch_account_usage_with_profile_context("openai-codex")
        third = providers._fetch_account_usage_with_profile_context("openai-codex")
    finally:
        providers._account_usage_status_cache.clear()
        _restore_config(old_cfg, old_mtime)

    assert first is None
    assert second is recovered_snapshot
    assert third is recovered_snapshot
    assert calls == [
        ("openai-codex", str(tmp_path), None),
        ("openai-codex", str(tmp_path), None),
    ]


def test_account_usage_profile_fetches_can_overlap_for_different_homes(monkeypatch, tmp_path):
    """Different profile quota fetches should not serialize on cron's global lock."""
    import api.providers as providers

    homes = {
        "quota-a": tmp_path / "a",
        "quota-b": tmp_path / "b",
    }
    for home in homes.values():
        home.mkdir()
    barrier = threading.Barrier(2, timeout=2)
    events = []
    errors = []

    def fake_home():
        return homes[threading.current_thread().name]

    def fake_fetch(provider, home, api_key=None):
        events.append(("enter", str(home)))
        barrier.wait()
        events.append(("exit", str(home)))
        return None

    monkeypatch.setattr(providers, "_get_hermes_home", fake_home)
    monkeypatch.setattr(providers, "_agent_fetch_account_usage_for_home", fake_fetch)

    def worker():
        try:
            providers._fetch_account_usage_with_profile_context("openai-codex")
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, name="quota-a"),
        threading.Thread(target=worker, name="quota-b"),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert [kind for kind, _home in events[:2]] == ["enter", "enter"]


def test_openai_api_key_detection_ignores_codex_oauth_jwt(monkeypatch, tmp_path):
    """A Codex OAuth JWT in OPENAI_API_KEY should not show a bare OpenAI card."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def b64url(payload: bytes) -> str:
        return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")

    codex_token = ".".join((
        b64url(b'{"alg":"none","typ":"JWT"}'),
        b64url(json.dumps({
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-codex"},
        }).encode("utf-8")),
        b64url(b"signature"),
    ))
    (tmp_path / ".env").write_text(f"OPENAI_API_KEY={codex_token}\n", encoding="utf-8")
    old_cfg, old_mtime = _with_config(model={"provider": "openai"})

    import api.providers as providers
    try:
        assert providers._provider_has_key("openai") is False
        assert providers._get_provider_api_key("openai") is None
        provider_ids = [p["id"] for p in providers.get_providers()["providers"]]
    finally:
        _restore_config(old_cfg, old_mtime)

    assert "openai" not in provider_ids
    assert "openai-codex" in provider_ids
    assert codex_token not in repr(provider_ids)


def test_openai_api_key_detection_still_accepts_real_api_keys(monkeypatch, tmp_path):
    """Filtering Codex OAuth tokens must not hide real OpenAI API keys."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-test-real-openai-key\n", encoding="utf-8")
    old_cfg, old_mtime = _with_config(model={"provider": "openai"})

    import api.providers as providers
    try:
        assert providers._provider_has_key("openai") is True
        assert providers._get_provider_api_key("openai") == "sk-test-real-openai-key"
    finally:
        _restore_config(old_cfg, old_mtime)



def test_openai_api_key_detection_falls_through_after_codex_jwt_config_value(monkeypatch, tmp_path):
    """A filtered OpenAI config value should not mask a later real API key source."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def b64url(payload: bytes) -> str:
        return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")

    codex_token = ".".join((
        b64url(b'{"alg":"none","typ":"JWT"}'),
        b64url(json.dumps({
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-codex"},
        }).encode("utf-8")),
        b64url(b"signature"),
    ))
    old_cfg, old_mtime = _with_config(
        model={"provider": "openai", "api_key": codex_token},
        providers={"openai": {"api_key": "sk-config-openai-key"}},
    )

    import api.providers as providers
    try:
        assert providers._provider_has_key("openai") is True
        assert providers._get_provider_api_key("openai") == "sk-config-openai-key"
    finally:
        _restore_config(old_cfg, old_mtime)


def test_provider_quota_route_is_registered():
    """The backend must expose a route for the UI to poll quota status."""
    routes = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    assert 'parsed.path == "/api/provider/quota"' in routes
    assert 'query.get("refresh", [""])' in routes
    assert "get_provider_quota(provider_id, refresh=refresh)" in routes


def test_provider_quota_card_is_rendered_in_providers_panel():
    """The Providers panel should show active provider quota/status before cards."""
    panels = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    assert "_fetchProviderQuotaStatus(false)" in panels
    assert "'/api/provider/quota'" in panels
    assert "function _buildProviderQuotaCard" in panels
    assert "provider_quota_title" in panels
    assert "provider-quota-card" in panels
    assert "account_limits" in panels
    assert "remaining_percent" in panels
    assert "provider-quota-details" in panels
    assert "provider_quota_credential_pool" in panels
    assert "provider-quota-pool-row" in panels
    assert "_buildProviderQuotaPoolBreakdown" in panels
    assert "_providerQuotaPoolShouldDefaultOpen" in panels
    assert "hermes-provider-quota-pool-open" in panels
    assert "provider-quota-pool-chevron" in panels
    assert 'aria-hidden="true"' in panels
    assert "count>0&&count<=3" in panels
    assert "status.status==='available'||accountLimits.pool" in panels
    assert "provider-quota-window-detail" in panels
    assert "provider_quota_session_limit" in panels
    assert "provider_quota_weekly_limit" in panels
    assert "_providerQuotaUnavailableReason" in panels
    assert "provider_quota_retry_after" in panels
    assert "accountLimits.details)&&!accountLimits.pool" in panels


def test_provider_quota_card_has_manual_refresh_control():
    """The quota card should let users force an immediate fresh usage lookup."""
    panels = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    assert "function _refreshProviderQuota" in panels
    assert "function _fetchProviderQuotaStatus" in panels
    assert "refresh=1" in panels
    assert "cache:'no-store'" in panels
    assert "data-provider-quota-refresh" in panels
    assert "provider_quota_refresh_usage" in panels
    assert "provider_quota_refresh_succeeded" in panels
    assert "provider_quota_refresh_failed" in panels
    assert "card.isConnected&&button" in panels
    assert "provider_quota_last_checked" in panels


def test_provider_quota_i18n_keys_exist_for_all_locales():
    """Provider quota UI keys must be present in every locale block."""
    i18n = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    locale_count = len(
        re.findall(r"^  (?:[A-Za-z_][A-Za-z0-9_]*|'[^']+'):\s*\{", i18n, re.MULTILINE)
    )
    keys = sorted(set(re.findall(r"provider_quota_[a-z0-9_]+", (ROOT / "static" / "panels.js").read_text(encoding="utf-8"))))
    assert locale_count >= 1
    assert "provider_quota_retry_after" in keys
    for key in keys:
        assert len(re.findall(rf"^\s+{re.escape(key)}:", i18n, re.MULTILINE)) == locale_count, key


def test_settings_label_and_description_i18n_keys_exist_for_all_locales():
    """Settings labels/descriptions referenced by the page need every locale."""
    i18n = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    index_html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    locale_count = len(
        re.findall(r"^  (?:[A-Za-z_][A-Za-z0-9_]*|'[^']+'):\s*\{", i18n, re.MULTILINE)
    )
    keys = sorted(
        set(re.findall(r'data-i18n="(settings_(?:label|desc)_[a-z0-9_]+)"', index_html))
    )
    assert locale_count >= 1
    assert "settings_label_fade_text_effect" in keys
    assert "settings_desc_fade_text_effect" in keys
    for key in keys:
        assert len(re.findall(rf"^\s+{re.escape(key)}:", i18n, re.MULTILINE)) == locale_count, key


def test_provider_quota_styles_exist():
    """Quota UI should have visible supported/unavailable/invalid states."""
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    for token in (
        ".provider-quota-card",
        ".provider-quota-metric",
        ".provider-quota-card-available",
        ".provider-quota-card-no_key",
        ".provider-quota-card-invalid_key",
        ".provider-quota-details",
        ".provider-quota-window",
        ".provider-quota-actions",
        ".provider-quota-refresh",
        ".provider-quota-checked",
        ".provider-quota-pool",
        ".provider-quota-pool-chevron",
        ".provider-quota-pool[open] .provider-quota-pool-chevron",
        ".provider-quota-pool-row",
        ".provider-quota-pool-window",
        ".provider-quota-window-detail",
    ):
        assert token in css


# ── Regression tests for #1912 ────────────────────────────────────────────────

def test_account_usage_subprocess_uses_devnull_stdin(monkeypatch):
    """Account-usage probe subprocess must receive stdin=DEVNULL.

    DEVNULL prevents the child from inheriting any pipe that could block or
    leak data.  This is a defence-in-depth measure beyond the parent-death
    signal; it is tested separately to make the invariant explicit.
    """
    import api.providers as providers
    import subprocess

    seen_stdin = None

    def capturing_run(*args, **kwargs):
        nonlocal seen_stdin
        seen_stdin = kwargs.get('stdin')
        class FakeProc:
            returncode = 0
            stdout = '{}'
            stderr = ''
        return FakeProc()

    monkeypatch.setattr(subprocess, 'run', capturing_run)
    try:
        providers._agent_fetch_account_usage_for_home(
            'openai-codex', Path('/nonexistent'), api_key=None
        )
    except Exception:
        pass  # errors are expected on a fake env; we only care about stdin

    assert seen_stdin is subprocess.DEVNULL, (
        f'expected stdin=subprocess.DEVNULL, got {seen_stdin!r}'
    )


def test_account_usage_probe_semaphore_has_correct_bound(monkeypatch, tmp_path):
    """The probe semaphore must enforce the declared concurrency cap.

    Verifying the bound directly ensures the cap actually prevents resource
    exhaustion when the UI polls multiple providers in rapid succession.
    """
    import api.providers as providers

    monkeypatch.setattr(profiles, 'get_active_hermes_home', lambda: tmp_path)
    old_cfg, old_mtime = _with_config(model={'provider': 'openai-codex'})

    sem = providers._get_account_usage_probe_semaphore()
    try:
        bound = sem._value
        assert bound == providers._MAX_CONCURRENT_ACCOUNT_USAGE_PROBES, (
            f'semaphore bound is {bound}, expected '
            f'{providers._MAX_CONCURRENT_ACCOUNT_USAGE_PROBES}'
        )
    finally:
        _restore_config(old_cfg, old_mtime)


def test_account_usage_preexec_fn_is_wired_on_posix(monkeypatch):
    """On POSIX systems the probe subprocess must receive a parent-death preexec_fn.

    The preexec_fn arranges prctl(PR_SET_PDEATHSIG, SIGTERM) so the child is
    terminated when the WebUI parent dies (OOM kill, systemctl restart, etc.).
    This test verifies the wiring and skips harmlessly on non-POSIX (Windows).
    """
    import api.providers as providers

    assert callable(providers._account_usage_preexec_fn)

    try:
        providers._account_usage_preexec_fn()
    except Exception as exc:
        raise AssertionError(
            f'_account_usage_preexec_fn raised {exc!r}; it should be '
            'safe to call unconditionally'
        ) from exc

    if hasattr(os, 'fork'):
        import subprocess

        captured_kwargs = {}

        def capture_run(*args, **kwargs):
            captured_kwargs.update(kwargs)
            class FakeProc:
                returncode = 0
                stdout = '{}'
                stderr = ''
            return FakeProc()

        monkeypatch.setattr(subprocess, 'run', capture_run)
        try:
            providers._agent_fetch_account_usage_for_home(
                'openai-codex', Path('/nonexistent'), api_key=None
            )
        except Exception:
            pass

        assert 'preexec_fn' in captured_kwargs, (
            'preexec_fn should be in subprocess.run kwargs on POSIX'
        )
        assert captured_kwargs['preexec_fn'] is providers._account_usage_preexec_fn


def test_account_usage_semaphore_caps_concurrency(monkeypatch, tmp_path):
    """The probe semaphore must actually serialise callers beyond its bound.

    Verifies the bounded semaphore is used in the call path and genuinely
    prevents more than _MAX_CONCURRENT_ACCOUNT_USAGE_PROBES probes running.
    """
    import api.providers as providers
    import threading

    monkeypatch.setattr(profiles, 'get_active_hermes_home', lambda: tmp_path)
    old_cfg, old_mtime = _with_config(model={'provider': 'openai-codex'})

    barrier = threading.Barrier(2, timeout=2)
    unblock = threading.Event()

    def slow_fetch(provider, home, api_key=None):
        barrier.wait()
        unblock.wait(timeout=5)
        return None

    monkeypatch.setattr(providers, '_agent_fetch_account_usage_for_home', slow_fetch)

    results = []
    errors = []

    def worker():
        try:
            results.append(
                providers._fetch_account_usage_with_profile_context('openai-codex')
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    unblock.set()

    try:
        assert not errors, f'workers raised: {errors}'
        assert len(results) == 2, f'expected 2 results, got {len(results)}'
    finally:
        _restore_config(old_cfg, old_mtime)
