"""Behavior coverage for Codex banked reset redemption in Providers."""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

import api.providers as providers
import api.routes as routes

ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")


class _FakeHandler:
    def __init__(self, body: dict | None = None, *, raw_body=None):
        raw = json.dumps(body if raw_body is None else raw_body).encode("utf-8")
        self.command = "POST"
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.headers = {"Content-Length": str(len(raw))}
        self.client_address = ("127.0.0.1", 12345)
        self.status = None
        self.sent_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass

    def payload(self):
        raw = self.wfile.getvalue().decode("utf-8")
        return json.loads(raw) if raw else {}


@contextmanager
def _noop_profile_env(*_args, **_kwargs):
    yield


def _snapshot(*, fetched_at: str = "2030-03-17T12:30:00Z", count: int = 1, pool=None, available=True):
    return SimpleNamespace(
        provider="openai-codex",
        source="usage_api" if pool is None else "usage_api_pool",
        title="Account limits",
        plan="Pro",
        windows=(
            SimpleNamespace(label="Session", used_percent=100.0 if available else 25.0, reset_at=datetime(2030, 3, 17, 17, 30, tzinfo=timezone.utc), detail=None),
            SimpleNamespace(label="Weekly", used_percent=40.0, reset_at=datetime(2030, 3, 24, 12, 30, tzinfo=timezone.utc), detail=None),
        ),
        details=("Credits balance: $12.50",),
        banked_resets=SimpleNamespace(available_count=count),
        available=True,
        unavailable_reason=None,
        fetched_at=fetched_at,
        pool=pool,
    )


def _extract_function_source(name: str) -> str:
    marker = f"async function {name}("
    start = PANELS_JS.find(marker)
    if start == -1:
        marker = f"function {name}("
        start = PANELS_JS.find(marker)
    assert start != -1, f"{name} not found"
    brace = PANELS_JS.find("{", start)
    depth = 0
    for idx in range(brace, len(PANELS_JS)):
        ch = PANELS_JS[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return PANELS_JS[start : idx + 1]
    raise AssertionError(f"unterminated function {name}")


def _target_lock_key(keys: tuple[str, ...]) -> str:
    for key in keys:
        if key.startswith("target:"):
            return key
    raise AssertionError("target key missing")


def _install_codex_reset_runtime_mocks(
    monkeypatch,
    *,
    resolved_base_url: str | None = None,
    resolved_api_key: str | None = None,
    pool_entries,
    helper_callable,
    resolve_callable=None,
    read_codex_tokens=None,
    source: str | None = None,
):
    import sys
    import types

    calls = {"resolve": 0, "load_pool": [], "helper": []}
    source_value = source or "hermes-auth-store"
    if read_codex_tokens is None and source_value == "hermes-auth-store":
        read_codex_tokens = lambda: {"tokens": {"access_token": str(resolved_api_key or "").strip()}}

    def fake_resolve_codex_runtime_credentials():
        calls["resolve"] += 1
        if resolve_callable is not None:
            return resolve_callable()
        resolved = {
            "provider": "openai-codex",
            "base_url": resolved_base_url,
            "api_key": resolved_api_key,
            "source": source_value,
        }
        return resolved

    class _CredentialPool:
        def entries(self):
            return pool_entries() if callable(pool_entries) else pool_entries

    def fake_load_pool(provider):
        calls["load_pool"].append(provider)
        return _CredentialPool()

    def fake_helper(*, base_url, api_key, force):
        calls["helper"].append((base_url, api_key, force))
        return helper_callable(base_url=base_url, api_key=api_key, force=force)

    hermes_cli_mod = types.ModuleType("hermes_cli")
    hermes_cli_mod.__path__ = []
    hermes_auth_mod = types.ModuleType("hermes_cli.auth")
    hermes_auth_mod.resolve_codex_runtime_credentials = fake_resolve_codex_runtime_credentials
    if read_codex_tokens is not None:
        hermes_auth_mod._read_codex_tokens = read_codex_tokens
    agent_mod = types.ModuleType("agent")
    agent_mod.__path__ = []
    credential_pool_mod = types.ModuleType("agent.credential_pool")
    credential_pool_mod.load_pool = fake_load_pool
    account_usage_mod = types.ModuleType("agent.account_usage")
    account_usage_mod.redeem_codex_reset_credit = fake_helper

    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_mod)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", hermes_auth_mod)
    monkeypatch.setitem(sys.modules, "agent", agent_mod)
    monkeypatch.setitem(sys.modules, "agent.credential_pool", credential_pool_mod)
    monkeypatch.setitem(sys.modules, "agent.account_usage", account_usage_mod)

    return calls


def test_redeem_codex_reset_fails_closed_for_ambiguous_pool(monkeypatch):
    calls = _install_codex_reset_runtime_mocks(
        monkeypatch,
        resolved_base_url="https://runtime.example/v1",
        resolved_api_key="resolved-secret-token",
        pool_entries=[{"runtime_api_key": "resolved-secret-token"}, {"runtime_api_key": "other-secret-token"}],
        helper_callable=lambda **kwargs: pytest.fail("helper should not be called"),
    )

    pool = {
        "total_credentials": 2,
        "queried_credentials": 2,
        "available_credentials": 2,
        "exhausted_credentials": 0,
        "failed_credentials": 0,
        "credentials": [
            {"label": "Primary", "status": "available", "windows": [], "banked_resets": {"available_count": 1}},
            {"label": "Backup", "status": "available", "windows": [], "banked_resets": {"available_count": 2}},
        ],
    }

    monkeypatch.setattr(providers, "_active_provider_id", lambda: "openai-codex")
    refreshes = []
    monkeypatch.setattr(
        providers,
        "get_provider_quota",
        lambda provider_id=None, refresh=False: (
            refreshes.append(refresh)
            or {
                "ok": True,
                "provider": "openai-codex",
                "status": "unavailable",
                "account_limits": {
                    "banked_resets": {"available_count": 3},
                    "pool": pool,
                },
                "message": "pool ambiguous",
            }
        ),
    )
    monkeypatch.setattr(providers, "invalidate_account_usage_status_cache", lambda provider_id=None: None)

    result = providers.redeem_codex_reset_credit_status(force=False)

    assert result["ok"] is False
    assert result["http_status"] == 409
    assert result["redemption"]["reason_code"] == "ambiguous_pool"
    assert result["quota_status"]["account_limits"]["banked_resets"]["available_count"] == 3
    assert calls["helper"] == []
    assert refreshes == [False]


