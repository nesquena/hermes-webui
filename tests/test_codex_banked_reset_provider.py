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
CANONICAL_REDEEM_REQUEST_ID = "11111111-1111-4ccc-8ccc-111111111111"


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

    func_end_re = re.compile(r"\n(?:async\s+)?function\s+[A-Za-z0-9_$]+\s*\(", re.MULTILINE)
    body_start = PANELS_JS.find("{", start)
    assert body_start != -1, f"{name} missing function body"
    match = func_end_re.search(PANELS_JS, body_start + 1)
    if match:
        return PANELS_JS[start : match.start()]
    return PANELS_JS[start:]


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

    def fake_helper(*, base_url, api_key, account_id, redeem_request_id, force):
        calls["helper"].append((base_url, api_key, account_id, redeem_request_id, force))
        return helper_callable(
            base_url=base_url,
            api_key=api_key,
            account_id=account_id,
            redeem_request_id=redeem_request_id,
            force=force,
        )

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

    result = providers.redeem_codex_reset_credit_status(force=False, redeem_request_id=CANONICAL_REDEEM_REQUEST_ID)

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

    result = providers.redeem_codex_reset_credit_status(force=False, redeem_request_id=CANONICAL_REDEEM_REQUEST_ID)

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

    result = providers.redeem_codex_reset_credit_status(force=True, redeem_request_id=CANONICAL_REDEEM_REQUEST_ID)

    assert result["ok"] is True
    assert result["redemption"]["state"] == "reset"
    assert result["redemption"]["message"] == "Reset redeemed."
    assert calls["resolve"] == 1
    assert calls["load_pool"] == ["openai-codex"]
    assert calls["helper"] == [
        ("https://runtime.example/v1", "resolved-secret-token", None, CANONICAL_REDEEM_REQUEST_ID, True),
    ]


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

    result = providers.redeem_codex_reset_credit_status(force=False, redeem_request_id=CANONICAL_REDEEM_REQUEST_ID)

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

    mismatch = providers.redeem_codex_reset_credit_status(force=False, redeem_request_id=CANONICAL_REDEEM_REQUEST_ID)
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
    result = providers.redeem_codex_reset_credit_status(force=False, redeem_request_id=CANONICAL_REDEEM_REQUEST_ID)
    assert result["ok"] is True
    assert result["redemption"]["state"] == "reset"
    assert result["redemption"]["message"] == "Reset redeemed."
    assert calls["helper"] == [
        (
            "https://runtime.example/v2",
            "resolved-secret-token",
            None,
            CANONICAL_REDEEM_REQUEST_ID,
            False,
        )
    ]


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

    result = providers.redeem_codex_reset_credit_status(force=False, redeem_request_id=CANONICAL_REDEEM_REQUEST_ID)
    assert result["ok"] is True
    assert result["redemption"]["state"] == "reset"
    assert calls["helper"] == [
        (
            "https://runtime.example/v3",
            "resolved-secret-token",
            None,
            CANONICAL_REDEEM_REQUEST_ID,
            False,
        )
    ]


