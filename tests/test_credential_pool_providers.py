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

    # Always remove the real agent.credential_pool so get_available_models() takes
    # the ImportError fallback path and reads from the monkeypatched auth store,
    # not the live ~/.hermes/auth.json via the real venv module.
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)

    if with_load_pool:
        _pool_data = pool_data or {}

        class _FakeEntry:
            """Minimal PooledCredential stand-in with attribute access (matching the real class)."""
            def __init__(self, d):
                self.source = d.get("source", "manual")
                self.label = d.get("label", "")
                self.key_source = d.get("key_source", "")
                self.id = d.get("id", "")
                self.runtime_api_key = (d.get("runtime_api_key") or d.get("access_token") or "")
                self.access_token = (d.get("access_token") or "")
                self.base_url = d.get("base_url", "")

        class _FakePool:
            def __init__(self, entries_list):
                self._entries = entries_list

            def entries(self):
                return self._entries

            def select(self):
                return self._entries[0] if self._entries else None

        def _fake_load_pool(pid):
            # Return ALL entries without filtering — mirrors the real load_pool()
            # which does NOT suppress ambient gh-cli tokens on its own.
            # Ambient-source filtering is the webui's responsibility.
            raw = _pool_data.get(pid, [])
            return _FakePool([_FakeEntry(e) for e in raw])

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

    config.invalidate_models_cache()
    try:
        return config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config.invalidate_models_cache()


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


def test_load_pool_copilot_ambient_key_source_only_remains_hidden(monkeypatch, tmp_path):
    """load_pool path: key_source-only ambient markers must also be suppressed."""
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "copilot": [
                {
                    "id": "lp001b",
                    "label": "copilot-token",
                    "source": "manual",
                    "key_source": "gh auth token",
                    "auth_type": "api_key",
                    "base_url": "https://api.githubcopilot.com",
                }
            ]
        },
    }

    result = _call_get_available_models(monkeypatch, tmp_path, auth_payload, with_load_pool=True)
    groups = _group_by_provider(result)
    assert "GitHub Copilot" not in groups, (
        "GitHub Copilot must stay hidden when load_pool entries only differ by key_source ambient markers; "
        f"got {list(groups)}"
    )


def test_load_pool_alias_provider_key_is_resolved(monkeypatch, tmp_path):
    """load_pool path: aliased pool keys should resolve to canonical provider ids."""
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "google": [
                {
                    "id": "gp001",
                    "label": "explicit-gemini",
                    "source": "manual",
                    "auth_type": "api_key",
                    "base_url": "https://generativelanguage.googleapis.com",
                }
            ]
        },
    }

    result = _call_get_available_models(monkeypatch, tmp_path, auth_payload, with_load_pool=True)
    groups = _group_by_provider(result)
    assert "Gemini" in groups, f"Expected Gemini in {list(groups)}"
    assert "Google" not in groups, f"Aliased provider key should not render under raw alias name: {list(groups)}"


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


# --- _apply_provider_prefix helper ---


def test_apply_provider_prefix_ollama_cloud_non_active():
    """Bare ollama-cloud model ids get @ollama-cloud: prefix when not active."""
    from api.config import _apply_provider_prefix

    raw = [{"id": "gpt-oss:20b", "label": "gpt-oss:20b"}, {"id": "qwen3:30b-a3b", "label": "qwen3:30b-a3b"}]
    result = _apply_provider_prefix(raw, "ollama-cloud", "openai-codex")
    ids = [m["id"] for m in result]
    assert ids == ["@ollama-cloud:gpt-oss:20b", "@ollama-cloud:qwen3:30b-a3b"], ids


def test_apply_provider_prefix_copilot_non_active():
    """Bare copilot model ids get @copilot: prefix when not active."""
    from api.config import _apply_provider_prefix

    raw = [{"id": "gpt-5.4", "label": "GPT-5.4"}, {"id": "claude-opus-4.6", "label": "Claude Opus 4.6"}]
    result = _apply_provider_prefix(raw, "copilot", "openai-codex")
    ids = [m["id"] for m in result]
    assert ids == ["@copilot:gpt-5.4", "@copilot:claude-opus-4.6"], ids