def test_redeem_codex_reset_rejects_exhausted_multi_pool_before_unavailable_gate(monkeypatch):
    calls = _install_codex_reset_runtime_mocks(
        monkeypatch,
        resolved_base_url="https://runtime.example/v1",
        resolved_api_key="resolved-secret-token",
        pool_entries=[{"runtime_api_key": "resolved-secret-token"}, {"runtime_api_key": "other-secret-token"}],
        helper_callable=lambda **kwargs: pytest.fail("helper should not be called"),
    )
    quota_status = {
        "ok": False,
        "provider": "openai-codex",
        "status": "unavailable",
        "account_limits": {
            "banked_resets": {"available_count": 2},
            "pool": {
                "total_credentials": 2,
                "available_credentials": 0,
                "exhausted_credentials": 2,
                "credentials": [{"status": "exhausted"}, {"status": "exhausted"}],
            },
        },
    }
    monkeypatch.setattr(providers, "_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(providers, "get_provider_quota", lambda provider_id=None, refresh=False: quota_status)

    result = providers.redeem_codex_reset_credit_status(force=False)

    assert result["http_status"] == 409
    assert result["redemption"]["reason_code"] == "ambiguous_pool"
    assert calls["helper"] == []


def test_redeem_codex_reset_bindings_use_resolved_credentials_without_second_resolution(monkeypatch):
    calls = _install_codex_reset_runtime_mocks(
        monkeypatch,
        resolved_base_url="https://runtime.example/v1",
        resolved_api_key="resolved-secret-token",
        pool_entries=[{"runtime_api_key": "resolved-secret-token", "runtime_base_url": "https://runtime.example/v1"}],
        helper_callable=lambda **kwargs: {
            "status": "reset",
            "redeemed": True,
            "message": "Reset redeemed.",
            "available_count": 1,
            "windows_reset": 1,
        },
    )

    monkeypatch.setattr(providers, "_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(
        providers,
        "get_provider_quota",
        lambda provider_id=None, refresh=False: {
            "ok": True,
            "provider": "openai-codex",
            "status": "available",
            "account_limits": {"banked_resets": {"available_count": 1}},
        },
    )

    result = providers.redeem_codex_reset_credit_status(force=True)

    assert result["ok"] is True
    assert result["redemption"]["state"] == "reset"
    assert result["redemption"]["message"] == "Reset redeemed."
    assert calls["resolve"] == 1
    assert calls["load_pool"] == ["openai-codex"]
    assert calls["helper"] == [("https://runtime.example/v1", "resolved-secret-token", True)]


@pytest.mark.parametrize("pool_entries", [None, {}, "bad", 1])
def test_redeem_codex_reset_fails_closed_for_unreadable_pool_state(monkeypatch, pool_entries):
    calls = _install_codex_reset_runtime_mocks(
        monkeypatch,
        resolved_base_url="https://runtime.example/v1",
        resolved_api_key="resolved-secret-token",
        pool_entries=pool_entries,
        helper_callable=lambda **kwargs: pytest.fail("helper should not be called"),
    )

    monkeypatch.setattr(providers, "_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(
        providers,
        "get_provider_quota",
        lambda provider_id=None, refresh=False: {
            "ok": True,
            "provider": "openai-codex",
            "status": "available",
            "account_limits": {"banked_resets": {"available_count": 1}},
        },
    )

    result = providers.redeem_codex_reset_credit_status(force=False)

    assert result["http_status"] == 409
    assert result["redemption"]["reason_code"] in {"unknown_account", "ambiguous_pool"}
    assert calls["helper"] == []


def test_redeem_codex_reset_rejects_resolved_credential_target_changes(monkeypatch):
    calls = _install_codex_reset_runtime_mocks(
        monkeypatch,
        resolved_base_url="https://runtime.example/v1/",
        resolved_api_key="resolved-secret-token",
        pool_entries=[{"runtime_api_key": "different-secret-token"}],
        helper_callable=lambda **kwargs: pytest.fail("helper should not be called on mismatch"),
    )

    monkeypatch.setattr(providers, "_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(
        providers,
        "get_provider_quota",
        lambda provider_id=None, refresh=False: {
            "ok": True,
            "provider": "openai-codex",
            "status": "available",
            "account_limits": {"banked_resets": {"available_count": 1}},
        },
    )

    mismatch = providers.redeem_codex_reset_credit_status(force=False)
    assert mismatch["http_status"] == 409
    assert mismatch["redemption"]["reason_code"] == "credential_target_changed"
    assert calls["helper"] == []


def test_redeem_codex_reset_allows_matching_credential_target_and_inherited_base_url(monkeypatch):
    calls = _install_codex_reset_runtime_mocks(
        monkeypatch,
        resolved_base_url="https://runtime.example/v2",
        resolved_api_key="resolved-secret-token",
        pool_entries=[{"runtime_api_key": "resolved-secret-token"}],
        helper_callable=lambda **kwargs: {
            "status": "reset",
            "redeemed": True,
            "message": "Reset redeemed.",
            "available_count": 2,
            "windows_reset": 2,
        },
    )
    monkeypatch.setattr(providers, "_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(
        providers,
        "get_provider_quota",
        lambda provider_id=None, refresh=False: {
            "ok": True,
            "provider": "openai-codex",
            "status": "available",
            "account_limits": {"banked_resets": {"available_count": 1}},
        },
    )
    result = providers.redeem_codex_reset_credit_status(force=False)
    assert result["ok"] is True
    assert result["redemption"]["state"] == "reset"
    assert result["redemption"]["message"] == "Reset redeemed."
    assert calls["helper"] == [("https://runtime.example/v2", "resolved-secret-token", False)]


def test_redeem_codex_reset_no_pool_entries_permits_reset_because_of_inherited_base_url(monkeypatch):
    calls = _install_codex_reset_runtime_mocks(
        monkeypatch,
        resolved_base_url="https://runtime.example/v3",
        resolved_api_key="resolved-secret-token",
        pool_entries=[],
        helper_callable=lambda **kwargs: {
            "status": "reset",
            "redeemed": True,
            "message": "Reset redeemed.",
            "available_count": 2,
            "windows_reset": 2,
        },
    )

    monkeypatch.setattr(providers, "_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(
        providers,
        "get_provider_quota",
        lambda provider_id=None, refresh=False: {
            "ok": True,
            "provider": "openai-codex",
            "status": "available",
            "account_limits": {"banked_resets": {"available_count": 1}},
        },
    )

    result = providers.redeem_codex_reset_credit_status(force=False)
    assert result["ok"] is True
    assert result["redemption"]["state"] == "reset"
    assert calls["helper"] == [("https://runtime.example/v3", "resolved-secret-token", False)]


def test_redeem_codex_reset_helper_single_flight_blocks_concurrent_requests(monkeypatch, tmp_path):
    started = threading.Event()
    release = threading.Event()
    thread_result = {}

    def helper(base_url, api_key, force):
        started.set()
        release.wait()
        return {
            "status": "reset",
            "redeemed": True,
            "message": "Reset redeemed.",
            "available_count": 0,
            "windows_reset": 0,
        }

    calls = _install_codex_reset_runtime_mocks(
        monkeypatch,
        resolved_base_url="https://runtime.example/v1",
        resolved_api_key="resolved-secret-token",
        pool_entries=[{"runtime_api_key": "resolved-secret-token", "runtime_base_url": "https://runtime.example/v1"}],
        helper_callable=lambda **kwargs: helper(**kwargs),
    )

    monkeypatch.setattr(providers, "_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(providers, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        providers,
        "get_provider_quota",
        lambda provider_id=None, refresh=False: {
            "ok": True,
            "provider": "openai-codex",
            "status": "available",
            "account_limits": {"banked_resets": {"available_count": 1}},
        },
    )

    lock_key = providers._codex_reset_lock_key(profile_home=str(tmp_path), provider="openai-codex")
    providers._CODEX_RESET_REDEMPTION_LOCKS.pop(lock_key, None)
    try:
        worker = threading.Thread(target=lambda: thread_result.setdefault("first", providers.redeem_codex_reset_credit_status(force=False)))
        worker.start()
        assert started.wait(1), "reset helper did not start"

        concurrent = providers.redeem_codex_reset_credit_status(force=False)
        assert concurrent["http_status"] == 409
        assert concurrent["redemption"]["reason_code"] == "in_progress"
        assert calls["helper"] == [("https://runtime.example/v1", "resolved-secret-token", False)]
        assert providers._CODEX_RESET_REDEMPTION_LOCKS.get(lock_key) is not None
        assert sum(1 for key in providers._CODEX_RESET_REDEMPTION_LOCKS if key == lock_key) == 1

        release.set()
        worker.join(1)
        first = thread_result["first"]

        assert first["http_status"] == 200
        assert first["redemption"]["state"] == "reset"
        assert calls["helper"] == [("https://runtime.example/v1", "resolved-secret-token", False)]

        third = providers.redeem_codex_reset_credit_status(force=False)
        assert third["http_status"] == 200
        assert third["redemption"]["state"] == "reset"
        assert calls["helper"] == [
            ("https://runtime.example/v1", "resolved-secret-token", False),
            ("https://runtime.example/v1", "resolved-secret-token", False),
        ]
        assert providers._CODEX_RESET_REDEMPTION_LOCKS.get(lock_key) is not None
        assert sum(1 for key in providers._CODEX_RESET_REDEMPTION_LOCKS if key == lock_key) == 1
    finally:
        providers._CODEX_RESET_REDEMPTION_LOCKS.pop(lock_key, None)


def test_codex_reset_target_lock_keys_includes_target_and_account_for_matching_hermes_auth_store(monkeypatch):
    import sys
    import types

    hermes_cli_mod = types.ModuleType("hermes_cli")
    hermes_cli_mod.__path__ = []
    hermes_auth_mod = types.ModuleType("hermes_cli.auth")
    hermes_auth_mod._read_codex_tokens = lambda: {
        "tokens": {"account_id": "acct-stable-001", "access_token": "wrapped-access-token"},
        "last_refresh": "2031-01-01T00:00:00Z",
    }

    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_mod)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", hermes_auth_mod)

    keys, key_error = providers._codex_reset_target_lock_keys(
        credential={
            "source": "hermes-auth-store",
            "base_url": "https://runtime.example/v1",
            "api_key": "wrapped-access-token",
        }
    )
    assert key_error is None
    expected_target = providers.hashlib.sha256("https://runtime.example/v1|wrapped-access-token".encode("utf-8")).hexdigest()
    expected_account = providers.hashlib.sha256("acct-stable-001".encode("utf-8")).hexdigest()
    assert set(keys) == {f"target:{expected_target}", f"account:{expected_account}"}
    target_key = _target_lock_key(keys)
    assert target_key == f"target:{expected_target}"
    for key in keys:
        assert "acct-stable-001" not in key
        assert "wrapped-access-token" not in key


