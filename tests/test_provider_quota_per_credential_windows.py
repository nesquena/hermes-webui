"""Per-credential quota windows for /api/provider/quota (card #14527).

A credential pool can hold several OpenCode Go plans. The top-level
``account_limits.windows`` stays the aggregate the composer chip reads; the
additive ``credentials`` list carries one entry per credential so the Settings
panel can show every plan. One credential must answer exactly as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.config as config
import api.profiles as profiles

ROOT = Path(__file__).resolve().parents[1]

_ENV_KEYS = ("OPENCODE_GO_API_KEY", "OPENCODE_API_KEY")
_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
_AGGREGATE_KEY = "private-aggregate-key"
_CRED_A = "private-credential-a"
_CRED_B = "private-credential-b"


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        return self._payload if size is None or size < 0 else self._payload[:size]


def _with_config(model=None, providers=None):
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_path = getattr(config, "_cfg_path", None)
    config.cfg.clear()
    config.cfg["model"] = model or {}
    if providers is not None:
        config.cfg["providers"] = providers
    try:
        config._cfg_mtime = Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    config._cfg_path = config._get_config_path()
    return old_cfg, (old_mtime, old_path)


def _restore_config(old_cfg, old_mtime):
    if isinstance(old_mtime, tuple):
        old_mtime, old_path = old_mtime
    else:
        old_path = getattr(config, "_cfg_path", None)
    config.cfg.clear()
    config.cfg.update(old_cfg)
    config._cfg_mtime = old_mtime
    config._cfg_path = old_path


def _entry(label, token, *, entry_id=None, status="ok"):
    return {
        "id": entry_id or ("id-" + label),
        "label": label,
        "runtime_api_key": token,
        "last_status": status,
    }


def _opener(responses, calls):
    """An opener that answers by bearer token; a missing token raises a network error."""

    def open(req, timeout=None):
        auth = req.headers.get("Authorization")
        calls.append({
            "url": req.full_url,
            "authorization": auth,
            "session": req.headers.get("X-opencode-session"),
        })
        body = responses.get(auth)
        if body is None:
            raise OSError("probe unavailable")
        return _FakeResponse(json.dumps(body).encode("utf-8"))

    return open


@pytest.fixture(autouse=True)
def _fresh_caches():
    """Both caches are process-wide; never let one test's payload reach another."""
    import api.providers as providers

    config._CREDENTIAL_POOL_CACHE.clear()
    providers._POOL_USAGE_CACHE.clear()
    yield
    config._CREDENTIAL_POOL_CACHE.clear()
    providers._POOL_USAGE_CACHE.clear()


