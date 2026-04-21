"""Regression tests for credential_pool provider detection in /api/models."""

import json
import sys
import types

import api.config as config
import api.profiles as profiles

_AMBIENT_SOURCES = {"gh_cli", "gh auth token"}


def _install_fake_hermes_cli(monkeypatch, *, with_load_pool: bool = False, pool_data: dict | None = None):
    """Stub hermes_cli modules so tests are deterministic and offline.

    When *with_load_pool* is True, also stubs hermes_cli.credential_pool with a
    suppression-aware load_pool() implementation that mirrors upstream behaviour:
    entries whose source/label/key_source signals ambient gh-cli auth are filtered out.
    """
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = lambda pid: (
        ["gpt-oss:20b", "qwen3:30b-a3b"] if pid == "ollama-cloud" else []
    )

    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {}

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)

    if with_load_pool:
        _pool_data = pool_data or {}

        class _FakePool:
            def __init__(self, entries_list):
                self._entries = entries_list

            def entries(self):
                return self._entries

        def _fake_load_pool(pid):
            raw = _pool_data.get(pid, [])
            usable = []
            for e in raw:
                src = str(e.get("source", "") or "").strip().lower()
                label = str(e.get("label", "") or "").strip().lower()
                key_src = str(e.get("key_source", "") or "").strip().lower()
                if src in _AMBIENT_SOURCES or label == "gh auth token" or key_src == "gh auth token":
                    continue
                usable.append(e)
            return _FakePool(usable)

        fake_cp = types.ModuleType("agent.credential_pool")
        fake_cp.load_pool = _fake_load_pool
        monkeypatch.setitem(sys.modules, "agent.credential_pool", fake_cp)


def _call_get_available_models(monkeypatch, tmp_path, auth_payload, *, with_load_pool: bool = False):
    """Call get_available_models() with auth.json pinned to a temp Hermes home."""
    _install_fake_hermes_cli(
        monkeypatch,
        with_load_pool=with_load_pool,
        pool_data=auth_payload.get("credential_pool", {}),
    )

    (tmp_path / "auth.json").write_text(json.dumps(auth_payload), encoding="utf-8")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {}
    try:
        # Pin mtime to avoid reload_config() clobbering our in-memory cfg patch.
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0

    try:
        return config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime


def _group_by_provider(result):
    return {g["provider"]: g["models"] for g in result.get("groups", [])}


def test_ollama_cloud_manual_credential_shows_group(monkeypatch, tmp_path):
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "ollama-cloud": [
                {
                    "id": "abc123",
                    "label": "ollama-manual",
                    "source": "manual",
                    "auth_type": "api_key",
                    "base_url": "https://ollama.com/v1",
                }
            ]
        },
    }

    result = _call_get_available_models(monkeypatch, tmp_path, auth_payload)
    groups = _group_by_provider(result)
    assert "Ollama Cloud" in groups, f"Expected Ollama Cloud in {list(groups)}"
    model_ids = [m["id"] for m in groups["Ollama Cloud"]]
    assert model_ids == ["@ollama-cloud:gpt-oss:20b", "@ollama-cloud:qwen3:30b-a3b"], model_ids


def test_copilot_gh_cli_only_credential_hidden(monkeypatch, tmp_path):
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "copilot": [
                {
                    "id": "def456",
                    "label": "gh auth token",
                    "source": "gh_cli",
                    "auth_type": "api_key",
                    "base_url": "https://api.githubcopilot.com",
                }
            ]
        },
    }

    result = _call_get_available_models(monkeypatch, tmp_path, auth_payload)
    groups = _group_by_provider(result)
    assert "GitHub Copilot" not in groups, (
        "GitHub Copilot should be hidden when only ambient gh auth token is present; "
        f"got {list(groups)}"
    )


def test_copilot_mixed_credential_pool_remains_visible(monkeypatch, tmp_path):
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "copilot": [
                {
                    "id": "def456",
                    "label": "gh auth token",
                    "source": "gh_cli",
                    "auth_type": "api_key",
                    "base_url": "https://api.githubcopilot.com",
                },
                {
                    "id": "ghi789",
                    "label": "explicit-copilot",
                    "source": "manual",
                    "auth_type": "api_key",
                    "base_url": "https://api.githubcopilot.com",
                },
            ]
        },
    }

    result = _call_get_available_models(monkeypatch, tmp_path, auth_payload)
    groups = _group_by_provider(result)
    assert "GitHub Copilot" in groups, f"Expected GitHub Copilot in {list(groups)}"
    model_ids = [m["id"] for m in groups["GitHub Copilot"]]
    assert "@copilot:gpt-5.4" in model_ids, model_ids
    assert "@copilot:claude-opus-4.6" in model_ids, model_ids


def test_copilot_empty_field_entries_are_treated_as_explicit(monkeypatch, tmp_path):
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "copilot": [
                {
                    "id": "jkl012",
                }
            ]
        },
    }

    result = _call_get_available_models(monkeypatch, tmp_path, auth_payload)
    groups = _group_by_provider(result)
    assert "GitHub Copilot" in groups, f"Expected GitHub Copilot in {list(groups)}"


def test_copilot_oauth_credential_is_visible(monkeypatch, tmp_path):
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "copilot": [
                {
                    "id": "mno345",
                    "label": "github-oauth",
                    "source": "oauth",
                    "auth_type": "oauth",
                    "base_url": "https://api.githubcopilot.com",
                }
            ]
        },
    }

    result = _call_get_available_models(monkeypatch, tmp_path, auth_payload)
    groups = _group_by_provider(result)
    assert "GitHub Copilot" in groups, f"Expected GitHub Copilot in {list(groups)}"


# --- load_pool path (suppression-aware) ---


def test_load_pool_copilot_ambient_only_remains_hidden(monkeypatch, tmp_path):
    """load_pool path: copilot with only ambient gh-cli entries is suppressed."""
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "copilot": [
                {
                    "id": "lp001",
                    "label": "gh auth token",
                    "source": "gh_cli",
                    "auth_type": "api_key",
                    "base_url": "https://api.githubcopilot.com",
                }
            ]
        },
    }

    result = _call_get_available_models(monkeypatch, tmp_path, auth_payload, with_load_pool=True)
    groups = _group_by_provider(result)
    assert "GitHub Copilot" not in groups, (
        "GitHub Copilot must be hidden when load_pool returns no usable entries; "
        f"got {list(groups)}"
    )


def test_load_pool_explicit_credential_shows_provider(monkeypatch, tmp_path):
    """load_pool path: provider with at least one explicit entry is visible."""
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "copilot": [
                {
                    "id": "lp002",
                    "label": "gh auth token",
                    "source": "gh_cli",
                    "auth_type": "api_key",
                    "base_url": "https://api.githubcopilot.com",
                },
                {
                    "id": "lp003",
                    "label": "explicit-pat",
                    "source": "manual",
                    "auth_type": "api_key",
                    "base_url": "https://api.githubcopilot.com",
                },
            ]
        },
    }

    result = _call_get_available_models(monkeypatch, tmp_path, auth_payload, with_load_pool=True)
    groups = _group_by_provider(result)
    assert "GitHub Copilot" in groups, (
        f"GitHub Copilot must appear when load_pool has at least one usable entry; got {list(groups)}"
    )