def test_apply_provider_prefix_no_double_prefix():
    """Already-prefixed or provider/model ids are not double-prefixed."""
    from api.config import _apply_provider_prefix

    raw = [
        {"id": "@copilot:gpt-5.4", "label": "already prefixed", "supports_fast_tier": True},
        {"id": "openai/gpt-5.4", "label": "slash form", "supports_fast_tier": True},
        {"id": "bare-model", "label": "bare", "supports_fast_tier": False},
    ]
    result = _apply_provider_prefix(raw, "copilot", "openai-codex")
    ids = [m["id"] for m in result]
    assert ids == ["@copilot:gpt-5.4", "openai/gpt-5.4", "@copilot:bare-model"], ids
    assert [m["supports_fast_tier"] for m in result] == [True, True, False]


def test_apply_provider_prefix_active_provider_no_prefix():
    """No prefix is added when the provider is already the active one."""
    from api.config import _apply_provider_prefix

    raw = [{"id": "gpt-5.4", "label": "GPT-5.4"}]
    result = _apply_provider_prefix(raw, "openai-codex", "openai-codex")
    ids = [m["id"] for m in result]
    assert ids == ["gpt-5.4"], ids


def test_copilot_mixed_pool_prefixed_models(monkeypatch, tmp_path):
    """Copilot with mixed pool and non-active provider has @copilot: prefixed model ids."""
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "copilot": [
                {
                    "id": "lp010",
                    "label": "explicit-copilot",
                    "source": "manual",
                    "auth_type": "api_key",
                    "base_url": "https://api.githubcopilot.com",
                }
            ]
        },
    }

    result = _call_get_available_models(monkeypatch, tmp_path, auth_payload)
    groups = _group_by_provider(result)
    assert "GitHub Copilot" in groups
    model_ids = [m["id"] for m in groups["GitHub Copilot"]]
    assert all(mid.startswith("@copilot:") for mid in model_ids), model_ids


def test_auth_store_active_provider_alias_is_resolved(monkeypatch, tmp_path):
    """active_provider read from auth.json must be alias-normalized.

    Regression: previously the alias table was applied only to config.yaml's
    active_provider, so an aliased name in auth.json (e.g. 'google') would
    not match the canonical pid ('gemini') and the prefixing logic would
    add an unwanted '@gemini:' prefix to the active provider's models.
    """
    auth_payload = {
        "version": 1,
        "providers": {},
        # Aliased name: 'google' → 'gemini' per _PROVIDER_ALIASES.
        "active_provider": "google",
        "credential_pool": {},
    }

    result = _call_get_available_models(monkeypatch, tmp_path, auth_payload)
    groups = _group_by_provider(result)
    # Gemini should appear under its canonical display name and its model
    # ids should NOT be prefixed (it's the active provider).
    assert "Gemini" in groups, f"Expected Gemini in {list(groups)}"
    model_ids = [m["id"] for m in groups["Gemini"]]
    assert model_ids, "Gemini group should have models"
    assert not any(mid.startswith("@") for mid in model_ids), (
        f"Active provider models must not be prefixed; got {model_ids}"
    )


def test_ollama_cloud_empty_catalog_skips_group(monkeypatch, tmp_path):
    """When hermes_cli returns no models for ollama-cloud, the group is omitted.

    Matches the named-custom and unknown-provider branches: we don't invent a
    catalog we can't enumerate. The logger.warning in the except branch keeps
    diagnostics available for operators.
    """
    _install_fake_hermes_cli(monkeypatch)

    # Override the stub to return empty for ollama-cloud.
    import sys as _sys
    _sys.modules["hermes_cli.models"].provider_model_ids = lambda pid: []

    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "ollama-cloud": [
                {
                    "id": "oc-empty",
                    "label": "ollama-manual",
                    "source": "manual",
                    "auth_type": "api_key",
                }
            ]
        },
    }

    (tmp_path / "auth.json").write_text(json.dumps(auth_payload), encoding="utf-8")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {}
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0

    try:
        result = config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime

    groups = _group_by_provider(result)
    assert "Ollama Cloud" not in groups, (
        f"Ollama Cloud group should be skipped when catalog is empty; got {list(groups)}"
    )


# --- _format_ollama_label helper ---


def test_format_ollama_label_simple():
    from api.config import _format_ollama_label

    assert _format_ollama_label("kimi-k2.5") == "Kimi K2.5"


def test_format_ollama_label_with_variant():
    from api.config import _format_ollama_label

    assert _format_ollama_label("qwen3-vl:235b-instruct") == "Qwen3 VL (235B Instruct)"