def test_codex_reset_target_lock_keys_fails_closed_when_read_codex_tokens_raises(monkeypatch):
    import types
    import sys

    hermes_cli_mod = types.ModuleType("hermes_cli")
    hermes_cli_mod.__path__ = []
    hermes_auth_mod = types.ModuleType("hermes_cli.auth")
    hermes_auth_mod._read_codex_tokens = lambda: (_ for _ in ()).throw(RuntimeError("boom"))

    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_mod)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", hermes_auth_mod)

    keys, key_error = providers._codex_reset_target_lock_keys(
        credential={
            "source": "hermes-auth-store",
            "base_url": "https://runtime.example/v1",
            "api_key": "wrapped-access-token",
        }
    )
    assert keys == ()
    assert key_error == "unknown_account"
    assert "wrapped-access-token" not in str(keys)


def test_codex_reset_target_lock_keys_fails_closed_for_credential_rotation(monkeypatch):
    import sys
    import types

    hermes_cli_mod = types.ModuleType("hermes_cli")
    hermes_cli_mod.__path__ = []
    hermes_auth_mod = types.ModuleType("hermes_cli.auth")
    hermes_auth_mod._read_codex_tokens = lambda: {
        "tokens": {"account_id": "acct-stable-002", "access_token": "unrelated-token"},
        "last_refresh": "2031-01-01T00:00:00Z",
    }

    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_mod)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", hermes_auth_mod)

    keys, key_error = providers._codex_reset_target_lock_keys(
        credential={
            "source": "hermes-auth-store",
            "base_url": "https://runtime.example/v1",
            "api_key": "wrapped-access-token",
        }
    )
    assert keys == ()
    assert key_error == "credential_target_changed"


def test_codex_reset_target_lock_keys_fails_closed_for_invalid_auth_store_token_payload(monkeypatch):
    import sys
    import types

    hermes_cli_mod = types.ModuleType("hermes_cli")
    hermes_cli_mod.__path__ = []
    hermes_auth_mod = types.ModuleType("hermes_cli.auth")

    hermes_auth_mod._read_codex_tokens = lambda: {"tokens": object()}

    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_mod)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", hermes_auth_mod)

    keys, key_error = providers._codex_reset_target_lock_keys(
        credential={
            "source": "hermes-auth-store",
            "base_url": "https://runtime.example/v1",
            "api_key": "wrapped-access-token",
        }
    )
    assert keys == ()
    assert key_error == "unknown_account"


def test_codex_reset_target_lock_keys_fails_closed_for_flat_auth_store_token_payload(monkeypatch):
    import sys
    import types

    hermes_cli_mod = types.ModuleType("hermes_cli")
    hermes_cli_mod.__path__ = []
    hermes_auth_mod = types.ModuleType("hermes_cli.auth")
    hermes_auth_mod._read_codex_tokens = lambda: {
        "access_token": "wrapped-access-token",
        "account_id": "acct-leak",
    }

    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_mod)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", hermes_auth_mod)

    keys, key_error = providers._codex_reset_target_lock_keys(
        credential={
            "source": "hermes-auth-store",
            "base_url": "https://runtime.example/v1",
            "api_key": "wrapped-access-token",
        }
    )
    assert keys == ()
    assert key_error == "unknown_account"
    assert "acct-leak" not in str(keys)


def test_codex_reset_target_lock_keys_ignores_empty_account_id(monkeypatch):
    import sys
    import types

    hermes_cli_mod = types.ModuleType("hermes_cli")
    hermes_cli_mod.__path__ = []
    hermes_auth_mod = types.ModuleType("hermes_cli.auth")
    hermes_auth_mod._read_codex_tokens = lambda: {
        "tokens": {
            "account_id": "   ",
            "access_token": "wrapped-access-token",
        }
    }

    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_mod)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", hermes_auth_mod)

    keys, key_error = providers._codex_reset_target_lock_keys(
        credential={
            "source": "hermes-auth-store",
            "base_url": "https://runtime.example/v1",
            "api_key": "wrapped-access-token",
        }
    )
    assert key_error is None
    target_key = _target_lock_key(keys)
    assert target_key == f"target:{providers.hashlib.sha256('https://runtime.example/v1|wrapped-access-token'.encode('utf-8')).hexdigest()}"
    assert len(keys) == 1


def test_codex_reset_target_lock_key_is_target_only_for_non_auth_store_source(monkeypatch):
    keys, key_error = providers._codex_reset_target_lock_keys(
        credential={
            "source": "credential_pool",
            "base_url": "https://runtime.example/v1",
            "api_key": "runtime-token",
        }
    )
    expected = providers.hashlib.sha256("https://runtime.example/v1|runtime-token".encode("utf-8")).hexdigest()
    assert key_error is None
    assert keys == (f"target:{expected}",)
    assert _target_lock_key(keys) == f"target:{expected}"


def test_codex_reset_target_lock_key_target_fingerprint_is_stable_across_sources(monkeypatch):
    import sys
    import types

    hermes_cli_mod = types.ModuleType("hermes_cli")
    hermes_cli_mod.__path__ = []
    hermes_auth_mod = types.ModuleType("hermes_cli.auth")
    hermes_auth_mod._read_codex_tokens = lambda: {
        "tokens": {
            "account_id": "acct-stable-004",
            "access_token": "stable-token",
        }
    }

    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_mod)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", hermes_auth_mod)

    auth_keys, auth_error = providers._codex_reset_target_lock_keys(
        credential={"source": "hermes-auth-store", "base_url": "https://runtime.example/v1", "api_key": "stable-token"}
    )
    pool_keys, pool_error = providers._codex_reset_target_lock_keys(
        credential={"source": "credential_pool", "base_url": "https://runtime.example/v1", "api_key": "stable-token"}
    )
    assert auth_error is None
    assert pool_error is None
    assert _target_lock_key(auth_keys) == _target_lock_key(pool_keys)
    assert _target_lock_key(auth_keys).startswith("target:")
    assert _target_lock_key(pool_keys).startswith("target:")


def test_redeem_codex_reset_refreshes_and_rejects_zero_count_without_agent(monkeypatch):
    calls = _install_codex_reset_runtime_mocks(
        monkeypatch,
        resolved_base_url="https://runtime.example/v1",
        resolved_api_key="resolved-secret-token",
        pool_entries=[{"runtime_api_key": "resolved-secret-token", "runtime_base_url": "https://runtime.example/v1"}],
        helper_callable=lambda **kwargs: {
            "status": "not_exhausted",
            "message": "Redeem is unnecessary.",
            "available_count": 0,
            "windows_reset": 0,
            "redeemed": True,
        },
    )
    refreshes = []
    monkeypatch.setattr(providers, "_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(providers, "get_provider_quota", lambda provider_id=None, refresh=False: (refreshes.append(refresh) or {
        "ok": True, "provider": "openai-codex", "status": "available", "account_limits": {
            "banked_resets": {"available_count": 0}, "pool": None,
        },
    }))
    result = providers.redeem_codex_reset_credit_status(force=False)

    assert result["http_status"] == 200
    assert result["redemption"]["state"] == "not_exhausted"
    assert result["redemption"]["reason_code"] is None
    assert result["redemption"]["message"] == "Redeem is unnecessary."
    assert refreshes == [True]
    assert calls["helper"] == [("https://runtime.example/v1", "resolved-secret-token", False)]