def test_redeem_codex_reset_helper_single_flight_blocks_concurrent_requests(monkeypatch, tmp_path):
    started = threading.Event()
    release = threading.Event()
    thread_result = {}

    def helper(**kwargs):
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
        worker = threading.Thread(target=lambda: thread_result.setdefault("first", providers.redeem_codex_reset_credit_status(force=False, redeem_request_id=CANONICAL_REDEEM_REQUEST_ID)))
        worker.start()
        assert started.wait(1), "reset helper did not start"

        concurrent = providers.redeem_codex_reset_credit_status(force=False, redeem_request_id=CANONICAL_REDEEM_REQUEST_ID)
        assert concurrent["http_status"] == 409
        assert concurrent["redemption"]["reason_code"] == "in_progress"
        assert calls["helper"] == [
            (
                "https://runtime.example/v1",
                "resolved-secret-token",
                None,
                CANONICAL_REDEEM_REQUEST_ID,
                False,
            )
        ]
        assert providers._CODEX_RESET_REDEMPTION_LOCKS.get(lock_key) is not None
        assert sum(1 for key in providers._CODEX_RESET_REDEMPTION_LOCKS if key == lock_key) == 1

        release.set()
        worker.join(1)
        first = thread_result["first"]

        assert first["http_status"] == 200
        assert first["redemption"]["state"] == "reset"
        assert calls["helper"] == [
            (
                "https://runtime.example/v1",
                "resolved-secret-token",
                None,
                CANONICAL_REDEEM_REQUEST_ID,
                False,
            )
        ]

        third = providers.redeem_codex_reset_credit_status(force=False, redeem_request_id=CANONICAL_REDEEM_REQUEST_ID)
        assert third["http_status"] == 200
        assert third["redemption"]["state"] == "reset"
        assert calls["helper"] == [
            (
                "https://runtime.example/v1",
                "resolved-secret-token",
                None,
                CANONICAL_REDEEM_REQUEST_ID,
                False,
            ),
            (
                "https://runtime.example/v1",
                "resolved-secret-token",
                None,
                CANONICAL_REDEEM_REQUEST_ID,
                False,
            ),
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

    keys, key_error, validated_account_id, _ = providers._codex_reset_target_lock_keys(
        credential={
            "source": "hermes-auth-store",
            "base_url": "https://runtime.example/v1",
            "api_key": "wrapped-access-token",
        }
    )
    assert key_error is None
    assert validated_account_id == "acct-stable-001"
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

    keys, key_error, _validated_account_id, _ = providers._codex_reset_target_lock_keys(
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

    keys, key_error, validated_account_id, _ = providers._codex_reset_target_lock_keys(
        credential={
            "source": "hermes-auth-store",
            "base_url": "https://runtime.example/v1",
            "api_key": "wrapped-access-token",
        }
    )
    assert keys == ()
    assert key_error == "credential_target_changed"
    assert validated_account_id is None


def test_codex_reset_target_lock_keys_fails_closed_for_invalid_auth_store_token_payload(monkeypatch):
    import sys
    import types

    hermes_cli_mod = types.ModuleType("hermes_cli")
    hermes_cli_mod.__path__ = []
    hermes_auth_mod = types.ModuleType("hermes_cli.auth")

    hermes_auth_mod._read_codex_tokens = lambda: {"tokens": object()}

    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_mod)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", hermes_auth_mod)

    keys, key_error, validated_account_id, _ = providers._codex_reset_target_lock_keys(
        credential={
            "source": "hermes-auth-store",
            "base_url": "https://runtime.example/v1",
            "api_key": "wrapped-access-token",
        }
    )
    assert keys == ()
    assert key_error == "unknown_account"
    assert validated_account_id is None


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

    keys, key_error, validated_account_id, _ = providers._codex_reset_target_lock_keys(
        credential={
            "source": "hermes-auth-store",
            "base_url": "https://runtime.example/v1",
            "api_key": "wrapped-access-token",
        }
    )
    assert keys == ()
    assert key_error == "unknown_account"
    assert "acct-leak" not in str(keys)
    assert validated_account_id is None


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

    keys, key_error, validated_account_id, _ = providers._codex_reset_target_lock_keys(
        credential={
            "source": "hermes-auth-store",
            "base_url": "https://runtime.example/v1",
            "api_key": "wrapped-access-token",
        }
    )
    assert key_error is None
    assert validated_account_id is None
    target_key = _target_lock_key(keys)
    assert target_key == f"target:{providers.hashlib.sha256('https://runtime.example/v1|wrapped-access-token'.encode('utf-8')).hexdigest()}"
    assert len(keys) == 1


def test_codex_reset_target_lock_key_is_target_only_for_non_auth_store_source(monkeypatch):
    keys, key_error, validated_account_id, _ = providers._codex_reset_target_lock_keys(
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

    auth_keys, auth_error, auth_validated_account_id, auth_scope = providers._codex_reset_target_lock_keys(
        credential={"source": "hermes-auth-store", "base_url": "https://runtime.example/v1", "api_key": "stable-token"}
    )
    pool_keys, pool_error, pool_validated_account_id, pool_scope = providers._codex_reset_target_lock_keys(
        credential={"source": "credential_pool", "base_url": "https://runtime.example/v1", "api_key": "stable-token"}
    )
    assert auth_error is None
    assert pool_error is None
    assert auth_validated_account_id == "acct-stable-004"
    assert pool_validated_account_id is None
    assert auth_scope != ""
    assert pool_scope != ""
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
    result = providers.redeem_codex_reset_credit_status(force=False, redeem_request_id=CANONICAL_REDEEM_REQUEST_ID)

    assert result["http_status"] == 200
    assert result["redemption"]["state"] == "not_exhausted"
    assert result["redemption"]["reason_code"] is None
    assert result["redemption"]["message"] == "Redeem is unnecessary."
    assert refreshes == [True]
    assert calls["helper"] == [
        (
            "https://runtime.example/v1",
            "resolved-secret-token",
            None,
            CANONICAL_REDEEM_REQUEST_ID,
            False,
        )
    ]


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

    result = providers.redeem_codex_reset_credit_status(force=False, redeem_request_id=CANONICAL_REDEEM_REQUEST_ID)

    assert result["ok"] is True
    assert result["quota_status"]["status"] == "unavailable"
    assert calls["helper"] == [
        (
            "https://runtime.example/v1",
            "resolved-secret-token",
            None,
            CANONICAL_REDEEM_REQUEST_ID,
            False,
        )
    ]


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

    result = providers.redeem_codex_reset_credit_status(force=False, redeem_request_id=CANONICAL_REDEEM_REQUEST_ID)

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
    assert helper_calls["helper"] == [
        (
            "https://runtime.example/v1",
            "resolved-secret-token",
            None,
            CANONICAL_REDEEM_REQUEST_ID,
            False,
        )
    ]
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

    result = providers.redeem_codex_reset_credit_status(force=False, redeem_request_id=CANONICAL_REDEEM_REQUEST_ID)

    assert result["ok"] is True
    assert result["http_status"] == 200
    assert result["redemption"]["state"] == "reset"
    assert result["redemption"]["message"] == "Reset redeemed."
    assert result["quota_status"]["status"] == "unavailable"
    assert "remaining reset count may be stale" in (result["quota_status"]["message"] or "")
    assert refreshes == [("openai-codex", True)]
    assert calls == ["openai-codex"]
    assert calls_helper["helper"] == [
        (
            "https://runtime.example/v1",
            "resolved-secret-token",
            None,
            CANONICAL_REDEEM_REQUEST_ID,
            False,
        )
    ]


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

    result = providers.redeem_codex_reset_credit_status(force=False, redeem_request_id=CANONICAL_REDEEM_REQUEST_ID)

    assert result["http_status"] == 200
    assert result["redemption"]["state"] == "unknown_outcome"
    assert result["redemption"]["reason_code"] == "unknown_outcome"
    assert result["redemption"]["message"] == (
        "The Codex backend did not return a definitive redemption result. "
        "The reset outcome is unknown; refresh account status before trying again."
    )
    assert "secret backend token" not in result["redemption"]["message"]
    assert "secret backend token" not in json.dumps(result).lower()
    assert calls["helper"] == [
        (
            "https://runtime.example/v1",
            "resolved-secret-token",
            None,
            CANONICAL_REDEEM_REQUEST_ID,
            False,
        )
    ]
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

    result = providers.redeem_codex_reset_credit_status(force=True, redeem_request_id=CANONICAL_REDEEM_REQUEST_ID)

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

    result = providers.redeem_codex_reset_credit_status(force="yes", redeem_request_id=CANONICAL_REDEEM_REQUEST_ID)

    assert result["ok"] is False
    assert result["http_status"] == 400
    assert result["redemption"]["reason_code"] == "invalid_force"
    assert helper_called == []


def test_codex_reset_route_validates_body_and_uses_profile_scope(monkeypatch):
    seen = {"entered": 0}
    allow_profile_scope = {"value": False}

    @contextmanager
    def fake_profile_env(path, logger_override=None):
        assert path == "/api/provider/openai-codex/reset"
        if allow_profile_scope["value"]:
            seen["entered"] += 1
        yield

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr("api.profiles.profile_env_for_active_request", fake_profile_env)
    def fake_route_redeem_codex_reset_credit_status(*, force=False, redeem_request_id=None):
        if redeem_request_id != CANONICAL_REDEEM_REQUEST_ID:
            return {
                "ok": False,
                "http_status": 400,
                "quota_status": {"provider": "openai-codex", "ok": False},
                "redemption": {
                    "ok": False,
                    "state": "failed",
                    "reason_code": "invalid_redeem_request_id",
                    "message": "redeem_request_id must be a canonical UUID",
                },
            }
        return {
            "ok": True,
            "http_status": 200,
            "quota_status": {"ok": True},
            "redemption": {"ok": True},
        }

    monkeypatch.setattr(routes, "redeem_codex_reset_credit_status", fake_route_redeem_codex_reset_credit_status)

    wrong_provider = _FakeHandler({"provider": "openai", "force": False})
    assert routes.handle_post(wrong_provider, urlparse("/api/provider/openai-codex/reset")) is True
    assert wrong_provider.status == 400
    assert "only force" in wrong_provider.payload()["error"]
    assert seen["entered"] == 0

    invalid_force = _FakeHandler({"force": "yes", "redeem_request_id": CANONICAL_REDEEM_REQUEST_ID})
    allow_profile_scope["value"] = False
    assert routes.handle_post(invalid_force, urlparse("/api/provider/openai-codex/reset")) is True
    assert invalid_force.status == 400
    assert "force" in invalid_force.payload()["error"]
    assert seen["entered"] == 0

    invalid_request_id = _FakeHandler({"force": False, "redeem_request_id": "not-a-canonical-uuid"})
    allow_profile_scope["value"] = False
    assert routes.handle_post(invalid_request_id, urlparse("/api/provider/openai-codex/reset")) is True
    assert invalid_request_id.status == 400
    assert invalid_request_id.payload()["redemption"]["reason_code"] == "invalid_redeem_request_id"
    assert invalid_request_id.payload()["redemption"]["message"] == "redeem_request_id must be a canonical UUID"
    assert seen["entered"] == 0

    for malformed in (None, [], "text"):
        malformed_handler = _FakeHandler(raw_body=malformed)
        allow_profile_scope["value"] = False
        assert routes.handle_post(malformed_handler, urlparse("/api/provider/openai-codex/reset")) is True
        assert malformed_handler.status == 400
        assert "JSON object" in malformed_handler.payload()["error"]
        assert seen["entered"] == 0

    ok = _FakeHandler({"force": False, "redeem_request_id": CANONICAL_REDEEM_REQUEST_ID})
    allow_profile_scope["value"] = True
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
            handler = _FakeHandler({"force": False, "redeem_request_id": CANONICAL_REDEEM_REQUEST_ID})
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
        assert calls["helper"] == [
            (base_url, token, "acct-shared-target", CANONICAL_REDEEM_REQUEST_ID, False)
        ]

        release_block.set()
        first.join(2)
        assert not first.is_alive(), "initial request blocked indefinitely"
        assert not errors
        assert "first" in results
        assert results["first"].status == 200
        assert results["first"].payload()["redemption"]["state"] == "reset"

        home_state.home = profile_b
        third = _FakeHandler({"force": False, "redeem_request_id": CANONICAL_REDEEM_REQUEST_ID})
        routes.handle_post(third, urlparse("/api/provider/openai-codex/reset"))
        if hasattr(home_state, "home"):
            del home_state.home

        assert third.status == 200
        assert third.payload()["redemption"]["state"] == "reset"
        assert calls["helper"] == [
            (base_url, token, "acct-shared-target", CANONICAL_REDEEM_REQUEST_ID, False),
            (base_url, token, None, CANONICAL_REDEEM_REQUEST_ID, False),
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
            handler = _FakeHandler({"force": False, "redeem_request_id": CANONICAL_REDEEM_REQUEST_ID})
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
        assert calls["helper"] == [(base_url, "resolved-token-a", account_id, CANONICAL_REDEEM_REQUEST_ID, False)]
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

    handler = _FakeHandler({"force": False, "redeem_request_id": CANONICAL_REDEEM_REQUEST_ID})
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
    release_gate = threading.Event()
    route_calls = []
    calls_gate = threading.Event()
    home_state = threading.local()

    @contextmanager
    def fake_profile_env_for_request(_path, logger_override=None):
        assert hasattr(home_state, "home"), "request profile home was not initialized"
        yield

    def fake_get_active_hermes_home():
        return home_state.home

    def helper(**kwargs):
        route_calls.append(
            (
                kwargs["base_url"],
                kwargs["api_key"],
                kwargs.get("account_id"),
                kwargs["redeem_request_id"],
                kwargs["force"],
            )
        )
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
            "source": "credential_pool",
        }

    def pool_entries_for_profile():
        home_key = str(home_state.home)
        base_url, api_key = profile_to_target[home_key]
        return [{"runtime_api_key": api_key, "runtime_base_url": base_url}]

    _install_codex_reset_runtime_mocks(
        monkeypatch,
        resolved_base_url="unused",
        resolved_api_key="unused",
        resolve_callable=resolve,
        pool_entries=pool_entries_for_profile,
        helper_callable=helper,
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
            handler = _FakeHandler({"force": False, "redeem_request_id": CANONICAL_REDEEM_REQUEST_ID})
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
        assert calls_gate.wait(1), "helper never started"
        assert first.is_alive()
        second.join(2)
        assert not second.is_alive()
        assert not errors
        assert "second" in results
        assert results["second"].status == 409
        assert results["second"].payload()["redemption"]["reason_code"] == "in_progress"
        assert route_calls == [("https://runtime-a.example/v1", "token-a", None, CANONICAL_REDEEM_REQUEST_ID, False)]

        release_gate.set()
        first.join(2)
        assert not first.is_alive()
        assert not second.is_alive()
        assert "first" in results
        assert results["first"].status == 200
        assert results["first"].payload()["redemption"]["state"] == "reset"

        home_state.home = profile_b
        retry = _FakeHandler({"force": False, "redeem_request_id": CANONICAL_REDEEM_REQUEST_ID})
        routes.handle_post(retry, urlparse("/api/provider/openai-codex/reset"))
        if hasattr(home_state, "home"):
            del home_state.home
        assert retry.status == 200
        assert retry.payload()["redemption"]["state"] == "reset"
        assert route_calls == [
            ("https://runtime-a.example/v1", "token-a", None, CANONICAL_REDEEM_REQUEST_ID, False),
            ("https://runtime-b.example/v1", "token-b", None, CANONICAL_REDEEM_REQUEST_ID, False),
        ]
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

    helper_functions = "\n".join(
        [
            _extract_function_source("_providerQuotaResetClassifyRedeemOutcome"),
            _extract_function_source("_providerQuotaResetSetButtonUnknownOutcome"),
            _extract_function_source("_providerQuotaResetScopeFromStatus"),
            _extract_function_source("_providerQuotaResetCanonicalizeRedeemRequestId"),
            _extract_function_source("_providerQuotaResetNormalizeScope"),
            _extract_function_source("_providerQuotaResetGenerateRequestId"),
            _extract_function_source("_providerQuotaResetNormalizeRecord"),
            _extract_function_source("_providerQuotaResetReadPendingRecords"),
            _extract_function_source("_providerQuotaResetWritePendingRecords"),
            _extract_function_source("_providerQuotaResetPendingRecordForScope"),
            _extract_function_source("_providerQuotaResetHasPendingForOtherScope"),
            _extract_function_source("_providerQuotaResetSetPendingRecord"),
            _extract_function_source("_providerQuotaResetClearPendingRecord"),
            _extract_function_source("_providerQuotaResetGetOrCreateRequestId"),
            _extract_function_source("_providerQuotaResetActionCountLabel"),
            _extract_function_source("_providerQuotaResetMaybeClearOnObservedDecrement"),
            _extract_function_source("_providerQuotaResetRequestForce"),
            _extract_function_source("_providerQuotaBankedResetState"),
            _extract_function_source("_parseProviderQuotaApiError"),
            _extract_function_source("_redeemProviderQuotaReset"),
            _extract_function_source("_buildProviderQuotaCard"),
        ]
    )

    script = (
        "(async()=>{\n"
        + helper_functions
        + "\n"
        "const assert = (cond, msg) => { if (!cond) throw new Error(msg); };\n"
        "const t = (key, ...args) => {\n"
        "  const table = {\n"
        "    provider_quota_reset_busy: 'Redeeming…',\n"
        "    provider_quota_reset_action: 'Redeem reset',\n"
        "    provider_quota_reset_action_count: 'Redeem resets ({0})',\n"
        "    provider_quota_reset_force_title: 'Redeem Codex reset?',\n"
        "    provider_quota_reset_force_message: 'A full reset may be wasted because your current Codex window is not exhausted.',\n"
        "    provider_quota_reset_confirm_message: 'Redeem this reset now?',\n"
        "    provider_quota_reset_confirm: 'Redeem',\n"
        "    provider_quota_reset_unknown_outcome: 'Unknown reset outcome',\n"
        "    provider_quota_reset_failed: 'Could not redeem reset',\n"
        "    provider_quota_status_available: 'available',\n"
        "    provider_quota_status_unavailable: 'unavailable',\n"
        "    provider_quota_title: 'Quota',\n"
        "    provider_quota_refresh_title: 'Refresh',\n"
        "    provider_quota_active_provider: 'Provider',\n"
        "    provider_quota_last_checked_after_refresh: 'checked',\n"
        "    provider_quota_window_fallback: 'Window',\n"
        "    provider_quota_account_limits_loaded: 'limits loaded',\n"
        "    provider_quota_unavailable: 'unavailable',\n"
        "    provider_quota_session_limit: 'Session',\n"
        "    provider_quota_weekly_limit: 'Weekly',\n"
        "    provider_quota_used_meta: 'used {0}',\n"
        "    provider_quota_resets_meta: 'resets {0}',\n"
        "    provider_quota_credential_pool: 'Credential pool',\n"
        "    provider_quota_metric_remaining: 'Remaining',\n"
        "    provider_quota_metric_used: 'Used',\n"
        "    provider_quota_metric_limit: 'Limit',\n"
        "    provider_quota_credential_label: 'Credential {0}',\n"
        "    provider_quota_pool_summary_available: '{0}/{1}',\n"
        "    provider_quota_pool_summary_exhausted: '{0} exhausted',\n"
        "    provider_quota_pool_summary_failed: '{0} failed',\n"
        "    provider_quota_pool_summary_checked: '{0} checked',\n"
        "    provider_quota_pool_plans: 'Plans: {0}',\n"
        "    provider_quota_pool_no_windows: 'No windows',\n"
        "  };\n"
        "  let text = table[key] || key;\n"
        "  args.forEach((arg, idx) => { text = text.replace(`{${idx}}`, String(arg)); });\n"
        "  return text;\n"
        "};\n"
        "globalThis.esc = (value) => {\n"
        "  if (value === undefined || value === null) return '';\n"
        "  return String(value);\n"
        "};\n"
        "const _PROVIDER_QUOTA_REDEMPTION_PENDING_KEY='hermes-provider-quota-reset-pending-v1';\n"
        "const _PROVIDER_QUOTA_REDEMPTION_PENDING_VERSION=1;\n"
        "const _PROVIDER_QUOTA_REDEMPTION_SCOPE_RE=/^[0-9a-f]{64}$/;\n"
        "const _CANONICAL_UUID_RE=/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;\n"
        "function _extractButtonFromMarkup(html, selector) {\n"
        "  const marker = selector === '[data-provider-quota-reset]' ? 'data-provider-quota-reset' : 'data-provider-quota-refresh';\n"
        "  const start = html.indexOf(marker);\n"
        "  if (start === -1) return null;\n"
        "  const buttonStart = html.lastIndexOf('<button', start);\n"
        "  if (buttonStart === -1) return null;\n"
        "  const buttonEnd = html.indexOf('</button>', buttonStart);\n"
        "  if (buttonEnd === -1) return null;\n"
        "  const segment = html.slice(buttonStart, buttonEnd + 9);\n"
        "  const textMatch = segment.match(/>([^<]*)<\\/button>/);\n"
        "  const raw = textMatch ? textMatch[1] : '';\n"
        "  const button = makeButton(raw);\n"
        "  button.disabled = /\\sdisabled(?:\\s|>)/.test(segment) || /\\sdisabled=/.test(segment);\n"
        "  const ariaDisabledMatch = segment.match(/aria-disabled=\"([^\"]*)\"/);\n"
        "  if (ariaDisabledMatch) button.attrs['aria-disabled'] = ariaDisabledMatch[1];\n"
        "  const ariaBusyMatch = segment.match(/aria-busy=\"([^\"]*)\"/);\n"
        "  if (ariaBusyMatch) button.attrs['aria-busy'] = ariaBusyMatch[1];\n"
        "  if (button.attrs['aria-disabled']) button.disabled = true;\n"
        "  if (button.disabled) button.attrs.disabled = true;\n"
        "  return button;\n"
        "}\n"
        "function FakeStorage(){\n"
        "  this.store = {};\n"
        "  this.setShouldFail = false;\n"
        "}\n"
        "FakeStorage.prototype.getItem = function(key){\n"
        "  return Object.prototype.hasOwnProperty.call(this.store, key) ? this.store[key] : null;\n"
        "};\n"
        "FakeStorage.prototype.setItem = function(key, value){\n"
        "  if (this.setShouldFail) throw new Error('storage write blocked');\n"
        "  this.store[key] = String(value);\n"
        "};\n"
        "FakeStorage.prototype.removeItem = function(key){ delete this.store[key]; };\n"
        "function FakeCard(){\n"
        "  this.isConnected = true;\n"
        "  this.replaced = null;\n"
        "  this._html = '';\n"
        "}\n"
        "function makeButton(text='Redeem reset'){\n"
        "  return {\n"
        "    disabled: false,\n"
        "    textContent: text,\n"
        "    attrs: {},\n"
        "    setAttribute(k, v){\n"
        "      this.attrs[k] = String(v);\n"
        "      if (k === 'disabled') this.disabled = true;\n"
        "    },\n"
        "    removeAttribute(k){\n"
        "      delete this.attrs[k];\n"
        "      if (k === 'disabled') this.disabled = false;\n"
        "    },\n"
        "    addEventListener(){},\n"
        "  };\n"
        "}\n"
        "FakeCard.prototype.replaceWith = function(node){ this.replaced = node; };\n"
        "FakeCard.prototype.querySelector = function(selector){\n"
        "  if (selector === '.provider-quota-pool') return null;\n"
        "  return _extractButtonFromMarkup(this._html, selector);\n"
        "};\n"
        "Object.defineProperty(FakeCard.prototype, 'innerHTML', {\n"
        "  set(value){ this._html = String(value || ''); },\n"
        "  get(){ return this._html; },\n"
        "});\n"
        "FakeCard.prototype.addEventListener = function(){};\n"
        "const fakeStorage = new FakeStorage();\n"
        "globalThis.localStorage = {\n"
        "  getItem: fakeStorage.getItem.bind(fakeStorage),\n"
        "  setItem: fakeStorage.setItem.bind(fakeStorage),\n"
        "  removeItem: fakeStorage.removeItem.bind(fakeStorage),\n"
        "};\n"
        "let confirmCalls = [];\n"
        "let postCalls = [];\n"
        "let fetchCalls = 0;\n"
        "let lastRenderedCard = null;\n"
        "const announcer = { textContent: '' };\n"
        "globalThis.$ = (id) => id === 'a11yAnnouncer' ? announcer : null;\n"
        "globalThis.requestAnimationFrame = (callback) => callback();\n"
        "globalThis.showToast = (msg) => { confirmCalls.push({ type: 'toast', msg }); };\n"
        "globalThis.renderProviderCostChart = () => {};\n"
        "globalThis.showConfirmDialog = async (opts) => {\n"
        "  confirmCalls.push(opts);\n"
        "  return true;\n"
        "};\n"
        "globalThis._fetchProviderQuotaStatus = async () => {\n"
        "  fetchCalls += 1;\n"
        "  return null;\n"
        "};\n"
        "globalThis.api = async (path, opts) => {\n"
        "  postCalls.push({ path, opts: JSON.parse(JSON.stringify(opts)) });\n"
        "  return {\n"
        "    status: 'available',\n"
        "    provider: 'openai-codex',\n"
        "    account_limits: {\n"
        "      windows: [{ remaining_percent: 90 }, { remaining_percent: 90 }],\n"
        "      banked_resets: { available_count: 0, redeemable: true, reason_code: null, redemption_scope: ('a'.repeat(64)) },\n"
        "      pool: { total_credentials: 1 },\n"
        "    },\n"
        "    redemption: { state: 'reset', ok: true, message: 'Reset redeemed.' },\n"
        "  };\n"
        "};\n"
        "globalThis.document = {\n"
        "  createElement: () => {\n"
        "    const card = new FakeCard();\n"
        "    card.querySelector = function(selector){\n"
        "      if (selector === '.provider-quota-pool') return null;\n"
        "      if (selector === '[data-provider-quota-refresh]') return _extractButtonFromMarkup(this._html, selector);\n"
        "      if (selector === '[data-provider-quota-reset]') return _extractButtonFromMarkup(this._html, selector);\n"
        "      return null;\n"
        "    };\n"
        "    return card;\n"
        "  },\n"
        "};\n"
        "globalThis._providerQuotaStatusLabel = (value) => String(value || 'unavailable');\n"
        "globalThis._formatProviderQuotaPercent = (value) => (value === undefined || value === null) ? '—' : String(value);\n"
        "globalThis._formatProviderQuotaReset = (value) => value ? String(value) : '';\n"
        "globalThis._formatProviderQuotaMoney = (value) => String(value == null ? '—' : value);\n"
        "globalThis._providerQuotaWindowMeta = (used, reset) => {\n"
        "  const out = [];\n"
        "  if (used !== '—') out.push(t('provider_quota_used_meta', used));\n"
        "  if (reset) out.push(t('provider_quota_resets_meta', reset));\n"
        "  return out;\n"
        "};\n"
        "globalThis._formatProviderQuotaWindowLabel = () => t('provider_quota_window_fallback');\n"
        "globalThis._providerQuotaRetryAfterText = (value) => value ? String(value) : '';\n"
        "globalThis._buildProviderQuotaPoolBreakdown = () => '';\n"
        "globalThis._formatProviderQuotaWindowLabel = () => t('provider_quota_window_fallback');\n"
        "globalThis._providerQuotaLastChecked = () => t('provider_quota_last_checked_after_refresh');\n"
        "globalThis._formatProviderQuotaLastChecked = () => t('provider_quota_last_checked_after_refresh');\n"
        "globalThis._providerQuotaRetryAfterText = (value) => String(value || '');\n"
        "globalThis._providerQuotaUnavailableReason = () => '';\n"
        "const _clone = (value) => JSON.parse(JSON.stringify(value));\n"
        "const canonicalScope = 'a'.repeat(64);\n"
        "const otherScope = 'b'.repeat(64);\n"
        "const idOne = '11111111-1111-4ccc-8ccc-111111111111';\n"
        "const idTwo = '22222222-2222-4ddd-8ddd-222222222222';\n"
        "const idThree = '33333333-3333-4eee-8eee-333333333333';\n"
        "const installCrypto = (ids, withRandomUUID = true) => {\n"
        "  const queue = ids.slice();\n"
        "  if (!withRandomUUID) {\n"
        "    const stub = { getRandomValues: () => {} };\n"
        "    try {\n"
        "      Object.defineProperty(globalThis, 'crypto', { value: stub, configurable: true, writable: true });\n"
        "    } catch (_) {\n"
        "      globalThis.crypto = stub;\n"
        "    }\n"
        "    return;\n"
        "  }\n"
        "  const stub = {\n"
        "    randomUUID: () => {\n"
        "      if (!queue.length) return idOne;\n"
        "      return queue.shift();\n"
        "    },\n"
        "    getRandomValues: () => {},\n"
        "  };\n"
        "  try {\n"
        "    Object.defineProperty(globalThis, 'crypto', { value: stub, configurable: true, writable: true });\n"
        "  } catch (_) {\n"
        "    globalThis.crypto = stub;\n"
        "  }\n"
        "};\n"
        "const pendingRaw = () => {\n"
        "  const raw = localStorage.getItem(_PROVIDER_QUOTA_REDEMPTION_PENDING_KEY);\n"
        "  return raw ? JSON.parse(raw) : null;\n"
        "};\n"
        "const pendingRecord = (scope) => {\n"
        "  const value = _providerQuotaResetPendingRecordForScope(scope);\n"
        "  return value;\n"
        "};\n"
        "const resetPostData = () => {\n"
        "  postCalls = [];\n"
        "  fetchCalls = 0;\n"
        "};\n"
        "const setPostResponse = (handler) => {\n"
        "  globalThis.api = async (path, opts) => {\n"
        "    postCalls.push({ path, opts: JSON.parse(JSON.stringify(opts)) });\n"
        "    return handler(path, opts);\n"
        "  };\n"
        "};\n"
        "const setConfirmResult = (result) => {\n"
        "  globalThis.showConfirmDialog = async (opts) => {\n"
        "    confirmCalls.push(opts);\n"
        "    return result;\n"
        "  };\n"
        "};\n"
        "const setReconcileResponse = (status) => {\n"
        "  globalThis._fetchProviderQuotaStatus = async () => {\n"
        "    fetchCalls += 1;\n"
        "    return status;\n"
        "  };\n"
        "};\n"
        "const baseStatus = {\n"
        "  provider: 'openai-codex',\n"
        "  status: 'available',\n"
        "  account_limits: {\n"
        "    windows: [{ remaining_percent: 25 }, { remaining_percent: 60 }],\n"
        "    banked_resets: { available_count: 3, redeemable: true, reason_code: null, redemption_scope: canonicalScope },\n"
        "    pool: { total_credentials: 1 },\n"
        "  },\n"
        "};\n"
        "const exhaustedSingleton = {\n"
        "  provider: 'openai-codex',\n"
        "  status: 'unavailable',\n"
        "  account_limits: {\n"
        "    windows: [],\n"
        "    banked_resets: { available_count: 1, redeemable: true, reason_code: null, redemption_scope: canonicalScope },\n"
        "    pool: {\n"
        "      total_credentials: 1,\n"
        "      exhausted_credentials: 1,\n"
        "      credentials: [{ status: 'exhausted' }],\n"
        "    },\n"
        "  },\n"
        "};\n"
        "globalThis._buildProviderQuotaCard = _buildProviderQuotaCard;\n"
        "\n"
        "assert(_providerQuotaResetRequestForce(baseStatus) === true, 'non-exhausted usage should require force');\n"
        "assert(_providerQuotaResetRequestForce(exhaustedSingleton) === false, 'exhausted singleton should not require force');\n"
        "assert(_providerQuotaResetActionCountLabel(3) === 'Redeem resets (3)', 'count label must be parameterized');\n"
        "\n"
        "const statusForScope = (scope, count, state='available') => _clone(baseStatus);\n"
        "\n"
        "installCrypto([idOne, idTwo, idThree]);\n"
        "setConfirmResult(false);\n"
        "setPostResponse(() => { throw new Error('api should not be called on cancel'); });\n"
        "const cancelButton = makeButton(_providerQuotaResetActionCountLabel(baseStatus.account_limits.banked_resets.available_count));\n"
        "const cancelCard = new FakeCard();\n"
        "await _redeemProviderQuotaReset(cancelCard, cancelButton, baseStatus);\n"
        "assert(confirmCalls.length === 1, 'cancel path must still show confirmation');\n"
        "assert(confirmCalls[0].message === t('provider_quota_reset_force_message'), 'forced confirmation required for non-exhausted status');\n"
        "assert(cancelButton.textContent === t('provider_quota_reset_action_count', 3), 'cancel should preserve counted label');\n"
        "assert(cancelButton.disabled === false, 'cancel should restore button enabled');\n"
        "assert(!('aria-busy' in cancelButton.attrs), 'cancel should clear aria-busy');\n"
        "assert(postCalls.length === 0, 'cancel should avoid posting');\n"
        "confirmCalls = [];\n"
        "fakeStorage.store = {};\n"
        "setReconcileResponse(null);\n"
        "setConfirmResult(true);\n"
        "\n"
        "assert(_providerQuotaResetNormalizeScope('A'.repeat(64)) === 'a'.repeat(64), 'scope should normalize to lowercase hex');\n"
        "assert(_providerQuotaResetNormalizeScope('not-a-scope') === '', 'malformed scope must be rejected');\n"
        "assert(_providerQuotaResetCanonicalizeRedeemRequestId(idOne) === idOne, 'canonical uuid should be preserved');\n"
        "assert(_providerQuotaResetCanonicalizeRedeemRequestId('ZZZZ1111-1111-4ccc-8ccc-111111111111') === '', 'non-canonical uuid should reject');\n"
        "const maliciousRecords = {\n"
        "  version: 1,\n"
        "  records: {\n"
        "    '__proto__': { request_id: idOne, available_count: 1, created_at: 1 },\n"
        "    'constructor': { request_id: idTwo, available_count: 1, created_at: 1 },\n"
        "    'toString': { request_id: idThree, available_count: 1, created_at: 1 },\n"
        "    [canonicalScope]: { request_id: idOne, available_count: 3, created_at: 10 },\n"
        "    ['g'.repeat(64)]: { request_id: idTwo, available_count: 2, created_at: 10 },\n"
        "    ['a'.repeat(64)]: { request_id: idTwo, available_count: -1, created_at: 10 },\n"
        "    ['A'.repeat(64)]: { request_id: idThree, available_count: 1, created_at: 10 },\n"
        "  },\n"
        "};\n"
        "fakeStorage.store[_PROVIDER_QUOTA_REDEMPTION_PENDING_KEY] = JSON.stringify(maliciousRecords);\n"
        "const repaired = _providerQuotaResetReadPendingRecords();\n"
        "assert(!Object.prototype.hasOwnProperty.call(repaired, '__proto__'), 'prototype keys should be discarded');\n"
        "assert(!Object.prototype.hasOwnProperty.call(repaired, 'constructor'), 'constructor key should be discarded');\n"
        "assert(!Object.prototype.hasOwnProperty.call(repaired, 'toString'), 'toString key should be discarded');\n"
        "assert(!Object.prototype.hasOwnProperty.call(repaired, 'g'.repeat(64)), 'invalid scope key should be discarded');\n"
        "assert(repaired[canonicalScope].request_id === idThree, 'uppercase canonical scope should collapse to canonical key');\n"
        "assert(Object.keys(repaired).length === 1, 'invalid entries should be pruned');\n"
        "fakeStorage.store = {};\n"
        "\n"
        "setPostResponse((path, opts) => {\n"
        "  if (path !== '/api/provider/openai-codex/reset') throw new Error(`unexpected endpoint ${path}`);\n"
        "  const body = JSON.parse(opts.body);\n"
        "  assert(opts.retries === 0, 'POST must set retries to 0');\n"
        "  const pre = _providerQuotaResetPendingRecordForScope(canonicalScope);\n"
        "  assert(pre && pre.request_id === body.redeem_request_id, 'pending request must be written before POST');\n"
        "  return {\n"
        "    provider: 'openai-codex',\n"
        "    status: 'available',\n"
        "    account_limits: {\n"
        "      windows: [{ remaining_percent: 90 }, { remaining_percent: 90 }],\n"
        "      banked_resets: { available_count: 0, redeemable: true, redemption_scope: canonicalScope },\n"
        "      pool: { total_credentials: 1 },\n"
        "    },\n"
        "    redemption: { state: 'reset', ok: true, message: 'Reset redeemed.' },\n"
        "  };\n"
        "});\n"
        "const successButton = makeButton(t('provider_quota_reset_action_count', 3));\n"
        "const successCard = new FakeCard();\n"
        "await _redeemProviderQuotaReset(successCard, successButton, baseStatus);\n"
        "assert(postCalls.length === 1, 'successful action should POST once');\n"
        "assert(postCalls[0].path === '/api/provider/openai-codex/reset', 'unexpected reset endpoint');\n"
        "assert(typeof postCalls[0].opts.body === 'string', 'request body must be stringified JSON');\n"
        "assert(JSON.parse(postCalls[0].opts.body).redeem_request_id === idOne, 'request must use tracked redeem id');\n"
        "assert(successButton.textContent === t('provider_quota_reset_busy'), 'successful submission should set in-flight button label');\n"
        "assert(postCalls[0].opts.retries === 0, 'successful path should also carry retries:0');\n"
        "assert(postCalls[0].opts.timeoutMs === 90000, 'timeout must be explicit 90000');\n"
        "assert(announcer.textContent === 'Reset redeemed.', 'announcer should reflect successful redemption message');\n"
        "assert(successCard.replaced, 'card must be rebuilt on successful response');\n"
        "const successRebuilt = successCard.replaced.querySelector('[data-provider-quota-reset]');\n"
        "assert(successRebuilt === null || !successRebuilt.disabled, 'successful path should not leave rebuilt button disabled');\n"
        "resetPostData();\n"
        "\n"
        "setPostResponse((path, opts) => {\n"
        "  if (path !== '/api/provider/openai-codex/reset') throw new Error(`unexpected endpoint ${path}`);\n"
        "  return {\n"
        "    provider: 'openai-codex',\n"
        "    status: 'available',\n"
        "    account_limits: {\n"
        "      windows: [{ remaining_percent: 90 }, { remaining_percent: 90 }],\n"
        "      banked_resets: { available_count: 0, redeemable: false, reason_code: null, redemption_scope: canonicalScope },\n"
        "      pool: { total_credentials: 1 },\n"
        "    },\n"
        "    redemption: { state: 'unknown_outcome', ok: false, message: t('provider_quota_reset_unknown_outcome') },\n"
        "  };\n"
        "});\n"
        "installCrypto([idOne]);\n"
        "fakeStorage.setShouldFail = false;\n"
        "const unknownButton = makeButton(t('provider_quota_reset_action_count', 3));\n"
        "const unknownCard = new FakeCard();\n"
        "await _redeemProviderQuotaReset(unknownCard, unknownButton, baseStatus);\n"
        "const pendingAfterUnknown = pendingRecord(canonicalScope);\n"
        "assert(unknownCard.replaced, 'unknown outcome must rebuild card');\n"
        "const rebuiltUnknownButton = unknownCard.replaced.querySelector('[data-provider-quota-reset]');\n"
        "assert(rebuiltUnknownButton && rebuiltUnknownButton.disabled === true, 'unknown outcome should disable rebuilt button');\n"
        "assert(rebuiltUnknownButton && rebuiltUnknownButton.attrs['aria-disabled'] === 'true', 'rebuilt unknown button should set aria-disabled');\n"
        "assert(unknownButton.disabled === true, 'old button should be disabled on unknown outcome');\n"
        "assert(unknownButton.attrs['aria-disabled'] === 'true', 'old button should be explicit aria-disabled');\n"
        "assert(!('aria-busy' in unknownButton.attrs), 'old button should clear stale aria-busy');\n"
        "assert(pendingAfterUnknown && pendingAfterUnknown.request_id === idOne, 'unknown outcome must retain pending request');\n"
        "assert(postCalls.length === 1, 'unknown outcome should not auto-post beyond the request');\n"
        "assert(_buildProviderQuotaCard({ ...baseStatus, status: 'available' }).querySelector('[data-provider-quota-reset]').attrs && !_buildProviderQuotaCard({ ...baseStatus, status: 'available' }).querySelector('[data-provider-quota-reset]').attrs['aria-busy'], 'rebuilt pending card should not render aria-busy');\n"
        "\n"
        "resetPostData();\n"
        "fakeStorage.store = {};\n"
        "setPostResponse((path, opts) => {\n"
        "  if (path !== '/api/provider/openai-codex/reset') throw new Error(`unexpected endpoint ${path}`);\n"
        "  return {\n"
        "    provider: 'openai-codex',\n"
        "    status: 'available',\n"
        "    account_limits: {\n"
        "      windows: [{ remaining_percent: 90 }, { remaining_percent: 90 }],\n"
        "      banked_resets: { available_count: 0, redeemable: true, redemption_scope: canonicalScope },\n"
        "      pool: { total_credentials: 1 },\n"
        "    },\n"
        "    redemption: { state: 'unknown_outcome', ok: false, message: 'Outcome unknown.' },\n"
        "  };\n"
        "});\n"
        "const firstReuseButton = makeButton(t('provider_quota_reset_action_count', 3));\n"
        "const firstReuseCard = new FakeCard();\n"
        "await _redeemProviderQuotaReset(firstReuseCard, firstReuseButton, baseStatus);\n"
        "const firstReusePayload = JSON.parse(postCalls[0].opts.body).redeem_request_id;\n"
        "const secondReuseButton = makeButton(t('provider_quota_reset_action_count', 3));\n"
        "const secondReuseCard = new FakeCard();\n"
        "await _redeemProviderQuotaReset(secondReuseCard, secondReuseButton, baseStatus);\n"
        "const secondReusePayload = JSON.parse(postCalls[1].opts.body).redeem_request_id;\n"
        "assert(firstReusePayload === idOne, 'explicit first POST should include deterministic id');\n"
        "assert(secondReusePayload === firstReusePayload, 're-invocation must reuse the persisted UUID');\n"
        "\n"
        "fakeStorage.store = {};\n"
        "assert(_providerQuotaResetSetPendingRecord(canonicalScope, idOne, 4) === true, 'set initial pending');\n"
        "assert(_providerQuotaResetSetPendingRecord(canonicalScope, idTwo, 4) === true, 'overwrite with newer pending id');\n"
        "assert(_providerQuotaResetClearPendingRecord(canonicalScope, idOne) === false, 'stale/mismatched id cannot clear newer record');\n"
        "assert(_providerQuotaResetClearPendingRecord(canonicalScope, idTwo) === true, 'matching id should clear pending');\n"
        "assert(_providerQuotaResetPendingRecordForScope(canonicalScope) === null, 'matching clear should remove record');\n"
        "assert(_providerQuotaResetSetPendingRecord(canonicalScope, idTwo, 4) === true, 'reseed pending for cross-scope test');\n"
        "assert(_providerQuotaResetSetPendingRecord(otherScope, idThree, 4) === true, 'seed another scope');\n"
        "assert(_providerQuotaResetClearPendingRecord(canonicalScope, idThree) === false, 'scope mismatch should not clear other scope record');\n"
        "assert(_providerQuotaResetPendingRecordForScope(canonicalScope).request_id === idTwo, 'scope mismatch should preserve original record');\n"
        "assert(_providerQuotaResetPendingRecordForScope(otherScope).request_id === idThree, 'other scope record must remain');\n"
        "assert(_providerQuotaResetClearPendingRecord(otherScope, idThree) === true, 'other scope clear removes that scope only');\n"
        "\n"
        "fakeStorage.store = {};\n"
        "assert(_providerQuotaResetSetPendingRecord(canonicalScope, idOne, 5) === true, 'seed observed decrement test');\n"
        "const staleLower = {\n"
        "  ...baseStatus,\n"
        "  account_limits: { ...baseStatus.account_limits, banked_resets: { available_count: 3, redeemable: true, reason_code: null, redemption_scope: canonicalScope } },\n"
        "  redemption: { state: 'reset', ok: true, message: 'lowered' },\n"
        "};\n"
        "assert(_providerQuotaResetMaybeClearOnObservedDecrement(staleLower) === true, 'lower available count should clear pending');\n"
        "assert(_providerQuotaResetPendingRecordForScope(canonicalScope) === null, 'pending should be cleared after observed decrement');\n"
        "assert(_providerQuotaResetSetPendingRecord(canonicalScope, idOne, 5) === true, 'reseed for non-actionable unknown/conflict test');\n"
        "const conflict = {\n"
        "  ...baseStatus,\n"
        "  redemption: { state: 'conflict', ok: false, message: 'conflict' },\n"
        "  account_limits: { ...baseStatus.account_limits, banked_resets: { available_count: 3, redeemable: true, reason_code: null, redemption_scope: canonicalScope } },\n"
        "};\n"
        "assert(_providerQuotaResetMaybeClearOnObservedDecrement(conflict) === false, 'conflict should not clear pending');\n"
        "assert(_providerQuotaResetPendingRecordForScope(canonicalScope) !== null, 'conflict should retain pending');\n"
        "\n"
        "setPostResponse(() => {\n"
        "  return {\n"
        "    provider: 'openai-codex',\n"
        "    status: 'available',\n"
        "    account_limits: {\n"
        "      windows: [{ remaining_percent: 90 }, { remaining_percent: 90 }],\n"
        "      banked_resets: { available_count: 5, redeemable: true, redemption_scope: canonicalScope },\n"
        "      pool: { total_credentials: 1 },\n"
        "    },\n"
        "    redemption: {},\n"
        "  };\n"
        "});\n"
        "const malformedButton = makeButton(t('provider_quota_reset_action_count', 2));\n"
        "const malformedCard = new FakeCard();\n"
        "fakeStorage.store = {};\n"
        "await _redeemProviderQuotaReset(malformedCard, malformedButton, baseStatus);\n"
        "const malformedPayload = JSON.parse(postCalls[0].opts.body).redeem_request_id;\n"
        "assert(_providerQuotaResetGetOrCreateRequestId(canonicalScope, baseStatus) === malformedPayload, 'get-or-create should reuse existing record id after malformed response');\n"
        "assert(_providerQuotaResetPendingRecordForScope(canonicalScope) !== null, 'malformed redemption should retain pending');\n"
        "assert(malformedButton.disabled === true, 'malformed redemption must settle as unknown and disable button');\n"
        "assert(malformedButton.attrs['aria-disabled'] === 'true', 'malformed settlement must set aria-disabled');\n"
        "resetPostData();\n"
        "\n"
        "setPostResponse(() => ({\n"
        "  provider: 'openai-codex',\n"
        "  status: 'available',\n"
        "  account_limits: {\n"
        "    windows: [{ remaining_percent: 90 }, { remaining_percent: 90 }],\n"
        "    banked_resets: { available_count: 4, redeemable: true, redemption_scope: canonicalScope },\n"
        "    pool: { total_credentials: 1 },\n"
        "  },\n"
        "  redemption: { state: 'in_progress', ok: false, message: 'still running' },\n"
        "}));\n"
        "const conflictButton = makeButton(t('provider_quota_reset_action_count', 2));\n"
        "const conflictCard = new FakeCard();\n"
        "fakeStorage.store = {};\n"
        "await _redeemProviderQuotaReset(conflictCard, conflictButton, baseStatus);\n"
        "assert(conflictButton.disabled === true, 'in_progress redemption must settle unknown and disable');\n"
        "assert(conflictButton.attrs['aria-disabled'] === 'true', 'in_progress should set aria-disabled');\n"
        "assert(_providerQuotaResetPendingRecordForScope(canonicalScope) !== null, 'in_progress should retain pending record');\n"
        "\n"
        "fakeStorage.store = {};\n"
        "_providerQuotaResetSetPendingRecord(canonicalScope, idOne, 4);\n"
        "assert(_providerQuotaResetHasPendingForOtherScope(otherScope) === true, 'another scope pending should block status other scope');\n"
        "const blockButton = makeButton(t('provider_quota_reset_action_count', 3));\n"
        "const blockCard = new FakeCard();\n"
        "const blockedStatus = { ...baseStatus, account_limits: { ...baseStatus.account_limits, banked_resets: { ...baseStatus.account_limits.banked_resets, redemption_scope: otherScope } } };\n"
        "setPostResponse(() => { throw new Error('post should be blocked by pending state'); });\n"
        "const blockPostCountBefore = postCalls.length;\n"
        "await _redeemProviderQuotaReset(blockCard, blockButton, blockedStatus);\n"
        "assert(blockButton.disabled === true, 'blocked pending should disable button');\n"
        "assert(blockButton.attrs['aria-disabled'] === 'true', 'blocked pending should set aria-disabled');\n"
        "assert(postCalls.length === blockPostCountBefore, 'pending-other-scope should block POST');\n"
        "const renderingBlockStatus = { ...baseStatus, account_limits: { windows: [{ remaining_percent: 25 }, { remaining_percent: 60 }], banked_resets: { available_count: 2, redeemable: true }, pool: { total_credentials: 1 } } };\n"
        "const renderingBlockCard = _buildProviderQuotaCard(renderingBlockStatus);\n"
        "const renderingBlockButton = renderingBlockCard && renderingBlockCard.querySelector('[data-provider-quota-reset]');\n"
        "assert(renderingBlockButton && renderingBlockButton.disabled === true, 'missing scope with active pending must render disabled control');\n"
        "assert(renderingBlockButton.attrs['aria-disabled'] === 'true', 'missing-scope blocked rendering must set aria-disabled');\n"
        "\n"
        "const unknownScopeStatus = { provider: 'openai-codex', status: 'available', account_limits: { windows: [{ remaining_percent: 10 }], banked_resets: { available_count: 2, redeemable: true }, pool: { total_credentials: 1 } } };\n"
        "const missingScopeButton = makeButton(t('provider_quota_reset_action_count', 2));\n"
        "const missingScopeCard = new FakeCard();\n"
        "const missingScopePostCountBefore = postCalls.length;\n"
        "await _redeemProviderQuotaReset(missingScopeCard, missingScopeButton, unknownScopeStatus);\n"
        "assert(postCalls.length === missingScopePostCountBefore, 'missing scope + active pending should block POST');\n"
        "assert(missingScopeButton.disabled === true, 'missing scope blocked action should disable control');\n"
        "\n"
        "const nonCodexStatus = {\n"
        "  provider: 'openrouter',\n"
        "  status: 'available',\n"
        "  account_limits: {\n"
        "    windows: [{ remaining_percent: 50 }],\n"
        "    banked_resets: { available_count: 2, redeemable: true, reason_code: null },\n"
        "    pool: { total_credentials: 1 },\n"
        "  },\n"
        "  redemption: { state: 'unknown', ok: false, message: 'should-not-render' },\n"
        "};\n"
        "const nonCodexCard = _buildProviderQuotaCard(nonCodexStatus);\n"
        "assert(nonCodexCard.querySelector('[data-provider-quota-reset]') === null, 'non-Codex provider should never render reset button');\n"
        "assert(nonCodexCard.innerHTML.indexOf('should-not-render') === -1, 'non-Codex provider should not render reset feedback');\n"
        "\n"
        "fakeStorage.store = {};\n"
        "delete globalThis.crypto;\n"
        "setConfirmResult(true);\n"
        "setPostResponse(() => { throw new Error('should not call api when secure crypto missing'); });\n"
        "const noCryptoButton = makeButton(t('provider_quota_reset_action_count', 2));\n"
        "const noCryptoCard = new FakeCard();\n"
        "const noCryptoPostCountBefore = postCalls.length;\n"
        "await _redeemProviderQuotaReset(noCryptoCard, noCryptoButton, baseStatus);\n"
        "assert(noCryptoButton.disabled === false, 'missing crypto should fail closed and restore button state');\n"
        "assert(postCalls.length === noCryptoPostCountBefore, 'missing crypto should not POST');\n"
        "\n"
        "installCrypto([idOne]);\n"
        "fakeStorage.store = {};\n"
        "fakeStorage.setShouldFail = true;\n"
        "setPostResponse(() => { throw new Error('should not call api when storage write fails'); });\n"
        "const noWriteButton = makeButton(t('provider_quota_reset_action_count', 2));\n"
        "const noWriteCard = new FakeCard();\n"
        "const noWritePostCountBefore = postCalls.length;\n"
        "await _redeemProviderQuotaReset(noWriteCard, noWriteButton, baseStatus);\n"
        "assert(noWriteButton.disabled === false, 'storage write failure should fail closed and restore button state');\n"
        "assert(postCalls.length === noWritePostCountBefore, 'storage write failure should not POST');\n"
        "fakeStorage.setShouldFail = false;\n"
        "\n"
        "const unknownRenderStatus = {\n"
        "  provider: 'openai-codex',\n"
        "  status: 'available',\n"
        "  account_limits: {\n"
        "    windows: [{ remaining_percent: 25 }, { remaining_percent: 60 }],\n"
        "    banked_resets: { available_count: 1, redeemable: true, redemption_scope: canonicalScope },\n"
        "    pool: { total_credentials: 1 },\n"
        "  },\n"
        "};\n"
        "setPostResponse(() => ({\n"
        "  status: 'available',\n"
        "  provider: 'openai-codex',\n"
        "  account_limits: unknownRenderStatus.account_limits,\n"
        "  redemption: { state: 'unknown', ok: false, message: t('provider_quota_reset_unknown_outcome') },\n"
        "}));\n"
        "const staleButton = makeButton(t('provider_quota_reset_action_count', 1));\n"
        "const staleCard = new FakeCard();\n"
        "await _redeemProviderQuotaReset(staleCard, staleButton, unknownRenderStatus);\n"
        "globalThis._buildProviderQuotaCard = () => null;\n"
        "const nullButton = makeButton(t('provider_quota_reset_action_count', 1));\n"
        "const nullCard = new FakeCard();\n"
        "await _redeemProviderQuotaReset(nullCard, nullButton, unknownRenderStatus);\n"
        "assert(nullButton.disabled === true, 'null card branch must disable stale button');\n"
        "assert(nullButton.attrs['aria-disabled'] === 'true', 'null card branch must set aria-disabled');\n"
        "assert(!('aria-busy' in nullButton.attrs), 'null card branch must clear aria-busy');\n"
        "globalThis._buildProviderQuotaCard = (next) => { throw new Error('card exploded'); };\n"
        "const throwButton = makeButton(t('provider_quota_reset_action_count', 1));\n"
        "const throwCard = new FakeCard();\n"
        "await _redeemProviderQuotaReset(throwCard, throwButton, unknownRenderStatus);\n"
        "assert(throwButton.disabled === true, 'throwing card branch must disable stale button');\n"
        "assert(throwButton.attrs['aria-disabled'] === 'true', 'throwing card branch must set aria-disabled');\n"
        "assert(!('aria-busy' in throwButton.attrs), 'throwing card branch must clear aria-busy');\n"
        "globalThis._buildProviderQuotaCard = _buildProviderQuotaCard;\n"
        "\n"
        "setConfirmResult(true);\n"
        "assert(_providerQuotaResetSetPendingRecord(canonicalScope, idOne, 1) === true, 'seed for explicit confirmation assertion');\n"
        "setPostResponse(() => {\n"
        "  return {\n"
        "    status: 'available',\n"
        "    provider: 'openai-codex',\n"
        "    account_limits: {\n"
        "      windows: [{ remaining_percent: 90 }, { remaining_percent: 90 }],\n"
        "      banked_resets: { available_count: 0, redeemable: true, redemption_scope: canonicalScope },\n"
        "      pool: { total_credentials: 1 },\n"
        "    },\n"
        "    redemption: { state: 'conflict', ok: false, message: 'conflict' },\n"
        "  };\n"
        "});\n"
        "const confirmCheckButton = makeButton(t('provider_quota_reset_action_count', 1));\n"
        "const confirmCheckCard = new FakeCard();\n"
        "const startConfirmCount = confirmCalls.filter((entry) => entry.type !== 'toast').length;\n"
        "await _redeemProviderQuotaReset(confirmCheckCard, confirmCheckButton, baseStatus);\n"
        "const confirmedActions = confirmCalls.filter((entry) => entry.type !== 'toast');\n"
        "assert(confirmedActions.length === startConfirmCount + 1, 'every redeem action should prompt confirmation');\n"
        "assert(confirmedActions[startConfirmCount].message === t('provider_quota_reset_force_message'), 'forced redemption should show waste warning');\n"
        "installCrypto([idOne]);\n"
        "setConfirmResult(true);\n"
        "setPostResponse(() => {\n"
        "  return {\n"
        "    status: 'available',\n"
        "    provider: 'openai-codex',\n"
        "    account_limits: {\n"
        "      windows: [{ remaining_percent: 0 }, { remaining_percent: 60 }],\n"
        "      banked_resets: { available_count: 0, redeemable: true, redemption_scope: canonicalScope },\n"
        "      pool: { total_credentials: 1 },\n"
        "    },\n"
        "    redemption: { state: 'reset', ok: false, message: 'done' },\n"
        "  };\n"
        "});\n"
        "const exhaustedCheckButton = makeButton(t('provider_quota_reset_action_count', 1));\n"
        "const exhaustedCheckCard = new FakeCard();\n"
        "await _redeemProviderQuotaReset(exhaustedCheckCard, exhaustedCheckButton, exhaustedSingleton);\n"
        "assert(confirmCalls[confirmCalls.length - 1].message === t('provider_quota_reset_confirm_message'), 'non-force confirmation should omit waste warning');\n"
        "const lastPost = postCalls[postCalls.length - 1];\n"
        "assert(lastPost && lastPost.opts && typeof lastPost.opts.body === 'string', 'non-force redemption should POST once');\n"
        "assert(JSON.parse(lastPost.opts.body).force === false, 'non-force redemption should send force=false');\n"
        "})()\n"
        ".catch((err) => { console.error(err); process.exit(1); });\n"
    )

    subprocess.run([node, "-e", script], cwd=ROOT, check=True, capture_output=True, text=True)


def test_codex_reset_frontend_markup_uses_header_action_and_keeps_pool_counts():
    header_start = PANELS_JS.index("<div class=\"provider-quota-header\">")
    header_end = PANELS_JS.index("<div class=\"provider-quota-body\">", header_start)
    header = PANELS_JS[header_start:header_end]
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert "data-provider-quota-reset" in header
    assert 'class="provider-quota-refresh" type="button" data-provider-quota-refresh' in header
    assert 'class="provider-quota-refresh provider-quota-reset-btn" type="button" data-provider-quota-reset' in header
    assert "_providerQuotaResetActionCountLabel(resetCount)" in header
    assert "aria-busy" not in header
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