def test_format_ollama_label_short_acronym():
    from api.config import _format_ollama_label

    assert _format_ollama_label("glm-5.1") == "GLM 5.1"


def test_format_ollama_label_gpt_oss_with_size():
    from api.config import _format_ollama_label

    assert _format_ollama_label("gpt-oss:20b") == "GPT OSS (20B)"


def test_format_ollama_label_empty_string():
    from api.config import _format_ollama_label

    assert _format_ollama_label("") == ""


def test_format_ollama_label_no_variant():
    from api.config import _format_ollama_label

    assert _format_ollama_label("nemotron-3-super") == "Nemotron 3 Super"


# --- Fallback-path (ImportError branch) alias resolution ---


def test_fallback_path_resolves_alias_when_load_pool_unavailable(monkeypatch, tmp_path):
    """When agent.credential_pool can't be imported, the manual-inspection
    branch must still canonicalize pool keys so aliased names (e.g. 'google')
    end up under their canonical provider id ('gemini')."""
    _install_fake_hermes_cli(monkeypatch)
    # Ensure agent.credential_pool is not importable so the fallback branch runs.
    monkeypatch.setitem(sys.modules, "agent.credential_pool", None)

    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "google": [
                {
                    "id": "gp-fallback",
                    "label": "explicit-gemini",
                    "source": "manual",
                    "auth_type": "api_key",
                }
            ]
        },
    }

    (tmp_path / "auth.json").write_text(json.dumps(auth_payload), encoding="utf-8")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {}
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0

    try:
        result = config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime

    groups = _group_by_provider(result)
    assert "Gemini" in groups, (
        f"Fallback path must resolve 'google' -> 'gemini'; got {list(groups)}"
    )
    assert "Google" not in groups, (
        f"Raw alias name must not leak when fallback path runs; got {list(groups)}"
    )


# ── New tests for credential-pool changes (code-review #4247 follow-ups) ──


# --- Regression: ambient gh_cli MUST NOT mark copilot as configured ---


def test_ambient_gh_cli_not_detectable_by_provider_has_key(monkeypatch, tmp_path):
    """Regression: _provider_has_key('copilot') must return False when only
    ambient gh auth token (gh_cli) is in the pool."""
    _install_fake_hermes_cli(monkeypatch, with_load_pool=True, pool_data={
        "copilot": [
            {"id": "ambient-1", "label": "gh auth token", "source": "gh_cli", "auth_type": "api_key"},
        ],
    })
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    from api.providers import _provider_has_key

    assert _provider_has_key("copilot") is False, (
        "copilot must NOT be marked as configured when only ambient gh_cli token exists"
    )


def test_ambient_gh_env_not_detectable_by_provider_has_key(monkeypatch, tmp_path):
    """Regression: _provider_has_key('copilot') must return False when only
    GITHUB_TOKEN env-variable entry is in the pool."""
    _install_fake_hermes_cli(monkeypatch, with_load_pool=True, pool_data={
        "copilot": [
            {"id": "env-1", "label": "env-token", "source": "env:github_token", "auth_type": "api_key"},
        ],
    })
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    from api.providers import _provider_has_key

    assert _provider_has_key("copilot") is False, (
        "copilot must NOT be marked as configured when only GITHUB_TOKEN env entry exists"
    )


def test_ambient_key_source_not_detectable_by_provider_has_key(monkeypatch, tmp_path):
    """Regression: _provider_has_key('copilot') must return False when only
    key_source='gh auth token' entries exist."""
    _install_fake_hermes_cli(monkeypatch, with_load_pool=True, pool_data={
        "copilot": [
            {
                "id": "key-src-1", "label": "copilot-pat",
                "source": "manual", "key_source": "gh auth token",
                "auth_type": "api_key",
            },
        ],
    })
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    from api.providers import _provider_has_key

    assert _provider_has_key("copilot") is False, (
        "copilot must NOT be marked as configured when only key_source=gh auth token entry exists"
    )


# --- Positive: custom:* providers with explicit credentials are detected ---