def test_redeem_codex_reset_allows_positive_count_for_single_exhausted_pool(monkeypatch):
    calls = _install_codex_reset_runtime_mocks(
        monkeypatch,
        resolved_base_url="https://runtime.example/v1",
        resolved_api_key="resolved-secret-token",
        pool_entries=[{"runtime_api_key": "resolved-secret-token", "runtime_base_url": "https://runtime.example/v1"}],
        helper_callable=lambda **kwargs: {"status": "reset", "redeemed": True, "message": "Reset redeemed."},
    )
    unavailable_snapshot = SimpleNamespace(
        provider="openai-codex",
        source="usage_api_pool",
        title="Account limits",
        plan=None,
        windows=(),
        details=("0/1 credentials available", "1 exhausted"),
        banked_resets={"available_count": 2},
        available=False,
        unavailable_reason="No Codex pool credentials returned available account limits.",
        fetched_at=None,
        pool={
            "total_credentials": 1,
            "available_credentials": 0,
            "exhausted_credentials": 1,
            "credentials": [{"status": "exhausted"}],
        },
    )
    monkeypatch.setattr(providers, "_fetch_account_usage_with_profile_context", lambda provider, refresh=False: unavailable_snapshot)
    aggregate_status = providers._provider_account_usage_status("openai-codex", "Codex", refresh=True)
    assert aggregate_status["status"] == "unavailable"
    assert aggregate_status["account_limits"]["available"] is False
    quota_status = {
        "ok": False,
        "provider": "openai-codex",
        "status": "unavailable",
        "account_limits": {
            "banked_resets": {"available_count": 2},
            "pool": {
                "total_credentials": 1,
                "available_credentials": 0,
                "exhausted_credentials": 1,
                "credentials": [{"status": "exhausted"}],
            },
        },
    }
    monkeypatch.setattr(providers, "_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(providers, "get_provider_quota", lambda provider_id=None, refresh=False: quota_status)

    result = providers.redeem_codex_reset_credit_status(force=False)

    assert result["ok"] is True
    assert result["quota_status"]["status"] == "unavailable"
    assert calls["helper"] == [("https://runtime.example/v1", "resolved-secret-token", False)]


def test_redeem_codex_reset_calls_shared_helper_invalidates_cache_and_refreshes_quota(monkeypatch):
    helper_calls = _install_codex_reset_runtime_mocks(
        monkeypatch,
        resolved_base_url="https://runtime.example/v1",
        resolved_api_key="resolved-secret-token",
        pool_entries=[{"runtime_api_key": "resolved-secret-token", "runtime_base_url": "https://runtime.example/v1"}],
        helper_callable=lambda **kwargs: SimpleNamespace(
            status="reset",
            message="Reset redeemed.",
            available_count=0,
            windows_reset=2,
            redeemed=True,
        ),
    )
    invalidated = []
    snapshots = [
        _snapshot(count=1, fetched_at="2030-03-17T12:30:00Z"),
        _snapshot(count=0, fetched_at="2030-03-17T12:31:00Z"),
    ]

    def fake_fetch(provider, refresh=False):
        assert provider == "openai-codex"
        return snapshots.pop(0)

    monkeypatch.setattr(providers, "_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(providers, "_fetch_account_usage_with_profile_context", fake_fetch)
    monkeypatch.setattr(providers, "invalidate_account_usage_status_cache", lambda provider_id=None: invalidated.append(provider_id))

    result = providers.redeem_codex_reset_credit_status(force=False)

    assert result["ok"] is True
    assert result["http_status"] == 200
    assert result["redemption"] == {
        "ok": True,
        "state": "reset",
        "message": "Reset redeemed.",
        "reason_code": None,
        "available_count": 0,
        "windows_reset": 2,
    }
    assert result["quota_status"]["account_limits"]["banked_resets"]["available_count"] == 1
    assert result["quota_status"]["account_limits"]["fetched_at"] == "2030-03-17T12:30:00Z"
    assert helper_calls["helper"] == [("https://runtime.example/v1", "resolved-secret-token", False)]
    assert invalidated == ["openai-codex"]


def test_redeem_codex_reset_preserves_success_when_quota_refresh_fails_and_warns_stale(monkeypatch):
    calls = []
    refreshes = []

    calls_helper = _install_codex_reset_runtime_mocks(
        monkeypatch,
        resolved_base_url="https://runtime.example/v1",
        resolved_api_key="resolved-secret-token",
        pool_entries=[{"runtime_api_key": "resolved-secret-token", "runtime_base_url": "https://runtime.example/v1"}],
        helper_callable=lambda **kwargs: {
            "status": "reset",
            "redeemed": True,
            "message": "Reset redeemed.",
            "available_count": 0,
            "windows_reset": 1,
        },
    )

    def fake_quota(provider_id=None, refresh=False):
        refreshes.append((provider_id, refresh))
        if refresh:
            raise RuntimeError("quota refresh failed")
        return {
            "ok": True,
            "provider": "openai-codex",
            "status": "available",
            "account_limits": {"banked_resets": {"available_count": 1}},
        }

    monkeypatch.setattr(providers, "_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(providers, "get_provider_quota", fake_quota)
    monkeypatch.setattr(providers, "invalidate_account_usage_status_cache", lambda provider_id=None: calls.append(provider_id))

    result = providers.redeem_codex_reset_credit_status(force=False)

    assert result["ok"] is True
    assert result["http_status"] == 200
    assert result["redemption"]["state"] == "reset"
    assert result["redemption"]["message"] == "Reset redeemed."
    assert result["quota_status"]["status"] == "unavailable"
    assert "remaining reset count may be stale" in (result["quota_status"]["message"] or "")
    assert refreshes == [("openai-codex", True)]
    assert calls == ["openai-codex"]
    assert calls_helper["helper"] == [("https://runtime.example/v1", "resolved-secret-token", False)]


def test_redeem_codex_reset_marks_unknown_outcome_with_unavailable_status_and_best_effort_refresh(monkeypatch):
    quota_calls = []
    calls = _install_codex_reset_runtime_mocks(
        monkeypatch,
        resolved_base_url="https://runtime.example/v1",
        resolved_api_key="resolved-secret-token",
        pool_entries=[{"runtime_api_key": "resolved-secret-token", "runtime_base_url": "https://runtime.example/v1"}],
        helper_callable=lambda **kwargs: {
            "status": "unavailable",
            "message": "Bearer secret backend token should not leak",
            "available_count": 3,
            "windows_reset": 1,
        },
    )

    def fake_quota(provider_id=None, refresh=False):
        quota_calls.append((provider_id, refresh))
        return {
            "ok": True,
            "provider": "openai-codex",
            "status": "available",
            "account_limits": {"banked_resets": {"available_count": 2}},
            "message": "refreshed after best-effort call",
        }

    monkeypatch.setattr(providers, "_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(providers, "get_provider_quota", fake_quota)
    monkeypatch.setattr(providers, "invalidate_account_usage_status_cache", lambda provider_id=None: None)

    result = providers.redeem_codex_reset_credit_status(force=False)

    assert result["http_status"] == 200
    assert result["redemption"]["state"] == "unknown_outcome"
    assert result["redemption"]["reason_code"] == "unknown_outcome"
    assert result["redemption"]["message"] == (
        "The Codex backend did not return a definitive redemption result. "
        "The reset outcome is unknown; refresh account status before trying again."
    )
    assert "secret backend token" not in result["redemption"]["message"]
    assert "secret backend token" not in json.dumps(result).lower()
    assert calls["helper"] == [("https://runtime.example/v1", "resolved-secret-token", False)]
    assert quota_calls == [("openai-codex", True)]


def test_normalize_codex_reset_redemption_requires_real_reset_contract():
    assert providers._normalize_codex_reset_redemption(
        {"ok": True, "status": "redeemed", "message": "invented"}
    ) == {
        "ok": False,
        "state": "redeemed",
        "message": "invented",
        "reason_code": None,
    }
    assert providers._normalize_codex_reset_redemption(
        SimpleNamespace(status="reset", message="not redeemed", available_count=1, windows_reset=0, redeemed=False)
    ) == {
        "ok": False,
        "state": "reset",
        "message": "not redeemed",
        "reason_code": None,
        "available_count": 1,
        "windows_reset": 0,
    }


def test_normalize_codex_reset_redemption_falls_through_none_dict_values():
    result = providers._normalize_codex_reset_redemption(
        {
            "state": None,
            "status": "reset",
            "redeemed": True,
            "message": None,
            "detail": "Reset redeemed.",
        }
    )

    assert result["ok"] is True
    assert result["state"] == "reset"
    assert result["message"] == "Reset redeemed."


def test_result_value_falls_through_none_dict_values_to_later_names():
    assert providers._result_value({"message": None, "detail": "details"}, "message", "detail") == "details"


@pytest.mark.parametrize("value", [False, 0, ""])
def test_result_value_preserves_falsey_dict_values(value):
    assert providers._result_value({"value": value}, "value", "fallback") is value


def test_normalize_codex_reset_redemption_preserves_long_not_exhausted_guidance_and_redacts_sensitive_output():
    guidance = (
        "Current Codex usage is not exhausted. Review the account-limits output in the Codex app or web, "
        "then retry only if you intend to spend a banked reset on the current window. If you have confirmed "
        "the active window is the one you want to clear, rerun /usage reset --force to continue."
    )

    result = providers._normalize_codex_reset_redemption(
        {
            "status": "not_exhausted",
            "message": guidance,
            "available_count": 4,
            "windows_reset": 0,
            "redeemed": False,
        }
    )

    assert result == {
        "ok": False,
        "state": "not_exhausted",
        "message": guidance,
        "reason_code": None,
        "available_count": 4,
        "windows_reset": 0,
    }
    assert result["message"].endswith("rerun /usage reset --force to continue.")

    redacted = providers._normalize_codex_reset_redemption(
        {
            "status": "not_exhausted",
            "message": "Bearer secret access_token leaked from provider output",
            "redeemed": False,
        }
    )

    assert redacted == {
        "ok": False,
        "state": "not_exhausted",
        "message": "Codex reset redemption failed.",
        "reason_code": None,
    }


def test_redeem_codex_reset_sanitizes_helper_failure(monkeypatch):
    _install_codex_reset_runtime_mocks(
        monkeypatch,
        resolved_base_url="https://runtime.example/v1",
        resolved_api_key="resolved-secret-token",
        pool_entries=[{"runtime_api_key": "resolved-secret-token", "runtime_base_url": "https://runtime.example/v1"}],
        helper_callable=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("secret bearer access_token should not leak")),
    )

    monkeypatch.setattr(providers, "_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(providers, "_fetch_account_usage_with_profile_context", lambda provider, refresh=False: _snapshot(count=1))
    monkeypatch.setattr(providers, "invalidate_account_usage_status_cache", lambda provider_id=None: None)

    result = providers.redeem_codex_reset_credit_status(force=True)

    assert result["ok"] is False
    assert result["http_status"] == 502
    assert result["redemption"]["ok"] is False
    assert "secret" not in json.dumps(result).lower()
    assert "access_token" not in json.dumps(result).lower()


def test_redeem_codex_reset_rejects_non_boolean_force_before_helper(monkeypatch):
    helper_called = []

    monkeypatch.setattr(providers, "_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(providers, "_fetch_account_usage_with_profile_context", lambda provider, refresh=False: _snapshot(count=1))

    import sys
    import types

    agent_mod = types.ModuleType("agent")
    agent_mod.__path__ = []
    account_usage_mod = types.ModuleType("agent.account_usage")

    def fake_redeem_codex_reset_credit(*, force=False):
        helper_called.append(force)
        raise AssertionError("helper should not be called")

    account_usage_mod.redeem_codex_reset_credit = fake_redeem_codex_reset_credit
    monkeypatch.setitem(sys.modules, "agent", agent_mod)
    monkeypatch.setitem(sys.modules, "agent.account_usage", account_usage_mod)

    result = providers.redeem_codex_reset_credit_status(force="yes")

    assert result["ok"] is False
    assert result["http_status"] == 400
    assert result["redemption"]["reason_code"] == "invalid_force"
    assert helper_called == []


def test_codex_reset_route_validates_body_and_uses_profile_scope(monkeypatch):
    seen = {"entered": 0}

    @contextmanager
    def fake_profile_env(path, logger_override=None):
        assert path == "/api/provider/openai-codex/reset"
        seen["entered"] += 1
        yield

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr("api.profiles.profile_env_for_active_request", fake_profile_env)
    monkeypatch.setattr(routes, "redeem_codex_reset_credit_status", lambda force=False: {"ok": True, "http_status": 200, "quota_status": {"ok": True}, "redemption": {"ok": True}})

    wrong_provider = _FakeHandler({"provider": "openai", "force": False})
    assert routes.handle_post(wrong_provider, urlparse("/api/provider/openai-codex/reset")) is True
    assert wrong_provider.status == 400
    assert "only force" in wrong_provider.payload()["error"]

    invalid_force = _FakeHandler({"force": "yes"})
    assert routes.handle_post(invalid_force, urlparse("/api/provider/openai-codex/reset")) is True
    assert invalid_force.status == 400
    assert "force" in invalid_force.payload()["error"]

    for malformed in (None, [], "text"):
        malformed_handler = _FakeHandler(raw_body=malformed)
        assert routes.handle_post(malformed_handler, urlparse("/api/provider/openai-codex/reset")) is True
        assert malformed_handler.status == 400
        assert "JSON object" in malformed_handler.payload()["error"]

    ok = _FakeHandler({"force": False})
    assert routes.handle_post(ok, urlparse("/api/provider/openai-codex/reset")) is True
    assert ok.status == 200
    assert seen["entered"] == 1


def test_codex_reset_route_blocks_same_target_across_profiles_and_allows_cross_profile_retry_after_release(tmp_path, monkeypatch):
    import api.profiles as profiles

    lock_keys_before = set(providers._CODEX_RESET_REDEMPTION_LOCKS.keys())
    request_started = threading.Event()
    release_block = threading.Event()
    home_state = threading.local()
    base_url = "https://runtime.example/v1"
    token = "resolved-secret-token"

    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"

    def token_for(_home_key: str) -> str:
        return token

    def resolve():
        home_key = str(home_state.home)
        return {
            "provider": "openai-codex",
            "base_url": base_url,
            "api_key": token,
            "source": "hermes-auth-store" if home_key == str(profile_a) else "credential_pool",
        }

    @contextmanager
    def fake_profile_env_for_request(_path, logger_override=None):
        assert hasattr(home_state, "home"), "request profile home was not initialized"
        yield

    def fake_get_active_hermes_home():
        return home_state.home

    def helper(**kwargs):
        request_started.set()
        release_block.wait()
        return {
            "status": "reset",
            "redeemed": True,
            "message": "Reset redeemed.",
            "available_count": 0,
            "windows_reset": 0,
        }

    def pool_entries():
        home_key = str(home_state.home)
        return [{"runtime_api_key": token_for(home_key), "runtime_base_url": base_url}]

    def read_codex_tokens():
        home_key = str(home_state.home)
        return {
            "tokens": {
                "account_id": "acct-shared-target",
                "access_token": token_for(home_key),
            }
        }

    calls = _install_codex_reset_runtime_mocks(
        monkeypatch,
        resolved_base_url="unused",
        resolved_api_key="unused",
        pool_entries=pool_entries,
        resolve_callable=resolve,
        helper_callable=helper,
        read_codex_tokens=read_codex_tokens,
    )

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(profiles, "profile_env_for_active_request", fake_profile_env_for_request)
    monkeypatch.setattr(profiles, "get_active_hermes_home", fake_get_active_hermes_home)
    monkeypatch.setattr(providers, "_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(
        providers,
        "get_provider_quota",
        lambda provider_id=None, refresh=False: {
            "ok": True,
            "provider": "openai-codex",
            "status": "available",
            "account_limits": {"banked_resets": {"available_count": 1}},
        },
    )

    results = {}
    errors = {}

    def run(profile_home: Path, key: str):
        try:
            home_state.home = profile_home
            handler = _FakeHandler({"force": False})
            routes.handle_post(handler, urlparse("/api/provider/openai-codex/reset"))
            results[key] = handler
        except Exception as exc:  # pragma: no cover - thread-safe assertion path
            errors[key] = exc
        finally:
            if hasattr(home_state, "home"):
                del home_state.home

    first = threading.Thread(target=run, args=(profile_a, "first"))
    second = threading.Thread(target=run, args=(profile_b, "second"))

    try:
        first.start()
        assert request_started.wait(1), "reset helper never started"

        second.start()
        second.join(2)
        assert not second.is_alive(), "contending request failed to settle"
        assert not errors
        assert "second" in results
        assert results["second"].status == 409
        assert results["second"].payload()["redemption"]["reason_code"] == "in_progress"
        assert calls["helper"] == [(base_url, token, False)]

        release_block.set()
        first.join(2)
        assert not first.is_alive(), "initial request blocked indefinitely"
        assert not errors
        assert "first" in results
        assert results["first"].status == 200
        assert results["first"].payload()["redemption"]["state"] == "reset"

        home_state.home = profile_b
        third = _FakeHandler({"force": False})
        routes.handle_post(third, urlparse("/api/provider/openai-codex/reset"))
        if hasattr(home_state, "home"):
            del home_state.home

        assert third.status == 200
        assert third.payload()["redemption"]["state"] == "reset"
        assert calls["helper"] == [
            (base_url, token, False),
            (base_url, token, False),
        ]
    finally:
        release_block.set()
        if hasattr(home_state, "home"):
            del home_state.home
        first.join(1)
        second.join(1)
        for key in set(providers._CODEX_RESET_REDEMPTION_LOCKS.keys()) - lock_keys_before:
            providers._CODEX_RESET_REDEMPTION_LOCKS.pop(key, None)


def test_codex_reset_route_blocks_same_target_for_same_account_different_tokens(tmp_path, monkeypatch):
    import api.profiles as profiles

    lock_keys_before = set(providers._CODEX_RESET_REDEMPTION_LOCKS.keys())
    request_started = threading.Event()
    release_block = threading.Event()
    home_state = threading.local()

    @contextmanager
    def fake_profile_env_for_request(_path, logger_override=None):
        assert hasattr(home_state, "home"), "request profile home was not initialized"
        yield

    def fake_get_active_hermes_home():
        return home_state.home

    def helper(**kwargs):
        request_started.set()
        release_block.wait()
        return {
            "status": "reset",
            "redeemed": True,
            "message": "Reset redeemed.",
            "available_count": 0,
            "windows_reset": 0,
        }

    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    base_url = "https://runtime.example/v1"
    account_id = "acct-shared-target"
    profile_to_target = {
        str(profile_a): "resolved-token-a",
        str(profile_b): "resolved-token-b",
    }

    def resolve():
        home_key = str(home_state.home)
        return {
            "provider": "openai-codex",
            "base_url": base_url,
            "api_key": profile_to_target[home_key],
            "source": "hermes-auth-store",
        }

    def pool_entries():
        home_key = str(home_state.home)
        return [{"runtime_api_key": profile_to_target[home_key], "runtime_base_url": base_url}]

    def read_codex_tokens():
        home_key = str(home_state.home)
        return {"tokens": {"account_id": account_id, "access_token": profile_to_target[home_key]}}

    calls = _install_codex_reset_runtime_mocks(
        monkeypatch,
        resolved_base_url="unused",
        resolved_api_key="unused",
        resolve_callable=resolve,
        pool_entries=pool_entries,
        helper_callable=helper,
        read_codex_tokens=read_codex_tokens,
    )

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(profiles, "profile_env_for_active_request", fake_profile_env_for_request)
    monkeypatch.setattr(profiles, "get_active_hermes_home", fake_get_active_hermes_home)
    monkeypatch.setattr(providers, "_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(
        providers,
        "get_provider_quota",
        lambda provider_id=None, refresh=False: {
            "ok": True,
            "provider": "openai-codex",
            "status": "available",
            "account_limits": {"banked_resets": {"available_count": 1}},
        },
    )

    results = {}
    errors = {}

    def run(profile_home: Path, key: str):
        try:
            home_state.home = profile_home
            handler = _FakeHandler({"force": False})
            routes.handle_post(handler, urlparse("/api/provider/openai-codex/reset"))
            results[key] = handler
        except Exception as exc:  # pragma: no cover - thread-safe assertion path
            errors[key] = exc
        finally:
            if hasattr(home_state, "home"):
                del home_state.home

    first = threading.Thread(target=run, args=(profile_a, "first"))
    second = threading.Thread(target=run, args=(profile_b, "second"))

    try:
        first.start()
        assert request_started.wait(1), "reset helper never started"
        second.start()
        second.join(2)
        assert not second.is_alive()
        assert not errors
        assert "second" in results
        assert results["second"].status == 409
        assert results["second"].payload()["redemption"]["reason_code"] == "in_progress"
        assert calls["helper"] == [(base_url, "resolved-token-a", False)]
    finally:
        release_block.set()
        if hasattr(home_state, "home"):
            del home_state.home
        first.join(1)
        second.join(1)
        for key in set(providers._CODEX_RESET_REDEMPTION_LOCKS.keys()) - lock_keys_before:
            providers._CODEX_RESET_REDEMPTION_LOCKS.pop(key, None)


def test_codex_reset_route_rejects_resolved_token_rotation_between_runtime_and_auth_store_lock_verification(tmp_path, monkeypatch):
    import api.profiles as profiles

    lock_keys_before = set(providers._CODEX_RESET_REDEMPTION_LOCKS.keys())
    base_url = "https://runtime.example/v1"
    resolved_token = "resolved-token"
    stored_token = "stored-token"

    @contextmanager
    def fake_profile_env_for_request(_path, logger_override=None):
        yield

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(profiles, "profile_env_for_active_request", fake_profile_env_for_request)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(providers, "_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(
        providers,
        "get_provider_quota",
        lambda provider_id=None, refresh=False: {
            "ok": True,
            "provider": "openai-codex",
            "status": "available",
            "account_limits": {"banked_resets": {"available_count": 1}},
        },
    )

    calls = _install_codex_reset_runtime_mocks(
        monkeypatch,
        resolved_base_url=base_url,
        resolved_api_key=resolved_token,
        pool_entries=[{"runtime_api_key": resolved_token, "runtime_base_url": base_url}],
        source="hermes-auth-store",
        helper_callable=lambda **kwargs: pytest.fail("helper must not run on token rotation mismatch"),
        read_codex_tokens=lambda: {
            "tokens": {"account_id": "acct-mismatch", "access_token": stored_token},
        },
        resolve_callable=lambda: {
            "provider": "openai-codex",
            "base_url": base_url,
            "api_key": resolved_token,
            "source": "hermes-auth-store",
        },
    )

    handler = _FakeHandler({"force": False})
    routes.handle_post(handler, urlparse("/api/provider/openai-codex/reset"))
    payload = handler.payload()
    assert handler.status == 409
    assert payload["redemption"]["reason_code"] == "credential_target_changed"
    assert calls["helper"] == []
    assert resolved_token not in json.dumps(payload)
    assert stored_token not in json.dumps(payload)

    for key in set(providers._CODEX_RESET_REDEMPTION_LOCKS.keys()) - lock_keys_before:
        providers._CODEX_RESET_REDEMPTION_LOCKS.pop(key, None)


def test_codex_reset_route_allows_concurrent_requests_for_distinct_targets_across_profiles(tmp_path, monkeypatch):
    import api.profiles as profiles

    lock_keys_before = set(providers._CODEX_RESET_REDEMPTION_LOCKS.keys())
    calls_gate = threading.Event()
    release_gate = threading.Event()
    helper_entries = []
    helper_lock = threading.Lock()
    home_state = threading.local()

    @contextmanager
    def fake_profile_env_for_request(_path, logger_override=None):
        assert hasattr(home_state, "home"), "request profile home was not initialized"
        yield

    def fake_get_active_hermes_home():
        return home_state.home

    def helper(**kwargs):
        with helper_lock:
            helper_entries.append((kwargs["base_url"], kwargs["api_key"], kwargs["force"]))
            if len(helper_entries) == 2:
                calls_gate.set()
        release_gate.wait()
        return {
            "status": "reset",
            "redeemed": True,
            "message": "Reset redeemed.",
            "available_count": 0,
            "windows_reset": 0,
        }

    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"

    profile_to_target = {
        str(profile_a): ("https://runtime-a.example/v1", "token-a"),
        str(profile_b): ("https://runtime-b.example/v1", "token-b"),
    }

    def resolve():
        home_key = str(home_state.home)
        base_url, api_key = profile_to_target[home_key]
        return {
            "provider": "openai-codex",
            "base_url": base_url,
            "api_key": api_key,
            "source": "hermes-auth-store",
        }

    def pool_entries_for_profile():
        home_key = str(home_state.home)
        base_url, api_key = profile_to_target[home_key]
        return [{"runtime_api_key": api_key, "runtime_base_url": base_url}]

    def read_codex_tokens():
        if str(home_state.home) == str(profile_a):
            return {"tokens": {"account_id": "acct-a", "access_token": "token-a"}}
        return {"tokens": {"account_id": "acct-b", "access_token": "token-b"}}

    calls = _install_codex_reset_runtime_mocks(
        monkeypatch,
        resolved_base_url="unused",
        resolved_api_key="unused",
        resolve_callable=resolve,
        pool_entries=pool_entries_for_profile,
        helper_callable=helper,
        read_codex_tokens=read_codex_tokens,
    )

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(profiles, "profile_env_for_active_request", fake_profile_env_for_request)
    monkeypatch.setattr(profiles, "get_active_hermes_home", fake_get_active_hermes_home)
    monkeypatch.setattr(providers, "_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(
        providers,
        "get_provider_quota",
        lambda provider_id=None, refresh=False: {
            "ok": True,
            "provider": "openai-codex",
            "status": "available",
            "account_limits": {"banked_resets": {"available_count": 1}},
        },
    )

    results = {}
    errors = {}

    def run(profile_home: Path, key: str):
        try:
            home_state.home = profile_home
            handler = _FakeHandler({"force": False})
            routes.handle_post(handler, urlparse("/api/provider/openai-codex/reset"))
            results[key] = handler
        except Exception as exc:  # pragma: no cover - thread-safe assertion path
            errors[key] = exc
        finally:
            if hasattr(home_state, "home"):
                del home_state.home

    first = threading.Thread(target=run, args=(profile_a, "first"))
    second = threading.Thread(target=run, args=(profile_b, "second"))

    try:
        first.start()
        second.start()
        assert calls_gate.wait(1), "both helper calls did not begin"
        assert first.is_alive() and second.is_alive()
        release_gate.set()
        first.join(2)
        second.join(2)
        assert not first.is_alive()
        assert not second.is_alive()
        assert not errors
        assert "first" in results
        assert "second" in results
        assert results["first"].status == 200
        assert results["second"].status == 200
        assert {results["first"].payload()["redemption"]["state"], results["second"].payload()["redemption"]["state"]} == {"reset"}
        assert set(tuple(entry) for entry in calls["helper"]) == {
            ("https://runtime-a.example/v1", "token-a", False),
            ("https://runtime-b.example/v1", "token-b", False),
        }
        assert calls_gate.is_set()
    finally:
        release_gate.set()
        if hasattr(home_state, "home"):
            del home_state.home
        first.join(1)
        second.join(1)
        for key in set(providers._CODEX_RESET_REDEMPTION_LOCKS.keys()) - lock_keys_before:
            providers._CODEX_RESET_REDEMPTION_LOCKS.pop(key, None)



def test_codex_reset_frontend_flow_covers_render_confirm_busy_and_rerender():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the frontend behavior harness")

    script = f"""
(async()=>{{
const assert = (cond, msg) => {{ if (!cond) throw new Error(msg); }};
const t = (key, ...args) => {{
  const table = {{
    provider_quota_reset_busy: 'Redeeming…',
    provider_quota_reset_action: 'Redeem reset',
    provider_quota_reset_force_title: 'Redeem Codex reset?',
    provider_quota_reset_force_message: 'A full reset may be wasted because your current Codex window is not exhausted.',
    provider_quota_reset_confirm_message: 'Redeem this reset now?',
    provider_quota_reset_confirm: 'Redeem',
  }};
  let text = table[key] || key;
  args.forEach((arg, idx) => {{ text = text.replace(`{{${{idx}}}}`, String(arg)); }});
  return text;
}};
{_extract_function_source('_providerQuotaResetRequestForce')}
{_extract_function_source('_providerQuotaBankedResetState')}
{_extract_function_source('_parseProviderQuotaApiError')}
{_extract_function_source('_redeemProviderQuotaReset')}

const announcer = {{ textContent: '' }};
globalThis.$ = (id) => id === 'a11yAnnouncer' ? announcer : null;
globalThis.requestAnimationFrame = (callback) => callback();

const makeButton = () => ({{
  disabled: false,
  textContent: 'Redeem reset',
  attrs: {{}},
  setAttribute(k, v) {{ this.attrs[k] = v; }},
  removeAttribute(k) {{ delete this.attrs[k]; }},
}});
const makeCard = () => ({{
  isConnected: true,
  replaced: null,
  replaceWith(node) {{ this.replaced = node; }},
}});

const status = {{
  provider: 'openai-codex',
  account_limits: {{
    windows: [{{remaining_percent: 25}}, {{remaining_percent: 60}}],
    banked_resets: {{available_count: 1, redeemable: true, reason_code: null}},
  }},
}};
const exhaustedSingleton = {{
  provider: 'openai-codex',
  status: 'unavailable',
  account_limits: {{
    windows: [],
    pool: {{
      total_credentials: 1,
      exhausted_credentials: 1,
      credentials: [{{status: 'exhausted'}}],
    }},
  }},
}};

const pooled = _providerQuotaBankedResetState({{
  provider: 'openai-codex',
  account_limits: {{
    windows: [{{remaining_percent: 0}}, {{remaining_percent: 55}}],
    banked_resets: {{available_count: 3, redeemable: false, reason_code: 'ambiguous_pool', complete: false}},
    pool: {{total_credentials: 2}},
  }},
}});
assert(pooled.canRedeem === false, 'ambiguous pool must not redeem');

const pooledUnknown = _providerQuotaBankedResetState({{
  provider: 'openai-codex',
  account_limits: {{
    windows: [{{remaining_percent: null}}, {{remaining_percent: undefined}}],
    banked_resets: null,
    pool: {{total_credentials: 2}},
  }},
}});
assert(pooledUnknown.canRedeem === false, 'unknown pool count must not redeem');

assert(_providerQuotaResetRequestForce(status) === true, 'non-exhausted usage should require force');
assert(_providerQuotaResetRequestForce(exhaustedSingleton) === false, 'exhausted singleton should not require force');

let confirmations = [];
let lastRequest = null;
let lastPostAttempts = 0;
let builtResetButton = null;
let reconcileCalls = 0;

const postedStatus = {{
  provider: 'openai-codex',
  status: 'available',
  account_limits: {{
    windows: [{{remaining_percent: 90}}, {{remaining_percent: 90}}],
    banked_resets: {{available_count: 0, redeemable: true, reason_code: null}},
    pool: {{ total_credentials: 1 }},
  }},
}};

globalThis.showConfirmDialog = async (opts) => {{
  confirmations.push(opts.message);
  assert(opts.title === 'Redeem Codex reset?', 'confirm title mismatch');
  return true;
}};
globalThis.api = async (path, opts) => {{
  if(path !== '/api/provider/openai-codex/reset') throw new Error(`unexpected endpoint ${{path}}`);
  lastPostAttempts += 1;
  lastRequest = {{ path, opts }};
  return {{
    ...postedStatus,
    redemption: {{ state: 'reset', ok: true, message: 'Reset redeemed.' }},
  }};
}};
globalThis._fetchProviderQuotaStatus = async (refresh) => {{
  reconcileCalls += 1;
  return {{
    provider: 'openai-codex',
    status: 'available',
    account_limits: {{
      windows: [{{remaining_percent: 90}}, {{remaining_percent: 90}}],
      banked_resets: {{available_count: 0, redeemable: false, reason_code: null}},
      pool: {{ total_credentials: 1 }},
    }},
    redemption: {{ state: 'unknown', ok: false, message: 'reconciled unresolved outcome' }},
  }};
}};
globalThis._buildProviderQuotaCard = (next) => {{
  builtResetButton = makeButton();
  return {{
    isConnected: true,
    querySelector: (selector) => selector === '[data-provider-quota-reset]' ? builtResetButton : null,
  }};
}};

const statusButton = makeButton();
const statusCard = makeCard();
await _redeemProviderQuotaReset(statusCard, statusButton, status);
assert(confirmations.length === 1, 'non-exhausted flow must confirm');
assert(confirmations[0] === t('provider_quota_reset_force_message'), 'force confirmation expected');
assert(lastRequest.path === '/api/provider/openai-codex/reset', 'reset endpoint mismatch');
assert(JSON.parse(lastRequest.opts.body).force === true, 'force payload mismatch');
assert(lastRequest.opts.retries === 0, 'reset request must not retry');
assert(lastPostAttempts === 1, 'successful flow should issue one POST call');
assert(lastRequest.opts.timeoutMs === 90000, 'reset request timeout mismatch');
assert(announcer.textContent === 'Reset redeemed.', 'redemption should update the persistent announcer');
assert(statusCard.replaced && !statusCard.replaced.querySelector('[data-provider-quota-reset]').disabled, 'successful flow should keep card button enabled');

confirmations = [];
const exhaustedButton = makeButton();
const exhaustedCard = makeCard();
await _redeemProviderQuotaReset(exhaustedCard, exhaustedButton, exhaustedSingleton);
assert(confirmations.length === 1, 'exhausted flow should still confirm');
assert(confirmations[0] === t('provider_quota_reset_confirm_message'), 'non-force confirmation expected for exhausted status');
assert(confirmations[0].indexOf('may be wasted') === -1, 'exhausted status should not show waste warning');
assert(JSON.parse(lastRequest.opts.body).force === false, 'exhausted singleton should post force false');

let transportCalls = 0;
globalThis._fetchProviderQuotaStatus = async (refresh) => {{
  transportCalls += 1;
  return {{
    provider: 'openai-codex',
    status: 'unavailable',
    account_limits: {{
      windows: [{{remaining_percent: 0}}],
      banked_resets: {{available_count: 0, redeemable: false, reason_code: null}},
      pool: {{ total_credentials: 1 }},
    }},
    redemption: {{ state: 'unknown', ok: false, message: 'reconciliation left outcome unresolved' }},
  }};
}};
globalThis.api = async () => {{
  lastPostAttempts += 1;
  const err = new Error('transport outage');
  err.body = 'not json';
  throw err;
}};
lastPostAttempts = 0;
const reconcileButton = makeButton();
const reconcileCard = makeCard();
await _redeemProviderQuotaReset(reconcileCard, reconcileButton, status);
assert(transportCalls === 1, 'transport error should invoke quota status reconciliation');
assert(lastPostAttempts === 1, 'transport error path should issue one POST call');
assert(reconcileCard.replaced && reconcileCard.replaced.querySelector('[data-provider-quota-reset]').disabled === true, 'unresolved reconciliation should keep reset disabled');

const unknownOutcomeButton = makeButton();
const unknownOutcomeCard = makeCard();
let unknownOutcomeRequest = null;
globalThis.showConfirmDialog = async () => {{ return true; }};
globalThis.api = async (path, opts) => {{
  if(path !== '/api/provider/openai-codex/reset') throw new Error(`unexpected endpoint ${{path}}`);
  lastPostAttempts += 1;
  unknownOutcomeRequest = {{ path, opts }};
  return {{
    ...postedStatus,
    redemption: {{ state: 'unknown_outcome', ok: false, message: 'The Codex backend did not return a definitive redemption result.' }},
  }};
}};
globalThis._fetchProviderQuotaStatus = async () => null;
lastPostAttempts = 0;
await _redeemProviderQuotaReset(unknownOutcomeCard, unknownOutcomeButton, status);
assert(unknownOutcomeRequest.opts.retries === 0, 'unknown_outcome request must not retry');
assert(JSON.parse(unknownOutcomeRequest.opts.body).force === true, 'unknown_outcome follow-up should preserve forced intent');
assert(unknownOutcomeCard.replaced && unknownOutcomeCard.replaced.querySelector('[data-provider-quota-reset]').disabled === true, 'unknown_outcome should keep reset disabled');
assert(unknownOutcomeButton.disabled === true, 'unknown_outcome should disable stale button');
assert(unknownOutcomeButton.attrs['aria-busy'] === 'true', 'unknown_outcome should preserve stale button busy');
assert(unknownOutcomeButton.attrs['aria-disabled'] === 'true', 'unknown_outcome should disable stale button');
assert(lastPostAttempts === 1, 'unknown_outcome path should issue one POST call');

const abortOutcomeButton = makeButton();
const abortOutcomeCard = makeCard();
let abortAttempts = 0;
let abortReconcileAttempts = 0;
globalThis.showConfirmDialog = async () => true;
globalThis.api = async (path, opts) => {{
  if(path !== '/api/provider/openai-codex/reset') throw new Error(`unexpected endpoint ${{path}}`);
  abortAttempts += 1;
  const error = new Error('request timeout');
  error.name = 'AbortError';
  throw error;
}};
globalThis._fetchProviderQuotaStatus = async () => {{
  abortReconcileAttempts += 1;
  return null;
}};
lastPostAttempts = 0;
await _redeemProviderQuotaReset(abortOutcomeCard, abortOutcomeButton, status);
assert(abortAttempts === 1, 'abort/timeout path should issue one POST call');
assert(lastPostAttempts === 0, 'timeout path should not retry POST calls in this test');
assert(abortReconcileAttempts === 1, 'timeout path should reconcile once');
assert(abortOutcomeCard.replaced && abortOutcomeCard.replaced.querySelector('[data-provider-quota-reset]').disabled === true, 'timeout unknown outcome should keep reset disabled');
assert(abortOutcomeButton.disabled === true, 'timeout unknown outcome should disable stale button');
assert(abortOutcomeButton.attrs['aria-busy'] === 'true', 'timeout unknown outcome should keep stale aria busy');
assert(abortOutcomeButton.attrs['aria-disabled'] === 'true', 'timeout unknown outcome should set stale aria disabled');
const abortToastResetButton = abortOutcomeCard.replaced ? abortOutcomeCard.replaced.querySelector('[data-provider-quota-reset]') : null;
assert(abortToastResetButton && abortToastResetButton.attrs['aria-disabled'] === 'true', 'timeout unknown outcome should disable rebuilt button');
assert(announcer.textContent === t('provider_quota_reset_unknown_outcome'), 'unknown outcome feedback should persist');

const cancelButton = makeButton();
const cancelCard = makeCard();
globalThis.showConfirmDialog = async () => false;
globalThis.api = async () => {{ throw new Error('should not call api'); }};
await _redeemProviderQuotaReset(cancelCard, cancelButton, status);
assert(cancelButton.disabled === false, 'cancellation should clear busy state');
assert(cancelButton.textContent === 'Redeem reset', 'cancellation should restore original button label');
}})().catch((err)=>{{ console.error(err); process.exit(1); }});
"""

    subprocess.run([node, "-e", script], cwd=ROOT, check=True, capture_output=True, text=True)


def test_codex_reset_frontend_markup_uses_header_action_and_keeps_pool_counts():
    header_start = PANELS_JS.index("<div class=\"provider-quota-header\">")
    header_end = PANELS_JS.index("<div class=\"provider-quota-body\">", header_start)
    header = PANELS_JS[header_start:header_end]
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert "data-provider-quota-reset" in header
    assert 'class="provider-quota-refresh" type="button" data-provider-quota-refresh' in header
    assert 'class="provider-quota-refresh provider-quota-reset-btn" type="button" data-provider-quota-reset' in header
    assert "provider_quota_reset_action')+' ('+bankedResetState.count" in header
    assert "bankedResetHtml" not in PANELS_JS
    assert "_buildProviderQuotaPoolBreakdown(accountLimits)" in PANELS_JS
    assert "provider-quota-pool-note" in PANELS_JS
    assert "$('a11yAnnouncer')" in PANELS_JS
    assert "setAttribute('aria-live'" not in PANELS_JS
    reset_style = css[css.index('.provider-quota-refresh.provider-quota-reset-btn'):css.index('.provider-quota-body')]
    assert 'min-width:44px' in reset_style
    assert 'min-height:44px' in reset_style


def test_codex_reset_i18n_uses_english_fallback_keys_only():
    src = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    locale_pattern = re.compile(r"^\s{2}(?:[A-Za-z0-9_]+|'[^']+'):\s*\{$", re.MULTILINE)
    locale_keys = locale_pattern.findall(src)
    assert locale_keys

    from tests.test_provider_quota_locale_helpers import RESET_FALLBACK_KEYS

    reset_fallback_keys = RESET_FALLBACK_KEYS
    for key in reset_fallback_keys:
        assert src.count(f"{key}:") == 1, f"{key} should live only in LOCALES.en"
    assert src.count("provider_quota_resets_meta:") == len(locale_keys)