def _quota(monkeypatch, tmp_path, *, pool_payloads, responses, key=_AGGREGATE_KEY):
    """Run get_provider_quota() for opencode-go with a pinned pool and probe responses."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    for env_var in _ENV_KEYS:
        monkeypatch.delenv(env_var, raising=False)
    if key:
        monkeypatch.setenv("OPENCODE_GO_API_KEY", key)
    old_cfg, old_mtime = _with_config(model={"provider": "opencode-go"})
    import api.providers as providers

    real_env_var_for = providers._provider_env_var_for
    monkeypatch.setattr(
        providers, "_provider_env_var_for",
        lambda pid: "OPENCODE_GO_API_KEY" if pid == "opencode-go" else real_env_var_for(pid),
    )
    calls = []
    monkeypatch.setattr(providers, "_BEARER_QUOTA_OPENER", SimpleNamespace(open=_opener(responses, calls)))
    if isinstance(pool_payloads, Exception):
        def _fail(_provider):
            raise pool_payloads

        monkeypatch.setattr(providers, "_pool_entry_payloads", _fail)
    else:
        monkeypatch.setattr(providers, "_pool_entry_payloads", lambda _provider: list(pool_payloads))
    try:
        result = providers.get_provider_quota()
    finally:
        _restore_config(old_cfg, old_mtime)
    return result, calls


_AGGREGATE_RESPONSE = {"usage": {"weekly": {"percent": 30}}}
_CRED_A_RESPONSE = {
    "usage": {
        "rolling": {"percent": 40, "status": "ok", "resetsAt": "2030-03-17T17:30:00Z"},
        "weekly": {"percent": 70, "status": "ok", "resetsAt": "2030-03-24T12:30:00Z"},
    },
}
_CRED_B_RESPONSE = {"usage": {"weekly": {"percent": 10}}, "plan": "Team"}


def test_single_credential_payload_is_unchanged(monkeypatch, tmp_path):
    """One configured credential keeps the exact single-window payload (no credentials key)."""
    result, calls = _quota(
        monkeypatch, tmp_path,
        pool_payloads=[_entry("only", _CRED_A)],
        responses={
            "Bearer " + _AGGREGATE_KEY: _AGGREGATE_RESPONSE,
        },
    )

    assert "credentials" not in result
    assert [w["label"] for w in result["account_limits"]["windows"]] == ["Weekly"]
    assert result["account_limits"]["pool"]["total_credentials"] == 1
    assert result["account_limits"]["pool"]["credentials"][0]["windows"] == []
    # A one-credential pool is not probed: only the aggregate usage call went out.
    assert [call["authorization"] for call in calls] == ["Bearer " + _AGGREGATE_KEY]


def test_two_credentials_get_aggregate_plus_per_credential_windows(monkeypatch, tmp_path):
    result, calls = _quota(
        monkeypatch, tmp_path,
        pool_payloads=[_entry("primary", _CRED_A), _entry("backup", _CRED_B)],
        responses={
            "Bearer " + _AGGREGATE_KEY: _AGGREGATE_RESPONSE,
            "Bearer " + _CRED_A: _CRED_A_RESPONSE,
            "Bearer " + _CRED_B: _CRED_B_RESPONSE,
        },
    )

    # The aggregate is untouched: the chip still reads this exact window.
    assert [w["label"] for w in result["account_limits"]["windows"]] == ["Weekly"]
    assert result["account_limits"]["windows"][0]["remaining_percent"] == 70.0

    assert [c["label"] for c in result["credentials"]] == ["primary", "backup"]
    first, second = result["credentials"]
    assert [w["label"] for w in first["windows"]] == ["5-hour", "Weekly"]
    assert [w["remaining_percent"] for w in first["windows"]] == [60.0, 30.0]
    assert first["plan"] == "primary"
    assert second["plan"] == "Team"
    assert [w["remaining_percent"] for w in second["windows"]] == [90.0]

    # Every pool probe carries the mandatory session header and hits the usage URL.
    pool_calls = [call for call in calls if call["authorization"] != "Bearer " + _AGGREGATE_KEY]
    assert len(pool_calls) == 2
    assert all(call["url"] == _USAGE_URL for call in pool_calls)
    assert all(call["session"] for call in pool_calls)


def test_credentials_payload_never_contains_tokens(monkeypatch, tmp_path):
    result, _calls = _quota(
        monkeypatch, tmp_path,
        pool_payloads=[_entry("primary", _CRED_A), _entry("backup", _CRED_B)],
        responses={
            "Bearer " + _AGGREGATE_KEY: _AGGREGATE_RESPONSE,
            "Bearer " + _CRED_A: _CRED_A_RESPONSE,
            "Bearer " + _CRED_B: _CRED_B_RESPONSE,
        },
    )

    serialized = json.dumps(result)
    for secret in (_AGGREGATE_KEY, _CRED_A, _CRED_B):
        assert secret not in serialized
    assert set(result["credentials"][0]) == {
        "label", "status", "plan", "windows", "details", "unavailable_reason", "retry_after",
    }


def test_failed_probe_leaves_only_that_row_without_windows(monkeypatch, tmp_path):
    result, _calls = _quota(
        monkeypatch, tmp_path,
        pool_payloads=[_entry("primary", _CRED_A), _entry("backup", _CRED_B)],
        responses={
            "Bearer " + _AGGREGATE_KEY: _AGGREGATE_RESPONSE,
            "Bearer " + _CRED_A: _CRED_A_RESPONSE,
            # _CRED_B raises: the probe must degrade, not fail the endpoint.
        },
    )

    assert result["status"] == "available"
    primary, backup = result["credentials"]
    assert len(primary["windows"]) == 2
    assert backup["windows"] == []
    assert backup["plan"] is None


@pytest.mark.parametrize("pool_failure", [OSError("unreadable"), None])
def test_missing_or_unreadable_credentials_file_still_answers(monkeypatch, tmp_path, pool_failure):
    result, _calls = _quota(
        monkeypatch, tmp_path,
        pool_payloads=pool_failure if pool_failure is not None else [],
        responses={},  # the aggregate call fails too: a degraded answer, never a 500
    )

    assert result["ok"] is False
    assert result["status"] == "unavailable"
    assert result["account_limits"] is None
    assert "credentials" not in result


def test_probe_results_are_cached_per_credential(monkeypatch, tmp_path):
    import api.providers as providers

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(
        providers, "_BEARER_QUOTA_OPENER",
        SimpleNamespace(open=_opener({"Bearer " + _CRED_A: _CRED_A_RESPONSE}, calls)),
    )
    payloads = [_entry("primary", _CRED_A), _entry("backup", _CRED_B)]
    monkeypatch.setattr(providers, "_pool_entry_payloads", lambda _provider: payloads)

    first = providers._local_pool_snapshot("opencode-go")
    second = providers._local_pool_snapshot("opencode-go")

    assert len(calls) == 2, "both credentials probed once"
    assert first.pool["credentials"][0]["windows"] == second.pool["credentials"][0]["windows"]
    assert len(second.pool["credentials"][0]["windows"]) == 2