def test_custom_provider_explicit_credential_detected_by_provider_has_key(monkeypatch, tmp_path):
    """Positive: custom:bothub with explicit manual entry must show _provider_has_key=True."""
    _install_fake_hermes_cli(monkeypatch, with_load_pool=True, pool_data={
        "custom:bothub": [
            {
                "id": "bothub-1",
                "label": "bothub-key",
                "source": "manual",
                "auth_type": "api_key",
                "runtime_api_key": "sk-bh...test",
                "base_url": "https://bothub.chat/v1",
            },
        ],
    })
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    from api.providers import _provider_has_key

    assert _provider_has_key("custom:bothub") is True, (
        "custom:bothub with explicit pool entry must be detected as configured"
    )


def test_custom_provider_detected_by_get_providers(monkeypatch, tmp_path):
    """Positive: custom:bothub must appear in get_providers() with has_key=True."""
    config._CREDENTIAL_POOL_CACHE.clear()

    _install_fake_hermes_cli(monkeypatch, with_load_pool=True, pool_data={
        "custom:bothub": [
            {
                "id": "bothub-prov-1",
                "label": "bothub-key",
                "source": "manual",
                "auth_type": "api_key",
                "runtime_api_key": "sk-bh...test",
                "base_url": "https://bothub.chat/v1",
            },
        ],
    })
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    # Custom providers need to be in config.yaml for get_providers() to list them
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {"provider": "custom:bothub"}
    config.cfg["custom_providers"] = [
        {
            "name": "bothub",
            "api_key": "sk-bh...test",
            "base_url": "https://bothub.chat/v1",
            "display_name": "Bothub",
        },
    ]
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    config.invalidate_models_cache()

    try:
        from api.providers import get_providers

        providers = get_providers()
        bothub = None
        for p in providers["providers"]:
            if p["id"] == "custom:bothub":
                bothub = p
                break
        assert bothub is not None, (
            f"custom:bothub must appear in get_providers(); "
            f"got {[p['id'] for p in providers['providers']]}"
        )
        assert bothub["has_key"] is True, "custom:bothub must show has_key=True"
    finally:
        config.invalidate_models_cache()
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime


def test_custom_provider_detected_by_get_available_models(monkeypatch, tmp_path):
    """Positive: custom provider with explicit pool credentials must appear as a group."""
    config._CREDENTIAL_POOL_CACHE.clear()

    _install_fake_hermes_cli(monkeypatch, with_load_pool=True, pool_data={
        "custom:bothub": [
            {
                "id": "bothub-mod-1",
                "label": "bothub-key",
                "source": "manual",
                "auth_type": "api_key",
                "runtime_api_key": "sk-bh...test",
                "base_url": "https://bothub.chat/v1",
            },
        ],
    })
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    # Need both auth.json (for pool) and custom_providers config (for model enumeration)
    (tmp_path / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
    }), encoding="utf-8")

    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {}
    config.cfg["custom_providers"] = [
        {
            "name": "bothub",
            "api_key": "sk-bh...test",
            "base_url": "https://bothub.chat/v1",
            "display_name": "Bothub",
        },
    ]
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    config.invalidate_models_cache()

    try:
        result = config.get_available_models()
    finally:
        config.invalidate_models_cache()
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime

    groups = _group_by_provider(result)
    assert "bothub" in groups, (
        f"Bothub must appear as a group in get_available_models; got {list(groups)}"
    )
    # Models may be empty since there's no real endpoint in test — the key is
    # that the group appears at all (credential pool detection works).


# --- OAuth token order: runtime_api_key must be preferred over access_token ---


def test_get_provider_api_key_prefers_runtime_api_key_over_access_token(monkeypatch, tmp_path):
    """_get_provider_api_key must return runtime_api_key when both access_token
    and runtime_api_key are present (runtime_api_key has priority)."""
    _install_fake_hermes_cli(monkeypatch, with_load_pool=True, pool_data={
        "custom:bothub": [
            {
                "id": "tok-order-1",
                "label": "bothub-key",
                "source": "manual",
                "auth_type": "api_key",
                "access_token": "sk-old-access-token",
                "runtime_api_key": "sk-preferred-runtime-key",
                "base_url": "https://bothub.chat/v1",
            },
        ],
    })
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    from api.providers import _get_provider_api_key

    key = _get_provider_api_key("custom:bothub")
    assert key == "sk-preferred-runtime-key", (
        f"runtime_api_key must take priority; got {key!r}"
    )


def test_get_provider_api_key_falls_back_to_access_token(monkeypatch, tmp_path):
    """_get_provider_api_key must fall back to access_token when runtime_api_key is absent."""
    _install_fake_hermes_cli(monkeypatch, with_load_pool=True, pool_data={
        "custom:bothub": [
            {
                "id": "tok-fallback-1",
                "label": "bothub-key",
                "source": "manual",
                "auth_type": "api_key",
                "access_token": "sk-fallback-access-token",
                "base_url": "https://bothub.chat/v1",
            },
        ],
    })
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    from api.providers import _get_provider_api_key

    key = _get_provider_api_key("custom:bothub")
    assert key == "sk-fallback-access-token", (
        f"access_token must be used when runtime_api_key absent; got {key!r}"
    )


# --- _has_explicit_pool_credentials unit tests ---


def test_has_explicit_pool_credentials_ambient_only_is_false(monkeypatch, tmp_path):
    """_has_explicit_pool_credentials must return False when only ambient entries exist."""
    _install_fake_hermes_cli(monkeypatch, with_load_pool=True, pool_data={
        "copilot": [
            {"id": "u-amb-1", "label": "gh auth token", "source": "gh_cli", "auth_type": "api_key"},
        ],
    })
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    config._CREDENTIAL_POOL_CACHE.clear()

    from api.config import _has_explicit_pool_credentials

    assert _has_explicit_pool_credentials("copilot") is False


def test_has_explicit_pool_credentials_explicit_is_true(monkeypatch, tmp_path):
    """_has_explicit_pool_credentials must return True when at least one explicit entry exists."""
    _install_fake_hermes_cli(monkeypatch, with_load_pool=True, pool_data={
        "custom:bothub": [
            {
                "id": "u-exp-1", "label": "bothub-key", "source": "manual",
                "auth_type": "api_key", "runtime_api_key": "sk-test",
                "base_url": "https://bothub.chat/v1",
            },
        ],
    })
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    config._CREDENTIAL_POOL_CACHE.clear()

    from api.config import _has_explicit_pool_credentials

    assert _has_explicit_pool_credentials("custom:bothub") is True


def test_has_explicit_pool_credentials_mixed_is_true(monkeypatch, tmp_path):
    """_has_explicit_pool_credentials must return True when both ambient and explicit entries exist."""
    _install_fake_hermes_cli(monkeypatch, with_load_pool=True, pool_data={
        "copilot": [
            {"id": "u-mix-amb", "label": "gh auth token", "source": "gh_cli", "auth_type": "api_key"},
            {
                "id": "u-mix-exp", "label": "explicit-pat", "source": "manual",
                "auth_type": "api_key", "runtime_api_key": "sk-pat",
                "base_url": "https://api.githubcopilot.com",
            },
        ],
    })
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    config._CREDENTIAL_POOL_CACHE.clear()

    from api.config import _has_explicit_pool_credentials

    assert _has_explicit_pool_credentials("copilot") is True


def test_has_explicit_pool_credentials_resolves_alias(monkeypatch, tmp_path):
    """_has_explicit_pool_credentials must resolve provider aliases (google -> gemini)."""
    _install_fake_hermes_cli(monkeypatch, with_load_pool=True, pool_data={
        # Pool data is stored under canonical ID 'gemini' after alias resolution
        "gemini": [
            {
                "id": "u-alias-1", "label": "gemini-key", "source": "manual",
                "auth_type": "api_key", "runtime_api_key": "sk-gemini",
                "base_url": "https://generativelanguage.googleapis.com",
            },
        ],
    })
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    config._CREDENTIAL_POOL_CACHE.clear()

    # Should work with both alias 'google' and canonical 'gemini'
    from api.config import _has_explicit_pool_credentials

    assert _has_explicit_pool_credentials("google") is True, (
        "alias 'google' should resolve to canon 'gemini' and find pool entry"
    )
    assert _has_explicit_pool_credentials("gemini") is True, (
        "canon 'gemini' should find pool entry directly"
    )


def test_has_explicit_pool_credentials_import_error_is_false(monkeypatch, tmp_path):
    """_has_explicit_pool_credentials must return False when load_pool not available."""
    _install_fake_hermes_cli(monkeypatch)  # no with_load_pool — agent.credential_pool is removed
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    config._CREDENTIAL_POOL_CACHE.clear()

    from api.config import _has_explicit_pool_credentials

    assert _has_explicit_pool_credentials("copilot") is False